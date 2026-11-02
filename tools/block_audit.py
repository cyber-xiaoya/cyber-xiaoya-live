"""block_audit.py — 黑名单体检(2026-10-20): 拿最近的真实弹幕做"拦 vs 不拦"对比。

只读 reader.log, 不碰线上。输出:
  · 近 N 分钟共多少条弹幕
  · 其中命中黑名单的有多少条(全部会被拦), 逐条列出
  · 如果**没有**黑名单, 这些弹幕本来会触发哪些动作(用来确认黑名单挡住的正是该挡的)
  · 顺带统计动作类弹幕占比、会被 T2 兜底的条数

用法: .venv\\Scripts\\python.exe tools\\block_audit.py [--minutes 120] [--max 3000]
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

READER_LOG = pathlib.Path(r"<弹幕采集目录>\logs\reader.log")
NAMES_PATH = ROOT / "tools" / "replay-names.json"
CHAT_RE = re.compile(r"^(\d\d:\d\d:\d\d) \[([^\]]+)\] Chat: ([^:]+): (.*)$")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=120, help="只看最近多少分钟")
    ap.add_argument("--max", type=int, default=4000, help="最多分析多少行")
    args = ap.parse_args()

    import json
    from src import dm_router

    names = json.loads(NAMES_PATH.read_text(encoding="utf-8"))["names"]
    r = dm_router.DanmakuRouter()
    r._names = set(names)
    r.init(None, fetch_names=None,
           conf_get=lambda: {"enabled": True, "mode": "auto", "cooldown_s": 0,
                             "read_interval_s": 4, "same_action_windows": [0, 0, 0]})
    r._trusted = {}
    r._load_aliases()

    # 读最近 N 分钟(按行尾倒着扫, 用最后一条的时间当"现在")
    lines: list[tuple[str, str, str]] = []          # (ts, plat, content)
    tail = collections.deque(maxlen=args.max)
    with READER_LOG.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = CHAT_RE.match(line.rstrip("\n"))
            if m:
                tail.append((m.group(1), m.group(2), m.group(4).strip()))
    if not tail:
        print("  reader.log 里没有弹幕")
        return 1
    lines = list(tail)

    def ts2dt(ts: str) -> datetime:
        h, mi, s = (int(x) for x in ts.split(":"))
        now = datetime.now()
        d = now.replace(hour=h, minute=mi, second=s, microsecond=0)
        if d > now:
            d -= timedelta(days=1)
        return d

    cutoff = ts2dt(lines[-1][0]) - timedelta(minutes=args.minutes)
    recent = [x for x in lines if ts2dt(x[0]) >= cutoff]

    blocked: list[tuple[str, str, str]] = []        # (content, 命中词, 不拦会发什么)
    fire = 0
    need_t2 = 0
    silent = 0
    for ts, plat, content in recent:
        bw = r.blocked(content)
        comp = r.composite(content)
        toy = r._prop_rewrite(content)
        hit = r.match(content)
        would = comp or toy or hit
        if bw:
            blocked.append((content, bw, would or "（本来也不触发）"))
            continue
        if would:
            fire += 1
        elif r.looks_like_cmd(content):
            silent += 1
        elif any(w in content for w in r._fw()) or dm_router.SHELL_RE.search(content):
            need_t2 += 1

    print(f"\n[黑名单体检] 时间窗 = 最近 {args.minutes} 分钟（{ts2dt(lines[-1][0]):%H:%M} 往前推）")
    print(f"  弹幕总数: {len(recent)} 条")
    print(f"  🚫 命中黑名单: {len(blocked)} 条  ← 全部被拦，不会进 Vidu")
    print(f"  ✅ 正常动作触发: {fire} 条")
    print(f"  💬 进 T2 判定: {need_t2} 条 · 动作类但不复读: {silent} 条")
    if blocked:
        print("\n  命中明细（观众原话 → 命中词 → 不拦会执行的动作）:")
        cnt = collections.Counter(b[1] for b in blocked)
        print("  词频: " + " · ".join(f"{w}×{n}" for w, n in cnt.most_common()))
        print()
        for content, bw, would in blocked[:25]:
            print(f"    「{content[:26]:<26}」 ← {bw:<4} → {would[:24]}")
        if len(blocked) > 25:
            print(f"    …还有 {len(blocked) - 25} 条")
    else:
        print("\n  本时间窗内没人发站/腿相关弹幕。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

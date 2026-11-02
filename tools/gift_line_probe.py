"""gift_line_probe.py — 只验"礼物合并感谢"这一句的生成质量(不连数字人, 不花积分)。

做三件事:
  1. 从真实历史礼物事件里按时间轴切出**真实窗口**(6s / ≥4 人 / ≥50 钻), 拿真礼包去问模型;
  2. 打印"喂进去的 system + user 原文", 以及模型**真实输出**;
  3. 连跑同一礼包 3 次, 看"最近 3 条话术"防重复有没有生效。

用法: .venv\\Scripts\\python.exe tools\\gift_line_probe.py
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

READER_LOG = pathlib.Path(r"<弹幕采集目录>\logs\reader.log")
GIFT_RE = re.compile(r"^(\d\d:\d\d:\d\d) \[([^\]]+)\] Gift: (.+?) 赠送 (.+?)(?: x(\d+))?(?: \(|$)")


def load_real_gifts() -> list[dict]:
    out: list[dict] = []
    with READER_LOG.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = GIFT_RE.match(line.rstrip("\n"))
            if not m:
                continue
            h, mi, s = (int(x) for x in m.group(1).split(":"))
            out.append({"t": h * 3600 + mi * 60 + s, "nick": m.group(3).strip(),
                        "item": m.group(4).strip(), "num": int(m.group(5) or 1)})
    return out


def split_windows(evs: list[dict], window: float = 6, people_hi: int = 4) -> list[list[dict]]:
    """按线上规则切窗口: 从第一条起 6s, 期间到 4 人就立刻切。"""
    out: list[list[dict]] = []
    buf: list[dict] = []
    start = None
    for e in evs:
        if not buf:
            start = e["t"]
        buf.append(e)
        people = len({x["nick"] for x in buf})
        if people >= people_hi or (e["t"] - start) >= window:
            out.append(buf)
            buf = []
    if buf:
        out.append(buf)
    return out


def to_gift_payload(batch: list[dict]) -> list[dict]:
    """把窗口内的原始事件合并成桥侧收到的礼包形态(同人同物累加数量)。"""
    merged: dict[tuple[str, str], dict] = {}
    for e in batch:
        key = (e["nick"], e["item"])
        if key in merged:
            merged[key]["num"] += e["num"]
        else:
            merged[key] = {"nick": e["nick"], "item": e["item"], "num": e["num"], "value": 0}
    return list(merged.values())


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass
    from src.engine import Engine
    eng = Engine()

    evs = load_real_gifts()
    wins = split_windows(evs)
    print(f"[probe] 真实礼物事件 {len(evs)} 条 → 按线上规则切成 {len(wins)} 个窗口")

    # 挑几种典型窗口: 1人 / 2人 / 3人 / 4人(阈值) / 6人(超出点名上限)
    def pick(pred, limit=1):
        got = []
        for w in wins:
            p = to_gift_payload(w)
            if pred(p) and len(p) <= 12:
                got.append(p)
                if len(got) >= limit:
                    return got
        return got

    cases: list[tuple[str, list[dict]]] = []
    cases += [("① 单人小额（真实历史窗口）", c) for c in pick(lambda p: len({x['nick'] for x in p}) == 1)]
    cases += [("② 两人（真实历史窗口）", c) for c in pick(lambda p: len({x['nick'] for x in p}) == 2)]
    cases += [("③ 四人（刚好触发立即发）", c) for c in pick(lambda p: len({x['nick'] for x in p}) == 4)]
    cases += [("④ 六人（超出 4 个点名上限）", c) for c in pick(lambda p: len({x['nick'] for x in p}) == 6)]
    # 人工构造两个极端: 高价单人 / 同人连击
    cases.append(("⑤ 单人高价（跑车 60 钻）",
                  [{"nick": "土豪大哥", "item": "跑车", "num": 1, "value": 60}]))
    cases.append(("⑥ 同人连击（10 个玫瑰）",
                  [{"nick": "阿糖不甜", "item": "玫瑰", "num": 10, "value": 10}]))

    print("\n" + "=" * 78)
    for title, payload in cases:
        # 复现桥侧组装: 名字缩写 + 人数/超限
        names: list[str] = []
        for e in payload:
            nm = eng._short_nick(e["nick"])
            if nm and nm not in names:
                names.append(nm)
        shown = names[:4]
        extra = max(0, len(names) - len(shown))
        print(f"\n【{title}】礼包 {len(payload)} 条 / {len(names)} 人")
        for e in payload[:8]:
            print(f"   · {eng._short_nick(e['nick'])} 送了 {eng._gift_phrase(e['item'], e['num'])}")
        if extra:
            print(f"   · 另有 {extra} 位朋友（不说名字）")
        line = await eng._gift_llm(payload, shown, extra) or eng._gift_fallback(payload, shown, extra)
        print(f"   → 模型输出：{line}")
        eng._gift_recent.append(line)

    print("\n" + "=" * 78)
    print("【⑦ 防重复：同一礼包连说 3 次】")
    payload = [{"nick": "阿糖不甜", "item": "小星星", "num": 1, "value": 1},
               {"nick": "张三", "item": "花", "num": 2, "value": 2}]
    names = [eng._short_nick(e["nick"]) for e in payload]
    for i in range(3):
        line = await eng._gift_llm(payload, names[:4], 0) or "(空)"
        print(f"   第{i + 1}次：{line}")
        eng._gift_recent.append(line)
    print("\n【最近话术缓存】")
    for r in eng._gift_recent:
        print("   · " + r)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

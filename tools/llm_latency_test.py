# LLM 延迟对比 v2: 模拟一条直播间弹幕 → 首字/完成延迟
# 两个配置交错跑(消除时段负载波动), 末尾加 flash+开思考 对照(演示为何必须关)
import asyncio
import json
import pathlib
import sys
import time

import httpx

try:
    import certifi
    _VERIFY = certifi.where()
except Exception:
    _VERIFY = True

KEY_FILE = r"key.txt"
SYSTEM = "你是一个口语化的中文数字人助手。"
PROMPT = ("【直播间弹幕·抖音】观众「星河入梦」说：主播晚上好呀，今天唱的这首歌是什么心情呀\n"
          "（用主播的口吻自然接话：可以叫他的名字，一到两句就够，不要复述他说的话）")

CFG_CHAT = {"name": "deepseek-chat(遗留别名)", "model": "deepseek-chat", "extra": {}}
CFG_FLASH_OFF = {"name": "flash+思考关", "model": "deepseek-flash",
                 "extra": {"thinking": {"type": "disabled"}}}
CFG_FLASH_ON = {"name": "flash+思考开(对照)", "model": "deepseek-flash",
                "extra": {"thinking": {"type": "enabled"}}}


async def one_run(model: str, extra: dict, key: str):
    body = {"model": model, "stream": True, "temperature": 0.7, "max_tokens": 300,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": PROMPT}],
            **extra}
    t0 = time.perf_counter()
    ttfb = total = None
    chars = 0
    reply = []
    async with httpx.AsyncClient(verify=_VERIFY, timeout=httpx.Timeout(30.0, read=120.0)) as c:
        async with c.stream("POST", "https://api.deepseek.com/chat/completions",
                            headers={"Authorization": f"Bearer {key}"},
                            json=body) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200].decode('utf-8','ignore')}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                p = line[5:].strip()
                if p == "[DONE]":
                    total = time.perf_counter() - t0
                    break
                try:
                    delta = json.loads(p)["choices"][0]["delta"]
                except Exception:
                    continue
                content = delta.get("content") or ""
                if content:
                    if ttfb is None:
                        ttfb = time.perf_counter() - t0
                    chars += len(content)
                    reply.append(content)
    return {"ttfb": ttfb, "total": total or (time.perf_counter() - t0), "chars": chars,
            "text": "".join(reply)}


def stat(rows):
    if not rows:
        return "无成功样本"
    tt = [r["total"] for r in rows]
    tf = [r["ttfb"] for r in rows if r["ttfb"] is not None]
    return (f"完成 均值 {sum(tt)/len(tt):.2f}s (min {min(tt):.2f} / max {max(tt):.2f})  "
            f"首字 均值 {sum(tf)/len(tf):.2f}s")


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    key = pathlib.Path(KEY_FILE).read_text(encoding="utf-8").strip()
    print(f"每配置 {n} 轮, 交错执行(同一时段同负载)…\n")
    rows = {CFG_CHAT["name"]: [], CFG_FLASH_OFF["name"]: []}
    for i in range(n):
        for cfg in (CFG_CHAT, CFG_FLASH_OFF):
            try:
                r = await one_run(cfg["model"], cfg["extra"], key)
                rows[cfg["name"]].append(r)
                note = "" if r["chars"] > 0 else "  ← ⚠️ 空回复!"
                print(f"  轮{i+1} {cfg['name']:18s} 完成 {r['total']:.2f}s  {r['chars']}字{note}")
            except Exception as e:  # noqa: BLE001
                print(f"  轮{i+1} {cfg['name']:18s} 失败: {type(e).__name__}: {str(e)[:120]}")
    print(f"\n══ 汇总 ══")
    for name, rs in rows.items():
        print(f"  {name:18s} {stat(rs)}")
    # 对照: flash 开思考(生产会踩的坑)
    print(f"\n── 对照组: {CFG_FLASH_ON['name']} × 2 (不交错) ──")
    for i in range(2):
        try:
            r = await one_run(CFG_FLASH_ON["model"], CFG_FLASH_ON["extra"], key)
            note = "" if r["chars"] > 0 else "  ← ⚠️ 空回复(思维链吃光 max_tokens)!"
            print(f"  对照{i+1} 完成 {r['total']:.2f}s  {r['chars']}字 | {r['text'][:40]}{note}")
        except Exception as e:  # noqa: BLE001
            print(f"  对照{i+1} 失败: {type(e).__name__}: {str(e)[:120]}")


asyncio.run(main())

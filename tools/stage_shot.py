#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage_shot.py — 用 Edge 的 CDP 给舞台页拍一张"工作状态"截图。

做法：起无头 Edge（远程调试）→ 等页面加载 → 注入几行示例字幕 + 打开「快捷指令」面板 →
Page.captureScreenshot 存 PNG。注入的只是展示用的示例数据，不会写进仓库的任何配置。
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
URL = "http://127.0.0.1:8795/stage?right=560"
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else r"<输出目录>\cyber-xiaoya-live\assets\stage-panel.png")

JS = r"""
(() => {
  const log = [];
  try { for (let i = 1; i < 5000; i++) clearInterval(i); log.push('timers cleared'); } catch (e) {}
  try {
    // 1) 藏掉"未连线"遮罩（截图用，不改变真实状态）
    const m = document.getElementById('mask'); if (m) { m.style.display = 'none'; log.push('mask hidden'); }
    // 2) 示例字幕/事件
    if (typeof trAdd === 'function') {
      trAdd('sys', '本场已连线 · 托管 RTC');
      trAdd('dh', '好呀，那我把头发扎起来给你们看，三、二、一 ——');
      trAdd('user', '【弹幕】小财迷简报：扎个马尾');
      trAdd('sys', '动作指令：扎高马尾（弹幕触发）');
      trAdd('dh', '诶，这样是不是精神多了？');
      trAdd('user', '【弹幕】M78星云的MADAO：livetalking 做的吗');
      trAdd('dh', '不是哦，这套是开源项目，本地跑的，等下把地址发你～');
      log.push('trAdd ok');
    }
    // 2b) 顶栏状态胶囊也对齐（否则会出现"未连线 + 有字幕"的矛盾画面）
    const pill = document.getElementById('phasePill');
    if (pill) { pill.textContent = '直播中 · 托管RTC'; pill.className = 'pill live'; log.push('pill ok'); }
    // 3) 打开快捷指令面板
    const c = document.getElementById('btnCmds'); if (c) { c.click(); log.push('cmds opened'); }
    // 4) 动作指令特效
    if (typeof cmdFxRender === 'function') {
      cmdFxRender({
        last: { text: '扎高马尾', kind: 'fire' },
        recent: [
          { kind: 'fire', text: '扎高马尾' },
          { kind: 'blocked', text: '站起来' },
          { kind: 'fire', text: '拿出墨镜戴上' }
        ]
      });
      log.push('fx ok');
    }
  } catch (e) { log.push('ERR ' + e.message); }
  return log.join(' | ');
})()
"""


def _targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


async def main() -> int:
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
        "--window-size=1640,1020", "--hide-scrollbars", "--force-device-scale-factor=1",
        URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        deadline = time.time() + 30
        url = None
        while time.time() < deadline:
            try:
                for t in _targets():
                    if t.get("type") == "page" and "8795" in (t.get("url") or ""):
                        url = t.get("webSocketDebuggerUrl")
                        break
            except Exception:
                pass
            if url:
                break
            time.sleep(0.5)
        if not url:
            print("找不到调试目标")
            return 1
        async with websockets.connect(url, max_size=None) as ws:
            async def call(method, params=None, _id=[0]):
                _id[0] += 1
                await ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == _id[0]:
                        return msg
            await call("Page.enable")
            await asyncio.sleep(3.0)                       # 等页面把 /api/* 拉完
            r = await call("Runtime.evaluate", {"expression": JS, "returnByValue": True})
            print("注入结果:", r.get("result", {}).get("result", {}).get("value"))
            await asyncio.sleep(1.5)
            shot = await call("Page.captureScreenshot", {"format": "png"})
            data = shot["result"]["data"]
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_bytes(base64.b64decode(data))
            print("截图:", OUT, OUT.stat().st_size // 1024, "KB")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

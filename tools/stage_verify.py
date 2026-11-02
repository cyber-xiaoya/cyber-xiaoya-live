#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开无头 Edge 拍一张舞台页截图（可注入 JS 先点击某个按钮），用于人工核对 UI 合并结果。

用法：
  python tools/stage_verify.py 输出.png "js 表达式"
例：
  python tools/stage_verify.py out.png '$("btnKoubo").click()'
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
PORT = 9334
URL = "http://127.0.0.1:8795/stage?right=560"


def _targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


async def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "stage-verify.png")
    js = sys.argv[2] if len(sys.argv) > 2 else ""
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
        "--window-size=1640,1020", "--hide-scrollbars", "--force-device-scale-factor=1", URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = None
        deadline = time.time() + 30
        while time.time() < deadline and not url:
            try:
                for t in _targets():
                    if t.get("type") == "page" and "8795" in (t.get("url") or ""):
                        url = t.get("webSocketDebuggerUrl")
                        break
            except Exception:
                pass
            if not url:
                time.sleep(0.5)
        if not url:
            print("找不到调试目标")
            return 1
        async with websockets.connect(url, max_size=None) as ws:
            counter = [0]

            async def call(method, params=None):
                counter[0] += 1
                await ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == counter[0]:
                        return msg

            await call("Page.enable")
            await asyncio.sleep(3.5)
            if js:
                r = await call("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                                    "awaitPromise": True})
                print("注入:", json.dumps(r.get("result", {}).get("result", {}), ensure_ascii=False)[:1600])
            await asyncio.sleep(1.5)
            shot = await call("Page.captureScreenshot", {"format": "png"})
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot["result"]["data"]))
            print("截图:", out, out.stat().st_size // 1024, "KB")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

r"""启动器: 起 uvicorn(默认 127.0.0.1:8795) + 自动开浏览器。

用法(见根目录 启动-组件版DEV.cmd, 在项目根目录):
  .venv\Scripts\python.exe -m src.main [--port 8795] [--host 127.0.0.1] [--no-browser]
"""
from __future__ import annotations

import argparse
import threading
import time
import webbrowser


def main() -> int:
    ap = argparse.ArgumentParser(description="Vidu S2 组件版数字人 · 桥")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    from . import config as appconf
    conf = appconf.load()
    port = args.port or int((conf.get("bridge") or {}).get("port", 8795))
    url = f"http://{args.host}:{port}/stage"

    print()
    print("  Vidu S2 组件版数字人(源码 DEV)")
    print(f"  → {url}")
    print(f"  会话端点: https://{(conf.get('vidu') or {}).get('host')}  (组件版 /live/s_avatar/component)")
    print(f"  LLM    : {(conf.get('llm') or {}).get('base_url')}  {(conf.get('llm') or {}).get('model')}")
    tts_conf = conf.get("tts") or {}
    _prov = str(tts_conf.get("provider") or "omnivoice").strip().lower()
    if _prov in ("say", "macos"):
        print(f"  TTS    : macOS say(本机)  音色={tts_conf.get('voice') or tts_conf.get('say_voice') or 'Tingting'}")
    elif _prov in ("edge", "edge-tts"):
        print(f"  TTS    : edge-tts(微软在线)  音色={tts_conf.get('voice') or tts_conf.get('edge_voice') or '晓晓'}")
    elif _prov in ("mlx", "mlx-audio"):
        print(f"  TTS    : MLX OmniVoice(本机 Apple Silicon)  音色={tts_conf.get('voice') or 'mlx_default'}")
    elif _prov in ("siliconflow", "cosyvoice", "sf"):
        print(f"  TTS    : siliconflow(CosyVoice2)  音色={tts_conf.get('voice') or tts_conf.get('sf_voice') or 'bella'}")
    else:
        print(f"  TTS    : {tts_conf.get('omnivoice_base')}  音色={tts_conf.get('voice')}")
    print()

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn
    from .bridge import app
    uvicorn.run(app, host=args.host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

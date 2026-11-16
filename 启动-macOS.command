#!/bin/bash
# 赛博小雅直播版 · macOS 一键启动（双击本文件即在本机终端里跑起来）
#
# 与 Windows 的「启动-组件版DEV.cmd」等价，只起本地桥 + 舞台页：
#   - 首次运行会自动建 Python 3.12 虚拟环境并装依赖（需要已装 uv 与 ffmpeg）
#   - 没有 config.json 时从 config.example.json 复制一份，记得去填 Vidu / LLM 密钥
#   - TTS：macOS 没有 NVIDIA 显卡，默认走系统自带 say（tts.provider = "say"）；
#          想用 OmniVoice 就把它跑在另一台 NVIDIA 机器/云端，然后填 tts.omnivoice_base
#
# 手工装环境（可选）：
#   brew install uv ffmpeg
#   uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt

set -e
cd "$(dirname "$0")"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "本脚本只给 macOS 用。"; exit 1
fi

# ---- 1. 环境 ----
if [ ! -x ".venv/bin/python" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "缺少 uv。先执行：brew install uv ffmpeg"; exit 1
  fi
  echo "[1/3] 创建虚拟环境 + 安装依赖（首次约 1~2 分钟）…"
  uv venv .venv --python 3.12
  uv pip install --python .venv/bin/python -r requirements.txt
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "缺少 ffmpeg（音频转码必需）。先执行：brew install ffmpeg"; exit 1
fi

# ---- 2. 配置 ----
if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "[2/3] 已生成 config.json —— 先填 vidu.api_key / avatar_id / llm，再重跑本脚本。"
  open -e config.json 2>/dev/null || true
  exit 0
fi

# macOS 没显卡可跑 OmniVoice：provider 留空时自动用 say，避免"没声音以为是 bug"
if ! .venv/bin/python - <<'PY'
import json, sys
c = json.load(open("config.json", encoding="utf-8"))
p = ((c.get("tts") or {}).get("provider") or "").strip().lower()
sys.exit(0 if p in ("say", "macos") else 1)
PY
then
  echo "[提示] config.json 里 tts.provider 不是 say —— 如果你没有在本机/局域网跑 OmniVoice，"
  echo "       会把 tts.provider 设成 \"say\" 才有声音（页面 ⚙ 里也能改）。"
fi

# ---- 3. 起服务 ----
echo "[3/3] 启动本地桥 → http://127.0.0.1:8795/stage （Ctrl+C 停止）"
exec .venv/bin/python -m src.main "$@"

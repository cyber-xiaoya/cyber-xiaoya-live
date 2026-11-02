#!/bin/bash
# ============================================================================
#  OmniVoice 语音引擎 —— WSL2 启动脚本（新手向，全是人话）
#
#  这个脚本干什么？
#    把 OmniVoice（一个能"克隆你声音"的 TTS 模型）启动成一个服务，
#    监听 9877 端口。启动之后，Windows 那边的数字人/语音桥就能调用它合成语音。
#
#  怎么用？
#    ① 先把本文件夹的两份文件拷进 WSL 家目录（只需一次）：
#       cp /mnt/<盘>/<仓库路径>/tools/omnivoice/{demo_flashinfer.py,start_server.sh} ~/omnivoice-wsl/
#       chmod +x ~/omnivoice-wsl/start_server.sh
#
#    ② 然后启动：
#       bash ~/omnivoice-wsl/start_server.sh                                  # 自动找模型
#       bash ~/omnivoice-wsl/start_server.sh /自己/的/模型/路径               # 指定模型
#
#  怎么知道成功了？
#    看到 "[OK] 启动中" 后等 1~3 分钟，再执行：
#       curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9877/
#    返回 200 就是好了（第一次加载模型慢一点很正常）。
#
#  出问题看哪？
#    ~/omnivoice-wsl/server.log   （最后 30 行基本能看出原因：tail -30 ~/omnivoice-wsl/server.log）
# ============================================================================
set -u

DIR="$HOME/omnivoice-wsl"

echo "=============================================="
echo "  OmniVoice 语音引擎 启动脚本"
echo "=============================================="

# ---------- 检查 1：目录在不在 ----------
if [ ! -d "$DIR" ]; then
  echo "[失败] 找不到目录：$DIR"
  echo ""
  echo "  怎么办：先把上游项目 clone 下来，再拷本仓库的两份文件进去："
  echo "    git clone https://github.com/k2-fsa/OmniVoice ~/omnivoice-wsl"
  echo "    cp /mnt/<盘>/<仓库路径>/tools/omnivoice/{demo_flashinfer.py,start_server.sh} ~/omnivoice-wsl/"
  echo "    chmod +x ~/omnivoice-wsl/start_server.sh"
  exit 1
fi
cd "$DIR" || { echo "[失败] 进不去 $DIR"; exit 1; }
echo "[1/4] 工作目录 OK：$DIR"

# ---------- 检查 2：启动文件在不在 ----------
if [ ! -f "$DIR/demo_flashinfer.py" ]; then
  echo "[失败] 缺少启动文件：$DIR/demo_flashinfer.py"
  echo ""
  echo "  怎么办：把仓库里的文件复制过来（把下面路径换成你 clone 仓库的实际位置）："
  echo "    cp <你的仓库>/tools/omnivoice/demo_flashinfer.py $DIR/"
  echo "    cp <你的仓库>/tools/omnivoice/start_server.sh   $DIR/"
  echo "    chmod +x $DIR/start_server.sh"
  exit 1
fi
echo "[2/4] 启动文件 OK"

# ---------- 检查 3：Python 环境在不在 ----------
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[失败] 缺少 Python 环境：$PY"
  echo ""
  echo "  OmniVoice 必须跑在一个专门的环境里（torch + flashinfer + CUDA 13），"
  echo "  不能直接用系统 Python。照着 README 的「TTS 引擎：OmniVoice」一节建一次就行，"
  echo "  核心就是下面几条（在 WSL 终端里执行，只需做一次）："
  echo "    cd ~/omnivoice-wsl"
  echo "    uv venv --python 3.12"
  echo "    source .venv/bin/activate"
  echo "    uv pip install torch --index-url https://download.pytorch.org/whl/cu130"
  echo "    uv pip install flashinfer-python flashinfer-jit-cache omnivoice"
  exit 1
fi
echo "[3/4] Python 环境 OK"

# ---------- 检查 4：已经开着就别重复开（重复开会吃双份显存） ----------
if pgrep -f "demo_flashinfer.py" >/dev/null 2>&1; then
  echo "[4/4] 服务已经在运行了，不用重复启动。"
  echo ""
  echo "  想看状态：curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:9877/"
  echo "  想重启  ：在 WSL 里执行  pkill -f demo_flashinfer.py  然后再跑本脚本"
  exit 0
fi
echo "[4/4] 环境干净，准备启动"

# ---------- 选模型 ----------
# 优先级：命令行参数 > 环境变量 OMNIVOICE_MODEL > 本地常见路径 > 让模型自己从网上下
MODEL="${1:-${OMNIVOICE_MODEL:-}}"
if [ -z "$MODEL" ]; then
  if [ -d /mnt/e/llama-models/models/OmniVoice-bf16 ]; then
    MODEL="/mnt/e/llama-models/models/OmniVoice-bf16"
  elif [ -d /mnt/d/models/OmniVoice-bf16 ]; then
    MODEL="/mnt/d/models/OmniVoice-bf16"
  else
    MODEL="k2-fsa/OmniVoice"
  fi
fi

echo ""
if [ "$MODEL" = "k2-fsa/OmniVoice" ]; then
  echo "----------------------------------------------------------------"
  echo "  还没找到你本地的模型，将从 HuggingFace 自动下载：k2-fsa/OmniVoice"
  echo "  ⚠ 首次下载约 10GB，看网速大概 10~60 分钟；下载完会存到"
  echo "    ~/.cache/huggingface/ 里，以后启动就不会再下了。"
  echo ""
  echo "  如果下载太慢/卡住，可以换成国内镜像后再试："
  echo "    export HF_ENDPOINT=https://hf-mirror.com"
  echo ""
  echo "  已经有本地模型？用参数指定，跳过下载（快得多）："
  echo "    bash ~/omnivoice-wsl/start_server.sh /你的/模型/路径"
  echo "----------------------------------------------------------------"
else
  if [ ! -d "$MODEL" ] && [ ! -f "$MODEL" ]; then
    echo "[提醒] 指定的模型路径不存在：$MODEL"
    echo "       将退回到在线下载 k2-fsa/OmniVoice（约 10GB）"
    MODEL="k2-fsa/OmniVoice"
  else
    echo "  使用本地模型：$MODEL"
  fi
fi

# WSL2 系统自带的 nvcc 12.8 会干扰 flashinfer 的版本检测 → 必须指向 venv 里的 cu13 版
export CUDA_HOME="$DIR/.venv/lib/python3.12/site-packages/nvidia/cuda_nvcc"

echo ""
echo "  正在启动（后台运行，日志写到 $DIR/server.log）..."
nohup "$PY" demo_flashinfer.py \
  --model "$MODEL" \
  --port 9877 --no-asr --ip 0.0.0.0 \
  --graph-buckets 5,10,15,20,30 \
  > server.log 2>&1 < /dev/null &

echo "[OK] 已启动（PID $!）"
echo ""
echo "  接下来："
echo "    1) 等 1~3 分钟（第一次要加载模型 + 预热，老机器更久）"
echo "    2) 看日志：tail -f $DIR/server.log"
echo "    3) 探活  ：curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:9877/"
echo "       返回 200 = 成功；一直连不上就看日志最后 30 行。"

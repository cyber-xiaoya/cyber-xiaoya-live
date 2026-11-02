# OmniVoice 语音引擎（WSL2）—— 新手安装说明

> 这个文件夹里的两个文件，是让 **OmniVoice**（能"克隆你声音"的 TTS 模型）跑起来的最小集合：
>
> - `demo_flashinfer.py` —— 服务启动器：加载模型 → 加 FlashInfer 加速 → 开一个网页服务（端口 **9877**）
> - `start_server.sh` —— 在 WSL 里一键启动它（自动找模型、先做四项环境预检）
>
> ⚠️ **`demo_flashinfer.py` 不是 OmniVoice 上游自带的文件**，是本项目提供的一层封装（上游只有库，没有这个"带 FlashInfer 加速的服务入口"）。
> 所以 clone 完上游**看不到它是正常的**，把这里的两份文件拷进 `~/omnivoice-wsl/` 即可。

**你不用手动改这些代码**，拷过去就能用。下面是从零到跑通的完整步骤。

---

## 一、先搞清楚三件事

| 名词 | 是什么 | 类比 |
|---|---|---|
| **WSL2** | Windows 里的 Linux 子系统 | 你电脑里另开了一台小 Linux 电脑 |
| **OmniVoice** | 一个语音合成模型，能克隆音色 | 给数字人"配音"的引擎 |
| **:9877** | 这个引擎对外开的端口 | 引擎的"门牌号"，别的程序靠它连过来 |

**为什么必须放 WSL2？** OmniVoice 依赖 Linux 下的 FlashInfer + CUDA Graph 加速，Windows 原生跑不起来。
实测效果：克隆首轮出音 **18 秒 → 1.6 秒**，热态约 **1.3 秒**。

---

## 二、你需要准备什么

1. **Windows 10/11** + **NVIDIA 显卡**（8GB 显存是底线，16GB 推荐；作者实测稳定占 ~11GB）
2. **WSL2 + Ubuntu-22.04**：管理员 PowerShell 执行一次，装完**重启电脑**
   ```powershell
   wsl --install -d Ubuntu-22.04
   ```
3. **模型**（二选一）：
   - 已有本地模型文件夹（如 `OmniVoice-bf16`）→ 启动时用参数指过去
   - 没有 → 什么都不用做，脚本会**自动从 HuggingFace 下载约 10GB**

---

## 三、四步跑通

### 第 1 步：装上游 OmniVoice + 建环境（只做一次，约 10~20 分钟）

在 **Ubuntu 终端**里执行：

```bash
# 1) 装 uv（Python 包管理器，比 pip 快很多）
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc

# 2) 拉上游代码（注意：这里只有库，没有 demo_flashinfer.py，正常）
sudo apt update && sudo apt install -y ffmpeg git
git clone https://github.com/k2-fsa/OmniVoice ~/omnivoice-wsl
cd ~/omnivoice-wsl

# 3) 建环境 + 装依赖（Blackwell / RTX 50 系必须 cu13 系列）
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install torch --index-url https://download.pytorch.org/whl/cu130
uv pip install flashinfer-python flashinfer-jit-cache
uv pip install -r requirements.txt
```

> ⚠️ 用 `cu129` 会报 `requires GPUs with sm75`；CUDA 版本对不上是这一步最常见的失败原因。

### 第 2 步：把本文件夹的两个文件拷进去

**Windows 里**（在仓库根目录执行，把 `<你的盘>` / `<仓库路径>` 换成实际）：

```powershell
wsl -d Ubuntu-22.04 -- bash -lc "cp /mnt/<你的盘>/<仓库路径>/tools/omnivoice/demo_flashinfer.py /mnt/<你的盘>/<仓库路径>/tools/omnivoice/start_server.sh ~/omnivoice-wsl/ && chmod +x ~/omnivoice-wsl/start_server.sh"
```

**或者直接在 WSL 里**：

```bash
cp /mnt/d/<你的仓库路径>/tools/omnivoice/{demo_flashinfer.py,start_server.sh} ~/omnivoice-wsl/
chmod +x ~/omnivoice-wsl/start_server.sh
```

> 这一步就是「demo_flashinfer.py 缺失」的答案：**它在本仓库的 `tools/omnivoice/` 里，不在上游仓库。**

### 第 3 步：启动

```bash
bash ~/omnivoice-wsl/start_server.sh                                # 自动找模型
bash ~/omnivoice-wsl/start_server.sh /mnt/e/models/OmniVoice-bf16   # 指定模型
```

脚本会先做 4 项预检（目录 / 启动文件 / Python 环境 / 是否已在运行），任何一项不过都会直接告诉你**该敲什么命令**。

### 第 4 步：验证

等 1~3 分钟，浏览器打开：

```
http://127.0.0.1:9877/
```

**能看到带「语音克隆」字样的网页 = 成功。** 命令行验证：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9877/    # 期望 200
```

成功后回到本项目，在 `config.json` 里确认：

```json
"tts": { "engine": "omnivoice", "omnivoice_base": "http://127.0.0.1:9877" }
```

（WSL2 用**镜像网络模式**时 `127.0.0.1` 可用；默认 NAT 模式要填 `wsl hostname -I` 查到的 IP。）

---

## 四、出错了看这张表

| 你看到的报错 | 原因 | 怎么修 |
|---|---|---|
| `No such file: demo_flashinfer.py` | 没做第 2 步 | 把本文件夹两份文件拷进 `~/omnivoice-wsl/` |
| `/usr/bin/env: 'bash\r': No such file` | 脚本被存成 Windows 换行 | `sed -i 's/\r$//' ~/omnivoice-wsl/*.sh` |
| `缺少 Python 环境 .venv` | 没做第 1 步 | 回去做第 1 步 |
| `requires GPUs with sm75` | CUDA 版本不对 | 用 cu130 装 torch |
| `Address already in use`（9877 被占） | 已有实例在跑 | `pkill -f demo_flashinfer.py` 再启动 |
| `CUDA out of memory` | 显存不够 | 关掉游戏/其他 AI 程序；16GB 以下别同时开渲染 |
| 一直连不上、日志停在下载 | 在下载 10GB 模型 | 等；或 `export HF_ENDPOINT=https://hf-mirror.com` |
| 网页能开但合成很慢（>10 秒） | 没用上 GPU / 首次冷启动 | 首次慢正常；持续慢看日志里 `FlashInfer applied` 有没有打印 |

**看日志**（90% 的问题这里能看出来）：

```bash
tail -30 ~/omnivoice-wsl/server.log
```

---

## 五、常见问题

**Q：能不能不用 WSL2？**
A：桥可以无声运行（舞台页能开、画面会动、只是不发声），TTS 目前只认 OmniVoice。

**Q：每次重启电脑都要重来吗？**
A：不用。环境只在第 1 步建一次；之后每次开机跑一次 `start_server.sh`（已在跑会自动跳过）。

**Q：模型必须放 `/mnt/e/...` 吗？**
A：不用。查找顺序：命令行参数 → 环境变量 `OMNIVOICE_MODEL` → 常见本地路径 → 自动下载。

**Q：这个文件夹会被改吗？**
A：仓库里是"母版"；实际运行的是 `~/omnivoice-wsl/` 里那份。改仓库不会影响已启动的实例（重启才生效）。

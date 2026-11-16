# 赛博小雅直播版 · macOS 版启动指南

Windows 版看 `README.md`。本文件只讲 Mac（Apple Silicon）。

当前 Mac 版能力：Vidu 组件版数字人连线、弹幕接入、LLM 对话、**本机 MLX 本地语音合成（OmniVoice 引擎，断网可用，零 API 费，支持克隆你自己的声音）**。已在 M4 Pro / 24G 实测通过，M1 Pro 及以上、内存 16G+ 均可。

---

## 一、前置安装（只做一次）

需要三样东西：Homebrew、uv、ffmpeg。打开「终端」逐条执行：

```bash
# 1. 装 Homebrew（已装可跳过）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 装 uv 和 ffmpeg
brew install uv ffmpeg
```

## 二、一键启动（日常用这个）

1. 双击项目根目录的 **`启动-macOS.command`**；
2. 首次运行会自动创建虚拟环境、装依赖（约 1~2 分钟）；
3. 看到 `→ http://127.0.0.1:8795/stage` 即启动成功，Chrome 打开这个地址就是舞台页；
4. 停止：在终端按 `Ctrl+C`。

## 三、手动启动（等价于双击脚本，排障时用）

```bash
cd 项目目录
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt

# 本地语音合成（可选但强烈推荐，跳过则只能用系统 say 音色）
uv pip install --python .venv/bin/python mlx-audio

.venv/bin/python -m src.main          # Ctrl+C 停止
```

## 四、首次配置（config.json）

没有 `config.json` 时脚本会自动从 `config.example.json` 复制一份，用文本编辑器打开，必填：

| 字段 | 说明 |
|---|---|
| `vidu.api_key` | Vidu 平台的密钥（数字人连线计费，按秒扣积分） |
| `avatar.avatar_id` | 数字人形象 ID（在 Vidu 平台用一张照片创建；本项目也可调 `src/vidu/client.py` 的 `create_avatar` 自动创建） |
| `llm.api_key` | LLM 平台密钥（如 DeepSeek） |
| `tts.provider` | 填 `"mlx"` = 本机合成（推荐）；`"say"` = 系统音色兜底 |

改完保存，重新双击启动脚本即可。

### 本地语音（MLX）说明

- 第一次合成语音时会自动下载模型（约 1.5GB，缓存在 `~/.cache/huggingface`，之后断网可用）；
- 桥重启后的**第一句话会有约 3 秒模型加载停顿**，属正常现象，先说一句打招呼台词热机；
- 音质档位在 `config.json → tts.mlx_num_steps`：官方 32 最稳（默认），16 更快；
- 想用自己的声音直播：把 15~20 秒的本人干净录音放进 `voices/` 子目录并写一个 `mlx.json`（`{"ref_audio": "ref.wav", "ref_text": "录音里说的话"}`），设置面板就会出现你的克隆音色；
- ⚠ 不要开 `tts.mlx_instruct` 风格指令——实测会把克隆音色带偏（甚至变成男声）。

## 五、直播前检查清单

1. **电源**：插电，系统设置里关闭「显示关闭时睡眠」（Mac 睡眠会断连线）；
2. **热机**：重启桥之后先说一句话，等模型加载完再正式开播；
3. **积分**：舞台页可见 Vidu 余额（连线按秒消耗）；
4. **关掉其他大负载程序**（本机合成会吃一点 GPU）；
5. Chrome 打开 `http://127.0.0.1:8795/stage` → 点「开始连线」。

## 六、常见问题

| 症状 | 原因 / 处理 |
|---|---|
| 声音卡卡的、有破碎感 | `tts.mlx_num_steps` 太低（低于 16 会有伪影），调回 24~32 |
| 每次重启后第一句没声音/停顿 | 模型加载中，等 3 秒或先热机 |
| 克隆声音突然变男声/变嗓 | 检查是否开了 `mlx_instruct`，关掉即可 |
| 完全没声音 | 看 `tts.provider`：mlx 需要 `uv pip install --python .venv/bin/python mlx-audio` |
| 端口被占用 | `pkill -f "src.main"` 后重开 |
| 连线中途画面冻结 | 多半是 Mac 睡眠了，或 Vidu 积分用完 |
| 改了代码/配置不生效 | 桥启动时加载代码，改完必须重启桥 |

---

## 七、不会弄？直接交给 AI

**如果你看不懂上面的步骤、或执行中报错不知道怎么办——不用自己折腾。**

把**整个项目文件夹**直接丢给 AI 编程助手（CodeBuddy、Claude Code、Cursor 等都可以），对它说一句话：

> 「请按照 README-MAC.md 帮我安装并启动这个项目，遇到问题自行解决。」

AI 会自动完成：装 Homebrew/uv/ffmpeg → 建虚拟环境装依赖 → 下载语音模型 → 检查配置 → 启动 → 排错。你只需要在它需要密钥（Vidu / LLM 的 api_key）时把 key 提供给它，其余全部交给它就行。

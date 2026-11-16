"""macOS 原生 TTS(`/usr/bin/say`)适配 —— 让整条链路在没有 NVIDIA 显卡的 Mac 上也能"说话"。

定位: OmniVoice 需要 CUDA(flashinfer), Apple Silicon 跑不了。本适配器用系统自带的
`say` 合成语音(完全离线、无需模型下载、延迟 ~1s), 输出 AIFF 后交给 ffmpeg 统一转
PCM s16le / 24kHz / mono(与 OmniVoice 出口完全一致, 上游无需任何改动)。

音色约定(复用 tts.voices_dir, 与 OmniVoice 预设同目录互不影响):
    子文件夹含 say.json  → {"voice": "Tingting", "rate": 200, "label": "婷婷"}
    voice = `say -v '?'` 里的名字(先用 `say -v '?' | grep zh_CN` 看一下有哪些)
    rate  = 语速(词/分钟, 省略用系统默认; 中文一般 180~220)
其它目录(design.json / ref_audio.*)会被自动跳过 —— 那些是给 OmniVoice 的。

若一个 say.json 都没有, 用 config `tts.say_voice`(默认 Tingting)当唯一音色。
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import pathlib
import subprocess
import tempfile
import time

from . import audio

SAY = "/usr/bin/say"
TAIL = (0.5, 30)                       # 合成超时 = len(text)*0.5 + 30 秒


def scan_voices(voices_dir: str) -> dict[str, dict]:
    """扫描音色目录 → {name: preset}; 只收含 say.json 的目录。"""
    out: dict[str, dict] = {}
    base = pathlib.Path(voices_dir or "")
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        f = entry / "say.json"
        if not (entry.is_dir() and f.is_file()):
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        out[entry.name] = {
            "name": entry.name,
            "label": str(p.get("label") or entry.name),
            "kind": "say",
            "voice": str(p.get("voice") or "Tingting"),
            "rate": p.get("rate"),
        }
    return out


class MacSayTTS:
    """与 OmniVoiceTTS 鸭子类型一致(engine / bridge 都只认这几个成员)。"""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.cfg_base = ""                                 # 无远端服务; 留空占位
        self.timeout_sec = 120.0
        self.default_voice = str(cfg.get("say_voice") or "Tingting")
        self.recent_ms = collections.deque(maxlen=12)
        self.restart_count = 0
        self.last_restart_at = 0.0
        self.fail_streak = 0                   # 与 OmniVoiceTTS 同名: 看门狗读它
        self._lock = asyncio.Lock()            # say 是进程级串行, 不快也无所谓

    # ---- 与 OmniVoiceTTS 对齐的只读接口 ----

    def slow_recent(self, limit_ms: int, n: int) -> bool:
        if n <= 0:
            return False
        vals = list(self.recent_ms)[-n:]
        return len(vals) >= n and all(v > limit_ms for v in vals)

    def active_base(self) -> str:
        return "say(本机)"

    # ---- 合成 ----

    def _preset_voice_rate(self, preset: dict) -> tuple[str, str | None]:
        preset = preset or {}
        voice = str(preset.get("voice") or self.default_voice)
        rate = preset.get("rate")
        return voice, (str(int(rate)) if rate not in (None, "") else None)

    def _synth_sync(self, text: str, preset: dict) -> str:
        """阻塞: say → 临时 AIFF 路径。只在线程池里跑。"""
        voice, rate = self._preset_voice_rate(preset)
        fd, path = tempfile.mkstemp(suffix=".aiff")
        os.close(fd)
        args = [SAY, "-v", voice, "-o", path, "-f", "-"]
        if rate:
            args += ["-r", rate]
        timeout = max(20.0, len(text) * TAIL[0] + TAIL[1])
        try:
            r = subprocess.run(args, input=text.encode("utf-8"),
                               capture_output=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(
                    f"say 合成失败(rc={r.returncode}): "
                    f"{r.stderr.decode('utf-8', 'ignore')[:200]}")
            if os.path.getsize(path) == 0:
                raise RuntimeError(f"say 没有产出音频(音色 {voice!r} 是否已安装?)")
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        """文本 → PCM s16le/24kHz/mono。异常向上抛, 由引擎决定如何收尾。"""
        if not text.strip():
            return b""
        async with self._lock:
            loop = asyncio.get_running_loop()
            t0 = time.monotonic()
            try:
                aiff = await loop.run_in_executor(None, self._synth_sync, text, preset)
                self.fail_streak = 0
            except Exception:
                self.fail_streak += 1
                raise
            finally:
                self.recent_ms.append(int((time.monotonic() - t0) * 1000))
            try:
                return await loop.run_in_executor(None, audio.decode_to_pcm, aiff, None, "")
            finally:
                try:
                    os.unlink(aiff)
                except OSError:
                    pass

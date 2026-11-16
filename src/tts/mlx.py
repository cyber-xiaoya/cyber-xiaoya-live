"""MLX OmniVoice 适配 —— 在 Apple Silicon 上本地运行 k2-fsa/OmniVoice 官方引擎。

为什么值得: 原项目在 Windows 上跑的就是 k2-fsa/OmniVoice(需 CUDA); mlx-audio
把它移植到了 MLX(M4 Pro 实测: 无参考音色 0.7s 合成 7s 音频, 实时率 0.10;
零样本克隆 1.5~2.6s/8s)。生成式 TTS, 韵律远超 edge-tts, 还能克隆任意参考
音频(比如台湾腔人声) —— 断网、零 API 费、声音完全自定义。

依赖(可选, 不进 requirements.txt, 免得拖累其它平台用户):
    uv pip install --python .venv/bin/python mlx-audio
模型权重自动从 HF 下载(默认 mlx-community/OmniVoice-bfloat16, ~1.5GB,
缓存在 ~/.cache/huggingface)。

配置(config.json → tts):
    "provider": "mlx",
    "mlx_lang": "zh",
    "mlx_num_steps": 8,               # 扩散步数, 越大越稳越慢
    "mlx_ref_audio": "assets/local/ref-tw.wav",   # 留空=默认音色
    "mlx_ref_text":  "参考音频里说的话"

音色预设(复用 tts.voices_dir, 子文件夹含 mlx.json):
    {"ref_audio": "ref.wav", "ref_text": "参考音频文字", "label": "台湾腔"}
"""
from __future__ import annotations

import asyncio
import collections
import json
import pathlib
import tempfile
import time

import numpy as np

from . import audio

DEFAULT_MODEL = "mlx-community/OmniVoice-bfloat16"

BUILTIN_VOICES: dict[str, dict] = {
    "mlx_default": {"name": "mlx_default", "label": "MLX·OmniVoice默认音色",
                    "kind": "mlx", "ref_audio": "", "ref_text": ""},
}


def scan_voices(voices_dir: str) -> dict[str, dict]:
    """扫描音色目录 → {name: preset}; 子文件夹含 mlx.json(带参考音频)算克隆音色。"""
    out: dict[str, dict] = {}
    base = pathlib.Path(voices_dir or "")
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        f = entry / "mlx.json"
        if not (entry.is_dir() and f.is_file()):
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        ref = str(p.get("ref_audio") or "")
        if ref and not pathlib.Path(ref).is_absolute():
            ref = str((base / entry.name / ref).resolve())
        out[entry.name] = {
            "name": entry.name,
            "label": str(p.get("label") or entry.name),
            "kind": "mlx",
            "ref_audio": ref,
            "ref_text": str(p.get("ref_text") or ""),
        }
    return out


def _est_duration_s(text: str) -> float:
    """按中文 ~0.22s/字 估算时长。模型按固定时长出音频, 估多了尾部是静音,
    由 _trim 尾部裁掉; 估少了会截字, 宁多勿少。"""
    n = sum(1 for ch in text if ord(ch) > 0x2E80)          # CJK
    n += sum(1 for ch in text if ch.isascii() and ch.isalnum())
    return min(20.0, max(2.0, 1.2 + 0.22 * n))


def _trim_tail(pcm: bytes, threshold: int = 300, keep_ms: int = 120) -> bytes:
    """裁掉尾部静音(模型按固定时长生成, 估长的部分是静音, 拖慢口型)。"""
    if not pcm:
        return pcm
    a = np.frombuffer(pcm, dtype=np.int16)
    idx = np.nonzero(np.abs(a) > threshold)[0]
    if idx.size == 0:
        return pcm
    keep = min(len(a), int(idx[-1]) + 24000 * keep_ms // 1000)
    return a[:keep].tobytes()


class MlxTTS:
    """与 OmniVoiceTTS / EdgeTTS 鸭子类型一致。模型在首次合成时加载(约2s)。"""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.cfg_base = ""
        self.model_id = str(cfg.get("mlx_model") or DEFAULT_MODEL)
        self.lang = str(cfg.get("mlx_lang") or "zh")
        self.num_steps = int(cfg.get("mlx_num_steps") or 32)   # 官方默认32; <16语音会有破碎伪影
        self.instruct = str(cfg.get("mlx_instruct") or "")     # 风格指令(模型支持的话影响语气/节奏)
        self.default_ref = str(cfg.get("mlx_ref_audio") or "")
        self.default_ref_text = str(cfg.get("mlx_ref_text") or "")
        self.timeout_sec = 120.0
        self.recent_ms = collections.deque(maxlen=12)
        self.restart_count = 0
        self.last_restart_at = 0.0
        self.fail_streak = 0
        self._lock = asyncio.Lock()
        self._loaded = False
        self._load_err = ""

    def slow_recent(self, limit_ms: int, n: int) -> bool:
        if n <= 0:
            return False
        vals = list(self.recent_ms)[-n:]
        return len(vals) >= n and all(v > limit_ms for v in vals)

    def active_base(self) -> str:
        if self._load_err:
            return f"mlx(加载失败: {self._load_err[:40]})"
        return f"mlx({self.model_id})" if self._loaded else "mlx(未加载)"

    # ---------- 模型加载(线程里做, 不卡事件循环) ----------

    def _load_sync(self) -> None:
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer
        from mlx_audio.codec.models.higgs_audio.higgs_audio import HiggsAudioTokenizer
        from mlx_audio.tts.models.omnivoice.omnivoice import Model, OmniVoiceConfig

        mp = snapshot_download(self.model_id)
        config = OmniVoiceConfig.from_dict(json.load(open(f"{mp}/config.json")))
        model = Model(config)
        weights = model.sanitize(dict(mx.load(f"{mp}/model.safetensors")))
        model.load_weights(list(weights.items()))
        mx.eval(model.parameters())
        self._model = model
        self._tok = HiggsAudioTokenizer.from_pretrained(mp)
        self._text_tok = AutoTokenizer.from_pretrained(mp)
        self._loaded = True

    async def preload(self) -> bool:
        """桥启动时可调用, 提前把 ~2GB 模型搬进内存。失败不抛, 记录原因。"""
        async with self._lock:
            if self._loaded:
                return True
            try:
                await asyncio.to_thread(self._load_sync)
                return True
            except Exception as e:                         # noqa: BLE001
                self._load_err = f"{type(e).__name__}: {str(e)[:120]}"
                return False

    # ---------- 合成 ----------

    def _generate_sync(self, text: str, ref_audio: str, ref_text: str,
                       instruct: str = "") -> bytes:
        """阻塞版生成 → 临时 wav 文件路径。在 to_thread 里跑。"""
        import mlx.core as mx                              # noqa: F401
        from mlx_audio.audio_io import write as audio_write

        dur = _est_duration_s(text)
        kwargs = {"duration_s": dur, "num_steps": self.num_steps}
        if instruct and instruct != "None":
            kwargs["instruct"] = instruct
        if ref_audio and pathlib.Path(ref_audio).is_file():
            kwargs["ref_audio"] = ref_audio
            kwargs["ref_text"] = ref_text or ""
        last_err: Exception | None = None
        for attempt in range(2):                           # MLX 偶发抖动, 重试一次
            try:
                audio = []
                sr = 24000
                for r in self._model.generate(text=text, language=self.lang,
                                              tokenizer=self._tok,
                                              text_tokenizer=self._text_tok, **kwargs):
                    audio.append(np.array(r.audio))
                    sr = r.sample_rate
                if not audio:
                    raise RuntimeError("MLX OmniVoice 返回空音频")
                pcm = np.concatenate(audio).astype(np.float32)
                pcm = np.clip(pcm, -1.0, 1.0)
                fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                audio_write(fd.name, pcm, sr)
                fd.close()
                return fd.name
            except Exception as e:                         # noqa: BLE001
                last_err = e
        raise RuntimeError(f"MLX 合成失败: {type(last_err).__name__}: {str(last_err)[:150]}")

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        """文本 → PCM s16le/24kHz/mono。"""
        if not text.strip():
            return b""
        if not self._loaded:
            ok = await self.preload()
            if not ok:
                raise RuntimeError(
                    f"MLX OmniVoice 模型加载失败({self._load_err})。"
                    "首次使用请执行: uv pip install --python .venv/bin/python mlx-audio")
        preset = preset or {}
        ref_audio = str(preset.get("ref_audio") or self.default_ref or "")
        ref_text = str(preset.get("ref_text") or self.default_ref_text or "")

        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        async with self._lock:
            try:
                tmp = await loop.run_in_executor(
                    None, self._generate_sync, text, ref_audio, ref_text,
                    str(preset.get("instruct") or self.instruct or ""))
                try:
                    pcm = await loop.run_in_executor(
                        None, audio.decode_to_pcm, tmp, None, "")
                finally:
                    try:
                        pathlib.Path(tmp).unlink(missing_ok=True)
                    except OSError:
                        pass
                self.fail_streak = 0
            except Exception:
                self.fail_streak += 1
                raise
            finally:
                self.recent_ms.append(int((time.monotonic() - t0) * 1000))
        return _trim_tail(pcm)

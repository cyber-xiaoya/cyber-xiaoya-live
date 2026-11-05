"""硅基流动 SiliconFlow TTS(CosyVoice2)适配 —— 自然度远超 edge-tts 的付费方案。

为什么要有它: edge-tts 本质是"机器朗读", 情感与气口是广播腔; CosyVoice2 是
生成式 TTS, 韵律接近真人, 还支持音色克隆(传参考音频就有自己的专属音色)。
计费: 按音频量计费, 很便宜(注册送体验额度), 一场 1~2 小时直播约几毛到几元。

接口: POST {base}/v1/audio/speech (OpenAI 兼容)
    model  = FunAudioLLM/CosyVoice2-0.5B
    voice  = "FunAudioLLM/CosyVoice2-0.5B:bella"   (系统音色)
           或 "speech:你的音色名:xxxxx:yyyy"        (克隆音色, 互斥)
    response_format = wav, sample_rate = 24000
返回 wav → ffmpeg 统一转 PCM(与其它 provider 出口一致)。

配置(config.json → tts):
    "provider": "siliconflow",
    "sf_api_key": "sk-xxx",           ← cloud.siliconflow.cn 申请
    "sf_voice":  "FunAudioLLM/CosyVoice2-0.5B:bella",
    "sf_speed":  1.0,                  (0.25~4.0)
    "sf_gain":   0.0                   (-10~10 dB)
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import pathlib
import tempfile
import time

import httpx

from . import audio

try:                                                   # 与 src/llm.py 同一套证书策略
    import certifi
    _VERIFY = certifi.where()
except Exception:                                      # noqa: BLE001
    _VERIFY = True

DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"

# 系统预置女声(男声 alex/benjamin/charles/david 没列)。label 是大致风格, 以实听为准。
BUILTIN_VOICES: dict[str, dict] = {
    "sf_bella": {"name": "sf_bella", "label": "SF·贝拉·甜美(克隆可换专属)",
                 "kind": "siliconflow",
                 "voice": f"{DEFAULT_MODEL}:bella", "speed": 1.0},
    "sf_anna": {"name": "sf_anna", "label": "SF·安娜·温柔",
                "kind": "siliconflow",
                "voice": f"{DEFAULT_MODEL}:anna", "speed": 1.0},
    "sf_claire": {"name": "sf_claire", "label": "SF·克莱尔·知性",
                  "kind": "siliconflow",
                  "voice": f"{DEFAULT_MODEL}:claire", "speed": 1.0},
    "sf_diana": {"name": "sf_diana", "label": "SF·黛安娜·冷艳",
                 "kind": "siliconflow",
                 "voice": f"{DEFAULT_MODEL}:diana", "speed": 1.0},
}


def scan_voices(voices_dir: str) -> dict[str, dict]:
    """音色目录兼容: 子文件夹含 sf.json 的也算一个预设。"""
    out: dict[str, dict] = {}
    base = pathlib.Path(voices_dir or "")
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        f = entry / "sf.json"
        if not (entry.is_dir() and f.is_file()):
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        out[entry.name] = {
            "name": entry.name,
            "label": str(p.get("label") or entry.name),
            "kind": "siliconflow",
            "voice": str(p.get("voice") or f"{DEFAULT_MODEL}:bella"),
            "speed": float(p.get("speed") or 1.0),
            "gain": float(p.get("gain") or 0.0),
        }
    return out


class SiliconFlowTTS:
    """与 OmniVoiceTTS / MacSayTTS / EdgeTTS 鸭子类型一致。"""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.cfg_base = (cfg.get("sf_base") or "https://api.siliconflow.cn").rstrip("/")
        self.api_key = (cfg.get("sf_api_key") or os.environ.get("SILICONFLOW_API_KEY") or "").strip()
        self.model = str(cfg.get("sf_model") or DEFAULT_MODEL)
        self.default_voice = str(cfg.get("sf_voice") or f"{DEFAULT_MODEL}:bella")
        self.default_speed = float(cfg.get("sf_speed") or 1.0)
        self.default_gain = float(cfg.get("sf_gain") or 0.0)
        self.jitter = bool(cfg.get("edge_jitter", True))    # 复用同一个开关
        self.timeout_sec = 60.0
        self.recent_ms = collections.deque(maxlen=12)
        self.restart_count = 0
        self.last_restart_at = 0.0
        self.fail_streak = 0
        self._lock = asyncio.Lock()

    def slow_recent(self, limit_ms: int, n: int) -> bool:
        if n <= 0:
            return False
        vals = list(self.recent_ms)[-n:]
        return len(vals) >= n and all(v > limit_ms for v in vals)

    def active_base(self) -> str:
        return "siliconflow(CosyVoice2)"

    def _params(self, preset: dict) -> dict:
        preset = preset or {}
        speed = float(preset.get("speed") or self.default_speed)
        if self.jitter:                                    # 每句 ±3% 语速微抖动
            import random
            speed = max(0.25, min(4.0, speed + random.uniform(-0.03, 0.03)))
        return {
            "voice": str(preset.get("voice") or self.default_voice),
            "speed": max(0.25, min(4.0, speed)),
            "gain": float(preset.get("gain") or self.default_gain),
        }

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        if not text.strip():
            return b""
        if not self.api_key:
            raise RuntimeError("siliconflow 未配置 key: config.json → tts.sf_api_key "
                               "(cloud.siliconflow.cn 申请, 注册送额度)")
        params = self._params(preset)
        async with self._lock:
            loop = asyncio.get_running_loop()
            t0 = time.monotonic()
            body = {
                "model": self.model,
                "input": text,
                "voice": params["voice"],
                "response_format": "wav",
                "sample_rate": 24000,
                "speed": params["speed"],
                "gain": params["gain"],
                "stream": False,
            }
            async def _post() -> bytes:
                async with httpx.AsyncClient(verify=_VERIFY, timeout=httpx.Timeout(self.timeout_sec)) as cli:
                    r = await cli.post(f"{self.cfg_base}/v1/audio/speech",
                                       headers={"Authorization": f"Bearer {self.api_key}"},
                                       json=body)
                if r.status_code >= 400:
                    raise RuntimeError(f"siliconflow HTTP {r.status_code}: {r.text[:200]}")
                return r.content
            try:
                data = await asyncio.wait_for(_post(), timeout=self.timeout_sec)
                if not data:
                    raise RuntimeError("siliconflow 返回空音频")
                self.fail_streak = 0
            except Exception:
                self.fail_streak += 1
                raise
            finally:
                self.recent_ms.append(int((time.monotonic() - t0) * 1000))

            fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            try:
                fd.write(data)
                fd.close()
                return await loop.run_in_executor(
                    None, audio.decode_to_pcm, fd.name, None, "")
            finally:
                try:
                    pathlib.Path(fd.name).unlink(missing_ok=True)
                except OSError:
                    pass

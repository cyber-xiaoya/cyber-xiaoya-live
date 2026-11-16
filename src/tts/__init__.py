"""TTS 子包: omnivoice(本机引擎直连) / say(macOS 系统音色) / audio(统一 PCM 工具)。

本包**不经过** D:\\speech-to-speech 的语音桥 —— 组件版数字人有自己的 TTS 状态
(音色预设独立热切), 与弹幕朗读等消费方互不影响; 底层共享 WSL2 的 OmniVoice 引擎。

provider 选择(config.json → tts.provider):
    ""/"omnivoice"  → OmniVoice(WSL2/CUDA, 原默认行为, Windows 用户不受影响)
    "say"/"macos"   → macOS 自带 say(无需 GPU, 离线; 适合 Apple Silicon)
"""
from __future__ import annotations

SAY_ALIASES = {"say", "macos", "macos-say", "mac", "darwin"}
EDGE_ALIASES = {"edge", "edge-tts", "edgetts"}
SF_ALIASES = {"siliconflow", "cosyvoice", "sf", "silicon-flow"}
MLX_ALIASES = {"mlx", "mlx-audio", "mlxaudio", "omnivoice-mlx"}


def provider_name(cfg: dict) -> str:
    p = str((cfg or {}).get("provider") or "").strip().lower()
    if p in SAY_ALIASES:
        return "say"
    if p in EDGE_ALIASES:
        return "edge"
    if p in SF_ALIASES:
        return "siliconflow"
    if p in MLX_ALIASES:
        return "mlx"
    return "omnivoice"


def build(cfg: dict):
    """按 config tts.provider 造一个 TTS 实例(engine / koubo 共用)。"""
    cfg = cfg or {}
    name = provider_name(cfg)
    if name == "say":
        from .say import MacSayTTS
        tts = MacSayTTS(cfg)
    elif name == "edge":
        from .edge import EdgeTTS
        tts = EdgeTTS(cfg)
    elif name == "siliconflow":
        from .siliconflow import SiliconFlowTTS
        tts = SiliconFlowTTS(cfg)
    elif name == "mlx":
        from .mlx import MlxTTS
        tts = MlxTTS(cfg)
    else:
        from .omnivoice import OmniVoiceTTS
        tts = OmniVoiceTTS(cfg.get("omnivoice_base") or "",
                           cfg.get("omnivoice_timeout_sec") or 120)
    tts.provider = name      # 热切 provider 时用它判断要不要重建实例
    return tts


def voices(cfg: dict) -> dict:
    """当前 provider 的音色预设表。"""
    cfg = cfg or {}
    name = provider_name(cfg)
    if name == "say":
        from .say import scan_voices
        found = scan_voices(cfg.get("voices_dir") or "")
        if found:
            return found
        voice = str(cfg.get("say_voice") or "Tingting")
        return {voice: {"name": voice, "label": voice, "kind": "say", "voice": voice}}
    if name == "edge":
        from .edge import BUILTIN_VOICES, scan_voices
        found = scan_voices(cfg.get("voices_dir") or "")
        if found:
            return found
        default = str(cfg.get("edge_voice") or "zh-CN-XiaoxiaoNeural")
        out = {k: dict(v) for k, v in BUILTIN_VOICES.items()}
        if default not in {v["voice"] for v in out.values()}:
            out["default"] = {"name": "default", "label": default, "kind": "edge",
                              "voice": default, "rate": "+0%", "pitch": "+0Hz"}
        return out
    if name == "siliconflow":
        from .siliconflow import BUILTIN_VOICES, scan_voices
        found = scan_voices(cfg.get("voices_dir") or "")
        if found:
            return found
        out = {k: dict(v) for k, v in BUILTIN_VOICES.items()}
        default = str(cfg.get("sf_voice") or "")
        if default and default not in {v["voice"] for v in out.values()}:
            out["default"] = {"name": "default", "label": default, "kind": "siliconflow",
                              "voice": default, "speed": 1.0}
        return out
    if name == "mlx":
        from .mlx import BUILTIN_VOICES, scan_voices
        out = {k: dict(v) for k, v in BUILTIN_VOICES.items()}   # 默认音色(无参考音频)始终可选
        out.update(scan_voices(cfg.get("voices_dir") or ""))
        return out
    from .omnivoice import scan_voices
    return scan_voices(cfg.get("voices_dir") or "")

"""edge-tts(微软在线神经音色)适配 —— 免费高质感中文女声, 不需要任何 Key。

定位: macOS say 音色机械、OmniVoice 又必须有 NVIDIA 显卡; edge-tts 用微软
Edge"朗读"的在线接口, 音色接近真人(晓晓/晓伊等), 延迟 ~0.5~1.5s, 纯 Python。
代价: **依赖网络且是非官方接口**, 可能被限流或失效 —— 直播生产环境建议
优先考虑远端 OmniVoice(可克隆音色), edge-tts 作为零成本起步方案。

依赖: uv pip install edge-tts  (首次缺失时 synth_pcm 会抛出带安装命令的报错)

音色预设(复用 tts.voices_dir, 子文件夹含 edge.json):
    {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+10%", "pitch": "+2Hz", "label": "晓晓"}
没有任何 edge.json 时, 用 config `tts.edge_voice`(默认晓晓)当唯一音色。
"""
from __future__ import annotations

import asyncio
import collections
import json
import pathlib
import tempfile
import time

from . import audio

# 无 voices_dir 时的内置预设: 挑了几个适合"甜美女主播"人设的
BUILTIN_VOICES: dict[str, dict] = {
    "xiaoxiao": {"name": "xiaoxiao", "label": "晓晓·甜美温柔",
                 "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                 "rate": "+0%", "pitch": "+0Hz"},
    "xiaoxiao_fast": {"name": "xiaoxiao_fast", "label": "晓晓·稍快语速",
                      "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                      "rate": "+12%", "pitch": "+0Hz"},
    "xiaoxiao_tuan": {"name": "xiaoxiao_tuan", "label": "晓晓·嗲妹妹(音调+25慢4%)",
                      "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                      "rate": "-4%", "pitch": "+25Hz"},
    "xiaoyi": {"name": "xiaoyi", "label": "晓伊·活泼俏皮",
               "kind": "edge", "voice": "zh-CN-XiaoyiNeural",
               "rate": "+0%", "pitch": "+2Hz"},
    "xiaoyi_tuan": {"name": "xiaoyi_tuan", "label": "晓伊·嗲妹妹(音调+20慢4%)",
                    "kind": "edge", "voice": "zh-CN-XiaoyiNeural",
                    "rate": "-4%", "pitch": "+20Hz"},
    "yujie": {"name": "yujie", "label": "御姐·冷艳(晓晓音调-12慢2%)",
              "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
              "rate": "-2%", "pitch": "-12Hz"},
    "yujie_deep": {"name": "yujie_deep", "label": "御姐·低沉慵懒(晓晓音调-18慢5%)",
                   "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                   "rate": "-5%", "pitch": "-18Hz"},
    "yujie_yi": {"name": "yujie_yi", "label": "御姐·飒爽(晓伊音调-8)",
                 "kind": "edge", "voice": "zh-CN-XiaoyiNeural",
                 "rate": "+0%", "pitch": "-8Hz"},
    "yujie_light": {"name": "yujie_light", "label": "御姐·轻调校(晓晓音调-8, 最自然)",
                    "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                    "rate": "+0%", "pitch": "-8Hz"},
    # 慵懒酷姐: 压低 + 明显放慢(慢才是"慵懒"的关键, 单纯降调只是"御姐")
    "cool_jie": {"name": "cool_jie", "label": "慵懒酷姐(晓晓音调-14慢12%)",
                 "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                 "rate": "-12%", "pitch": "-14Hz"},
    "cool_jie_lazy": {"name": "cool_jie_lazy", "label": "慵懒酷姐·极懒(晓晓音调-16慢18%)",
                      "kind": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                      "rate": "-18%", "pitch": "-16Hz"},
    "cool_jie_yi": {"name": "cool_jie_yi", "label": "酷姐·低沉磁性(晓伊音调-10慢10%)",
                    "kind": "edge", "voice": "zh-CN-XiaoyiNeural",
                    "rate": "-10%", "pitch": "-10Hz"},
    # 南方口音: 台湾腔最接近"南方人的普通话"; 粤语音色读的是粤语发音
    "tw_chen": {"name": "tw_chen", "label": "南方口音·台湾腔小陈",
                "kind": "edge", "voice": "zh-TW-HsiaoChenNeural",
                "rate": "+0%", "pitch": "+0Hz"},
    "tw_yu": {"name": "tw_yu", "label": "南方口音·台湾腔小瑜(软糯)",
              "kind": "edge", "voice": "zh-TW-HsiaoYuNeural",
              "rate": "+0%", "pitch": "+0Hz"},
    "hk_maan": {"name": "hk_maan", "label": "粤语·香港晓曼(读粤语)",
                "kind": "edge", "voice": "zh-HK-HiuMaanNeural",
                "rate": "+0%", "pitch": "+0Hz"},
    # 南方口音 × 御姐向
    "tw_chen_yujie": {"name": "tw_chen_yujie", "label": "御姐·台湾腔小陈(音调-8)",
                      "kind": "edge", "voice": "zh-TW-HsiaoChenNeural",
                      "rate": "+0%", "pitch": "-8Hz"},
    "tw_yu_yujie": {"name": "tw_yu_yujie", "label": "御姐·台湾腔小瑜(软糯, 音调-8)",
                    "kind": "edge", "voice": "zh-TW-HsiaoYuNeural",
                    "rate": "+0%", "pitch": "-8Hz"},
}

AUDIO_OK = ("audio", )


def _num(s: str, suffix: str) -> float:
    """'+12%' / '-18Hz' → 浮点; 解析不了当 0。"""
    try:
        return float(str(s).strip().removeprefix("+").removesuffix(suffix))
    except Exception:                                      # noqa: BLE001
        return 0.0


def _jitter(params: dict) -> dict:
    """每句加 ±1.5% 语速 / ±1.2Hz 音调的随机微抖动。

    真人说话每句的基调和节奏都有细微变化, 固定参数会让长直播听起来"太稳定"——
    这就是典型的AI感来源之一。抖动幅度刻意压在听感阈值附近: 有变化, 听不出"被调过"。
    """
    import random
    out = dict(params)
    r = int(round(_num(out["rate"], "%") + random.uniform(-1.5, 1.5)))
    p = int(round(_num(out["pitch"], "Hz") + random.uniform(-1.2, 1.2)))
    out["rate"] = f"{r:+d}%"
    out["pitch"] = f"{p:+d}Hz"
    return out


def scan_voices(voices_dir: str) -> dict[str, dict]:
    """扫描音色目录 → {name: preset}; 只收含 edge.json 的目录。"""
    out: dict[str, dict] = {}
    base = pathlib.Path(voices_dir or "")
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        f = entry / "edge.json"
        if not (entry.is_dir() and f.is_file()):
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        out[entry.name] = {
            "name": entry.name,
            "label": str(p.get("label") or entry.name),
            "kind": "edge",
            "voice": str(p.get("voice") or "zh-CN-XiaoxiaoNeural"),
            "rate": str(p.get("rate") or "+0%"),
            "pitch": str(p.get("pitch") or "+0Hz"),
            "volume": str(p.get("volume") or "+0%"),
        }
    return out


class EdgeTTS:
    """与 OmniVoiceTTS / MacSayTTS 鸭子类型一致(engine / bridge 都只认这几个成员)。"""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.cfg_base = ""                                  # 无远端服务; 留空占位
        self.timeout_sec = 60.0
        self.default_voice = str(cfg.get("edge_voice") or "zh-CN-XiaoxiaoNeural")
        self.default_rate = str(cfg.get("edge_rate") or "+0%")
        self.default_pitch = str(cfg.get("edge_pitch") or "+0Hz")
        self.default_volume = str(cfg.get("edge_volume") or "+0%")
        self.jitter = bool(cfg.get("edge_jitter", True))    # 逐句微抖动: 对抗"太稳定"的AI感
        self.recent_ms = collections.deque(maxlen=12)
        self.restart_count = 0
        self.last_restart_at = 0.0
        self.fail_streak = 0                                # 与 OmniVoiceTTS 同名: 看门狗读它
        self._lock = asyncio.Lock()

    # ---- 与 OmniVoiceTTS 对齐的只读接口 ----

    def slow_recent(self, limit_ms: int, n: int) -> bool:
        if n <= 0:
            return False
        vals = list(self.recent_ms)[-n:]
        return len(vals) >= n and all(v > limit_ms for v in vals)

    def active_base(self) -> str:
        return "edge-tts(微软在线)"

    # ---- 合成 ----

    def _params(self, preset: dict) -> dict:
        preset = preset or {}
        params = {
            "voice": str(preset.get("voice") or self.default_voice),
            "rate": str(preset.get("rate") or self.default_rate),
            "pitch": str(preset.get("pitch") or self.default_pitch),
            "volume": str(preset.get("volume") or self.default_volume),
        }
        if self.jitter:
            params = _jitter(params)
        return params

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        """文本 → PCM s16le/24kHz/mono。异常向上抛, 由引擎决定如何收尾。"""
        if not text.strip():
            return b""
        try:
            import edge_tts
        except ImportError as e:                           # noqa: F811
            raise RuntimeError(
                "edge-tts 未安装: 在项目根目录执行  uv pip install --python .venv/bin/python edge-tts"
            ) from e

        params = self._params(preset)
        async with self._lock:
            loop = asyncio.get_running_loop()
            t0 = time.monotonic()
            try:
                buf = bytearray()

                async def _pull() -> None:
                    com = edge_tts.Communicate(text, voice=params["voice"],
                                               rate=params["rate"],
                                               pitch=params["pitch"],
                                               volume=params["volume"])
                    async for chunk in com.stream():
                        if chunk["type"] in AUDIO_OK:
                            buf.extend(chunk["data"])

                await asyncio.wait_for(_pull(), timeout=self.timeout_sec)
                if not buf:
                    raise RuntimeError("edge-tts 返回了空音频(接口可能被限流, 稍后重试)")
                self.fail_streak = 0
            except Exception:
                self.fail_streak += 1
                raise
            finally:
                self.recent_ms.append(int((time.monotonic() - t0) * 1000))

            # mp3 落临时文件 → ffmpeg 统一转 PCM(与 OmniVoice 出口完全一致)
            fd = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            try:
                fd.write(bytes(buf))
                fd.close()
                return await loop.run_in_executor(
                    None, audio.decode_to_pcm, fd.name, None, "")
            finally:
                try:
                    pathlib.Path(fd.name).unlink(missing_ok=True)
                except OSError:
                    pass

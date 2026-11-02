"""本机 OmniVoice TTS(WSL2 常驻服务)直连适配 + 音色预设库。

- 引擎地址: **127.0.0.1:9877 优先**(走 WSL2 localhost 转发, 不可用时瞬时被拒);
  其次 config tts.omnivoice_base(WSL2 内网 IP, 默认 http://192.168.1.244:9877 ——
  2026-10-13 实测: WSL2 重启后该 IP 会变黑洞, 拨号挂几十秒); 最后
  D:\\speech-to-speech\\bridge-config.json 里记录的地址; 拨通后把可用值记进 _active_base
- 音色预设库: 复用 D:\\speech-to-speech\\voices 的目录约定(与语音桥同源):
    子文件夹含 design.json                      → omni-design(参数设计音色)
    子文件夹含 ref_audio.* + omnivoice.txt 标记 → omni-clone(克隆参考音频)
    只有 ref_audio.*(qwen3 预设)               → 跳过(本桥不加载 Qwen3)
- 调用: gradio_client(直接 HTTP 缺 session_hash 会 500, 必须走官方客户端库)
- 输出: OmniVoice 原生 24kHz 单声道 WAV → ffmpeg 统一转 PCM s16le/24k/mono
- 线程安全: gradio_client 非线程安全 → 全局锁串行; async 侧用 executor 不卡事件循环

与弹幕朗读(DSH 语音桥)的关系: 两边**音色状态独立**, 只共享底层引擎 ——
互不干扰; 若真出现引擎争抢, 给数字人单独再起一个 OmniVoice 实例改 base 即可。
"""
from __future__ import annotations

import collections
import time

import asyncio
import json
import pathlib
import threading

from . import audio

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}

# OmniVoice 公共采样参数(与语音桥 _synthesize_omnivoice 同源)
DEFAULT_PARAMS = {"lang": "Chinese", "ns": 32, "gs": 2.0, "dn": True,
                  "sp": 1.0, "du": None, "pp": True, "po": True}


# ───────────────────────── 音色预设库 ─────────────────────────

def scan_voices(voices_dir: str) -> dict[str, dict]:
    """扫描音色预设目录 → {name: preset}。preset.kind: design|clone。"""
    out: dict[str, dict] = {}
    base = pathlib.Path(voices_dir)
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        design_file = entry / "design.json"
        omni_marker = entry / "omnivoice.txt"
        audio_file = next((f for f in entry.iterdir()
                           if f.is_file() and f.suffix.lower() in AUDIO_EXTS), None)
        ref_text_file = next((f for f in entry.iterdir()
                              if f.is_file() and f.name.lower() in ("ref_text.txt", "ref_text.text")),
                             None)
        if design_file.is_file():
            try:
                params = json.loads(design_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            label = str(params.pop("label", name))
            out[name] = {"name": name, "label": label, "kind": "design", "params": params}
        elif audio_file is not None and omni_marker.is_file():
            ref_text = ref_text_file.read_text(encoding="utf-8", errors="ignore").strip() \
                if ref_text_file is not None else ""
            # 可选 label.txt: 给页面下拉/音色卡用的中文显示名(缺省=目录名)
            label = name
            label_file = entry / "label.txt"
            if label_file.is_file():
                try:
                    label = label_file.read_text(encoding="utf-8", errors="ignore").strip() or name
                except Exception:                      # noqa: BLE001 标签读失败绝不影响音色
                    label = name
            out[name] = {"name": name, "label": label, "kind": "clone",
                         "ref_audio": str(audio_file), "ref_text": ref_text}
        # 只有 ref_audio 的 qwen3 预设: 本桥不用, 跳过
    return out


# ───────────────────────── OmniVoice 引擎 ─────────────────────────

class OmniVoiceTTS:
    def __init__(self, base: str, timeout_sec: float = 120):
        self.cfg_base = (base or "").rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self._client = None                    # gradio Client 单例(惰性)
        self._active_base = ""                 # 实际拨通的地址
        self._lock = threading.Lock()          # gradio_client 非线程安全
        self._loop_lock = asyncio.Lock()       # async 侧串行
        # 2026-10-29 显存看门狗用: 最近若干次合成的耗时(ms) + 重启计数
        self.recent_ms = collections.deque(maxlen=12)
        self.restart_count = 0
        self.last_restart_at = 0.0
        self.fail_streak = 0          # 连续合成失败次数(看门狗用)

    # ---- 地址解析: 配置值 → 127.0.0.1 → 语音桥 bridge-config.json 记录值 ----

    def _candidate_bases(self) -> list[str]:
        """候选地址顺序: **127.0.0.1 优先**。
       为什么(2026-10-13 实测): WSL2 重启后内网 IP 会变/暂时不可达, 而配置里那个内网 IP
        会变成 TCP 黑洞 —— 拨号要挂几十秒才超时; 127.0.0.1 走 WSL 的 localhost 转发,
        不可用时是**瞬时被拒**(立刻换下一个候选)。所以本地优先, 配置值/语音桥记录值兜底。"""
        cands: list[str] = ["http://127.0.0.1:9877"]
        if self.cfg_base:
            cands.append(self.cfg_base)
        try:
            bc = json.loads(pathlib.Path(r"<语音桥目录>\bridge-config.json")
                            .read_text(encoding="utf-8"))
            b = str(((bc.get("omnivoice") or {}).get("base")) or "").rstrip("/")
            if b:
                cands.append(b)
        except Exception:
            pass
        seen, out = set(), []
        for c in cands:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _make_client(self, base: str):
        from gradio_client import Client
        return Client(base, verbose=False)

    def _ensure_client(self):
        """拿一个已拨通的 Client; 没有就按候选地址依次试。"""
        if self._client is not None:
            return self._client
        errs = []
        for base in self._candidate_bases():
            try:
                c = self._make_client(base)
                self._client = c
                self._active_base = base
                return c
            except Exception as e:  # noqa: BLE001
                errs.append(f"{base}: {type(e).__name__}")
        raise RuntimeError("OmniVoice 服务不可达: " + " | ".join(errs))

    def _reset_client(self):
        self._client = None
        self._active_base = ""

    # ---- 合成 ----

    def _predict_sync(self, text: str, preset: dict) -> str:
        """阻塞调用 gradio → 返回 wav 本地路径。只在线程池里跑。"""
        _t0 = time.monotonic()
        try:
            out = self._predict_inner(text, preset)
            self.fail_streak = 0
            return out
        except Exception:
            self.fail_streak = getattr(self, "fail_streak", 0) + 1
            raise
        finally:
            self.recent_ms.append(int((time.monotonic() - _t0) * 1000))

    def slow_recent(self, limit_ms: int, n: int) -> bool:
        """最近 n 次合成是否都超过 limit_ms(看门狗用)。"""
        if n <= 0:
            return False
        vals = list(self.recent_ms)[-n:]
        return len(vals) >= n and all(v > limit_ms for v in vals)

    def _predict_inner(self, text: str, preset: dict) -> str:
        params = dict(DEFAULT_PARAMS)
        params.update(preset.get("params") or {})
        with self._lock:
            client = self._ensure_client()
            try:
                if preset.get("kind") == "design":
                    result = client.predict(
                        text, params.get("lang", "Chinese"),
                        int(params.get("ns", 32)), float(params.get("gs", 2.0)),
                        bool(params.get("dn", True)), float(params.get("sp", 1.0)),
                        params.get("du"), bool(params.get("pp", True)),
                        bool(params.get("po", True)),
                        params.get("gender", "Auto"), params.get("age", "Auto"),
                        params.get("pitch", "Auto"), params.get("style", "Auto"),
                        params.get("accent", "Auto"), params.get("dialect", "Auto"),
                        api_name="/_design_fn",
                    )
                else:  # clone
                    from gradio_client import handle_file
                    result = client.predict(
                        text, params.get("lang", "Chinese"),
                        handle_file(preset["ref_audio"]), preset.get("ref_text", ""),
                        "",  # instruct
                        int(params.get("ns", 32)), float(params.get("gs", 2.0)),
                        bool(params.get("dn", True)), float(params.get("sp", 1.0)),
                        params.get("du"), bool(params.get("pp", True)),
                        bool(params.get("po", True)),
                        api_name="/_clone_fn",
                    )
            except Exception as exc:  # noqa: BLE001
                # 客户端可能因服务重启而失效: 重置后允许下次重新拨号
                self._reset_client()
                raise RuntimeError(f"OmniVoice 合成失败: {type(exc).__name__}: {exc}") from exc
        wav_path, status = result[0], result[1]
        if status and "Done" not in str(status) and "成功" not in str(status):
            # 服务端偶尔返回非 Done 的状态串, 保守起见记录但不一票否决
            pass
        return str(wav_path)

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        """文本 → PCM s16le/24kHz/mono。异常向上抛, 由引擎决定如何收尾。"""
        if not text.strip():
            return b""
        async with self._loop_lock:
            loop = asyncio.get_running_loop()
            try:
                wav_path = await loop.run_in_executor(None, self._predict_sync, text, preset)
            except RuntimeError:
                raise
            pcm = await loop.run_in_executor(None, audio.decode_to_pcm, wav_path, None, "")
        self._drop_wav(wav_path)
        return pcm

    @staticmethod
    def _drop_wav(wav_path: str) -> None:
        """合成产物用完即删。背景(2026-10-13): gradio 客户端每次合成把服务端 wav 下载进
        %TEMP%\\gradio\\<hash>\\audio.wav 且**从不清理** —— 长跑实测 5989 个文件/1.0GB,
        是「越跑越卡」排查里坐实的无界累积物之一。解码进 PCM 后这份文件即无用。"""
        try:
            p = pathlib.Path(wav_path)
            p.unlink(missing_ok=True)
            try:
                p.parent.rmdir()          # 空哈希目录顺手带走(非空会抛 OSError, 忽略)
            except OSError:
                pass
        except Exception:                 # 清理失败绝不影响合成主链路
            pass

    def active_base(self) -> str:
        return self._active_base or self.cfg_base


def sweep_client_cache(max_age_sec: float = 7200) -> int:
    """兜底清理 gradio 客户端缓存目录(`%TEMP%\\gradio\\<hash>\\`), 返回删掉的文件数。

    为什么需要(2026-10-13): gradio_client 每合成一次就把服务端 wav 下载进本目录, **从不清理**
    (长跑实测 5989 个文件 / 1.0GB)。本桥自己的产物已在 _drop_wav 里逐个删掉, 但
    **D:\\speech-to-speech 那个语音桥(8765)与本桥共用同一个 %TEMP%\\gradio**, 它下载的
    文件我们没有机会删 —— 所以按年龄兜底(默认删超过 2 小时的, 不碰在途文件)。
    开销: 一次目录遍历(每 30 分钟由 bridge._tts_cache_gc_loop 触发), 无额外进程。"""
    import tempfile
    import time as _time

    n = 0
    now = _time.time()
    root = pathlib.Path(tempfile.gettempdir()) / "gradio"
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file() and now - p.stat().st_mtime > max_age_sec:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    except Exception:                     # noqa: BLE001 清理失败绝不影响合成
        pass
    return n

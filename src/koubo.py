"""口播工作台: 长文切分 → 分段 TTS(OmniVoice) → 顺序喂给组件版数字人。

与直播链路共用同一个 bridge/engine: 只是把"一句一句说话"变成"一段一段点播"。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import time
import wave

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config as appconf
from .tts import audio as tts_audio
from .tts.omnivoice import OmniVoiceTTS, scan_voices

ROOT = pathlib.Path(__file__).resolve().parent.parent
STORE = ROOT / "koubo" / "project.json"
# 2026-10-29: 口播合成产物单独放 koubo/audio —— 原来写进 audio/ 会让"本地点播目录"
# 冒出 26 个 koubo_s*.wav, 混在歌单里很难找歌。
AUDIO_DIR = ROOT / "koubo" / "audio"

router = APIRouter()
ENGINE = None
TTS: OmniVoiceTTS | None = None
_TTS_LOCK = asyncio.Lock()
_PLAY_TASK: asyncio.Task | None = None
_TTS_TASK: asyncio.Task | None = None
_TTS_JOB: dict = {"running": False, "done": 0, "total": 0, "idx": None, "stop": False, "error": None}

# URL / 邮箱 / 版本号 / 时间 / 小数 / 百分数 / 英文单词 —— 这些原子不能被切断
_PROTECT = re.compile(
    r"(https?://[^\s，。；！？、）】」』\"']+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\d{1,2}:\d{2}(?::\d{2})?"
    r"|v?\d+\.\d+(?:\.\d+)*%?"
    r"|\d+(?:,\d{3})*(?:\.\d+)?%?"
    r"|[A-Za-z][A-Za-z0-9'._+\-/]*"
    r")"
)


def init(engine) -> None:
    global ENGINE
    ENGINE = engine


# ── 状态 ─────────────────────────────────────────────────────────────────────
def _blank() -> dict:
    return {"text": "", "max_chars": 60, "segments": [], "playing": False,
            "play_idx": None, "stop": False, "updated": time.time()}


def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _blank()


def save(st: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    st["updated"] = time.time()
    STORE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _seg(i: int) -> dict:
    return {"id": f"s{i:02d}", "text": "", "takes": [], "selected": None,
            "action": "", "status": ""}


# ── 切分 ─────────────────────────────────────────────────────────────────────
def _safe_spans(s: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _PROTECT.finditer(s)]


def _inside(spans, pos: int) -> bool:
    return any(a < pos < b for a, b in spans)


_CLAUSE_PUNCT = "。！？!?；;，,、：:"


def _clauses(text: str) -> list[str]:
    """按标点切成最小语义单元（保留标点）；URL/英文/数字内部不切。"""
    spans = _safe_spans(text)
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in _CLAUSE_PUNCT and not _inside(spans, i + 1):
            piece = text[start:i + 1].strip()
            if piece:
                out.append(piece)
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _split_long(sent: str, max_chars: int) -> list[str]:
    """只处理“单句本身超过软上限”的极端情况：仍优先标点，最后才按 token 边界硬切。"""
    spans = _safe_spans(sent)
    out = []
    while len(sent) > max_chars:
        window = sent[: max_chars + 1]
        cut = -1
        for i in range(len(window) - 1, max(0, int(max_chars * 0.5)), -1):
            if window[i] in _CLAUSE_PUNCT and not _inside(spans, i + 1):
                cut = i + 1
                break
        if cut < 0:
            cut = max_chars
            while cut > max_chars * 0.6 and _inside(spans, cut):
                cut -= 1
            if cut <= max_chars * 0.6:
                for a, b in spans:
                    if a < max_chars < b:
                        cut = b
                        break
        out.append(sent[:cut].strip())
        sent = sent[cut:].strip()
        spans = _safe_spans(sent)
    if sent:
        out.append(sent)
    return out


def split_text(text: str, max_chars: int) -> list[str]:
    """标点优先的切分：字数只是“软目标”，默认允许超 25%，绝不为了凑字数把句子拦腰切断。"""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    max_chars = max(20, int(max_chars))
    soft = int(max_chars * 1.35)

    segs, buf = [], ""
    for c in _clauses(text):
        if not buf:
            buf = c
            continue
        if len(buf) + len(c) <= soft:
            buf += c
        else:
            segs.append(buf)
            buf = c
    if buf:
        segs.append(buf)

    out = []
    for s in segs:
        s = s.strip()
        if not s:
            continue
        if len(s) <= max(int(max_chars * 1.5), max_chars):
            out.append(s)
        else:
            out.extend(x for x in _split_long(s, max_chars) if x)
    # 尾段过短就并入上一段（宁可略超，也不要留一个语气不完整的尾巴）
    if len(out) >= 2 and len(out[-1]) < max(10, max_chars // 4):
        out[-2] += out[-1]
        out.pop()
    return out


def _renumber(st: dict) -> None:
    for i, seg in enumerate(st["segments"], 1):
        old = seg["id"]
        seg["id"] = f"s{i:02d}"
        if old != seg["id"]:
            for t in seg.get("takes") or []:
                t["file"] = t.get("file", "")


# ── TTS ──────────────────────────────────────────────────────────────────────
def _tts() -> OmniVoiceTTS:
    global TTS
    if TTS is None:
        c = (appconf.load().get("tts") or {})
        TTS = OmniVoiceTTS(c.get("omnivoice_base") or "http://127.0.0.1:9877",
                           c.get("omnivoice_timeout_sec") or 120)
    return TTS


def _preset() -> dict:
    c = (appconf.load().get("tts") or {})
    voices = scan_voices(c.get("voices_dir") or r"<语音桥目录>\voices")
    name = c.get("voice") or "tw-xiaoya"
    if name not in voices:
        raise RuntimeError(f"音色不存在: {name}")
    return voices[name]


def _wav_dur(path: pathlib.Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


async def _do_tts(seg: dict) -> dict:
    text = (seg.get("text") or "").strip()
    if not text:
        raise RuntimeError("该段没有文本")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    n = len(seg.get("takes") or []) + 1
    name = f"koubo_{seg['id']}_v{n}.wav"
    dst = AUDIO_DIR / name
    async with _TTS_LOCK:
        loop = asyncio.get_running_loop()
        wav_path = await loop.run_in_executor(None, _tts()._predict_sync, text, _preset())
        dst.write_bytes(pathlib.Path(wav_path).read_bytes())
        try:
            pathlib.Path(wav_path).unlink(missing_ok=True)
        except Exception:
            pass
    take = {"file": name, "dur": round(_wav_dur(dst), 2), "ts": time.time()}
    seg.setdefault("takes", []).append(take)
    seg["selected"] = len(seg["takes"]) - 1
    return take


# ── DeepSeek 动作指令 ────────────────────────────────────────────────────────
async def _gen_action(text: str) -> str:
    c = (appconf.load().get("llm") or {})
    key = ""
    kf = (c.get("key_file") or "").strip()
    if kf:
        try:
            key = pathlib.Path(kf).read_text(encoding="utf-8").strip()
        except Exception:
            key = ""
    if not key:
        import os
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("DeepSeek key 找不到")
    prompt = (
        "你是数字人动作导演。下面是一句口播台词，请设计一条动作指令（20~40 字）。\n"
        "要求：只描述表情、视线、手势、身体姿态；与台词内容自然匹配；幅度克制，"
        "不要遮挡脸部、不要把手怼到镜头前、不要大范围走动；不重复台词；只输出指令本身。\n\n"
        f"台词：{text}"
    )
    body = {
        "model": c.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens": 120,
        "thinking": {"type": "disabled"},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = (c.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0)) as cli:
        r = await cli.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"DeepSeek {r.status_code}: {r.text[:300]}")
        data = r.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    out = (msg.get("content") or "").strip()
    out = re.sub(r"^[\"'“”「」]|[\"'“”「」]$", "", out).strip()
    return out[:80]


# ── API ─────────────────────────────────────────────────────────────────────
class SplitBody(BaseModel):
    text: str | None = None
    max_chars: int | None = None


class SegBody(BaseModel):
    id: str
    text: str | None = None


class MergeBody(BaseModel):
    ids: list[str]


class SplitSegBody(BaseModel):
    id: str
    pos: int


class TakeBody(BaseModel):
    id: str
    take: int


class PlayBody(BaseModel):
    id: str | None = None
    gap: float = 0.0


class TtsAllBody(BaseModel):
    only_missing: bool = True


@router.get("/api/koubo/state")
async def kb_state():
    st = load()
    st.pop("stop", None)
    st["tts_job"] = dict(_TTS_JOB)
    return st


@router.post("/api/koubo/split")
async def kb_split(body: SplitBody):
    st = load()
    if body.text is not None:
        st["text"] = body.text
    if body.max_chars:
        st["max_chars"] = max(20, min(200, int(body.max_chars)))
    parts = split_text(st.get("text") or "", int(st.get("max_chars") or 60))
    st["segments"] = []
    for i, t in enumerate(parts, 1):
        s = _seg(i)
        s["text"] = t
        st["segments"].append(s)
    save(st)
    return {"ok": True, "count": len(parts), "segments": st["segments"]}


@router.post("/api/koubo/seg")
async def kb_seg(body: SegBody):
    st = load()
    seg = next((s for s in st["segments"] if s["id"] == body.id), None)
    if not seg:
        raise HTTPException(404, "段不存在")
    if body.text is not None:
        seg["text"] = body.text
        seg["takes"] = []
        seg["selected"] = None
        seg["action"] = ""
    save(st)
    return {"ok": True, "segment": seg}


@router.post("/api/koubo/merge")
async def kb_merge(body: MergeBody):
    st = load()
    idx = [i for i, s in enumerate(st["segments"]) if s["id"] in body.ids]
    if len(idx) < 2:
        raise HTTPException(400, "至少选两段")
    idx.sort()
    if idx != list(range(idx[0], idx[-1] + 1)):
        raise HTTPException(400, "只能合并相邻段")
    merged = "".join(st["segments"][i]["text"] for i in idx)
    st["segments"][idx[0]]["text"] = merged
    st["segments"][idx[0]]["takes"] = []
    st["segments"][idx[0]]["selected"] = None
    for i in reversed(idx[1:]):
        st["segments"].pop(i)
    _renumber(st)
    save(st)
    return {"ok": True, "segments": st["segments"]}


@router.post("/api/koubo/seg/split")
async def kb_split_seg(body: SplitSegBody):
    st = load()
    i = next((k for k, s in enumerate(st["segments"]) if s["id"] == body.id), None)
    if i is None:
        raise HTTPException(404, "段不存在")
    text = st["segments"][i]["text"]
    pos = max(1, min(len(text) - 1, int(body.pos)))
    # 不切进 URL / 英文 / 数字：就近挪到 token 边界
    for a, b in _safe_spans(text):
        if a < pos < b:
            pos = b if (b - pos) <= (pos - a) else a
            break
    left, right = text[:pos], text[pos:]
    st["segments"][i]["text"] = left
    st["segments"][i]["takes"] = []
    st["segments"][i]["selected"] = None
    new = _seg(i + 2)
    new["text"] = right
    st["segments"].insert(i + 1, new)
    _renumber(st)
    save(st)
    return {"ok": True, "segments": st["segments"]}


@router.post("/api/koubo/tts")
async def kb_tts(body: SegBody):
    st = load()
    seg = next((s for s in st["segments"] if s["id"] == body.id), None)
    if not seg:
        raise HTTPException(404, "段不存在")
    try:
        take = await _do_tts(seg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    save(st)
    return {"ok": True, "segment": seg, "take": take}


@router.post("/api/koubo/tts_all")
async def kb_tts_all(body: TtsAllBody):
    """一键按顺序合成所有段落（默认只补缺的），后台跑，状态走 /state 的 tts_job。"""
    global _TTS_TASK
    if _TTS_JOB.get("running"):
        raise HTTPException(409, "正在批量合成")
    st = load()
    targets = [s for s in st["segments"]
               if (s.get("text") or "").strip() and (not body.only_missing or not s.get("takes"))]
    if not targets:
        return {"ok": True, "total": 0, "message": "没有需要合成的段"}
    if _TTS_TASK and not _TTS_TASK.done():
        raise HTTPException(409, "正在批量合成")

    _TTS_JOB.update(running=True, done=0, total=len(targets), idx=None, stop=False, error=None)

    async def run():
        try:
            for s in targets:
                if _TTS_JOB.get("stop"):
                    break
                _TTS_JOB["idx"] = s["id"]
                cur_state = load()
                cur = next((x for x in cur_state["segments"] if x["id"] == s["id"]), None)
                if cur is None:
                    _TTS_JOB["done"] += 1
                    continue
                if body.only_missing and cur.get("takes"):
                    _TTS_JOB["done"] += 1
                    continue
                try:
                    await _do_tts(cur)
                    save(cur_state)
                except Exception as e:  # noqa: BLE001
                    cur["status"] = f"tts error: {e}"[:120]
                    save(cur_state)
                    _TTS_JOB["error"] = f"{s['id']}: {e}"
                _TTS_JOB["done"] += 1
        finally:
            _TTS_JOB["running"] = False
            _TTS_JOB["idx"] = None

    _TTS_TASK = asyncio.create_task(run())
    return {"ok": True, "total": len(targets)}


@router.post("/api/koubo/select")
async def kb_select(body: TakeBody):
    st = load()
    seg = next((s for s in st["segments"] if s["id"] == body.id), None)
    if not seg or not (0 <= body.take < len(seg.get("takes") or [])):
        raise HTTPException(400, "take 不存在")
    seg["selected"] = body.take
    save(st)
    return {"ok": True, "segment": seg}


@router.post("/api/koubo/action")
async def kb_action(body: SegBody):
    st = load()
    seg = next((s for s in st["segments"] if s["id"] == body.id), None)
    if not seg:
        raise HTTPException(404, "段不存在")
    try:
        seg["action"] = await _gen_action(seg["text"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    save(st)
    return {"ok": True, "segment": seg}


@router.post("/api/koubo/actions_all")
async def kb_actions_all():
    st = load()
    errs = []
    for seg in st["segments"]:
        if seg.get("action"):
            continue
        try:
            seg["action"] = await _gen_action(seg["text"])
        except Exception as e:  # noqa: BLE001
            errs.append(f"{seg['id']}: {e}")
    save(st)
    return {"ok": True, "errors": errs, "segments": st["segments"]}


def _take_path(seg: dict) -> pathlib.Path:
    takes = seg.get("takes") or []
    if not takes:
        raise RuntimeError("该段还没合成音频")
    k = seg.get("selected")
    if k is None or not (0 <= k < len(takes)):
        k = 0
    p = AUDIO_DIR / takes[k]["file"]
    if not p.is_file():
        raise RuntimeError(f"音频文件不存在: {takes[k]['file']}")
    return p


async def _decode_seg(seg: dict) -> bytes:
    """把一段 wav 解码成 Vidu 要的 PCM（24k s16le mono），供预解码队列使用。"""
    p = _take_path(seg)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, tts_audio.decode_to_pcm, str(p), None, "")


async def _play_one(seg: dict) -> None:
    pcm = await _decode_seg(seg)
    await ENGINE.say_pcm(pcm, script=seg.get("text") or "", label=seg["id"])


@router.post("/api/koubo/play")
async def kb_play(body: PlayBody):
    if ENGINE is None or not getattr(ENGINE, "_ws_ok", lambda: False)():
        raise HTTPException(409, "还没有连线")
    st = load()
    seg = next((s for s in st["segments"] if s["id"] == body.id), None)
    if not seg:
        raise HTTPException(404, "段不存在")
    try:
        await _play_one(seg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    return {"ok": True}


async def _wait_speech_done(timeout: float = 600.0) -> bool:
    """等当前这一段真正喂完（say_file 是后台任务，会立刻返回）。
    返回 False 表示被 stop/断线打断。"""
    await asyncio.sleep(0.05)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ENGINE is None or not getattr(ENGINE, "_ws_ok", lambda: False)():
            return False
        sp = getattr(ENGINE, "speaking_text", "")
        task = getattr(ENGINE, "_speak_task", None)
        if not sp and (task is None or task.done()):
            return True
        await asyncio.sleep(0.03)
    return False


@router.post("/api/koubo/play_all")
async def kb_play_all(body: PlayBody):
    global _PLAY_TASK
    if ENGINE is None or not getattr(ENGINE, "_ws_ok", lambda: False)():
        raise HTTPException(409, "还没有连线")
    st = load()
    if not any(s.get("takes") for s in st["segments"]):
        raise HTTPException(400, "还没有任何分段音频")
    if _PLAY_TASK and not _PLAY_TASK.done():
        raise HTTPException(409, "正在连播")

    async def run():
        s2 = load()
        s2["playing"] = True
        s2["stop"] = False
        save(s2)
        try:
            segs = [s for s in s2["segments"] if s.get("takes")]
            if not segs:
                return
            # 预解码第一段
            pcm = await _decode_seg(segs[0])
            for i, seg in enumerate(segs):
                if load().get("stop"):
                    break
                s2 = load()
                cur = next((x for x in s2["segments"] if x["id"] == seg["id"]), None)
                if cur is None:
                    continue
                s2["play_idx"] = cur["id"]
                cur["status"] = "playing"
                save(s2)
                ok = True
                try:
                    await ENGINE.say_pcm(pcm, script=cur.get("text") or "", label=cur["id"], barge=False)
                except Exception as e:  # noqa: BLE001
                    ok = False
                    cur["status"] = f"error: {e}"[:120]
                    save(s2)
                # 当前段还在播时，后台预解码下一段（切换不再等解码）
                next_task = None
                if i + 1 < len(segs):
                    next_task = asyncio.create_task(_decode_seg(segs[i + 1]))
                await _wait_speech_done()
                if not load().get("stop"):
                    if ok:
                        cur["status"] = "done"
                    save(s2)
                if load().get("stop"):
                    if next_task:
                        next_task.cancel()
                    break
                if next_task:
                    pcm = await next_task
                if body.gap and body.gap > 0:
                    await asyncio.sleep(float(body.gap))
        finally:
            s2 = load()
            s2["playing"] = False
            s2["play_idx"] = None
            save(s2)

    _PLAY_TASK = asyncio.create_task(run())
    return {"ok": True}


@router.post("/api/koubo/stop")
async def kb_stop():
    st = load()
    st["stop"] = True
    save(st)
    _TTS_JOB["stop"] = True
    if ENGINE is not None:
        try:
            await ENGINE.interrupt()
        except Exception:
            pass
    return {"ok": True}


@router.get("/api/koubo/audio/{name}")
async def kb_audio(name: str):
    if "/" in name or "\\" in name:
        raise HTTPException(400, "bad name")
    p = AUDIO_DIR / name
    if not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="audio/wav")






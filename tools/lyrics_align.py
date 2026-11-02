#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""歌词打轴(方案C): 本机 ASR 字级时间戳 × 歌词 md 的成曲顺序/字数定额
→ 生成逐行字幕缓存 .lrc.json, 供桥在点播喂流时按毫秒发 type10。

实测结论(2026-10-09): SeACo-Paraformer 对「AI翻唱+伴奏」几乎失聪(270s 只出 1 字),
**主用 faster-whisper large-v3**(词级时间戳 + 内置 VAD 跳过前奏/间奏); SeACo 仅作兜底。
对齐原理不依赖 ASR 文本准确性, 只用**字级时间戳的单调性**: 各行字数配额占比 → 切 ASR 字流时间轴。
前奏/间奏期间没有歌声 → 行时间自然跳过乐器段。

用法(用带 funasr/faster-whisper 的 venv-speech 跑, 不是项目 venv):
  <语音桥目录>\venv-speech\Scripts\python.exe tools\lyrics_align.py "<歌曲.flac>"
  <语音桥目录>\venv-speech\Scripts\python.exe tools\lyrics_align.py --all     # 批量: song_dirs 全部
  <语音桥目录>\venv-speech\Scripts\python.exe tools\lyrics_align.py "<歌>" --force   # 忽略缓存重打

输出: <项目>/lyrics-cache/<歌名>.lrc.json
  {"song":..., "duration_ms":..., "lines":[{"text","start","end"}], "method":"whisper-large-v3", ...}
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

FFMPEG = r"C:\ffmpeg7.1.1\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg7.1.1\bin\ffprobe.exe"
SEACO = r"<语音桥目录>\models\funasr\speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
WHISPER_LOCAL = Path(r"<语音桥目录>\models\whisper\faster-whisper-large-v3")
ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "lyrics-cache"
CJK_RE = re.compile(r"[\u4e00-\u9fff a-zA-Z0-9]")


# ───────────────────────── 歌词 md 解析 ─────────────────────────

def parse_lyric_md(md_path: Path) -> dict:
    """从歌词 md 提取: 成曲顺序展开后的逐行新词(+字数定额)。
    文件结构(用户工作流生成):
      ### B1 [Verse] → 表格 | 字数 ✓ | 原词 | **新词** |
      **成曲顺序**：`B1,B2,B3,B4,B2,B2`
    """
    text = md_path.read_text(encoding="utf-8-sig", errors="replace")

    m = re.search(r"\*\*成曲顺序\*\*[：:]\s*`([^`]+)`", text)
    if not m:
        raise ValueError("md 里没找到「成曲顺序」")
    order = [s.strip() for s in re.split(r"[,，、\s]+", m.group(1).strip()) if s.strip()]

    sections: dict[str, list[dict]] = {}
    cur = None
    for line in text.splitlines():
        hs = re.match(r"^#{2,4}\s*(\w+)\s*\[", line.strip())
        if hs:
            cur = hs.group(1)
            sections.setdefault(cur, [])
            continue
        if cur is None or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= set("-: "):
            continue                      # 表头/分隔行
        qm = re.match(r"^(\d+)", cells[0])
        if not qm:
            continue
        new_words = ""
        bm = re.search(r"\*\*(.+?)\*\*", cells[-1])
        if bm:
            new_words = bm.group(1).strip()
        elif cells[-1] not in ("", "-"):
            new_words = cells[-1].strip()
        if new_words:
            sections[cur].append({"text": new_words, "quota": int(qm.group(1))})

    lines: list[dict] = []
    for sec in order:
        if sec not in sections or not sections[sec]:
            raise ValueError(f"成曲顺序里的段落 {sec} 在逐行对照中没找到/为空")
        lines.extend(json.loads(json.dumps(sections[sec])))   # 深拷贝(重复段落)
    if not lines:
        raise ValueError("没解析出任何歌词行")
    return {"lines": lines, "order": order,
            "total_quota": sum(l["quota"] for l in lines)}


# ───────────────────────── 歌名 ↔ 歌词 配对 ─────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s（）()\[\]【】《》·、，。\-—_.]+", "", s or "").lower()


def song_orig_name(song_path: Path) -> str:
    """(原曲名)-AI版-第N封·xxx.flac → 原曲名"""
    m = re.match(r"^\((.+?)\)", song_path.stem)
    return m.group(1) if m else song_path.stem


def _track_no(s: str) -> str:
    """曲目编号前缀: "01 你藏进…" / "01 Jam-南" → "01"; 没有则空串。"""
    m = re.match(r"^(\d{1,3})[ .、_-]", (s or "").strip())
    return m.group(1) if m else ""


def find_lyric_md(song_path: Path, lyrics_dirs: Path | list) -> Path | None:
    """按歌名配对歌词 md; 配不上再**按曲目编号兜底**。

    为什么需要兜底(2026-10-17 新专辑): 音频按"首句歌词"命名(01 你藏进旧衣橱里望见茶盏的雾气.flac),
    歌词 md 按"原曲名"命名(01 Jam-南.md) → 歌名相似度几乎为 0, 但**编号一致**可稳定配对。
    lyrics_dirs: 单个目录或目录列表(多个合集各有自己的 _歌词 目录)。
    优先级: 歌名匹配(≥0.75) > 曲目编号兜底。"""
    raw = lyrics_dirs if isinstance(lyrics_dirs, (list, tuple)) else [lyrics_dirs]
    dirs = [Path(d) for d in raw if str(d or "").strip()]
    orig = song_orig_name(song_path)
    song_no = _track_no(song_path.stem)
    best: tuple[float, Path] | None = None      # 歌名匹配
    track_same: Path | None = None              # 编号匹配 + 同专辑目录(优先)
    track_other: Path | None = None             # 编号匹配 + 别的目录
    for d in dirs:
        if not d.is_dir():
            continue
        same_album = (d.parent == song_path.parent)   # .../新专辑_待命名/_歌词 vs .../新专辑_待命名/x.flac
        for md in sorted(d.glob("*.md")):
            if md.name.startswith("_"):
                continue                            # 合集/索引文件
            stem = md.stem
            tail = re.split(r"[-—]", stem, maxsplit=1)[-1]     # 去掉 "NN 歌手-" 前缀
            score = 1.0 if _norm(tail) == _norm(orig) else _ratio(_norm(tail), _norm(orig))
            if score >= 0.75 and (best is None or score > best[0]):
                best = (score, md)
            if song_no and _track_no(stem) == song_no:
                # ⚠️ 两个专辑都有 01/02/… 编号 → 只按编号会串台(踩过): 同专辑目录优先
                if same_album and track_same is None:
                    track_same = md
                elif track_other is None:
                    track_other = md
    return best[1] if best else (track_same or track_other)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


# ───────────────────────── ASR 后端 ─────────────────────────

def audio_duration_ms(path: Path) -> int:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, timeout=60)
    return int(float(r.stdout.decode().strip()) * 1000)


def _to_wav16k(song: Path) -> Path:
    tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tf.close()
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(song),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", tf.name],
                   check=True, timeout=600)
    return Path(tf.name)


def _add_nvidia_dll_dirs() -> None:
    """ctranslate2 用裸 LoadLibrary 找 cuBLAS/cuDNN → 只认 PATH(add_dll_directory 无效)。
    nvidia pip wheel 的 DLL 在 site-packages/nvidia/*/bin。"""
    import os
    import sysconfig
    sp = Path(sysconfig.get_paths()["purelib"])
    parts = []
    for rel in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
        d = sp / rel
        if d.is_dir():
            parts.append(str(d))
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
    if parts:
        os.environ["PATH"] = os.pathsep.join(parts) + os.pathsep + os.environ.get("PATH", "")


def asr_whisper(wav16k: Path, model_size: str = "large-v3", use_vad: bool = True,
                lang: str = "zh"):
    """faster-whisper: 返回 (每字开始ms, 每字结束ms, 识别文本, 段列表)。词跨度均分到单字。
    直接喂 numpy 数组绕过 PyAV(av>=15 删了 metadata_errors, 传路径会炸)。
    ⚠️ 实测教训(2026-10-09): initial_prompt+condition_on_previous_text=False 对唱歌是
    负优化(上下文条件帮 whisper 在音乐里保持连贯, 加 prompt 反而扰乱识别分布),
    全曲锚点率普遍下降 → 保持默认参数。
    use_vad: 重伴奏的 AI 翻唱里 Silero VAD 会把演唱当静音吞掉(实测《类似爱情》281s 只出 47 字),
    锚点过少时用 use_vad=False 重识别自救。"""
    import os
    import wave

    import numpy as np
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from faster_whisper import WhisperModel
    _add_nvidia_dll_dirs()
    src = str(WHISPER_LOCAL) if WHISPER_LOCAL.is_dir() else model_size
    model = WhisperModel(src, device="cuda", compute_type="float16")
    with wave.open(str(wav16k), "rb") as wf:
        sr = wf.getframerate()
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    arr = samples.astype(np.float32) / 32768.0
    segments, info = model.transcribe(
        arr, language=(lang or None), word_timestamps=True, vad_filter=use_vad,
        vad_parameters={"min_silence_duration_ms": 500})
    starts, ends, chars = [], [], []
    segs = []                                 # (start_ms, end_ms, text)
    for seg in segments:
        s_txt = ""
        for w in (seg.words or []):
            t = (w.word or "").strip()
            if not t:
                continue
            n = len(t)
            span = (w.end - w.start) * 1000.0
            for i, ch in enumerate(t):
                if not CJK_RE.match(ch):
                    continue                  # 标点/空格不进字流
                starts.append(int(w.start * 1000 + span * i / n))
                ends.append(int(w.start * 1000 + span * (i + 1) / n))
                chars.append(ch)
            s_txt += t
        if s_txt.strip():
            segs.append((int(seg.start * 1000), int(seg.end * 1000), s_txt.strip()))
    return starts, ends, "".join(chars), segs


def asr_seaco(wav16k: Path):
    """SeACo-Paraformer 兜底(对唱歌效果差, 实测几乎不出字)。段=按字间隙>500ms 切。"""
    from funasr import AutoModel
    model = AutoModel(model=SEACO, trust_remote_code=True, device="cuda",
                      dtype="float16", disable_update=True)
    res = model.generate(input=str(wav16k), batch_size_s=120, timestamp=True)[0]
    ts = res.get("timestamp") or []
    starts = [int(t[0]) for t in ts if isinstance(t, (list, tuple)) and len(t) >= 2]
    ends = [int(t[1]) for t in ts if isinstance(t, (list, tuple)) and len(t) >= 2]
    text = res.get("text") or ""
    segs = []
    if starts:
        cur_s, cur_e, cur_t = starts[0], ends[0], text[0]
        for k in range(1, len(starts)):
            if starts[k] - ends[k - 1] > 500:      # 间隙切段
                segs.append((cur_s, cur_e, cur_t))
                cur_s, cur_t = starts[k], ""
            cur_e = ends[k]
            if k < len(text):
                cur_t += text[k]
        segs.append((cur_s, cur_e, cur_t))
    return starts, ends, text, segs


def run_asr(wav16k: Path, backend: str = "whisper", whisper_model: str = "large-v3", lang: str = "zh"):
    if backend == "whisper":
        s, e, t, g = asr_whisper(wav16k, whisper_model, lang=lang)
        if len(s) < 10:
            print(f"  [asr] whisper 只出 {len(s)} 字, 换 SeACo 兜底…")
            try:
                s2, e2, t2, g2 = asr_seaco(wav16k)
                if len(s2) > len(s):
                    return s2, e2, t2, g2, "seaco"
            except Exception as ex:  # noqa: BLE001
                print(f"  [asr] SeACo 也不行: {type(ex).__name__}")
        return s, e, t, g, "whisper"
    s, e, t, g = asr_seaco(wav16k)
    return s, e, t, g, "seaco"


# ───────────────────────── 对齐 ─────────────────────────

def align(lines: list[dict], total_quota: int, char_starts: list[int],
          char_ends: list[int], duration_ms: int) -> list[dict]:
    """[旧] 字数配额 → ASR 字流时间区间(单调)。
    缺陷实测: whisper 识别疏密不均+空洞时, 字数索引比例映射会整段漂移。仅作兜底保留。"""
    n = len(char_starts)
    out = []
    prev_end = 0
    cum = 0
    for l in lines:
        a = cum / total_quota
        cum += l["quota"]
        b = cum / total_quota
        if n >= 2:
            ia = min(n - 1, int(round(a * n)))
            ib = max(ia + 1, min(n, int(round(b * n))))
            start, end = char_starts[ia], char_ends[ib - 1]
        else:
            start, end = int(a * duration_ms), int(b * duration_ms)
        start = max(start, prev_end)
        end = max(end, start + 400)
        end = min(end, duration_ms)
        out.append({"text": l["text"], "start": start, "end": end,
                    "quota": l["quota"]})
        prev_end = end
    return out


def align_anchor(lines: list[dict], segs: list[tuple[int, int, str]],
                 duration_ms: int) -> tuple[list[dict], list[int]]:
    """[新·锚点对齐] 歌词行 ↔ whisper 段做模糊匹配:
    匹配上的行用真实段时间钉死; 没匹配上的行在相邻锚点之间按字数配额插值。
    返回 (schedule, anchors: 每个锚点的行号列表, 供报告)。"""
    n_lines = len(lines)
    times: list[tuple[int, int] | None] = [None] * n_lines
    anchor_lines: list[int] = []

    j = 0                                    # 段游标(单调推进)
    for i, l in enumerate(lines):
        best = (0.0, None)
        for j2 in range(j, min(j + 4, len(segs))):        # 允许跳过最多 3 个段
            for w in (1, 2, 3):                           # 行可对应 1-3 个连续段
                if j2 + w > len(segs):
                    break
                t0, t1 = segs[j2][0], segs[j2 + w - 1][1]
                txt = "".join(s[2] for s in segs[j2:j2 + w])
                # 时长护栏: 段窗远超该行的合理唱时长 → 多半是识别空洞, 不锚
                w_quota = sum(x["quota"] for x in lines[i:i + w])
                if (t1 - t0) > max(10000, w_quota * 2200):
                    continue
                s = _ratio(_norm(l["text"]), _norm(txt))
                if s > best[0]:
                    best = (s, (j2, w))
        if best[0] >= 0.34 and best[1]:
            j2, w = best[1]
            times[i] = (segs[j2][0], segs[j2 + w - 1][1])
            anchor_lines.append(i)
            j = j2 + w
        # 没匹配上: 不推进游标, 让后面的行有机会匹配后面的段

    if len(anchor_lines) < 2:                # 锚点太少 → 旧比例兜底
        return [], anchor_lines

    # 相邻锚点之间: 按配额比例插值
    for a, b in zip(anchor_lines, anchor_lines[1:]):
        gap_lines = list(range(a + 1, b))
        if not gap_lines:
            continue
        a_end, b_start = times[a][1], times[b][0]
        if b_start <= a_end:
            b_start = a_end + 1
        gq = sum(lines[k]["quota"] for k in gap_lines) or 1
        cum = 0
        for k in gap_lines:
            s0 = a_end + (b_start - a_end) * cum / gq
            cum += lines[k]["quota"]
            e0 = a_end + (b_start - a_end) * cum / gq
            times[k] = (int(s0), max(int(s0) + 400, int(e0)))

    # 首锚点之前: 按配额倒推, 但外推区间封顶(锚点前 85%)——防止识别空洞时
    # 前段歌词被一路摊到 0.0s(前奏还没唱词就上屏)
    first = anchor_lines[0]
    if first > 0:
        fq = sum(lines[k]["quota"] for k in range(first))
        a_start = times[first][0]
        f_start = max(0, a_start - min(int(fq * 1800), int(a_start * 0.85)))
        cum = 0
        for k in range(first):
            s0 = f_start + (a_start - f_start) * cum / fq
            cum += lines[k]["quota"]
            e0 = f_start + (a_start - f_start) * cum / fq
            times[k] = (int(s0), max(int(s0) + 400, int(e0)))
    # 尾锚点之后: 按配额铺到歌尾前留 2s
    last = anchor_lines[-1]
    tail_end = max(times[last][1], min(duration_ms - 2000, duration_ms))
    if last < n_lines - 1:
        tq = sum(lines[k]["quota"] for k in range(last + 1, n_lines))
        cum = 0
        for k in range(last + 1, n_lines):
            s0 = times[last][1] + (tail_end - times[last][1]) * cum / tq
            cum += lines[k]["quota"]
            e0 = times[last][1] + (tail_end - times[last][1]) * cum / tq
            times[k] = (int(s0), max(int(s0) + 400, int(e0)))

    # 单调化 + 收尾
    out = []
    prev_end = 0
    for k, l in enumerate(lines):
        st, en = times[k] if times[k] else (prev_end, prev_end + 800)
        st = max(st, prev_end)
        en = max(en, st + 400)
        en = min(en, duration_ms)
        out.append({"text": l["text"], "start": int(st), "end": int(en),
                    "quota": l["quota"], "anchor": k in anchor_lines})
        prev_end = en
    return out, anchor_lines


# ───────────────────────── 主流程 ─────────────────────────

def align_song(song: Path, lyrics_dir: Path, force: bool = False,
               backend: str = "whisper", whisper_model: str = "large-v3",
               retry_frac: float = 0.35, lang: str = "zh") -> dict | None:
    stem = song.stem
    cache = CACHE_DIR / f"{stem}.lrc.json"
    if cache.exists() and not force:
        print(f"  [=] 已有缓存 {cache.name} (—force 重打)")
        return json.loads(cache.read_text(encoding="utf-8"))
    md = find_lyric_md(song, lyrics_dir)
    if not md:
        print(f"  [X] {stem}: 没配到歌词 md, 跳过")
        return None
    lyric = parse_lyric_md(md)
    print(f"  [md] {md.name} → 段落顺序 {'→'.join(lyric['order'])}, "
          f"{len(lyric['lines'])} 行, 配额 {lyric['total_quota']} 字")

    dur_ms = audio_duration_ms(song)
    print(f"  [asr] ffmpeg 转 16k wav…")
    n_lines = len(lyric["lines"])
    wav_path = _to_wav16k(song)
    try:
        t0 = time.time()
        starts, ends, asr_text, segs, used = run_asr(wav_path, backend, whisper_model, lang=lang)
        print(f"  [asr] {used} 识别 {len(starts)} 字 / {len(segs)} 段, "
              f"耗时 {time.time()-t0:.1f}s (歌 {dur_ms/1000:.0f}s)")
        sched, anchor_lines = align_anchor(lyric["lines"], segs, dur_ms)
        # 弱歌自救: 重伴奏骗过 VAD(演唱被当静音吞掉) → 关 VAD 重识别, 谁锚点多用谁
        if backend == "whisper" and len(anchor_lines) < max(4, int(n_lines * retry_frac)):
            print(f"  [!] 锚点仅 {len(anchor_lines)}/{n_lines} → 关 VAD 重识别…")
            try:
                s2, e2, t2, g2 = asr_whisper(wav_path, whisper_model, use_vad=False, lang=lang)
                sched2, anchors2 = align_anchor(lyric["lines"], g2, dur_ms)
                print(f"  [asr] VAD off: {len(s2)} 字 / {len(g2)} 段, 锚点 {len(anchors2)}/{n_lines}")
                if len(anchors2) > len(anchor_lines) or (
                        sched2 and len(anchors2) == len(anchor_lines) and len(s2) > len(starts)):
                    starts, ends, asr_text, segs = s2, e2, t2, g2
                    sched, anchor_lines = sched2, anchors2
                    used += "+novad"
            except Exception as ex:  # noqa: BLE001
                print(f"  [!] VAD off 重识别失败: {type(ex).__name__}: {ex}")
    finally:
        try:
            wav_path.unlink()
        except Exception:
            pass

    method = f"{used}-anchor"
    if not sched:
        print(f"  [!] 锚点不足({len(anchor_lines)} 个), 退化为比例打轴")
        sched = align(lyric["lines"], lyric["total_quota"], starts, ends, dur_ms)
        method = f"{used}-proportional"
    else:
        print(f"  [锚点] {len(anchor_lines)}/{len(sched)} 行钉在真实段时间上: {anchor_lines}")
    sched_json = {"song": song.name, "lyrics_md": md.name, "duration_ms": dur_ms,
                  "asr_chars": len(starts), "quota_total": lyric["total_quota"],
                  "method": method, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "lines": sched}
    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps(sched_json, ensure_ascii=False, indent=1), encoding="utf-8")

    # 报告: 首行起点(≈前奏长度) + 大于 8s 的空档(间奏)
    print(f"  [out] {cache.name}: {len(sched)} 行; 首行 @{sched[0]['start']/1000:.1f}s")
    for i in range(1, len(sched)):
        gap = sched[i]["start"] - sched[i - 1]["end"]
        if gap > 8000:
            print(f"      间奏 {gap/1000:.1f}s: '{sched[i-1]['text'][-12:]}' → '{sched[i]['text'][:12]}'")
    return sched_json


def main():
    global CACHE_DIR
    ap = argparse.ArgumentParser(description="歌词打轴(whisper 字级时间戳)")
    ap.add_argument("song", nargs="?", help="歌曲文件路径")
    ap.add_argument("--all", action="store_true", help="批量: 处理 config 里所有 song_dirs")
    ap.add_argument("--force", action="store_true", help="已有缓存也重打")
    ap.add_argument("--backend", default="whisper", choices=["whisper", "seaco"])
    ap.add_argument("--whisper-model", default="large-v3")
    ap.add_argument("--lang", default="zh", help="ASR 语种: zh/ja/ko/en...; 空串=自动检测")
    ap.add_argument("--retry-frac", type=float, default=0.35,
                    help="锚点行数/总行数 低于该比例时关 VAD 重识别(1.01=无条件重试)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    tts = cfg.get("tts") or {}
    _ly = tts.get("lyrics_dir") or []
    lyrics_dirs = [Path(p) for p in ([_ly] if isinstance(_ly, str) else _ly) if str(p).strip()]
    CACHE_DIR = ROOT / (tts.get("lyrics_cache_dir") or "lyrics-cache")
    songs: list[Path] = []
    if args.all:
        for d in (tts.get("song_dirs") or []):
            songs.extend(p for p in Path(d).glob("*")
                         if p.is_file() and p.suffix.lower() in
                         {".flac", ".mp3", ".wav", ".m4a", ".ogg", ".opus"} and not p.name.startswith("_"))
    elif args.song:
        songs.append(Path(args.song))
    else:
        ap.error("给歌曲路径或 --all")
    lyrics_dirs = [p for p in lyrics_dirs if p.is_dir()]
    if not lyrics_dirs:
        raise SystemExit("歌词目录一个都不存在（检查 config.tts.lyrics_dir）")
    print(f"歌词目录 {len(lyrics_dirs)} 个: " + " | ".join(str(p) for p in lyrics_dirs))

    print(f"待打轴 {len(songs)} 首 → 缓存目录 {CACHE_DIR}")
    ok = fail = 0
    for s in sorted(songs):
        print(f"▶ {s.name}")
        try:
            r = align_song(s, lyrics_dirs, force=args.force,
                           backend=args.backend, whisper_model=args.whisper_model,
                           retry_frac=args.retry_frac, lang=args.lang)
            ok += 1 if r else 0
            fail += 0 if r else 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  [X] 失败: {type(e).__name__}: {str(e)[:200]}")
    print(f"\n完成: {ok} 成功 / {fail} 失败/跳过")


if __name__ == "__main__":
    main()

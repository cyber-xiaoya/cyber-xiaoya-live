#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「钢琴翻唱专辑」的歌词合集拆成单曲 .md，供 lyrics_align.py 打轴。

合集结构（`_歌词/_全曲目新词合集.md`）：
    ### V003 独一无二、金达莱花、火花

    键盘声敲响　荧光照脸庞　那年我们攥着滚烫倔强…
    手指落在方向键　敲出个夏天…

音频文件名是「编号 + 首句歌词」（如 `001 键盘声敲响.flac`），所以用**首句相似度**配对，
比编号可靠（合集里的 V 编号是视频序号，与音频编号不一致）。

用法：
    python tools/split_lyrics_album.py            # 只打印配对结果，不写文件
    python tools/split_lyrics_album.py --write    # 写 <歌名>.md 到 _歌词 目录
"""
from __future__ import annotations

import difflib
import pathlib
import re
import sys

ALBUM = pathlib.Path(r"C:\Users\<你>\Music\钢琴翻唱专辑_待命名")
SONG_DIR = ALBUM / "人工挑选"
LYRIC_DIR = ALBUM / "_歌词"
COMPENDIUM = LYRIC_DIR / "_全曲目新词合集.md"


def norm(s: str) -> str:
    return re.sub(r"[\s　,，。.、!！?？:：;；\"'“”‘’()（）\-—_·]+", "", s or "")


def parse_sections(text: str) -> list[dict]:
    """按 `### V0xx ...` 切段, 收集其下的歌词行。"""
    out: list[dict] = []
    cur = None
    for ln in text.splitlines():
        m = re.match(r"^###\s+(.+)$", ln.strip())
        if m:
            if cur and cur["lines"]:
                out.append(cur)
            cur = {"title": m.group(1).strip(), "lines": []}
            continue
        if cur is None:
            continue
        t = ln.strip()
        if not t:
            continue
        cur["lines"].append(t)
    if cur and cur["lines"]:
        out.append(cur)
    return out


def main() -> int:
    write = "--write" in sys.argv
    if not COMPENDIUM.is_file():
        print("找不到合集:", COMPENDIUM)
        return 1
    sections = parse_sections(COMPENDIUM.read_text(encoding="utf-8"))
    print(f"合集切出 {len(sections)} 段")

    songs = sorted(p for p in SONG_DIR.glob("*.flac"))
    print(f"待配对歌曲 {len(songs)} 首\n")

    rows = []
    for sp in songs:
        stem = sp.stem
        title = re.sub(r"^\d+\s*", "", stem)          # 去掉 "001 "
        nt = norm(title)
        best = (0.0, None)
        for sec in sections:
            head = norm(re.sub(r"^V\d+\s*", "", sec["title"]))
            first = norm(sec["lines"][0]) if sec["lines"] else ""
            # 三个信号取最大: 首行开头包含标题 / 标题包含在首行 / 标题与段落标题相似
            s1 = 1.0 if first.startswith(nt) else 0.0
            s2 = 1.0 if nt and nt in first else 0.0
            s3 = difflib.SequenceMatcher(None, nt, head).ratio() if head else 0.0
            s4 = difflib.SequenceMatcher(None, nt, first[:len(nt) + 4]).ratio() if first else 0.0
            score = max(s1, s2, s3 * 0.9, s4)
            if score > best[0]:
                best = (score, sec)
        rows.append((sp, title, best[0], best[1]))
        mark = "✅" if best[0] >= 0.8 else ("⚠" if best[0] >= 0.55 else "❌")
        print(f"  {mark} {stem}  →  score={best[0]:.2f}  {best[1]['title'] if best[1] else '-'}"
              f"  ({len(best[1]['lines']) if best[1] else 0} 行)")

    ok = [r for r in rows if r[2] >= 0.8]
    weak = [r for r in rows if 0.55 <= r[2] < 0.8]
    print(f"\n匹配: 高置信 {len(ok)} / 存疑 {len(weak)} / 未匹配 {len(rows) - len(ok) - len(weak)}")

    if weak:
        print("\n存疑项（建议人工核对）:")
        for sp, title, score, sec in weak:
            print(f"  {sp.name}  ({score:.2f}) → {sec['title']}")

    if not write:
        print("\n(预览模式，未写文件；加 --write 才写入)")
        return 0

    LYRIC_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for sp, title, score, sec in rows:
        if sec is None or score < 0.55:
            continue
        # 合集是"散文式"段落 → 按 　(全角空格) 拆成短句, 再转成打轴器要求的表格格式
        rows_txt: list[str] = []
        for ln in sec["lines"]:
            for seg in re.split(r"[\u3000]+", ln):
                seg = seg.strip()
                if seg:
                    rows_txt.append(seg)
        if not rows_txt:
            continue
        body = ["## A1 [Verse]", "", "| 字数 | 原词 | 新词 |", "|---|---|---|"]
        for seg in rows_txt:
            body.append(f"| {len(seg)} | - | **{seg}** |")
        body += ["", "**成曲顺序**：`A1`", ""]
        body = ("---\ntitle: " + sp.stem + "\nsource: _全曲目新词合集.md\n---\n\n" + "\n".join(body))
        (LYRIC_DIR / (sp.stem + ".md")).write_text(body, encoding="utf-8")
        written += 1
    print(f"\n已写 {written} 个单曲词文件(表格格式) 到 {LYRIC_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

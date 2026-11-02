"""人设库：`personas/<id>.json` 一组文件 = 多套人设，设置里切换。

为什么单独存文件（2026-10-29 用户要求）：
  · 一个 config.json 里塞不下多套人设；换人设不该覆盖旧的那套
  · json 文件能进 git、能备份、能直接手改，也能并行建设好几个角色

生效优先级（见 `llm.py::system_prompt`）：
  1. `config.json → llm.persona` 指向的人设文件（存在就用它）
  2. 否则退回 `config.json → llm.system_prompt`（老行为，兼容）

文件结构：
  { "id", "name", "tagline", "voice", "avatar_note", "system_prompt", "updated" }
"""
from __future__ import annotations

import json
import pathlib
import re
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "personas"

_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _path(pid: str) -> pathlib.Path:
    return DIR / f"{pid}.json"


def ensure_dir() -> pathlib.Path:
    DIR.mkdir(parents=True, exist_ok=True)
    return DIR


def load_all() -> list[dict]:
    """读取全部人设（按文件名排序）。坏文件跳过，不炸。"""
    out: list[dict] = []
    if not DIR.is_dir():
        return out
    for p in sorted(DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        d.setdefault("id", p.stem)
        d.setdefault("name", d["id"])
        if not str(d.get("system_prompt") or "").strip():
            continue
        d["file"] = p.name
        out.append(d)
    return out


def get(pid: str) -> dict | None:
    if not pid or not _ID_RE.match(str(pid)):
        return None
    p = _path(str(pid))
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    d.setdefault("id", p.stem)
    d.setdefault("name", d["id"])
    return d


def save(pid: str, data: dict) -> dict:
    """新建/覆盖一套人设。id 只允许小写字母数字下划线横线。"""
    pid = str(pid or "").strip().lower()
    if not _ID_RE.match(pid):
        raise ValueError("人设 id 只能用 a-z 0-9 _ - （1~32 位）")
    prompt = str(data.get("system_prompt") or "").strip()
    if not prompt:
        raise ValueError("system_prompt 不能为空")
    ensure_dir()
    d = {
        "id": pid,
        "name": str(data.get("name") or pid).strip()[:40],
        "tagline": str(data.get("tagline") or "").strip()[:120],
        "voice": str(data.get("voice") or "").strip()[:60],
        "avatar_note": str(data.get("avatar_note") or "").strip()[:200],
        "system_prompt": prompt,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _path(pid).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def delete(pid: str) -> bool:
    p = _path(str(pid))
    if p.is_file():
        p.unlink()
        return True
    return False


def _llm_section(conf: dict | None) -> dict:
    """兼容两种入参: 完整的 config(有 llm 段) 或直接就是 llm 段。

    ⚠ 2026-10-29 踩坑: `LLMClient.conf()` 返回的是 **llm 段本身**，
    早期版本这里只认 `conf["llm"]["persona"]` → 永远取不到 → 人设形同虚设，
    她一直用 config.llm.system_prompt(小雅)。
    """
    if isinstance(conf, dict):
        inner = conf.get("llm")
        if isinstance(inner, dict):
            return inner
        return conf
    return {}


def active_id(conf: dict) -> str:
    return str(_llm_section(conf).get("persona") or "").strip()


def active_prompt(conf: dict) -> str | None:
    """当前人设的 system_prompt；没配人设或文件不存在 → None（调用方退回旧字段）。"""
    pid = active_id(conf)
    if not pid:
        return None
    d = get(pid)
    if not d:
        return None
    p = str(d.get("system_prompt") or "").strip()
    return p or None


def brief(d: dict) -> dict:
    """给接口/页面用的精简信息（不含整段 prompt）。"""
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "tagline": d.get("tagline") or "",
        "voice": d.get("voice") or "",
        "avatar_note": d.get("avatar_note") or "",
        "chars": len(str(d.get("system_prompt") or "")),
        "updated": d.get("updated") or "",
    }

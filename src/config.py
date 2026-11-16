"""config.json 读写 —— 明文配置(DEV 版不加密), 带原子写与深合并 patch。

约定:
- load() 每次都从磁盘读? 不 —— 进程内驻留一份, 写操作经 save_conf() 同步落盘,
  外部手改文件时可用 reload() 强制重读(桥也提供 /api/settings/reload)。
- patch() 做深合并: {"rtc": {"mode": "customer"}} 只改 rtc.mode, 其他键不动。
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 可用环境变量 VIDU_CONF 指向别的配置文件 —— 用于"不动线上桥、另起一个实例自检"
# (例如临时把 bridge.port 改成 8796 验证启动链路, 见 src/selfcheck_boot.py)。
CONF_PATH = pathlib.Path(os.environ.get("VIDU_CONF") or (ROOT / "config.json"))

_lock = threading.Lock()
_conf: dict | None = None


def _is_placeholder(v: str) -> bool:
    """示例配置里的占位符（如 "<在这里填你的数字人形象ID>"）不当作真实值迁移。"""
    v = (v or "").strip()
    return (not v) or v.startswith("<") or v.startswith("在这里")


def _migrate(conf: dict) -> dict:
    """兼容老配置：2026-11 之前的 config.example.json 把形象/动作放在 vidu 下。

    引擎只读顶层 avatar / action，老用户照着旧示例填的 vidu.avatar_id 会一直报
    "未配置形象"。这里在内存里把老键上提一次（不回写磁盘，避免动用户文件）；
    用户之后任何一次保存设置都会自然落成新结构。
    """
    vidu = conf.get("vidu")
    if not isinstance(vidu, dict):
        return conf
    avatar = conf.get("avatar")
    if not isinstance(avatar, dict):
        avatar = conf.setdefault("avatar", {})
    if not (str(avatar.get("avatar_id") or "").strip()
            or str(avatar.get("image_uri") or "").strip()):
        old = str(vidu.get("avatar_id") or "").strip()
        if not _is_placeholder(old):
            avatar["avatar_id"] = old
            print("[config] 已兼容老配置: vidu.avatar_id → avatar.avatar_id", file=sys.stderr)
    if conf.get("action") is None:
        old_act = vidu.get("action")
        if isinstance(old_act, dict):
            lib = str(old_act.get("library_id") or "").strip()
            if not _is_placeholder(lib) and lib != "0":
                conf["action"] = {"enabled": True, "library_id": lib}
                print(f"[config] 已兼容老配置: vidu.action → action "
                      f"(library_id={lib})", file=sys.stderr)
    return conf


def load() -> dict:
    global _conf
    with _lock:
        if _conf is None:
            _conf = _migrate(json.loads(CONF_PATH.read_text(encoding="utf-8")))
        return _conf


def reload() -> dict:
    global _conf
    with _lock:
        _conf = _migrate(json.loads(CONF_PATH.read_text(encoding="utf-8")))
        return _conf


def save_conf(conf: dict) -> None:
    """原子写 + 上一版留 .bak。"""
    global _conf
    with _lock:
        bak = CONF_PATH.with_suffix(".json.bak")
        try:
            if CONF_PATH.exists():
                bak.write_text(CONF_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        tmp = CONF_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONF_PATH)
        _conf = conf


def deep_merge(base: dict, updates: dict) -> dict:
    """把 updates 深合并进 base 的副本并返回新 dict(dict/list 覆盖语义: dict 递归, 其余替换)。"""
    out = copy.deepcopy(base)
    for k, v in (updates or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def patch(updates: dict) -> dict:
    """深合并更新并落盘, 返回合并后的完整配置。"""
    merged = deep_merge(load(), updates or {})
    save_conf(merged)
    return merged


def public() -> dict:
    """脱敏视图(给前端): api_key / app_key / secret_key 只回是否已填 + 掩码。"""
    conf = copy.deepcopy(load())

    def mask(v: str) -> str:
        if not v:
            return ""
        return (v[:6] + "…" + v[-4:]) if len(v) > 12 else "…"

    vidu = conf.get("vidu") or {}
    if vidu.get("api_key"):
        vidu["api_key_masked"] = mask(vidu["api_key"])
        vidu["api_key_set"] = True
        del vidu["api_key"]
    else:
        vidu["api_key_set"] = False
    if vidu.get("api_keys") is not None:          # 备用 key 列表绝不回传明文
        vidu["api_keys_count"] = len(vidu["api_keys"])
        del vidu["api_keys"]
    artc = ((conf.get("rtc") or {}).get("artc") or {})
    if artc.get("app_key"):
        artc["app_key_masked"] = mask(artc["app_key"])
        artc["app_key_set"] = True
        del artc["app_key"]
    else:
        artc["app_key_set"] = False
    trtc = ((conf.get("rtc") or {}).get("trtc") or {})
    if trtc.get("secret_key"):
        trtc["secret_key_masked"] = mask(trtc["secret_key"])
        trtc["secret_key_set"] = True
        del trtc["secret_key"]
    else:
        trtc["secret_key_set"] = False
    return conf

"""LLM 客户端(OpenAI 兼容 /chat/completions, 流式) + 增量分句器。

默认指向 DeepSeek 官方(api.deepseek.com + deepseek-chat); base_url / model / key_file
全部在 config.json 里, 以后换 API 只改配置不改代码。key 解析顺序:
  1. llm.key_file 指向的文本文件(如 E:\\deepseek-works\\vidu-dh\\deepseek-key.txt)
  2. 环境变量 DEEPSEEK_API_KEY
流式输出边到边分句: 每凑齐一句就交出去合成, 首字延迟 ≈ LLM 首句时间 + 一次 TTS。
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import pathlib
import re

import httpx

try:
    import certifi
    _VERIFY = certifi.where()
except Exception:
    _VERIFY = True

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_MD_NOISE_RE = re.compile(r"[*_#`>\[\]~|]")

# 强停顿(必须分句)与弱停顿(句子够长时才分)
STRONG_ENDS = "。！？!?；;"   # 2026-10-29: 不再把换行当句末(模型排版换行会切在句子中间)
SOFT_ENDS = "，,、"


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def clean_for_speech(text: str) -> str:
    """喂给 TTS 前清掉 markdown 噪音与多余空白。"""
    t = _MD_NOISE_RE.sub("", text)
    # 2026-10-29: 换行/制表符一律当空格, 否则模型排版用的换行会变成"句末"把话切两半
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"[ ]{2,}", " ", t)
    return t.strip()


class SentenceStream:
    """增量分句: feed(增量文本) → 本轮凑齐的完整句子列表; flush() 收尾。

    规则(2026-10-13 收紧, 用户拍板: 按标点切、≤30字、每轮合成时间最短):
    强停顿立即分句; 弱停顿只在已积累 ≥ soft_min(10) 字时分(太短的逗号切了
    韵律会碎); 缓冲超过 hard_max(30) 字强制放出(无标点长串兜底)。
    """

    def __init__(self, soft_min: int = 10, hard_max: int = 45):   # 2026-10-29: 30→45, 给标点留出回退区间
        self.buf = ""
        self.soft_min = soft_min
        self.hard_max = hard_max

    def feed(self, delta: str) -> list[str]:
        self.buf += delta or ""
        out: list[str] = []
        while True:
            cut = -1
            soft = False
            for i, ch in enumerate(self.buf):
                if ch in STRONG_ENDS:
                    cut, soft = i, False
                    break
                if ch in SOFT_ENDS and i + 1 >= self.soft_min:
                    cut, soft = i, True
                    break
            if cut < 0:
                if len(self.buf) > self.hard_max:   # 兜底: 强制放行
                    # 2026-10-29: 别硬按第 N 个字切(会在句中劈开) → 回退到最近的标点/空格
                    n = self.hard_max
                    cut_at = max(self.buf.rfind(c, 0, n) for c in SOFT_ENDS)
                    sp = self.buf.rfind(" ", 0, n)
                    if cut_at < 6:
                        cut_at = sp if sp >= 6 else n - 1
                    out.append(self.buf[:cut_at + 1].strip())
                    self.buf = self.buf[cut_at + 1:]
                    continue
                break
            piece = self.buf[:cut + 1].strip()
            self.buf = self.buf[cut + 1:]
            if piece:
                out.append(piece)
            if soft:
                continue
        return out

    def flush(self) -> str:
        tail = self.buf.strip()
        self.buf = ""
        return tail


def merge_for_tts(pieces: list[str], min_chars: int = 30, first_min: int = 14) -> list[str]:
    """把分句结果**合并成更长的合成单元**(2026-10-17 用户方案B)。

    背景: 每段都要跑一次完整 GPU 合成(实测 1.9~3.5 秒), 而 10 来字的短句只出 ~2 秒音频,
    串行合成时队列补不上 → 每个短分句之间空一小拍, 听感就是"一卡一卡"。
    合并成更大的块: GPU 调用少 2~3 倍、每块音频更长, 句间空档基本消失。

    两级阈值(兼顾开口速度):
      · **第一块**攒到 `first_min`(默认 14 字)就出 —— 别为了攒长度拖慢开口;
      · 之后每块攒到 `min_chars`(默认 30 字)再出。
    """
    out: list[str] = []
    buf = ""
    for s in (pieces or []):
        s = s or ""
        if not s.strip():
            continue
        buf += s
        target = first_min if not out else min_chars
        if len(buf) >= max(6, target):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out or [""][:0]


def split_for_tts(text: str, soft_min: int = 10, hard_max: int = 30) -> list[str]:
    """批式分段: 整段文本一次性切成 TTS 合成单元(与流式 SentenceStream 同规则)。
    用于 弹幕复读/直接开口 这类"整条到手"的路径 —— 原来整条一坨喂 synth,
    长文本首包慢且断句失控(2026-10-13 用户反馈)。"""
    t = clean_for_speech(text or "")
    if not t:
        return []
    ss = SentenceStream(soft_min, hard_max)
    out = [s for s in ss.feed(t) if s.strip()]
    tail = ss.flush()
    if tail.strip():
        out.append(tail)
    return out or [t]


class LLMClient:
    def __init__(self, conf_get):
        self._conf_get = conf_get          # () -> llm 配置段(热改配置立即生效)
        # 2026-10-30: 原来是 maxlen=32(16 轮), history_turns 调到 15 后几乎顶死 —— 留一倍余量
        self.history: collections.deque = collections.deque(maxlen=48)
        # 本场统计(2026-11-01 用户要求"显示本场消耗多少 token")
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        self.turn_count = 0          # 记忆用掉多少轮(礼物时归零, 做"假倒计时")

    # ---- 配置 / key ----

    def conf(self) -> dict:
        return self._conf_get() or {}

    def api_key(self) -> str:
        c = self.conf()
        kf = (c.get("key_file") or "").strip()
        if kf:
            try:
                k = pathlib.Path(kf).read_text(encoding="utf-8").strip()
                if k:
                    return k
            except Exception:
                pass
        return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    def system_prompt(self) -> str:
        """人设优先级: config.llm.persona 指向的 personas/<id>.json > llm.system_prompt。
        (2026-10-29 人设库: 支持并存多套人设, 设置里切换)"""
        from . import personas as _per
        conf = self.conf()
        p = _per.active_prompt(conf)
        if p:
            return p
        return conf.get("system_prompt") or "你是一个口语化的中文数字人助手。"

    def _messages(self, user_text: str) -> list[dict]:
        msgs = [{"role": "system", "content": self.system_prompt()}]
        msgs.extend(self.history)
        msgs.append({"role": "user", "content": user_text})
        return msgs

    # ---- 流式调用 ----

    async def stream(self, user_text: str, record: bool = True):
        """yield 增量文本; 结束后自动把问答写进 history(record=False 则不写)。异常向上抛。

        record=False 用于**系统自己发起的调用**（空闲主动搭话等）—— 那种 prompt 不是观众
        说的话，写进 history 会污染上下文（2026-10-29 发现）。"""
        c = self.conf()
        key = self.api_key()
        if not key:
            raise RuntimeError(f"LLM key 未找到(key_file={c.get('key_file')!r} 且无 DEEPSEEK_API_KEY)")
        base = (c.get("base_url") or "https://api.deepseek.com").rstrip("/")
        body = {
            "model": c.get("model") or "deepseek-chat",
            "messages": self._messages(user_text),
            "stream": True,
            "temperature": float(c.get("temperature", 0.7)),
            "max_tokens": int(c.get("max_tokens", 300)),
            # 2026-11-01: 让流式响应带上 usage(否则拿不到 token 用量)
            "stream_options": {"include_usage": True},
        }
        # 思考模式开关(2026-10-10): 官方新模型(deepseek-flash)默认开思考且 effort=high,
        # 思维链会把小 max_tokens 吃光返回空 content → 弹幕短回复必须显式 disabled。
        _think = (c.get("thinking") or "").strip().lower()
        if _think in ("enabled", "disabled"):
            body["thinking"] = {"type": _think}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        url = base + "/chat/completions"
        full: list[str] = []
        timeout = httpx.Timeout(30.0, read=300.0)

        # ── 重试 + 换模型(2026-10-30) ────────────────────────────────
        # 线上真实故障: DeepSeek 间歇性 503 "Service is too busy" / 读超时,
        # 原来一次失败就整条回复丢掉 → 观众看到的是"她卡住不说话了"。
        # 现在: 同一模型重试 2 次 → 再按 llm.fallback_models 换模型 → 都失败才抛。
        _primary = body.get("model") or "deepseek-chat"
        _fb = [m for m in ((c.get("fallback_models") or ["deepseek-v4-pro"])) if m and m != _primary]
        _models = [_primary] + _fb
        _retry_codes = {429, 500, 502, 503, 504}
        _last_err: Exception | None = None

        async def _run_once(model: str):
            body["model"] = model
            async with client.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode("utf-8", "ignore")
                    e = RuntimeError(f"LLM HTTP {r.status_code}: {text[:300]}")
                    e.status = r.status_code          # type: ignore[attr-defined]
                    raise e
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    _u = obj.get("usage")
                    if isinstance(_u, dict):
                        self.usage["prompt"] += int(_u.get("prompt_tokens") or 0)
                        self.usage["completion"] += int(_u.get("completion_tokens") or 0)
                        self.usage["total"] += int(_u.get("total_tokens") or 0)
                        self.usage["calls"] += 1
                    delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
                    piece = delta.get("content") or ""
                    if piece:
                        yield piece

        async with httpx.AsyncClient(verify=_VERIFY, timeout=timeout) as client:
            _done = False
            for _mi, _model in enumerate(_models):
                for _attempt in range(2):
                    _got = False
                    try:
                        async for _piece in _run_once(_model):
                            _got = True
                            full.append(_piece)
                            yield _piece
                        _done = True
                        break
                    except Exception as _e:            # noqa: BLE001
                        _code = getattr(_e, "status", None)
                        _retryable = (_code in _retry_codes) or isinstance(
                            _e, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError))
                        if _got or not _retryable:
                            raise                     # 已经吐过字 / 非可重试错误 → 不重试
                        _last_err = _e
                        print(f"[llm] ⚠ {_model} 第{_attempt+1}次失败({type(_e).__name__}"
                              f"{' ' + str(_code) if _code else ''}) → "
                              f"{'重试' if _attempt == 0 else '换下一个模型'}", flush=True)
                        await asyncio.sleep(0.45 if _attempt == 0 else 0.9)
                if _done:
                    break
            if not _done and _last_err is not None:
                raise _last_err
        reply = strip_think("".join(full))
        if reply:
            if record:
                self.turn_count += 1
                self.history.append({"role": "user", "content": user_text})
                self.history.append({"role": "assistant", "content": reply})

    def trim_history(self):
        """按 history_turns 截断(2 条/回合)。"""
        turns = int(self.conf().get("history_turns", 15))
        while len(self.history) > turns * 2:
            self.history.popleft()

    def reset_stats(self) -> None:
        """本场开始: token 用量与记忆轮数归零。"""
        self.usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        self.turn_count = 0

    def reset_turns(self) -> None:
        """有人送礼 → 记忆"假倒计时"回到满格(不动真实 history)。"""
        self.turn_count = 0

    def reset_history(self):
        self.history.clear()

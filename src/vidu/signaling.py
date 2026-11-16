"""Vidu 组件版 WebSocket 信令客户端(/live/v1/external-lives/{live_id}/stream)。

协议要点(官方《详细参数》):
- 文本帧 = JSON WsSignal: {"type","live_id","conn_id","seq_id","payload"}; 二进制帧 = 裸 PCM
- 首帧必须是 conn_init(1); 成功回执前上行的音频帧 / audio_interrupted / prompt_operation 会被丢弃
- conn_init_ack(2).success=true 才算就绪; error_code=NOT_READY → 重发 conn_init, **不要重连 WS**
- 服务端约每 5s 发 ping, 约 15s 未回 pong 即断开(普通数据帧不续期) —— websockets 库自动回 pong
- 上行信令: 5=call_hangup 7=audio_interrupted 9=input_transcription 10=output_transcription
- 下行信令: 2=conn_init_ack 6=force_hangup(随后服务端关 WS)

鉴权: 服务端场景用 Authorization Header(本文件), 浏览器场景才用 client_secret query 参数。
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time

import websockets

try:
    import certifi
    _CA_FILE = certifi.where()
except Exception:
    _CA_FILE = None


def ssl_context() -> ssl.SSLContext:
    """与 httpx 同源(certifi)的信任库 —— 打包/精简环境下系统 CA 可能不存在。"""
    if _CA_FILE:
        try:
            return ssl.create_default_context(cafile=_CA_FILE)
        except Exception:
            pass
    return ssl.create_default_context()


TYPE_CONN_INIT, TYPE_CONN_INIT_ACK = 1, 2
TYPE_HANGUP, TYPE_FORCE_HANGUP = 5, 6
TYPE_INTERRUPT, TYPE_INPUT_TEXT, TYPE_OUTPUT_TEXT = 7, 9, 10
TYPE_PROMPT_OPERATION = 11        # 参考图切换/清除(R2V 参考生): 换装 / 拿起指定商品 / 换背景
TYPE_PROMPT_OPERATION_ACK = 12    # 参考图回执: {"prompt_operation_ack": {"success": true}}
TYPE_PROMPT_OPERATION_ERR = 14    # 参考图错误(成功才走 12)

# conn_init_ack.error_code 的处理分类
RETRYABLE_CODES = {"NOT_READY"}


def _ws_proxy():
    """wss 信令的代理策略(websockets.connect 的 proxy 参数)。

    默认 **强制直连**(None): Vidu 是国内服务, 且 macOS 上 websockets 会读到
    **系统级代理**(Clash 等, 127.0.0.1:7897 之类), 走 SOCKS 还要求装 python-socks ——
    用户实测报错 "connecting through a SOCKS proxy requires python-socks" 就是它。
    而且国内流量绕道海外节点又慢又容易触发风控, 直连才是对的。

    特殊网络环境(如海外用户)需要走代理时, 设环境变量 VIDU_WS_PROXY:
      VIDU_WS_PROXY=system   → 跟随系统/环境变量代理(需要 python-socks)
      VIDU_WS_PROXY=socks5://127.0.0.1:7897 → 显式指定代理地址
    """
    import os
    v = (os.environ.get("VIDU_WS_PROXY") or "").strip().lower()
    if v == "system":
        return True
    if v and v != "direct":
        return os.environ["VIDU_WS_PROXY"]
    return None


def envelope(t: int, live_id: str, payload: dict, conn_id: str, seq: int) -> dict:
    return {"type": t, "live_id": str(live_id), "conn_id": conn_id,
            "seq_id": seq, "payload": payload}


class ComponentSignaling:
    def __init__(self, host: str, live_id: str, ws_path: str,
                 conn_id: str | None = None, api_key: str | None = None):
        self.host = host
        self.live_id = str(live_id)
        self.ws_path = ws_path or "/live/v1/external-lives/{live_id}/stream"
        self.conn_id = conn_id or f"comp-{time.time_ns() % 10**9}"
        self.api_key = api_key
        self.ws = None
        self.seq = 0
        self.audio_bytes = 0          # 已上行 PCM 字节(诊断: 48B = 1ms 音频)
        self.last_send = 0.0
        self.last_recv = 0.0
        self.closing = False
        self.events: asyncio.Queue = asyncio.Queue()   # 下行事件(6/-1/未知类型)给引擎泵
        self.init_ok = False
        self._ack_evt: asyncio.Event = asyncio.Event()
        self._ack: dict = {}
        self._listener: asyncio.Task | None = None
        # 断线取证(2026-10-10 "唱歌时打字聊几句就断流"): 记录到底是谁关的、什么码。
        # 服务端因心跳超时/审核/额度关连接时 close_code/reason 与"对端关闭"能区分开。
        self.close_info: dict = {}

    # ---------- 建连 / 初始化 ----------

    async def connect(self) -> None:
        path = self.ws_path.format(live_id=self.live_id)
        url = f"wss://{self.host}{path}?conn_id={self.conn_id}"
        headers = {"Authorization": f"Token {self.api_key}"} if self.api_key else {}
        self.ws = await websockets.connect(url, additional_headers=headers, max_size=None,
                                           ssl=ssl_context(), ping_interval=10,
                                           ping_timeout=20, close_timeout=5,
                                           proxy=_ws_proxy())
        self.last_send = self.last_recv = time.time()
        self._listener = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """读下行。websockets 新版里对端关闭时 `async for` 是正常结束而非抛错,
        所以无论怎么退出都要给上层补一个 type=-1 事件, 否则引擎以为会话还活着。"""
        try:
            async for raw in self.ws:
                self.last_recv = time.time()
                if not isinstance(raw, str):
                    continue          # 服务端理论上不下发二进制; 收到也忽略
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                t = msg.get("type")
                p = msg.get("payload") or {}
                if t == TYPE_CONN_INIT_ACK:
                    ack = p.get("conn_init_ack") or {}
                    self._ack = {"success": bool(ack.get("success")),
                                 "error_code": ack.get("error_code") or "",
                                 "error_msg": ack.get("error_msg") or ""}
                    if self._ack["success"]:
                        self.init_ok = True
                    self._ack_evt.set()
                    continue
                await self.events.put(msg)
        except websockets.ConnectionClosed as e:
            self.close_info = {"who": "closed", "code": getattr(e, "code", None),
                               "reason": str(getattr(e, "reason", "") or "")[:120]}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.close_info = {"who": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
        if not self.closing:
            self.close_info.setdefault("who", "peer_eof")   # async-for 正常结束 = 对端关的
            await self.events.put({"type": -1, "payload": {"event": "ws_closed",
                                                           "close": dict(self.close_info)}})

    async def conn_init(self, retries: int = 15, interval: float = 2.0) -> bool:
        """发 conn_init 直到成功回执。NOT_READY 正常重试(不重连);
        LIVE_ENDED / LIVE_CONN_INIT_FAILED 抛 RuntimeError(需要重新创建会话)。"""
        last_code = ""
        for attempt in range(1, retries + 1):
            if self.closing or self.ws is None:
                return False
            self._ack_evt.clear()
            self._ack = {}
            await self._send(TYPE_CONN_INIT, {"conn_init": {"version": 1}})
            try:
                await asyncio.wait_for(self._ack_evt.wait(), timeout=interval)
            except asyncio.TimeoutError:
                last_code = "ack_timeout"
                continue
            ack = self._ack
            if ack.get("success"):
                return True
            last_code = ack.get("error_code") or "unknown"
            if last_code not in RETRYABLE_CODES:
                raise RuntimeError(
                    f"conn_init 失败: {last_code} {ack.get('error_msg') or ''}".strip())
        raise RuntimeError(f"conn_init 多次未就绪(最后一次: {last_code})")

    # ---------- 上行 ----------

    async def _send(self, t: int, payload: dict) -> None:
        if self.ws is None:
            raise RuntimeError("WS 未连接")
        self.seq += 1
        self.last_send = time.time()
        await self.ws.send(json.dumps(envelope(t, self.live_id, payload, self.conn_id, self.seq),
                                      ensure_ascii=False))

    async def send_input_text(self, content: str) -> None:
        await self._send(TYPE_INPUT_TEXT, {"text_msg": {
            "msg_id": f"in-{self.seq + 1}", "content": content,
            "timestamp": int(time.time() * 1000)}})

    async def send_output_text(self, content: str) -> None:
        await self._send(TYPE_OUTPUT_TEXT, {"text_msg": {
            "msg_id": f"out-{self.seq + 1}", "content": content,
            "timestamp": int(time.time() * 1000)}})

    async def interrupt(self) -> None:
        await self._send(TYPE_INTERRUPT, {})

    async def prompt_operation(self, images: list[dict] | None = None,
                               op_type: str = "switch") -> None:
        """参考图操作(type11 prompt_operation): switch=挂上参考图 / remove=清除。

        服务端行为(2026-10-17 实测): **成功不回 ack**(数秒内画面自己变), 失败才回 type=14;
        **不去重** → 别"没回执就重发"。images 每项: {image_uri, image_id, kind?, user_text?},
        单次 1~3 张; kind ∈ object(手持物) / garment(换装, 实测**不传更稳**) / background(换背景)。"""
        payload: dict = {"op_type": op_type or "switch"}
        if images:
            payload["images"] = images
        await self._send(TYPE_PROMPT_OPERATION, {"prompt_operation": payload})

    async def hangup(self, reason: str = "client_hangup") -> None:
        await self._send(TYPE_HANGUP, {"hangup": {"hangup_reason": reason}})

    async def send_audio(self, data: bytes) -> None:
        """裸 PCM 二进制帧(s16le / 24kHz / mono)。调用方负责按实时节奏喂。"""
        if self.ws is None:
            raise RuntimeError("WS 未连接")
        self.last_send = time.time()
        self.audio_bytes += len(data)
        await self.ws.send(data)

    # ---------- 关闭 ----------

    async def close(self) -> None:
        self.closing = True
        if self._listener:
            try:
                self._listener.cancel()
            except Exception:
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    def stats(self) -> dict:
        return {"conn_id": self.conn_id, "seq": self.seq,
                "audio_bytes": self.audio_bytes,
                "audio_seconds": round(self.audio_bytes / 48000.0, 2),
                "idle_send": round(time.time() - self.last_send, 1),
                "idle_recv": round(time.time() - self.last_recv, 1),
                "init_ok": self.init_ok}

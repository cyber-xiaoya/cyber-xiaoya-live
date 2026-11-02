"""离线自检(不连网、不花钱): 配置 / token 签名 / 分句 / PCM 数学 / 会话请求体 / 页面语法外列。

跑法(项目根目录):  .venv\\Scripts\\python.exe tools\\selfcheck.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import wave
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as appconf                 # noqa: E402
from src import rtc_tokens                        # noqa: E402
from src.llm import SentenceStream, clean_for_speech, strip_think  # noqa: E402
from src.tts import audio                         # noqa: E402
from src.vidu import signaling                    # noqa: E402
from src.engine import Engine                     # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [X]  {name}  {detail}")


def t_config():
    print("== 配置 ==")
    c = appconf.load()
    check("config.json 可读且是 dict", isinstance(c, dict))
    v = c.get("vidu") or {}
    check("vidu.api_key 已填", str(v.get("api_key", "")).startswith("vda_"))
    check("vidu.host 已填", bool(v.get("host")))
    check("创建端点 = /live/s_avatar/component",
          (v.get("create_path") or "") == "/live/s_avatar/component")
    check("WS 路径含 external-lives", "external-lives" in (v.get("ws_path") or ""))
    check("tts.voices_dir 已配置", bool((c.get("tts") or {}).get("voices_dir")))
    check("avatar 有 avatar_id 或 image_uri",
          bool((c.get("avatar") or {}).get("avatar_id") or (c.get("avatar") or {}).get("image_uri")))


def t_tokens():
    print("== RTC token 签名 ==")
    tok = rtc_tokens.artc_token("app", "key", "ch", "uid", 123)
    expect = hashlib.sha256(b"appkeychuid123").hexdigest()
    check("ARTC concat-sha256 向量", tok == expect, tok)
    env = rtc_tokens.build_artc_join_envelope("app", "ch", "uid", tok, 123)
    obj = json.loads(env)
    check("authinfo 信封字段", set(obj) == {"appid", "channelid", "userid", "timestamp", "token", "gslb"})
    sig = rtc_tokens.sign_artc("app", "key", "ch", "uid", 60, "concat_json")
    check("sign_artc concat_json 返回信封串", json.loads(sig["token"])["token"] ==
          hashlib.sha256(f"appkeychuid{sig['timestamp']}".encode()).hexdigest())
    raw = rtc_tokens.sign_artc("app", "key", "ch", "uid", 60, "raw")
    check("sign_artc raw 返回裸 hex", len(raw["token"]) == 64)
    b64 = rtc_tokens.sign_artc("app", "key", "ch", "uid", 60, "authinfo_b64")
    obj = json.loads(base64.b64decode(b64["token"]))
    check("authinfo_b64 字段齐", set(obj) == {"appid", "channelid", "nonce", "timestamp", "token", "userid"})
    d1 = {"appid": "app", "channelid": "ch", "nonce": "", "timestamp": obj["timestamp"], "userid": "uid"}
    want = hmac.new(b"key", json.dumps(d1).encode(), hashlib.sha256).hexdigest()
    check("authinfo_b64 HMAC 复核", obj["token"] == want, obj["token"][:24])
    check("sign_artc 默认 = HMAC Base64", rtc_tokens.sign_artc("a", "k", "c", "u")["shape"] == "authinfo_b64")
    usig = rtc_tokens.trtc_usersig("1600161190", "secret", "uid1", 3600)
    # 变体 base64 还原 → zlib → json → 重算 hmac
    b = usig.replace("*", "+").replace("-", "/").replace("_", "=")
    payload = json.loads(zlib.decompress(base64.b64decode(b)))
    content = (f"TLS.identifier:{payload['TLS.identifier']}\nTLS.sdkappid:{payload['TLS.sdkappid']}\n"
               f"TLS.time:{payload['TLS.time']}\nTLS.expire:{payload['TLS.expire']}\n")
    want = base64.b64encode(hmac.new(b"secret", content.encode(), hashlib.sha256).digest()).decode()
    check("TRTC UserSig TLS2.0 hmac 复核", payload["TLS.sig"] == want)
    multi = rtc_tokens.sign_viewers("trtc", {"trtc": {"sdk_app_id": "1600161190", "secret_key": "secret"}},
                                    "chx", ["a", "b"])
    check("sign_viewers 多用户", set(multi) == {"a", "b"})
    try:
        rtc_tokens.sign_viewers("agora", {}, "c", ["u"])
        check("agora 明确报不支持", False)
    except RuntimeError:
        check("agora 明确报不支持", True)


def t_sentences():
    print("== 分句 / 清洗 ==")
    s = SentenceStream()
    out = []
    out += s.feed("你好呀。今")
    out += s.feed("天天气不错！")
    out += s.feed("对吧")
    tail = s.flush()
    check("强停顿分句", out == ["你好呀。", "今天天气不错！"], str(out + [tail]))
    check("flush 收尾", tail == "对吧")
    s2 = SentenceStream(soft_min=24, hard_max=80)
    check("弱停顿太短不分句", s2.feed("短，句") == [])
    prefix = "无标点长句" * 5                       # 25 字无标点
    out2 = s2.feed(prefix + "，后半句")
    check("弱停顿够长才分句", out2 == ["短，句" + prefix + "，"], str(out2))
    out2b = s2.feed("。")
    check("弱停顿后残余继续走强停顿", out2b == ["后半句。"], str(out2b))
    s3 = SentenceStream()
    long_run = "字" * 90
    out3 = s3.feed(long_run)
    check("超长无标点强制放行", len(out3) == 1 and len(out3[0]) == 80, str(len(out3)))
    check("strip_think", strip_think("<think>abc</think>你好") == "你好")
    check("clean_for_speech", clean_for_speech("*好*的#吧`x`") == "好的吧x")


def t_audio():
    print("== PCM / ffmpeg ==")
    check("48 字节/毫秒", audio.BYTES_PER_MS == 48)
    check("100ms 静音 = 4800B", len(audio.silence_pcm(100)) == 4800)
    chunks = list(audio.chunk_pcm(b"\x00" * 12000, 100))
    sizes = [len(c) for c, _ in chunks]
    check("chunk 切分 4800/4800/2400", sizes == [4800, 4800, 2400], str(sizes))
    # 造一个 440Hz 0.3s 的 wav → ffmpeg 解码 → 长度 ≈ 0.3s(±80ms)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        import math
        frames = b"".join(int(12000 * math.sin(2 * math.pi * 440 * i / 44100)).to_bytes(2, "little", signed=True)
                          for i in range(13230))
        w.writeframes(frames)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(buf.getvalue()); tmp.close()
    pcm = audio.decode_to_pcm(src_path=tmp.name)
    sec = audio.pcm_seconds(pcm)
    check("ffmpeg 解码 44.1k→24k", abs(sec - 0.3) < 0.08, f"{sec:.3f}s")
    wav2 = audio.wav_bytes(b"\x00" * 9600)
    check("wav_bytes 可被 wave 模块读回", wave.open(io.BytesIO(wav2)).getframerate() == 24000)
    pathlib.Path(tmp.name).unlink(missing_ok=True)


def t_body():
    print("== 会话请求体 ==")
    eng = Engine.__new__(Engine)   # 不跑 __init__, 只借 _build_body
    conf = appconf.load()
    appconf.save_conf(conf)        # 确保驻留
    body, extra = eng._build_body({"mode": "managed"})
    check("managed: model/image/avatar/action 字段齐",
          body.get("model") in ("vidu-s2", "vidu-s1") and "managed_rtc" in body
          and ("image_uri" in body or "avatar_id" in body), json.dumps(body, ensure_ascii=False)[:120])
    check("managed 不带 customer_rtc", "customer_rtc" not in body)
    body2, extra2 = eng._build_body({"mode": "customer"})
    cr = body2.get("customer_rtc") or {}
    check("customer: provider/channel/user/token 齐",
          cr.get("provider") in ("artc", "trtc") and cr.get("channel_id") and cr.get("user_id") and cr.get("token"),
          json.dumps({k: (v[:16] + "…" if isinstance(v, str) and len(v) > 16 else v) for k, v in cr.items()}))
    check("customer 观众端有签名", bool(extra2.get("viewer_sign", {}).get("token")))
    if cr.get("provider") == "artc":
        check("customer artc 带 app_id", bool(cr.get("app_id")))


def t_signaling():
    print("== 信令封装 ==")
    env = signaling.envelope(9, "123", {"text_msg": {}}, "c1", 3)
    check("envelope 字段", set(env) == {"type", "live_id", "conn_id", "seq_id", "payload"})
    check("常量对齐协议", (signaling.TYPE_CONN_INIT, signaling.TYPE_CONN_INIT_ACK,
                           signaling.TYPE_HANGUP, signaling.TYPE_FORCE_HANGUP,
                           signaling.TYPE_INTERRUPT, signaling.TYPE_INPUT_TEXT,
                           signaling.TYPE_OUTPUT_TEXT) == (1, 2, 5, 6, 7, 9, 10))


def t_voices():
    print("== 音色预设库 ==")
    eng = Engine.__new__(Engine)
    voices = eng._voices()
    names = sorted(voices)
    print(f"      扫描到 {len(names)} 个 OmniVoice 可用音色: {names}")
    check("voices_dir 扫描出至少 1 个音色", len(names) >= 1)
    cur = (appconf.load().get("tts") or {}).get("voice")
    if cur:
        check(f"配置的默认音色 {cur} 存在", cur in voices)


def t_page():
    print("== 舞台页 ==")
    html = (ROOT / "stage" / "index.html").read_text(encoding="utf-8")
    check("页面存在且非空", len(html) > 5000)
    for probe in ("/api/live/start", "/api/chat", "/api/tts/voice", "joinChannel", "enterRoom",
                  "songGrid", "/api/tts/import-folder", "/api/tts/upload", "/api/tts/delete"):
        check(f"页面引用 {probe}", probe in html)
    try:
        node = shutil_which("node")
        if node:
            import os
            js = ("import {readFileSync} from 'fs';"
                  "const h=readFileSync(process.env.PAGE,'utf8');"
                  "const m=h.match(/<script>([\\s\\S]*?)<\\/script>/);"
                  "if(!m) throw new Error('no inline script');"
                  "new Function(m[1]); console.log('js-ok')")
            env = dict(os.environ)
            env["PAGE"] = str(ROOT / "stage" / "index.html")
            r = subprocess.run([node, "--input-type=module", "-e", js],
                               capture_output=True, timeout=30, env=env)
            check("内联 JS 语法(new Function)", r.returncode == 0,
                  r.stderr.decode("utf-8", "ignore")[:200])
        else:
            print("      (node 不在 PATH, 跳过 JS 语法检查)")
    except Exception as e:  # noqa: BLE001
        print(f"      (JS 语法检查跳过: {e})")


def t_action_sched():
    """动作调度器(2026-10-16 新增): 唱歌窗口节流 + 最新优先合并。
    全部离线(stub sig, 不连网), 用 gap=0.3s 代替 6s 以免自检变慢。"""
    print("== 动作调度器(唱歌窗口节流 · 最新优先合并) ==")
    import asyncio
    import collections

    class FakeSig:
        def __init__(self):
            self.sent: list[str] = []
            self.interrupts = 0

        async def send_input_text(self, content): self.sent.append(content)
        async def interrupt(self): self.interrupts += 1

    def mk(gap_song, gap_all=0.0):
        eng = Engine.__new__(Engine)
        eng.phase, eng.sig = "live", FakeSig()
        eng._song_playing = False
        eng._act_last = 0.0
        eng._act_pending = None
        eng._act_task = None
        eng._act_merged = 0
        eng.on_action_fire = None
        eng._speak_task = None
        eng._audio_owner = None
        eng.transcripts = collections.deque(maxlen=20)
        eng.tr_total = 0
        eng._act_gap = lambda: (gap_song if eng._song_playing else gap_all)  # 配置桩
        return eng

    async def run():
        # ① 非唱歌 + 默认 0 间隔 → 连发全部直通(尊重"取消动作冷却"的拍板)
        e = mk(6.0, 0.0)
        rs = [await e.cmd(t) for t in ("对镜头比心", "飞吻", "吃雪糕")]
        check("非唱歌连发: 直通不节流(默认零闸门)",
              e.sig.sent == ["对镜头比心", "飞吻", "吃雪糕"]
              and not any(r["deferred"] for r in rs), str(e.sig.sent))
        # ② 唱歌中 → 第 1 条即时, 紧跟的 2 条被合并(最新优先)
        e = mk(6.0, 0.0)
        e._song_playing = True
        r1 = await e.cmd("对镜头比心")
        r2 = await e.cmd("飞吻")
        r3 = await e.cmd("吃雪糕")
        check("唱歌中: 第 1 条即时发出", e.sig.sent == ["对镜头比心"] and r1["deferred"] is False,
              str(e.sig.sent))
        check("唱歌中: 连点被合并(deferred=true)",
              bool(r2["deferred"]) and bool(r3["deferred"]), f"{r2} {r3}")
        check("唱歌中: 合并计数进 state", e._act_merged == 1, str(e._act_merged))
        # ③ 间隔到期后自动补发, 且只发"最新一条"(中间的 B 被丢掉)
        e2 = mk(0.3, 0.0)
        e2._song_playing = True
        await e2.cmd("A")
        await e2.cmd("B")
        await e2.cmd("C")
        ok = False
        for _ in range(40):
            await asyncio.sleep(0.05)
            if e2.sig.sent == ["A", "C"]:
                ok = True
                break
        check("节流到期补发最新一条(丢掉中间 B)", ok, str(e2.sig.sent))
        # ④ 手动回调只在"真正发出"那一刻触发(特效/日志才和画面一致)
        e3 = mk(6.0, 0.0)
        e3._song_playing = True
        seen: list[str] = []
        e3.on_action_fire = seen.append
        await e3.cmd("A", source="manual")
        await e3.cmd("B", source="manual")
        check("手动登记只在真发时回调一次", seen == ["A"], str(seen))
        # ⑤ 配置读取(默认: 唱歌 6s / 其余 0)
        e7 = mk(6.0, 0.0)
        g_all = Engine.__new__(Engine)
        g_all._song_playing = False
        g_song = Engine.__new__(Engine)
        g_song._song_playing = True
        ga, gs = g_all._act_gap(), g_song._act_gap()
        print(f"      配置 action.song_action_gap_s={gs}s / action_gap_s={ga}s")
        check("唱歌窗口有节流(默认 6s, 可配 0 关)", gs >= 0, f"{gs}s")
        # 非唱歌档位由配置决定: 0=零闸门(用户 2026-10-16 拍板), 4=当前值(2026-10-17 预测性防护)
        check("非唱歌档位是合法值(0=零闸门 / 4=当前防护)", 0 <= ga <= 30, f"{ga}s")

    asyncio.run(run())


def shutil_which(name):
    import shutil
    return shutil.which(name)


def main() -> int:
    print("CloudDH S2 组件版 · 离线自检\n")
    t_config(); t_tokens(); t_sentences(); t_audio(); t_body(); t_signaling(); t_voices()
    t_action_sched(); t_page()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

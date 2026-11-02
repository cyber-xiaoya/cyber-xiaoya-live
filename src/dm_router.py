"""dm_router.py — 弹幕→指令 自动触发路由(Stage B, 2026-10-12 用户拍板)。

漏斗:
  T0 本地匹配  归一化(去称呼/请求壳/量词/语气词) + 别名表 → 动作名命中, 零 token 零延迟
  预筛        启发式判断弹幕"像不像指令", 不过的不花 T2
  T2 编译器    独立 LLM 调用(无历史, flash+thinking disabled, 纯 JSON): 槽位白名单
              (吃X/喝X/拿出X + 外表封闭集) + 红线正则双重拦截 → fire|none|deny
  模式(用户定稿) off=纯手点 | confirm=T0直发+T2结果进确认条 | auto=T2结果也直发
  护栏        全局冷却(cooldown_s, 仅自动触发), 红线, 疑似队列(确认条, 180s 过期)
  Top10       触发即计数, 按天分桶落盘 tools/dm-stats.json, 排序取近 1 天(1 天滚动)

铁律: 指令静默不出声、不进对话记忆; 主聊天 LLM 永远看不到本模块的任何调用;
     T2 与聊天是两个独立调用, prompt cache 按前缀各存各份互不干扰。
"""
from __future__ import annotations

import asyncio
import collections
import json
import pathlib
import re
import time
from datetime import date, timedelta

import httpx

try:
    import certifi
    _VERIFY = certifi.where()
except Exception:                                    # pragma: no cover
    _VERIFY = True

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALIAS_PATH = ROOT / "tools" / "dm-aliases.json"
STATS_PATH = ROOT / "tools" / "dm-stats.json"
# 过滤词表(2026-10-19 外置): 预筛词/前缀/尾词/红线/道具包装/T0 子串约束都在这里,
# 改词不必重启桥(按 mtime 热读), 缺项回落本文件里的内置默认值。
FILTER_PATH = ROOT / "tools" / "dm-filter.json"

# 外表/发型封闭集(用户拍板: 只允许这几个, 不开放自由发挥)
STATIC_NAMES = ["双马尾", "高马尾", "扎丸子头", "编辫子", "反戴棒球帽",
                "戴上蝴蝶结发夹", "拿出墨镜戴上", "涂口红"]

# 红线(用户点名: 脱衣/换装/泳装类绝不进直播间; 其余沿用人设审核红线)
REDLINE_RES = [
    r"[脱换穿][一条件套个]?(衣|衣服|裤|袜子?|裙子|装|泳装|泳衣|比基尼|内衣|胸罩|丝袜|鞋)",
    r"泳装|泳衣|比基尼|裸(体|露)|脱光|色情|开腿|胸罩|内衣|援交|卖淫",
    r"政治|国家领导|主席|政府|赌博|赌钱|赌场|毒品|吸毒|枪支|自杀|自残",
]

# 请求壳(预筛用) + 前缀剥离表(逐个试, 剥到剥不动) + 尾词表(量词/语气/疑问尾)
SHELL_RE = re.compile(
    r"(可以[^。！？?！]{0,8}吗|能不能|给我?|来一?[个份只条颗点]|来个|求一?个?|麻烦|请你?|帮忙|"
    r"表演|展示|看一下?|看看|整[个来]|安排|要不要|想不想看?|我想|我要)")
PREFIXES = ["我想看", "我想要", "我想来", "我想", "我要看", "我要", "给我来", "给我",
            "来一个", "来个", "来一份", "来只", "来条", "来颗", "来点", "来段", "来发个",
            "求一个", "求个", "求", "麻烦你", "麻烦", "请你", "请", "帮忙", "表演",
            "展示", "看一下", "看看", "整一个", "整个", "安排", "上一个", "想要",
            "想看", "想听", "可以", "能不能", "发个"]
TAIL_WORDS = ["一个", "一下", "一次", "一份", "呗", "吧", "啊", "呀", "嘛", "哦", "嗯", "啦",
              "咯", "哟", "么", "捏", "谢谢", "好嘛", "好不好", "行吗", "可以吗", "看看",
              "瞧瞧", "吗", "呢"]
CALL_RE = re.compile(r"^(小雅|雅雅|雅宝|小汐|汐汐|汐宝|主播|宝(贝)?|老婆|姐姐|妹妹|美女)[,，、!！?？\s~]*")

# 复合指令的"先后连接词"(2026-10-19 引入, 2026-10-20 回放后收紧):
#   只认**真的把两个动作串起来**的词。「先 / 再 / 完了 / 最后」一律不算 ——
#   它们在日常聊天里到处都是("先不要发""再开发""唱歌做完了吗"), 一算就误判。
SEQ_RE = re.compile(r"(然后|接着|随后|之后|紧跟着|再来)")

# 预筛: 命中任一动词/名词线索才进 T2(保护成本), 命中不了当闲聊
PREFILTER_WORDS = [
    "吃", "喝", "拿", "戴", "脱", "换", "穿", "表演", "展示", "来一", "来个", "求个", "来份",
    "看一下", "看看", "比心", "飞吻", "亲", "抱抱", "跳舞", "唱歌", "点头", "摇头", "挥手",
    "转圈", "敬礼", "加油", "关注", "响指", "卖萌", "生气", "哭", "笑一个", "撒娇", "扭",
    "托腮", "思考", "点赞", "拍手", "眨", "wink", "伸爪", "搓", "揉", "擦", "抹", "戳",
    "举牌", "叉腰", "投降", "委屈", "无聊", "伤心", "难过", "夸", "打赏", "礼物", "马尾",
    "丸子头", "辫子", "帽子", "猫", "狗", "西瓜", "奶茶", "咖啡", "可乐",
]


def _now() -> float:
    return time.time()


# 时间闸豁免(用户 2026-10-13 拍板): 手动手点/手动批准不受任何时间限制;
# 反过来, 手动动作会登记进"刚做过", 压制自动路径的重复触发。
MANUAL_SOURCES = ("confirm", "manual", "vip", "choreo")
AUTO_SOURCES = ("t0", "t2", "t2k", "trust")



def _ts() -> str:
    """日志时间戳(2026-10-31): 出问题时要能按时间对账(此前业务日志没有时间)。"""
    return time.strftime("%H:%M:%S")

class DanmakuRouter:
    def __init__(self) -> None:
        self.engine = None
        self._fetch_names = None              # async () -> list[str] (桥注入)
        self._conf_get = None                 # () -> dict (桥注入 appconf)
        self._names: set[str] = set()         # 库内动作名(动态刷新)
        self._dict: dict[str, str] = {}       # 匹配词 → 发送文本
        self._aliases: dict[str, list[str]] = {}
        self._t2_sem = asyncio.Semaphore(2)
        self._last_fire = 0.0
        self._last_fired_text = ""            # 刚做过的动作(suggest 的"刚做过"用)
        self._fire_hist: dict[str, float] = {}   # 动作→最近触发时刻(逐动作记账)
        self._fire_count: dict[str, int] = {}    # 动作→本场累计触发次数(递增窗口用)
        self._last_suggest = 0.0              # 新建议限速(6s 内最多 1 条新的, 防确认条刷屏)
        self._trusted: dict[str, int] = {}    # 指令→用户确认次数(≥2 自动执行, 逐条信任)
        self._pending: list[dict] = []
        self._next_id = 1
        # 弹幕指令缓冲(2026-10-19 用户反馈"洪峰时只处理最后一条"后改造):
        # 原来单槽"最新覆盖" —— 一个 4s 周期里观众发 8 条不同指令, 只处理最后 1 条,
        # 其余静默消失且不记账。现在留最近 8 条: 每拍**全扫一遍 T0(零成本)**, 命中的
        # 按到达顺序发; 只有不确定的那 1 条才花 T2, 预算超出的计数丢弃。
        self._inbox: collections.deque = collections.deque(maxlen=8)
        self._inbox_dropped = 0               # 队列满被挤掉的条数(诊断)
        self._t2_skipped = 0                  # 因 T2 预算/无命中被跳过的条数(诊断)
        # ---- 过滤词表热读(外置 tools/dm-filter.json) ----
        self._filt_mtime = -1.0
        self._filt_data: dict = {}
        # ── 指令流水(2026-10-14 用户要求: 画面中间偏右的"动作指令"特效) ──
        #    recent: 最近若干条"从弹幕里精简出来的指令"(含被闸门拦下/拒绝的), 供滚动展示
        #    last:   最近一次**真正执行**的动作(供高亮放大 + 小礼花)
        self._cmd_feed: collections.deque = collections.deque(maxlen=24)
        self._last_cmd: dict = {}
        self.stats: dict = {"days": {}}
        self._dirty = False

    # 2026-10-20 用户要求: 画面右侧那圈"动作指令"特效是给**观众**看的 —— 只该滚观众自己发的弹幕
    # 被翻译成指令、然后被选中的过程。手动点的快捷指令、🎲 随机表演的动作、以及程序直接发给 Vidu
    # 的东西都**不该出现在上面**(观众会以为是自己发的)。
    _FEED_HIDDEN_SOURCES = ("manual", "random")

    def _feed_add(self, kind: str, text: str, name: str = "", source: str = "") -> None:
        """记一条指令流水。kind: cand(提取出的候选) / fire(已执行) / suggest(进确认条) / deny(红线拒绝)。
        只做记账, 任何异常都不影响主链路。"""
        try:
            if str(source or "").lower() in self._FEED_HIDDEN_SOURCES:
                return                      # 手点/随机表演: 不进特效

            item = {"kind": kind, "text": (text or "").strip()[:24],
                    "name": (name or "").strip()[:24], "source": source,
                    "ts": round(_now(), 2)}
            self._cmd_feed.append(item)
            if kind == "fire":
                self._last_cmd = dict(item)
        except Exception:                            # noqa: BLE001
            pass

    def cmd_feed(self) -> dict:
        """给舞台页特效用: {"last": 最近执行的动作, "recent": 指令流水}。"""
        try:
            return {"last": dict(self._last_cmd), "recent": list(self._cmd_feed)}
        except Exception:                            # noqa: BLE001
            return {"last": {}, "recent": []}

    # ──────────── 初始化 / 后台任务 ────────────

    def init(self, engine, fetch_names=None, conf_get=None) -> None:
        self.engine = engine
        self._fetch_names = fetch_names
        self._conf_get = conf_get or (lambda: {})
        self._load_stats()
        self._load_aliases()

    async def start(self) -> None:
        asyncio.create_task(self._name_refresh_loop())
        asyncio.create_task(self._stats_save_loop())
        asyncio.create_task(self._inbox_worker())

    def _conf(self) -> dict:
        try:
            c = dict(self._conf_get() or {})
        except Exception:
            c = {}
        # 同动作递增窗口(2026-10-14 用户拍板: 原 120/600/1800 太长 → 12/60/180)
        wins = c.get("same_action_windows") or [12, 60, 180]
        try:
            wins = [max(0, int(x)) for x in list(wins)[:3]]
        except Exception:                            # noqa: BLE001
            wins = [12, 60, 180]
        while len(wins) < 3:
            wins.append(wins[-1] if wins else 12)
        return {"enabled": bool(c.get("enabled", False)),
                "mode": (c.get("mode") or "confirm").strip().lower(),
                # ⭐ cooldown_s / same_action_windows 支持 0 = 关闭(2026-10-16 用户拍板:
                #    "取消掉动作冷却, 有些动作观众就是喜欢看") —— 下限放开到 0, 别再夹到 2。
                "cooldown_s": max(0, int(c.get("cooldown_s", 4))),
                "read_interval_s": max(2, int(c.get("read_interval_s", 4))),
                "same_action_windows": wins}

    # ──────────── 词典 / 归一化 / T0 ────────────

    def _load_aliases(self) -> None:
        try:
            self._aliases = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
        except Exception:
            self._aliases = {}
        self._rebuild_dict()

    def _rebuild_dict(self) -> None:
        d: dict[str, str] = {}
        for n in list(self._names) + self._statics():
            d[n] = n
        for target, arr in (self._aliases or {}).items():
            for a in (arr or []):
                d.setdefault(a, target)
        self._dict = d

    async def _name_refresh_loop(self) -> None:
        while True:
            try:
                if self._fetch_names:
                    names = await self._fetch_names()
                    if names and set(names) != self._names:
                        self._names = set(names)
                        self._rebuild_dict()
                        print(f"[dmcmd] 动作词表已刷新: {len(names)} 条", flush=True)
            except Exception as e:                   # noqa: BLE001
                print(f"[dmcmd] 动作词表刷新失败(下轮重试): {e}", flush=True)
            await asyncio.sleep(600)

    # ──────────── 过滤词表(外置 tools/dm-filter.json, 按 mtime 热读) ────────────

    def _filt(self) -> dict:
        """读 tools/dm-filter.json。任何失败都回落空 dict → 各 getter 用内置默认值,
        词表坏了绝不影响直播主链路。改文件即热生效(下一次取值), 不用重启桥。"""
        try:
            m = FILTER_PATH.stat().st_mtime
        except OSError:
            m = 0.0
        if m != self._filt_mtime:
            data: dict = {}
            try:
                if m:
                    data = json.loads(FILTER_PATH.read_text(encoding="utf-8")) or {}
            except Exception as e:                       # noqa: BLE001
                print(f"[dmcmd] 过滤词表读取失败(回落内置默认): {e}", flush=True)
                data = {}
            self._filt_data = data
            self._filt_mtime = m
            self._rebuild_dict()                          # 静态外观集可能变了
        return self._filt_data

    def _fw(self) -> list[str]:
        raw = self._filt().get("prefilter_words") or PREFILTER_WORDS
        return [str(x) for x in raw if str(x).strip()]

    def _prefixes(self) -> list[str]:
        raw = self._filt().get("prefixes") or PREFIXES
        return [str(x) for x in raw if str(x).strip()]

    def _tails(self) -> list[str]:
        raw = self._filt().get("tail_words") or TAIL_WORDS
        return [str(x) for x in raw if str(x).strip()]

    def _statics(self) -> list[str]:
        raw = self._filt().get("static_names") or STATIC_NAMES
        return [str(x) for x in raw if str(x).strip()]

    def _redlines(self) -> list[re.Pattern]:
        raw = self._filt().get("redline") or REDLINE_RES
        out: list[re.Pattern] = []
        for p in raw:
            try:
                out.append(re.compile(str(p)))
            except Exception:                            # noqa: BLE001
                continue
        return out or [re.compile(p) for p in REDLINE_RES]

    def _match_cfg(self) -> dict:
        m = self._filt().get("match") or {}
        try:
            return {"min_key": int(m.get("substring_min_key_len", 3)),
                    "max_chars": int(m.get("substring_max_sentence_chars", 12)),
                    "min_ratio": float(m.get("substring_min_ratio", 0.5))}
        except Exception:                                # noqa: BLE001
            return {"min_key": 3, "max_chars": 12, "min_ratio": 0.5}

    def _composite_cfg(self) -> dict:
        c = self._filt().get("composite") or {}
        try:
            return {"enabled": bool(c.get("enabled", True)),
                    "min_actions": max(2, int(c.get("min_actions", 2))),
                    "max_actions": max(2, int(c.get("max_actions", 3))),
                    "max_chars": max(8, int(c.get("max_chars", 48)))}
        except Exception:                                # noqa: BLE001
            return {"enabled": True, "min_actions": 2, "max_actions": 3, "max_chars": 48}

    def _prop_cfg(self) -> dict:
        return self._filt().get("prop_wrap") or {}

    # ──────────── 归一化 / T0 ────────────

    def _normalize(self, text: str) -> str:
        t = (text or "").strip()
        for _ in range(3):
            t2 = CALL_RE.sub("", t).strip()
            if t2 == t:
                break
            t = t2
        t = re.sub(r"[。，,、!！?？~～.。\s]", "", t)
        prefixes, tails = self._prefixes(), self._tails()
        for _ in range(len(prefixes)):            # 前缀壳: 剥到剥不动
            for p in prefixes:
                if t.startswith(p) and len(t) > len(p):
                    t = t[len(p):]
                    break
            else:
                break
        for _ in range(6):                        # 尾部量词/语气/疑问尾: 剥到剥不动
            for w in tails:
                if t.endswith(w) and len(t) > len(w):
                    t = t[: -len(w)]
                    break
            else:
                break
        return t.strip()

    def match(self, content: str) -> str | None:
        """T0: 返回应发送的指令文本, 未命中返回 None。
        2026-10-13 收紧: ≤2 字短词(唱歌/关注/加油/猫)只整句精确命中 —— 观众
        「你唱歌真好听」这类评价句曾因子串包含被误触发开唱; ≥3 字才做子串匹配。
        2026-10-19 再加约束: 子串命中还要满足「整句够短」或「命中片段占整句够大」,
        否则「拿出手机看看我小蛋糕」这种长句会被"小蛋糕"骗走。"""
        try:
            cfg = self._match_cfg()
            n = self._normalize(content)
            if not n:
                return None
            if n in self._dict:
                return self._dict[n]
            if len(n) < 2:
                return None
            best, best_len, best_key = None, 0, ""
            for k, target in self._dict.items():
                if len(k) < cfg["min_key"] or k not in n:
                    continue
                if len(k) > best_len:
                    best, best_len, best_key = target, len(k), k
            if best is None:
                return None
            # 疑问/否定语境不直发(2026-10-20 回放发现「为什么不能站起来」「没站起来啊」
            #   「这次应该不会站起来了吧」都被当成了指令) —— 这类交给 T2 判。
            pos = n.find(best_key)
            if pos > 0 and re.search(r"(为什么|怎么|为啥|是不是|会不会|能不能|不能|不要|不会|别|没|不)$",
                                     n[max(0, pos - 3):pos]):
                return None
            if pos > 0 and re.search(r"[吗呢？?]$", n):
                return None
            # 归一化会把句尾「吗/呢」剥掉, 所以疑问句要用**原文**再判一次
            if pos > 0 and re.search(r"[吗呢？?]\s*$", (content or "").strip()):
                return None
            if len(n) <= cfg["max_chars"] or (best_len / max(1, len(n))) >= cfg["min_ratio"]:
                return best
            return None
        except Exception:                            # noqa: BLE001
            return None

    def match_all(self, text: str) -> list[tuple[int, str]]:
        """按出现顺序返回**所有**命中的 (位置, 动作目标)。用于复合指令判定。
        长键优先、区间不重叠 —— 「拿出墨镜戴上」不会在里面再算一遍「戴上」。"""
        try:
            n = self._normalize(text)
            if not n:
                return []
            keys = sorted(((k, t) for k, t in self._dict.items() if len(k) >= 2),
                          key=lambda kv: -len(kv[0]))
            used = [False] * (len(n) + 1)
            hits: list[tuple[int, str]] = []
            for k, target in keys:
                start = n.find(k)
                while start != -1:
                    end = start + len(k)
                    if not any(used[start:end]):
                        for i in range(start, end):
                            used[i] = True
                        hits.append((start, target))
                    start = n.find(k, start + 1)
            hits.sort()
            out: list[tuple[int, str]] = []
            seen: set[str] = set()
            for pos, target in hits:
                if target not in seen:
                    seen.add(target)
                    out.append((pos, target))
            return out
        except Exception:                            # noqa: BLE001
            return []

    def _strip_shell(self, content: str) -> str:
        """去开头称呼 + 去请求壳, 但**保留标点与顺序** —— 复合指令要原样交给 Vidu
        (用户实测: 直接把「先戴上墨镜，然后涂口红」发进输入框, Vidu 会连着做两个动作)。"""
        t = (content or "").strip()
        t = CALL_RE.sub("", t).strip()
        prefixes = self._prefixes()
        for _ in range(len(prefixes)):
            for p in prefixes:
                if t.startswith(p) and len(t) > len(p):
                    t = t[len(p):].lstrip(" ，,、!！~")
                    break
            else:
                break
        return t.strip()[:120]

    def _negated_at(self, n: str, pos: int) -> bool:
        """命中点前面是不是否定/劝阻(「不要再跳舞了」「先别哭」)。
        ⚠ 不能用"整句含别"判断: 「把头发**别**到耳朵后」的"别"是动词。"""
        pre = n[max(0, pos - 3):pos]
        if re.search(r"(别|不要|不用|甭|莫|不许|不准)$", pre):
            return True
        return bool(re.match(r"(别|不要|不用|甭|莫|不许|不准)", n))

    def _strip_symbols(self, t: str) -> str:
        """去掉 [捂脸]/[比心]/[看] 这类表情标签和 emoji —— 2026-10-20 回放发现
        它们会被别名表当成动作命中(「站起来[捂脸]」变成两个动作), 必须清掉再判。"""
        s = re.sub(r"\[[^\]]{1,10}\]", "", t or "")
        s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]", "", s)
        return re.sub(r"\s{2,}", " ", s).strip()

    def composite(self, content: str) -> str | None:
        """① 复合指令直通(2026-10-19): 一句里要 ≥2 个不同动作 → 整句原样交给 Vidu。
        原来 T0 只会挑最长的一个命中、T2 又只许输出单条, 所以「先戴墨镜再涂口红」必丢一半。"""
        try:
            cfg = self._composite_cfg()
            if not cfg["enabled"]:
                return None
            clean = self._strip_shell(content)
            clean = self._strip_symbols(clean)
            if not clean:
                return None
            hits = self.match_all(clean)
            targets = [t for _, t in hits]
            enough = len(targets) >= cfg["min_actions"]
            seq = bool(SEQ_RE.search(clean))
            # 2026-10-20 回放实测修正(两轮): 光"≥2 个动作词"会把吐槽句当指令
            # (「咋突然唱歌还吃汉堡了」「感谢我关注吧」「唱歌做完了吗」「站起来[捂脸]」)。
            # 现在只有三种结构放行:
            #   ① 真连接词(然后/接着/随后/之后) + ≥1 个动作
            #   ② 以动作开场 + ≥2 个动作 + 整句 ≤16 字 + 动作之间没有废话词 + 不是疑问/否定
            #   ③ 请求壳(给我/能不能/看一下…) + ≥2 个动作 + 整句 ≤30 字 + 不是疑问
            start_hit = bool(hits) and hits[0][0] <= 1
            shell = bool(SHELL_RE.search(clean))
            ask = bool(re.search(r"(怎么|为什么|为啥|吗|呢|不|没|没有|是不是)", clean))
            adjacent = False
            if len(hits) >= 2:
                p0, t0 = hits[0]
                p1, t1 = hits[1]
                key0 = next((k for k, v in self._dict.items() if v == t0 and k in clean), "")
                end0 = p0 + len(key0) if key0 else p0 + 1
                gap = clean[end0:p1]
                adjacent = (p1 <= end0 + 1) or bool(re.search(r"[，,。;；\s]", gap))
            tier_a = seq and len(targets) >= 1
            tier_b = start_hit and enough and len(clean) <= 16 and not ask and adjacent
            tier_c = shell and enough and len(clean) <= 30 and not ask
            if not (tier_a or tier_b or tier_c):
                return None
            if len(targets) > cfg["max_actions"]:
                return None                       # 太复杂 → 交给 T2(none) / 不触发
            # 「不要再跳舞了」「先别哭」: 命中点前面有否定 → 不是指令
            norm = self._normalize(clean)
            if any(self._negated_at(norm, p) for p, _ in hits):
                return None
            if len(clean) > cfg["max_chars"]:
                return None
            return clean
        except Exception:                            # noqa: BLE001
            return None

    def _prop_rewrite(self, text: str) -> str | None:
        """② 道具安全包装(2026-10-19 用户拍板): 武器类道具一律改成「玩具X」再发 Vidu,
        观众看到的是表演感、Vidu 也能正常出画面; 真实暴力语境(打/杀/射/砍)不包装, 交给红线。"""
        cfg = self._prop_cfg()
        pat = cfg.get("pattern") or ""
        if not pat:
            return None
        try:
            prefix = cfg.get("prefix") or "一把迷你玩具"
            block = cfg.get("block_verbs") or ""
            verbs = cfg.get("verbs") or ""
            if block and re.search(block, text):
                return None
            m = re.search(pat, text, re.I)
            if not m:
                return None
            if "玩具" in text:
                return None                          # 已经是「玩具…」, 别重复加
            if verbs and not re.search(verbs, text):
                return None                          # 没说要拿出来 / 展示, 不猜
            return f"拿出{prefix}{m.group(0)}"
        except Exception:                            # noqa: BLE001
            return None

    def canonical(self, text: str) -> str:
        """编译结果归一化: 已知动作/别名 → 库内原名(比个心→对镜头比心), 否则去壳去尾。"""
        t = (text or "").strip()
        if not t:
            return ""
        if t in self._dict:
            return self._dict[t]
        n = self._normalize(t)
        if n in self._dict:
            return self._dict[n]
        return n or t

    # ──────────── 红线 / 冷却 / 触发 ────────────

    def looks_like_cmd(self, content: str) -> bool:
        """零成本判定: 这条弹幕是不是动作指令 —— 给播报通道用(2026-10-19 用户拍板:
        「动作类弹幕不念、其他照旧」)。只走 composite / 道具包装 / T0 三步, 不花 token,
        也不做任何记账 —— 真正的触发仍由 _process_one 那一拍决定。"""
        try:
            if self.composite(content):
                return True
            if self._prop_rewrite(content):
                return True
            return bool(self.match(content))
        except Exception:                            # noqa: BLE001
            return False

    def _redlined(self, text: str) -> str | None:
        for p in self._redlines():
            m = p.search(text)
            if m:
                return m.group(0)
        return None

    def _block_words(self, auto_only: bool = False) -> list[str]:
        """🚫 动作黑名单(tools/dm-filter.json block_commands, 改文件即时生效):
          · words            → **任何来源**都不做(站起来/腿: 安全与平台风险)
          · auto_only_words  → **只拦弹幕自动触发**; 手点面板/直发输入框照常执行
                                (舌头类: 别被观众带节奏, 自己想吐舌就吐)"""
        b = self._filt().get("block_commands") or {}
        if not b.get("enabled", True):
            return []
        out = [str(w) for w in (b.get("words") or []) if str(w).strip()]
        if auto_only:
            out += [str(w) for w in (b.get("auto_only_words") or []) if str(w).strip()]
        return out

    def blocked(self, text: str, source: str = "auto") -> str | None:
        """命中黑名单返回命中的词, 否则 None。
        手动来源(MANUAL_SOURCES: manual/confirm)只受 words 约束, 不受 auto_only_words 约束。"""
        t = (text or "").strip()
        if not t:
            return None
        _src = str(source or "").lower()
        # 2026-11-01 用户要求: 操作者自己发起的通道(手点/直发/贵宾/编排)不做词表过滤
        _exempt = [str(x).lower() for x in ((self._filt().get("block_commands") or {}).get("exempt_sources") or [])]
        if _src in _exempt:
            return None
        is_manual = _src in MANUAL_SOURCES
        for w in self._block_words(auto_only=not is_manual):
            if w in t:
                return w
        return None

    def _cooled(self) -> bool:
        cd = self._conf()["cooldown_s"]
        return (_now() - self._last_fire) >= cd

    async def do_fire(self, text: str, source: str, user: str = "") -> bool:
        # 48 字上限(2026-10-19): 复合指令「先戴上墨镜，然后涂口红」要留得下, 原来切 24 会截断
        text = (text or "").strip()[:48]
        if not text:
            return False
        # 🚫 黑名单最先判(2026-10-20): 「站起来」这类不管从 T0/T2/复合/确认条来, 一律不进 Vidu
        if (bw := self.blocked(text, source)) or self.blocked(self._normalize(text), source):
            self._feed_add("blocked", text, "", source)
            print(f"[dmcmd {_ts()}] 🚫 动作已屏蔽({source}): '{text}' ← 黑名单 '{bw or text}'", flush=True)
            return False
        if (hit := self._redlined(text)) or self._redlined(self._normalize(text)):
            self._feed_add("blocked", text, "", source)
            print(f"[dmcmd {_ts()}] 红线拦截({source}): '{text}' ← 命中 '{hit}'", flush=True)
            return False
        canon = self.canonical(text)
        # ⭐ 同动作递增窗口(2026-10-13 引入, 2026-10-14 用户拍板收紧)——
        #    逐动作记账(中间插别的动作也绕不过), 窗口随次数递增, 取值来自
        #    config danmaku_cmd.same_action_windows(默认 [12, 60, 180] 秒):
        #    第 1-2 次 12s → 第 3-5 次 60s → 之后 180s。只限自动源; 手动永久豁免。
        if source not in MANUAL_SOURCES:
            n = int(self._fire_count.get(canon, 0))
            w = self._conf()["same_action_windows"]
            win = w[0] if n < 2 else (w[1] if n < 5 else w[2])
            if (_now() - float(self._fire_hist.get(canon, 0))) < win:
                self._feed_add("blocked", text, canon, source)
                print(f"[dmcmd] 同动作 {win}s 窗口不重复自动触发({source}, 本场第{n}次): '{text}'",
                      flush=True)
                return False
        if source not in MANUAL_SOURCES and not self._cooled():
            self._feed_add("blocked", text, canon, source)
            print(f"[dmcmd {_ts()}] 冷却中丢弃({source}): '{text}'", flush=True)
            return False
        try:
            await self.engine.cmd(text, source, user=user)      # 带上来源: t0/t0c/t0p/t2/trust/confirm 一眼可查
        except Exception as e:                       # noqa: BLE001
            self._feed_add("blocked", text, canon, source)
            print(f"[dmcmd] 发送失败({source}) '{text}': {e}", flush=True)
            return False
        self._last_fire = _now()
        self._last_fired_text = canon
        self._fire_hist[canon] = self._last_fire
        self._fire_count[canon] = int(self._fire_count.get(canon, 0)) + 1
        self.count(text)
        self._feed_add("fire", text, canon, source)      # 特效: 高亮放大 + 小礼花
        print(f"[dmcmd {_ts()}] ✅ 触发({source}): {text}", flush=True)
        return True

    def fire_async(self, text: str, source: str, user: str = "") -> None:
        asyncio.get_event_loop().create_task(self.do_fire(text, source, user))

    def note_manual(self, text: str) -> None:
        """手动手点/直发成功后的登记(桥 /api/cmd 调用)。

        ⭐ 2026-10-14 用户纠偏: **手动指令不参与任何时间闸门记账** —— 之前这里会写
        `_last_fire`(启动 5s 全局冷却) 和 `_fire_count`(累加次数→把该动作推进 60s/180s 高档),
        结果"手动点的动作反而给它自己招来冷却": 日志实锤 `冷却中丢弃(t0): 张开双臂求抱抱`、
        `同动作 180s 窗口…本场第9次: 对镜头比心`(第 9 次里含手点)。

        现在: 手动 = 直通且在自动闸门眼里"不存在" ——
        · 不写 `_last_fire`  → 不启动全局冷却(你点完, 自动路径照样能立刻做别的)
        · 不写 `_fire_hist`  → 不产生逐动作窗口
        · 不累加 `_fire_count` → 不会把手动点击算成"本场第 N 次"而升级窗口档位
        只保留 `_last_fired_text`: 让确认条"刚做过 90s 不重复问"仍然生效(避免同一动作反复弹确认条)。
        """
        canon = self.canonical(text)
        if canon:
            self._last_fired_text = canon
            self._feed_add("fire", text, canon, "manual")   # 手点也让特效亮一下
            print(f"[dmcmd {_ts()}] 🖐 手动直发(不入冷却/不升级窗口): {canon}", flush=True)

    # ──────────── T2 编译器 ────────────

    def _compiler_prompt(self) -> str:
        names = "\n".join("- " + n for n in sorted(self._names)) or "- (清单未加载)"
        static = "/".join(STATIC_NAMES)
        return (
            "你是直播间弹幕指令编译器。观众给一位坐姿女主播发弹幕, 你判断弹幕是否在请求她做动作,"
            "并把它编译成简短动作指令。\n"
            "【已知动作清单】\n" + names + "\n"
            "【规则】\n"
            '1. 弹幕意图对应清单里某条动作 → {"result":"fire","text":"清单原文"}\n'
            '2. 想吃东西/喝东西/拿出某样日常安全物品且清单里没有 → {"result":"fire","text":"吃哈密瓜"}'
            "(新短语≤10字, 动词开头: 吃/喝/拿出)\n"
            '2b. 道具/器械类(枪、刀、火箭筒之类)一律写成「拿出一把迷你玩具X」, 例: {"result":"fire","text":"拿出一把迷你玩具AK47"}\n'
            '2c. 一句里要求**连着做 2-3 个动作**(先…然后…再…) → 把每个动作中文逗号连起来原样输出,'
            ' 例: {"result":"fire","text":"先戴上墨镜，然后涂口红"}, 超过 3 个只取前 3 个\n'
            '3. 发型/外表类意图 → 只准输出这些固定短语之一: ' + static + " → fire\n"
            '4. 其他肢体/表情类意图但清单里没有对应动作 → {"result":"none"}\n'
            '5. 脱衣/换衣/泳装/暴露/色情/低俗/政治/赌博/违禁/真实暴力(打人、杀人) → '
            '{"result":"deny","reason":"≤10字"}\n'
            '6. 闲聊/提问/夸奖/评价/吐槽(如"唱歌真好听""你跳舞真好看""这个吃水果不太行")'
            '不是动作请求 → {"result":"none"}\n'
            "只输出一行 JSON, 不要解释, 不要 markdown。"
        )

    async def _t2(self, content: str) -> dict | None:
        llm = {}
        try:
            from . import config as appconf          # 延迟导入, 便于离线单测
            llm = appconf.load().get("llm") or {}
        except Exception:
            pass
        key = ""
        kf = (llm.get("key_file") or "").strip()
        if kf:
            try:
                key = pathlib.Path(kf).read_text(encoding="utf-8").strip()
            except Exception:
                key = ""
        key = key or (llm.get("api_key") or "") or (__import__("os").environ.get("DEEPSEEK_API_KEY") or "")
        if not key:
            print("[dmcmd] T2 跳过: 无 LLM key", flush=True)
            return None
        base = (llm.get("base_url") or "https://api.deepseek.com").rstrip("/")
        body = {
            "model": llm.get("model") or "deepseek-chat",
            "messages": [{"role": "system", "content": self._compiler_prompt()},
                         {"role": "user", "content": (content or "")[:200]}],
            "stream": False, "temperature": 0.2, "max_tokens": 200,
            "thinking": {"type": "disabled"},
        }
        async with self._t2_sem:
            async with httpx.AsyncClient(verify=_VERIFY, timeout=httpx.Timeout(25.0)) as cli:
                r = await cli.post(base + "/chat/completions",
                                   headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"},
                                   json=body)
        if r.status_code >= 400:
            print(f"[dmcmd] T2 HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        content_out = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        content_out = re.sub(r"```(json)?|```", "", content_out).strip()
        try:
            return json.loads(content_out)
        except Exception:
            m = re.search(r"\{.*\}", content_out, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return None
            return None

    async def _process_one(self, nick: str, content: str, allow_t2: bool = True) -> tuple[str, bool]:
        """单条弹幕走完整漏斗。返回 (状态, 是否消耗了 T2)。

        顺序(2026-10-19 用户三条反馈后):
          红线整句 → ① 复合指令直通 → ② 道具玩具包装 → 预筛 → T0 → (可选) T2
        状态: fire / deny / blocked / none(闲聊) / defer(排队等下一拍的 T2 预算)。
        """
        content = (content or "").strip()
        if not content:
            return ("empty", False)
        # 0a) 🚫 动作黑名单(2026-10-20): 「站起来」这类整句直接挡, 连复合/道具都不必再判
        if (bw := self.blocked(content, "t0")):
            self._feed_add("blocked", content, "", "t0")
            print(f"[dmcmd {_ts()}] 🚫 动作已屏蔽: '{content[:30]}' ← 黑名单 '{bw}'", flush=True)
            return ("blocked", False)
        # 0) 红线先挡整句(真实暴力/政治/脱衣类): 后面的包装/编译都不必再猜
        if (hit := self._redlined(content)):
            self._feed_add("blocked", content, "", "t0")
            print(f"[dmcmd] 红线拦截: '{content[:30]}' ← 命中 '{hit}'", flush=True)
            return ("blocked", False)
        # ① 复合指令: 「先戴上墨镜，然后涂口红」→ 整句原样交给 Vidu
        comp = self.composite(content)
        if comp:
            await self.do_fire(comp, "t0c", nick)
            return ("fire", False)
        # ② 道具玩具包装: 「拿出AK47」→「拿出玩具AK47」
        toy = self._prop_rewrite(content)
        if toy:
            await self.do_fire(toy, "t0p", nick)
            return ("fire", False)
        # T0 兜底复查: 上一拍因冷却转成闲聊的, 这一拍直接命中
        hit = self.match(content)
        if hit:
            await self.do_fire(hit, "t0", nick)
            return ("fire", False)
        # 预筛: 不像指令就当闲聊, 不花 T2
        # ⚠ 2026-10-19 把这步挪到 T0 **之后**: T0 免费且更准, 而"比个心/歪头笑"这类
        #   说法并不含预筛词表里的字面词, 先预筛会把它们误判成闲聊丢掉。
        if not any(w in content for w in self._fw()) and not SHELL_RE.search(content):
            return ("none", False)
        if not allow_t2:
            return ("defer", False)
        res = await self._t2(content)
        if not isinstance(res, dict):
            return ("none", True)
        result = (res.get("result") or "").strip().lower()
        if result == "deny":
            self._feed_add("deny", content, "", "t2")
            print(f"[dmcmd] T2 拒绝: '{content[:30]}' ({res.get('reason', '')})", flush=True)
            return ("deny", True)
        if result != "fire":
            return ("none", True)
        text = (res.get("text") or "").strip()[:48]
        # T2 产物也过一遍玩具包装(模型偶尔直接给「拿出AK47」)
        if (toy2 := self._prop_rewrite(text)):
            text = toy2
        if not text or self._redlined(text) or self._redlined(self._normalize(text)):
            print(f"[dmcmd] T2 产物异常/触红线: '{text[:30]}'", flush=True)
            return ("blocked", True)
        # 确认模式: T2 产物一律进确认条(2026-10-13 用户纠偏: 曾把"命中已知动作
        # 就直发(t2k)"打开导致几乎不弹确认, 已回退)。防刷屏由 suggest() 的
        # 合并×N / 限速 / 刚做过抑制 / 信任学习 / 递增窗口负责, 不靠"少问"。
        canon = self.canonical(text)
        if self._conf()["mode"] == "confirm":
            self.suggest(nick, content, canon, "T2")
        else:
            await self.do_fire(canon, "t2", nick)
        return ("fire", True)

    # ──────────── 确认队列(确认条): 合并重复 + 信任学习, 少打扰 ────────────

    def suggest(self, nick: str, content: str, text: str, source: str) -> int:
        """进确认条(已归一化的 canonical 文本)。防刷屏四道闸:
        ①信任(确认过≥2次→直接做) ②刚做过(90s 同动作不问) ③同文本合并×N
        ④新建议限速(6s 一条)。返回 item id(0=没进队列)。"""
        text = (text or "").strip()[:24]
        if not text:
            return 0
        canon = self.canonical(text) or text
        now = _now()
        # ① 信任学习: 同一条指令确认过 ≥2 次 → 以后自动执行, 不再问
        if self._trusted.get(canon, 0) >= 2:
            print(f"[dmcmd] 信任直发(确认×{self._trusted[canon]}): {canon}", flush=True)
            self.fire_async(canon, "trust")
            return 0
        # ② 刚做过: 同动作 90s 内不重复问
        if canon == self._last_fired_text and now - self._last_fire < 90:
            print(f"[dmcmd] 刚做过 '{canon}', 跳过确认", flush=True)
            return 0
        # ③ 同文本/同弹幕已有待确认 → 合并计数, 不新增条目
        for p in self._prune_pending():
            if p.get("key") == canon or p.get("content") == (content or "")[:40]:
                p["count"] = int(p.get("count", 1)) + 1
                p["ts"] = now
                if nick:
                    p["nick"] = nick
                print(f"[dmcmd] 合并确认 ×{p['count']}: '{canon}'", flush=True)
                return p["id"]
        # ④ 新建议限速: 6s 内最多 1 条新的
        if now - self._last_suggest < 6:
            print(f"[dmcmd] 新建议限速丢弃: '{canon}'", flush=True)
            return 0
        self._last_suggest = now
        item = {"id": self._next_id, "nick": nick or "观众", "content": (content or "")[:40],
                "text": canon, "key": canon, "count": 1, "source": source, "ts": now}
        self._next_id += 1
        self._pending.append(item)
        self._pending = self._pending[-6:]
        self._feed_add("suggest", canon, canon, source)   # 特效: 进确认条也算"提取出的指令"
        print(f"[dmcmd] 待确认: '{content}' → {canon}", flush=True)
        return item["id"]

    def _prune_pending(self) -> list[dict]:
        now = _now()
        self._pending = [p for p in self._pending if now - p["ts"] < 180]
        return self._pending

    def confirm(self, item_id: int) -> bool:
        item = next((p for p in self._prune_pending() if p["id"] == item_id), None)
        if not item:
            return False
        self._pending = [p for p in self._pending if p["id"] != item_id]
        canon = item.get("key") or item["text"]
        self._trusted[canon] = int(self._trusted.get(canon, 0)) + 1
        self._dirty = True                     # 信任计数随 stats 落盘(30s 合并写)
        self.fire_async(canon, "confirm")
        return True

    def dismiss(self, item_id: int) -> bool:
        before = len(self._pending)
        self._pending = [p for p in self._prune_pending() if p["id"] != item_id]
        return len(self._pending) < before

    # ──────────── Top10 计数(1 天滚动) ────────────

    def _load_stats(self) -> None:
        try:
            self.stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        except Exception:
            self.stats = {"days": {}}
        self._trusted = dict(self.stats.get("trusted") or {})
        self._prune_stats()

    def _prune_stats(self) -> None:
        days = self.stats.get("days") or {}
        keep = {(date.today() - timedelta(days=i)).isoformat() for i in range(3)}
        self.stats["days"] = {k: v for k, v in days.items() if k in keep}

    def count(self, cmd: str) -> None:
        cmd = (cmd or "").strip()[:24]
        if not cmd:
            return
        today = date.today().isoformat()
        days = self.stats.setdefault("days", {})
        bucket = days.setdefault(today, {})
        bucket[cmd] = int(bucket.get(cmd, 0)) + 1
        self._dirty = True

    def top(self, n: int = 10) -> list[dict]:
        today = date.today().isoformat()
        bucket = (self.stats.get("days") or {}).get(today) or {}
        arr = sorted(bucket.items(), key=lambda kv: -kv[1])[:max(1, n)]
        return [{"text": k, "count": v} for k, v in arr]

    async def _stats_save_loop(self) -> None:
        while True:
            try:
                if self._dirty:
                    self._prune_stats()
                    self.stats["trusted"] = dict(self._trusted)   # 信任表一起落盘
                    STATS_PATH.write_text(json.dumps(self.stats, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
                    self._dirty = False
            except Exception as e:                   # noqa: BLE001
                print(f"[dmcmd] 计数落盘失败: {e}", flush=True)
            await asyncio.sleep(30)

    # ──────────── 开关 / 状态 ────────────

    def set_config(self, enabled: bool | None = None, mode: str | None = None) -> dict:
        from . import config as appconf
        patch: dict = {}
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if mode in ("confirm", "auto"):
            patch["mode"] = mode
        if patch:
            appconf.patch({"danmaku_cmd": patch})
        return self.state()

    # ──────────── 弹幕指令队列(最新优先, 5s 一跳) ────────────

    def enqueue(self, nick: str, content: str) -> None:
        """弹幕到达进缓冲队列(最近 8 条), worker 每 read_interval_s 全扫一遍。
        2026-10-13: 指令读取太频繁 → 排队节流, LLM 调用同步大降。
        2026-10-19: 单槽"最新覆盖"改为队列 —— 洪峰里 8 条指令不再只剩最后 1 条活着;
        全扫阶段只跑 T0(免费), 每拍最多 1 条走 T2(有预算, 超出的计数丢弃)。"""
        if not self._conf()["enabled"]:
            return
        if len(self._inbox) >= (self._inbox.maxlen or 8):
            self._inbox_dropped += 1
        self._inbox.append({"nick": (nick or "").strip(),
                            "content": (content or "").strip(), "ts": _now()})

    def _drain_inbox(self) -> list[dict]:
        items = list(self._inbox)
        self._inbox.clear()
        return items

    async def _inbox_worker(self) -> None:
        while True:
            await asyncio.sleep(self._conf()["read_interval_s"])
            try:
                items = self._drain_inbox()
                if not items:
                    continue
                t2_left = 1                        # 每拍只允许 1 次 T2(成本闸门)
                for it in items:
                    try:
                        status, used_t2 = await self._process_one(
                            it.get("nick") or "", it.get("content") or "",
                            allow_t2=(t2_left > 0))
                    except Exception as e:           # noqa: BLE001
                        print(f"[dmcmd] 单条处理异常: {e}", flush=True)
                        continue
                    if used_t2:
                        t2_left -= 1
                    if status in ("none", "defer"):
                        self._t2_skipped += 1
                        if status == "defer":
                            print(f"[dmcmd] ⏭ 本拍 T2 预算用完, 留到下一拍: "
                                  f"{str(it.get('content'))[:24]}", flush=True)
            except Exception as e:                   # noqa: BLE001
                print(f"[dmcmd] 队列 worker 异常: {e}", flush=True)

    def state(self) -> dict:
        c = self._conf()
        return {"enabled": c["enabled"], "mode": c["mode"], "cooldown_s": c["cooldown_s"],
                "read_interval_s": c["read_interval_s"],
                "same_action_windows": c["same_action_windows"],
                "top": self.top(10), "pending": list(self._prune_pending()),
                "names_loaded": len(self._names),
                # 2026-10-19 新增: 队列/预算可视 + 两个新通道是否开着
                "inbox_depth": len(self._inbox), "inbox_dropped": self._inbox_dropped,
                "t2_skipped": self._t2_skipped,
                "composite": self._composite_cfg(), "prop_wrap_on": bool(self._prop_cfg().get("pattern"))}


router = DanmakuRouter()

# ── 模块级直通(桥以 dm_router.xxx 调用) ──
def init(*args, **kwargs):                           # noqa: ANN002, ANN003
    router.init(*args, **kwargs)


async def start() -> None:
    await router.start()


def enqueue(nick: str, content: str) -> None:
    router.enqueue(nick, content)


def note_manual(text: str) -> None:
    router.note_manual(text)


def looks_like_cmd(content: str) -> bool:
    """零成本判定"这条弹幕是不是动作指令"(播报通道不念动作类弹幕用)。
    ⚠ 2026-10-20 踩过: 只在类里加了方法、忘了这个模块级直通 →
      桥调用时 AttributeError 被 except 吞掉, answer_only 永远是 False。"""
    return router.looks_like_cmd(content)


def count(cmd: str) -> None:
    router.count(cmd)


def state() -> dict:
    return router.state()


def cmd_feed() -> dict:
    """指令流水(舞台页"动作指令"特效): {"last": 最近执行, "recent": 最近 24 条}。"""
    return router.cmd_feed()


def set_config(enabled: bool | None = None, mode: str | None = None) -> dict:
    return router.set_config(enabled, mode)


def confirm(item_id: int) -> bool:
    return router.confirm(item_id)


def dismiss(item_id: int) -> bool:
    return router.dismiss(item_id)

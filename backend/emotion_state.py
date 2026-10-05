"""L0 即时情感信号：毫秒级、每轮必跑、永不失败。

为什么要有这一层（2026-09-28 沙箱实测）：情感分析那一次上游往返要 **27.8~90.8 秒**，
而对话生成本身只有 **4~8 秒** —— 用户 80~95% 的时间花在"看到第一个字之前"。
能做的只有**把这次调用从首字路径上挪走**，而不是把它算快一点。

（2026-09-29 更正：这一层当初的理由里还顺带写了一句"`emotion_analysis_node` 本来就已经
`use_thinking=False`，所以关思考是空想"——那句作废。`llm_config._build_extra_body()` 对
当前生效的 MiMo-V2.6-Flash 返回 `None`（`MODEL_PROFILES` 里没有它），那次调用和
`use_thinking=True` 发的是**同一个 payload**，所以那两组对照测的是同一个请求。
"关思考到底能不能快"要由 `tools/probe_first_token.py` 重新量，不许再拿旧结论当依据。）

设计文档：`docs/情感预热系统设计.md`（三层信号：L0 规则 / L1 基线 / L2 临时对策）。
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from memory_manager import EmotionAnalyzer, EmotionType

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "moz.db")

# 标签→中文：给 prompt 用，别让"stressed"这种英文 key 漏进她嘴里
EMOTION_CN: Dict[str, str] = {
    EmotionType.HAPPY.value: "开心",
    EmotionType.SAD.value: "难过",
    EmotionType.ANXIOUS.value: "焦虑",
    EmotionType.ANGRY.value: "生气",
    EmotionType.NEUTRAL.value: "平静",
    EmotionType.EXCITED.value: "兴奋",
    EmotionType.FEARFUL.value: "害怕",
    EmotionType.GRATEFUL.value: "感恩",
    EmotionType.LONELY.value: "孤独",
    EmotionType.HOPEFUL.value: "有盼头",
    EmotionType.STRESSED.value: "累、压力大",
    EmotionType.RELIEVED.value: "松了口气",
}

# 敏感类目：命中这几类，用户拍板"本轮值得等一次模型"（2026-09-28 决定 1）。
# 门槛必须窄——写宽了就等于每轮都等，P1 白做。
SENSITIVE_CUES: Dict[str, Tuple[str, ...]] = {
    "health": ("医院", "复查", "手术", "住院", "确诊", "癌", "体检报告", "报告出来",
               "化疗", "病假", "挂号"),
    "loss": ("去世", "走了", "过世", "离世", "忌日", "不在了", "没抢救"),
    "conflict": ("吵架", "吵了一架", "分手", "离婚", "被裁", "裁员", "翻脸", "绝交", "冷战"),
    "failure": ("挂科", "落榜", "没过", "被拒", "泡汤", "黄了", "撤了", "未通过", "调剂失败"),
}
# 考试/答辩这类只在"眼看就要发生"时才等：平时提一句考试不该让人多等半分钟
NEAR_MARKERS = ("今天", "明天", "待会", "待会儿", "下午", "上午", "中午", "晚上", "这周", "本周")
EXAM_MARKERS = ("面试", "答辩", "考试", "复试", "截止", "交材料", "开题")

# 主题归类：L2 预热按这个键去找记忆和存对策
TOPIC_HINTS: Dict[str, Tuple[str, ...]] = {
    "健康": ("医院", "复查", "手术", "体检", "吃药", "住院", "病", "牙"),
    "家人": ("妈", "爸", "家里", "老婆", "老公", "孩子", "爷爷", "奶奶", "女儿", "儿子"),
    "工作": ("加班", "老板", "同事", "领导", "开会", "项目", "客户", "上班", "裁员", "续约"),
    "考试": ("面试", "答辩", "考试", "复试", "论文", "开题", "截止"),
    "关系": ("吵架", "分手", "冷战", "朋友", "绝交", "对象", "男朋友", "女朋友"),
    "钱": ("房租", "贷款", "工资", "花完", "缺钱", "报销"),
    "宠物": ("猫", "狗", "宠物", "团子"),
}


def _matched_keywords(text: str) -> List[Tuple[str, str]]:
    """命中的情感词（带所属标签），用来把"她自己说的哪个词"引回给她。"""
    hits = []
    for emotion, keywords in EmotionAnalyzer.EMOTION_KEYWORDS.items():
        for kw in keywords:
            if kw and kw in text:
                hits.append((emotion.value, kw))
    # 规则表里缺的一些口语说法（2026-09-28 实测：「吵了一架，有点堵」被判成平静）
    for kw, value in EXTRA_CUES:
        if kw in text:
            hits.append((value, kw))
    return hits


# 关键词表没覆盖到的口语：吵架、堵得慌、心凉了…
EXTRA_CUES: Tuple[Tuple[str, str], ...] = (
    ("吵架", EmotionType.ANGRY.value), ("吵了一架", EmotionType.ANGRY.value),
    ("吵起来", EmotionType.ANGRY.value), ("气人", EmotionType.ANGRY.value),
    ("堵", EmotionType.SAD.value), ("憋屈", EmotionType.SAD.value),
    ("心凉", EmotionType.SAD.value), ("委屈", EmotionType.SAD.value),
    ("累", EmotionType.STRESSED.value), ("熬", EmotionType.STRESSED.value),
    ("崩了", EmotionType.STRESSED.value), ("烦", EmotionType.ANXIOUS.value),
)

# 敏感类目的中文说法：只给用户看这些，`health`/`conflict` 这种键不许漏进她嘴里
SENSITIVE_CN: Dict[str, str] = {
    "health": "身体和医院这类事",
    "loss": "家里有人走了",
    "conflict": "跟人起了冲突",
    "failure": "落榜、被拒这类",
    "exam": "马上就要面试答辩",
}

# 规则没抓到情绪、但话题本身就带分量的时候给的保底档（省得她用平静语气接忌日）
SENSITIVE_FALLBACK: Dict[str, Tuple[str, float]] = {
    "health": (EmotionType.ANXIOUS.value, 0.5),
    "loss": (EmotionType.SAD.value, 0.6),
    "conflict": (EmotionType.ANGRY.value, 0.5),
    "failure": (EmotionType.SAD.value, 0.55),
    "exam": (EmotionType.ANXIOUS.value, 0.5),
}


def categorize(text: str) -> Dict[str, List[str]]:
    topics = [name for name, cues in TOPIC_HINTS.items() if any(c and c in text for c in cues)]
    sensitive = [name for name, cues in SENSITIVE_CUES.items() if any(c in text for c in cues)]
    if not sensitive and any(m in text for m in EXAM_MARKERS) \
            and any(n in text for n in NEAR_MARKERS):
        sensitive.append("exam")
    return {"topics": topics, "sensitive": sensitive}


def _compose_cn(emotion_value: str, intensity: float, hits: List[Tuple[str, str]],
                asked: bool, sensitive: List[str]) -> str:
    """把规则结果写成一句人话，供对话 prompt 当"现在的心情"。

    绝不能写成"用户当前情感：stressed，强度 0.7"——那是内部标记，
    她照着念出来就像体检报告了（界面口径：话要像人说的）。
    """
    label = EMOTION_CN.get(emotion_value, "平静")
    quotes = []
    for _emo, kw in hits:
        if kw not in quotes:
            quotes.append(kw)
        if len(quotes) >= 3:
            break
    if emotion_value == EmotionType.NEUTRAL.value:
        # 规则没抓到情绪词：宁可说"听不出"，也不要编一个档
        base = "这句话听不出明显情绪，先按平常的语气接"
    else:
        base = f"听着是{label}"
        if quotes:
            base += f"（她自己用了「{'、'.join(quotes)}」）"
        if intensity >= 0.75:
            base += "，而且劲儿挺大"
        elif intensity <= 0.3:
            base += "，但说得很轻"
    if asked:
        base += "；她是在问事，不是在求安慰"
    if sensitive:
        base += "；这事碰不得马虎（" + "、".join(
            SENSITIVE_CN.get(s, s) for s in sensitive) + "），先接住情绪再谈别的"
    return base


def live_signal(text: str) -> Dict[str, Any]:
    """L0：一次纯规则的情感判档，毫秒级、不会失败。"""
    text = (text or "").strip()
    emotion, intensity = EmotionAnalyzer.analyze(text)
    hits = _matched_keywords(text)
    asked = bool(text) and (text[-1] == "?" or ord(text[-1]) == 0xFF1F
                            or text.endswith("吗") or text.endswith("呢"))
    kinds = categorize(text)
    value = emotion.value if hasattr(emotion, "value") else str(emotion)
    intensity = float(intensity)
    # 规则什么都没抓到、但聊的是医院/忌日这类事：按话题给个保底档，
    # 免得她用轻快的语气接一句"我爸走了"
    if value == EmotionType.NEUTRAL.value and kinds["sensitive"]:
        value, floor = SENSITIVE_FALLBACK[kinds["sensitive"][0]]
        intensity = max(intensity, floor)
        # 不给她"编"一句原话引用：她自己没说的词不能写成「她自己用了」
    if asked:
        intensity = max(intensity, 0.5)
    return {
        "current_emotion": value,
        "emotion_intensity": round(intensity, 3),
        "emotion_change": "首次",
        "emotion_summary": _compose_cn(value, intensity, hits, asked, kinds["sensitive"]),
        "topics": kinds["topics"],
        "sensitive": kinds["sensitive"],
        "source": "rule",
    }


def worth_waiting(signal: Dict[str, Any]) -> bool:
    """这一轮值不值得为了情绪多等一次上游（用户 2026-09-28 的决定：敏感主题等）。"""
    return bool(signal.get("sensitive"))


# ── L1 情感基线：从她自己说过的话里攒出"这个人平时什么样" ────────────────
# 用户 2026-09-28 把刷新节奏交给我定，下面这几个数就是我定的，写死在这里而不是散在代码里。
MIN_EVIDENCE = 6          # 依据少于 6 条就宁可不总结：编出来的"性格"比没有更糟
MIN_INTERVAL = 6 * 3600.0  # 两次生成之间至少隔 6 小时，别为聊天窗口里三句话反复重算
MAX_AGE = 7 * 86400.0      # 事实没变但太久没重算，也允许再算一次
DAILY_CAP = 3              # 一天最多算 3 次，不拿额度去赌
LIST_MAX = 6
TEXT_MAX = 40

BASELINE_PROMPT = """你是"相处总结"模块。下面给的是关于这个人的既有记录，
其中第一行【她说这些事时的情绪】是按话题聚合过的情绪档和强度——那是你总结的主要依据。
请据此总结她平时的情绪相处方式，供陪伴型 AI 决定语气。

严格输出 JSON，不要多余文字：
{
  "tone_default": "平时该用什么语气，6~12字，中文",
  "baseline_emotion": "happy/sad/anxious/angry/lonely/stressed/hopeful/neutral 里选一个",
  "trigger_topics": ["容易被什么牵着情绪，最多6个，每个2~8字，用中文话题名"],
  "landmines": ["不能拿来打趣或轻描淡写的，最多6个"],
  "comfort_style": "她吃哪一套安慰方式，10字以内",
  "insufficient": false
}

规则：
- 只许用给到的记录，一条都不许编；看不出来的字段留空字符串或空数组。
- 只有在【她说这些事时的情绪】那一行完全没有内容、且记录里也读不出任何情绪线索时，
  才输出 {"insufficient": true}。有几条情绪档就照着总结，别客气也别夸大。
- 字段值一律写成中文自然语言，不要出现英文键名、不要写"用户"这种第三人称。"""

_ALLOWED_KEYS = ("tone_default", "baseline_emotion", "trigger_topics", "landmines",
                 "comfort_style")


def _clean_text(value: Any, limit: int = TEXT_MAX) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def sanitize_baseline(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """模型给的字段一律消毒：只留白名单、限长、去空；依据不足就返回 None。"""
    if not raw or raw.get("insufficient"):
        return None
    out: Dict[str, Any] = {}
    for key in _ALLOWED_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            items = []
            for item in value:
                text = _clean_text(item, 12)
                if text and text not in items:
                    items.append(text)
            out[key] = items[:LIST_MAX]
        else:
            out[key] = _clean_text(value)
    raw_label = str(out.get("baseline_emotion", "")).strip().lower()
    # 模型可能给英文 key（"stressed"），也可能直接给中文（"焦虑"）；两个都要认
    out["baseline_emotion"] = (EMOTION_CN.get(raw_label)
                               or (raw_label if raw_label in EMOTION_CN.values() else "平静"))
    # 一句实在话都没有 → 当作没总结出来，别留一个空壳让用户以为她懂他
    if not any(out.get(k) for k in ("tone_default", "trigger_topics", "landmines", "comfort_style")):
        return None
    return out


def format_baseline(data: Dict[str, Any]) -> str:
    """把基线写成一行给对话模型看的中文（内部键名一律不许出现在这里）。"""
    if not data:
        return ""
    bits = []
    if data.get("tone_default"):
        bits.append(f"平时用{data['tone_default']}的语气接她")
    if data.get("baseline_emotion"):
        bits.append(f"底色偏{data['baseline_emotion']}")
    if data.get("trigger_topics"):
        bits.append("容易被" + "、".join(data["trigger_topics"]) + "牵着情绪")
    if data.get("landmines"):
        bits.append("别拿来打趣的有" + "、".join(data["landmines"]))
    if data.get("comfort_style"):
        bits.append(f"她吃「{data['comfort_style']}」这一套")
    if not bits:
        return ""
    return "；".join(bits) + "（这是从她说过的事里总结的，说错了她会纠正）"


# ── 首字延迟分段取样（只读指标用，不放任何用户内容）────────────────────
# 口径钉死在这里，免得又量成"只算自己好看的那一段"：
#   first_token   = 后端收到请求 → 第一个字发出去，等于界面上那块秒表
#   relay_ttfb    = 请求打到中转 → 中转吐第一个字（原来唯一在量的那一段）
# 中间每一段各自有名有姓，加起来对不上 first_token 就是漏了埋点。
STAGE_WINDOW = 200
STAGES = ("route_prep", "lock_wait", "local_prep", "sensitive_wait", "retrieval",
          "prompt_build", "relay_ttfb", "generate", "first_token")
_stage_samples: Dict[str, deque] = {}


def record_stage(name: str, seconds: float) -> None:
    if seconds is None or seconds <= 0:
        return
    if name not in STAGES:
        # 不抛（不能在回话路上炸），但也不静默：多出来的名字在指标里看得见
        logger.warning("[分段计时] 不认识的时间段 %s", name)
    _stage_samples.setdefault(name, deque(maxlen=STAGE_WINDOW)).append(float(seconds))


def _pct(ordered: List[float], q: float) -> float:
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def stage_stats(name: str) -> Dict[str, Optional[float]]:
    samples = _stage_samples.get(name)
    if not samples:
        return {"samples": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(samples)
    return {"samples": len(ordered), "p50": _pct(ordered, 0.5), "p95": _pct(ordered, 0.95),
            "max": round(ordered[-1], 2)}


def all_stage_stats() -> Dict[str, Dict[str, Optional[float]]]:
    """每一段都要出现在这里，一条样本都没有也照样列出来——
    漏了埋点的段就该显示 samples=0，而不是从指标里消失。"""
    return {name: stage_stats(name) for name in STAGES}


# ── 规则档 vs 模型档的一致率（P1 的退路判据）───────────────────────────
# 用户把"低到多少就回滚"交给我定：我定的口径是**连续两轮沙箱样本 <0.6 就修规则表，
# 而不是把阻塞调用加回来**（回滚动作见 RUNLOG，P1 提交是 159cd9f）。
_AGREE_WINDOW = 200
_agree_samples: deque = deque(maxlen=_AGREE_WINDOW)


def record_agreement(rule_emotion: str, model_emotion: str) -> bool:
    """记一条样本，并回答"规则档和模型档一样吗"——调用方要用它判断这次等待值不值。"""
    if not (rule_emotion and model_emotion):
        return False
    agreed = rule_emotion == model_emotion
    _agree_samples.append(1.0 if agreed else 0.0)
    return agreed


def agreement_stats() -> Dict[str, Any]:
    if not _agree_samples:
        return {"samples": 0, "rate": None}
    return {"samples": len(_agree_samples),
            "rate": round(sum(_agree_samples) / len(_agree_samples), 3)}


class EmotionStore:
    """情感基线 / 临时对策的存储（moz.db 里的 `emotion_state` 表，与 care_store 同一套连接约定）。

    为什么必须落库：`--reload` 一改 .py 就把进程内状态清零（HANDOFF §1），
    只放内存等于每天重启一次就把"她了解你"忘掉一遍。
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        self._conn().executescript(
            """
            CREATE TABLE IF NOT EXISTS emotion_state (
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                source_watermark REAL NOT NULL DEFAULT 0,
                expires_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.5,
                PRIMARY KEY (user_id, kind, topic)
            );
            CREATE TABLE IF NOT EXISTS emotion_budget (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
            """
        )
        self._conn().commit()

    # ── 读写 ─────────────────────────────────────────────
    def get(self, user_id: str, kind: str, topic: str = "") -> Optional[Dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM emotion_state WHERE user_id=? AND kind=? AND topic=?",
            (user_id, kind, topic),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "data": payload, "revision": int(row["revision"]),
            "source_watermark": float(row["source_watermark"]),
            "expires_at": float(row["expires_at"]), "updated_at": float(row["updated_at"]),
            "confidence": float(row["confidence"]),
        }

    def put(self, user_id: str, kind: str, payload: Dict[str, Any], *, topic: str = "",
            source_watermark: float = 0.0, expires_at: float = 0.0,
            confidence: float = 0.5) -> int:
        prev = self.get(user_id, kind, topic)
        revision = (prev["revision"] + 1) if prev else 1
        self._conn().execute(
            """INSERT OR REPLACE INTO emotion_state
               (user_id, kind, topic, data, revision, source_watermark, expires_at, updated_at, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, kind, topic, json.dumps(payload, ensure_ascii=False), revision,
             source_watermark, expires_at, time.time(), confidence),
        )
        self._conn().commit()
        return revision

    def clear(self, user_id: str, kind: Optional[str] = None) -> int:
        sql = "DELETE FROM emotion_state WHERE user_id=?"
        args: List[Any] = [user_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        cur = self._conn().execute(sql, args)
        self._conn().commit()
        return cur.rowcount

    def list_rows(self, user_id: str, kind: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT topic, data, expires_at, updated_at, confidence FROM emotion_state"
            " WHERE user_id=? AND kind=? ORDER BY updated_at DESC",
            (user_id, kind),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append({"topic": r["topic"], "data": json.loads(r["data"]),
                            "expires_at": float(r["expires_at"]),
                            "updated_at": float(r["updated_at"]),
                            "confidence": float(r["confidence"])})
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    # ── 该不该重算（刷新节奏全在这几个判断里）─────────────────────────
    def used_today(self, user_id: str) -> int:
        day = time.strftime("%Y-%m-%d")
        row = self._conn().execute(
            "SELECT n FROM emotion_budget WHERE user_id=? AND day=?", (user_id, day)
        ).fetchone()
        return int(row["n"]) if row else 0

    def spend(self, user_id: str) -> int:
        """记一次生成。基线每个用户只有一行，所以**不能**拿行数当额度计数。"""
        day = time.strftime("%Y-%m-%d")
        self._conn().execute(
            "INSERT INTO emotion_budget (user_id, day, n) VALUES (?, ?, 1)"
            " ON CONFLICT(user_id, day) DO UPDATE SET n = n + 1",
            (user_id, day),
        )
        self._conn().commit()
        return self.used_today(user_id)

    def needs_baseline(self, user_id: str, watermark: float, evidence: int,
                       now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if evidence < MIN_EVIDENCE:
            return False          # 依据太少就别总结，宁缺毋滥
        if self.used_today(user_id) >= DAILY_CAP:
            return False          # 今天已经算了 3 次，别再拿额度赌
        row = self.get(user_id, "baseline")
        if row is None:
            return True
        age = now - row["updated_at"]
        changed = abs(row["source_watermark"] - watermark) > 1e-6
        if not changed and age < MAX_AGE:
            return False
        return age >= MIN_INTERVAL  # 事实刚动过也要隔够 6 小时再算，别为三句话反复重算

    def prompt_line(self, user_id: str, watermark: Optional[float] = None) -> str:
        """基线那行话。给了当前水位就对一下：事实变了就先别拿旧总结说话。

        红线是"用户删掉的记忆不该还在影响措辞"——TTL 只管 30 分钟，基线能挂 7 天。
        """
        row = self.get(user_id, "baseline")
        if not row:
            return ""
        if watermark is not None and abs(row["source_watermark"] - watermark) > 1e-6:
            bump_counter("stale_skipped")
            return ""
        return format_baseline(row["data"])

    def stats(self, user_id: str) -> Dict[str, Any]:
        base = self.get(user_id, "baseline")
        plans = self.list_rows(user_id, "plan")
        return {
            "baseline_present": bool(base),
            "baseline_age_seconds": round(time.time() - base["updated_at"], 1) if base else None,
            "plans": len(plans),
            "plans_live": sum(1 for p in plans if p["expires_at"] > time.time()),
            "daily_baseline": self.used_today(user_id),
        }


def collect_signals(memory_manager, care_store, profile_manager, user_id: str) -> Tuple[float, int]:
    """(水位, 依据条数)。水位=各来源最大时间戳之和；任何一处变了它就变。

    故意**不要求**每个写入点都记得来通知（那种"必须记得调一下"的坑 HANDOFF §7.1 记过），
    改成每次自己扫一遍取最大值：漏改的可能没有。
    """
    stamps: List[float] = []
    count = 0
    active = active_memories(memory_manager, user_id)
    if memory_manager is not None:
        count += len(active)
        stamps.append(max((getattr(m, "updated_at", 0.0) or getattr(m, "created_at", 0.0)
                           for m in active), default=0.0))
    if care_store is not None:
        try:
            items = care_store.list_items(user_id, status=None)
            count += len(items)
            stamps.append(max((float(i.get("updated_at") or 0.0) for i in items), default=0.0))
        except Exception as e:  # noqa: BLE001
            logger.warning("[情感基线] 读关心事项失败: %s", e)
    if profile_manager is not None:
        try:
            profile = profile_manager.get_profile(user_id) if hasattr(profile_manager, "get_profile") else None
            if isinstance(profile, dict):
                fields = profile.get("fields") or profile
                count += sum(1 for v in fields.values() if v)
                stamps.append(float(profile.get("updated_at") or 0.0))
        except Exception as e:  # noqa: BLE001
            logger.warning("[情感基线] 读档案卡失败: %s", e)
    evidence = count
    watermark = sum(s for s in stamps if s)
    return watermark, evidence


def active_memories(memory_manager, user_id: str) -> List[Any]:
    if memory_manager is None:
        return []
    try:
        return [m for m in memory_manager._get_user_memories(user_id).values() if m.active()]
    except Exception as e:  # noqa: BLE001
        logger.warning("[情感基线] 读记忆失败: %s", e)
        return []


def topic_of(text: str) -> str:
    for name, cues in TOPIC_HINTS.items():
        if any(c and c in text for c in cues):
            return name
    return "其他"


def emotion_tally(memory_manager, user_id: str) -> List[str]:
    """把"她说哪些事时是什么情绪"聚成几行——这才是基线的证据，光列事实推不出脾气。

    （用户 2026-09-28 说的"提前根据用户记忆总结出情感的区块链"，落地就是这个聚合。）
    """
    buckets: Dict[Tuple[str, str], List[float]] = {}
    for m in active_memories(memory_manager, user_id):
        emotion = getattr(getattr(m, "emotion", None), "value", None)
        if not emotion:
            continue
        buckets.setdefault((topic_of(m.content), emotion), []).append(float(m.emotion_intensity or 0.0))
    lines = []
    for (topic, emotion), values in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"{topic}：{EMOTION_CN.get(emotion, emotion)}×{len(values)}（平均强度 "
                     f"{sum(values) / len(values):.2f}）")
    return lines[:12]


def gather_evidence(memory_manager, care_store, user_id: str, limit: int = 40) -> List[str]:
    lines: List[str] = []
    tally = emotion_tally(memory_manager, user_id)
    if tally:
        lines.append("【她说这些事时的情绪】" + "；".join(tally))
    for m in sorted(active_memories(memory_manager, user_id),
                    key=lambda x: x.importance, reverse=True)[:limit]:
        emotion = getattr(getattr(m, "emotion", None), "value", None)
        tag = f"[{EMOTION_CN.get(emotion, emotion)}]" if emotion else ""
        lines.append(f"{tag}{m.content}")
    if care_store is not None:
        try:
            for item in care_store.list_items(user_id, status=None)[:20]:
                title = (item.get("title") or "").strip()
                if title:
                    lines.append(f"[该惦记的事] {title}")
        except Exception as e:  # noqa: BLE001
            logger.warning("[情感基线] 取事项失败: %s", e)
    return [l for l in lines if l][:limit]


def build_baseline(store: EmotionStore, memory_manager, care_store, user_id: str,
                   llm=None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """一次模型调用，把"和她相处下来的感觉"写回基线。依据不足就不写（返回 None）。"""
    now = time.time() if now is None else now
    evidence = gather_evidence(memory_manager, care_store, user_id)
    if len(evidence) < MIN_EVIDENCE:
        logger.info("[情感基线] 依据只有 %d 条，不足 %d 条，这次不总结",
                    len(evidence), MIN_EVIDENCE)
        return None
    if llm is None:
        from llm_config import get_llm_client
        llm = get_llm_client(temperature=0.3, use_thinking=False)
    try:
        response = llm.invoke([SystemMessage(content=BASELINE_PROMPT),
                               HumanMessage(content="既有记录：\n" + "\n".join(evidence))])
        content = (response.content or "").strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        raw = json.loads(content)
    except Exception as e:  # noqa: BLE001 - 中转抽风是常态，别把预热变成故障
        logger.warning("[情感基线] 生成失败（保持上一次基线）: %s", e)
        return None
    clean = sanitize_baseline(raw if isinstance(raw, dict) else {})
    if clean is None:
        logger.info("[情感基线] 模型说依据不足，保持空基线")
        return None
    watermark, _count = collect_signals(memory_manager, care_store, None, user_id)
    store.put(user_id, "baseline", clean, source_watermark=watermark, confidence=0.6)
    used = store.spend(user_id)
    logger.info("[情感基线] 已更新（今天第 %d/%d 次）：%s",
                used, DAILY_CAP, format_baseline(clean)[:80])
    return clean


async def emotion_heartbeat(store: EmotionStore, memory_manager, care_store,
                            profile_manager, user_id: str, interval: int = 60,
                            build=None) -> None:
    """基线心跳：每 60 秒问一句"该重算了吗"，该算才算。

    和 care_loop 一样的取向：代价不摊到每条对话上；生成失败静默，下一轮再试。
    """
    build = build or (lambda: build_baseline(store, memory_manager, care_store, user_id))
    logger.info("[情感基线] 心跳启动，每 %ss 检查一次是否该重算", interval)
    while True:
        try:
            watermark, evidence = collect_signals(memory_manager, care_store,
                                                 profile_manager, user_id)
            if store.needs_baseline(user_id, watermark, evidence):
                await asyncio.to_thread(build)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[情感基线] 这轮检查失败（不影响回话）: %s", e)
        await asyncio.sleep(interval)


# ── L2 临时情感对策：聊到某类事件时，提前在后台把那块的记忆想清楚 ──────────
PLAN_TTL = 30 * 60.0
PLAN_TTL_SENSITIVE = 5 * 60.0        # 医院/忌日这类话题改口快，对策只信 5 分钟以内的
PREWARM_HOURLY_CAP = int(os.environ.get("MOZ_PREWARM_HOURLY_CAP", "6"))

PLAN_PROMPT = """你是"临时对策"模块。她现在聊到了「{topic}」。
下面是关于这件事的既有记录，以及她说这类事时的情绪档。请给出**这一阵**跟她聊这件事的分寸。

严格输出 JSON，不要多余文字：
{{
  "say": "可以先提起的一句，不超过25字，必须落在给到的事实上",
  "avoid": ["这会儿别说什么/别打趣什么，最多3条"],
  "tone": "语气，6字以内，例如 轻一点、别追问",
  "followup": "可以顺着问的一句，不超过20字",
  "insufficient": false
}}

规则：
- 只用给到的记录，不许编造人名、时间、病情。
- 记录里读不出这件具体事的细节就输出 {{"insufficient": true}}。
- 全用中文自然语言，别出现英文键名、别写"用户"。"""

_COUNTERS = {"asked": 0, "hit": 0, "expired_skipped": 0, "dup_skipped": 0,
             "quota_skipped": 0, "built": 0, "failed": 0, "stale_skipped": 0,
             # 敏感主题那 45 秒到底兑没兑现（用户 2026-09-28 拍板要等，但等要有账）
             "sensitive_asked": 0, "sensitive_paid_off": 0, "sensitive_agreed": 0,
             "sensitive_timeout": 0, "sensitive_failed": 0, "sensitive_had_plan": 0,
             # 思考/关思考的参数到底发没发出去（不发出去就等于在用同一个请求做对照）
             "thinking_param_sent": 0, "thinking_param_absent": 0,
             # 整条没回话时"原样再发一次"的账（用户 2026-10-05 拍板：不换嗓子，只多给一次机会）
             "retry_fired": 0, "retry_recovered": 0, "retry_still_silent": 0,
             "retry_first_timeout": 0, "retry_first_error": 0, "retry_first_empty": 0}


def bump_counter(name: str, n: int = 1) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + n


def plan_stats() -> Dict[str, int]:
    """预热那一套的计数（敏感等待、thinking 参数、重试各走各的账，别挤在一个桶里）。"""
    return {k: v for k, v in _COUNTERS.items()
            if not k.startswith(("sensitive_", "thinking_", "retry_"))}


def note_retry(reason: str) -> None:
    """第一枪一个字都没出去 → 准备原样重发。reason: timeout / error / empty。"""
    bump_counter("retry_fired")
    bump_counter(f"retry_first_{reason}")


def note_retry_outcome(recovered: bool) -> None:
    """第二枪的结果。`fired - recovered` = 再发一次也没救回来（含第二枪直接报错的那种）。"""
    bump_counter("retry_recovered" if recovered else "retry_still_silent")


def retry_stats() -> Dict[str, Any]:
    out = {k: v for k, v in _COUNTERS.items() if k.startswith("retry_")}
    fired = out.get("retry_fired", 0)
    out["recovery_rate"] = round(out.get("retry_recovered", 0) / fired, 3) if fired else None
    return out


def wait_stats() -> Dict[str, Any]:
    """敏感主题那一次"值得等"的账。兑现率 = 模型在限内回了、且给的档和规则档不一样；
    等了半天给的还是同一个档，等于白花一次往返。上限动不动，看这个数。"""
    out = {k: v for k, v in _COUNTERS.items() if k.startswith("sensitive_")}
    asked = out.get("sensitive_asked", 0)
    out["payoff_rate"] = round(out.get("sensitive_paid_off", 0) / asked, 3) if asked else None
    return out


def thinking_stats() -> Dict[str, int]:
    return {k: v for k, v in _COUNTERS.items() if k.startswith("thinking_")}



def reset_plan_stats() -> None:
    for key in list(_COUNTERS):
        _COUNTERS[key] = 0


def sanitize_plan(raw: Dict[str, Any], topic: str) -> Optional[Dict[str, Any]]:
    if not raw or raw.get("insufficient"):
        return None
    say = _clean_text(raw.get("say"), 40)
    followup = _clean_text(raw.get("followup"), 34)
    tone = _clean_text(raw.get("tone"), 12)
    avoid = []
    for item in (raw.get("avoid") or [])[:3]:
        text = _clean_text(item, 26)
        # 模型爱写成"别打趣她妈妈养花"，前面再挂一句"这会儿避开"就成了双重否定
        for prefix in ("千万不要", "不要", "不可", "别", "勿"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip("，,、 ")
                break
        if text and text not in avoid:
            avoid.append(text)
    if not (say or followup):
        return None            # 一句可说的话都没有就别留壳
    return {"topic": _clean_text(topic, 12), "say": say, "avoid": avoid,
            "tone": tone, "followup": followup}


def plan_line(data: Dict[str, Any]) -> str:
    bits = []
    if data.get("say"):
        bits.append(f"可以先提：{data['say']}")
    if data.get("followup"):
        bits.append(f"顺着可以问：{data['followup']}")
    if data.get("avoid"):
        bits.append("这会儿避开：" + "、".join(data["avoid"]))
    if data.get("tone"):
        bits.append(f"语气{data['tone']}")
    return "；".join(bits)


def topic_plan(store: EmotionStore, user_id: str, topic: str,
               now: Optional[float] = None,
               watermark: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """取这个主题的对策，**双闸**：过期不许用、主题不对不许用。"""
    now = time.time() if now is None else now
    row = store.get(user_id, "plan", topic)
    if row is None:
        return None
    if row["expires_at"] <= now:
        bump_counter("expired_skipped")          # 拦住了才计数，用来证明闸门真的在闸
        return None
    if watermark is not None and abs(row["source_watermark"] - watermark) > 1e-6:
        bump_counter("stale_skipped")            # 它依据的事实已经变了，这条对策作废
        return None
    return row["data"]


def plan_for(store: EmotionStore, user_id: str, topics: List[str],
             now: Optional[float] = None,
             watermark: Optional[float] = None) -> str:
    """本轮要用的那行对策（读不到就空串，绝不因此耽误开口）。"""
    for topic in topics or []:
        data = topic_plan(store, user_id, topic, now=now, watermark=watermark)
        if data:
            bump_counter("hit")
            return plan_line(data)
    return ""


def gather_topic_evidence(memory_manager, care_store, user_id: str, topic: str) -> List[str]:
    lines: List[str] = []
    for m in active_memories(memory_manager, user_id):
        if topic_of(m.content) == topic or topic in m.content:
            emotion = getattr(getattr(m, "emotion", None), "value", None)
            tag = f"[{EMOTION_CN.get(emotion, emotion)}]" if emotion else ""
            lines.append(f"{tag}{m.content}")
    if care_store is not None:
        try:
            for item in care_store.list_items(user_id, status=None):
                title = (item.get("title") or "").strip()
                if title and (topic in title or topic_of(title) == topic):
                    lines.append(f"[她说过要惦记的] {title}")
        except Exception as e:  # noqa: BLE001
            logger.warning("[情感对策] 读事项失败: %s", e)
    tally = [t for t in emotion_tally(memory_manager, user_id) if t.startswith(f"{topic}：")]
    if tally:
        lines = [f"【她提这类事时的情绪】{'；'.join(tally)}"] + lines
    return lines[:20]


def build_plan(store: EmotionStore, memory_manager, care_store, user_id: str, topic: str,
               sensitive: bool = False, llm=None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """一次模型调用，把"这件事这会儿该怎么接"想清楚存下来。**绝不写进 memories。**"""
    now = time.time() if now is None else now
    evidence = gather_topic_evidence(memory_manager, care_store, user_id, topic)
    if len(evidence) < 2:
        logger.info("[情感对策] 「%s」只有 %d 条依据，这次不猜", topic, len(evidence))
        return None
    if llm is None:
        from llm_config import get_llm_client
        llm = get_llm_client(temperature=0.4, use_thinking=False)
    try:
        response = llm.invoke([
            SystemMessage(content=PLAN_PROMPT.format(topic=topic)),
            HumanMessage(content="现有记录：\n" + "\n".join(evidence)),
        ])
        content = (response.content or "").strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        raw = json.loads(content)
    except Exception as e:  # noqa: BLE001 - 预热失败不该影响任何人
        bump_counter("failed")
        logger.warning("[情感对策] 「%s」生成失败（跳过，不影响回话）: %s", topic, e)
        return None
    plan = sanitize_plan(raw if isinstance(raw, dict) else {}, topic)
    if plan is None:
        return None
    ttl = PLAN_TTL_SENSITIVE if sensitive else PLAN_TTL
    watermark, _ = collect_signals(memory_manager, care_store, None, user_id)
    store.put(user_id, "plan", plan, topic=topic, source_watermark=watermark,
              expires_at=now + ttl, confidence=0.5)
    bump_counter("built")
    logger.info("[情感对策] 「%s」备好，%d 分钟内有效：%s", topic, int(ttl // 60), plan_line(plan)[:70])
    return plan


async def run_prewarm(deps, job: Dict[str, Any]) -> None:
    """单飞 worker 的预热入口（deps 是 SaveDeps，里面已经有记忆/事项/图那几个管理器）。"""
    topic = (job.get("topic") or "").strip()
    if not topic:
        return
    await asyncio.to_thread(
        build_plan, deps.emotion_store, deps.memory_manager, deps.care_store,
        job.get("user_id") or "default", topic,
        bool((job.get("user_message") or "") and categorize(job["user_message"])["sensitive"]),
    )
def schedule_prewarm(store, queue, worker, user_id: str, user_message: str,
                     topics: List[str], cap: Optional[int] = None) -> List[str]:
    """把该预热的主题排进**已有的单飞队列**；返回真的排进去的主题。

    去重（同主题已在路上/已有活的对策）、限流（每小时上限）、以及"绝不新开线程"
    都收在这里——预热是锦上添花，不许把中转打出风暴（设计文档 §7）。
    """
    cap = PREWARM_HOURLY_CAP if cap is None else cap
    queued: List[str] = []
    if queue is None or not topics:
        return queued
    if queue.hourly_count("prewarm") >= cap:
        bump_counter("quota_skipped", len(topics))
        return queued
    for topic in topics[:2]:
        if topic_plan(store, user_id, topic) or queue.queued_for(user_id, "prewarm", topic):
            bump_counter("dup_skipped")
            continue
        queue.enqueue(user_id, user_message, "", kind="prewarm", topic=topic)
        bump_counter("asked")
        queued.append(topic)
    if queued and worker is not None:
        worker.submit()
    return queued

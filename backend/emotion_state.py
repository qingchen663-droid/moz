"""L0 即时情感信号：毫秒级、每轮必跑、永不失败。

为什么要有这一层（2026-09-28 沙箱实测）：情感分析那一次上游往返要 **27.8~90.8 秒**，
而对话生成本身只有 **4~8 秒** —— 用户 80~95% 的时间花在"看到第一个字之前"。
而且 `emotion_analysis_node` 本来就已经 `use_thinking=False`，所以"算得快一点"是空想；
能做的只有**把这次调用从首字路径上挪走**：先用规则定档让她开口，模型那次调用退到后台校正。

设计文档：`docs/情感预热系统设计.md`（三层信号：L0 规则 / L1 基线 / L2 临时对策）。
"""

import time
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

from memory_manager import EmotionAnalyzer, EmotionType

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


# ── 首字延迟取样（只读指标用，不放任何用户内容）────────────────────────
TTFT_WINDOW = 200
_ttft_samples: deque = deque(maxlen=TTFT_WINDOW)


def record_ttft(seconds: float) -> None:
    if seconds and seconds > 0:
        _ttft_samples.append(float(seconds))


def ttft_stats() -> Dict[str, Optional[float]]:
    if not _ttft_samples:
        return {"samples": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(_ttft_samples)

    def pct(q: float) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return round(ordered[idx], 2)

    return {"samples": len(ordered), "p50": pct(0.5), "p95": pct(0.95),
            "max": round(ordered[-1], 2)}

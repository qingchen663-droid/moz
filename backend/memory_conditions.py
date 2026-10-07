"""条件槽：从一句话里确定性抽出「什么时候成立 / 什么时候明确不适用」，并判断是否与当前查询冲突。

借鉴的是"用声明条件做负路由"这个机制：命中`不适用条件`的记忆**不该被当成答案**，
而不是仅按词面相似度参与排序。没有声明条件的记忆不受任何影响（不加分也不减分）。

口径与边界（2026-10-06 修过一轮，如实记着）：
- 只用词面判冲突，不做语义蕴含——换了说法的条件剔不掉，别当它是语义理解；
- 短条件（≤2 字）要求**整串出现在查询里**，不看二元组比例：
  早先用比例时「地方」这种泛指词会把任何含"地方"的查询都判成冲突（误剔）；
- 单字条件（如"辣"）走同一套子串判断——早先按二元组折时单字自成一组，永远命不中；
- 抽取时截断连接词（否则/就/而/所以…），不再吞掉后半句；
- 泛指词表内的条件**不采信**（存了也只会误剔）。
"""
import os
import re

ENABLED = os.environ.get("MOZ_NEG_ROUTING", "1") != "0"
# DEFER：声明了生效条件、而本次查询确认的是**另一条**记忆的条件时，把未确认那条往后放
# （降档，不删除）。确认有三条路：词面对上、问句问的正是这一类条件、同义组桥接。
# 早先两个版本都实测有害：①无条件记忆也当竞争对手 → 干扰池 hit@10 0.923→0.885；
# ②只认词面 → 「他什么时候去锻炼」把 gold 自己降了下去。现在两条都补上，由 D 臂看着。
DEFER_ENABLED = os.environ.get("MOZ_DEFER", "1") != "0"
# 长条件（≥3 字）允许"整串出现"或"二元组重合率达标"两种命中方式
LONG_RATIO = float(os.environ.get("MOZ_NEG_ROUTING_MIN", "0.6"))

# 从句里截断的连接词——条件本身不该把后半句一起吞进来
_STOP = r"否则|不然|就|便|则|才|而|所以|因为|由于|不过|但是|可是|而且|还是|以及|如果|要是"
_PUNCT = r"，|。|；|,|;|\s"
_CUT = f"(?:{_STOP}|{_PUNCT})"

_INVALID_PATTERNS = [
    re.compile(r"除非(?P<cond>.{1,24}?)" + _CUT + "|除非(?P<cond2>.{1,24})$"),
    re.compile(r"(?:不要|不许|不能|别)(?P<cond>.{1,20}?)" + _CUT + "|(?:不要|不许|不能|别)(?P<cond2>.{1,20})$"),
    re.compile(r"(?:忌|避免)(?P<cond>.{1,20}?)" + _CUT),
]
_VALID_PATTERNS = [
    re.compile(r"(?:当|如果|要是|假如|一旦)(?P<cond>.{1,24}?)(?:的时候|时|的情况下|之后|后|就|便|则|才)"
               r"|(?P<cond2>.{1,24}?)的时候"),
]

# 这些词当条件没有区分度，抽到也不采信
_GENERIC = {w.strip() for w in os.environ.get(
    "MOZ_CONDITION_GENERIC",
    "地方,东西,时候,情况,问题,那边,这里,那里,一些,什么,怎么,为什么,有点,比较,可能,应该,反正,而已,这样,那样",
).split(",") if w.strip()}


def _han(text: str) -> str:
    return "".join(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())


def _bigrams(clean: str) -> set:
    if len(clean) >= 2:
        return {clean[i:i + 2] for i in range(len(clean) - 1)}
    return {clean} if clean else set()


def _pick(match: re.Match) -> str:
    for name in ("cond", "cond2"):
        val = match.groupdict().get(name)
        if val:
            return val
    return ""


def _strip_filler(cond: str) -> str:
    """去掉指示词/程度词这类填充成分，剩下的才是条件真正的区分度。"""
    return re.sub(r"那种|这种|那些|那些|那个|这个|一些|些|种|很|比较|有点|去|到|在|的|就|了|要|会|能|是", "", cond)


def _usable(cond: str) -> bool:
    core = _strip_filler(cond)
    return bool(core) and core not in _GENERIC


def derive_condition(content: str) -> tuple:
    """从记忆正文里确定性取条件；取不到、或只是泛指词，就返回空串（宁缺毋滥）。"""
    text = (content or "").strip()
    if not text:
        return "", ""
    for pattern in _INVALID_PATTERNS:
        m = pattern.search(text)
        if m:
            cond = _han(_pick(m)).rstrip("的")
            if cond and _usable(cond):
                return "", cond
    for pattern in _VALID_PATTERNS:
        m = pattern.search(text)
        if m:
            cond = _han(_pick(m)).rstrip("的")
            if cond and _usable(cond):
                return cond, ""
    return "", ""


def trustworthy(cond: str, source_text: str) -> bool:
    """抽取器给出的条件必须能在**用户原话**里找到词面，否则不采信。

    起因是真实中转实测：用户说"除非周末有空，否则我基本不做饭"，模型回填的条件是
    ``when_valid="非周末（没有空）", when_invalid="周末有空"``——**正反说反了**。
    说反的条件比没有条件更坏（会在该用的时候把记忆剔掉），所以照"写入值必须是原文子串"
    这条口径筛：不在原话里的，一律交回正文规则去抽（规则那句抽出来是 不适用=周末有空，对的）。
    """
    if not cond or not source_text:
        return False
    core = _han(cond)
    if not core:
        return False
    return core in _han(source_text)


def prepare_query(query: str):
    """查询侧算一次即可的条件判据：(清洗串, 二元组, 原始问句)。"""
    raw = (query or "").strip()
    clean = _han(raw)
    return clean, _bigrams(clean), raw


def _matches(cond: str, prepared) -> bool:
    """条件与查询是否对得上：短条件要整串出现，长条件允许二元组重合率达标。"""
    q_clean, q_bigrams = prepared[0], prepared[1]
    if not q_clean:
        return False
    cond = _han(cond)
    if not cond or not _usable(cond):
        return False
    if len(cond) <= 2:
        return cond in q_clean
    if cond in q_clean:
        return True
    cond_bigrams = _bigrams(cond)
    if not cond_bigrams:
        return False
    return (len(cond_bigrams & q_bigrams) / len(cond_bigrams)) >= LONG_RATIO


def conflicts_with_query(when_invalid: str, prepared) -> bool:
    """`不适用条件`与当前查询词面冲突 → 这条不该当答案。"""
    if not ENABLED or not when_invalid or not prepared:
        return False
    return _matches(when_invalid, prepared)


_FACET_MARKERS = {
    "time": ("周", "星期", "点", "晚上", "早上", "上午", "下午", "中午", "凌晨",
             "天", "月", "号", "日", "周末", "工作日", "每", "早起", "晚睡", "季节", "时候"),
    "place": ("家", "办公室", "公司", "学校", "这里", "那里", "外地", "出差", "路上",
              "健身房", "厨房", "车里", "老家", "本地"),
    "person": ("妈妈", "爸爸", "朋友", "同事", "老板", "客户", "孩子", "老婆", "老公", "室友"),
    "state": ("累", "忙", "有空", "心情", "难受", "不舒服", "疼", "感冒", "喝过酒", "加班"),
}

# 只对**明确的**类别疑问词让步；"什么"这种泛问不算（否则 DEFER 永远不生效，等于没做）
_FACET_QUESTIONS = {
    "time": ("什么时候", "何时", "哪天", "哪一天", "几号", "周几", "星期几", "什么时间",
             "啥时候", "几点", "哪晚", "哪些天", "哪个周末", "平时", "通常"),
    "place": ("哪里", "哪儿", "什么地方", "在哪", "在哪儿", "从哪"),
    "person": ("谁", "跟谁", "和谁", "哪个人"),
    "state": ("什么情况下", "什么情况", "什么时候会", "什么状态下"),
}


def _facets_of(cond: str) -> set:
    core = _han(cond)
    return {facet for facet, marks in _FACET_MARKERS.items()
            if any(m in core for m in marks)}


def _asked_about(cond: str, prepared) -> bool:
    """问句问的正是这一类条件（"他什么时候去锻炼" ↔ 生效条件"周五晚上"）→ 视为已确认。

    这类查询里那条带条件的记忆**就是答案**，降档等于把答案藏起来，是纯伤害。
    """
    raw = prepared[2] if len(prepared) > 2 else ""
    if not raw:
        return False
    facets = _facets_of(cond)
    return any(q in raw for facet in facets for q in _FACET_QUESTIONS.get(facet, ()))


def _synonym_match(cond: str, prepared) -> bool:
    """条件里的说法与查询里的说法在同一同义组（周五↔礼拜五、锻炼↔健身）也算确认。"""
    q_bigrams = prepared[1]
    c_bigrams = _bigrams(_han(cond))
    if not c_bigrams or not q_bigrams:
        return False
    from memory_synonyms import expand
    return bool(set(expand(c_bigrams)) & q_bigrams)


_PERIOD_BUCKETS = (
    ("清早", "早晨", "早上", "上午"),
    ("中午", "午间"),
    ("下午", "傍晚", "黄昏", "晚间", "晚上", "夜里", "半夜", "深夜"),
)
_DAY_CLASSES = {
    "工作日": ("工作日", "上班日", "平日", "周内"),
    "周末": ("周末", "双休", "休息日", "周六", "周日", "星期六", "星期日", "礼拜六", "礼拜天"),
    "周一": ("周一", "星期一", "礼拜一"),
    "周二": ("周二", "星期二", "礼拜二"),
    "周三": ("周三", "星期三", "礼拜三"),
    "周四": ("周四", "星期四", "礼拜四"),
    "周五": ("周五", "星期五", "礼拜五"),
}


def _day_class(text: str):
    for cls, marks in _DAY_CLASSES.items():
        if any(m in text for m in marks):
            return cls
    return None


def _period_bucket(text: str):
    for i, marks in enumerate(_PERIOD_BUCKETS):
        if any(m in text for m in marks):
            return i
    return None


def _time_agrees(cond: str, raw_query: str) -> bool:
    """把两边的时间说法归一再比：工作日≈工作日、傍晚与晚上同属一个时段桶。

    条件里的时间是"工作日晚上"，问句说"工作日傍晚"——词面不同但情境相同，
    这种必须算确认，否则 DEFER 会把最该用的那条压下去（实测就是这样掉过 hit@10）。
    两边都没给时间线索时返回 False：不能凭空替记忆确认条件。
    """
    if not raw_query:
        return False
    c_day, q_day = _day_class(cond), _day_class(raw_query)
    c_bucket, q_bucket = _period_bucket(cond), _period_bucket(raw_query)
    if c_day is None and c_bucket is None:
        return False                      # 条件根本不是时间，交给别的途径判
    if c_day and q_day and c_day != q_day:
        return False
    if c_bucket is not None and q_bucket is not None and c_bucket != q_bucket:
        return False
    if q_day is None and q_bucket is None:
        return False
    return bool((c_day and q_day == c_day) or (c_bucket is not None and q_bucket == c_bucket))


def condition_confirmed(when_valid: str, prepared) -> bool:
    """`生效条件`有没有被本次查询确认。没声明条件 = 视为已确认（不牵连旧记忆）。

    三种确认途径：词面直接对上、问句问的是这一类条件、同义组桥接。
    都不成立才算未确认（降档，不删除）。
    """
    if not when_valid:
        return True
    if not prepared:
        return True
    if _matches(when_valid, prepared):
        return True
    if _asked_about(when_valid, prepared):
        return True
    if _time_agrees(when_valid, prepared[2] if len(prepared) > 2 else ""):
        return True
    return _synonym_match(when_valid, prepared)

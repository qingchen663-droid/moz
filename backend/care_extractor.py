"""从对话里自动抽取"以后该主动惦记的事"，写进关心数据库。

用户不该为陪伴类产品填表：聊到了就自己记下来。
主路径交给模型（它能把"下周三""6月3号""高考后两周"都折成日期），
模型不可用时回落到 TemporalExtractor 的相对日期解析，宁缺毋滥。
"""

import datetime as dt
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from care_store import CareStore

logger = logging.getLogger(__name__)

KINDS = {"birthday", "event", "promise", "checkin", "health", "person", "note"}
REPEATS = {"none", "daily", "weekly", "yearly"}
LOCATION_TITLE = "所在城市"

# 兜底规则：只有出现这些强信号词才记，避免把闲聊当成长期事项
TRIGGER_WORDS = ("生日", "过生", "纪念日", "面试", "考试", "答辩", "汇报", "复诊", "体检",
                 "手术", "搬家", "截止", "deadline", "约定", "周三", "周四", "周五", "下周",
                 "后天", "明天", "月初", "月底")

# 中文写法常带空格："10 月 5 日"、"8月2号"、"2026 年 3 月 15 日"
_CN_DATE = re.compile(r"(?:(\d{2,4})\s*[年\-/])?\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*[日号]?")

# 一句话里塞两件事是常态（"答辩下周三，之后一周出结果"），整句只取前半句会把链掐掉
MAX_CLAUSES = 3
_CLAUSE_SPLIT = re.compile(r"[，。,.；;！!？?\n]+")

# "N 天后"式相对偏移：TemporalExtractor 只认"下周/明天"这类词表，算不出"一周后/半个月后"
_UNIT_DAYS = {"天": 1.0, "日": 1.0, "周": 7.0, "星期": 7.0, "个月": 30.0, "月": 30.0, "年": 365.0}
_CN_NUM = {"半": 0.5, "一": 1.0, "两": 2.0, "二": 2.0, "三": 3.0, "四": 4.0, "五": 5.0,
           "六": 6.0, "七": 7.0, "八": 8.0, "九": 9.0, "十": 10.0}
_CHAIN = re.compile(
    r"([一-鿿A-Za-z0-9]{2,12}?)之?后\s*"
    r"(半|[一二两三四五六七八九十]|\d+(?:\.\d+)?)\s*(天|日|星期|周|个月|月|年)"
    r"([一-鿿A-Za-z0-9]{0,12})")

PROMPT = """你在帮一个 AI 朋友挑出"以后该主动惦记的事"。今天是 {today}（{weekday}）。

只挑这几类：生日/纪念日、明确的约定或截止时间、复诊体检、正在推进的事（面试/考试/答辩/项目/搬家）、重要的人。
不要挑：情绪、观点、闲聊、已经过去的旧事、一次性的小确幸。

严格输出 JSON 数组，不要任何解释文字，每项字段：
  "kind": birthday|event|promise|checkin|health|person|note
  "title": 不超过 12 个字的短标题，写事本身（如"妈妈生日""项目答辩"）
  "due_date": "YYYY-MM-DD"；生日/纪念日只说了月日时用 "MM-DD"；说不清就给 null
  "repeat": "none" 或 "yearly"（生日、纪念日一律 yearly）
  "after": 只有用户明说了先后关系时才填，写【先发生】那件事的 title（可以是本数组里的、也可以是用户以前提过的）；没有就 null
  "after_days": 从 after 那件事到这件事大约隔几天，整数；说不清就 null
  "why": 一句话说明依据

after 的方向最容易搞反：填反了 moz 就会在答辩当天追问结果。拿不准就给 null，别猜。
若用户提到自己所在的城市，额外给一项 {{"kind":"note","title":"{loc}","detail":"省|市"}}，例如 "浙江|杭州"。
没有可记的就只输出 []。"""

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", (title or "").strip())[:24]


def _to_timestamp(raw: Any, yearly: bool) -> float:
    """把 YYYY-MM-DD / MM-DD 折成本地时间戳；生日只给月日时取最近一次未到的那天。"""
    text = str(raw or "").strip()
    if not text or text.lower() in ("null", "none"):
        return 0.0
    now = dt.datetime.now()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if m:
        try:
            d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 9, 0)
        except ValueError:
            return 0.0
        return d.timestamp()
    m = re.match(r"^(?:\d{4}-)?(\d{1,2})-(\d{1,2})$", text)
    if m and yearly:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = dt.datetime(now.year, month, day, 9, 0)
        except ValueError:
            return 0.0
        if candidate.timestamp() < now.timestamp() - 86400:
            try:
                candidate = dt.datetime(now.year + 1, month, day, 9, 0)
            except ValueError:
                return 0.0
        return candidate.timestamp()
    return 0.0


def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    """模型常把 JSON 包在 ```json 里或前后带话，取第一对方括号。"""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _split_clauses(text: str) -> List[str]:
    return [p.strip() for p in _CLAUSE_SPLIT.split(text or "") if p.strip()][:MAX_CLAUSES]


def _item_from_clause(clause: str) -> Optional[Dict[str, Any]]:
    """单个分句 → 一条事项；认不出可靠日期就返回 None（宁缺毋滥）。"""
    m = _CN_DATE.search(clause)
    if not m:
        try:
            from temporal_metadata import TemporalExtractor
            temporal = TemporalExtractor.extract_from_text(clause)
        except Exception:
            return None
        ts = (temporal.event_time or {}).get("timestamp")
        # 解析出来的时间戳原来被丢成 due_date=None，于是"下周三面试"记成了一条没日期的事：
        # 永远不会到点提醒，也当不了先后链的起点。过去的说法也不该再来烦用户。
        if not ts or ts <= time.time():
            return None
        # 和上面绝对日期那条路一样：说法已经折进 due_at 了，别再留在标题里
        title = re.sub(r"[的下在是]+$", "",
                       _normalize_title(TemporalExtractor.strip_phrases(clause)))
        if not title:
            return None
        return [{"kind": "event", "title": title, "due_date": None,
                 "due_at": ts, "repeat": "none",
                 "why": f"规则兜底：{temporal.event_time.get('description', '')}提到过"}]

    year_s, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    yearly = bool(re.search(r"生日|过生|纪念日|忌日", clause))
    # 标题只取日期以外的部分，否则"我妈生日是10月5日，我下周三面试"会留一长条
    title = re.sub(r"[的下在是]+$", "", _normalize_title(clause.replace(m.group(0), " ")))
    if not title:
        return None
    try:
        dt.datetime(2024, month, day)  # 先把"2 月 30 日"这种挡掉
        if yearly:
            due = f"{month:02d}-{day:02d}"
        else:
            now = dt.datetime.now()
            year = int(year_s) if year_s else now.year
            cand = dt.datetime(year, month, day, 9, 0)
            if not year_s and cand.timestamp() < now.timestamp():
                cand = dt.datetime(year + 1, month, day, 9, 0)
            due = f"{cand.year}-{month:02d}-{day:02d}"
    except ValueError:
        return None
    return [{"kind": "birthday" if yearly else "event", "title": title, "due_date": due,
             "repeat": "yearly" if yearly else "none",
             "why": f"规则兜底：句子里有具体日期 {m.group(0).strip()}"}]


def _fallback_items(user_msg: str) -> List[Dict[str, Any]]:
    """模型不可用时的保守兜底：强信号词 + 可靠日期。

    以前只认"下周/明天"这类相对说法，结果"我妈生日是 10 月 5 日"这种最该记住的
    一句话什么都没记下——而中转挂了时走的就是这条路。
    按分句扫：一句话里塞两件事是常态，整句只取前半句会漏掉后半件、也建不出先后链。
    """
    out: List[Dict[str, Any]] = []
    for clause in _split_clauses(user_msg):
        if not any(w in clause for w in TRIGGER_WORDS):
            continue
        out.extend(_item_from_clause(clause) or [])
    return out


def _days_from(num: str, unit: str) -> float:
    value = _CN_NUM.get(num)
    if value is None:
        try:
            value = float(num)
        except ValueError:
            return 0.0
    return value * _UNIT_DAYS.get(unit, 0.0)


def _rule_chains(text: str) -> List[Tuple[str, float, str]]:
    """从"答辩之后一周出结果"这种说法里取出 (前件文本, 隔几天, 后件标题)。

    只认这一个句式："锚点 + 后 + 数量 + 单位"。"出结果再说续约"这种没天数的
    说法一律放过——猜错天数时 moz 会当用户面说错话，比不追问糟得多。
    """
    out: List[Tuple[str, float, str]] = []
    for m in _CHAIN.finditer(text or ""):
        days = _days_from(m.group(2), m.group(3))
        if not days:
            continue
        out.append((m.group(1), days, _normalize_title(m.group(4))))
    return out


def _match_anchor(anchor: str, titles: Dict[str, str], skip: str = "") -> Optional[str]:
    """把"答辩之后一周"里的「答辩」对到一条已记事项。

    认不出、或者能对上两条以上，就返回 None：模糊匹配在这里不是聪明，是危险。
    """
    key = _normalize_title(anchor)
    if len(key) < 2:
        return None
    hits = {item_id for title, item_id in titles.items()
            if item_id and item_id != skip and len(title) >= 2 and (title in key or key in title)}
    return hits.pop() if len(hits) == 1 else None


def extract(user_msg: str, assistant_msg: str, llm_client) -> List[Dict[str, Any]]:
    now = dt.datetime.now()
    prompt = PROMPT.format(today=now.strftime("%Y-%m-%d"), weekday=_WEEKDAYS[now.weekday()],
                           loc=LOCATION_TITLE)
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        reply = llm_client.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=f"用户说：{user_msg}\n你回了：{(assistant_msg or '')[:200]}"),
        ])
        raw = (getattr(reply, "content", "") or "").strip()
    except Exception as e:
        logger.warning("[关心抽取] 模型调用失败，走兜底: %s", type(e).__name__)
        raw = ""
    items = _parse_json_array(raw) if raw else []
    return items or _fallback_items(user_msg)


def _apply_location(store: CareStore, user_id: str, detail: str) -> bool:
    parts = [p.strip() for p in re.split(r"[|/，,]", detail or "") if p.strip()]
    if not parts:
        return False
    province, city = (parts[0], parts[1]) if len(parts) >= 2 else ("", parts[0])
    current = store.get_settings(user_id)
    if current.get("city"):
        return False
    store.save_settings(user_id, {"province": province[:20], "city": city[:20]})
    logger.info("[关心抽取] 自动填上所在城市：%s %s", province, city)
    return True


def _offset_days(raw_days: Any, before_row: Dict[str, Any], after_row: Dict[str, Any]) -> float:
    """隔几天：模型给的天数优先，其次用两个已算好的日期相减。

    模型算"日期差"比算"一周后 = 7 天"可靠得多，所以后者只作为兜底的兜底。
    """
    try:
        days = float(raw_days)
    except (TypeError, ValueError):
        days = 0.0
    if days > 0:
        return days  # 上限交给 add_chain 把关，这里不重复定规矩
    first = float((before_row or {}).get("due_at") or 0)
    later = float((after_row or {}).get("due_at") or 0)
    if first and later and later > first:
        return (later - first) / 86400.0
    return 0.0


def _apply_chains(store: CareStore, graph, user_id: str, text: str,
                  titles: Dict[str, Dict[str, Any]],
                  recorded: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> List[str]:
    """把"先 A 后 B"落成有方向的边。没传 graph 就什么都不做（老调用方行为不变）。

    两条来源：模型给的 after/after_days（source=llm），和规则句式"答辩之后一周出结果"
    （source=rule）。锚点认不出、对上两条以上、前件没日期、天数算不出来 ——
    任一条成立就放弃，不硬连。
    """
    if graph is None:
        return []
    index = {title: (row or {}).get("id", "") for title, row in titles.items()}
    made: List[str] = []

    def link(after_id: str, before_id: str, days: float, source: str, after_title: str) -> None:
        before_title = (store.get_item(before_id) or {}).get("title", "")
        if graph.add_chain(user_id, after_id, before_id, days=days, source=source,
                           before_title=before_title):
            made.append(f"{after_title}（接在{before_title}之后{days:g}天）")

    for item, raw in recorded:
        want = _normalize_title(str(raw.get("after") or ""))
        after_id = (item or {}).get("id", "")
        if not want or not after_id:
            continue
        before_id = _match_anchor(want, index, skip=after_id)
        if not before_id:
            continue
        days = _offset_days(raw.get("after_days"), store.get_item(before_id) or {}, item)
        if days:
            link(after_id, before_id, days, "llm", item.get("title", ""))

    for anchor, days, tail_title in _rule_chains(text):
        before_id = _match_anchor(anchor, index)
        if not before_id:
            continue
        before_row = store.get_item(before_id) or {}
        first = float(before_row.get("due_at") or 0)
        if not first:
            continue  # 前件本身没日期，"之后一周"就没有起点
        after_id = _match_anchor(tail_title, index, skip=before_id) if tail_title else None
        if not after_id:
            if not tail_title:
                continue  # 叫不出后件名字就别造一条匿名事项
            item = store.add_item(user_id, title=tail_title, kind="event",
                                  due_at=first + days * 86400, source="auto")
            titles[tail_title] = item
            index[tail_title] = item["id"]
            after_id, after_title = item["id"], item["title"]
        else:
            after_title = (store.get_item(after_id) or {}).get("title", tail_title)
        link(after_id, before_id, days, "rule", after_title)
    return made


def harvest(store: CareStore, user_id: str, user_msg: str, assistant_msg: str,
            llm_client=None, graph=None) -> List[str]:
    """抽取 + 去重 + 落库 + 顺带记先后关系，返回新增/更新的标题列表。"""
    text = (user_msg or "").strip()
    if not text or len(text) < 4:
        return []
    if llm_client is None:
        items = _fallback_items(text)
    else:
        items = extract(text, assistant_msg, llm_client)
    if not items:
        return []

    existing = {_normalize_title(it["title"]): it for it in store.list_items(user_id)}
    touched: List[str] = []
    recorded: List[Dict[str, Any]] = []
    for raw in items:
        title = _normalize_title(raw.get("title", ""))
        if not title:
            continue
        if title == LOCATION_TITLE or raw.get("kind") == "note" and LOCATION_TITLE in (raw.get("detail") or ""):
            _apply_location(store, user_id, raw.get("detail", ""))
            continue
        kind = raw.get("kind") if raw.get("kind") in KINDS else "event"
        repeat = raw.get("repeat") if raw.get("repeat") in REPEATS else "none"
        due_at = _to_timestamp(raw.get("due_date"), repeat == "yearly") or float(raw.get("due_at") or 0)
        if repeat == "yearly" and not due_at:
            continue  # 生日没日期就没法主动祝福，别存成噪音

        dup = existing.get(title)
        if dup:
            # 同一件事再提一次：只补更精确的信息，不新增重复条目
            patch: Dict[str, Any] = {}
            if due_at and (not dup["due_at"] or abs(dup["due_at"] - due_at) > 3600):
                patch["due_at"] = due_at
            if repeat != "none" and dup["repeat"] == "none":
                patch["repeat"] = repeat
            if patch:
                updated = store.update_item(user_id, dup["id"], patch)
                existing[title] = updated or {**dup, **patch}
                touched.append(f"{title}(更新)")
            recorded.append((existing.get(title) or raw, raw))
            continue

        item = store.add_item(user_id, title=title, kind=kind, due_at=due_at,
                              repeat=repeat, source="auto")
        existing[title] = item
        recorded.append((item, raw))
        touched.append(title)
        logger.info("[关心抽取] 记下 %s（%s, %s）", title, kind,
                    dt.datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M") if due_at else "未定时")

    chains = _apply_chains(store, graph, user_id, text, existing, recorded)
    return touched + [f"[链] {c}" for c in chains]

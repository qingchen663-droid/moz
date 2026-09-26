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

PROMPT = """你在帮一个 AI 朋友挑出"以后该主动惦记的事"。今天是 {today}（{weekday}）。

只挑这几类：生日/纪念日、明确的约定或截止时间、复诊体检、正在推进的事（面试/考试/答辩/项目/搬家）、重要的人。
不要挑：情绪、观点、闲聊、已经过去的旧事、一次性的小确幸。

严格输出 JSON 数组，不要任何解释文字，每项字段：
  "kind": birthday|event|promise|checkin|health|person|note
  "title": 不超过 12 个字的短标题，写事本身（如"妈妈生日""项目答辩"）
  "due_date": "YYYY-MM-DD"；生日/纪念日只说了月日时用 "MM-DD"；说不清就给 null
  "repeat": "none" 或 "yearly"（生日、纪念日一律 yearly）
  "why": 一句话说明依据

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


def _fallback_items(user_msg: str) -> List[Dict[str, Any]]:
    """模型不可用时的保守兜底：强信号词 + 相对日期解析。"""
    if not user_msg or not any(w in user_msg for w in TRIGGER_WORDS):
        return []
    try:
        from temporal_metadata import TemporalExtractor
        temporal = TemporalExtractor.extract_from_text(user_msg)
    except Exception:
        return []
    ts = (temporal.event_time or {}).get("timestamp")
    if not ts:
        return []
    title = _normalize_title(re.split(r"[，。,.!！?？]", user_msg.strip())[0])
    if not title:
        return []
    return [{"kind": "event", "title": title, "due_date": None, "repeat": "none",
             "why": f"规则兜底：{temporal.event_time.get('description', '')}提到过"}]


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


def harvest(store: CareStore, user_id: str, user_msg: str, assistant_msg: str,
            llm_client=None) -> List[str]:
    """抽取 + 去重 + 落库，返回新增/更新的标题列表。"""
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
    for raw in items:
        title = _normalize_title(raw.get("title", ""))
        if not title:
            continue
        if title == LOCATION_TITLE or raw.get("kind") == "note" and LOCATION_TITLE in (raw.get("detail") or ""):
            _apply_location(store, user_id, raw.get("detail", ""))
            continue
        kind = raw.get("kind") if raw.get("kind") in KINDS else "event"
        repeat = raw.get("repeat") if raw.get("repeat") in REPEATS else "none"
        due_at = _to_timestamp(raw.get("due_date"), repeat == "yearly")
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
                store.update_item(user_id, dup["id"], patch)
                touched.append(f"{title}(更新)")
            continue

        store.add_item(user_id, title=title, kind=kind, due_at=due_at, repeat=repeat, source="auto")
        existing[title] = {"id": "", "due_at": due_at, "repeat": repeat}
        touched.append(title)
        logger.info("[关心抽取] 记下 %s（%s, %s）", title, kind,
                    dt.datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M") if due_at else "未定时")
    return touched

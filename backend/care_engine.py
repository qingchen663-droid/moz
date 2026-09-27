"""主动关心引擎：定时判断"现在该不该主动说句话"，把要说的话放进待读队列。

设计要点：
- 调度只在这里做，托盘和前端都只负责取走队列，避免多处计时逻辑打架。
- 每条主动消息都要过三道闸：总开关、安静时段、当日配额 + 最小间隔。
- 措辞优先交给模型润色；模型不可用时回落到模板，绝不因为调不通就不说话。
"""

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from llm_config import get_llm_client
from weather import get_weather
from care_graph import ITEM as NODE_ITEM

logger = logging.getLogger(__name__)

MIN_GAP_SECONDS = 90 * 60      # 两条主动消息之间至少隔多久
USER_PRESENT_SECONDS = 20 * 60  # 用户刚聊过就别插话
EVENT_LOOKBACK = 6 * 3600       # 过期超过 6 小时就不翻旧账
RAIN_WINDOW = (7, 10)           # 带伞提醒只在早上这个时段说
BIRTHDAY_AFTER_HOUR = 9
MAX_PROACTIVE_PER_DAY = 8       # 硬上限：事项堆一起了也不能当通知轰炸机

# 没来由地搭话：吃"话多话少"换算出来的当日名额，受「平时主动找我说话」开关管
CHAT_KINDS = ("open_loop", "miss_you")

_last_user_seen: Dict[str, float] = {}
_last_bot_seen: Dict[str, float] = {}


def note_user_activity(user_id: str) -> float:
    """由对话接口调用：记录"用户刚说话"，并返回距上次说话过了多久（秒）。"""
    prev = _last_user_seen.get(user_id, 0)
    now = time.time()
    _last_user_seen[user_id] = now
    return now - prev if prev else 0.0


def note_bot_reply(user_id: str) -> None:
    """moz 回完一句（包括主动关心说出去的）：用来判断用户接话快不快。"""
    _last_bot_seen[user_id] = time.time()


def seconds_since_bot_reply(user_id: str) -> float:
    """0 表示后端刚重启、没有参照。"""
    prev = _last_bot_seen.get(user_id, 0)
    return time.time() - prev if prev else 0.0


def _day_start(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def _same_day(a: float, b: float) -> bool:
    if not a:
        return False
    return dt.datetime.fromtimestamp(a).date() == dt.datetime.fromtimestamp(b).date()


def _parse_hhmm(text: str, default: tuple) -> tuple:
    try:
        h, m = str(text).split(":")
        return (int(h) % 24, int(m) % 60)
    except (ValueError, AttributeError):
        return default


def in_quiet_hours(settings: Dict[str, Any], now: float) -> bool:
    """安静时段；支持跨零点（如 23:00~08:00）。"""
    sh, sm = _parse_hhmm(settings.get("quiet_start"), (23, 0))
    eh, em = _parse_hhmm(settings.get("quiet_end"), (8, 0))
    cur = dt.datetime.fromtimestamp(now)
    minutes = cur.hour * 60 + cur.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def _idle_threshold(score: float) -> float:
    """越爱聊（score 高），能容忍的空档越短。"""
    return max(3.0, 12.0 - 8.0 * float(score or 0.5))


def collect(store, wm_store, user_id: str, now: Optional[float] = None,
            graph=None) -> List[Dict[str, Any]]:
    """列出此刻够格主动开口的理由，按优先级排好。

    两个开关各管一段：「到点提醒」管记下的事，「主动找我说话」管问候和追话头，
    互不牵连；带伞提醒有自己的勾，不受这两个影响。
    传了 graph 就给每条事项附上"和它连着线的旧事"，措辞时才有"记得"而不是"通知"的感觉。
    """
    now = now or time.time()
    cur = dt.datetime.fromtimestamp(now)
    settings = store.get_settings(user_id)
    remind_on = bool(settings.get("remind_events", True))
    chat_on = bool(settings.get("initiate_chat", True))
    out: List[Dict[str, Any]] = []

    if remind_on:
        for it in store.list_items(user_id):
            title = it["title"]
            kind = it["kind"]
            repeat = it["repeat"]
            fired = it["last_fired_at"] or 0

            if repeat == "yearly":
                if not it["due_at"]:
                    continue
                d = dt.datetime.fromtimestamp(it["due_at"])
                if (d.month, d.day) != (cur.month, cur.day) or cur.hour < BIRTHDAY_AFTER_HOUR:
                    continue
                if _same_day(fired, now):
                    continue
                out.append({"priority": 0, "kind": kind, "ref_id": it["id"], "title": title,
                            "why": f"今天是「{title}」（每年重复），该主动提起了"})

            elif repeat == "daily":
                due_h = dt.datetime.fromtimestamp(it["due_at"]).hour if it["due_at"] else 9
                if cur.hour < due_h or _same_day(fired, now):
                    continue
                out.append({"priority": 2, "kind": kind, "ref_id": it["id"], "title": title,
                            "why": f"每天的事「{title}」到点了"})

            elif repeat == "weekly":
                if not it["due_at"] or now < it["due_at"]:
                    continue
                if fired and now - fired < 7 * 86400:
                    continue
                out.append({"priority": 2, "kind": kind, "ref_id": it["id"], "title": title,
                            "why": f"每周的事「{title}」又到期了"})

            else:  # 一次性
                if not it["due_at"] or now < it["due_at"] or now - it["due_at"] > EVENT_LOOKBACK:
                    continue
                if fired and fired >= it["due_at"]:
                    continue
                soon = kind in ("promise", "checkin", "health")
                out.append({"priority": 1, "kind": kind, "ref_id": it["id"], "title": title,
                            "why": f"「{title}」就安排在今天，{'该问问进展' if soon else '该提醒一声'}"})

    if settings.get("rain_reminder") and settings.get("city"):
        if RAIN_WINDOW[0] <= cur.hour < RAIN_WINDOW[1] and store.fired_since(user_id, _day_start(now), "rain") == 0:
            w = get_weather(settings.get("province", ""), settings["city"])
            if w and w.get("rain_today"):
                out.append({"priority": 2, "kind": "rain", "ref_id": "", "title": settings["city"],
                            "why": f"{settings['city']}今天有雨（{w['today'].get('day')}/{w['today'].get('night')}，"
                                   f"{w['degree']}°，湿度{w['humidity']}）"})

    # 搭话类才吃"话多话少"的当日名额；到点提醒不该被闲聊名额挤掉
    budget = store.daily_budget(user_id) if chat_on else 0
    chat_used = sum(store.fired_since(user_id, _day_start(now), k) for k in CHAT_KINDS)
    if chat_used < budget:
        followup = ""
        if wm_store:
            try:
                followup = wm_store.get_followup_text(user_id)
            except Exception as e:  # 开放话题坏了不该拖垮整个引擎
                logger.warning("[主动关心] 读取开放话题失败: %s", e)
        if followup and store.fired_since(user_id, _day_start(now), "open_loop") == 0:
            out.append({"priority": 3, "kind": "open_loop", "ref_id": "", "title": followup,
                        "why": f"用户之前留了个没说完的话头：{followup}"})

        if store.fired_since(user_id, _day_start(now), "miss_you") == 0:
            idle_hours = (now - _last_user_seen.get(user_id, 0)) / 3600 if _last_user_seen.get(user_id) else None
            if idle_hours is None or idle_hours >= _idle_threshold(settings.get("talk_score", 0.5)):
                out.append({"priority": 4, "kind": "miss_you", "ref_id": "", "title": "",
                            "why": f"已经大约{int(idle_hours or 0)}小时没聊了，主动问候一句"})

    if graph is not None:
        for cand in out:
            if not cand.get("ref_id"):
                continue
            try:
                cand["context"] = graph.context_lines(user_id, NODE_ITEM, cand["ref_id"])
            except Exception as e:  # 关联是加分项，坏了也不能不说话
                logger.warning("[主动关心] 取关联上下文失败: %s", e)
                cand["context"] = []

    return sorted(out, key=lambda c: c["priority"])


def _template(cand: Dict[str, Any]) -> str:
    kind, title = cand["kind"], cand["title"]
    if kind == "birthday":
        return f"{title}快乐呀！今天打算怎么过？"
    if kind == "event":
        return f"今天不是{title}嘛，准备得怎么样了？"
    if kind in ("promise", "checkin", "health"):
        return f"上次说的{title}，后来怎么样了？"
    if kind == "person":
        return f"突然想起{title}，最近还联系吗？"
    if kind == "rain":
        return f"{title}今天有雨，出门记得带伞，别淋湿了。"
    if kind == "open_loop":
        return f"想起你之前说的{title}，后来有下文了吗？"
    return "没什么事，就是想问问你今天过得怎么样。"


def _polish(cand: Dict[str, Any], persona: str = "") -> str:
    """让模型把事实说成人话；调不通就用模板，保证功能不哑。"""
    fallback = _template(cand)
    context = [line for line in (cand.get("context") or []) if line][:2]
    try:
        llm = get_llm_client(temperature=0.9)
        system = (
            "你是 moz，用户的朋友。现在由你主动发起关心，不是客服也不是通知机器人。\n"
            "要求：只说一到两句，不超过 "
            f"{'55' if context else '45'}"
            " 个字；口语、具体、贴着事实说；"
            "不要客套开头，不要用「您好」「请问」，不要解释自己为什么说话，不要加表情符号。\n"
        )
        if context:
            system += (
                "下面几件旧事和它连着线（同一个人或同一类事）。最多挑一条自然地带上，"
                "让这句话听像是记得，而不是提醒音；用不上就一条都别硬塞：\n"
                + "\n".join(f"- {line}" for line in context) + "\n"
            )
        if persona:
            system += f"你的说话风格参考：{persona[:200]}\n"
        msg = [SystemMessage(content=system),
               HumanMessage(content=f"背景：{cand['why']}\n请只输出你要对用户说的那句话。")]
        reply = llm.invoke(msg)
        text = (getattr(reply, "content", "") or "").strip().strip('"“”')
        if text and len(text) <= 120:
            return text
    except Exception as e:
        logger.warning("[主动关心] 措辞润色失败，用模板兜底: %s", type(e).__name__)
    return fallback


def tick_once(store, wm_store, user_id: str, now: Optional[float] = None,
              dry_run: bool = False, persona: str = "", graph=None) -> List[Dict[str, Any]]:
    """跑一轮判定。dry_run=True 时只返回"将要说什么"，不写库。"""
    now = now or time.time()
    settings = store.get_settings(user_id)
    if not settings.get("enabled", True):
        return []
    if in_quiet_hours(settings, now):
        return []
    if now - _last_user_seen.get(user_id, 0) < USER_PRESENT_SECONDS:
        return []

    # "话多话少"换算的当日名额只限没来由的搭话（在 collect 里判），
    # 到点提醒不该被闲聊名额挤掉，但事项堆一起时得有这条硬上限兜底
    if store.fired_since(user_id, _day_start(now)) >= MAX_PROACTIVE_PER_DAY:
        return []
    if now - store.last_fired_at(user_id) < MIN_GAP_SECONDS:
        return []

    candidates = collect(store, wm_store, user_id, now, graph=graph)
    if not candidates:
        return []

    cand = candidates[0]  # 一次只说一件事，别刷屏
    text = _template(cand) if dry_run else _polish(cand, persona)
    result = {"kind": cand["kind"], "ref_id": cand["ref_id"], "title": cand["title"],
              "why": cand["why"], "text": text, "context": cand.get("context") or []}
    if dry_run:
        return [result]

    store.enqueue(user_id, cand["kind"], text, cand["ref_id"])
    if cand["ref_id"]:
        store.mark_fired(cand["ref_id"], now)
    logger.info("[主动关心] %s -> %s", cand["kind"], text)
    return [result]


async def care_loop(store, wm_store, user_id: str, interval: int = 60,
                    persona_getter=None, graph=None, memory_manager=None) -> None:
    """后端常驻心跳：每 interval 秒判定一次。

    顺带刷关联图：按水位增量补边（只有改过的事项/记忆才重新抽枢纽），
    所以事件关联晚一分钟出现没关系，代价不该摊到每条对话上。
    """
    logger.info("[主动关心] 心跳启动，每 %ss 判定一次（用户 %s）", interval, user_id)
    while True:
        try:
            if graph is not None:
                await asyncio.to_thread(graph.sync_all, store, memory_manager, user_id)
            persona = persona_getter() if persona_getter else ""
            await asyncio.to_thread(tick_once, store, wm_store, user_id, None, False,
                                    persona, graph)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[主动关心] 本轮判定异常: %s", e)
        await asyncio.sleep(interval)

"""
工作记忆模块 - 跨对话的动态上下文

解决：用户切换对话后 AI 丢失上下文的问题。

工作记忆 vs 长期记忆：
- 工作记忆：当前状态摘要，始终注入 system prompt，跨对话持久
- 长期记忆：具体事实/事件，按相关性检索，按遗忘曲线衰减

Open Loops（借鉴 Zep temporal awareness + MEMORY_FRAMEWORK.md 设计）：
结构化跟踪用户提到的未完成事件，支持主动追问。
"""

import json
import time
import uuid
import sqlite3
import threading
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class OpenLoop:
    """一条待跟进的开放话题。"""

    def __init__(self, topic: str, status: str = "waiting", due_at: float = 0.0,
                 loop_id: str = "", created_at: float = 0.0):
        self.id = loop_id or uuid.uuid4().hex[:8]
        self.topic = topic
        self.status = status  # waiting | resolved | expired
        self.due_at = due_at
        self.created_at = created_at or time.time()

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "status": self.status,
            "due_at": self.due_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "OpenLoop":
        return cls(
            topic=data.get("topic", ""),
            status=data.get("status", "waiting"),
            due_at=data.get("due_at", 0),
            loop_id=data.get("id", ""),
            created_at=data.get("created_at", 0),
        )

    def is_active(self) -> bool:
        if self.status != "waiting":
            return False
        # Auto-expire after 30 days without resolution
        if self.created_at and (time.time() - self.created_at > 30 * 86400):
            return False
        return True


def _normalize_topics(raw: List) -> List[Dict]:
    """Convert flat string list or dict list to normalized dicts."""
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(OpenLoop(topic=item).to_dict())
        elif isinstance(item, dict) and item.get("topic"):
            result.append(item)
    return result


class WorkingMemoryStore:
    """跨对话工作记忆存储（SQLite 持久化）。"""

    def __init__(self, db_path: str = ""):
        if not db_path:
            import os
            db_path = os.path.join(
                os.path.abspath(os.path.dirname(__file__)), "moz.db"
            )
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS working_memory (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                open_topics TEXT NOT NULL DEFAULT '[]',
                current_emotion TEXT NOT NULL DEFAULT 'neutral',
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

    def load(self, user_id: str) -> Dict:
        """加载用户的工作记忆。open_topics 返回结构化的 dict 列表。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT summary, open_topics, current_emotion, updated_at FROM working_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return {"summary": "", "open_topics": [], "current_emotion": "neutral", "updated_at": 0}
        try:
            raw_topics = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            raw_topics = []
        open_loops = _normalize_topics(raw_topics)
        return {
            "summary": row[0],
            "open_topics": open_loops,
            "current_emotion": row[2],
            "updated_at": row[3],
        }

    def save(self, user_id: str, summary: str, open_loops: List, current_emotion: str):
        """保存用户的工作记忆。open_loops 接受字符串列表或字典列表。"""
        normalized = _normalize_topics(open_loops if isinstance(open_loops, list) else [])
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO working_memory (user_id, summary, open_topics, current_emotion, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, summary, json.dumps(normalized, ensure_ascii=False), current_emotion, time.time()),
        )
        conn.commit()

    def clear(self, user_id: str):
        """删除该用户的工作记忆（含开放话题）。"""
        conn = self._get_conn()
        conn.execute("DELETE FROM working_memory WHERE user_id = ?", (user_id,))
        conn.commit()

    def get_followup_text(self, user_id: str) -> str:
        """获取需要主动追问的开放话题文本。

        筛选条件：status == waiting 且已存在超过 1 天的话题。
        """
        data = self.load(user_id)
        followups = []
        for loop_dict in data["open_topics"]:
            loop = OpenLoop.from_dict(loop_dict)
            if not loop.is_active():
                continue
            age_hours = (time.time() - loop.created_at) / 3600
            if age_hours >= 24:
                days = int(age_hours // 24)
                time_hint = f"({days}天前提到)" if days < 7 else "(上周提到)"
                followups.append(f"{loop.topic}{time_hint}")

        if not followups:
            return ""
        return "你可以自然地关心一下这些事的进展：" + "；".join(followups[:3])

    def format_for_prompt(self, user_id: str) -> str:
        """格式化工作记忆为可注入 prompt 的文本。"""
        data = self.load(user_id)

        active_loops = []
        for loop_dict in data["open_topics"]:
            loop = OpenLoop.from_dict(loop_dict)
            if loop.is_active():
                active_loops.append(loop)

        if not data["summary"] and not active_loops:
            return ""

        parts = []
        if data["summary"]:
            parts.append(data["summary"])
        if active_loops:
            topics_text = "、".join(loop.topic for loop in active_loops[:5])
            parts.append(f"用户之前提到的未完成话题：{topics_text}")
        if data["current_emotion"] and data["current_emotion"] != "neutral":
            parts.append(f"用户最近的情绪：{data['current_emotion']}")

        return "\n".join(parts)


WORKING_MEMORY_UPDATE_PROMPT = """你是一个记忆更新模块。请根据最新对话更新工作记忆。

工作记忆是跨对话持久保存的，用于让 AI 在不同对话间保持对用户的了解。

当前工作记忆：
{current_memory}

最新对话：
用户：{user_message}
AI：{assistant_reply}

请更新工作记忆，规则：
1. 保留仍然有效的信息
2. 添加新的重要信息（用户明确说出的）
3. 移除已过时或矛盾的信息
4. summary 控制在 200 字以内，只保留最重要的信息
5. open_loops 跟踪用户提到的未完成事件/约定/等待结果的事（最多 5 个），每条格式 {{"topic": "..."}}
6. 如果用户说某件事已经完成/解决了，把对应的 open_loop 的 status 改为 "resolved"
7. 禁止编造或推测任何信息

输出 JSON：
{{"summary": "更新后的整体了解", "open_topics": [{{"topic": "话题", "status": "waiting|resolved"}}], "current_emotion": "情感标签"}}"""


def update_working_memory(
    store: WorkingMemoryStore,
    user_id: str,
    user_message: str,
    assistant_reply: str,
    llm_client=None,
):
    """使用 LLM 更新工作记忆，LLM 不可用时降级到规则更新。"""
    current = store.load(user_id)

    if llm_client is None:
        _rule_based_update(store, user_id, current, user_message, assistant_reply)
        return

    current_memory_text = current["summary"]
    active_topics = [
        OpenLoop.from_dict(d).topic
        for d in current["open_topics"]
        if OpenLoop.from_dict(d).is_active()
    ]
    if active_topics:
        current_memory_text += f"\n待跟进：{'、'.join(active_topics)}"
    if not current_memory_text.strip():
        current_memory_text = "（空，这是第一次对话）"

    from langchain_core.messages import HumanMessage

    prompt = WORKING_MEMORY_UPDATE_PROMPT.format(
        current_memory=current_memory_text,
        user_message=user_message[:500],
        assistant_reply=assistant_reply[:500],
    )

    try:
        response = llm_client.invoke([HumanMessage(content=prompt)])
        content = response.content.strip()

        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        result = json.loads(content)
        raw_loops = result.get("open_topics", [])

        # Preserve existing loop IDs and created_at when topics match
        existing_by_topic = {}
        for d in current["open_topics"]:
            existing_by_topic[d.get("topic", "")] = d

        merged_loops = []
        for raw in raw_loops:
            if isinstance(raw, str):
                raw = {"topic": raw}
            topic = raw.get("topic", "")
            prev = existing_by_topic.get(topic)
            loop = OpenLoop(
                topic=topic,
                status=raw.get("status", "waiting"),
                loop_id=prev.get("id", "") if prev else "",
                created_at=prev.get("created_at", 0) if prev else 0,
            )
            merged_loops.append(loop.to_dict())

        store.save(
            user_id=user_id,
            summary=result.get("summary", current["summary"]),
            open_loops=merged_loops,
            current_emotion=result.get("current_emotion", current["current_emotion"]),
        )
        logger.info(f"[工作记忆] 已更新 (用户: {user_id})")
    except Exception as e:
        logger.warning(f"[工作记忆] LLM 更新失败，使用规则更新: {e}")
        _rule_based_update(store, user_id, current, user_message, assistant_reply)


def _rule_based_update(
    store: WorkingMemoryStore,
    user_id: str,
    current: Dict,
    user_message: str,
    assistant_reply: str,
):
    """基于规则的简单工作记忆更新（LLM 不可用时的降级方案）。"""
    from memory_manager import EmotionAnalyzer

    summary = current["summary"]
    open_loops = list(current["open_topics"])

    emotion, _ = EmotionAnalyzer.analyze(user_message)
    current_emotion = emotion.value if emotion.value != "neutral" else current["current_emotion"]

    if len(user_message.strip()) >= 4:
        new_info = user_message.strip()[:80]
        if new_info not in summary:
            if summary:
                summary = summary + f"；最近提到：{new_info}"
            else:
                summary = f"最近提到：{new_info}"
            if len(summary) > 300:
                summary = summary[-300:]

    store.save(user_id, summary, open_loops, current_emotion)

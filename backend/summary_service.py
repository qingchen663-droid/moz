"""
持久化对话总结服务 — 记忆金字塔

借鉴 BaiShou-Next 的 summary cascade + snapshot 持久化模式：
1. 会话内旧轮次摘要 → 持久化到 SQLite（重启不丢失）
2. 同一周内的 session 摘要积累到阈值 → 合并为 weekly 周记
3. 未合并的周记积累到阈值 → 级联合并为 monthly 月记
4. 对话 Agent 注入最近摘要，让 AI 记得"上周/上个月聊过什么"
"""

import os
import re
import time
import sqlite3
import threading
import logging
from datetime import date
from typing import Dict, List, Optional, Callable

logger = logging.getLogger(__name__)

WEEKLY_TO_MONTHLY_THRESHOLD = 4
SESSIONS_PER_WEEKLY_THRESHOLD = 3


class SummaryService:
    """SQLite 持久化的周期性对话总结。"""

    def __init__(
        self,
        db_path: str = "",
        merger: Optional[Callable[[List[str]], str]] = None,
    ):
        if not db_path:
            db_path = os.path.join(
                os.path.abspath(os.path.dirname(__file__)), "moz.db"
            )
        self.db_path = db_path
        self._local = threading.local()
        self._cascade_lock = threading.Lock()
        # 可注入的合并函数（测试用）；默认走 LLM
        self._merger = merger
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                period_type TEXT NOT NULL DEFAULT 'session',
                period_key TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                merged_into INTEGER DEFAULT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_user_period "
            "ON conversation_summaries(user_id, period_type, period_key)"
        )
        conn.commit()

    def clear(self, user_id: str) -> int:
        """删除该用户的全部总结（会话/周记/月记），返回删除条数。"""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM conversation_summaries WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount

    def save_summary(
        self,
        user_id: str,
        content: str,
        period_type: str = "session",
        period_key: str = "",
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO conversation_summaries (user_id, period_type, period_key, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, period_type, period_key, content, time.time()),
        )
        conn.commit()
        summary_id = cursor.lastrowid
        logger.info("[SummaryService] 保存 %s 摘要 user=%s key=%s", period_type, user_id, period_key)
        return summary_id

    def get_recent_summaries(self, user_id: str, limit: int = 6) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, period_type, period_key, content, created_at "
            "FROM conversation_summaries WHERE user_id = ? AND merged_into IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [
            {"id": r[0], "period_type": r[1], "period_key": r[2], "content": r[3], "created_at": r[4]}
            for r in rows
        ]

    def format_for_prompt(self, user_id: str, max_chars: int = 800) -> str:
        summaries = self.get_recent_summaries(user_id)
        if not summaries:
            return ""
        parts: List[str] = []
        total_len = 0
        label_map = {"session": "近期对话", "weekly": "周记", "monthly": "月记"}
        for s in summaries:
            label = label_map.get(s["period_type"], s["period_type"])
            line = f"[{label} {s['period_key']}] {s['content']}"
            if total_len + len(line) > max_chars:
                break
            parts.append(line)
            total_len += len(line)
        return "\n".join(parts)

    def get_unmerged_weeklies(self, user_id: str) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content, period_key FROM conversation_summaries "
            "WHERE user_id = ? AND period_type = 'weekly' AND merged_into IS NULL "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        return [{"id": r[0], "content": r[1], "period_key": r[2]} for r in rows]

    def merge_completed(self, user_id: str, monthly_content: str, month_key: str, weekly_ids: List[int]):
        """兼容旧接口：把若干 weekly 合并记录为一条 monthly。"""
        parent_id = self._create_merged(user_id, "monthly", month_key, monthly_content)
        self._mark_merged(user_id, weekly_ids, parent_id)
        logger.info("[SummaryService] 级联完成 user=%s %d weeklies -> monthly %s", user_id, len(weekly_ids), month_key)

    # ------------------------------------------------------------------
    # 级联合并
    # ------------------------------------------------------------------

    @staticmethod
    def _month_key_from_week(week_key: str) -> str:
        """'2026-W30' → '2026-07'（取该 ISO 周周一所在月份）。"""
        m = re.match(r"^(\d{4})-W(\d{1,2})$", week_key or "")
        if not m:
            return ""
        try:
            d = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            return d.strftime("%Y-%m")
        except ValueError:
            return ""

    def _merge_contents(self, contents: List[str]) -> Optional[str]:
        if not contents:
            return None
        if self._merger is not None:
            return self._merger(contents).strip() or None
        return self._llm_merge(contents)

    def _llm_merge(self, contents: List[str]) -> Optional[str]:
        from llm_config import get_llm_client
        from langchain_core.messages import HumanMessage

        joined = "\n".join(f"- {c}" for c in contents)
        prompt = f"""以下是同一主题的多条周期摘要，请合并为一条简洁完整的总结（120字以内）。
只输出合并后的内容，不要解释。

{joined}"""
        response = get_llm_client(temperature=0.3, use_thinking=False).invoke(
            [HumanMessage(content=prompt)]
        )
        text = response.content.strip()
        return text or None

    def _create_merged(self, user_id: str, period_type: str, period_key: str, content: str) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO conversation_summaries (user_id, period_type, period_key, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, period_type, period_key, content, time.time()),
        )
        conn.commit()
        return cursor.lastrowid

    def _mark_merged(self, user_id: str, child_ids: List[int], parent_id: int):
        conn = self._get_conn()
        conn.executemany(
            "UPDATE conversation_summaries SET merged_into = ? WHERE id = ? AND user_id = ?",
            [(parent_id, cid, user_id) for cid in child_ids],
        )
        conn.commit()

    def maybe_cascade(self, user_id: str) -> Dict[str, int]:
        """执行一次级联合并：session→weekly→monthly。

        返回本轮新生成的 {"weeklies": n, "monthlies": m}。
        已在级联中时直接跳过，避免并发重复合并。
        """
        if not self._cascade_lock.acquire(blocking=False):
            return {"weeklies": 0, "monthlies": 0}
        result = {"weeklies": 0, "monthlies": 0}
        try:
            result["weeklies"] = self._rollup_sessions(user_id)
            result["monthlies"] = self._rollup_weeklies(user_id)
        finally:
            self._cascade_lock.release()
        return result

    def cascade_async(self, user_id: str):
        """在后台线程执行级联，不阻塞对话主流程。"""
        threading.Thread(target=self.maybe_cascade, args=(user_id,), daemon=True).start()

    def _rollup_sessions(self, user_id: str) -> int:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, period_key, content FROM conversation_summaries "
            "WHERE user_id = ? AND period_type = 'session' AND merged_into IS NULL "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

        groups: Dict[str, List[Dict]] = {}
        for rid, key, content in rows:
            groups.setdefault(key, []).append({"id": rid, "content": content})

        merged_count = 0
        for week_key, items in groups.items():
            if len(items) < SESSIONS_PER_WEEKLY_THRESHOLD:
                continue
            merged_text = self._merge_contents([it["content"] for it in items])
            if not merged_text:
                continue
            weekly_id = self._create_merged(user_id, "weekly", week_key, merged_text)
            self._mark_merged(user_id, [it["id"] for it in items], weekly_id)
            logger.info("[SummaryService] 会话摘要聚合 user=%s %d sessions -> weekly %s", user_id, len(items), week_key)
            merged_count += 1
        return merged_count

    def _rollup_weeklies(self, user_id: str) -> int:
        unmerged = self.get_unmerged_weeklies(user_id)
        if len(unmerged) < WEEKLY_TO_MONTHLY_THRESHOLD:
            return 0

        batch = unmerged[:WEEKLY_TO_MONTHLY_THRESHOLD]
        merged_text = self._merge_contents([w["content"] for w in batch])
        if not merged_text:
            return 0
        month_key = self._month_key_from_week(batch[-1]["period_key"]) or time.strftime("%Y-%m")
        monthly_id = self._create_merged(user_id, "monthly", month_key, merged_text)
        self._mark_merged(user_id, [w["id"] for w in batch], monthly_id)
        logger.info("[SummaryService] 级联完成 user=%s %d weeklies -> monthly %s", user_id, len(batch), month_key)
        return 1

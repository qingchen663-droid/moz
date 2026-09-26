import os
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Dict, Optional, Tuple


class ConversationStore:
    """将会话记录持久化到 SQLite 数据库。"""

    def __init__(self, db_path: str = ""):
        if not db_path:
            db_path = os.path.join(
                os.path.abspath(os.path.dirname(__file__)), "moz.db"
            )
        self.db_path = db_path
        self._local = threading.local()
        self._locks_guard = threading.Lock()
        self._locks: Dict[str, threading.RLock] = {}
        self._init_db()

    @contextmanager
    def locked(self, user_id: str):
        """Serialize read-modify-write cycles for one user across threads."""
        with self._locks_guard:
            lock = self._locks.setdefault(user_id, threading.RLock())
        with lock:
            yield

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                user_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
                user_id, conversation_id UNINDEXED, title, content,
                tokenize='unicode61'
            )
        """)
        conn.commit()

    def save(self, user_id: str, conversations: Dict, current_id: str):
        data = json.dumps(
            {"current_id": current_id, "conversations": conversations},
            ensure_ascii=False,
        )
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO conversations (user_id, data) VALUES (?, ?)",
            (user_id, data),
        )
        self._sync_fts(user_id, conversations, current_id)
        conn.commit()

    def load(self, user_id: str) -> Tuple[Optional[Dict], Optional[str]]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT data FROM conversations WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None, None
        try:
            data = json.loads(row[0])
            return data.get("conversations", {}), data.get("current_id")
        except (json.JSONDecodeError, KeyError):
            return None, None

    def _sync_fts(self, user_id: str, conversations: Dict, current_id: str):
        """Rebuild the FTS index for a user (simple full rebuild on save)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM conversation_fts WHERE user_id = ?", (user_id,))
        for conv_id, conv in conversations.items():
            title = conv.get("title", "")
            content_parts = []
            for msg in conv.get("messages", []):
                content_parts.append(msg.get("content", ""))
            content = " ".join(content_parts)
            if title or content:
                conn.execute(
                    "INSERT INTO conversation_fts (user_id, conversation_id, title, content) VALUES (?, ?, ?, ?)",
                    (user_id, conv_id, title, content[:5000]),
                )
        conn.commit()

    def search(self, user_id: str, query: str, limit: int = 10) -> list:
        """Full-text search across conversation titles and message content."""
        if not query.strip():
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT conversation_id, title FROM conversation_fts "
                "WHERE conversation_fts MATCH ? AND user_id = ? LIMIT ?",
                (query.strip(), user_id, limit),
            ).fetchall()
            return [{"conversation_id": r[0], "title": r[1]} for r in rows]
        except sqlite3.OperationalError:
            return []

    def get_user_ids(self) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute("SELECT DISTINCT user_id FROM conversations").fetchall()
        return [row[0] for row in rows]

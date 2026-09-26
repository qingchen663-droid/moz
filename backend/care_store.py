"""个人关心数据库：提醒事项、主动消息队列、发送历史与设置。

与 moz.db 同库，沿用「按 __file__ 定位、不依赖 cwd」的既有约定。
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "moz.db")

ITEM_KINDS = ("birthday", "event", "promise", "checkin", "health", "person", "note")
REPEATS = ("none", "daily", "weekly", "yearly")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "province": "",
    "city": "",
    "quiet_start": "23:00",
    "quiet_end": "08:00",
    # talk_mode: auto=按数据库判断话多话少；quiet/normal/chatty 为手动覆盖
    "talk_mode": "auto",
    # auto 时由历史行为算出的 0~1 倾向，越大越爱聊
    "talk_score": 0.5,
    "rain_reminder": True,
}

TALK_BUDGET = {"quiet": 1, "normal": 2, "chatty": 4}


class CareStore:
    """线程安全的关心数据存储（SQLite WAL，连接按线程复用）。"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS care_items (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'event',
                title TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                due_at REAL NOT NULL DEFAULT 0,
                repeat TEXT NOT NULL DEFAULT 'none',
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'manual',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_fired_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_care_items_user ON care_items(user_id, status);

            CREATE TABLE IF NOT EXISTS proactive_queue (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL,
                acked_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_user ON proactive_queue(user_id, acked_at);

            CREATE TABLE IF NOT EXISTS care_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                ref_id TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                fired_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_care_log_user ON care_log(user_id, fired_at);

            CREATE TABLE IF NOT EXISTS care_settings (
                user_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        conn.commit()

    # ── 提醒事项 ─────────────────────────────────────────
    def add_item(self, user_id: str, title: str, kind: str = "event", detail: str = "",
                 due_at: float = 0.0, repeat: str = "none", source: str = "manual") -> Dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("提醒事项需要标题")
        if kind not in ITEM_KINDS:
            kind = "event"
        if repeat not in REPEATS:
            repeat = "none"
        now = time.time()
        item = {
            "id": uuid.uuid4().hex[:10],
            "user_id": user_id,
            "kind": kind,
            "title": title,
            "detail": (detail or "").strip(),
            "due_at": float(due_at or 0),
            "repeat": repeat,
            "status": "active",
            "source": source,
            "created_at": now,
            "updated_at": now,
            "last_fired_at": 0.0,
        }
        self._conn().execute(
            "INSERT INTO care_items (id,user_id,kind,title,detail,due_at,repeat,status,source,"
            "created_at,updated_at,last_fired_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(item.values()),
        )
        self._conn().commit()
        return item

    def list_items(self, user_id: str, status: Optional[str] = "active") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM care_items WHERE user_id = ?"
        args: List[Any] = [user_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY (due_at = 0), due_at ASC, created_at DESC"
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM care_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def update_item(self, user_id: str, item_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {"kind", "title", "detail", "due_at", "repeat", "status"}
        fields = {k: v for k, v in (patch or {}).items() if k in allowed and v is not None}
        if not fields:
            return self.get_item(item_id)
        if "kind" in fields and fields["kind"] not in ITEM_KINDS:
            fields.pop("kind")
        if "repeat" in fields and fields["repeat"] not in REPEATS:
            fields.pop("repeat")
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn().execute(
            f"UPDATE care_items SET {sets} WHERE id = ? AND user_id = ?",
            (*fields.values(), item_id, user_id),
        )
        self._conn().commit()
        return self.get_item(item_id)

    def delete_item(self, user_id: str, item_id: str) -> bool:
        cur = self._conn().execute("DELETE FROM care_items WHERE id = ? AND user_id = ?", (item_id, user_id))
        self._conn().commit()
        return cur.rowcount > 0

    def mark_fired(self, item_id: str, when: Optional[float] = None) -> None:
        self._conn().execute(
            "UPDATE care_items SET last_fired_at = ?, updated_at = ? WHERE id = ?",
            (when or time.time(), time.time(), item_id),
        )
        self._conn().commit()

    # ── 主动消息队列 ─────────────────────────────────────
    def enqueue(self, user_id: str, kind: str, text: str, ref_id: str = "") -> Dict[str, Any]:
        now = time.time()
        row = {
            "id": uuid.uuid4().hex[:10],
            "user_id": user_id,
            "kind": kind,
            "text": text,
            "created_at": now,
            "acked_at": None,
        }
        conn = self._conn()
        conn.execute(
            "INSERT INTO proactive_queue (id,user_id,kind,text,created_at,acked_at) VALUES (?,?,?,?,?,?)",
            tuple(row.values()),
        )
        conn.execute(
            "INSERT INTO care_log (user_id,kind,ref_id,text,fired_at) VALUES (?,?,?,?,?)",
            (user_id, kind, ref_id, text, now),
        )
        conn.commit()
        return row

    def pending(self, user_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM proactive_queue WHERE user_id = ? AND acked_at IS NULL ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def ack(self, user_id: str, ids: List[str]) -> List[str]:
        """确认已读，返回**本次真正由自己翻转成功**的 id。

        托盘和前端都在 ack 同一个队列，只有先到的那个会拿到 id，
        调用方据此决定要不要落库，否则同一条话会被写进对话两次。
        """
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        conn = self._conn()
        rows = conn.execute(
            f"SELECT id FROM proactive_queue WHERE user_id = ? AND acked_at IS NULL AND id IN ({marks})",
            (user_id, *ids),
        ).fetchall()
        winners = [r["id"] for r in rows]
        if winners:
            wm = ",".join("?" * len(winners))
            conn.execute(
                f"UPDATE proactive_queue SET acked_at = ? WHERE user_id = ? AND acked_at IS NULL AND id IN ({wm})",
                (time.time(), user_id, *winners),
            )
            conn.commit()
        return winners

    # ── 节流依据 ─────────────────────────────────────────
    def fired_since(self, user_id: str, since_ts: float, kind: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM care_log WHERE user_id = ? AND fired_at >= ?"
        args: List[Any] = [user_id, since_ts]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return self._conn().execute(sql, args).fetchone()[0]

    def last_fired_at(self, user_id: str) -> float:
        row = self._conn().execute(
            "SELECT MAX(fired_at) FROM care_log WHERE user_id = ?", (user_id,)
        ).fetchone()
        return float(row[0] or 0)

    # ── 设置 ─────────────────────────────────────────────
    def get_settings(self, user_id: str) -> Dict[str, Any]:
        row = self._conn().execute(
            "SELECT data FROM care_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        merged = dict(DEFAULT_SETTINGS)
        if row:
            try:
                merged.update(json.loads(row[0]))
            except (json.JSONDecodeError, TypeError):
                pass
        merged["user_id"] = user_id
        return merged

    def save_settings(self, user_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_settings(user_id)
        for key in DEFAULT_SETTINGS:
            if key in patch and patch[key] is not None:
                current[key] = patch[key]
        current.pop("user_id", None)
        self._conn().execute(
            "INSERT OR REPLACE INTO care_settings (user_id, data, updated_at) VALUES (?,?,?)",
            (user_id, json.dumps(current, ensure_ascii=False), time.time()),
        )
        self._conn().commit()
        return self.get_settings(user_id)

    # ── 话多话少 ─────────────────────────────────────────
    def daily_budget(self, user_id: str) -> int:
        """当日可主动开口的条数：手动模式优先，否则按 talk_score 换算。"""
        s = self.get_settings(user_id)
        if not s.get("enabled", True):
            return 0
        mode = s.get("talk_mode", "auto")
        if mode in TALK_BUDGET:
            return TALK_BUDGET[mode]
        score = float(s.get("talk_score", 0.5) or 0.5)
        return max(0, min(TALK_BUDGET["chatty"], int(round(score * TALK_BUDGET["chatty"]))))

    def observe_style(self, user_id: str, user_text_len: int, user_initiated: bool) -> None:
        """把每次对话的行为折算进 talk_score（指数平滑，越聊越准）。"""
        # 长回复 + 主动开口 = 爱聊；短促 + 被动 = 想安静
        length_signal = min(1.0, user_text_len / 60.0)
        signal = 0.5 * length_signal + 0.5 * (1.0 if user_initiated else 0.25)
        s = self.get_settings(user_id)
        if s.get("talk_mode", "auto") != "auto":
            return  # 手动覆盖时不再自动漂移
        old = float(s.get("talk_score", 0.5))
        s["talk_score"] = round(0.85 * old + 0.15 * signal, 3)
        self._conn().execute(
            "INSERT OR REPLACE INTO care_settings (user_id, data, updated_at) VALUES (?,?,?)",
            (user_id, json.dumps({k: v for k, v in s.items() if k != "user_id"}, ensure_ascii=False), time.time()),
        )
        self._conn().commit()

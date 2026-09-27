"""后台落库队列：把"聊完这一轮要记住什么"从一次性内存任务变成可续跑的持久任务。

存在的理由：一轮后台落库要跑 4 次模型调用（实测 25~135 秒一次），旧实现把它挂在
`asyncio.create_task` 上，`--reload` 一重启这轮就"聊完白聊"。任务先写进 SQLite，
进程重启后由 recover() 接手继续跑，所以对话记录里的东西最终都会落进记忆。
"""

import asyncio
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

DB_PATH = os.path.join(os.path.abspath(os.path.dirname(__file__)), "moz.db")

MAX_ATTEMPTS = 3          # 中转偶发连接失败，重试三次仍失败才认输
DONE_HISTORY = 200        # 只留最近 200 条已完成任务供排查
STALE_AFTER = 1800.0      # running 超过 30 分钟视为进程遗骸


class SaveQueue:
    """save_jobs 表的线程安全读写（连接按线程复用，与 care_store 同一套约定）。"""

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
            CREATE TABLE IF NOT EXISTS save_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                user_message TEXT NOT NULL,
                reply TEXT NOT NULL,
                emotion_type TEXT,
                emotion_intensity REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                worker TEXT,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_save_jobs_status ON save_jobs(status, created_at);
            """
        )
        self._conn().commit()

    def enqueue(
        self,
        user_id: str,
        user_message: str,
        reply: str,
        conversation_id: Optional[str] = None,
        emotion_type: Optional[str] = None,
        emotion_intensity: Optional[float] = None,
    ) -> Optional[int]:
        """登记一轮落库；空回复/空消息不值得占用队列。"""
        if not (user_message or "").strip() or not (reply or "").strip():
            return None
        now = time.time()
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO save_jobs
               (user_id, conversation_id, user_message, reply, emotion_type,
                emotion_intensity, status, attempts, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'pending', 0, ?, ?)""",
            (user_id, conversation_id, user_message, reply, emotion_type,
             emotion_intensity, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)

    def claim(self, worker: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """取最早一条 pending 并原子地占为 running；没有则返回 None。"""
        conn = self._conn()
        now = time.time()
        while True:
            row = conn.execute(
                "SELECT * FROM save_jobs WHERE status='pending' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE save_jobs SET status='running', attempts=attempts+1,"
                " worker=?, updated_at=? WHERE id=? AND status='pending'",
                (worker or uuid.uuid4().hex[:8], now, row["id"]),
            )
            conn.commit()
            if cur.rowcount == 1:
                return dict(row)
            # 别的协程刚好抢走了，重看一次

    def mark_done(self, job_id: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE save_jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?",
            (time.time(), job_id),
        )
        conn.commit()

    def mark_failed(self, job_id: int, error: str) -> str:
        """没到上限的退回 pending 重试，到了才标 failed。返回新状态。"""
        conn = self._conn()
        row = conn.execute("SELECT attempts FROM save_jobs WHERE id=?", (job_id,)).fetchone()
        attempts = int(row["attempts"]) if row else MAX_ATTEMPTS
        status = "pending" if attempts < MAX_ATTEMPTS else "failed"
        conn.execute(
            "UPDATE save_jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, (error or "")[:500], time.time(), job_id),
        )
        conn.commit()
        return status

    def recover(self) -> int:
        """把上次进程遗骸（running）退回 pending，供重启后续跑。"""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE save_jobs SET status='pending', worker=NULL, updated_at=?"
            " WHERE status='running'",
            (time.time(),),
        )
        conn.commit()
        return cur.rowcount

    def reap_stale(self, max_age: float = STALE_AFTER) -> int:
        """清理器：长时间卡在 running 的任务按遗骸处理。"""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE save_jobs SET status='pending', worker=NULL, updated_at=?"
            " WHERE status='running' AND updated_at < ?",
            (time.time(), time.time() - max_age),
        )
        conn.commit()
        return cur.rowcount

    def stats(self) -> Dict[str, int]:
        rows: List[sqlite3.Row] = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM save_jobs GROUP BY status"
        ).fetchall()
        out = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for row in rows:
            out[row["status"]] = int(row["n"])
        return out

    def pending_for(self, user_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM save_jobs WHERE user_id=? AND status IN ('pending','running')",
            (user_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def prune(self, keep: int = DONE_HISTORY) -> int:
        """只删已完成/已认输的历史行，未跑完的一条都不动。"""
        conn = self._conn()
        keep_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM save_jobs WHERE status IN ('done','failed')"
                " ORDER BY id DESC LIMIT ?", (keep,)
            ).fetchall()
        ]
        if keep_ids:
            marks = ",".join("?" * len(keep_ids))
            cur = conn.execute(
                f"DELETE FROM save_jobs WHERE status IN ('done','failed')"
                f" AND id NOT IN ({marks})",
                keep_ids,
            )
        else:
            cur = conn.execute("DELETE FROM save_jobs WHERE status IN ('done','failed')")
        conn.commit()
        return cur.rowcount


class SaveWorker:
    """单飞worker：一次处理一轮落库，enqueue 时立刻被叫醒。

    一次只跑一轮是有意的——四步在任务内部已经并行了，再叠轮次只会让中转限流。
    """

    def __init__(
        self,
        queue: SaveQueue,
        process: Callable[[Dict[str, Any]], Awaitable[None]],
        poll_seconds: float = 2.0,
    ):
        self.queue = queue
        self.process = process
        self.poll_seconds = poll_seconds
        self.worker_id = uuid.uuid4().hex[:8]
        self._wake = asyncio.Event()
        self._stopping = False
        self.processed = 0
        self.errors = 0

    def submit(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def run(self) -> None:
        import logging
        log = logging.getLogger(__name__)
        while not self._stopping:
            job = self.queue.claim(self.worker_id)
            if job is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue
            self._wake.clear()
            try:
                await self.process(job)
                self.queue.mark_done(job["id"])
                self.processed += 1
            except asyncio.CancelledError:
                self.queue.mark_failed(job["id"], "进程退出，任务被取消")
                raise
            except Exception as e:
                self.errors += 1
                status = self.queue.mark_failed(job["id"], f"{type(e).__name__}: {e}")
                log.warning("[落库队列] 任务 %s 失败（退回 %s）: %s", job["id"], status, e)
            if self.processed and self.processed % 20 == 0:
                self.queue.prune()

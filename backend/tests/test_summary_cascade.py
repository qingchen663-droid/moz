"""
总结级联（session -> weekly -> monthly）+ OpenLoop 生命周期测试。
不依赖 LLM：通过注入 merger 模拟合并结果。
"""

import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))

from summary_service import SummaryService
from working_memory import OpenLoop, WorkingMemoryStore


def _close_and_remove(store, path):
    """先关闭线程本地 SQLite 连接，再删除库文件与 WAL 伴生文件（Windows 必须）。"""
    conn = getattr(getattr(store, "_local", None), "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _make_service():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    merger = lambda parts: "合并:" + "+".join(p[:6] for p in parts)
    return SummaryService(db_path=tmp.name, merger=merger), tmp.name


class TestSessionToWeekly:
    def test_rollup_when_threshold_reached(self):
        svc, path = _make_service()
        try:
            for i in range(3):
                svc.save_summary("u1", f"会话摘要{i}", "session", "2026-W30")

            result = svc.maybe_cascade("u1")
            assert result["weeklies"] == 1

            recent = {s["period_type"]: s["content"] for s in svc.get_recent_summaries("u1")}
            assert recent.get("weekly", "").startswith("合并:")
            types = [s["period_type"] for s in svc.get_recent_summaries("u1")]
            assert "session" not in types
        finally:
            _close_and_remove(svc, path)

    def test_no_rollup_below_threshold(self):
        svc, path = _make_service()
        try:
            for i in range(2):
                svc.save_summary("u1", f"会话摘要{i}", "session", "2026-W30")
            result = svc.maybe_cascade("u1")
            assert result == {"weeklies": 0, "monthlies": 0}
            assert len(svc.get_recent_summaries("u1")) == 2
        finally:
            _close_and_remove(svc, path)


class TestWeeklyToMonthly:
    def test_monthly_created_after_four_weeklies(self):
        svc, path = _make_service()
        try:
            for key in ["2026-W27", "2026-W28", "2026-W29"]:
                svc.save_summary("u1", f"周记{key}", "weekly", key)
            # 第四条 weekly 通过会话聚合产生，键同为 W30
            for i in range(3):
                svc.save_summary("u1", f"会话{i}", "session", "2026-W30")
            # 级联一次性冲到底：3条会话聚合成第4条周记后，紧接着触发月报合并
            result = svc.maybe_cascade("u1")
            assert result == {"weeklies": 1, "monthlies": 1}
            monthly = [s for s in svc.get_recent_summaries("u1") if s["period_type"] == "monthly"]
            assert len(monthly) == 1
            assert monthly[0]["period_key"] == "2026-07"
            assert monthly[0]["content"].startswith("合并:")
            assert svc.get_unmerged_weeklies("u1") == []
        finally:
            _close_and_remove(svc, path)

    def test_batch_respects_threshold(self):
        svc, path = _make_service()
        try:
            for key in ["2026-W20", "2026-W21", "2026-W22"]:
                svc.save_summary("u1", f"周记{key}", "weekly", key)
            result = svc.maybe_cascade("u1")
            assert result["monthlies"] == 0
        finally:
            _close_and_remove(svc, path)


class TestMonthKeyConversion:
    def test_iso_week_to_month(self):
        assert SummaryService._month_key_from_week("2026-W27") == "2026-06"
        assert SummaryService._month_key_from_week("2026-W01") == "2025-12"
        assert SummaryService._month_key_from_week("bad-key") == ""


class TestOpenLoopLifecycle:
    def _store(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return WorkingMemoryStore(db_path=tmp.name), tmp.name

    def test_active_loop_is_followed_up(self):
        store, path = self._store()
        try:
            old = time.time() - 2 * 86400
            store.save("u1", "", [
                {"topic": "周四面试", "status": "waiting", "created_at": old},
            ], "neutral")
            text = store.get_followup_text("u1")
            assert "周四面试" in text
            prompt = store.format_for_prompt("u1")
            assert "周四面试" in prompt
        finally:
            _close_and_remove(store, path)

    def test_recent_loop_not_followed_up_yet(self):
        store, path = self._store()
        try:
            store.save("u1", "", [{"topic": "刚说的事"}], "neutral")
            assert store.get_followup_text("u1") == ""
            assert "刚说的事" in store.format_for_prompt("u1")
        finally:
            _close_and_remove(store, path)

    def test_expired_loop_hidden(self):
        store, path = self._store()
        try:
            stale = time.time() - 31 * 86400
            loop = OpenLoop(topic="过期话题", created_at=stale)
            assert not loop.is_active()
            store.save("u1", "", [loop.to_dict()], "neutral")
            assert "过期话题" not in store.format_for_prompt("u1")
            assert store.get_followup_text("u1") == ""
        finally:
            _close_and_remove(store, path)

    def test_resolved_loop_not_injected(self):
        store, path = self._store()
        try:
            old = time.time() - 3 * 86400
            store.save("u1", "", [
                {"topic": "已完成的事", "status": "resolved", "created_at": old},
            ], "neutral")
            assert "已完成的事" not in store.get_followup_text("u1")
            assert "已完成的事" not in store.format_for_prompt("u1")
        finally:
            _close_and_remove(store, path)


class TestCascadeConcurrencyGuard:
    def test_lock_prevents_double_run(self):
        svc, path = _make_service()
        try:
            held = svc._cascade_lock.acquire(blocking=False)
            assert held, "fresh service lock should be free"
            try:
                for i in range(3):
                    svc.save_summary("u1", f"会话{i}", "session", "2026-W30")
                assert svc.maybe_cascade("u1") == {"weeklies": 0, "monthlies": 0}
            finally:
                svc._cascade_lock.release()
        finally:
            _close_and_remove(svc, path)

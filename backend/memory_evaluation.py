"""Deterministic offline evaluation for the governed long-term memory system."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List

from memory_manager import EmotionType, MemoryCategory, MemoryManager, MemoryStatus
from memory_governance import normalized_content

# The evaluator stubs embeddings, so this placeholder prevents startup warnings
# without changing retrieval behavior.
os.environ.setdefault("ZHIPU_API_KEY", "offline-memory-evaluation")


@dataclass
class ScenarioReport:
    name: str
    passed: bool
    metrics: Dict[str, float] = field(default_factory=dict)
    details: Dict[str, object] = field(default_factory=dict)


@contextmanager
def memory_manager_context():
    directory = tempfile.TemporaryDirectory(prefix="moz-memory-eval-")
    manager = None
    try:
        manager = MemoryManager(
            storage_path=directory.name,
            db_path=os.path.join(directory.name, "memory.db"),
        )
        # Keep the evaluation offline and deterministic.
        manager.embedding_service.get_embedding = lambda text: None
        manager.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
        yield manager
    finally:
        if manager is not None:
            active_connection = getattr(manager._local, "conn", None)
            if active_connection is not None:
                active_connection.close()
        directory.cleanup()


def _search(manager: MemoryManager, query: str, limit: int = 3):
    started = time.perf_counter()
    results = manager.search_memories("eval-user", query, limit=limit)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return results, round(elapsed_ms, 3)


def _metrics(
    *,
    recall_hit: bool = False,
    recall_total: int = 0,
    retrieval_latency_ms: float = 0,
) -> Dict[str, float]:
    return {
        "recall_hit": float(recall_hit),
        "recall_total": float(recall_total),
        "retrieval_latency_ms": float(retrieval_latency_ms),
    }


def evaluate_identity_recall() -> Dict[str, object]:
    with memory_manager_context() as manager:
        manager.add_memory(
            "eval-user",
            "用户叫林清，住在杭州",
            category=MemoryCategory.FACT,
            confidence=0.92,
            conversation_id="day-1",
        )
        results, latency = _search(manager, "用户叫什么名字")
        hit = any("林清" in item.content for item in results)
        assert hit, f"identity memory was not recalled: {[item.content for item in results]}"
        return {"metrics": _metrics(recall_hit=True, recall_total=1, retrieval_latency_ms=latency)}


def evaluate_preference_recall() -> Dict[str, object]:
    with memory_manager_context() as manager:
        manager.add_memory(
            "eval-user",
            "用户喜欢安静的工作环境，不喜欢被突然点名",
            category=MemoryCategory.PREFERENCE,
            confidence=0.88,
            conversation_id="day-1",
        )
        results, latency = _search(manager, "我喜欢什么样的工作环境")
        hit = any("安静" in item.content for item in results)
        assert hit, f"preference memory was not recalled: {[item.content for item in results]}"
        return {"metrics": _metrics(recall_hit=True, recall_total=1, retrieval_latency_ms=latency)}


def evaluate_cross_conversation_growth() -> Dict[str, object]:
    with memory_manager_context() as manager:
        memory = manager.add_memory(
            "eval-user",
            "用户提到了一个新的学习项目",
            category=MemoryCategory.FACT,
            emotion=EmotionType.NEUTRAL,
            emotion_intensity=0.1,
            confidence=0.60,
            conversation_id="day-1",
        )
        initial_grade = int(memory.grade)
        manager.reinforce_memory(user_id="eval-user", memory_id=memory.id, conversation_id="day-8")
        manager.reinforce_memory(user_id="eval-user", memory_id=memory.id, conversation_id="day-16")
        manager.reinforce_memory(user_id="eval-user", memory_id=memory.id, conversation_id="day-24")
        manager.reinforce_memory(user_id="eval-user", memory_id=memory.id, conversation_id="day-32")

        history = manager.get_grade_history("eval-user", memory.id)
        assert memory.mention_count == 4, f"mention_count={memory.mention_count}"
        assert memory.independent_conversation_count == 5, (
            f"independent={memory.independent_conversation_count}, events={memory.mention_events}"
        )
        assert int(memory.grade) > initial_grade, (
            f"grade={memory.grade}, initial={initial_grade}, score={memory.degree_score}"
        )
        assert len(history) >= 3
        return {
            "metrics": {
                "grade_change": float(int(memory.grade) - initial_grade),
                "independent_conversations": float(memory.independent_conversation_count),
            },
            "details": {"audit_events": len(history)},
        }


def evaluate_short_time_suppression() -> Dict[str, object]:
    with memory_manager_context() as manager:
        memory = manager.add_memory(
            "eval-user",
            "用户今天感到有点疲惫",
            category=MemoryCategory.EMOTION,
            emotion=EmotionType.NEUTRAL,
            emotion_intensity=0.3,
            confidence=0.72,
            conversation_id="today",
        )
        for _ in range(5):
            manager.reinforce_memory(
                user_id="eval-user",
                memory_id=memory.id,
                conversation_id="today",
            )
        assert memory.mention_count == 1
        assert memory.independent_conversation_count == 1
        return {"metrics": {"mention_count": float(memory.mention_count)}}


def evaluate_inferred_grade_cap() -> Dict[str, object]:
    with memory_manager_context() as manager:
        memory = manager.add_memory(
            "eval-user",
            "用户可能害怕失去最重要的关系",
            category=MemoryCategory.RELATIONSHIP,
            emotion=EmotionType.FEARFUL,
            emotion_intensity=1.0,
            confidence=0.95,
            source_type="reflection",
            conversation_id="reflection-1",
        )
        assert int(memory.grade) <= 2
        return {"metrics": {"grade": float(memory.grade), "degree_score": memory.degree_score}}


def evaluate_duplicate_control() -> Dict[str, object]:
    with memory_manager_context() as manager:
        first = manager.add_memory(
            "eval-user",
            "用户喜欢安静的工作环境",
            category=MemoryCategory.PREFERENCE,
            confidence=0.86,
            conversation_id="day-1",
        )
        second = manager.add_memory(
            "eval-user",
            "[关于用户] 用户喜欢安静的工作环境。",
            category=MemoryCategory.PREFERENCE,
            confidence=0.86,
            conversation_id="day-2",
        )
        contents = [item.content for _, item in manager.get_user_memory_items("eval-user")]
        normalized_values = [normalized_content(content) for content in contents]
        duplicate_accepted = second.id != first.id and normalized_content(second.content) == normalized_content(first.content)
        assert not duplicate_accepted, contents
        assert len(normalized_values) == len(set(normalized_values))
        return {
            "metrics": {
                "duplicate_accepted": float(duplicate_accepted),
                "active_normalized_records": float(len(set(normalized_values))),
            }
        }


def evaluate_correction_survival() -> Dict[str, object]:
    with memory_manager_context() as manager:
        old = manager.add_memory(
            "eval-user",
            "用户住在上海",
            category=MemoryCategory.FACT,
            confidence=0.90,
            conversation_id="before-move",
        )
        replacement = manager.correct_memory("eval-user", old.id, "用户住在北京")
        new_results, latency = _search(manager, "用户现在住在哪里")
        old_results, _ = _search(manager, "上海")

        assert old.status == MemoryStatus.SUPERSEDED
        assert any(item.id == replacement.id for item in new_results)
        assert all(item.id != old.id for item in old_results)
        return {
            "metrics": _metrics(recall_hit=True, recall_total=1, retrieval_latency_ms=latency),
            "details": {"replacement_id": replacement.id},
        }


def evaluate_deletion_suppression() -> Dict[str, object]:
    with memory_manager_context() as manager:
        memory = manager.add_memory(
            "eval-user",
            "用户养了一只猫，名字叫小黑",
            category=MemoryCategory.FACT,
            confidence=0.91,
            conversation_id="pet-day",
        )
        assert manager.soft_delete_memory("eval-user", memory.id)
        results, latency = _search(manager, "小黑")
        stats = manager.get_memory_stats("eval-user")
        assert results == []
        assert stats["total"] == 0
        return {
            "metrics": _metrics(retrieval_latency_ms=latency),
            "details": {"harmful_callback": False},
        }


def evaluate_harmful_feedback_demotes() -> Dict[str, object]:
    with memory_manager_context() as manager:
        memory = manager.add_memory(
            "eval-user",
            "用户对花生严重过敏",
            category=MemoryCategory.FACT,
            emotion=EmotionType.NEUTRAL,
            emotion_intensity=0.4,
            confidence=0.94,
            conversation_id="health-day",
        )
        assert manager.set_manual_grade("eval-user", memory.id, 3, locked=False)
        updated = manager.apply_feedback("eval-user", memory.id, "wrong")
        assert updated is not None
        assert int(updated.grade) < 3
        return {
            "metrics": {"old_grade": 3.0, "new_grade": float(updated.grade)},
            "details": {"harmful_count": updated.harmful_count},
        }


def evaluate_locked_core_resists_feedback() -> Dict[str, object]:
    with memory_manager_context() as manager:
        memory = manager.add_memory(
            "eval-user",
            "用户的长期支持偏好是先倾听，不要急着给建议",
            category=MemoryCategory.PREFERENCE,
            emotion=EmotionType.NEUTRAL,
            emotion_intensity=0.3,
            confidence=0.96,
            conversation_id="support-day",
        )
        assert manager.set_manual_grade("eval-user", memory.id, 4, locked=True)
        updated = manager.apply_feedback("eval-user", memory.id, "irrelevant")
        assert updated is not None
        assert int(updated.grade) == 4
        assert updated.locked is True
        return {"metrics": {"grade": float(updated.grade), "locked": 1.0}}


def evaluate_persistence_and_audit() -> Dict[str, object]:
    directory = tempfile.TemporaryDirectory(prefix="moz-memory-eval-persist-")
    database_path = os.path.join(directory.name, "memory.db")
    try:
        first = MemoryManager(storage_path=directory.name, db_path=database_path)
        first.embedding_service.get_embedding = lambda text: None
        first.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
        memory = first.add_memory(
            "eval-user",
            "用户正在准备周四的产品面试",
            category=MemoryCategory.GOAL,
            emotion=EmotionType.ANXIOUS,
            emotion_intensity=0.62,
            confidence=0.88,
            conversation_id="interview-day",
            message_id="message-001",
        )
        expected_id = memory.id
        expected_grade = int(memory.grade)
        connection = getattr(first._local, "conn", None)
        if connection is not None:
            connection.close()

        second = MemoryManager(storage_path=directory.name, db_path=database_path)
        try:
            second.embedding_service.get_embedding = lambda text: None
            second.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
            restored = second.get_memory("eval-user", expected_id)
            assert restored is not None
            assert restored.active()
            assert restored.conversation_id == "interview-day"
            assert restored.message_id == "message-001"
            assert int(restored.grade) == expected_grade
            history = second.get_grade_history("eval-user", expected_id)
            assert any(event["event_type"] == "created" for event in history)
        finally:
            connection = getattr(second._local, "conn", None)
            if connection is not None:
                connection.close()
        return {
            "metrics": {"audit_events": float(len(history)), "grade": float(restored.grade)}
        }
    finally:
        directory.cleanup()


SCENARIOS: List[Callable[[], Dict[str, object]]] = [
    evaluate_identity_recall,
    evaluate_preference_recall,
    evaluate_cross_conversation_growth,
    evaluate_short_time_suppression,
    evaluate_inferred_grade_cap,
    evaluate_duplicate_control,
    evaluate_correction_survival,
    evaluate_deletion_suppression,
    evaluate_harmful_feedback_demotes,
    evaluate_locked_core_resists_feedback,
    evaluate_persistence_and_audit,
]


def run_memory_evaluation() -> Dict[str, object]:
    reports: List[ScenarioReport] = []
    for scenario in SCENARIOS:
        started = time.perf_counter()
        try:
            payload = scenario() or {}
            report = ScenarioReport(
                name=scenario.__name__,
                passed=True,
                metrics=payload.get("metrics", {}),
                details=payload.get("details", {}),
            )
        except Exception as exc:
            report = ScenarioReport(
                name=scenario.__name__,
                passed=False,
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
        report.metrics["scenario_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        reports.append(report)

    recall_metrics = [
        (report.metrics.get("recall_hit"), report.metrics.get("recall_total", 0))
        for report in reports
        if "recall_hit" in report.metrics
    ]
    recall_hits = sum(float(hit) for hit, _ in recall_metrics)
    recall_total = sum(float(total) for _, total in recall_metrics)
    latencies = sorted(
        float(report.metrics["retrieval_latency_ms"])
        for report in reports
        if "retrieval_latency_ms" in report.metrics
    )
    p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
    passed_count = sum(1 for report in reports if report.passed)
    return {
        "passed": passed_count == len(reports),
        "passed_scenarios": passed_count,
        "failed_scenarios": len(reports) - passed_count,
        "recall_at_3": round(recall_hits / recall_total, 3) if recall_total else 0.0,
        "duplicate_acceptance_rate": sum(
            float(report.metrics.get("duplicate_accepted", 0)) for report in reports
        ),
        "contradiction_callback_rate": sum(
            float(report.details.get("harmful_callback", 0)) for report in reports
        ),
        "correction_survival": next(
            (
                float(report.passed)
                for report in reports
                if report.name == "evaluate_correction_survival"
            ),
            0.0,
        ),
        "deletion_suppression": next(
            (
                float(report.passed)
                for report in reports
                if report.name == "evaluate_deletion_suppression"
            ),
            0.0,
        ),
        "retrieval_latency_p95_ms": round(latencies[p95_index], 3) if latencies else 0.0,
        "reports": [asdict(report) for report in reports],
    }


def main() -> int:
    report = run_memory_evaluation()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

import os
import json
import tempfile

from memory_manager import EmotionType, MemoryCategory, MemoryManager, MemoryStatus
from memory_evaluation import run_memory_evaluation


class TemporaryMemoryManager:
    def __init__(self):
        self._directory = tempfile.TemporaryDirectory()
        self.manager = MemoryManager(
            storage_path=self._directory.name,
            db_path=os.path.join(self._directory.name, "memory.db"),
        )
        self.manager.embedding_service.get_embedding = lambda text: None
        self.manager.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)

    def close(self):
        conn = getattr(self.manager._local, "conn", None)
        if conn is not None:
            conn.close()
        self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def make_manager():
    return TemporaryMemoryManager()


def test_longitudinal_memory_evaluation_suite():
    report = run_memory_evaluation()
    assert report["passed"], json.dumps(report["reports"], ensure_ascii=False)
    assert report["passed_scenarios"] >= 10
    assert report["recall_at_3"] == 1.0
    assert report["duplicate_acceptance_rate"] == 0.0
    assert report["contradiction_callback_rate"] == 0.0
    assert report["correction_survival"] == 1.0
    assert report["deletion_suppression"] == 1.0


def test_exact_duplicate_is_reinforced():
    with make_manager() as context:
        manager = context.manager
        first = manager.add_memory(
            "user",
            "用户喜欢安静的工作环境",
            category=MemoryCategory.PREFERENCE,
            emotion=EmotionType.NEUTRAL,
            emotion_intensity=0.2,
            conversation_id="conversation-1",
        )
        duplicate = manager.add_memory(
            "user",
            "用户喜欢安静的工作环境",
            category=MemoryCategory.PREFERENCE,
            emotion=EmotionType.NEUTRAL,
            emotion_intensity=0.2,
            conversation_id="conversation-1",
        )

        assert duplicate.id == first.id
        assert first.mention_count == 1


def test_short_time_repeats_do_not_inflate_frequency():
    with make_manager() as context:
        manager = context.manager
        memory = manager.add_memory(
            "user",
            "用户正在准备面试",
            category=MemoryCategory.GOAL,
            emotion=EmotionType.ANXIOUS,
            emotion_intensity=0.5,
            confidence=0.8,
            conversation_id="conversation-1",
        )
        for _ in range(3):
            manager.reinforce_memory(
                user_id="user",
                memory_id=memory.id,
                conversation_id="conversation-1",
            )

        assert memory.mention_count == 1


def test_cross_conversation_mentions_can_promote_grade():
    with make_manager() as context:
        manager = context.manager
        memory = manager.add_memory(
            "user",
            "用户在准备重要面试",
            category=MemoryCategory.GOAL,
            emotion=EmotionType.ANXIOUS,
            emotion_intensity=0.7,
            confidence=0.85,
            conversation_id="conversation-1",
        )
        old_grade = memory.grade

        manager.reinforce_memory(
            user_id="user",
            memory_id=memory.id,
            conversation_id="conversation-2",
        )

        assert memory.mention_count == 1
        assert memory.independent_conversation_count == 2
        assert memory.grade >= old_grade


def test_reflection_cannot_start_as_core():
    with make_manager() as context:
        manager = context.manager
        memory = manager.add_memory(
            "user",
            "用户可能害怕失去最重要的关系，这是核心事实",
            category=MemoryCategory.RELATIONSHIP,
            emotion=EmotionType.FEARFUL,
            emotion_intensity=1.0,
            confidence=0.95,
            source_type="reflection",
        )

        assert memory.grade <= 2


def test_correction_supersedes_old_memory():
    with make_manager() as context:
        manager = context.manager
        old = manager.add_memory("user", "用户住在上海")
        replacement = manager.correct_memory("user", old.id, "用户住在北京")

        assert replacement.supersedes_id == old.id
        assert replacement.source_type == "user_edit"
        assert old.status == MemoryStatus.SUPERSEDED
        assert len(manager.search_memories("user", "上海")) == 0


def test_soft_delete_hides_memory_and_keeps_history():
    with make_manager() as context:
        manager = context.manager
        memory = manager.add_memory("user", "用户的猫叫小黑")
        history_before_delete = manager.get_grade_history("user", memory.id)
        assert manager.soft_delete_memory("user", memory.id)

        results = manager.search_memories("user", "小黑")
        stats = manager.get_memory_stats("user")
        history_after_delete = manager.get_grade_history("user", memory.id)

        assert results == []
        assert stats["total"] == 0
        assert len(history_before_delete) >= 1
        assert len(history_after_delete) > len(history_before_delete)


def test_manual_grade_is_locked_and_audited():
    with make_manager() as context:
        manager = context.manager
        memory = manager.add_memory("user", "用户对花生严重过敏")
        assert manager.set_manual_grade("user", memory.id, 4, locked=True)

        assert memory.grade == 4
        assert memory.layer == 1
        assert memory.locked is True
        events = manager.get_grade_history("user", memory.id)
        assert any(event["event_type"] == "manual" and event["new_grade"] == 4 for event in events)

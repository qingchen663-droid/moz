"""
moz - RAG 优化 & 工作记忆 单元测试

运行: cd backend && python -m pytest tests/test_rag.py -v
"""

import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-for-rag-tests")


def _make_temp_db():
    """创建临时数据库文件，返回 (db_path, cleanup)。"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    def cleanup():
        try:
            os.unlink(db_path)
        except PermissionError:
            pass

    return db_path, cleanup


# ================================================================
# 工作记忆测试
# ================================================================

class TestWorkingMemoryStore:

    def test_save_and_load(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            store.save("user1", "用户是程序员", ["工作压力"], "anxious")
            data = store.load("user1")
            assert data["summary"] == "用户是程序员"
            assert len(data["open_topics"]) == 1
            assert data["open_topics"][0]["topic"] == "工作压力"
            assert data["current_emotion"] == "anxious"
            assert data["updated_at"] > 0
        finally:
            store._get_conn().close()
            cleanup()

    def test_load_nonexistent_user(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            data = store.load("ghost_user")
            assert data["summary"] == ""
            assert data["open_topics"] == []
            assert data["current_emotion"] == "neutral"
        finally:
            store._get_conn().close()
            cleanup()

    def test_overwrite_save(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            store.save("user1", "旧摘要", ["旧话题"], "sad")
            store.save("user1", "新摘要", ["新话题1", "新话题2"], "happy")
            data = store.load("user1")
            assert data["summary"] == "新摘要"
            assert len(data["open_topics"]) == 2
            assert data["current_emotion"] == "happy"
        finally:
            store._get_conn().close()
            cleanup()

    def test_format_for_prompt_empty(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            text = store.format_for_prompt("user1")
            assert text == ""
        finally:
            store._get_conn().close()
            cleanup()

    def test_format_for_prompt_with_data(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            store.save("user1", "用户是程序员", ["加班", "项目"], "stressed")
            text = store.format_for_prompt("user1")
            assert "用户是程序员" in text
            assert "加班" in text
            assert "stressed" in text
        finally:
            store._get_conn().close()
            cleanup()

    def test_format_for_prompt_neutral_emotion_omitted(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            store.save("user1", "摘要", [], "neutral")
            text = store.format_for_prompt("user1")
            assert "情绪" not in text
        finally:
            store._get_conn().close()
            cleanup()

    def test_multiple_users_isolated(self):
        from working_memory import WorkingMemoryStore
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            store.save("user_a", "用户A的摘要", ["话题A"], "happy")
            store.save("user_b", "用户B的摘要", ["话题B"], "sad")
            assert store.load("user_a")["summary"] == "用户A的摘要"
            assert store.load("user_b")["summary"] == "用户B的摘要"
        finally:
            store._get_conn().close()
            cleanup()


class TestRuleBasedUpdate:
    """测试工作记忆的规则更新（LLM 不可用时的降级方案）。"""

    def test_rule_update_appends_info(self):
        from working_memory import WorkingMemoryStore, _rule_based_update
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            current = {"summary": "", "open_topics": [], "current_emotion": "neutral"}
            _rule_based_update(store, "user1", current, "我今天很开心", "太好了！")
            data = store.load("user1")
            assert "开心" in data["summary"]
            assert data["current_emotion"] == "happy"
        finally:
            store._get_conn().close()
            cleanup()

    def test_rule_update_short_message_skipped(self):
        from working_memory import WorkingMemoryStore, _rule_based_update
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            current = {"summary": "", "open_topics": [], "current_emotion": "neutral"}
            _rule_based_update(store, "user1", current, "嗯", "好的")
            data = store.load("user1")
            assert data["summary"] == ""
        finally:
            store._get_conn().close()
            cleanup()

    def test_rule_update_truncates_long_summary(self):
        from working_memory import WorkingMemoryStore, _rule_based_update
        db_path, cleanup = _make_temp_db()
        try:
            store = WorkingMemoryStore(db_path=db_path)
            long_summary = "这是一段很长的摘要" * 50
            current = {"summary": long_summary, "open_topics": [], "current_emotion": "neutral"}
            _rule_based_update(store, "user1", current, "我今天加班了很累", "辛苦了")
            data = store.load("user1")
            assert len(data["summary"]) <= 400
        finally:
            store._get_conn().close()
            cleanup()


# ================================================================
# 混合检索 + RRF + Reranking 测试
# ================================================================

class TestHybridSearch:

    def _make_mm(self):
        """创建一个使用临时数据库的 MemoryManager。"""
        from memory_manager import MemoryManager, EmotionAnalyzer, ImportanceScorer, EmbeddingService
        import threading

        db_path, cleanup = _make_temp_db()
        mm = MemoryManager.__new__(MemoryManager)
        mm.db_path = db_path
        mm._local = type('Local', (), {'conn': None})()
        mm._conn_lock = threading.Lock()
        mm._state_lock = threading.RLock()
        mm.emotion_analyzer = EmotionAnalyzer()
        mm.importance_scorer = ImportanceScorer()
        mm.embedding_service = EmbeddingService()
        mm.memories = {}
        mm._dirty = set()
        mm._init_db()
        return mm, db_path, cleanup

    def test_search_empty_memories(self):
        mm, db_path, cleanup = self._make_mm()
        try:
            results = mm.search_memories("user1", "你好")
            assert results == []
        finally:
            mm._get_conn().close()
            cleanup()

    def test_keyword_search_raw_returns_results(self):
        """关键词检索应返回匹配的记忆（查询需 >=2 字符）。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户喜欢猫", category=MemoryCategory.PREFERENCE)
            results = mm._keyword_search_raw(mm._get_user_memories("user1"), "喜欢猫", None, 0.0)
            assert len(results) > 0
            assert "猫" in results[0][1].content
        finally:
            mm._get_conn().close()
            cleanup()

    def test_keyword_search_raw_no_match(self):
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户喜欢猫", category=MemoryCategory.PREFERENCE)
            results = mm._keyword_search_raw(mm._get_user_memories("user1"), "量子物理", None, 0.0)
            assert len(results) == 0
        finally:
            mm._get_conn().close()
            cleanup()

    def test_semantic_search_raw_no_crash(self):
        """Embedding 不可用时应返回空列表（不崩溃）。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户喜欢猫", category=MemoryCategory.PREFERENCE)
            results = mm._semantic_search_raw(mm._get_user_memories("user1"), "喜欢猫", None, 0.0)
            assert isinstance(results, list)
        finally:
            mm._get_conn().close()
            cleanup()

    def test_rrf_merges_both_channels(self):
        """RRF 应融合语义和关键词两路结果。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户喜欢猫和狗", category=MemoryCategory.PREFERENCE)
            mm.add_memory("user1", "用户是一名程序员", category=MemoryCategory.FACT)

            results = mm.search_memories("user1", "喜欢猫")
            assert len(results) >= 1
            found = any("猫" in m.content for m in results)
            assert found, f"未找到包含'猫'的记忆，返回: {[m.content for m in results]}"
        finally:
            mm._get_conn().close()
            cleanup()

    def test_search_with_multi_queries(self):
        """多查询检索应召回更多相关记忆。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户工作压力大", category=MemoryCategory.CONCERN)
            mm.add_memory("user1", "用户喜欢听音乐", category=MemoryCategory.PREFERENCE)

            results_single = mm.search_memories("user1", "压力大", limit=5)
            results_multi = mm.search_memories("user1", "压力大", limit=5, queries=["压力大", "工作压力", "用户困扰"])
            assert len(results_multi) >= len(results_single)
        finally:
            mm._get_conn().close()
            cleanup()

    def test_rerank_prefers_recent(self):
        """Reranking 应偏好近期记忆。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryItem
            now = time.time()

            old_mem = MemoryItem(
                id="old", content="用户喜欢猫", importance=0.5,
                created_at=now - 720 * 3600, last_accessed=now - 720 * 3600,
            )
            new_mem = MemoryItem(
                id="new", content="用户喜欢狗", importance=0.5,
                created_at=now - 1, last_accessed=now - 1,
            )
            mm._get_user_memories("user1")["old"] = old_mem
            mm._get_user_memories("user1")["new"] = new_mem

            candidates = [(0.01, old_mem), (0.01, new_mem)]
            ranked = mm._rerank(candidates, "喜欢什么")
            assert ranked[0].id == "new", "近期记忆应排在前面"
        finally:
            mm._get_conn().close()
            cleanup()

    def test_rerank_prefers_emotion_match(self):
        """Reranking 应偏好情感匹配的记忆。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryItem, EmotionType
            now = time.time()

            anxious_mem = MemoryItem(
                id="anxious", content="用户工作压力大", importance=0.5,
                emotion=EmotionType.ANXIOUS, created_at=now, last_accessed=now,
            )
            happy_mem = MemoryItem(
                id="happy", content="用户周末很开心", importance=0.5,
                emotion=EmotionType.HAPPY, created_at=now, last_accessed=now,
            )
            mm._get_user_memories("user1")["anxious"] = anxious_mem
            mm._get_user_memories("user1")["happy"] = happy_mem

            candidates = [(0.01, anxious_mem), (0.01, happy_mem)]
            ranked = mm._rerank(candidates, "我很焦虑怎么办")
            assert ranked[0].id == "anxious", "情感匹配的记忆应排在前面"
        finally:
            mm._get_conn().close()
            cleanup()

    def test_search_updates_access_count(self):
        """检索后应更新 access_count 和 last_accessed。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户喜欢猫", category=MemoryCategory.PREFERENCE)
            mem_before = mm._get_user_memories("user1")
            mid = list(mem_before.keys())[0]
            count_before = mem_before[mid].access_count

            mm.search_memories("user1", "喜欢猫")
            count_after = mem_before[mid].access_count
            assert count_after > count_before, "检索后 access_count 应增加"
        finally:
            mm._get_conn().close()
            cleanup()

    def test_search_respects_limit(self):
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            for i in range(10):
                mm.add_memory("user1", f"用户喜欢猫的第{i}条记录", category=MemoryCategory.PREFERENCE)
            results = mm.search_memories("user1", "喜欢猫", limit=3)
            assert len(results) <= 3
        finally:
            mm._get_conn().close()
            cleanup()

    def test_search_respects_min_importance(self):
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            mm.add_memory("user1", "用户喜欢猫", category=MemoryCategory.PREFERENCE)
            results = mm.search_memories("user1", "喜欢猫", min_importance=0.99)
            assert len(results) == 0
        finally:
            mm._get_conn().close()
            cleanup()

    def test_search_batches_and_caches_query_embeddings(self):
        """重复查询应复用查询向量，改写查询应一次批量请求。"""
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryCategory
            memory = mm.add_memory("user1", "用户喜欢猫", category=MemoryCategory.PREFERENCE)
            calls = []
            mm.embedding_service.get_embeddings_batch = lambda texts: (calls.append(list(texts)) or [[1.0, 0.0] for _ in texts])
            mm.embedding_service.get_embedding = lambda text: [1.0, 0.0]
            memory.embedding = [1.0, 0.0]
            mm.search_memories("user1", "喜欢猫", queries=["喜欢猫", "用户偏好"])
            mm.search_memories("user1", "喜欢猫", queries=["喜欢猫", "用户偏好"])
            assert calls[0] == ["喜欢猫", "用户偏好"]
            assert sum(1 for batch in calls if batch == ["喜欢猫", "用户偏好"]) == 1
        finally:
            mm._get_conn().close()
            cleanup()

    def test_search_index_cache_reuses_matrix(self):
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryItem
            memories = mm._get_user_memories("user1")
            memories["a"] = MemoryItem(id="a", content="猫", embedding=[1.0, 0.0])
            first_ids, first_matrix = mm._build_search_index("user1")
            second_ids, second_matrix = mm._build_search_index("user1")
            assert first_ids == second_ids == ["a"]
            assert first_matrix is second_matrix
            assert mm.get_search_metrics()["cache_hits"] >= 1
        finally:
            mm._get_conn().close()
            cleanup()

    def test_keyword_search_uses_idf_to_prefer_specific_terms(self):
        mm, db_path, cleanup = self._make_mm()
        try:
            from memory_manager import MemoryItem
            memories = mm._get_user_memories("user1")
            memories["generic"] = MemoryItem(id="generic", content="用户喜欢猫和狗")
            memories["specific"] = MemoryItem(id="specific", content="用户喜欢猫")
            results = mm._keyword_search_raw(memories, "猫", None, 0.0)
            assert results[0][1].id == "specific"
        finally:
            mm._get_conn().close()
            cleanup()


# ================================================================
# 查询改写测试
# ================================================================

class TestQueryRewrite:
    """查询改写：离线钉住。

    第十三轮发现这四个用例里有两条**真的在打中转**（9.27s / 7.09s，整套 pytest 的
    大头就是它们），而 rewrite_query 会吞掉一切异常 —— 所以中转挂了它们照样绿，
    等于"花 16 秒买了个假通过"。现在一律 monkeypatch 掉模型客户端。
    """

    @staticmethod
    def _fake_llm(monkeypatch, content="[]", delay=0.0):
        import llm_config

        class Fake:
            def invoke(self, msgs):
                if delay:
                    time.sleep(delay)
                return type("R", (), {"content": content})()

        monkeypatch.setattr(llm_config, "get_llm_client", lambda **kw: Fake())

    def test_rewrite_short_query(self):
        from memory_manager import rewrite_query
        assert rewrite_query("") == [""]

    def test_rewrite_single_char(self):
        from memory_manager import rewrite_query
        assert rewrite_query("好") == ["好"]

    def test_rewrite_returns_list(self, monkeypatch):
        from memory_manager import rewrite_query
        self._fake_llm(monkeypatch, content='["心情不好", "压力"]')
        result = rewrite_query("我今天心情不好")
        assert isinstance(result, list) and len(result) >= 1
        assert "我今天心情不好" in result
        assert "压力" in result

    def test_rewrite_preserves_original_and_caps(self, monkeypatch):
        from memory_manager import rewrite_query
        self._fake_llm(monkeypatch, content='["a", "b", "c", "d", "e"]')
        result = rewrite_query("我最近压力好大")
        assert result[0] == "我最近压力好大"
        assert len(result) <= 4, f"改写没封顶：{result}"

    def test_slow_rewrite_does_not_block_the_search(self, monkeypatch):
        """超时必须真的把调用方放走。

        以前写成 with ThreadPoolExecutor(...)：future.result(5) 是超时了，
        但退出 with 还要等工作线程跑完，实测模型 8 秒回、调用方等 8.00 秒。
        """
        import memory_manager as MM
        self._fake_llm(monkeypatch, content='["压力"]', delay=3.0)
        monkeypatch.setattr(MM, "QUERY_REWRITE_TIMEOUT", 0.4)
        t0 = time.time()
        result = MM.rewrite_query("我最近压力好大")
        spent = time.time() - t0
        assert result == ["我最近压力好大"]
        assert spent < 1.5, f"超时没截住，调用方白等了 {spent:.2f}s"

    def test_broken_rewrite_output_falls_back(self, monkeypatch):
        from memory_manager import rewrite_query
        self._fake_llm(monkeypatch, content="这不是 JSON")
        assert rewrite_query("我最近压力好大") == ["我最近压力好大"]



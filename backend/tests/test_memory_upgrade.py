"""
记忆系统升级 v2 单元测试

测试覆盖：
1. UserProfile 和 ProfileManager
2. TemporalMetadata 和 TemporalExtractor
3. MemoryLayer
4. 数据库迁移脚本
"""

import os
import sys
import json
import time
import tempfile
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from user_profile import UserProfile, ProfileManager
from temporal_metadata import TemporalMetadata, TemporalExtractor
from memory_layer import MemoryLayer


class TestUserProfile:
    """测试 UserProfile 数据结构"""

    def test_profile_creation(self):
        profile = UserProfile(user_id="test_user")
        assert profile.user_id == "test_user"
        assert profile.version == 1
        assert profile.identity == {}
        assert profile.preferences == {}

    def test_profile_to_dict(self):
        profile = UserProfile(
            user_id="test_user",
            identity={"name": "小明", "age": 25},
            preferences={"hobbies": ["编程", "旅行"]},
        )
        d = profile.to_dict()
        assert d["identity"]["name"] == "小明"
        assert d["preferences"]["hobbies"] == ["编程", "旅行"]

    def test_profile_from_dict(self):
        data = {
            "identity": {"name": "小明"},
            "preferences": {"hobbies": ["编程"]},
        }
        profile = UserProfile.from_dict("test_user", data)
        assert profile.user_id == "test_user"
        assert profile.identity["name"] == "小明"

    def test_profile_to_prompt_context(self):
        profile = UserProfile(
            user_id="test_user",
            identity={"name": "小明", "age": 25, "occupation": "程序员"},
            preferences={"hobbies": ["编程", "旅行"]},
            emotional_profile={"recent_mood_trend": "比较好"},
        )
        context = profile.to_prompt_context()
        assert "小明" in context
        assert "25岁" in context
        assert "程序员" in context
        assert "编程" in context
        assert "比较好" in context

    def test_profile_to_prompt_context_empty(self):
        profile = UserProfile(user_id="test_user")
        context = profile.to_prompt_context()
        assert context == ""


class TestProfileManager:
    """测试 ProfileManager"""

    def test_save_and_load(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            manager = ProfileManager(db_path)
            
            # 创建并保存档案卡
            profile = UserProfile(
                user_id="test_user",
                identity={"name": "小明"},
            )
            manager.save_profile(profile)

            # 加载档案卡
            loaded = manager.get_profile("test_user")
            assert loaded is not None
            assert loaded.identity["name"] == "小明"

            manager.close()
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass  # Windows 文件锁

    def test_cache_hit(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            manager = ProfileManager(db_path)
            
            profile = UserProfile(user_id="test_user", identity={"name": "小明"})
            manager.save_profile(profile)

            # 第一次加载（缓存未命中）
            loaded1 = manager.get_profile("test_user")
            assert loaded1.identity["name"] == "小明"

            # 修改数据库中的值
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE user_profiles SET profile_data = ? WHERE user_id = ?",
                (json.dumps({"identity": {"name": "小红"}}), "test_user")
            )
            conn.commit()
            conn.close()

            # 第二次加载（缓存命中，应该还是旧值）
            loaded2 = manager.get_profile("test_user")
            assert loaded2.identity["name"] == "小明"

            manager.close()
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass

    def test_nonexistent_profile(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            manager = ProfileManager(db_path)
            profile = manager.get_profile("nonexistent")
            assert profile is None
            manager.close()
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


class TestTemporalExtractor:
    """测试时间提取器"""

    def test_relative_time_yesterday(self):
        text = "昨天我去公园散步"
        metadata = TemporalExtractor.extract_from_text(text)
        
        assert metadata.event_time["description"] == "昨天"
        assert metadata.event_time["precision"] == "relative"
        
        expected_date = datetime.now() - timedelta(days=1)
        actual_date = datetime.fromtimestamp(metadata.event_time["timestamp"])
        assert actual_date.date() == expected_date.date()

    def test_compound_time_last_summer(self):
        text = "去年夏天我去海边玩"
        metadata = TemporalExtractor.extract_from_text(text)
        
        assert metadata.event_time["description"] == "去年夏天"
        assert metadata.time_context["season"] == "夏天"
        
        expected_year = datetime.now().year - 1
        actual_year = datetime.fromtimestamp(metadata.event_time["timestamp"]).year
        assert actual_year == expected_year

    def test_season_mapping(self):
        # 测试"夏天"
        metadata1 = TemporalExtractor.extract_from_text("夏天我去海边")
        assert metadata1.time_context["season"] == "夏天"
        
        # 测试"夏季"
        metadata2 = TemporalExtractor.extract_from_text("夏季天气很热")
        assert metadata2.time_context["season"] == "夏季"
        
        # 测试"冬天"
        metadata3 = TemporalExtractor.extract_from_text("冬天很冷")
        assert metadata3.time_context["season"] == "冬天"
        
        # 测试"冬季"
        metadata4 = TemporalExtractor.extract_from_text("冬季下雪")
        assert metadata4.time_context["season"] == "冬季"

    def test_life_stage(self):
        text = "大学时期我经常去图书馆"
        metadata = TemporalExtractor.extract_from_text(text)
        assert metadata.time_context["life_stage"] == "大学时期"

    def test_recurrence(self):
        text = "我每天都会看书"
        metadata = TemporalExtractor.extract_from_text(text)
        assert metadata.recurrence["is_recurring"] is True
        assert metadata.recurrence["frequency"] == "daily"

    def test_no_temporal_info(self):
        text = "我喜欢编程"
        metadata = TemporalExtractor.extract_from_text(text)
        assert metadata.event_time == {}
        assert metadata.time_context == {}
        assert metadata.recurrence == {}


class TestTemporalMetadata:
    """测试 TemporalMetadata"""

    def test_to_dict_and_from_dict(self):
        metadata = TemporalMetadata(
            event_time={"timestamp": time.time(), "description": "昨天"},
            time_context={"season": "夏天"},
            recurrence={"is_recurring": True, "frequency": "daily"},
        )
        
        d = metadata.to_dict()
        loaded = TemporalMetadata.from_dict(d)
        
        assert loaded.event_time["description"] == "昨天"
        assert loaded.time_context["season"] == "夏天"
        assert loaded.recurrence["is_recurring"] is True

    def test_get_age_in_days(self):
        # 创建 2 天前的时间戳
        two_days_ago = time.time() - 2 * 86400
        metadata = TemporalMetadata(
            event_time={"timestamp": two_days_ago}
        )
        
        age = metadata.get_age_in_days()
        assert age is not None
        assert abs(age - 2.0) < 0.1

    def test_is_recent(self):
        # 创建 3 天前的时间戳
        three_days_ago = time.time() - 3 * 86400
        metadata = TemporalMetadata(
            event_time={"timestamp": three_days_ago}
        )
        
        assert metadata.is_recent(days=7) is True
        assert metadata.is_recent(days=2) is False


class TestMemoryLayer:
    """测试 MemoryLayer"""

    def test_from_importance_core(self):
        layer = MemoryLayer.from_importance(importance=0.9, emotion_intensity=0.5)
        assert layer == MemoryLayer.CORE

    def test_from_importance_important(self):
        layer = MemoryLayer.from_importance(importance=0.7, emotion_intensity=0.5)
        assert layer == MemoryLayer.IMPORTANT

    def test_from_importance_regular(self):
        layer = MemoryLayer.from_importance(importance=0.3, emotion_intensity=0.3)
        assert layer == MemoryLayer.REGULAR

    def test_from_importance_high_emotion(self):
        layer = MemoryLayer.from_importance(importance=0.5, emotion_intensity=0.9)
        assert layer == MemoryLayer.CORE

    def test_get_forgetting_strength(self):
        assert MemoryLayer.CORE.get_forgetting_strength() == 10.0
        assert MemoryLayer.IMPORTANT.get_forgetting_strength() == 2.0
        assert MemoryLayer.REGULAR.get_forgetting_strength() == 1.0

    def test_get_retrieval_weight(self):
        assert MemoryLayer.CORE.get_retrieval_weight() == 1.0
        assert MemoryLayer.IMPORTANT.get_retrieval_weight() == 0.8
        assert MemoryLayer.REGULAR.get_retrieval_weight() == 0.5


class TestMigration:
    """测试数据库迁移脚本"""

    def test_migration_v1_to_v2(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name

        try:
            # 创建 v1 数据库
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE memories (
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (user_id, memory_id)
                )
            """)

            # 插入测试数据
            test_data_1 = {
                "id": "test_1",
                "content": "用户喜欢编程",
                "importance": 0.9,
                "emotion_intensity": 0.5,
            }
            test_data_2 = {
                "id": "test_2",
                "content": "用户非常焦虑",
                "importance": 0.5,
                "emotion_intensity": 0.9,
            }
            conn.execute(
                "INSERT INTO memories (user_id, memory_id, data) VALUES (?, ?, ?)",
                ("test_user", "test_1", json.dumps(test_data_1))
            )
            conn.execute(
                "INSERT INTO memories (user_id, memory_id, data) VALUES (?, ?, ?)",
                ("test_user", "test_2", json.dumps(test_data_2))
            )
            conn.commit()
            conn.close()

            # 执行迁移
            from migration_v2 import migrate_to_v2
            migrate_to_v2(db_path)

            # 验证
            conn = sqlite3.connect(db_path)

            # 检查 schema_version
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()]
            assert 1 in versions, "应该补上 v1 记录"
            assert 2 in versions, "应该有 v2 记录"

            # 检查 memories 表新字段
            row1 = conn.execute(
                "SELECT data FROM memories WHERE user_id = ? AND memory_id = ?",
                ("test_user", "test_1")
            ).fetchone()
            data1 = json.loads(row1[0])
            assert "layer" in data1, "应该有 layer 字段"
            assert data1["layer"] == 1, "importance=0.9 应该是核心记忆"

            row2 = conn.execute(
                "SELECT data FROM memories WHERE user_id = ? AND memory_id = ?",
                ("test_user", "test_2")
            ).fetchone()
            data2 = json.loads(row2[0])
            assert data2["layer"] == 1, "emotion_intensity=0.9 应该是核心记忆"

            conn.close()
        finally:
            os.unlink(db_path)




class TestProfileAccumulation:
    """档案卡的列表要累加。

    提取提示词让模型"数组字段返回完整数组"，可它每轮只看得到那一句话，
    给的"完整"只是本轮新出现的那几个 —— 照着覆盖就会把上次记的抹掉
    （第十二轮实测：连说三个爱好只剩最后一个，说两位家人只剩一位）。
    """

    def test_merge_accumulates_lists_and_replaces_scalars(self):
        from user_profile import merge_profile_value

        assert merge_profile_value(["编程"], ["旅行"]) == ["编程", "旅行"]
        assert merge_profile_value(["编程", "旅行"], ["编程"]) == ["编程", "旅行"]   # 不攒重复
        assert merge_profile_value(None, ["钓鱼"]) == ["钓鱼"]
        assert merge_profile_value("程序员", "设计师") == "设计师"                    # 标量以新的为准
        fam = [{"relation": "妈妈", "description": "喜欢养花"}]
        merged = merge_profile_value(fam, [{"relation": "妈妈", "description": "爱跳广场舞"}])
        assert len(merged) == 1 and merged[0]["description"] == "爱跳广场舞"          # 同一人不新增
        merged2 = merge_profile_value(merged, [{"relation": "姐姐", "description": "在念书"}])
        assert [x["relation"] for x in merged2] == ["妈妈", "姐姐"]

    def test_three_turns_keep_three_hobbies(self):
        import json as _json
        from user_profile import ProfileUpdater

        class StubClient:
            def __init__(self, payload):
                self.payload = payload

            def invoke(self, messages):
                return type("R", (), {"content": _json.dumps(self.payload, ensure_ascii=False)})()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            db_path = f.name
        try:
            manager = ProfileManager(db_path)
            updater = ProfileUpdater(manager)
            for payload in ({"preferences.hobbies": ["编程"]},
                            {"preferences.hobbies": ["旅行"]},
                            {"preferences.hobbies": ["钓鱼"]}):
                updater.update_from_conversation("acc_user", "随便说一句", "嗯", StubClient(payload))
            profile = manager.get_profile("acc_user")
            hobbies = profile.preferences.get("hobbies")
            assert hobbies == ["编程", "旅行", "钓鱼"], f"爱好被后一轮覆盖成：{hobbies}"
            assert "编程" in profile.to_prompt_context() and "钓鱼" in profile.to_prompt_context()
            manager.close()
        finally:
            try:
                os.unlink(db_path)
            except PermissionError:
                pass


def test_scalar_back_into_a_list_field_appends():
    """模型有时把 hobbies 回成单个字符串，那也算追加，不许把整张表换掉。"""
    from user_profile import merge_profile_value

    assert merge_profile_value(["编程", "旅行"], "钓鱼") == ["编程", "旅行", "钓鱼"]
    assert merge_profile_value(["编程"], "编程") == ["编程"]


def test_profile_refuses_empty_shells():
    """模型只回一个称谓、名字和描述全空时，别在档案卡上留一行空白。

    第十三轮沙箱实测存成了：
    family = [{"relation":"宠物","name":"团子","description":"五岁橘猫"},
              {"relation":"母亲","name":"","description":""}]
    第二条在界面上就是"母亲"后面什么都没有。
    """
    from user_profile import merge_profile_value

    fam = [{"relation": "宠物", "name": "团子", "description": "五岁橘猫"}]
    assert merge_profile_value(fam, [{"relation": "母亲", "name": "", "description": ""}]) == fam
    assert merge_profile_value(fam, ["", None]) == fam
    assert merge_profile_value(["编程"], ["旅行", ""]) == ["编程", "旅行"]   # 混在里面的空串丢掉
    assert merge_profile_value("小明", "") == "小明"       # 空值不许把已有的抹掉
    assert merge_profile_value(None, "") is None
    after = merge_profile_value(fam, [{"relation": "妈妈", "name": "王芳"}])
    assert len(after) == 2 and after[-1]["name"] == "王芳"   # 有名字就不算空壳


def test_question_answer_pair_leaves_no_memory(tmp_path):
    """用户问一句、她答一句，那句回答不该变成一条"关于用户的事实"。

    第十四轮实测：上一轮沙箱 11 条长期记忆里 4 条是 `[对话摘要] AI回复要点：…`
    （全是答提问时存下来的），检索前 5 名里她们家回声占 12/30。
    用户那句是陈述时照旧成对存（用户说 + 她的要点），这条不许退化。
    """
    from memory_manager import MemoryManager, MemoryCategory

    m = MemoryManager(storage_path=str(tmp_path), db_path=str(tmp_path / "m.db"))
    try:
        m.embedding_service.get_embedding = lambda text: None
        m._extract_facts = lambda *a, **k: []          # 单测不许打中转
        m.extract_and_store_facts("q_user", "我家猫叫什么来着？", "团子呀，五岁的橘猫～",
                                  category=MemoryCategory.FACT)
        assert list(m._get_user_memories("q_user").values()) == [], "一问一答存出了记忆"

        m.extract_and_store_facts("s_user", "我最近在学做酸菜鱼", "先少放点辣椒，别辣到胃",
                                  category=MemoryCategory.FACT)
        stored = [x.content for x in m._get_user_memories("s_user").values()]
        assert any("酸菜鱼" in c for c in stored), stored
        assert any("AI回复要点" in c for c in stored), stored
    finally:
        conn = getattr(getattr(m, "_local", None), "conn", None)
        if conn is not None:
            conn.close()          # MemoryManager 没有 _conn()，连接挂在线程本地


def test_extraction_path_skips_empty_values(tmp_path):
    """整条落地路径也不许写空值：identity.nickname="" 以前会真的存成空串。"""
    from user_profile import ProfileManager, ProfileUpdater, UserProfile

    manager = ProfileManager(str(tmp_path / "p.db"))
    try:
        prof = UserProfile(user_id="shell_probe")
        updater = ProfileUpdater(manager)
        updater._apply_extractions(prof, {
            "identity.nickname": "",
            "relationships.family": [{"relation": "母亲", "name": "", "description": ""}],
            "preferences.food": "讨厌香菜",
        })
        assert prof.identity == {}
        assert prof.relationships == {}
        assert prof.preferences.get("food") == "讨厌香菜"
    finally:
        manager.close()




if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

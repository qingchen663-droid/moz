"""检索/维护改动守卫测试（2026-10-06）

覆盖三件改了就可能悄悄坏掉的事：
1. 查询向量探测不再占首句（背景化），且服务冷却时计数如实；
2. 容量维护不再在写路径上同步跑；
3. `find_clusters` 换矩阵算法后与原两两余弦**逐位等价**；
4. 同义扩展只加不减（候选集是超集），关掉时行为与原实现一致。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

import memory_synonyms
from memory_manager import MemoryManager, MemoryItem
import memory_consolidation as MC


def make_manager(tmp_path, *, with_vectors=False):
    mgr = MemoryManager(storage_path=str(tmp_path / "s"), db_path=str(tmp_path / "m.db"))
    mgr.embedding_service._client = None          # 测试一律不发网络
    return mgr


def seed(mgr, user, contents, embeddings=None):
    store = mgr._get_user_memories(user)
    for i, content in enumerate(contents):
        mid = "s%03d" % i
        item = MemoryItem(id=mid, content=content, importance=0.6)
        if embeddings is not None:
            item.embedding = embeddings[i]
        store[mid] = item
        mgr._dirty.add((user, mid))
    mgr._save_to_disk()
    mgr._invalidate_search_cache(user)
    return store


QUERY = "豆子是谁"


class SlowEmbeddingService:
    """假装向量服务在位，但每次批量请求都要 0.6 秒——用来抓"首句等网络"的回归。

    分得清两类请求：查询向量探测（批次里含查询原文）与整库补算（批次里是记忆正文）。
    """

    def __init__(self):
        self.calls = 0
        self.query_calls = 0
        self._disabled_until = 0.0
        self.tried = 0
        self.failed = 0

    def available(self):
        return True

    def get_embeddings_batch(self, texts):
        self.calls += 1
        if any((t or "") == QUERY for t in texts):      # 只认查询串本身，别把补算批次算进来
            self.query_calls += 1
        time.sleep(0.6)
        return [[0.1, 0.2, 0.3] for _ in texts]

    def get_embedding(self, text):
        return self.get_embeddings_batch([text])[0]


def test_query_probe_does_not_block_first_sentence(tmp_path):
    mgr = make_manager(tmp_path)
    seed(mgr, "u", ["用户喜欢下雨天", "用户的猫叫豆子"])
    slow = SlowEmbeddingService()
    mgr.embedding_service = slow

    elapsed = []
    for _ in range(3):                       # 连着问三句同一问题
        started = time.perf_counter()
        mgr.search_memories("u", QUERY, limit=3)
        elapsed.append(time.perf_counter() - started)

    assert max(elapsed) < 0.3, "首句不该等 0.6 秒的向量往返（实测最慢 %.2fs）" % max(elapsed)
    # 探测确实发了，但单飞：三句话不该发出三次查询向量请求
    assert slow.query_calls <= 1, "查询向量探测应单飞，实测发了 %d 次" % slow.query_calls
    assert mgr._search_metrics["embed_probe"] >= 1
    deadline = time.time() + 5
    cached = {}
    while time.time() < deadline:
        with mgr._state_lock:
            cached = dict(mgr._query_embedding_cache)
        if QUERY in cached:
            break
        time.sleep(0.05)
    assert QUERY in cached, "后台探测应把查询向量落进缓存，下一句起语义路生效"


def test_probe_is_skipped_and_counted_while_cooling(tmp_path):
    mgr = make_manager(tmp_path)
    seed(mgr, "u", ["用户爱喝美式"])

    class Cooling(SlowEmbeddingService):
        def available(self):
            return False

    mgr.embedding_service = Cooling()
    before = mgr.get_search_metrics()["embed_skip"]
    mgr.search_memories("u", "喝什么", limit=3)
    after = mgr.get_search_metrics()["embed_skip"]
    assert after > before, "冷却期内应记一次 skip，而不是静默当没发生"


def test_add_memory_does_not_run_maintenance_inline(tmp_path):
    mgr = make_manager(tmp_path)
    seed(mgr, "u", ["条目 %d 内容" % i for i in range(30)])
    runs = {"n": 0}

    def slow_maintain(user_id):
        runs["n"] += 1
        time.sleep(0.8)

    mgr.auto_maintain = slow_maintain
    mgr.CONSOLIDATION_TRIGGER = 10          # 已经远超阈值
    mgr.MAX_ACTIVE_MEMORIES = 100

    started = time.perf_counter()
    mgr.add_memory(user_id="u", content="不该被维护卡住的写入")
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, "写句不该同步等维护（实测 %.2fs）" % elapsed

    res = mgr.drain_maintenance_due(timeout_s=10)
    assert res["drained"] >= 1, "登记过的待办必须被跑掉，返回值不能假装 0 就是干净"
    assert not res.get("timed_out")
    assert runs["n"] == res["drained"] >= 1


def test_find_clusters_matches_pairwise_reference(tmp_path):
    """换算法不许改结果：与原两两余弦实现逐条比对簇的成员与顺序。"""
    mgr = make_manager(tmp_path)
    n, dim = 60, 32
    rng = np.random.RandomState(5)
    centers = [rng.randn(dim).astype(float) for _ in range(n // 6)]   # 每组一个中心
    embeddings = []
    for i in range(n):
        center = centers[i // 6]
        v = center + (0.0 if i % 6 == 0 else 0.02 * rng.randn(dim))   # 组内彼此接近、组间几乎正交
        embeddings.append([float(x) for x in v])
    seed(mgr, "u", ["片段 %d 各不一样" % i for i in range(n)], embeddings=embeddings)

    def reference(user_id):
        cands = [m for m in mgr._get_user_memories(user_id).values()
                 if m.active() and not m.is_consolidated and m.embedding is not None and not m.locked]
        if len(cands) < MC.MIN_CLUSTER_SIZE:
            return []
        assigned, out = set(), []
        for i, seed_item in enumerate(cands):
            if seed_item.id in assigned:
                continue
            cluster = [seed_item]
            for other in cands[i + 1:]:
                if other.id in assigned or other.embedding is None:
                    continue
                sim = float(np.dot(seed_item.embedding, other.embedding) /
                            (np.linalg.norm(seed_item.embedding) * np.linalg.norm(other.embedding)))
                if sim >= MC.CLUSTER_SIMILARITY:
                    cluster.append(other)
            if len(cluster) >= MC.MIN_CLUSTER_SIZE:
                out.append(cluster)
                assigned.update(m.id for m in cluster)
        return out

    new = [[m.id for m in c] for c in MC.MemoryConsolidator(mgr).find_clusters("u")]
    old = [[m.id for m in c] for c in reference("u")]
    assert new == old, "簇划分发生变化：新实现 = %d 组 / 参考 = %d 组" % (len(new), len(old))
    assert len(new) >= 2, "这份数据应至少能聚出两组，否则本测试什么都没测"


def test_synonym_expansion_only_widens_candidates(tmp_path):
    mgr = make_manager(tmp_path)
    contents = [
        "用户不吃辣，胃受不了",
        "用户习惯用中文写注释",
        "用户每周五晚上去健身房",
        "无关的一条：订单号 88231 的发货地址缺失",
    ]
    seed(mgr, "u", contents)
    query = "他什么时候会去锻炼"          # 「锻炼」与正文「健身房」靠同义组相连

    def ids(enabled):
        memory_synonyms.ENABLED = enabled
        MemoryManager._prepare_query.cache_clear()
        try:
            found = mgr._keyword_search_raw(dict(mgr._get_user_memories("u")), query, None, 0.0, user_id="u")
            return {m.id for _, m in found}
        finally:
            memory_synonyms.ENABLED = True
            MemoryManager._prepare_query.cache_clear()

    off_ids = ids(False)
    on_ids = ids(True)
    assert off_ids <= on_ids, "扩展只允许加候选，不许把原来的命中剪掉：%s" % (off_ids - on_ids)


def test_synonym_disabled_matches_legacy_prepared_query():
    """反向对照：关掉开关时，打分词表必须与原实现逐位一致（否则开关是假的）。"""
    MemoryManager._prepare_query.cache_clear()
    memory_synonyms.ENABLED = False
    try:
        prepared = MemoryManager._prepare_query("他喜欢吃什么")
    finally:
        memory_synonyms.ENABLED = True
        MemoryManager._prepare_query.cache_clear()
    assert prepared["expanded"] == 0
    assert [t for t, w in prepared["scored"]] == prepared["tokens"]
    assert all(w == 1.0 for _, w in prepared["scored"])


def test_condition_derivation_is_deterministic_and_conservative():
    from memory_conditions import derive_condition
    assert derive_condition("除非出差，用户周五晚上去健身房") == ("", "出差")
    assert derive_condition("当胃不舒服的时候，用户不吃辣") == ("胃不舒服", "")
    # 截断连接词：条件不该把后半句一起吞进来
    assert derive_condition("除非出差否则六点接孩子") == ("", "出差")
    # 口语里最常见的"如果X就Y"必须抽得到
    assert derive_condition("如果下雨就不跑步了") == ("下雨", "")
    assert derive_condition("用户喜欢下雨天") == ("", "")
    # 泛指词当条件没有区分度，抽到也不采信
    assert derive_condition("除非去那种地方否则不出门") == ("", "")


def test_short_conditions_use_substring_single_char_works():
    """单字条件（如"辣"）走子串判断；≤2 字条件必须整串出现，别拿二元组比例误剔。"""
    from memory_conditions import conflicts_with_query, prepare_query
    assert conflicts_with_query("辣", prepare_query("今天吃辣吗")), "单字条件永远判不出冲突是回归"
    assert conflicts_with_query("胃不舒服", prepare_query("胃不舒服还能吃辣吗"))
    assert not conflicts_with_query("地方", prepare_query("成都有什么好玩的地方")), "泛指词不该驱动剔除"
    assert not conflicts_with_query("胃不舒服", prepare_query("今天天气如何"))
    assert conflicts_with_query("膝盖运动前热身", prepare_query("膝盖运动前要注意什么")), "长条件按比例命中仍要生效"


def test_negative_routing_drops_conflicting_memory(tmp_path):
    import memory_conditions
    mgr = make_manager(tmp_path)
    store = seed(mgr, "u", ["用户不吃辣，胃受不了", "用户吃辣从不忌口，胃从来没有问题"])
    store["s001"].when_invalid = "胃不舒服"
    mgr._save_to_disk(); mgr._invalidate_search_cache("u")

    def answer_ids(enabled):
        memory_conditions.ENABLED = enabled
        try:
            return {m.id for m in mgr.search_memories("u", "胃不舒服还能吃辣吗", limit=5)}
        finally:
            memory_conditions.ENABLED = True

    assert "s001" in answer_ids(False), "关掉负路由时这条该照常参与（证明开关不是假的）"
    on = answer_ids(True)
    assert "s001" not in on, "命中自己声明的『不适用条件』的记忆不该当答案"
    assert "s000" in on, "没声明条件的记忆不许被牵连"
    assert mgr.get_search_metrics()["neg_routed"] >= 1, "剔除必须留痕，不许静默消失"


def test_condition_slots_survive_persistence_roundtrip(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.add_memory(user_id="u", content="用户吃辣从不忌口", when_invalid="胃不舒服")
    mgr._save_to_disk()
    reloaded = MemoryManager(storage_path=str(tmp_path / "s"), db_path=str(tmp_path / "m.db"))
    kept = [m for m in reloaded._get_user_memories("u").values() if m.when_invalid == "胃不舒服"]
    assert len(kept) == 1, "条件槽必须落盘并可读回，否则负路由只是内存里的摆设"


def test_legacy_memories_without_conditions_are_unaffected(tmp_path):
    """旧库没有条件槽：读回不许炸，检索结果不许变。"""
    mgr = make_manager(tmp_path)
    store = seed(mgr, "u", ["用户不吃辣", "用户爱下雨天"])
    data = [m.to_dict() for m in store.values()]
    for d in data:
        d.pop("when_valid", None)
        d.pop("when_invalid", None)          # 模拟改动之前落盘的老行
    restored = [MemoryItem.from_dict(d) for d in data]
    assert all(m.when_invalid == "" for m in restored)
    assert {m.id for m in mgr.search_memories("u", "不吃什么", limit=5)} == {"s000"}


def test_maintenance_lock_is_per_instance(tmp_path):
    """锁必须是实例级：类属性会让 A 实例以为 B 实例的维护线程是"自己人"，从而谎报已跑完。"""
    a = make_manager(tmp_path)
    b = MemoryManager(storage_path=str(tmp_path / "s2"), db_path=str(tmp_path / "m2.db"))
    b.embedding_service._client = None
    assert a._maintenance_lock is not b._maintenance_lock
    assert a._probe_lock is not b._probe_lock


def test_public_dict_exposes_condition_slots(tmp_path):
    """前端要能看见条件：public_dict 不带出来的话，用户既看不到也无从核对。"""
    mgr = make_manager(tmp_path)
    mgr.add_memory(user_id="u", content="如果下雨就不跑步了")
    item = next(m for m in mgr._get_user_memories("u").values() if "下雨" in m.content)
    pub = item.public_dict()
    assert pub["when_valid"] == "下雨"
    assert pub["when_invalid"] == ""
    assert "embedding" not in pub


def test_synonym_groups_have_no_polarity_or_generic_leak():
    """组内不许跨情绪极性，也不许拿泛指词成组——这两种都会把不该来的干扰条扩进候选。"""
    banned_pairs = ({("怕", "讨厌")}, )
    for group in memory_synonyms.SYNONYM_GROUPS:
        assert not ("怕" in group and "讨厌" in group), "「怕」≠「讨厌」：扩进来只会引入新冒充"
        assert "地方" not in group and "那边" not in group, "泛指词不成组"
        assert len(group) >= 2, "单词组什么都没扩"


def test_dup_prefilter_never_hides_a_real_duplicate():
    """粗筛必须是判重的**超集**：被它筛掉的对，逐条全判也必然判不出重复。"""
    import itertools
    import random as _random
    from memory_manager import _dup_prefilter
    from memory_governance import is_near_duplicate, normalized_content, negated

    rng = _random.Random(9)
    atoms = list("我喜欢吃香菜不没别了着很那点儿些猫妈")
    corpus = set()
    for _ in range(500):
        n = rng.randint(1, 7)
        corpus.add("".join(rng.choice(atoms) for _ in range(n)))
    corpus |= {"喜欢猫", "他喜欢猫", "我喜欢吃香菜", "用户喜欢吃香菜", "不吃辣", "吃辣", "", "香菜"}
    texts = [normalized_content(t) for t in sorted(corpus)]

    hidden = 0
    checked = 0
    for a, b in itertools.permutations(texts, 2):
        full = is_near_duplicate(a, b)
        passed = _dup_prefilter(len(a), len(b), negated(a), negated(b))
        checked += 1
        if full and not passed:
            hidden += 1
        assert (not passed) or True
    assert hidden == 0, "粗筛漏掉了真重复：%d 对" % hidden
    assert checked > 10000, "样本太小，这条测试什么都没测（%d 对）" % checked


def test_stream_token_guard_does_not_kill_the_workflow():
    """中转把异常对象塞进 chunk.content 时，不许再把整条流式工作流打死。

    改之前 `full_reply += token` 会抛
    TypeError: can only concatenate str (not "RuntimeError") to str。
    """
    from emotion_graph import _stream_token_or_error
    assert _stream_token_or_error("你好") == ("你好", None)
    assert _stream_token_or_error(None) == ("", None)
    err = RuntimeError("上游炸了")
    text, bad = _stream_token_or_error(err)
    assert text == "" and bad is err, "异常对象该当流内错误交回兜底路径"
    text, bad = _stream_token_or_error(["分段内容"])
    assert text == "" and isinstance(bad, RuntimeError)


def test_llm_extractor_reads_condition_objects(tmp_path):
    """抽取器给出带条件的对象时，条件要一路进到记忆里（不许半路丢掉）。"""
    import llm_config
    payload = '[{"fact": "如果下雨就不去跑步", "when_valid": "下雨", "when_invalid": ""}]'

    class _Resp:
        content = payload

    class _LLM:
        def invoke(self, msgs):
            return _Resp()

    original = llm_config.get_llm_client
    llm_config.get_llm_client = lambda *a, **k: _LLM()
    try:
        mgr = make_manager(tmp_path)
        mgr.embedding_service.get_embedding = lambda text: None
        detailed = mgr._llm_extract_facts("我下雨天就不跑步了", "好，那就不跑")
        assert detailed and detailed[0][1] == "下雨", detailed
        mgr.extract_and_store_facts("u", "我下雨天就不跑步了", "好，那就不跑")
        stored = [m for m in mgr._get_user_memories("u").values() if "跑步" in m.content]
        assert stored and stored[0].when_valid == "下雨"
    finally:
        llm_config.get_llm_client = original


def test_llm_extractor_still_accepts_plain_strings(tmp_path):
    import llm_config
    class _Resp:
        content = '["用户养了一只猫叫团子"]'
    class _LLM:
        def invoke(self, msgs):
            return _Resp()
    original = llm_config.get_llm_client
    llm_config.get_llm_client = lambda *a, **k: _LLM()
    try:
        mgr = make_manager(tmp_path)
        mgr.embedding_service.get_embedding = lambda text: None
        assert mgr._extract_facts("我家猫叫团子", "团子呀") == [
            ("[关于用户] 用户养了一只猫叫团子", "", "")]
    finally:
        llm_config.get_llm_client = original


def test_defer_swaps_wrong_situation_memory_below_confirmed_one(tmp_path):
    """DEFER 的作用要能被测出来：词面更抢手但情境不对的那条，必须被已确认的那条压下去。

    两头都断言——关掉时它确实排在前面（否则这道测试什么都没测），开起来才换位。
    """
    import memory_conditions
    assert memory_conditions.DEFER_ENABLED is True, "默认已开启：门禁实测不再伤 hit@10/hit@1"
    mgr = make_manager(tmp_path)
    store = seed(mgr, "u", [
        "她一般傍晚在江滩动一动，那个风最凉",
        "工作日散个步就行",
        "用户不吃辣，胃受不了",
    ])
    store["s000"].when_valid = "周末晚上"
    store["s001"].when_valid = "工作日晚上"
    mgr._save_to_disk(); mgr._invalidate_search_cache("u")
    query = "工作日傍晚她一般怎么动"

    memory_conditions.DEFER_ENABLED = False
    try:
        off = [m.id for m in mgr.search_memories("u", query, limit=5)]
        assert off.index("s000") < off.index("s001"), "关掉时词面更强的那条在前：%s" % off
    finally:
        memory_conditions.DEFER_ENABLED = True

    before = mgr.get_search_metrics()["defer_demoted"]
    on = [m.id for m in mgr.search_memories("u", query, limit=5)]
    assert on.index("s001") < on.index("s000"), "开 DEFER 后情境被确认的那条该在前：%s" % on
    assert "s000" in on, "降档不是删除，记忆还得留着"
    assert mgr.get_search_metrics()["defer_demoted"] > before

    memory_conditions.DEFER_ENABLED = False
    try:
        untouched = [m.id for m in mgr.search_memories("u", "我不吃什么", limit=5)]
        assert untouched == ["s002"], untouched        # 没声明条件的记忆完全不受影响
    finally:
        memory_conditions.DEFER_ENABLED = True


def test_extract_seam_is_single_and_accepts_plain_strings(tmp_path):
    """抽取只有一个 seam：桩住 `_extract_facts` 必须真的挡住中转。

    之前把它拆成 detailed 之后，桩住老名字的用例会绕开桩直接打大模型——
    快检门禁号称"不碰大模型"，一旦这样就是悄悄烧额度 + 读数不可复现。
    """
    import llm_config
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("桩住 _extract_facts 之后不该再碰大模型")

    original = llm_config.get_llm_client
    llm_config.get_llm_client = boom
    try:
        mgr = make_manager(tmp_path)
        mgr.embedding_service.get_embedding = lambda text: None
        mgr._extract_facts = lambda *a, **k: ["[关于用户] 用户讨厌吃香菜"]   # 老形态：纯字符串
        texts = mgr.extract_and_store_facts("u", "我讨厌吃香菜", "记住了")
        assert texts == ["[关于用户] 用户讨厌吃香菜"], texts
        assert calls["n"] == 0, "seam 被绕过了：还是在偷偷打中转"
        stored = [m.content for m in mgr._get_user_memories("u").values()]
        assert stored == ["[关于用户] 用户讨厌吃香菜"], stored
    finally:
        llm_config.get_llm_client = original


def test_model_supplied_conditions_must_come_from_the_users_own_words(tmp_path):
    """抽取器回填的条件必须是用户原话里的词面；说反或编出来的不许进记忆。

    真实中转实测：用户说"除非周末有空，否则我基本不做饭"，模型写成
    生效=非周末 / 不适用=周末有空（正反颠倒）——那样会在该用的时候把记忆剔掉。
    """
    import llm_config
    from memory_conditions import derive_condition, trustworthy
    payload = ('[{"fact": "用户基本不做饭", "when_valid": "非周末（没有空）", '
               '"when_invalid": "周末有空"}]')

    class _Resp:
        content = payload
    class _LLM:
        def invoke(self, msgs):
            return _Resp()

    user_msg = "我最近迷上了做酸菜鱼，不过除非周末有空，否则我基本不做饭"
    original = llm_config.get_llm_client
    llm_config.get_llm_client = lambda *a, **k: _LLM()
    try:
        mgr = make_manager(tmp_path)
        mgr.embedding_service.get_embedding = lambda text: None
        mgr.extract_and_store_facts("u", user_msg, "听起来好吃！")
        stored = [m for m in mgr._get_user_memories("u").values() if "不做饭" in m.content]
        assert stored, "该落的那条记忆没落"
        m = stored[0]
        assert not trustworthy("非周末（没有空）", user_msg), "原话里找不到这个词面"
        assert m.when_valid == "", "原话里找不到词面的生效条件不许采信：%s" % m.when_valid
        # 「除非周末有空，否则基本不做饭」的例外就是"周末有空"：
        # 模型这一项与确定性规则算出的同一个值，所以留下；只砍掉它对不上原话的那一半
        assert m.when_invalid == "周末有空", m.when_invalid
        assert derive_condition(user_msg) == ("", "周末有空"), derive_condition(user_msg)
    finally:
        llm_config.get_llm_client = original


def test_negative_routing_lives_in_the_recall_paths(tmp_path):
    """剔除必须长在两路召回本体里，不是只长在 search_memories 上。

    否则以后任何人新开一条读法（直接调 _keyword_search_raw / _semantic_search_raw）
    就会把"自己声明过不适用情境"的记忆原样带出去，而这正是这道裁决要挡的东西。
    """
    import memory_conditions
    memory_conditions.ENABLED = True
    mgr = make_manager(tmp_path)
    store = seed(mgr, "u", ["用户不吃辣，胃受不了", "用户吃辣从不忌口，胃从来没有问题"])
    store["s001"].when_invalid = "胃不舒服"
    mgr._save_to_disk(); mgr._invalidate_search_cache("u")

    direct = mgr._keyword_search_raw(dict(mgr._get_user_memories("u")),
                                     "胃不舒服还能吃辣吗", None, 0.0, user_id="u")
    assert "s001" not in {m.id for _score, m in direct}, "绕过 search_memories 就绕过了裁决"
    assert "s000" in {m.id for _score, m in direct}

    sem = mgr._semantic_search_raw(dict(mgr._get_user_memories("u")),
                                   "胃不舒服还能吃辣吗", None, 0.0, user_id="u")
    assert "s001" not in {m.id for _score, m in sem}


class _FakeEmbeddings:
    """语义路打通之后，这些分支第一次变得可达——用桩向量跑，不碰中转。"""

    def __init__(self, vector=None):
        self._vector = vector if vector is not None else [1.0] + [0.0] * 63
        self.failed = 0
        self.tried = 0
        self._disabled_until = 0.0
        self._client = object()

    def available(self):
        return True

    def get_embedding(self, text):
        return list(self._vector)

    def get_embeddings_batch(self, texts):
        return [list(self._vector) for _ in texts]


def test_semantic_dedupe_branch_survives_real_embeddings(tmp_path):
    """向量一通，语义查重那条分支第一次会真的执行。

    它原来往 `reinforce_memory` 传了个不存在的 `importance_value` 参数，
    一执行就 TypeError 把整轮落库打断（测试全把 `_client` 置 None，所以从没走到）。
    """
    mgr = make_manager(tmp_path)
    mgr.embedding_service = _FakeEmbeddings()
    scores = iter([0.4, 0.9])                     # add_memory 自己算重要性，这里替它决定
    mgr.importance_scorer.calculate = lambda *a, **k: next(scores)

    first = mgr.add_memory(user_id="u", content="用户不吃辣，胃受不了")
    second = mgr.add_memory(user_id="u", content="用户明确表示不能吃苦辣的东西")
    active = [m for m in mgr._get_user_memories("u").values() if m.active()]
    assert len(active) == 1, "同一件事该并成一条：%s" % [m.content for m in active]
    assert active[0].id == first.id
    assert abs(active[0].importance - 0.9) < 1e-6, "去重时重要性应取两者较大，实测 %s" % active[0].importance
    # 不断言 regrade_reason：reinforce_memory 的再评分流程会拥有并覆盖这个字段


def test_extract_and_store_with_embeddings_live(tmp_path):
    """整条"抽取 → 查重 → 落库"链在向量可用时必须不炸。"""
    import llm_config
    calls = {"n": 0}

    class _Boom:
        def invoke(self, msgs):
            calls["n"] += 1
            raise AssertionError("桩住 _extract_facts 之后不该打中转")

    original = llm_config.get_llm_client
    llm_config.get_llm_client = lambda *a, **k: _Boom()
    try:
        mgr = make_manager(tmp_path)
        mgr.embedding_service = _FakeEmbeddings()
        mgr._extract_facts = lambda *a, **k: [
            ("[关于用户] 用户不吃辣", "", "胃不舒服"),
            ("[关于用户] 用户不吃香菜", "", ""),
        ]
        stored = mgr.extract_and_store_facts("u", "我辣的和香菜都不吃", "记住了")
        assert calls["n"] == 0
        assert len(mgr._get_user_memories("u")) >= 1
        assert mgr.get_search_metrics()["embed_probe"] == 0   # 写侧用的是桩，不发探测
        assert stored, stored
    finally:
        llm_config.get_llm_client = original

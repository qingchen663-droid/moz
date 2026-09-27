"""关键词检索分段计时：把"记忆一多就慢"拆开看，到底慢在哪一步。

只在临时目录里跑，绝不碰 backend/moz.db 和用户真实记忆。
②③④⑤ 是"每次查询全库重算"的老写法每一步的代价，⑦ 是现在缓存后的样子。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/profile_search.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/profile_search.py --level 50000
"""

import argparse
import logging
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("ZHIPU_API_KEY", "offline-profile")
os.environ["MOZ_MAX_ACTIVE_MEMORIES"] = os.environ.get("MOZ_PROFILE_CAP", "200000")
os.environ["MOZ_CONSOLIDATION_TRIGGER"] = "99999999"
# 默认超过 20000 条就不缓存；这个台子要量缓存后的样子，所以抬到盖得住 --level
os.environ.setdefault("MOZ_KEYWORD_INDEX_MAX_DOCS", "200000")

logging.disable(logging.INFO)

from memory_manager import MemoryStatus  # noqa: E402
import stress_memory as stress  # noqa: E402

USER = stress.USER


def best(fn):
    """跑 3 次取最小：只关心稳定成本，不想把 GC 抖动算进来。"""
    runs = []
    for _ in range(3):
        t = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - t) * 1000)
    return min(runs)


def once(fn):
    t = time.perf_counter()
    fn()
    return (time.perf_counter() - t) * 1000


def df_build(manager, docs):
    df = {}
    for m in docs:
        for token in set(manager._search_tokens(m.content)):
            df[token] = df.get(token, 0) + 1
    return df


def counts_only(manager, docs):
    out = {}
    for m in docs:
        counts = {}
        for token in manager._search_tokens(m.content):
            counts[token] = counts.get(token, 0) + 1
        out[m.id] = counts
    return out


def scores_all(manager, docs, query):
    return [(manager._match_score(m, query), m.id) for m in docs]


def docs_snapshot(store):
    return [m for m in store.values() if m.status == MemoryStatus.ACTIVE]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=20000)
    args = ap.parse_args()

    rng = random.Random(7)
    tmp = tempfile.mkdtemp(prefix="moz-profile-")
    manager = stress.new_manager(tmp)
    stress.bulk_insert(manager, stress.make_memories(rng, args.level, 0), 0)
    store = dict(manager._get_user_memories(USER))
    docs = docs_snapshot(store)
    query = stress.questions(rng, 1)[0]
    queries = stress.questions(rng, 5) * 2
    per = lambda ms: ms * 1000 / max(len(docs), 1)
    print(f"沙箱 {tmp}，{len(docs)} 条活跃记忆，查询「{query}」\n")

    steps = [
        ("① 快照全部记忆", lambda: dict(manager._get_user_memories(USER))),
        ("① 筛活跃文档", lambda: docs_snapshot(store)),
        ("② 全部重新分词", lambda: {m.id: manager._search_tokens(m.content) for m in docs}),
        ("③ 建 DF 表", lambda: df_build(manager, docs)),
        ("④ 逐条统计词频", lambda: counts_only(manager, docs)),
        ("⑤ 逐条算匹配分", lambda: scores_all(manager, docs, query)),
        ("⑥ 同一份新代码但不缓存", lambda: manager._keyword_search_raw(store, query, None, 0.0)),
    ]
    print(f"{'步骤（②③④⑤ = 老写法每次查询都要走一遍）':<40} {'ms':>9} {'µs/条':>8}")
    for name, fn in steps:
        ms = best(fn)
        print(f"{name:<40} {ms:9.1f} {per(ms):8.2f}")

    manager._invalidate_search_cache(USER)
    cold = once(lambda: manager._keyword_search_raw(store, query, None, 0.0, user_id=USER))
    warm = best(lambda: manager._keyword_search_raw(store, query, None, 0.0, user_id=USER))
    print(f"{'⑦ 缓存后：首次（建索引）':<40} {cold:9.1f} {per(cold):8.2f}")
    print(f"{'⑦ 缓存后：之后每次查询':<40} {warm:9.1f} {per(warm):8.2f}"
          f"   ← 一轮 3 条改写里，第 2、3 次就是这个价")
    print(f"{'⑧ 进程总内存':<40} {stress.rss_mb():9.1f} MB")

    # 生产节奏：每轮对话后会有 1~3 条新记忆写入 → 缓存作废 → 下一轮的首次检索
    from memory_manager import MemoryCategory
    manager.add_memory(USER, "用户提到阳台那盆龟背竹该换盆了", category=MemoryCategory.FACT)
    after_write = once(lambda: manager._keyword_search_raw(
        manager._get_user_memories(USER), query, None, 0.0, user_id=USER))
    print(f"{'⑨ 写入新记忆后的首次检索':<40} {after_write:9.1f} {per(after_write):8.2f}"
          f"   ← 只为新记忆分词，老记忆复用派生数据")

    print("\n线上一次对话会拿 3 条改写各查一遍：")
    manager._invalidate_search_cache(USER)
    turn = once(lambda: [manager._keyword_search_raw(store, q, None, 0.0, user_id=USER)
                         for q in queries[:3]])
    print(f"  一轮 3 次 keyword 合计 {turn:.1f}ms（含建索引）")
    lat = [once(lambda q=q: manager._keyword_search_raw(store, q, None, 0.0, user_id=USER))
           for q in queries]
    print(f"  单查询 p50={statistics.median(lat):.1f}ms  max={max(lat):.1f}ms")
    hits = len(manager._keyword_search_raw(store, query, None, 0.0, user_id=USER))
    print(f"\nscore>0 的条数 / 总条数 = {hits}/{len(docs)}")
    print(f"索引状态：{manager.get_keyword_index_stats()}")


if __name__ == "__main__":
    main()

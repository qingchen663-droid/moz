"""记忆检索压力测试：灌多少条记忆会把检索顶爆。

只在临时目录里跑，绝不碰 backend/moz.db 和用户真实记忆。
（沿用 backend/memory_evaluation.py 的沙箱做法：临时 db + 把 embedding 打桩成 None，
 这台机器本来就没有可用的向量模型，线上走的就是关键词那一路。）

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/stress_memory.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/stress_memory.py --levels 1000,5000,20000
"""

import argparse
import ctypes
import ctypes.wintypes
import logging
import os
import random
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("ZHIPU_API_KEY", "offline-stress")

# --no-cap：把容量策略的上限抬到几乎无限，才能量出"检索本身"的天花板
# （必须在 import memory_manager 之前设：那两个值是类属性，导入时就定了）
if "--no-cap" in sys.argv:
    os.environ["MOZ_MAX_ACTIVE_MEMORIES"] = os.environ.get("MOZ_STRESS_CAP", "200000")
    os.environ["MOZ_CONSOLIDATION_TRIGGER"] = "99999999"

# --real-embed：不给 embedding 打桩，量线上真实路径（含向量服务 401 与失败冷却）
REAL_EMBED = "--real-embed" in sys.argv

from memory_manager import MemoryCategory, MemoryManager  # noqa: E402

# 自动维护会一条一条打 INFO 日志，会把表格冲掉
logging.disable(logging.INFO)

USER = "stress-user"
PEOPLE = ["妈妈", "爸爸", "姥姥", "老张", "小林", "表姐", "导师", "房东", "儿科医生", "组长"]
TOPICS = ["体检", "答辩", "面试", "搬家", "项目上线", "孩子上学", "牙医复诊", "驾照换证",
          "续约", "团建", "社保转移", "白内障手术", "毕业照", "车险", "居住证"]
PLACES = ["杭州", "上海", "成都", "公司三楼", "小区门口", "老家县城", "滨江那家", "市一医院"]
VERBS = ["安排在", "说要在", "答应陪", "得去", "要准备", "提过"]
FILLERS = ["最近睡得还行", "有点累", "钱不是大问题", "时间还早", "她嘴上说不在意",
           "我这次一定记得问", "别跟别人说", "先这样吧", "到时候提醒你"]


class _Pmc(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS（psapi）：字段顺序必须和 Windows 头文件一致。"""

    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> float:
    """当前进程占用的物理内存（MB）；拿不到就返回 -1。

    两处坑：GetCurrentProcess 的返回值必须按句柄取（否则 64 位伪句柄被截成 int），
    而且 psapi 的 argtypes 要显式声明，不然参数又被截回去 → ERROR_INVALID_HANDLE。
    """
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.POINTER(_Pmc), ctypes.wintypes.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
        pmc = _Pmc()
        pmc.cb = ctypes.sizeof(_Pmc)
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1048576.0
    except Exception:
        pass
    return -1.0


def make_memories(rng: random.Random, count: int, start: int):
    """造一批"像从聊天里抽出来的"中文记忆，长度和用词都尽量自然。"""
    out = []
    for i in range(count):
        n = start + i
        style = n % 4
        if style == 0:
            text = f"用户说{rng.choice(PEOPLE)}{rng.randint(1, 12)}月{rng.randint(1, 28)}号{rng.choice(VERBS)}{rng.choice(TOPICS)}，在{rng.choice(PLACES)}"
        elif style == 1:
            text = f"{rng.choice(PEOPLE)}的{rng.choice(TOPICS)}定在{rng.randint(1, 12)}月{rng.randint(1, 28)}号，用户说{rng.choice(FILLERS)}"
        elif style == 2:
            text = f"[对话摘要] 用户说：{rng.choice(TOPICS)}那件事还没定，{rng.choice(PLACES)}那边要{rng.randint(1, 9)}个工作日"
        else:
            text = f"用户{rng.choice(VERBS)}把{rng.choice(TOPICS)}排在{rng.choice(PEOPLE)}有空的那周，备注是{rng.choice(FILLERS)}"
        out.append(text[:120])
    return out


def needle_text(code: str) -> str:
    return f"用户最宝贝的那盆紫鸢尾 {code} 今年第一次开花了，她说谁都不能碰"


def needle_query(code: str) -> str:
    return f"紫鸢尾 {code} 那盆花怎么样了"


def questions(rng: random.Random, n: int = 12):
    return [
        f"{rng.choice(PEOPLE)}的{rng.choice(TOPICS)}是什么时候",
        f"我之前说的那个{rng.choice(TOPICS)}后来怎么样了",
        f"{rng.choice(PLACES)}那边要几个工作日",
        f"我答应过{rng.choice(PEOPLE)}什么",
        f"{rng.choice(TOPICS)}要不要提醒我",
    ][:n]


def new_manager(dirpath: str) -> MemoryManager:
    m = MemoryManager(storage_path=dirpath, db_path=os.path.join(dirpath, "memory.db"))
    if REAL_EMBED:
        # 不打桩：量的就是线上真实路径（向量服务是过期令牌 → 看失败冷却省下多少）
        return m
    # 这台机器 embedding 不可用：线上走的就是关键词那一路，打桩保持一致
    m.embedding_service.get_embedding = lambda text: None
    m.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
    return m


def timed(fn, *a, **kw):
    t = time.perf_counter()
    r = fn(*a, **kw)
    return r, (time.perf_counter() - t) * 1000.0


def bulk_insert(manager, texts, start):
    """直接写库 + 填内存，绕开 add_memory 的去重/衰减/逐条落盘。

    那些是"写入路径"的开销；这里要量的是"检索"的天花板，
    不然 3 万条要等半小时写，测不出检索本身。
    """
    import json

    from memory_manager import MemoryItem

    store = manager._get_user_memories(USER)
    template = next(iter(store.values()), None)
    if template is None:
        template = manager.add_memory(USER, "模板：一件小事", category=MemoryCategory.FACT)
        template = next(iter(store.values()))
    base = template.to_dict()
    conn = manager._get_conn()
    now = time.time()
    rows = []
    for i, text in enumerate(texts):
        mid = f"bulk{start + i:09d}"
        d = dict(base)
        d.update({"id": mid, "content": text, "status": "active", "created_at": now,
                  "updated_at": now, "last_accessed": now, "access_count": 0,
                  "is_consolidated": False, "regrade_reason": "stress_bulk"})
        rows.append((USER, mid, json.dumps(d, ensure_ascii=False)))
    conn.executemany("INSERT OR REPLACE INTO memories (user_id, memory_id, data) VALUES (?,?,?)", rows)
    conn.commit()
    for _u, mid, data in rows:
        store[mid] = MemoryItem.from_dict(json.loads(data))
    manager._invalidate_search_cache(USER)
    return len(rows)


def run(levels, seed=7, prod_queries=3, sample=12, bulk=False):
    rng = random.Random(seed)
    tmp = tempfile.mkdtemp(prefix="moz-stress-")
    manager = new_manager(tmp)
    db = Path(tmp) / "memory.db"
    stored = 0
    needles = []
    print(f"沙箱目录 {tmp}（真实库没被碰过）\n")
    header = (f"{'条数':>7} {'写入s':>7} {'检索p50ms':>10} {'检索p95ms':>10} "
              f"{'线上式ms':>9} {'召回':>6} {'库MB':>6} {'内存MB':>8} {'重启加载s':>9}")
    print(header)
    print("-" * len(header))

    for level in levels:
        add = level - stored
        if add <= 0:
            continue
        code = f"K{int(level):05d}X"
        texts = make_memories(rng, add - 1, stored) + [needle_text(code)]
        t0 = time.perf_counter()
        if bulk:
            bulk_insert(manager, texts, stored)
        else:
            for text in texts:
                manager.add_memory(USER, text, category=MemoryCategory.FACT,
                                   conversation_id=f"c{stored // 1000}")
            manager._save_to_disk()
        write_s = time.perf_counter() - t0
        stored = level
        needles.append(code)

        lat = []
        for q in questions(rng, sample):
            _, ms = timed(manager.search_memories, USER, q, limit=10)
            lat.append(ms)
        p50 = statistics.median(lat)
        p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]

        # 线上口径（第廿四轮核过）：limit=10、**用户原话单查询**，多路改写那套已随死代码删掉
        q3 = [f"用户提到的{rng.choice(TOPICS)}", f"{rng.choice(PEOPLE)}{rng.choice(TOPICS)}", "上次说的那件事"]
        prod_ms = []
        for one in q3:
            _, ms = timed(manager.search_memories, USER, one, limit=10)
            prod_ms.append(ms)
        prod_ms = statistics.median(prod_ms)

        hits = 0
        missed = []
        for c in needles:
            got = manager.search_memories(USER, needle_query(c), limit=10)
            if any(c in m.content for m in got):
                hits += 1
                continue
            item = next((m for m in manager.memories.get(USER, {}).values() if c in m.content), None)
            missed.append(f"{c}={'不在库里' if item is None else item.status.value}")

        _, stats_ms = timed(manager.get_memory_stats, USER)
        db_mb = db.stat().st_size / 1048576.0 if db.exists() else 0.0

        # 重启代价：MemoryManager 构造时就把整库读回内存（后端 --reload 后第一次请求就是这个）
        t_reload = time.perf_counter()
        again = new_manager(tmp)
        reload_s = time.perf_counter() - t_reload
        del again

        # 库里各状态的条数：这才是"顶爆"的真相——超cap的先归档，归档堆到2倍就物理删
        tally = {}
        for m in manager.memories.get(USER, {}).values():
            tally[m.status.value] = tally.get(m.status.value, 0) + 1
        state = f"活{tally.get('active', 0)} 档{tally.get('archived', 0)} 其{sum(v for k, v in tally.items() if k not in ('active', 'archived'))}"
        rows = manager._get_conn().execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ?", (USER,)
        ).fetchone()[0]
        state += f" 库里{rows}行"

        print(f"{stored:>7} {write_s:>7.1f} {p50:>10.1f} {p95:>10.1f} {prod_ms:>9.1f} "
              f"{hits}/{len(needles):<4} {db_mb:>6.1f} {rss_mb():>8.0f} {reload_s:>9.2f}  {state}  {missed}")
        sys.stdout.flush()

    print(f"\n最后规模：{stored} 条记忆，库文件 {db.stat().st_size / 1048576:.1f} MB")
    stage = manager.get_search_metrics()["avg_stage_ms"]
    print("每次检索的阶段均值（ms，含 3 条改写）："
          + "  ".join(f"{k}={v}" for k, v in stage.items() if k != "total")
          + f"  合计={stage['total']}")
    print(f"关键词索引：{manager.get_keyword_index_stats()}")
    print(f"沙箱留在 {tmp}（真实库全程没被写过；这个目录可以直接删）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="500,1000,2000,4000,8000,16000,32000,64000")
    ap.add_argument("--seed", type=int, default=7)
    # 真正生效的地方在文件顶部（import memory_manager 之前），这里只是让 argparse 认它
    ap.add_argument("--no-cap", action="store_true",
                    help="抬掉活跃上限，量检索本身的天花板")
    ap.add_argument("--bulk", action="store_true",
                    help="直接写库灌数据（绕开 add_memory 的写入开销），用来冲大库")
    # 真正生效的地方在文件顶部（import memory_manager 之前），这里只是让 argparse 认它
    ap.add_argument("--real-embed", action="store_true",
                    help="不给 embedding 打桩：量线上真实路径（含向量服务失败冷却）")
    a = ap.parse_args()
    levels = [int(x) for x in re.split(r"[,\s]+", a.levels) if x.strip()]
    run(sorted(levels), seed=a.seed, bulk=a.bulk)


if __name__ == "__main__":
    main()

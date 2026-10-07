"""检索面门禁：hit@k / MRR + 同义扩展与条件负路由的三臂 A-B，跑在隔离临时库上。

口径（写清测不出什么）：
- 只评**检索命中**，不评答案正确性、不评模型有没有照记忆说话；
- A 池是零干扰（池内全是 gold），高命中率不可外推为端到端记忆能力；
- B 池加了否定条目与无关条目，用来看"干扰会不会冒充"；
- 负路由只按词面（二元组重合）判冲突，不做语义蕴含——换了说法的条件它剔不掉；
- 反向对照：某一臂与对照臂逐位相同 → 说明这道门禁测不出该项，脚本判失败退出。

跑法：`cd backend && python memory_retrieval_bench.py`
"""
import json
import os
import sys
import tempfile
import time


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import memory_synonyms
    import memory_conditions
    from memory_manager import MemoryManager, MemoryItem

    GOLD = [
        ("g01", "用户不吃辣，胃受不了"),
        ("g02", "用户的猫叫豆子，今年三岁"),
        ("g03", "用户每周五晚上去健身房"),
        ("g04", "用户在学 Rust，觉得生命周期最难缠"),
        ("g05", "用户的生日是 1993 年 4 月 17 日"),
        ("g06", "用户左膝去年扭伤过，运动前必须热身"),
        ("g07", "用户练吉他总卡在 F 和弦"),
        ("g08", "用户每天通勤单程四十分钟"),
        ("g09", "用户不爱吃香菜"),
        ("g10", "用户说下雨天让他安心"),
        ("g11", "用户和妈妈每周通一次电话"),
        ("g12", "用户上个月去成都，夸过那里的兔头"),
        ("g13", "用户熬夜之后第二天会头疼"),
        ("g14", "用户喜欢《尼尔》的原声，尤其那首散步"),
        ("g15", "用户习惯用中文写注释"),
        ("g16", "用户怕鸽子，小时候被追过"),
        ("g17", "用户想养成早起的习惯，一直没成"),
        ("g18", "用户的口头禅是'先这样吧'"),
        ("g19", "用户嫌开会前不通知人"),
        ("g20", "用户的多肉被浇得快死了"),
        ("g21", "她一般傍晚在江滩动一动，那个风最凉"),   # 词面抢手，但它声明的情境是周末晚上
        ("g22", "用户工作日晚饭后散步二十分钟"),
        ("g22b", "工作日散个步就行"),
    ]
    DISTURB = [
        ("d01", "用户很喜欢吃辣，无辣不欢"),
        ("d02", "用户讨厌健身，从不运动"),
        ("d03", "用户养了一只仓鼠叫糯米"),
        ("d04", "用户生日是 2001 年 9 月 1 日"),
        ("d05", "用户在学 Go 语言，觉得很好上手"),
        ("d06", "用户通勤开车，从不挤地铁"),
        ("d07", "用户不喜欢音乐，工作时嫌吵"),
        ("d08", "用户的猫叫牛奶"),
        ("d09", "用户从不写日记"),
        ("d10", "用户和妈妈一年才联系一次"),
        ("d11", "用户上周去了哈尔滨"),
        ("d12", "用户睡眠很好，从不超过十二点"),
        ("d13", "用户弹得一手好贝斯"),
        ("d14", "用户膝盖从来没伤过"),
        ("d15", "用户觉得英文注释更规范"),
        ("d16", "服务器重启日志显示三次超时"),
        ("d17", "订单号 88231 的发货地址缺失"),
        ("d18", "用户爱吃香菜，每碗都加"),
        ("d19", "用户每周三晚上上钢琴课"),
        ("d20", "用户说下雨天烦人"),
        ("d21", "用户吃辣从不忌口，胃从来没有问题"),
    ]
    # 干扰条目的「不适用条件」——模拟"这条在某个情境下明确不该拿来当答案"
    INVALID_COND = {
        "d01": "胃不舒服",        # 问「胃不舒服能吃辣吗」时，这条不该出现
        "d12": "头疼",            # 问熬夜之后头疼时不该出现
        "d14": "膝盖运动前热身",   # 问膝盖/热身时不该出现
        "d21": "胃不舒服",        # 与 gold 抢同一题面的干扰条，用来测"剔掉之后 gold 上位"
    }
    QUERIES = [
        ("用户能吃辣吗", "g01"),
        ("★ 他对重口味的态度", "g01"),
        ("★ 他养的小动物叫什么", "g02"),
        ("豆子是谁", "g02"),
        ("★ 他什么时候去锻炼", "g03"),
        ("周五晚上干嘛", "g03"),
        ("Rust 哪里难", "g04"),
        ("★ 他庆生的日子", "g05"),
        ("★ 他哪个关节伤过", "g06"),
        ("★ 他弹琴卡在哪个指法", "g07"),
        ("上班路上要多久", "g08"),
        ("★ 他不吃的蔬菜", "g09"),
        ("★ 什么天气让他踏实", "g10"),
        ("★ 他和家里长辈怎么联系", "g11"),
        ("兔头是哪个城市", "g12"),
        ("★ 晚睡之后他会怎样", "g13"),
        ("★ 他爱听哪张专辑的曲子", "g14"),
        ("代码注释用什么语言", "g15"),
        ("★ 他怕哪种鸟", "g16"),
        ("★ 他没能坚持下来的作息", "g17"),
        ("他常挂在嘴边的话", "g18"),
        ("★ 他对开会通知的要求", "g19"),
        ("★ 他养的植物怎么了", "g20"),
        # 负路由专项：这些题面命中了某条干扰记忆的「不适用条件」
        ("◆ 胃不舒服还能吃辣吗", "g01"),
        ("◆ 熬夜以后头疼怎么办", "g13"),
        ("◆ 膝盖运动前要注意什么", "g06"),
        # DEFER 专项：两条记忆各自声明了时间条件，问句只确认其中一条
        ("◇ 工作日傍晚她一般怎么动", "g22b"),   # 词面更强的 g21 情境不对，DEFER 该压下它
    ]
    # 生效条件专项：这两条记忆自己声明了"什么时候才算数"
    VALID_COND = {"g03": "周五晚上", "g17": "早起", "g21": "周末晚上",
                  "g22": "工作日晚上", "g22b": "工作日晚上"}
    DEFER_QUERIES = ["工作日傍晚她一般怎么动"]
    # 专项题 → (词面抢手但情境不对的那条, 应答的那条)
    DEFER_TRAP_PAIRS = {"工作日傍晚她一般怎么动": ("g21", "g22b")}
    ROUTING_QUERIES = [q.lstrip("★◆◇ ").strip() for q, _ in QUERIES if q.startswith("◆")]
    # 专项题 → 该题面命中了哪条干扰记忆的「不适用条件」（这条本就不该出现在答案里）
    CONFLICT_MAP = {
        "胃不舒服还能吃辣吗": "d21",
        "熬夜以后头疼怎么办": "d12",
        "膝盖运动前要注意什么": "d14",
    }

    def build(with_disturb: bool):
        root = tempfile.mkdtemp(prefix="moz-rb-")
        mgr = MemoryManager(storage_path=os.path.join(root, "s"), db_path=os.path.join(root, "m.db"))
        mgr.embedding_service._client = None          # 门禁只考词法面，向量不参与
        store = mgr._get_user_memories("rb")
        pool = list(GOLD) + (list(DISTURB) if with_disturb else [])
        for mid, content in pool:
            item = MemoryItem(id=mid, content=content, importance=0.6)
            item.when_valid = VALID_COND.get(mid, "")
            if with_disturb:
                item.when_invalid = INVALID_COND.get(mid, "")
            store[mid] = item
            mgr._dirty.add(("rb", mid))
        mgr._save_to_disk()
        mgr._invalidate_search_cache("rb")
        return mgr, {mid for mid, _ in pool}

    def measure(mgr, pool_ids):
        hits = {1: 0, 5: 0, 10: 0}
        rr = 0.0
        top5 = []
        ms = []
        for query, gold in QUERIES:
            clean = query.lstrip("★◆◇ ").strip()
            t0 = time.perf_counter()
            results = mgr.search_memories("rb", clean, limit=10)
            ms.append((time.perf_counter() - t0) * 1000)
            ranked = [m.id for m in results]
            top5.append((clean, gold, ranked[:5]))
            if gold in pool_ids:
                for k in hits:
                    if gold in ranked[:k]:
                        hits[k] += 1
                rr += 1.0 / (ranked.index(gold) + 1) if gold in ranked else 0.0
        n = len(QUERIES)
        return {
            "hit@1": round(hits[1] / n, 3),
            "hit@5": round(hits[5] / n, 3),
            "hit@10": round(hits[10] / n, 3),
            "MRR": round(rr / n, 3),
            "avg_ms": round(sum(ms) / len(ms), 1),
            "top5": top5,
            "neg_routed": mgr.get_search_metrics().get("neg_routed", 0),
            "defer_demoted": mgr.get_search_metrics().get("defer_demoted", 0),
        }

    def run_arm(pools, synonym_on, routing_on, defer_on=True):
        memory_synonyms.ENABLED = synonym_on
        memory_conditions.ENABLED = routing_on
        memory_conditions.DEFER_ENABLED = defer_on
        MemoryManager._prepare_query.cache_clear()
        out = {}
        for name, disturb in pools:
            mgr, pool_ids = build(disturb)
            out[name] = measure(mgr, pool_ids)
        MemoryManager._prepare_query.cache_clear()
        return out

    def squat_rate(arm):
        """top5 里非 gold 条目的平均占比（二值"有没有"会虚高，改用占比）。"""
        gold_ids = {g for _, g in QUERIES}
        rates = [sum(1 for i in top5 if i not in gold_ids) / max(len(top5), 1)
                 for _, _, top5 in arm["top5"]]
        return round(sum(rates) / len(rates), 3)

    pools = [("零干扰池", False), ("干扰池", True)]
    arms = [
        # 每一臂只比上一臂多开一样东西；DEFER 必须显式传 False，
        # 否则默认值会把前面的臂一起打开，B→C 的差就混进了 DEFER 的效果（真踩过）。
        ("A 关同义/关负路由", run_arm(pools, False, False, defer_on=False)),
        ("B 开同义/关负路由", run_arm(pools, True, False, defer_on=False)),
        ("C 同义+负路由/关DEFER", run_arm(pools, True, True, defer_on=False)),
        ("D 同义+负路由/开DEFER", run_arm(pools, True, True)),
    ]

    print("检索面门禁（题量 %d，其中负路由专项 %d；只考检索命中，不考答案对错）" % (
        len(QUERIES), len(ROUTING_QUERIES)))
    print("%-22s %-8s %7s %7s %7s %7s %8s %8s %8s" % (
        "臂", "池", "hit@1", "hit@5", "hit@10", "MRR", "ms/查询", "冒充率", "剔条数"))
    for label, out in arms:
        for name, _ in pools:
            arm = out[name]
            print("%-22s %-8s %7.3f %7.3f %7.3f %7.3f %8.1f %8.3f %8d" % (
                label, name, arm["hit@1"], arm["hit@5"], arm["hit@10"], arm["MRR"],
                arm["avg_ms"], squat_rate(arm) if name == "干扰池" else 0.0, arm["neg_routed"]))

    a, b, c, d = arms[0][1], arms[1][1], arms[2][1], arms[3][1]

    def _rank_of(arm_out, q, mid):
        row = next(r for r in arm_out["干扰池"]["top5"] if r[0] == q)
        return row[2].index(mid) + 1 if mid in row[2] else 99

    DEFER_ORDER_FLIPPED = False
    for q in DEFER_QUERIES:
        trap, answer = DEFER_TRAP_PAIRS[q]
        c_trap, c_ans = _rank_of(c, q, trap), _rank_of(c, q, answer)
        d_trap, d_ans = _rank_of(d, q, trap), _rank_of(d, q, answer)
        print("   DEFER 专项 %s：关臂 抢手但情境不对的 %s=%d / 应答 %s=%d ‖ 开臂 %s=%d / %s=%d" % (
            q, trap, c_trap, answer, c_ans, trap, d_trap, answer, d_ans))
        if c_trap < c_ans and d_ans < d_trap:
            DEFER_ORDER_FLIPPED = True
    if not DEFER_ORDER_FLIPPED:
        print("   !! 专项题上顺序没翻转：DEFER 这次什么都没做，别把它算作有效改动")

    print("\nDEFER 对照（干扰池）：关臂 hit@10=%s 开臂 hit@10=%s；开臂降档次数=%d" % (
        c["干扰池"]["hit@10"], d["干扰池"]["hit@10"], d["干扰池"]["defer_demoted"]))
    for (q_raw, gold) in QUERIES:
        q = q_raw.lstrip("★◆◇ ").strip()
        row_c = next(r for r in c["干扰池"]["top5"] if r[0] == q)
        row_d = next(r for r in d["干扰池"]["top5"] if r[0] == q)
        rc = row_c[2].index(gold) + 1 if gold in row_c[2] else "未进top5"
        rd = row_d[2].index(gold) + 1 if gold in row_d[2] else "未进top5"
        if rc != rd:
            print("   DEFER 改了名次：%s → gold %s：%s 名 → %s 名" % (q, gold, rc, rd))

    # 同义扩展会不会把新的干扰条带进 top5（新冒充）——只看 hit@k 是看不出来的
    def squat_ids(arm):
        gold_ids = {g for _, g in QUERIES}
        return {(q, i) for q, _, top5 in arm["top5"] for i in top5 if i not in gold_ids}

    squat_a, squat_b = squat_ids(a["干扰池"]), squat_ids(b["干扰池"])
    new_squat = sorted(q for q, i in (squat_b - squat_a))
    dropped_squat = sorted(q for q, i in (squat_a - squat_b))
    print("\n同义扩展带进来的新冒充题面：%d（%s）" % (len(new_squat), ", ".join(new_squat[:6]) or "无"))
    print("同义扩展挤掉的旧冒充题面：%d" % len(dropped_squat))
    print("\n专项读数（题面命中了某条干扰记忆的『不适用条件』，该条本不该进答案）")
    removed = 0
    for q, conflict in CONFLICT_MAP.items():
        line = []
        for label, out in (("B", b), ("C", c)):
            row = next(r for r in out["干扰池"]["top5"] if r[0] == q)
            in_top5 = conflict in row[2]
            gold_rank = row[2].index(row[1]) + 1 if row[1] in row[2] else "未进top5"
            line.append("%s臂 冲突条%s=%s gold名次=%s" % (
                label, conflict, "在top5" if in_top5 else "已剔除", gold_rank))
            if label == "B" and not in_top5:
                removed += 1        # 对照臂里本来就没挤进来，C 臂无从证明
        print("   %-14s %s | %s" % (q, line[0], line[1]))
    preventable = len(CONFLICT_MAP) - removed

    fails = []
    if (a["干扰池"]["hit@1"], a["干扰池"]["hit@10"], a["干扰池"]["MRR"]) == \
       (b["干扰池"]["hit@1"], b["干扰池"]["hit@10"], b["干扰池"]["MRR"]):
        fails.append("同义扩展两臂逐位相同——这道门禁测不出同义扩展")
    if preventable == 0:
        fails.append("冲突条目在对照臂（B）里本就没进 top5——负路由无从证明自己在起作用，"
                     "这道门禁测不出负路由（需要加让冲突条挤进 top5 的题面）")
    if c["干扰池"]["neg_routed"] == 0:
        fails.append("负路由一条都没剔——条件冲突判据没生效（或题面不考它）")
    if c["干扰池"]["hit@10"] < b["干扰池"]["hit@10"]:
        fails.append("开负路由后 hit@10 下降 %0.3f→%0.3f：剔掉了本该命中的 gold" % (
            b["干扰池"]["hit@10"], c["干扰池"]["hit@10"]))
    if d["干扰池"]["hit@10"] < c["干扰池"]["hit@10"]:
        fails.append("DEFER 之后 hit@10 从 %0.3f 掉到 %0.3f：降档把本该命中的挤出了 top10，"
                     "不许默认开启" % (c["干扰池"]["hit@10"], d["干扰池"]["hit@10"]))
    if d["干扰池"]["hit@1"] <= c["干扰池"]["hit@1"] and not DEFER_ORDER_FLIPPED:
        fails.append("DEFER 专项顺序没翻转、聚合 hit@1 也没涨（关=%0.3f 开=%0.3f）："
                     "门禁测不出它的作用，不许当作有效改动" % (
                         c["干扰池"]["hit@1"], d["干扰池"]["hit@1"]))
    if d["干扰池"]["hit@1"] < c["干扰池"]["hit@1"]:
        fails.append("DEFER 之后 hit@1 从 %0.3f 掉到 %0.3f" % (
            c["干扰池"]["hit@1"], d["干扰池"]["hit@1"]))
    if new_squat and (a["干扰池"]["hit@10"], a["干扰池"]["hit@1"]) == \
       (b["干扰池"]["hit@10"], b["干扰池"]["hit@1"]):
        fails.append("同义扩展只带进 %d 个新冒充、命中一点没涨——这项是净负担，得改词表或降权重" % len(new_squat))
    regress = []
    for (q, gold), (_, _, o5), (_, _, n5) in zip(QUERIES, b["干扰池"]["top5"], c["干扰池"]["top5"]):
        o_rank = o5.index(gold) + 1 if gold in o5 else 99
        n_rank = n5.index(gold) + 1 if gold in n5 else 99
        if n_rank > o_rank and not q.startswith("◆"):
            regress.append((q.lstrip("★◆◇ ").strip(), gold, o_rank, n_rank))
    if regress:
        print("\n开负路由后 gold 变差的非专项题：%d" % len(regress))
        print("   先怀疑自己再怀疑代码：本项目踩过一次——A/B 臂没显式传 defer_on=False，"
              "于是 B→C 的差里混进了 DEFER 的效果，看起来像\"负路由让 gold 后退了一名\"，"
              "实际是 DEFER 的差别（当时还编了一套 `_rerank` max 归一的解释，去掉这个混淆后现象消失）。"
              "确认每一臂只比上一臂多开一样东西，再来读这条。")
        for q, gold, x, y in regress:
            rowb = next(r for r in b["干扰池"]["top5"] if r[0] == q)
            rowc = next(r for r in c["干扰池"]["top5"] if r[0] == q)
            print("   %s → %s：%d 名 → %d 名" % (q, gold, x, y))
            print("      B 臂前五:", rowb[2])
            print("      C 臂前五:", rowc[2])

    if fails:
        print("\n未达标：")
        for f in fails:
            print("   -", f)
        print("   说明：以上任一项存在时，不得把本轮读数当作『改动有效』的证据。")
        return 1
    print("\n达标：两项改动各自被测出净变化，且 hit@10 未下降。")
    out_path = os.path.join(here, "retrieval_bench_latest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({label.split()[0]: {pool: {k: v for k, v in arm.items() if k != "top5"}
                                      for pool, arm in out.items()}
                   for label, out in arms}, f, ensure_ascii=False, indent=2)
    print("读数已写入:", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

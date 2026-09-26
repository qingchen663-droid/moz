"""清掉自测探针写进真实用户库的测试痕迹。

上一轮的 --full 探针（发纯色图问"这张图是什么颜色"）走的是真实用户的 /api/chat，
于是测试内容进了 web_user_001 的长期记忆、会话历史和工作记忆——
用户一打开应用，看到的就是自己没说过的话、以及 moz 一本正经聊那张根本不存在的图。

判定按"宁可漏、不可错杀"：只删命中探针特征、且落在指定时间窗里的条目，其余全部保留并打印出来。

    # 先演练，只看报告
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/clean_probe_data.py
    # 确认没问题再落地（会自动先存一份快照，并把回滚命令打给你）
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/clean_probe_data.py --apply
"""

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend" / "moz.db"
API = os.environ.get("MOZ_SELFTEST_API", "http://127.0.0.1:8000/api")

# 探针原话：正常用户不会照着念这几句
PROBE_USER = re.compile(r"这张图是什么颜色|只回颜色名|只说颜色名|自测探针")
COLOR = r"(?:红|绿|蓝|黄|紫|橙|灰|黑|白|纯白|浅灰)色?"
# 记忆里的测试痕迹一律是自动兜底的对话摘要
PROBE_MEMORY = re.compile(rf"^\[对话摘要\].*(图|颜色|色块|白底|自测探针|无法回复|{COLOR})")
TEST_USER = re.compile(r"^(care_engine|care_smoketest|__selftest|selftest|probe_)")


def _f(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def plan_memories(conn, since_ts, include_summaries=False):
    """命中探针特征的记忆。

    include_summaries=True 只给 --full 跑完后的自动清理用：那两分钟里没人聊天，
    探针窗口内新出现的自动摘要一律算测试内容（模型回什么我们控制不了，
    光靠"图/颜色"这类字样会漏）。手工跑的工具不开这个口子。
    """
    drop, keep = [], []
    for user_id, mid, data in conn.execute("SELECT user_id, memory_id, data FROM memories").fetchall():
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            d = {}
        content = str(d.get("content", ""))
        created = float(d.get("created_at", 0) or 0)
        probe_like = bool(PROBE_MEMORY.search(content))
        late_summary = include_summaries and content.startswith("[对话摘要]")
        hit = created >= since_ts and (probe_like or late_summary)
        (drop if hit else keep).append((user_id, mid, content, created))
    return drop, keep


def plan_conversation(conn):
    """按"探针问句 + 它后面那句回复"配对删对话；其余一律保留。"""
    conv_new, conv_drop, suspicious = {}, {}, []
    for user_id, data in conn.execute("SELECT user_id, data FROM conversations").fetchall():
        try:
            blob = json.loads(data)
        except json.JSONDecodeError:
            continue
        cleaned, removed = {}, 0
        for cid, conv in (blob.get("conversations") or {}).items():
            keep, was_probe = [], False
            for m in conv.get("messages", []):
                text = str(m.get("content", ""))
                if m.get("role") == "user" and PROBE_USER.search(text):
                    was_probe = True  # 这句是探针发的，它后面那句回复也一定是探针产物
                    removed += 1
                    continue
                if was_probe and m.get("role") == "assistant":
                    was_probe = False
                    removed += 1
                    continue
                was_probe = False
                if m.get("image"):
                    suspicious.append(f"{user_id}: {text[:50]}")
                keep.append(m)
            if keep or not conv.get("messages"):
                c = dict(conv)
                c["messages"] = keep
                cleaned[cid] = c
        conv_new[user_id] = cleaned
        conv_drop[user_id] = removed
    return conv_new, conv_drop, suspicious


def scan(conn, since_ts, include_summaries=False):
    mem_drop, mem_keep = plan_memories(conn, since_ts, include_summaries)
    conv_new, conv_drop, suspicious = plan_conversation(conn)

    working = []
    for user_id, summary, updated in conn.execute(
        "SELECT user_id, summary, updated_at FROM working_memory"
    ).fetchall():
        # 工作记忆的摘要是模型写的，不带固定前缀：只认"这段时间 + 整段都在讲那张图"
        if float(updated or 0) >= since_ts and re.search(r"这张图|图片|颜色|色块|自测探针", summary or ""):
            working.append((user_id, summary))

    test_rows = []
    for table in ("working_memory", "care_log", "proactive_queue", "care_settings", "care_items", "memories"):
        for user_id, n in conn.execute(f"SELECT user_id, COUNT(*) FROM {table} GROUP BY user_id").fetchall():
            if TEST_USER.match(user_id or ""):
                test_rows.append((table, user_id, n))

    return {"mem_drop": mem_drop, "mem_keep": mem_keep, "conv_new": conv_new,
            "conv_drop": conv_drop, "working": working, "test_rows": test_rows,
            "suspicious": suspicious}


def report(res, conn):
    print(f"记忆：命中探针特征 {len(res['mem_drop'])} 条 / 共 {len(res['mem_drop']) + len(res['mem_keep'])} 条")
    for u, mid, content, ts in res["mem_drop"]:
        print(f"   删 [{u} {dt.datetime.fromtimestamp(ts):%m-%d %H:%M}] {content[:66]}")
    for u, mid, content, ts in res["mem_keep"]:
        print(f"   留 [{u}] {content[:66]}")
    for u, n in res["conv_drop"].items():
        left = sum(len(c["messages"]) for c in res["conv_new"][u].values())
        print(f"对话：{u} 删 {n} 句，剩 {left} 句")
    for u, summary in res["working"]:
        print(f"工作记忆：{u} 「{summary[:60]}」将被清空")
    for table, u, n in res["test_rows"]:
        print(f"测试用户杂行：{table} / {u} 共 {n} 行")
    if res["suspicious"]:
        print("！这些话看着像测试内容但规则没删，人工看一眼：")
        for s in res["suspicious"]:
            print("   ", s)
    ids = [m for _, m, _, _ in res["mem_drop"]]
    if ids:
        q = ",".join("?" * len(ids))
        n = conn.execute(f"SELECT COUNT(*) FROM memory_grade_events WHERE memory_id IN ({q})", ids).fetchone()[0]
        print(f"连带删除记忆评分事件 {n} 条")
        return n
    return 0


def rebuild_conversation_index(users, db_path=DB):
    """全文检索表是会话的另一份副本，不重建的话"搜对话"还能搜到已删掉的测试句。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from conversation_store import ConversationStore

    store = ConversationStore(str(db_path))
    for u in users:
        with store.locked(u):
            convs, cur = store.load(u)
            store.save(u, convs or {}, cur)


def delete_memories(entries, conn, api=API):
    """能走接口就走接口：MemoryManager 在进程里缓存着记忆，
    直接 DELETE 数据库会让界面继续显示旧内容，甚至被缓存回写复活。"""
    left = []
    for user_id, mid, _content, _ts in entries:
        if api:
            req = urllib.request.Request(f"{api}/memory/{user_id}/{mid}", method="DELETE")
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    if res.status < 400:
                        continue
            except (urllib.error.URLError, OSError, ValueError):
                pass
        left.append((user_id, mid))
    for user_id, mid in left:
        conn.execute("DELETE FROM memory_grade_events WHERE memory_id = ?", (mid,))
        conn.execute("DELETE FROM memories WHERE user_id = ? AND memory_id = ?", (user_id, mid))
    if left:
        print(f"！{len(left)} 条没走成接口（后端没在跑？），改用直连删除——记得重启后端，"
              "否则界面上的记忆还是旧的")
    return len(left)


def apply_(res, conn, db_path=DB, api=API, reset_talk_score=True):
    # 不显式开事务：python 的 sqlite3 自己会隐式 BEGIN，再写 BEGIN 会直接报错
    for u, cleaned in res["conv_new"].items():
        if res["conv_drop"][u]:
            cur = next((cid for cid in cleaned), None)
            conn.execute("INSERT OR REPLACE INTO conversations (user_id,data) VALUES (?,?)",
                         (u, json.dumps({"current_id": cur, "conversations": cleaned}, ensure_ascii=False)))
    conn.commit()
    delete_memories(res["mem_drop"], conn, api)
    for u, _s in res["working"]:
        conn.execute("UPDATE working_memory SET summary='', open_topics='[]', updated_at=0 WHERE user_id=?", (u,))
    for table, u, _n in res["test_rows"]:
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (u,))
    # 话多话少是被测试对话喂出来的（探针全是一句话回答），一律回落到中性。
    # 自动清理（selftest --full）不该动这个值，它会自己把探针前的值写回去。
    if reset_talk_score:
        for u, data in conn.execute("SELECT user_id, data FROM care_settings").fetchall():
            try:
                d = json.loads(data)
            except json.JSONDecodeError:
                continue
            if d.get("talk_score") not in (None, 0.5):
                d["talk_score"] = 0.5
                conn.execute("UPDATE care_settings SET data=? WHERE user_id=?",
                             (json.dumps(d, ensure_ascii=False), u))
                print(f"talk_score 回落到 0.5：{u}")
    conn.commit()
    rebuild_conversation_index([u for u, n in res["conv_drop"].items() if n], db_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的动手，默认只报告")
    ap.add_argument("--since", default="2026-09-26 22:00", help="只处理这个时间之后的条目")
    a = ap.parse_args()
    if not DB.exists():
        print("找不到 backend/moz.db")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        res = scan(conn, _f(a.since))
        report(res, conn)
        if not a.apply:
            print("\n演练模式，没动数据。加 --apply 才落地。")
            return 0
        sys.path.insert(0, str(ROOT / "tools"))
        from snapshot_data import snapshot
        target = snapshot("pre-probe-clean")
        print(f"\n已存快照 {target.name}（要回滚： .venv/Scripts/python.exe tools/snapshot_data.py --restore {target.name}）")
        apply_(res, conn)
        print("清理完成。界面刷新后应该看到：记忆 0 条、会话只剩你自己说的话。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

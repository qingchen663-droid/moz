"""moz 全局自测台：一条命令跑完后端接口、边界输入、引擎逻辑与数据不变量检查。

设计给"整夜反复跑"用，因此：
  - 默认只跑快检（不碰大模型，秒级）；--full 才加一次真实对话往返（慢、耗额度）
  - 任何写操作都用 try/finally 还原，绝不把测试数据留在用户库里
  - 结果分 fail / warn：fail 一定要修，warn 是环境类（如外网天气不通）

用法：
    python tools/selftest.py            # 快检
    python tools/selftest.py --full     # 含真实对话
    python tools/selftest.py --json     # 机器可读输出
"""

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

API = os.environ.get("MOZ_SELFTEST_API", "http://127.0.0.1:8000/api")
USER = "web_user_001"
DB = ROOT / "backend" / "moz.db"

results = []


def check(name, fn, tier="fast"):
    """跑一项检查；返回 True/False/None(=warn)。"""
    t0 = time.time()
    try:
        r = fn()
        if r in (True, None):
            status, detail = "pass", ""
        elif isinstance(r, str) and (r == "warn" or r.startswith("warn")):
            # 环境类问题（外网不通、中转丢图、回复慢）不该算产品缺陷
            status, detail = "warn", r[:160]
        else:
            status, detail = "fail", str(r)[:160]
    except SkipCheck as e:
        status, detail = "skip", str(e)[:160]
    except Exception as e:
        status, detail = "fail", f"{type(e).__name__}: {e}"[:200]
    results.append({"name": name, "status": status, "ms": round((time.time() - t0) * 1000), "detail": detail, "tier": tier})


class SkipCheck(Exception):
    pass


def http(path, method="GET", body=None, timeout=30, raw=False):
    url = API + path if path.startswith("/") else path
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            payload = res.read()
            if raw:
                return res.status, payload
            return res.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ── 1. 只读接口 ───────────────────────────────────────────
READ_ENDPOINTS = [
    "/health", f"/conversations/{USER}", f"/memory/{USER}/stats", f"/memory/{USER}/detail",
    "/config/model", "/config/model-presets", "/config/prompt", "/users",
    f"/care/items?user_id={USER}", f"/care/settings?user_id={USER}", f"/care/pending?user_id={USER}",
    f"/profile/{USER}", "/logs?limit=5", f"/export/{USER}",
]


def run_reads():
    bad = []
    for p in READ_ENDPOINTS:
        code, body = http(p)
        if code != 200:
            bad.append(f"{p}->{code}")
    return bad or True


def conversation_detail():
    code, data = http(f"/conversations/{USER}")
    if code != 200:
        return f"列表 {code}"
    cid = data.get("current_id")
    if not cid:
        return True  # 全新用户没有当前会话，属正常
    code2, _ = http(f"/conversations/{USER}/{cid}")
    return True if code2 == 200 else f"详情 {code2}"


# ── 2. 边界与非法输入 ─────────────────────────────────────
def bad_inputs():
    ok = True
    cases = [
        (f"/memory/not-a-real-user-%24%24/stats", "GET", None, (400, 403, 404)),
        ("/config/list-models", "POST", {"base_url": "ftp://x/v1", "api_key": "k"}, (400,)),
        ("/config/list-models", "POST", {"base_url": "", "api_key": "k"}, (400,)),
        (f"/avatar/{USER}", "PUT", {"data_url": "data:image/png;base64,AAAA"}, (400,)),
        (f"/avatar/{USER}", "PUT", {"data_url": "not-a-data-url"}, (400,)),
        (f"/care/items?user_id={USER}", "POST", {"title": "   "}, (400,)),
    ]
    for path, method, body, expected in cases:
        code, _ = http(path, method, body)
        if code not in expected:
            ok = f"{path} 期望 {expected} 实得 {code}"
    return ok


def oversized_avatar():
    big = "data:image/jpeg;base64," + ("A" * (900 * 1024))
    code, _ = http(f"/avatar/{USER}", "PUT", {"data_url": big})
    return True if code in (400, 413) else f"超大头像未被拒绝: {code}"


# ── 3. 关心数据往返（写完必还原）──────────────────────────
def care_roundtrip():
    _, before = http(f"/care/items?user_id={USER}&status=all")
    created = None
    try:
        code, body = http(f"/care/items?user_id={USER}", "POST",
                          {"title": "__selftest__", "kind": "event", "due_at": time.time() + 3600})
        if code != 200 or not body.get("item"):
            return f"新增失败 {code}"
        created = body["item"]["id"]
        code, lst = http(f"/care/items?user_id={USER}")
        if not any(i["id"] == created for i in lst.get("items", [])):
            return "新增后列表里没有"
        code, upd = http(f"/care/items/{created}?user_id={USER}", "PATCH", {"title": "__selftest2__"})
        if upd.get("item", {}).get("title") != "__selftest2__":
            return "改名未生效"
        return True
    finally:
        if created:
            http(f"/care/items/{created}?user_id={USER}", "DELETE")
        _, after = http(f"/care/items?user_id={USER}&status=all")
        leftover = [i["id"] for i in after.get("items", []) if i["id"] not in {b["id"] for b in before.get("items", [])}]
        for lid in leftover:
            http(f"/care/items/{lid}?user_id={USER}", "DELETE")


def care_settings_roundtrip():
    _, original = http(f"/care/settings?user_id={USER}")
    try:
        code, got = http(f"/care/settings?user_id={USER}", "PUT", {"quiet_start": "04:00"})
        if got.get("quiet_start") != "04:00":
            return f"写入未生效 {code}"
        # 两个开关必须独立：只关"没来由搭话"不该把到点提醒一起关掉
        _, half = http(f"/care/settings?user_id={USER}", "PUT", {"initiate_chat": False})
        if half.get("remind_events") is not True or half.get("enabled") is not True:
            return f"关一个开关连带关掉了另一个: {half}"
        if "budget_today" not in half:
            return "设置接口没给换算出来的当日条数，界面只能显示内部数字"
        return True
    finally:
        # 整份写回，别靠手写键名清单——加字段时清单会漏，把用户设置清空
        http(f"/care/settings?user_id={USER}", "PUT", original)


def model_list_probes():
    """能连通时验证解析；连不通只降级为 warn，不算产品缺陷。"""
    code, body = http("/config/list-models", "POST",
                      {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-invalid"})
    if code != 200:
        return f"端点异常 {code}"
    if isinstance(body, dict) and body.get("ok") is False and "拒绝" in (body.get("error") or ""):
        return True
    return f"401 场景提示语异常: {body}"


# ── 4. 引擎与抽取的纯逻辑（不起服务也能测）────────────────
def engine_logic():
    import care_engine as E
    import datetime as _dt
    fail = []
    s = _dt.datetime(2026, 1, 1, 3, 0).timestamp()   # 03:00 在 23:00~08:00 内
    e = _dt.datetime(2026, 1, 1, 12, 0).timestamp()  # 12:00 在外
    st = {"quiet_start": "23:00", "quiet_end": "08:00"}
    if not E.in_quiet_hours(st, s):
        fail.append("03:00 应判为安静时段")
    if E.in_quiet_hours(st, e):
        fail.append("12:00 不应判为安静时段")
    if E.in_quiet_hours({"quiet_start": "00:00", "quiet_end": "00:00"}, s):
        fail.append("起止相同应视为不静音")
    if E._parse_hhmm("坏值", (23, 0)) != (23, 0):
        fail.append("坏时间应回落默认值")
    return "; ".join(fail) or True


def extractor_logic():
    import care_extractor as X
    import datetime as _dt
    fail = []
    if X._to_timestamp("2026-10-05", False) <= 0:
        fail.append("绝对日期解析失败")
    y = _dt.datetime.fromtimestamp(X._to_timestamp("06-03", True))
    if (y.month, y.day) != (6, 3):
        fail.append("月日型生日解析错")
    if y.timestamp() < _dt.datetime.now().timestamp():
        fail.append("已过去的生日应推到明年")
    if X._to_timestamp("null", False) != 0 or X._to_timestamp("乱写", False) != 0:
        fail.append("坏日期应给 0")
    if len(X._parse_json_array('前缀 ```json\n[{"kind":"event","title":"x"}]\n``` 后缀')) != 1:
        fail.append("带围栏的 JSON 应能解析")
    if X._parse_json_array("没有数组") != []:
        fail.append("无 JSON 应返回空")
    if X._fallback_items("今天天气不错"):
        fail.append("闲聊不该被兜底记成事项")
    return "; ".join(fail) or True


def store_logic():
    from care_store import CareStore
    u = "__selftest_store__"
    s = CareStore()
    try:
        for t in ("care_items", "proactive_queue", "care_log"):
            s._conn().execute(f"DELETE FROM {t} WHERE user_id=?", (u,))
        s._conn().execute("DELETE FROM care_settings WHERE user_id=?", (u,))
        s._conn().commit()
        it = s.add_item(u, "测试事", kind="birthday", due_at=time.time() + 7200, repeat="yearly")
        if s.add_item.__name__ != "add_item":
            return "store 接口异常"
        dup = s.enqueue(u, "event", "第一句")
        again = s.enqueue(u, "event", "第二句")
        if len(s.pending(u)) != 2:
            return "队列应有 2 条"
        w1 = s.ack(u, [dup["id"], again["id"], "不存在的id"])
        w2 = s.ack(u, [dup["id"], again["id"]])
        if sorted(w1) != sorted([dup["id"], again["id"]]):
            return f"首次 ack 应拿到全部，实得 {w1}"
        if w2 != []:
            return "重复 ack 不应再拿到同一条（会重复写历史）"
        if s.daily_budget(u) != 2:
            return f"auto 默认预算应为 2，实得 {s.daily_budget(u)}"
        s.save_settings(u, {"talk_mode": "chatty"})
        if s.daily_budget(u) != 4:
            return "chatty 预算应为 4"
        s.save_settings(u, {"remind_events": False, "initiate_chat": False})
        if s.daily_budget(u) != 0:
            return "两个开关都关后预算应为 0"
        s.save_settings(u, {"remind_events": True, "talk_mode": "quiet"})
        if s.daily_budget(u) != 1:
            return "quiet 预算应为 1"
        s.save_settings(u, {"talk_mode": "auto", "talk_score": 0.5})
        # 被问一句答一句不该判成话少：moz 刚回完就被短回答接上，得分要往上走
        s.observe_style(u, 8, since_user_msg=30, since_bot_reply=25)
        if s.get_settings(u)["talk_score"] <= 0.5:
            return f"老实答题被判成话少: {s.get_settings(u)['talk_score']}"
        s.observe_style(u, 200, since_user_msg=7200, since_bot_reply=0)
        if not (0 < s.get_settings(u)["talk_score"] <= 1):
            return "talk_score 越界"
        s.save_settings(u, {"talk_mode": "normal"})
        before = s.get_settings(u)["talk_score"]
        s.observe_style(u, 200, since_user_msg=7200, since_bot_reply=0)
        if s.get_settings(u)["talk_score"] != before:
            return "手动模式下不应自动漂移"
        if not s.update_item(u, it["id"], {"title": "改名"}):
            return "改名失败"
        return True
    finally:
        for t in ("care_items", "proactive_queue", "care_log"):
            s._conn().execute(f"DELETE FROM {t} WHERE user_id=?", (u,))
        s._conn().execute("DELETE FROM care_settings WHERE user_id=?", (u,))
        s._conn().commit()


def care_switch_logic():
    """两个开关必须真的独立，且闲聊名额不能挤掉到点提醒。"""
    from care_store import CareStore
    import care_engine as E
    import json as _json
    u = "__selftest_switches__"
    s = CareStore()
    now = time.time()
    try:
        for t in ("care_items", "proactive_queue", "care_log"):
            s._conn().execute(f"DELETE FROM {t} WHERE user_id=?", (u,))
        s._conn().execute("DELETE FROM care_settings WHERE user_id=?", (u,))
        s._conn().commit()
        s.add_item(u, "面试", kind="event", due_at=now - 60)

        both = {c["kind"] for c in E.collect(s, None, u, now)}
        if "event" not in both or "miss_you" not in both:
            return f"默认两个开关都该有候选: {both}"

        s.save_settings(u, {"initiate_chat": False})
        only_remind = {c["kind"] for c in E.collect(s, None, u, now)}
        if "event" not in only_remind or "miss_you" in only_remind:
            return f"关掉搭话后只剩提醒才对: {only_remind}"

        s.save_settings(u, {"initiate_chat": True, "remind_events": False})
        only_chat = {c["kind"] for c in E.collect(s, None, u, now)}
        if "event" in only_chat or "miss_you" not in only_chat:
            return f"关掉到点提醒后只剩搭话才对: {only_chat}"

        s.save_settings(u, {"initiate_chat": False, "remind_events": False})
        if s.get_settings(u)["enabled"] is not False:
            return "两个开关都关时总开关应自动为假"
        if E.collect(s, None, u, now):
            return "两个开关都关时不该还有任何主动消息"

        # 名额：闲聊用完后，到点提醒照说；老记录（只有 enabled）要能摊开
        s.save_settings(u, {"initiate_chat": True, "remind_events": True, "talk_mode": "quiet"})
        s.enqueue(u, "miss_you", "随便问候一句")
        after_quota = {c["kind"] for c in E.collect(s, None, u, now)}
        if "event" not in after_quota:
            return f"闲聊名额用完不该挡住到点提醒: {after_quota}"
        if "miss_you" in after_quota:
            return "闲聊名额用完不该再没来由搭话"

        s._conn().execute(
            "INSERT OR REPLACE INTO care_settings (user_id,data,updated_at) VALUES (?,?,?)",
            (u, _json.dumps({"enabled": False, "city": "杭州"}), now),
        )
        legacy = s.get_settings(u)
        if legacy["remind_events"] or legacy["initiate_chat"]:
            return "老记录的总开关为假时，两个新开关也该是假"
        return True
    finally:
        for t in ("care_items", "proactive_queue", "care_log"):
            s._conn().execute(f"DELETE FROM {t} WHERE user_id=?", (u,))
        s._conn().execute("DELETE FROM care_settings WHERE user_id=?", (u,))
        s._conn().commit()


def probe_cleaner_works():
    """清理器本身必须被测：错杀真实数据比留下残渣严重得多。

    在 moz.db 的临时副本上造"一条真实记忆 + 一条探针记忆 + 两对对话"，
    要求只吃掉探针那部分，且删完的测试句不能再被全文检索搜到。
    """
    import shutil
    import tempfile

    sys.path.insert(0, str(ROOT / "tools"))
    import clean_probe_data as CP

    tmp = Path(tempfile.mkdtemp()) / "moz_copy.db"
    shutil.copy2(DB, tmp)
    conn = sqlite3.connect(tmp)
    now = time.time()
    try:
        conn.execute("DELETE FROM memories WHERE user_id = ?", (USER,))
        conn.executemany(
            "INSERT INTO memories VALUES (?,?,?)",
            [
                (USER, "real01", json.dumps({"content": "妈妈生日是 10 月 5 日", "created_at": now}, ensure_ascii=False)),
                (USER, "junk01", json.dumps({"content": "[对话摘要] 用户说：只说颜色名，这张图是什么颜色？",
                                             "created_at": now}, ensure_ascii=False)),
            ],
        )
        blob = {"current_id": "c1", "conversations": {"c1": {"title": "你是谁", "created": "09/25 09:29", "messages": [
            {"role": "user", "content": "你是谁"},
            {"role": "assistant", "content": "我是 moz 啊"},
            # 探针问句 + 一句"看着人畜无害"的回复：只能靠配对关系识别，不能靠字样
            {"role": "user", "content": "自测探针：请用一句话回答你好"},
            {"role": "assistant", "content": "你好，我在呢。"},
            {"role": "user", "content": "这张图是什么颜色？只回颜色名", "image": "data:image/png;base64,xxx"},
            {"role": "assistant", "content": "我这边看到的是一片纯白，没图案"},
        ]}}}
        conn.execute("INSERT OR REPLACE INTO conversations VALUES (?,?)",
                     (USER, json.dumps(blob, ensure_ascii=False)))
        conn.execute("DELETE FROM working_memory WHERE user_id = ?", (USER,))
        conn.execute("INSERT INTO working_memory (user_id,summary,open_topics,current_emotion,updated_at) "
                     "VALUES (?,?,?,?,?)", (USER, "用户反复追问这张图的颜色", "[]", "neutral", now))
        conn.commit()

        CP.apply_(CP.scan(conn, now - 60), conn, db_path=tmp, api=None)

        left = [json.loads(r[0])["content"] for r in
                conn.execute("SELECT data FROM memories WHERE user_id = ?", (USER,)).fetchall()]
        msgs = json.loads(conn.execute("SELECT data FROM conversations WHERE user_id = ?", (USER,))
                          .fetchone()[0])["conversations"]["c1"]["messages"]
        wm = conn.execute("SELECT summary FROM working_memory WHERE user_id = ?", (USER,)).fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE user_id = ? AND content LIKE '%颜色%'",
                           (USER,)).fetchone()[0]
        bad = []
        if "妈妈生日是 10 月 5 日" not in left:
            bad.append(f"真实记忆被错删：{left}")
        if any("颜色" in c for c in left):
            bad.append("探针记忆没删掉")
        if [m["content"] for m in msgs] != ["你是谁", "我是 moz 啊"]:
            bad.append(f"对话清理结果不对：{msgs}")
        if any("image" in m for m in msgs):
            bad.append("测试图片还留在会话里")
        if (wm or "").strip():
            bad.append("工作记忆没清空")
        if fts:
            bad.append("全文检索还能搜到删掉的测试句")
        return "; ".join(bad) or True
    finally:
        conn.close()
        shutil.rmtree(tmp.parent, ignore_errors=True)


def css_uses_dvh():
    """PWA 窗口矮时输入框被顶掉：布局高度必须用 dvh，vh 含地址栏。

    连 .tsx 里的内联 style 一起扫——崩溃页的 100vh 就写在内联样式里，只查 .css 会漏。
    """
    bad = []
    src = ROOT / "frontend" / "src"
    for f in sorted(list(src.rglob("*.css")) + list(src.rglob("*.tsx"))):
        text = f.read_text(encoding="utf-8", errors="replace")
        hits = re.findall(r"\b\d+(?:\.\d+)?vh\b", text)
        if hits:
            bad.append(f"{f.name}×{len(hits)}")
    if bad:
        return "还在用 vh，改 dvh：" + " ".join(bad)
    return True


def frontend_no_junk():
    """孤儿样式/备份文件：曾经因为弹窗样式只在 ModelDialog.new.css 里而整块裸奔。"""
    comp = ROOT / "frontend" / "src" / "components"
    files = [p.name for p in comp.iterdir()]
    junk = [f for f in files if f.endswith((".bak", ".timestamp")) or f.endswith(".new.css")]
    if junk:
        return f"杂物文件：{junk}"
    tsx_text = "\n".join(p.read_text(encoding="utf-8") for p in comp.glob("*.tsx"))
    orphans = [f for f in files if f.endswith(".css") and f not in tsx_text]
    return True if not orphans else f"没人 import 的孤儿样式：{orphans}"


# ── 5. 数据不变量（防止一夜跑下来悄悄跑坏）────────────────
def db_invariants():
    if not DB.exists():
        return "moz.db 不存在"
    conn = sqlite3.connect(DB)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return "SQLite integrity_check 失败"
        probe = conn.execute("SELECT COUNT(*) FROM care_items WHERE title LIKE '%__selftest%'").fetchone()[0]
        probe += conn.execute("SELECT COUNT(*) FROM care_log WHERE text LIKE '%__selftest%'").fetchone()[0]
        probe += conn.execute("SELECT COUNT(*) FROM care_settings WHERE user_id LIKE '__selftest%'").fetchone()[0]
        # --full 探针会走真实 /api/chat，测试内容会变成"用户说过的话"落进长期记忆。
        # 探针跑完自己负责擦干净（probe_cleanup），这条就是防止它没擦干净。
        # 只认探针原话这种"用户不会照着念"的字样，别拿"图/颜色"这种常用词误伤真实数据。
        TEST_SAYS = ("自测探针", "只回颜色名", "只说颜色名")
        for table, col in (("memories", "data"), ("conversations", "data"), ("working_memory", "summary")):
            for phrase in TEST_SAYS:
                probe += conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{phrase}%",)
                ).fetchone()[0]
        if probe:
            return f"测试数据残留 {probe} 条"
        return True
    finally:
        conn.close()


def weather_probe():
    from weather import get_weather
    w = get_weather("浙江", "杭州", force=True)
    if w is None:
        return "warn"  # 外网不通不是产品缺陷
    if not all(k in w for k in ("rain_today", "rain_tomorrow", "degree")):
        return "天气字段缺失"
    return True


def avatar_probe():
    code, payload = http(f"/avatar/{USER}", raw=True)
    if code == 404:
        return True  # 没设头像也合法
    if code != 200:
        return f"头像读取 {code}"
    return True if payload[:3] == b"\xff\xd8\xff" else "返回的不是 JPEG"


# ── 6. 慢检：真实对话往返 ────────────────────────────────
PROBE_START = time.time()  # 本次跑之前库里不该有属于"这段时间"的测试内容


def read_care_settings(user_id):
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("SELECT data FROM care_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def write_care_settings(user_id, data):
    if data is None:
        return
    conn = sqlite3.connect(DB)
    try:
        conn.execute("UPDATE care_settings SET data = ? WHERE user_id = ?", (data, user_id))
        conn.commit()
    finally:
        conn.close()


def probe_cleanup(rounds=6, gap=8):
    """把 --full 探针在真实用户库里留下的痕迹擦干净。

    记忆抽取是后端异步任务，比对话晚到十几秒，所以一遍扫不干净：
    每轮先等一下再扫，扫到没东西为止。只删命中探针特征、且在本次探针窗口内的，
    宁可漏擦不可错删（判定复用 tools/clean_probe_data.py，别写两套）。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import clean_probe_data as CP

    total = 0
    for _ in range(rounds):
        time.sleep(gap)
        moved = 0
        conn = sqlite3.connect(DB)
        try:
            res = CP.scan(conn, PROBE_START - 5, include_summaries=True)
            moved = len(res["mem_drop"]) + sum(res["conv_drop"].values()) + len(res["working"])
            if moved:
                CP.apply_(res, conn, reset_talk_score=False)
        finally:
            conn.close()
        total += moved
        if not moved:
            break
    return total


def full_chat_roundtrip():
    _, before = http(f"/conversations/{USER}")
    cid = before.get("current_id")
    _, msgs_before = http(f"/conversations/{USER}/{cid}") if cid else (200, {"messages": []})
    n_before = len(msgs_before.get("messages", []))
    body = {"message": "自测探针：请用一句话回答你好", "conversation_id": cid,
            "conversation_history": [], "image_data": None}
    t0 = time.time()
    req = urllib.request.Request(f"{API}/chat/{USER}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    got_reply, err = False, None
    with urllib.request.urlopen(req, timeout=300) as res:
        for raw in res:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data: "):
                continue
            p = line[6:]
            if p == "[DONE]":
                break
            try:
                ev = json.loads(p)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "reply":
                got_reply = True
            if ev.get("type") == "error":
                err = ev.get("text")
    if err:
        return f"对话报错: {err}"
    if not got_reply:
        return "没有收到完整回复"
    secs = round(time.time() - t0, 1)
    # 抽取是后台任务，稍等再看有没有污染
    time.sleep(8)
    _, items = http(f"/care/items?user_id={USER}")
    for i in items.get("items", []):
        if "自测探针" in i["title"]:
            http(f"/care/items/{i['id']}?user_id={USER}", "DELETE")
    return True if secs < 180 else f"warn: 回复耗时 {secs}s"


def vision_payload_contract():
    """带图必 400 的回归：发给模型的图片必须是完整 data URL，且 text 不能是空串。"""
    sys.path.insert(0, str(ROOT / "backend"))
    import emotion_graph as eg
    raw_b64 = "iVBORw0KGgoAAAANSUhEUg"  # 没前缀的裸 base64
    msgs = eg._build_dialogue_messages({
        "user_message": "", "image_data": "data:image/png;base64," + raw_b64,
        "conversation_history": [], "memory_context": "", "emotion_analysis": None,
        "emotion_summary": "", "working_memory_text": "", "profile_context": "",
    })
    blocks = msgs[-1].content
    if not isinstance(blocks, list):
        return "带图时不该退回纯文本"
    url = blocks[0]["image_url"]["url"]
    if not url.startswith("data:image/"):
        return f"image_url 不是完整 data URL: {url[:30]}"
    text = blocks[1]["text"]
    if not text.strip():
        return "纯图片时 text 为空，中转会判 Invalid chat format"
    # 历史里的图片必须被剔掉，否则请求体会一路涨到中转拒收
    hist = eg._sanitize_history([
        {"role": "user", "content": "x" * 9000, "image": "data:image/png;base64," + raw_b64}
    ] * 40)
    if len(hist) > eg.HISTORY_MAX_MESSAGES:
        return f"历史没截断：{len(hist)} 条"
    if any(len(m["content"]) > eg.HISTORY_MAX_CHARS + 40 for m in hist):
        return "历史单条没限长"
    if any("data:image" in m["content"] for m in hist):
        return "历史里还留着图片 data URL"
    return True


def model_capability_roundtrip():
    """多模态声明要能读回来：否则用户勾了"能看图"，刷新就说不支持。"""
    _, cfg = http("/config/model")
    if "multimodal" not in cfg or "multimodal_declared" not in cfg:
        return f"缺字段: {sorted(cfg)}"
    if cfg["multimodal_declared"] and not isinstance(cfg["multimodal"], bool):
        return "declared 为真但 multimodal 不是布尔"
    return True


def export_covers_care():
    """导出必须带上关心那块，否则"备份"恢复出来生日和提醒是空的。"""
    code, data = http(f"/export/{USER}")
    if code != 200:
        return f"导出 {code}"
    care = data.get("care") or {}
    if "items" not in care or "settings" not in care:
        return f"导出缺 care: {sorted(data)}"
    if data.get("version", 0) < 2:
        return f"快照版本没升上来: {data.get('version')}"
    return True


def rate_limit_not_hostile():
    """本地应用翻界面就会被限流打掉，是这夜实测到的：连打 80 次只读接口都得过。"""
    bad = 0
    for _ in range(80):
        code, _r = http("/health", timeout=5)
        if code == 429:
            bad += 1
    return True if bad == 0 else f"80 次里有 {bad} 次被 429"


def vision_probe():
    """中转的视觉稳不稳只报事实，不当失败：同一张图它确实会一会儿对一会儿白。"""
    import base64 as b64
    import io as _io
    from PIL import Image

    def png(color):
        buf = _io.BytesIO()
        Image.new("RGB", (200, 200), color).save(buf, format="PNG")
        return "data:image/png;base64," + b64.b64encode(buf.getvalue()).decode()

    def ask(img):
        body = {"message": "这张图是什么颜色？只回颜色名", "conversation_history": [], "image_data": img}
        req = urllib.request.Request(f"{API}/chat/{USER}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        out = []
        with urllib.request.urlopen(req, timeout=200) as res:
            for raw in res:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "token":
                    out.append(ev.get("text", ""))
                elif ev.get("type") in ("done", "error"):
                    break
        return "".join(out)

    red = ask(png((220, 30, 30)))
    green = ask(png((30, 120, 40)))
    hits = ("红" in red) + ("绿" in green)
    if hits == 2:
        return True
    return f"warn: 看图不稳（红→{red[:18] or '空回复'} / 绿→{green[:18] or '空回复'}）"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    check("后端只读接口全通", run_reads)
    check("会话详情可读", conversation_detail)
    check("非法入参被拒绝", bad_inputs)
    check("超大头像被拒绝", oversized_avatar)
    check("关心事项往返", care_roundtrip)
    check("关心设置往返", care_settings_roundtrip)
    check("模型列表 401 提示", model_list_probes)
    check("图片请求格式契约", vision_payload_contract)
    check("多模态声明可读回", model_capability_roundtrip)
    check("导出覆盖关心事项", export_covers_care)
    check("限流不误伤本地", rate_limit_not_hostile)
    check("安静时段判定", engine_logic)
    check("日期抽取与去噪", extractor_logic)
    check("存储与预算与ack竞态", store_logic)
    check("关心两开关独立", care_switch_logic)
    check("数据库不变量", db_invariants)
    check("探针清理器不错杀", probe_cleaner_works)
    check("天气源可用", weather_probe)
    check("头像字节流", avatar_probe)
    check("前端高度用dvh", css_uses_dvh)
    check("前端无孤儿杂物", frontend_no_junk)
    if a.full:
        # 探针会污染"话多话少"的学习值（探针全是一句话回答），跑完原样还回去
        care_before = read_care_settings(USER)
        try:
            check("真实对话往返", full_chat_roundtrip, tier="full")
            check("中转看图能力", vision_probe, tier="full")
        finally:
            try:
                print(f"[cleanup] 探针擦掉测试痕迹 {probe_cleanup()} 处")
            except Exception as e:
                print(f"[cleanup] 探针清理失败，请跑 tools/clean_probe_data.py：{type(e).__name__}: {e}")
            write_care_settings(USER, care_before)
        check("探针没留残渣", db_invariants, tier="full")

    fails = [r for r in results if r["status"] == "fail"]
    warns = [r for r in results if r["status"] == "warn"]
    if a.json:
        print(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                          "fail": len(fails), "warn": len(warns), "results": results},
                         ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = {"pass": " OK ", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}[r["status"]]
            print(f"[{mark}] {r['name']:<22} {r['ms']:>6}ms  {r['detail']}")
        print(f"\n合计 {len(results)} 项：{len(fails)} 失败 / {len(warns)} 警告")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

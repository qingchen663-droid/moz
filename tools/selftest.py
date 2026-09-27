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
import math
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
    "/config/model", "/config/model-presets", "/config/saved-models", "/config/prompt", "/users",
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
    got = X._fallback_items("我妈生日是10月5日，我下周三还有个面试")
    birthday = next((g for g in got if g.get("kind") == "birthday"), None)
    if not birthday:
        fail.append("兜底认不出带空格的中文月日")
    elif len(birthday["title"]) > 8 or "月" in birthday["title"]:
        fail.append(f"兜底标题该只留事本身：{birthday['title']}")
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


def care_harvest_roundtrip():
    """"不用你填表，聊到生日面试它自己记下"——这条卖点的最短链路（规则兜底，不碰大模型）。

    大模型挂了或没配 Key 时走的就是这条路，所以它必须自己能记下事、不重复、不乱记。
    """
    from care_store import CareStore
    import care_extractor as X

    u = "__selftest_harvest__"
    s = CareStore()
    try:
        for t in ("care_items", "proactive_queue", "care_log"):
            s._conn().execute(f"DELETE FROM {t} WHERE user_id=?", (u,))
        s._conn().execute("DELETE FROM care_settings WHERE user_id=?", (u,))
        s._conn().commit()

        X.harvest(s, u, "我妈生日是 10 月 5 日", "")
        items = s.list_items(u)
        if not items:
            return "说了一句带日子的话，什么都没记下来"
        got = items[0]
        if got["kind"] != "birthday" or got["repeat"] != "yearly":
            return f"生日没被认成每年重复：{got['kind']}/{got['repeat']}"
        if not got["due_at"] or got["due_at"] < time.time():
            return f"生日日期没折成未来的时间点：{got['due_at']}"
        if "10 月" in got["title"] or "10月" in got["title"]:
            return f"标题里还带着日期，读起来像半句话：{got['title']}"
        n0 = len(items)
        X.harvest(s, u, "我妈生日是 10 月 5 日", "")
        after = len(s.list_items(u))
        if after != n0:
            return f"同一件事说了两遍被记成 {after - n0} 条新的（该去重）"
        X.harvest(s, u, "今天天气不错", "")
        X.harvest(s, u, "你好", "")
        X.harvest(s, u, "我生日是 2 月 30 日", "")
        after = len(s.list_items(u))
        if after != n0:
            return f"闲聊/坏日期被记成了事项（多了 {after - n0} 条）"
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


def care_extract_llm_probe():
    """自动记事的主路径（大模型抽取）灵不灵：只问不写库，零污染。

    中转经常整条回复是空的（这轮看图探针就撞上过一次），那属于环境问题报 warn；
    只有"给了结果但是错了"才算缺陷。
    """
    sys.path.insert(0, str(ROOT / "backend"))
    import care_extractor as X

    try:
        from llm_config import get_llm_client
        client = get_llm_client(temperature=0.0, use_thinking=False)
    except Exception as e:
        return f"warn: 拿不到模型客户端（{type(e).__name__}）"

    items = X.extract("我妈生日是10月5日，我下周三还有个面试", "好，我都记下了", client)
    if not items:
        return "warn: 模型那条没出可用结果，兜底也没接住（多半是空回复）"
    if all(str(i.get("why", "")).startswith("规则兜底") for i in items):
        return "warn: 只有规则兜底在撑着，模型那条是空的"
    birthday = next((i for i in items if i.get("kind") == "birthday"), None)
    if not birthday:
        return f"生日没认出来：{items}"
    if birthday.get("repeat") != "yearly" or not str(birthday.get("due_date") or "").endswith("10-05"):
        return f"生日字段不对：{birthday}"
    if X.extract("我最近睡不好，唉", "听起来挺难受的", client):
        return "把情绪当成待办事项记下来了"
    return True


def saved_models_roundtrip():
    """"我存的模型"的存/切/删/上限：全程只碰临时文件。

    真实的 runtime_model_config.json 里是用户的真 Key，绝不能在自测里被覆盖，
    所以这里把 model_config 的两个路径临时挪到 tmp 目录。
    """
    import shutil
    import tempfile

    sys.path.insert(0, str(ROOT / "backend"))
    import model_config as M

    tmp = Path(tempfile.mkdtemp())
    real_cfg, real_saved = M._CONFIG_PATH, M._SAVED_PATH
    try:
        M._CONFIG_PATH = str(tmp / "runtime.json")
        M._SAVED_PATH = str(tmp / "saved.json")

        M.save_active_config(model="a-model", base_url="https://a/v1", api_key="sk-A",
                             use_thinking=True, multimodal=False)
        M.save_current_as("A 家")
        M.save_active_config(model="b-model", base_url="https://b/v1", api_key="sk-B")
        M.save_current_as("B 家")
        items = M.load_saved_models()
        bad = []
        if [i["name"] for i in items] != ["A 家", "B 家"]:
            bad.append(f"列表不对：{[i['name'] for i in items]}")
        if not any(i.get("api_key") == "sk-A" for i in items):
            bad.append("密钥没跟着存下来，切回去会 401")
        M.apply_saved_model("A 家")
        cfg = M.load_active_config()
        if cfg["model"] != "a-model" or cfg["api_key"] != "sk-A" or not cfg["use_thinking"]:
            bad.append(f"切回去没还原全套：{ {k: v for k, v in cfg.items() if k != 'api_key'} }")
        M.save_current_as("A 家")
        if len(M.load_saved_models()) != 2:
            bad.append("同名存两次变成了两条")
        if not M.delete_saved_model("B 家") or M.delete_saved_model("没这个名字"):
            bad.append("删除结果不对")
        for n in range(M.SAVED_MAX + 3):
            M.save_current_as(f"p{n}")
        if len(M.load_saved_models()) > M.SAVED_MAX:
            bad.append(f"超过上限还在存：{len(M.load_saved_models())}")
        try:
            M.apply_saved_model("没这个名字")
            bad.append("切到不存在的名字该报错")
        except KeyError:
            pass
        return "; ".join(bad) or True
    finally:
        M._CONFIG_PATH, M._SAVED_PATH = real_cfg, real_saved
        shutil.rmtree(tmp, ignore_errors=True)


def capacity_policy_check():
    """"长期记忆"的容量底线：不许悄悄把用户说过的东西弄没。

    实测过：上限 300 时，灌到 1000 条 planted 记忆，最早那批先被静默归档、
    再被物理删除，界面上只表现为"长期记忆 · 300 条"不再涨。
    """
    import shutil
    import tempfile

    sys.path.insert(0, str(ROOT / "backend"))
    import memory_manager as MM

    cls = MM.MemoryManager
    bad, soft = [], []
    if cls.MAX_ACTIVE_MEMORIES < 2000:
        bad.append(f"活跃上限只有 {cls.MAX_ACTIVE_MEMORIES}：每天聊几句的人几周就撞顶，之后旧记忆静默归档")
    if cls.ARCHIVE_DELETE_AFTER != 0:
        bad.append(f"归档仍会被硬删（阈值 {cls.ARCHIVE_DELETE_AFTER}），用户说过的话会凭空消失")
    if cls.CONSOLIDATION_TRIGGER > cls.MAX_ACTIVE_MEMORIES:
        bad.append("巩固触发点高于活跃上限，等于永远不跑")

    d = tempfile.mkdtemp()
    try:
        m = cls(storage_path=d, db_path=os.path.join(d, "cap.db"))
        m.embedding_service.get_embedding = lambda text: None
        old = time.time() - 400 * 86400
        item = m.add_memory("cap-user", "很久没再提过的一件小事", confidence=0.6)
        item.created_at = old
        item.last_accessed = old
        item.access_count = 0
        item.importance = 0.12
        archived = m.prune_memories("cap-user")
        after = m.memories["cap-user"][item.id].importance
        if not archived:
            soft.append(f"warn: 遗忘曲线的归档分支跑不到（衰减后 importance={after:.3f} 正好等于阈值 0.1，"
                        "判据是严格小于）——文档里说会'慢慢淡忘'，实际只会降到地板")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(bad) or (soft[0] if soft else True)


def keyword_parity_check():
    """检索提速的红线：结果必须和"每次查询全库重算"的老写法逐条、逐分值一模一样。

    老算法在这里重写一遍当标尺（含它自带的匹配分函数），因为排序走的是 RRF 名次，
    末位浮点差都可能换掉用户看到的那 5 条记忆。顺带钉住两件事：
    索引缓存真的建起来了，且改情感/改状态会让它作废。
    """
    import shutil
    import tempfile

    import memory_manager as MM

    st = MM.MemoryManager._search_tokens

    def ref_match(memory, query):
        query_lower = query.lower()
        content_lower = memory.content.lower()
        clean_query = "".join(c for c in query_lower if c.isalnum())
        clean_content = "".join(c for c in content_lower if c.isalnum())
        if len(clean_query) >= 2 and clean_query in clean_content:
            return 0.8
        if len(clean_content) >= 2 and clean_content in clean_query:
            return 0.8
        common_count = 0
        for word_len in range(2, min(5, len(clean_query) + 1)):
            for i in range(len(clean_query) - word_len + 1):
                if clean_query[i:i + word_len] in clean_content:
                    common_count += 1
        if common_count > 0:
            return common_count / max(len(clean_query), 1) * (0.6 + memory.importance * 0.4)
        qw = set(query_lower.replace("，", " ").replace("。", " ")
                 .replace("！", " ").replace("？", " ").split())
        cw = set(content_lower.replace("，", " ").replace("。", " ")
                 .replace("！", " ").replace("？", " ").split())
        common = qw & cw
        if common:
            return len(common) / len(qw | cw) * (0.6 + memory.importance * 0.4)
        return 0.0

    def ref_search(memories, query, emotion_filter, min_importance):
        docs = [m for m in memories.values()
                if m.status == MM.MemoryStatus.ACTIVE
                and m.importance >= min_importance
                and not (emotion_filter and m.emotion != emotion_filter)]
        if not docs:
            return []
        query_tokens = st(query)
        doc_tokens = {m.id: st(m.content) for m in docs}
        df = {}
        for tokens in doc_tokens.values():
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        avgdl = sum(len(t) for t in doc_tokens.values()) / max(len(docs), 1)
        k1, b = 1.2, 0.75
        out = []
        for memory in docs:
            tokens = doc_tokens[memory.id]
            counts = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            bm25 = 0.0
            for token in query_tokens:
                if token not in counts:
                    continue
                idf = math.log(1 + (len(docs) - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
                tf = counts[token]
                bm25 += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(tokens) / max(avgdl, 1)))
            score = ref_match(memory, query) + min(0.4, bm25 / max(len(query_tokens), 1))
            if score > 0:
                out.append((score, memory.id))
        out.sort(key=lambda x: x[0], reverse=True)
        return out

    corpus = [
        ("用户叫林清，住在杭州", None, 0.95),
        ("用户最喜欢的花是紫鸢尾，谁都不能碰", MM.EmotionType.HAPPY, 0.8),
        ("用户说妈妈下周三要做白内障手术", MM.EmotionType.ANXIOUS, 0.9),
        ("User prefers dark roast coffee and hates small talk at meetings", None, 0.7),
        ("那条没确认的传闻：可能换工作", None, 0.3),
        ("！！", None, 0.6),
        ("用户提过一次驾照换证，后来办了", None, 0.65),
    ]
    # 再垫一批：DF/平均长度/idf 只在语料大了以后才有区分度，排序差异也出在这里
    bits = ["春天", "跑步", "咖啡", "代码", "周末", "妈妈", "手术", "钥匙", "雨伞", "夜班"]
    for i in range(60):
        corpus.append((f"用户说{bits[i % 10]}那件事和{i}有关，另外 {bits[(i * 3) % 10]} 他不喜欢",
                       None if i % 4 else MM.EmotionType.SAD, 0.5 + (i % 5) / 10))
    queries = ["用户叫什么名字", "紫鸢尾", "白内障手术什么时候", "coffee", "妈妈",
               "！", "", "换工作", "用户提过一次驾照换证，后来办了", "dark roast coffee",
               "春天和咖啡", "和3有关", "夜班 钥匙"]
    filters = [(None, 0.0), (MM.EmotionType.HAPPY, 0.0), (None, 0.5), (None, 0.95)]

    d = tempfile.mkdtemp()
    diffs = []
    try:
        m = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "parity.db"))
        m.embedding_service.get_embedding = lambda text: None
        m.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
        ids = []
        for content, emotion, confidence in corpus:
            kwargs = {"confidence": confidence}
            if emotion:
                kwargs["emotion"] = emotion
            ids.append(m.add_memory("parity-user", content, **kwargs).id)
        # 软删一条：状态变了但条数没变，只有正确的作废逻辑能看出来
        m.soft_delete_memory("parity-user", ids[-1])
        store = m._get_user_memories("parity-user")

        for query in queries:
            for emotion_filter, min_importance in filters:
                want = ref_search(store, query, emotion_filter, min_importance)
                got = [(score, memory.id) for score, memory in
                       m._keyword_search_raw(store, query, emotion_filter, min_importance,
                                             user_id="parity-user")]
                if got != want:
                    diffs.append(f"「{query}」filter={emotion_filter}/min={min_importance}："
                                 f"{len(got)} 条 vs 老算法 {len(want)} 条")
                    for a, b in zip(got, want):
                        if a != b:
                            diffs[-1] += f" 首个差异 {a} ≠ {b}"
                            break

        active_docs = sum(1 for x in store.values() if x.status == MM.MemoryStatus.ACTIVE)
        stats = m.get_keyword_index_stats()
        if stats["cached_docs"] < active_docs:
            diffs.append(f"关键词索引没生效：cached_docs={stats['cached_docs']}/{active_docs}")
        sig = m._keyword_index_cache["parity-user"]["signature"]
        m.update_emotion("parity-user", ids[1], MM.EmotionType.SAD, 0.5)
        if m._keyword_index_cache.get("parity-user", {}).get("signature") == sig:
            diffs.append("改情感标签没作废索引缓存（用户改了情绪标注，检索还是旧结果）")
        joy = m.search_memories("parity-user", "紫鸢尾", emotion_filter=MM.EmotionType.HAPPY)
        if any(i.id == ids[1] for i in joy):
            diffs.append("作废不彻底：改成 SAD 后仍被 HAPPY 过滤检索命中")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(diffs[:4]) if diffs else True


def care_link_graph_check():
    """事件关联：到点提醒时得能把"和这件事连着线的旧事"一起想起来。

    钉住四件产品上不能坏的事：
    1. 「妈妈生日」能连到「妈妈喜欢养花」，靠的是同一个人，不是碰巧的词；
    2. 抽人不能误伤（"小姐姐""我小憩了一下"都不算人）；
    3. 到处都出现的词不许当桥梁（80 条都含"体检"时不该互相连出来）；
    4. 用户删了事项/清空记忆以后，关联必须跟着没——不能从图里翻出已经抹掉的旧事。
    """
    import shutil
    import tempfile
    import types

    from care_graph import ITEM, MEMORY, CareGraph, extract_persons
    from care_store import CareStore
    import care_engine as E

    bad = []
    d = tempfile.mkdtemp(prefix="moz-selftest-graph-")
    user = "__selftest_links__"
    mm = None
    try:
        import memory_manager as MM
        store = CareStore(os.path.join(d, "moz.db"))
        graph = CareGraph(os.path.join(d, "moz.db"))
        mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "moz.db"))
        mm.embedding_service.get_embedding = lambda text: None
        mm.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
        mom = store.add_item(user, "妈妈生日", kind="birthday", source="auto")
        claim = store.add_item(user, "项目答辩", kind="event", source="auto")
        store.add_item(user, "姥姥体检", kind="health", source="manual")
        yanghua = mm.add_memory(user, "用户的妈妈喜欢养花", confidence=0.9)
        mm.add_memory(user, "用户下周三的项目答辩在总部三楼，负责人是老郑", confidence=0.9)
        hua = mm.add_memory(user, "用户的猫叫团子，五岁橘猫", confidence=0.9)

        graph.sync_all(store, mm, user)
        linked = {r["label"]: r for r in graph.related(user, ITEM, mom["id"], limit=5)}
        if not any("养花" in label for label in linked):
            bad.append(f"「妈妈生日」没连到「妈妈喜欢养花」：{list(linked)}")
        if any("团子" in label for label in linked):
            bad.append("不相干的记忆（猫）被连进了妈妈生日的邻居")
        if not any("答辩" in r["label"] for r in graph.related(user, ITEM, claim["id"], limit=5)):
            bad.append("「项目答辩」没连到答辩那条记忆")
        if not any(("关于" in line or "相关" in line or "还记着" in line)
                   for line in graph.context_lines(user, ITEM, mom["id"])):
            bad.append("关联没换算成能进提示词的一句话")

        if extract_persons("小姐姐你好，我小憩了一会儿") != []:
            bad.append("抽人误伤了：小姐姐/小憩 不算新的人物")
        if extract_persons("我妈生日是10月5日") != ["妈妈"]:
            bad.append(f"「我妈」没归成「妈妈」：{extract_persons('我妈生日是10月5日')}")

        before = graph.stats(user)["edges"]
        graph.sync_all(store, mm, user)
        after = graph.stats(user)["edges"]
        if after != before:
            bad.append(f"重复同步不幂等：边数 {before} → {after}")

        # 热枢纽：所有事项都含"体检"时，它们不该互相连成一片
        hot_store = CareStore(os.path.join(d, "hot.db"))
        hot = CareGraph(os.path.join(d, "hot.db"))
        ids = [hot_store.add_item(user, f"第{i}次体检", kind="health", source="manual")["id"]
               for i in range(80)]
        hot.sync_all(hot_store, None, user)
        if hot.related(user, ITEM, ids[0], limit=3):
            bad.append("80 条都含「体检」时仍然互相连出邻居（热枢纽没降权）")

        # 用户抹掉的事不能从图里漏回来：删记忆（软删）和删事项都要断边
        soft = mm.add_memory(user, "用户的妈妈爱看京剧", confidence=0.9)
        graph.sync_all(store, mm, user)
        if not any("京剧" in r["label"] for r in graph.related(user, ITEM, mom["id"], limit=5)):
            bad.append("新加的记忆没进图")
        mm.soft_delete_memory(user, soft.id)
        graph.sync_all(store, mm, user)
        if any("京剧" in r["label"] for r in graph.related(user, ITEM, mom["id"], limit=5)):
            bad.append("记忆被用户删掉后还当邻居（等于 moz 替用户记得他已经抹掉的东西）")
        store.delete_item(user, mom["id"])
        graph.sync_all(store, mm, user)
        if any(r["id"] == mom["id"] for r in graph.related(user, MEMORY, yanghua.id, limit=5)):
            bad.append("删掉事项后它还被当成邻居（用户抹掉的事会从图里漏回来）")

        # 清空 = 事项、记忆、关联一起没
        gone = graph.clear_user(user)
        if gone <= 0 or graph.stats(user)["edges"]:
            bad.append(f"clear_user 没清干净关联（清了 {gone} 条，还剩 {graph.stats(user)['edges']}）")

        # 到点提醒的候选要带上关联上下文，并且真的进了措辞提示词
        today_noon = dt.datetime.now().replace(hour=13, minute=0, second=0, microsecond=0)
        due = store.add_item(user, "妈妈生日", kind="birthday", repeat="yearly",
                             due_at=today_noon.timestamp(), source="auto")
        graph.sync_all(store, mm, user)
        cands = E.collect(store, None, user, today_noon.timestamp() + 60, graph=graph)
        mine = [c for c in cands if c.get("ref_id") == due["id"]]
        if not mine:
            bad.append(f"生日事项当天没进主动关心候选：{[c.get('title') for c in cands]}")
        elif not mine[0].get("context"):
            bad.append(f"候选没带关联上下文：{mine[0]}")
        captured = {}

        class FakeLLM:
            def invoke(self, msgs):
                captured["system"] = msgs[0].content
                return types.SimpleNamespace(content="妈妈生日呀，她那几盆花最近怎么样？")

        real_llm = E.get_llm_client
        try:
            E.get_llm_client = lambda **kw: FakeLLM()
            said = E._polish(mine[0] if mine else {}, persona="")
        finally:
            E.get_llm_client = real_llm
        if "养花" not in captured.get("system", ""):
            bad.append("关联事实没进措辞提示词（模型压根看不到，等于白建图）")
        elif "花" not in said:
            bad.append(f"润色后的话没落回结果：{said!r}")
    finally:
        conn = getattr(mm._local, "conn", None) if mm is not None else None
        if conn is not None:
            conn.close()
        shutil.rmtree(d, ignore_errors=True)
    return "; ".join(bad[:3]) if bad else True


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
    check("我存的模型可切换", saved_models_roundtrip)
    check("导出覆盖关心事项", export_covers_care)
    check("限流不误伤本地", rate_limit_not_hostile)
    check("安静时段判定", engine_logic)
    check("日期抽取与去噪", extractor_logic)
    check("聊到日子自己记下", care_harvest_roundtrip)
    check("存储与预算与ack竞态", store_logic)
    check("关心两开关独立", care_switch_logic)
    check("数据库不变量", db_invariants)
    check("记忆容量不悄悄删", capacity_policy_check)
    check("检索提速不改排序", keyword_parity_check)
    check("事件关联成图", care_link_graph_check)
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
            check("模型自动记事", care_extract_llm_probe, tier="full")
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

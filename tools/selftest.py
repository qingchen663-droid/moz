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
import http.client
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


def close_db_conn(obj) -> None:
    """关掉自测临时库的 sqlite 连接——连接不关，Windows 上目录就删不掉（§7 第 18 条）。

    `CareStore` / `CareGraph` / `SaveQueue` 有 `_conn()`；**MemoryManager 没有这个方法**，
    它把连接挂在 `self._local` 上。以前凡是建 MemoryManager 的项都照着 CareStore 写
    `m._conn().close()`，外面再套一层 `except Exception: pass`——于是连接其实一直没关过，
    `shutil.rmtree` 在 Windows 上静默失败。第十四轮清点 `/tmp`：攒了 **95 个** `moz-*` 临时库
    （`moz-selftest-graph-*` 44、`moz-selftest-chain-*` 34、`moz-turn-queue-*` 12 …），根因就是这个。
    """
    conn = None
    getter = getattr(obj, "_conn", None)
    if callable(getter):
        try:
            conn = getter()
        except Exception:
            conn = None
    if conn is None:
        conn = getattr(getattr(obj, "_local", None), "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


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
    f"/care/save-queue?user_id={USER}", f"/emotion/state?user_id={USER}",
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
        ("/memory/not-a-real-user-%24%24/stats", "GET", None, (400, 403, 404)),
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


def relative_dates_land_right():
    """规则兜底里的相对说法必须落在"对的那个日子"，不能只是"今天+7天"。

    中转挂掉/没配 Key 时走的就是这条路：以前"下周三"被折成下周一、"下个月3号"被折成
    +30 天，日期错了 moz 就会在错的日子当面说错话；时间戳还带着"用户敲字那一刻"的钟点。
    """
    import datetime as _dt
    import care_extractor as X

    WD = "一二三四五六日"                       # weekday(): 0=周一
    today = _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())
    now = _dt.datetime.now()
    fail = []

    def one(text):
        """一句只该记一件事时，返回那条 (本地时间, 事项)；没记就 None。"""
        got = X._fallback_items(text)
        if not got:
            return None
        it = got[0]
        ts = float(it.get("due_at") or 0)
        if not ts:
            ts = X._to_timestamp(it.get("due_date"), it.get("repeat") == "yearly")
        return (_dt.datetime.fromtimestamp(ts), it) if ts else None

    # 1) 下周X = 下一个自然周的星期 X（不是"今天+7天"）
    for ch, want in (("三", 2), ("五", 4), ("一", 0), ("日", 6)):
        r = one(f"我下周{ch}要去体检")
        if not r:
            fail.append(f"下周{ch}没记下来")
            continue
        d, it = r
        if d.weekday() != want:
            fail.append(f"下周{ch}落在星期{WD[d.weekday()]}")
        if not 7 <= (d.date() - monday).days <= 13:
            fail.append(f"下周{ch}不在下一个自然周：{d:%m-%d}")
        if f"下周{ch}" in it["title"]:
            fail.append(f"标题还带着日期，读起来像半句话：{it['title']}")
        if d.date() <= today:
            fail.append(f"下周{ch}记成了过去：{d:%m-%d}")

    # 2) 裸写和"这周X"= 本周那个星期几，过了就顺延一周；没记只能是因为当天已经过了
    for ch, want in (("三", 2), ("六", 5)):
        r = one(f"我周{ch}要去找医生复诊")
        if not r:
            this_wd = monday + _dt.timedelta(days=want)
            if not (this_wd <= today and (this_wd != today or now.hour >= 9)):
                fail.append(f"本周{ch}不该记却拒了")
            continue
        d, _ = r
        if d.weekday() != want:
            fail.append(f"周{ch}落在星期{WD[d.weekday()]}")
        if not (today <= d.date() <= monday + _dt.timedelta(days=13)):
            fail.append(f"周{ch}离今天太远/太近：{d:%m-%d}")

    # 3) "下个月N号"是真的下个月 N 号，不是今天+30 天
    r = one("我下个月3号要搬家")
    if not r:
        fail.append("下个月3号没记下来")
    else:
        d, _ = r
        nm = today.month % 12 + 1
        if (d.month, d.day) != (nm, 3):
            fail.append(f"下个月3号折成了 {d:%Y-%m-%d}")
        if (d.hour, d.minute) != (9, 0):
            fail.append(f"没给钟点时该落在早上 9 点，实得 {d:%H:%M}")

    # 4) 给了钟点就用它：别把"下午两点"的答辩记成早上九点
    r = one("我下周三下午两点要去做项目答辩")
    if not r:
        fail.append("下周三下午两点没记下来")
    else:
        d, _ = r
        if (d.hour, d.minute) != (14, 0):
            fail.append(f"下午两点折成了 {d:%H:%M}")

    # 5) 明天这类词表说法也别带上"敲字那一刻"的钟点
    r = one("我明天要去复诊")
    if not r:
        fail.append("明天没记下来")
    else:
        d, _ = r
        if d.date() != today + _dt.timedelta(days=1):
            fail.append(f"明天折成了 {d:%m-%d}")
        if d.hour == now.hour and d.minute == now.minute:
            fail.append(f"明天保留了敲字的钟点 {d:%H:%M}")

    # 6) 重复发生的事不许被记成一次性的某月某日
    if one("我每周三都要去体检"):
        fail.append("把每周的事记成了一条一次性的")
    # 7) 没有的那一天宁可不记，也别滚成"下个月的第一天"这种鬼日子
    import calendar as _cal
    if one("我下个月40号要搬家"):
        fail.append("根本没有 40 号，却记下来了")
    r = one("我下个月31号要搬家")
    if r and _cal.monthrange(r[0].year, r[0].month)[1] < 31:
        fail.append(f"{r[0]:%Y-%m} 没有 31 号，却记成了 {r[0]:%Y-%m-%d}")
    return "; ".join(fail) or True


def care_no_dump():
    """没人听得见就别说话；攒下来的过时话不许一次倒给用户。

    第八轮之前节流只记「生成」不记「送到」：应用关掉两天，回来一次性收到 13 条待读
    （实测数字），其中 7 条已经跨了天——而这些话句句带「今天」，隔天说就是说错日子。
    """
    import care_engine as E
    import care_store as S
    import tempfile
    from care_store import CareStore

    class Clock:
        """假时钟：整个模拟（含入库的 created_at）都走它，否则测不到"放了一夜"。"""
        now = 0.0

        @staticmethod
        def time():
            return Clock.now

        @staticmethod
        def sleep(x):
            pass

    u = "__selftest_nodump__"
    real = (E.time, S.time)
    polish = E._polish
    E._last_poll_seen.clear()
    E.note_poll(u)                          # 先验接口接得上：server.py 调的就是这两个函数
    listening = E.someone_listening(u, time.time())
    E.note_poll(u, listening=False)         # 托盘按了暂停：还在轮询，但不算"有人在听"
    paused = E.someone_listening(u, time.time())
    E._last_poll_seen.clear()
    # 用 mkdtemp 不用 TemporaryDirectory：sqlite 连接是线程复用的，退出时删目录会撞
    # WinError 32（同一个坑在 backend/tests 里也兜过）
    d = tempfile.mkdtemp(prefix="moz-nodump-")
    store = CareStore(db_path=os.path.join(d, "moz.db"))
    E.time = S.time = Clock
    E._polish = lambda cand, persona="": E._template(cand)   # 快检门禁不许调模型
    E._last_user_seen.clear()
    E._last_poll_seen.clear()
    try:
        store.save_settings(u, {"talk_mode": "chatty"})
        day = dt.datetime(2026, 3, 10, 9, 0).timestamp()
        store.add_item(u, "项目答辩", kind="event", due_at=day + 3600, source="auto")
        store.add_item(u, "妈妈生日", kind="birthday", due_at=day + 7200,
                       repeat="yearly", source="auto")
        store.add_item(u, "复诊", kind="health", due_at=day + 7 * 3600, source="auto")

        # ① 一整天没人来取：一条都不该生成
        Clock.now = day
        for k in range(300):                     # 每 60 秒一跳，跳满 5 小时
            E.tick_once(store, None, u, now=Clock.now)
            Clock.now += 60
        silent = store._conn().execute(
            "SELECT COUNT(*) FROM care_log WHERE user_id=?", (u,)).fetchone()[0]

        # ② 有人开着页面（每 20 秒来取一次）：该说的话照样得说，别把闸门焊死
        for k in range(300):
            E._last_poll_seen[u] = Clock.now     # 等价于"这一刻有人开着页面"
            E.tick_once(store, None, u, now=Clock.now)
            Clock.now += 60
        heard = store._conn().execute(
            "SELECT COUNT(*) FROM care_log WHERE user_id=?", (u,)).fetchone()[0] - silent

        ids = [r[0] for r in store._conn().execute(
            "SELECT id FROM proactive_queue WHERE user_id=? ORDER BY created_at", (u,))]

        # ③ 队列里的话：跨天的、放了两小时的问候，都不许再补发
        def backdate(rid, seconds, kind):
            store._conn().execute(
                "UPDATE proactive_queue SET created_at=?, kind=? WHERE id=?",
                (Clock.now - seconds, kind, rid))
            store._conn().commit()

        if len(ids) < 4:
            return f"有人在听时只攒出 {len(ids)} 条待读，测不了过期判定"
        fresh_chat, old_chat, old_event, day_event = ids[:4]
        backdate(old_chat, 3 * 3600, "miss_you")      # 三小时前的"想问问你今天"
        backdate(old_event, 30 * 3600, "event")       # 昨天的"今天不是答辩嘛"
        backdate(day_event, 5 * 3600, "birthday")     # 今天早上的生日祝福
        backdate(fresh_chat, 3600, "miss_you")        # 一小时前那句还算数
        keep = {i["id"] for i in store.pending(u, Clock.now)}
        stale = set(store.stale_ids(u, Clock.now))
        swept = store.ack(u, list(stale))
        left_after_sweep = store.stale_ids(u, Clock.now)
        after = {i["id"] for i in store.pending(u, Clock.now)}

        fail = []
        if not listening:
            fail.append("刚有人来取过却判定成没人在听")
        if paused:
            fail.append("托盘按了暂停还算「有人在听」")
        if silent:
            fail.append(f"没人听得见还是生成了 {silent} 条")
        if heard < 2:
            fail.append(f"有人在听时只说了 {heard} 条，闸门像是焊死了")
        if fresh_chat not in keep:
            fail.append("一小时前那句问候被当过期清掉了")
        if old_chat in keep or old_chat not in stale:
            fail.append("三小时前的问候还打算补发")
        if old_event in keep or old_event not in stale:
            fail.append("隔了一天的「今天」还打算补发")
        if day_event not in keep:
            fail.append("当天的生日祝福不该被清掉")
        if sorted(swept) != sorted(stale):
            fail.append("过期消息没能出队")
        if left_after_sweep or after != keep:
            fail.append(f"出队后还剩 {len(left_after_sweep)} 条过期没清干净")
        if day_event not in after or fresh_chat not in after:
            fail.append("清过期时把还值得说的也带走了")
        # 数字留在各条 fail 里和 RUNLOG 里：门禁约定只有 True 才算过
        return "; ".join(fail) or True
    finally:
        E.time, S.time = real
        E._polish = polish
        E._last_user_seen.clear()
        E._last_poll_seen.clear()
        import shutil
        try:
            store._conn().close()          # Windows 上连接不关就删不掉目录
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)


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

    **日期一律现算，不许写死**（原来写死的「10 月 5 日」在生日当天必然红：那天已经过了 9 点）。
    写死还遮住了真正的口径：生日**就是今天**时要落在今天（到点就提醒），不许推到明年。
    """
    import datetime as _dt
    from care_store import CareStore
    import care_extractor as X

    u = "__selftest_harvest__"
    today = _dt.date.today()
    ahead = today + _dt.timedelta(days=20)          # 还没到：折成未来的那一天
    past = today - _dt.timedelta(days=35)           # 今年已经过掉：必须滚到下一次

    def say(person, d):
        return f"{person}生日是 {d.month} 月 {d.day} 日"

    def local_date(ts):
        return _dt.datetime.fromtimestamp(ts).date() if ts else None

    s = CareStore()
    try:
        for t in ("care_items", "proactive_queue", "care_log"):
            s._conn().execute(f"DELETE FROM {t} WHERE user_id=?", (u,))
        s._conn().execute("DELETE FROM care_settings WHERE user_id=?", (u,))
        s._conn().commit()

        X.harvest(s, u, say("我妈", ahead), "")
        items = s.list_items(u)
        if not items:
            return "说了一句带日子的话，什么都没记下来"
        got = items[0]
        if got["kind"] != "birthday" or got["repeat"] != "yearly":
            return f"生日没被认成每年重复：{got['kind']}/{got['repeat']}"
        if local_date(got["due_at"]) != ahead:
            return f"「{ahead.month} 月 {ahead.day} 日」没折成那一天：{got['due_at']}"
        if got["due_at"] < time.time():
            return f"没过到的日子却给了个过去的时间点：{got['due_at']}"
        if "月" in got["title"] or str(ahead.day) in got["title"]:
            return f"标题里还带着日期，读起来像半句话：{got['title']}"

        # 生日就是今天：不许推到明年（推到明年＝当天一句都不提）
        X.harvest(s, u, say("姥姥", today), "")
        same_day = [i for i in s.list_items(u) if "外婆" in i["title"] or "姥姥" in i["title"]]
        if not same_day:
            return "说「姥姥生日是今天这一号」什么都没记下来"
        if local_date(same_day[0]["due_at"]) != today:
            return (f"生日正好是今天却被推到 {local_date(same_day[0]['due_at'])}，"
                    f"当天就不会提了：{same_day[0]['due_at']}")

        # 今年已经过掉的日子：必须滚到下一次，不许停在刚过去那天
        X.harvest(s, u, say("我爸", past), "")
        rolled = [i for i in s.list_items(u) if "爸爸" in i["title"]]
        if not rolled:
            return "说「我爸生日是 35 天前那个日子」什么都没记下来"
        landed = local_date(rolled[0]["due_at"])
        if not landed:
            return "过掉的那个日子压根没折出时间点"
        if (landed.month, landed.day) != (past.month, past.day):
            return f"35 天前那个日子被折成了别的月日：{landed}"
        if landed < today:
            return f"过掉的日子没滚到下一次，停在 {landed}（今天 {today}）"

        n0 = len(s.list_items(u))
        X.harvest(s, u, say("我妈", ahead), "")
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

        conn.executemany(
            "INSERT INTO save_jobs (user_id,user_message,reply,status,attempts,created_at,updated_at)"
            " VALUES (?,?,?,'done',0,?,?)",
            [(USER, "自测探针：请用一句话回答你好", "你好", now, now),
             ("__selftest_queue", "我妈生日是10月5号", "记下了", now, now),
             (USER, "用户真实说过的一句标记话", "记下了", now, now)],
        )
        conn.commit()

        CP.apply_(CP.scan(conn, now - 60), conn, db_path=tmp, api=None)

        # 落库队列（save_jobs）也存原话，探针那几轮同样得清干净
        queued = conn.execute("SELECT user_id, user_message FROM save_jobs").fetchall()
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
        if any("探针" in (m or "") for _u, m in queued):
            bad.append("落库队列里还留着探针那几轮的原话")
        if any(u == "__selftest_queue" for u, _m in queued):
            bad.append("测试用户的落库任务没清")
        if not any("标记话" in (m or "") for _u, m in queued):
            bad.append("清理器把不像探针的落库任务也吃掉了")
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
    # 先后链的新字段：模型不给 after 不算缺陷（规则兜底那条路会补），但要能看见它给没给
    chain = X.extract("项目答辩下周三，答辩之后一周出结果", "好，我记下了", client)
    notes = [] if any(i.get("after") for i in chain) else ["模型没用 after 字段（规则兜底仍会连）"]
    return "; ".join(notes) or True


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
    from memory_manager import EbbinghausCurve as MM_Ebbinghaus

    cls = MM.MemoryManager
    bad = []
    if cls.MAX_ACTIVE_MEMORIES < 2000:
        bad.append(f"活跃上限只有 {cls.MAX_ACTIVE_MEMORIES}：每天聊几句的人几周就撞顶，之后旧记忆静默归档")
    if cls.ARCHIVE_DELETE_AFTER != 0:
        bad.append(f"归档仍会被硬删（阈值 {cls.ARCHIVE_DELETE_AFTER}），用户说过的话会凭空消失")
    if cls.CONSOLIDATION_TRIGGER > cls.MAX_ACTIVE_MEMORIES:
        bad.append("巩固触发点高于活跃上限，等于永远不跑")

    # 用户 2026-09-27 拍板：不遗忘，不重要的事只是权重降到最低。
    # 所以这里钉的是：衰减到头停在地板上、状态一个都不变、地板权重真的排在后面。
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
        hit_floor = m.prune_memories("cap-user")
        after = m.memories["cap-user"][item.id].importance
        state = m.memories["cap-user"][item.id].status
        if item.id not in hit_floor:
            bad.append(f"400 天没提的事没降到权重地板（importance={after:.3f}，地板 "
                       f"{MM_Ebbinghaus.MIN_IMPORTANCE}）")
        if after < MM_Ebbinghaus.MIN_IMPORTANCE - 1e-9:
            bad.append(f"权重掉到地板以下了（{after:.3f}）：那不是淡忘，是丢")
        if state != MM.MemoryStatus.ACTIVE:
            bad.append(f"衰减把记忆转成了 {state}——用户要的是不遗忘，只降权")

        # 同样命中关键词的两条，只有权重差：低权重必须明显排在后面
        hi = m.add_memory("cap-user", "用户习惯周六早上去滨江那家馆子吃饭", confidence=0.9)
        lo = m.add_memory("cap-user", "用户随口说过滨江那家馆子还去过一次", confidence=0.6)
        hi.importance, lo.importance = 0.9, MM_Ebbinghaus.MIN_IMPORTANCE
        s_hi = m._match_score(hi, "滨江那家馆子")
        s_lo = m._match_score(lo, "滨江那家馆子")
        if not s_hi > s_lo * 1.8:
            bad.append(f"最低权重的记忆没被压到后面（{s_lo:.3f} vs {s_hi:.3f}）——"
                       "对外说的只是权重降到最低，实现上必须真的想不起来")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(bad) or True


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
    from memory_governance import is_question_shaped

    def ref_match(memory, query):
        query_lower = query.lower()
        content_lower = memory.content.lower()
        clean_query = "".join(c for c in query_lower if c.isalnum())
        clean_content = "".join(c for c in content_lower if c.isalnum())
        w = (0.45 if is_question_shaped(memory.content)
             else 0.6 if memory.source_type == "ai_reply" else 1.0)
        f = (0.30 + memory.importance * 0.70) * w
        if len(clean_query) >= 2 and clean_query in clean_content:
            return 0.8 * f
        if len(clean_content) >= 2 and clean_content in clean_query:
            return 0.8 * f
        common_count = 0
        for word_len in range(2, min(5, len(clean_query) + 1)):
            for i in range(len(clean_query) - word_len + 1):
                if clean_query[i:i + word_len] in clean_content:
                    common_count += 1
        if common_count > 0:
            return common_count / max(len(clean_query), 1) * f
        qw = set(query_lower.replace("，", " ").replace("。", " ")
                 .replace("！", " ").replace("？", " ").split())
        cw = set(content_lower.replace("，", " ").replace("。", " ")
                 .replace("！", " ").replace("？", " ").split())
        common = qw & cw
        if common:
            return len(common) / len(qw | cw) * f
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
        close_db_conn(m)
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
    mm = store = graph = hot_store = hot = None
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
        mm.add_memory(user, "用户的猫叫团子，五岁橘猫", confidence=0.9)

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
        for obj in (store, graph, hot_store, hot, mm):
            close_db_conn(obj)
        shutil.rmtree(d, ignore_errors=True)
    return "; ".join(bad[:3]) if bad else True


def care_chain_link_check():
    """事件先后链：A 之后 N 天是 B，到点要顺着问 B，而不是再提醒一遍 A。

    链是这套主动关心里最容易"当用户面说错话"的功能：前件认错了，moz 就会在答辩当天
    追问结果。所以这里既验"连得上"，更验"该拒的时候拒"：认不出锚点不连、
    父件删了要断、一天最多问一条、模型挂了也说得出人话。
    """
    import shutil
    import tempfile
    import types

    import care_engine as E
    import care_extractor as X
    from care_graph import ITEM, CareGraph
    from care_store import CareStore

    bad = []
    d = tempfile.mkdtemp(prefix="moz-selftest-chain-")
    user = "__selftest_chain__"
    store = graph = solo = solo_graph = None
    try:
        store = CareStore(os.path.join(d, "c.db"))
        graph = CareGraph(os.path.join(d, "c.db"))
        # 安静时段设成起止相同 = 永不安静，否则半夜跑这项会假失败
        store.save_settings(user, {"quiet_start": "03:00", "quiet_end": "03:00"})
        now = time.time()

        touched = X.harvest(store, user, "项目答辩下周三，答辩之后一周出结果", "", None, graph=graph)
        rows = graph.chain_rows(user)
        items = store.list_items(user)
        if len(rows) != 1:
            bad.append(f"规则兜底没建出先后链：harvest={touched}，链={len(rows)}")
        elif rows[0]["source"] != "rule" or not 0 < float(rows[0]["gap_days"]) <= 8:
            bad.append(f"链的出处或天数不对：{dict(rows[0])}")
        if len(items) < 2:
            bad.append(f"一句话里的两件事只记下 {len(items)} 条：{[i['title'] for i in items]}")

        # 同一句话再说一遍：不许多出事项，也不许多出链
        before_items, before_chains = len(store.list_items(user)), len(graph.chain_rows(user))
        X.harvest(store, user, "项目答辩下周三，答辩之后一周出结果", "", None, graph=graph)
        graph.sync_all(store, None, user)
        graph.sync_all(store, None, user)
        if (len(store.list_items(user)), len(graph.chain_rows(user))) != (before_items, before_chains):
            bad.append(f"重复说不幂等：事项 {before_items}→{len(store.list_items(user))}，"
                       f"链 {before_chains}→{len(graph.chain_rows(user))}")

        # 认不出前件就不许连：宁可不问，也别问错
        solo = CareStore(os.path.join(d, "s.db"))
        solo_graph = CareGraph(os.path.join(d, "s.db"))
        solo_user = user + "_solo"
        X.harvest(solo, solo_user, "下周三体检，之后三天要出差", "", None, graph=solo_graph)
        if solo_graph.chain_rows(solo_user):
            bad.append("「之后三天」没点名前件，还是被硬连成了链")

        # 到点追问：expect = 前件日期 + 间隔；同时那条后件不许再被当孤立事项提醒一遍
        a = store.add_item(user, "面试", kind="checkin", due_at=now - 3 * 86400, source="auto")
        b = store.add_item(user, "出结果", kind="promise", due_at=now, source="auto")
        graph.add_chain(user, b["id"], a["id"], days=3, source="rule", before_title="面试")
        cands = E.collect(store, None, user, now, graph=graph)
        mine = [c for c in cands if c["ref_id"] == b["id"]]
        if len(mine) != 1 or mine[0]["kind"] != "chain":
            bad.append(f"到期链该以 chain 身份问一次，实际：{[(c['kind'], c['title']) for c in mine]}")
        if [c for c in cands if c["kind"] == "chain" and c["ref_id"] != b["id"]]:
            bad.append("链把不该今天问的事也捞出来了")
        if not graph.add_chain(user, b["id"], a["id"], days=3, source="rule", before_title="面试"):
            bad.append("重写同一条链被拒了（一件事只允许一个前件）")

        # 问过了就别再问：mark_fired 之后这条链应当彻底安静
        store.mark_fired(b["id"], now)
        if any(c["kind"] == "chain" and c["ref_id"] == b["id"]
               for c in E.collect(store, None, user, now, graph=graph)):
            bad.append("已经问过的链当天又被拿出来问")

        # 配额：一天最多顺一条链；当天的主动消息够 8 条就一条都不发
        c2 = store.add_item(user, "搬家", kind="event", due_at=now - 86400, source="auto")
        d2 = store.add_item(user, "装宽带", kind="promise", source="auto")
        graph.add_chain(user, d2["id"], c2["id"], days=1, source="rule", before_title="搬家")
        chains_now = [c for c in E.collect(store, None, user, now, graph=graph) if c["kind"] == "chain"]
        if len(chains_now) > 1:
            bad.append(f"一天顺了 {len(chains_now)} 条链，超过上限 1")
        for _ in range(E.MAX_PROACTIVE_PER_DAY):
            store.enqueue(user, "rain", "占位")
        if E.tick_once(store, None, user, now, dry_run=True, graph=graph):
            bad.append("当天主动消息已经到硬上限，还继续发（变成通知轰炸机）")

        # 措辞：提示词里必须同时有前后两件事；模板兜底不能写成通知腔
        cand = mine[0]
        captured = {}

        class FakeLLM:
            def invoke(self, msgs):
                captured["system"] = msgs[0].content
                captured["human"] = msgs[1].content
                return types.SimpleNamespace(content="上次面试完，结果出来了吗？")

        real = E.get_llm_client
        try:
            E.get_llm_client = lambda **kw: FakeLLM()
            said = E._polish(cand, persona="")
        finally:
            E.get_llm_client = real
        if "面试" not in captured.get("system", "") or "出结果" not in captured.get("system", ""):
            bad.append("提示词里没同时给出前后两件事，模型无从顺着问")
        if "面试" not in cand["why"]:
            bad.append(f"why 没交代前件：{cand['why']}")
        if not said.strip():
            bad.append("润色返回空话")
        plain = E._template(cand)
        if any(w in plain for w in ("提醒", "通知", "您好", "请问")):
            bad.append(f"模型挂了时的兜底句像通知：{plain}")

        # 父件没了要断链；链也不许当成枢纽邻居污染 related()
        if a["id"] in [r["id"] for r in graph.related(user, ITEM, b["id"], limit=5)]:
            bad.append("链边被当成了枢纽邻居，related() 被污染")
        store.delete_item(user, a["id"])
        graph.forget(user, ITEM, a["id"])
        graph.sync_all(store, None, user)
        if any(r["before_id"] == a["id"] for r in graph.chain_rows(user)):
            bad.append("删掉前件后链还在（会拿不存在的事去追问用户）")

        # 不传 graph 时行为必须和改动前一致
        if any(c["kind"] == "chain" for c in E.collect(store, None, user, now)):
            bad.append("没接关联图时也会冒链候选（不该悄悄改变老行为）")
    finally:
        for obj in (store, graph, solo, solo_graph):
            close_db_conn(obj)
        shutil.rmtree(d, ignore_errors=True)
    return "; ".join(bad[:4]) if bad else True


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


def save_queue_survives_restart():
    """后台落库不许再挂在内存任务上：重启等于崩溃，队列要能自己捡回来。

    旧实现是 asyncio.create_task 里的 4 次串行模型调用（实测 3~7 分钟），
    --reload 一砸这轮就"聊完白聊"。这里用临时库模拟"跑到一半进程没了"。
    """
    import shutil
    import tempfile

    from save_queue import MAX_ATTEMPTS, SaveQueue, SaveWorker

    bad = []
    d = tempfile.mkdtemp()
    try:
        q = SaveQueue(os.path.join(d, "q.db"))
        if q.enqueue("u1", "我妈生日是10月5号", "记下了") is None:
            bad.append("空实现：一句话都没排进队列")
        q.enqueue("u1", "", "空的不该占位")
        if q.stats()["pending"] != 1:
            bad.append(f"空消息也被排队：{q.stats()}")
        if q.pending_for("u1") != 1:
            bad.append("pending_for 数不对，接口上的排队数会骗人")

        first = q.claim("worker-a")
        if not first or first["status"] != "pending":
            bad.append("claim 没取到任务（取到的还是 running 之外的状态）")
        if q.claim("worker-b") is not None:
            bad.append("同一条任务被两个 worker 同时 claim 走了")
        if q.stats()["running"] != 1:
            bad.append("claim 之后没标 running，重启时无从判断谁是遗骸")

        recovered = q.recover()          # 进程重启：把 running 当遗骸捡回来
        if recovered != 1 or q.stats()["pending"] != 1:
            bad.append(f"重启后没把 running 捡回来（recover={recovered}, {q.stats()}）")
        again = q.claim("worker-c")
        if not again or again["user_message"] != "我妈生日是10月5号":
            bad.append("捡回来的任务读不回原文，等于白排")
        q.mark_done(again["id"])
        if q.stats()["done"] != 1 or q.pending_for("u1") != 0:
            bad.append(f"mark_done 之后状态不干净：{q.stats()}")

        q.enqueue("u1", "再说一次", "好的")
        for _ in range(MAX_ATTEMPTS + 1):
            got = q.claim("w")
            if not got:
                break
            q.mark_failed(got["id"], "中转连接失败")
        st = q.stats()
        if st["failed"] != 1 or st["pending"]:
            bad.append(f"重试没有上限（{st}）：坏任务会永远占着队列")
        if not isinstance(q.reap_stale(max_age=-1), int):
            bad.append("reap_stale 不可用")

        # worker 必须真的调用处理函数并回写状态，否则队列只是个体面摆设
        seen = []

        async def fake(job):
            seen.append(job["user_message"])

        import asyncio

        wq = SaveQueue(os.path.join(d, "w.db"))
        wq.enqueue("u2", "跑一遍就好", "嗯")

        async def once():
            worker = SaveWorker(wq, fake, poll_seconds=0.05)
            task = asyncio.create_task(worker.run())
            deadline = time.time() + 5
            while time.time() < deadline and wq.stats()["done"] != 1:
                await asyncio.sleep(0.05)
            worker.stop()
            task.cancel()

        asyncio.run(once())
        if seen != ["跑一遍就好"] or wq.stats()["done"] != 1:
            bad.append(f"worker 没把任务跑完（跑过 {seen} 次，{wq.stats()}）")

        kept = SaveQueue(os.path.join(d, "p.db"))
        for n in range(6):
            kept.enqueue("u3", f"第{n}轮", "好")
            job = kept.claim("w")
            kept.mark_done(job["id"])
        kept.enqueue("u3", "还没跑", "好")
        kept.prune(keep=2)
        after = kept.stats()
        if after["done"] != 2 or after["pending"] != 1:
            bad.append(f"prune 动了没跑完的任务（{after}）")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    import inspect

    src = inspect.getsource(_load_emotion_graph())
    if "_background_save" in src:
        bad.append("emotion_graph 里还有内存态的 _background_save")
    return "; ".join(bad) or True


def _load_emotion_graph():
    import emotion_graph

    return emotion_graph


def memory_quality_floor():
    """长期记忆的底线：同一件事不许攒成三条，提问和moz自己的话不许冒充用户的事实。

    都用临时库，绝不碰 backend/moz.db——上一轮 --full 探针污染用户库的事不再重复。
    """
    import shutil
    import tempfile

    import memory_manager as MM
    from memory_governance import is_near_duplicate, is_question_shaped, normalized_content

    bad = []
    # 提问形状：? / ？ / 「…吗」都要认，"今天好烦呢"是陈述不是提问
    for text in ["[对话摘要] 用户说：我家猫叫什么来着？几岁了？", "你喜欢吃香菜吗", "这是几点？"]:
        if not is_question_shaped(text):
            bad.append(f"提问没被认出来：{text}")
    for text in ["我家猫叫团子", "今天好烦呢", "[关于用户] 我讨厌吃香菜"]:
        if is_question_shaped(text):
            bad.append(f"陈述被误判成提问：{text}")

    # 近义判据：只差虚词算重复，换主语/换日期不算
    dup = [("我讨厌吃香菜", "用户讨厌吃香菜"), ("我妈喜欢养花", "用户的妈妈喜欢养花"),
           ("我的猫叫团子", "用户的猫叫团子")]
    for a, b in dup:
        if not is_near_duplicate(normalized_content(a), normalized_content(b)):
            bad.append(f"同一件事没认出来：{a} / {b}")
    keep = [("我妈生日是十月初五", "我妈生日是三月初五"), ("我喜欢猫", "他喜欢猫"),
            ("我不吃香菜", "我吃香菜"), ("猫叫团子", "狗叫团子")]
    for a, b in keep:
        if is_near_duplicate(normalized_content(a), normalized_content(b)):
            bad.append(f"两件事被当成重复：{a} / {b}")

    d = tempfile.mkdtemp()
    try:
        m = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "q.db"))
        m.embedding_service.get_embedding = lambda text: None
        # 事实提取走的是大模型：快检门禁不许碰它，这里钉成"什么都没提到"，
        # 让它落到 [对话摘要] 那条回落分支
        m._extract_facts = lambda *a, **k: []
        u = "quality"
        for text in ["[关于用户] 我讨厌吃香菜", "[关于用户] 用户讨厌吃香菜",
                     "[关于用户] 我讨厌吃香菜的"]:
            m.add_memory(u, text, category=MM.MemoryCategory.FACT)
        stored = [x.content for x in m._get_user_memories(u).values()]
        if len(stored) != 1:
            bad.append(f"同一件事攒了 {len(stored)} 条：{stored}")

        m.extract_and_store_facts(u, "我家猫叫什么来着？几岁了？", "团子呀，五岁了",
                                  category=MM.MemoryCategory.FACT)
        added = [x.content for x in m._get_user_memories(u).values() if x.content not in stored]
        if added:
            # 这一条第十四轮改过口径：以前断言"AI 的回复必须落一条"，
            # 实测那种回声占检索前 5 名 12/30、把真答案压到第 3，所以改成一句都不许落
            bad.append(f"一问一答不该留下任何记忆（回答是她自己的话）：{added}")

        m.extract_and_store_facts(u, "我最近在学做酸菜鱼", "先少放点辣椒，别辣到胃",
                                  category=MM.MemoryCategory.FACT)
        pair = [x for x in m._get_user_memories(u).values()
                if x.content not in stored]
        if not any(c.content.startswith("[对话摘要] 用户说：") for c in pair):
            bad.append(f"陈述那句连用户的话都没落：{[c.content for c in pair]}")
        echo = [x for x in pair if "AI回复要点" in x.content]
        if not echo:
            bad.append("陈述那句该成对落下她的要点（回落分支不能整个哑掉）")
        elif echo[0].source_type != "ai_reply":
            bad.append(f"AI 回复没单独标 source_type（{echo[0].source_type}），检索时降不了权")

        m.add_memory(u, "[关于用户] 我妈喜欢养花", category=MM.MemoryCategory.FACT)
        m.add_memory(u, "[对话摘要] AI回复要点：你喜欢养花呀", category=MM.MemoryCategory.FACT,
                     source_type="ai_reply")
        # 库里早就存在的"提问形状"条目也要降权：只挡新写入不够，直接量打分
        m.add_memory(u, "[对话摘要] 用户说：我妈喜欢什么来着？", category=MM.MemoryCategory.FACT)
        scored = {
            x.content: m._match_score(x, "妈妈喜欢养花")
            for x in m._get_user_memories(u).values() if "养花" in x.content or "喜欢什么" in x.content
        }
        mine = [v for k, v in scored.items() if k.startswith("[关于用户]")]
        others = [v for k, v in scored.items() if not k.startswith("[关于用户]")]
        if not mine or not others:
            bad.append(f"打分样本没取到：{scored}")
        elif min(mine) <= max(others):
            bad.append(f"用户自己的事实没排在提问/AI回复之上：{scored}")
    finally:
        close_db_conn(m)
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(bad) or True


def system_copy_not_memory():
    """她自己那句"抱歉，我暂时无法回复"不配占用一条关于用户的长期记忆。

    实测（第十一轮）：模型那条空的时候，4 句对话会存下 7 条记忆，其中两条是
    「抱歉，我暂时无法回复。」和「这个模型名在中转那边没有可用的渠道…」——
    后者以后会被检索当成"用户的事"回忆出来，等于让她复述自己的报错。
    """
    import shutil
    import tempfile

    import memory_manager as MM
    from llm_errors import looks_like_system_copy

    bad = []
    for text in ["", "  ", "抱歉，我暂时无法回复。",
                 "这个模型名在中转那边暂时没有可用的渠道——可能名字写错了。等十几秒再发一次。",
                 "这句我没接住：后台出了点意外（AttributeError）。再发一次试试。"]:
        if not looks_like_system_copy(text):
            bad.append(f"系统文案没被认出来：{text}")
    for text in ["好的，我记下了：你下周三要答辩", "团子呀，今年五岁了，还是只橘猫呢"]:
        if looks_like_system_copy(text):
            bad.append(f"正常回复被误判成系统文案：{text}")

    d = tempfile.mkdtemp()
    try:
        m = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "copy.db"))
        m.embedding_service.get_embedding = lambda text: None
        m._extract_facts = lambda *a, **k: []      # 快检门禁不许碰大模型
        u = "copycheck"
        turns = [("我下周三下午两点要去做项目答辩", "抱歉，我暂时无法回复。"),
                 ("我讨厌吃香菜", "这个模型名在中转那边暂时没有可用的渠道——等十几秒再发一次。"),
                 ("我家猫叫团子，今年五岁", "好的，我记下了：你家有只五岁的橘猫")]
        for user_msg, reply in turns:
            m.extract_and_store_facts(u, user_msg, reply, category=MM.MemoryCategory.FACT)
        stored = [x.content for x in m._get_user_memories(u).values()]
        leaked = [c for c in stored if "无法回复" in c or "可用的渠道" in c or "再发一次" in c]
        if leaked:
            bad.append(f"系统文案进了长期记忆：{leaked}")
        for word in ("答辩", "香菜", "团子"):
            if not any(word in c for c in stored):
                bad.append(f"用户自己的话反而没记下（缺{word}）：{stored}")
        # 数字（3 句落几条）写在 RUNLOG 里；门禁约定只有 True 才算过（§7 第 15 条）
        return "; ".join(bad) or True
    finally:
        close_db_conn(m)
        shutil.rmtree(d, ignore_errors=True)


def same_turn_no_duplicate_facts():
    """同一句话抽出的"一窄一宽"两条事实，不许各占一行记忆。

    第十五轮实测（沙箱第 5 句「我讨厌吃香菜，以后别推荐我带香菜的东西」）落了两条：
      [关于用户] 用户讨厌吃香菜，不希望被推荐带香菜的食物
      [关于用户] 我讨厌吃香菜
    归一后窄的那条**整段**是宽的那条的前缀。反例必须各留：
      「用户的妈妈喜欢养花」(归一 妈喜欢养花) 与「用户喜欢养花」(归一 喜欢养花) 是**后缀**关系，
      差的是一个人——所以判据只认前缀，不认任意包含。
    """
    import shutil
    import tempfile

    from memory_governance import drop_redundant_prefix_facts as drop

    bad = []
    if drop(["用户讨厌吃香菜，不希望被推荐带香菜的食物", "我讨厌吃香菜"]) != [
            "用户讨厌吃香菜，不希望被推荐带香菜的食物"]:
        bad.append("同句一窄一宽没合成一条")
    keep = drop(["用户的妈妈喜欢养花", "用户喜欢养花"])
    if len(keep) != 2:
        bad.append(f"差一个人的两条被误合：{keep}")
    if len(drop(["用户的猫叫团子", "用户的猫今年五岁", "用户的猫是只橘猫"])) != 3:
        bad.append("三件不同的事被并了")
    if drop(["我喜欢猫", "我喜欢猫"]) != ["我喜欢猫"]:
        bad.append("一模一样的两条没去掉")

    d = tempfile.mkdtemp(prefix="moz-selftest-facts-")
    try:
        import memory_manager as MM
        m = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "f.db"))
        m.embedding_service.get_embedding = lambda text: None
        m._extract_facts = lambda *a, **k: [
            "用户讨厌吃香菜，不希望被推荐带香菜的食物", "我讨厌吃香菜"]
        m.extract_and_store_facts("dupe", "我讨厌吃香菜，以后别推荐我带香菜的东西", "好",
                                  category=MM.MemoryCategory.FACT)
        stored = [x.content for x in m._get_user_memories("dupe").values()]
        if len(stored) != 1:
            bad.append(f"整条路径落库 {len(stored)} 条：{stored}")
        return "; ".join(bad) or True
    finally:
        close_db_conn(m)
        shutil.rmtree(d, ignore_errors=True)


def question_answer_not_memory():
    """用户问一句、她答一句，那句回答不配单独占一条"关于用户的事实"。

    第十四轮实测（临时库灌进上一轮沙箱那 11 条原文）：这种 `[对话摘要] AI回复要点：…`
    占检索前 5 名的 12/30（库里只占 4/14），问「有什么吃的我不能吃来着」时
    真答案被压到第 3，前两名是猫的玩笑和老郑那句。
    """
    import shutil
    import tempfile

    import memory_manager as MM

    d = tempfile.mkdtemp(prefix="moz-selftest-echo-")
    bad = []
    try:
        m = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "echo.db"))
        m.embedding_service.get_embedding = lambda text: None
        m._extract_facts = lambda *a, **k: []      # 快检门禁不许碰大模型
        m.extract_and_store_facts("echoq", "我家猫叫什么来着？几岁了？",
                                  "团子呀，五岁的橘猫～ 怎么，团子的名字都能忘？",
                                  category=MM.MemoryCategory.FACT)
        stored = [x.content for x in m._get_user_memories("echoq").values()]
        if stored:
            bad.append(f"一问一答存下 {len(stored)} 条：{stored}")

        m.extract_and_store_facts("echos", "我最近在学做酸菜鱼", "先少放点辣椒，别辣到胃",
                                  category=MM.MemoryCategory.FACT)
        got = [x.content for x in m._get_user_memories("echos").values()]
        if not any("酸菜鱼" in c for c in got):
            bad.append(f"陈述那句连用户的话都没记下：{got}")
        if not any("AI回复要点" in c for c in got):
            bad.append(f"陈述那句该成对存（用户说 + 她的要点），实际：{got}")
        return "; ".join(bad) or True
    finally:
        close_db_conn(m)
        shutil.rmtree(d, ignore_errors=True)


def birthday_not_mislabeled():
    """生日被模型标成"事件提醒"时，界面和到点那句话都会跟着错。

    第十二轮 --full 实测到模型返回
    {"kind":"event","title":"妈妈生日","due_date":"10-05","repeat":"yearly"}，
    于是界面上写「事件提醒 · 妈妈生日 · 每年」，到那天她会问"准备得怎么样了？"。
    """
    import shutil
    import tempfile

    import care_engine as E
    import care_extractor as X
    from care_store import CareStore

    bad = []
    cases = [("event", "妈妈生日", "yearly", "birthday", "yearly"),
             ("birthday", "姥姥生日", "none", "birthday", "yearly"),
             ("health", "每年体检", "yearly", "health", "yearly"),      # 不许误伤
             ("event", "项目答辩", "none", "event", "none"),
             ("person", "结婚纪念日", "yearly", "person", "yearly")]
    for kind, title, repeat, wk, wr in cases:
        got = X.reconcile(kind, title, repeat)
        if got != (wk, wr):
            bad.append(f"{kind}/{title}/{repeat} 归一成 {got}，应为 {(wk, wr)}")

    hi = E._template({"kind": "birthday", "title": "妈妈生日"})
    if "快乐" not in hi:
        bad.append(f"生日模板丢了祝福：{hi}")
    mi = E._template({"kind": "birthday", "title": "姥姥忌日"})
    if "快乐" in mi:
        bad.append(f"忌日说了祝福：{mi}")
    if not mi.strip():
        bad.append("忌日那句是空的")

    BUGGY = [{"kind": "event", "title": "妈妈生日", "due_date": "10-05",
              "repeat": "yearly", "why": "用户明确说妈妈生日是10月5日"}]
    d = tempfile.mkdtemp(prefix="moz-bday-")
    keep = X.extract
    try:
        X.extract = lambda *a, **k: list(BUGGY)
        store = CareStore(db_path=os.path.join(d, "moz.db"))
        u = "bday"
        X.harvest(store, u, "我妈生日是10月5日", "", llm_client=object())
        rows = store.list_items(u)
        if not rows:
            bad.append("生日那条没存下来")
        else:
            it = rows[0]
            if it["kind"] != "birthday" or it["repeat"] != "yearly":
                bad.append(f"模型标错没被纠正：kind={it['kind']} repeat={it['repeat']}")
            # 同一件事再说一遍，也不许把标签改回 event
            X.harvest(store, u, "我妈生日是10月5日", "", llm_client=object())
            again = store.list_items(u)
            if len(again) != 1:
                bad.append(f"同一件生日攒了 {len(again)} 条")
            elif again[0]["kind"] != "birthday":
                bad.append(f"重复一次之后标签又退回 {again[0]['kind']}")
        return "; ".join(bad) or True
    finally:
        X.extract = keep
        try:
            store._conn().close()
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)


def same_fact_one_item():
    """同一件生日换个说法，不许在关心库里攒成三条。

    第十三轮复现（临时库）：去重键是标题整串相等，所以
    「我妈生日是10月5日」「妈妈生日是10月5日」「我妈妈的生日是10月5号」各存一条，
    collect() 到那天早上给出三条候选，心跳每隔 MIN_GAP_SECONDS 说一遍
    「我妈妈的生日快乐呀！」——一句不像人话，三句是骚扰。
    """
    import shutil
    import tempfile

    import care_engine as E
    import care_extractor as X
    from care_store import CareStore

    d = tempfile.mkdtemp(prefix="moz-samefact-")
    bad = []
    try:
        store = CareStore(db_path=os.path.join(d, "c.db"))
        u = "kin"
        for msg in ("我妈生日是10月5日", "妈妈生日是10月5日", "我妈妈的生日是10月5号"):
            X.harvest(store, u, msg, "")
        X.harvest(store, u, "我爸生日是10月6日", "")
        rows = store.list_items(u)
        titles = [it["title"] for it in rows]
        moms = [t for t in titles if "10月" not in t and ("妈" in t)]
        if len(moms) != 1:
            bad.append(f"妈妈的生日攒成 {len(moms)} 条：{titles}")
        if len(rows) != 2:
            bad.append(f"两条该有（妈妈/爸爸生日），实际 {len(rows)} 条：{titles}")
        for t in titles:
            if t.startswith(("我", "俺", "咱")):
                bad.append(f"标题还带着「我」，念出来不像人话：{t}")
        due = time.mktime((2026, 10, 5, 10, 0, 0, 0, -1, -1))
        for it in rows:
            if "妈" in it["title"]:
                store.update_item(u, it["id"], {"due_at": due})
        said = [E._template(c) for c in E.collect(store, None, u, now=due)
                if c["kind"] == "birthday"]
        if len(said) != 1:
            bad.append(f"到那天早上要说 {len(said)} 遍生日快乐：{said}")
        elif said[0].startswith(("我", "俺", "咱")) or "我的" in said[0]:
            bad.append(f"生日那句读起来不像人话：{said[0]}")
        return "; ".join(bad) or True
    finally:
        try:
            store._conn().close()
        except Exception:
            pass
        shutil.rmtree(d, ignore_errors=True)


def profile_no_empty_shell():
    """档案卡不许收"只有称谓、别的都空"的条目。

    第十三轮沙箱实测存成过：
    family = [{...团子...}, {"relation": "母亲", "name": "", "description": ""}]
    界面上那就是一行「母亲」后面什么都没有；`identity.nickname = ""` 这种空标量也照存过。
    """
    import shutil
    import tempfile

    from user_profile import ProfileManager, ProfileUpdater, UserProfile, merge_profile_value

    bad = []
    fam = [{"relation": "宠物", "name": "团子", "description": "五岁橘猫"}]
    if merge_profile_value(fam, [{"relation": "母亲", "name": "", "description": ""}]) != fam:
        bad.append("空壳人物还是收进来了")
    if merge_profile_value("小明", "") != "小明":
        bad.append("空串把已有的昵称抹掉了")
    if merge_profile_value(["编程"], ["旅行", ""]) != ["编程", "旅行"]:
        bad.append("列表里混进空串")

    d = tempfile.mkdtemp(prefix="moz-selftest-shell-")
    pm = None
    try:
        pm = ProfileManager(os.path.join(d, "p.db"))
        prof = UserProfile(user_id="probe")
        ProfileUpdater(pm)._apply_extractions(prof, {
            "identity.nickname": "",
            "relationships.family": fam + [{"relation": "母亲", "name": "", "description": ""}],
        })
        if prof.identity.get("nickname") == "":
            bad.append("存了个空昵称")
        if len(prof.relationships.get("family") or []) != 1:
            bad.append(f"家人列表里 {len(prof.relationships.get('family') or [])} 条（该只有团子那条）")
        return "; ".join(bad) or True
    finally:
        if pm is not None:
            try:
                pm.close()
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)


def followup_not_a_prompt():
    """追话头那句不许把给模型的指令念给用户听。

    第十二轮复现（临时库）：collect() 以前直接拿 get_followup_text() 当话题标题，
    模型措辞一挂就走兜底模板，用户会收到
    「想起你之前说的你可以自然地关心一下这些事的进展：面试结果要等一周(3天前提到)，后来有下文了吗？」
    """
    import shutil
    import tempfile

    import care_engine as E
    from care_store import CareStore
    from working_memory import WorkingMemoryStore

    d = tempfile.mkdtemp(prefix="moz-followup-")
    try:
        store = CareStore(db_path=os.path.join(d, "c.db"))
        wm = WorkingMemoryStore(os.path.join(d, "w.db"))
        u = "loop"
        old = time.time() - 3 * 86400
        wm.save(u, "最近在换工作",
                [{"topic": "面试结果要等一周", "status": "waiting", "due_at": 0,
                  "created_at": old, "id": "L1"}], "neutral")
        store.save_settings(u, {"talk_mode": "chatty"})

        topic, days = wm.next_followup(u)
        bad = []
        if topic != "面试结果要等一周" or days != 3:
            bad.append(f"next_followup 给的是 {topic!r}/{days} 天")
        cand = next((c for c in E.collect(store, wm, u, now=time.time())
                     if c["kind"] == "open_loop"), None)
        if not cand:
            bad.append("collect 没出 open_loop 候选（拿不到话头还是名额卡住了）")
        else:
            text = E._template(cand)
            for leak in ("你可以", "进展", "提到)"):
                if leak in cand["title"]:
                    bad.append(f"话题标题里混进了指令：{cand['title']}")
                    break
            if "你可以" in text or "进展" in text:
                bad.append(f"会说出口的那句里还有指令：{text}")
            if "面试结果要等一周" not in text:
                bad.append(f"那句没提真正的话题：{text}")
            if "3天前" not in cand["why"]:
                bad.append(f"依据里没写放了几天：{cand['why']}")
        # 刚说不到一天的事不该就追问
        fresh = time.time() - 3600
        wm.save(u, "刚提的", [{"topic": "新话头", "status": "waiting", "due_at": 0,
                              "created_at": fresh, "id": "L2"}], "neutral")
        if wm.next_followup(u)[0] == "新话头":
            bad.append("刚说一小时就被列为可追问")
        return "; ".join(bad) or True
    finally:
        for closer in (getattr(store, "_conn", None), getattr(wm, "_get_conn", None)):
            try:
                closer().close()
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)


def first_token_path_clear():
    """首字路径上不许有上游往返（docs/情感预热系统设计.md 的唯一硬指标）。

    真中转今天既可能 4 秒也可能 125 秒不吐字，靠它量延迟会得出随机结论；
    这里全部打桩，钉的是**结构**：普通句子根本不调情感模型、也不调"记忆摘要"，
    敏感句子才调一次，而且超时就先开口。
    """
    import asyncio
    import shutil
    import tempfile
    from types import SimpleNamespace

    import emotion_graph as EG
    import memory_manager as MM

    bad = []
    calls = {"emotion_node": 0, "invoke": 0, "stream": 0}
    fake_node_original = EG.emotion_analysis_node
    real_get_client = EG.get_chat_client

    class FakeResponse:
        def __init__(self, text):
            self.content = text

    class FakeClient:
        def __init__(self, sleep):
            self._sleep = sleep

        def invoke(self, messages):
            calls["invoke"] += 1
            time.sleep(self._sleep)
            return FakeResponse('{"memory_context": "- x", "memory_summary": "y"}')

        def stream(self, messages):
            calls["stream"] += 1
            # 生产里 langchain 吐的是带 .content 的 chunk，打桩必须照这个形状来
            return iter([SimpleNamespace(content=t) for t in ("嗯", "，", "我在")])

    def fake_node(state):
        calls["emotion_node"] += 1
        time.sleep(0.05)
        return {
            "emotion_analysis": {"current_emotion": "anxious", "emotion_intensity": 0.8,
                                 "emotion_change": "首次", "emotion_summary": "模型给的档"},
            "emotion_summary": "模型给的档", "workflow_log": []}

    d = tempfile.mkdtemp()
    try:
        mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "q.db"))
        mm.embedding_service.get_embedding = lambda text: None
        mm.embedding_service.get_embeddings_batch = lambda texts: [None] * len(texts)
        for text in ["用户习惯周六早上去滨江那家馆子", "用户的妈妈喜欢养花",
                     "用户的项目在总部三楼评审", "用户讨厌吃香菜"]:
            mm.add_memory("path-user", text, category=MM.MemoryCategory.FACT)

        EG.emotion_analysis_node = fake_node
        EG.get_chat_client = lambda **kw: FakeClient(0.0)
        # 打桩只为"快检不许碰大模型"这条铁律；改写到底还站不站在首字路径上，
        # 由下面单独那项「查询改写不挡首字」钉（它靠让 rewrite_query 一调用就炸来抓）
        real_rewrite = EG.rewrite_query
        EG.rewrite_query = lambda q: [q]

        async def collect(message, wait_cap, history=None):
            EG.SENSITIVE_WAIT_SECONDS = wait_cap
            events, t0 = [], time.time()
            gen = EG.run_emotion_workflow_streaming(
                memory_manager=mm, user_id="path-user", user_message=message,
                conversation_history=history or [], conversation_id="c1")
            async for ev in gen:
                events.append((round(time.time() - t0, 2), ev.get("type"), ev.get("text", "")[:12]))
            return events

        # ① 普通句子：一次情感模型调用都不该有，也不该有"记忆摘要"那次 invoke
        calls.update({"emotion_node": 0, "invoke": 0, "stream": 0})
        ev1 = asyncio.run(collect("我今天加班到十点才回家，好累", 1.0))
        if calls["emotion_node"]:
            bad.append(f"普通句子仍然调了情感模型 {calls['emotion_node']} 次")
        if calls["invoke"]:
            bad.append(f"首字之前还有 {calls['invoke']} 次同步模型往返（记忆摘要没摘干净）")
        first_token_at = next((t for t, k, _ in ev1 if k == "token"), None)
        if first_token_at is None:
            bad.append("一个 token 都没收到")
        elif first_token_at > 3.0:
            bad.append(f"普通句子首字要 {first_token_at}s，路径上还有别的东西在等")
        if not any(k == "reply" for _t, k, _x in ev1):
            bad.append("普通句子没走完 reply 事件")

        # ② 敏感句子：允许等一次（用户拍板），但必须有上限——打桩成绝不回话
        calls.update({"emotion_node": 0, "invoke": 0, "stream": 0})
        EG.emotion_analysis_node = lambda state: (time.sleep(3.0), fake_node(state))[1]
        ev2 = asyncio.run(collect("我妈下周要去医院做复查，我有点担心", 0.6))
        waited = next((t for t, k, _ in ev2 if k == "token"), None)
        if not calls["emotion_node"]:
            bad.append("敏感句子没有等模型（用户要的是这类等一次）")
        if waited is None:
            bad.append("敏感句子超时后没开口，等于把等变成卡死")
        elif waited > 3.0:
            bad.append(f"敏感句子等了 {waited}s 才开口，45 秒那种上限没起作用")

        # ③ 规则档要能读：内部键名不许漏进给她看的那句话
        from emotion_state import live_signal
        sig = live_signal("中午和同事吵了一架，有点堵")
        if not sig["sensitive"] or "conflict" in sig["emotion_summary"]:
            bad.append(f"敏感判档或措辞不对：{sig}")
        if "stressed" in live_signal("今天加班到十点，好累")["emotion_summary"]:
            bad.append("英文标签漏进了中文措辞里")

        # ④ 61 轮长历史：开口之前一次同步往返都不许有。
        #    先说清这一支今天量到什么：`_sanitize_history` 把历史截到 HISTORY_MAX_MESSAGES 条，
        #    比摘要触发线（SUMMARY_TRIGGER_ROUNDS 轮 = 60 条）还短，所以 /api/chat 上那段
        #    摘要分支**根本跑不到**——真正在挡首字的是查询改写，不是它。这一条钉的是
        #    "哪天放宽历史上限，摘要不许又变成开口前的同步等待"。
        calls.update({"emotion_node": 0, "invoke": 0, "stream": 0})
        requested: list = []
        real_request = EG._request_summary_async
        EG._request_summary_async = lambda uid, early, rounds: requested.append(rounds) or True
        EG._summary_cache.pop("path-user", None)
        hist61 = []
        for i in range(61):
            hist61.append({"role": "user", "content": f"第{i}句聊工作{i}和家里{i}"})
            hist61.append({"role": "assistant", "content": f"收到{i}"})
        reachable = len(EG._sanitize_history(hist61)) > EG.MAX_RECENT_ROUNDS * 2
        try:
            ev4 = asyncio.run(collect("我周末想回去看看她", 1.0, history=hist61))
        finally:
            EG._request_summary_async = real_request
        if calls["invoke"]:
            bad.append(f"61 轮长历史开口前还有 {calls['invoke']} 次同步往返（会话摘要又变回同步了）")
        if reachable and not requested:
            bad.append("长历史已经能触发摘要了，却没排进后台——既不等也不补，这段背景悄悄没了")
        if next((t for t, k, _ in ev4 if k == "token"), None) is None:
            bad.append("61 轮长历史一个 token 都没收到")
    finally:
        EG.emotion_analysis_node = fake_node_original
        EG.get_chat_client = real_get_client
        EG.rewrite_query = real_rewrite
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(bad) or True


def session_summary_off_the_clock():
    """长对话的"之前聊了什么"必须后台补：本轮不挡首字、不排第二遍、下一句才吃到。

    这里量的是**时间**而不是调用次数——摘要原来那次同步 `llm.invoke` 没有任何上限
    （2026-09-28 实测长历史那轮 121.3 秒没回，最可能就是它和生成串在了一起）。
    全程假客户端 + 假摘要服务：快检不许碰中转，也不许写 backend/moz.db。
    """
    from types import SimpleNamespace

    import emotion_graph as EG

    bad = []
    RELAY_SLEEP = 0.4
    user = "summary-async-user"
    real_client, real_service = EG.get_chat_client, EG.get_summary_service
    saved_cache, saved_lock = dict(EG._summary_cache), EG._summary_lock
    calls = {"invoke": 0}
    stored = []
    try:
        class SlowClient:
            def invoke(self, messages):
                calls["invoke"] += 1
                time.sleep(RELAY_SLEEP)
                return SimpleNamespace(content="那几周她提过答辩和妈妈的睡眠")

        class FakeService:
            def save_summary(self, uid, text, kind, key):
                stored.append(text)

            def cascade_async(self, uid):
                pass

            def format_for_prompt(self, uid):
                return ""

        EG.get_chat_client = lambda **kw: SlowClient()
        EG.get_summary_service = lambda: FakeService()

        hist = []
        # 45 轮才够着那支摘要分支：`recent_rounds = min(轮数, MAX_RECENT_ROUNDS)`，
        # 历史必须比 40 轮长才剩得出"更早的部分"。（/api/chat 上 `_sanitize_history`
        # 先把历史截到 30 条，所以这一支今天是直接打 builder 才量得到。）
        for i in range(45):
            hist.append({"role": "user", "content": f"第{i}句聊工作{i}和家里{i}"})
            hist.append({"role": "assistant", "content": f"收到{i}"})
        state = {"user_id": user, "user_message": "我周末想回去看看她",
                 "conversation_history": hist, "memory_context": "",
                 "working_memory_text": "", "emotion_summary": ""}

        # ① 本轮：拼装必须立刻返回（那段摘要还没生成出来，本轮就不该为它等）
        EG._summary_cache.pop(user, None)
        t0 = time.time()
        first_msgs = EG._build_dialogue_messages(dict(state))
        cost = time.time() - t0
        if cost > RELAY_SLEEP / 2:
            bad.append(f"45 轮长历史拼装花了 {cost:.2f}s，摘要还在开口之前同步等模型")
        if any("那几周" in str(getattr(m, "content", "")) for m in first_msgs):
            bad.append("本轮摘要还没生成出来，prompt 里却已经写上了（这是在编）")

        # ② 单飞：后台还在跑时再排一次必须被拒（同 SummaryService.maybe_cascade 的写法）
        again = EG._request_summary_async(user, hist[-4:], 45)
        if again:
            bad.append("后台摘要没有单飞锁，能排第二遍——等于拿同一条中转去排队")

        deadline = time.time() + 5.0
        while not stored and time.time() < deadline:
            time.sleep(0.05)
        if not calls["invoke"]:
            bad.append("摘要一次都没生成：既不等也没投后台，这段背景等于悄悄没了")
        if not stored:
            bad.append("5 秒了后台还没把摘要补上（补不上就等于这段背景永远没有）")
        if calls["invoke"] > 1:
            bad.append(f"摘要跑了 {calls['invoke']} 次，单飞没起作用")

        # ③ 下一句：缓存里的摘要必须真的进到 prompt 里，且不再调模型
        calls["invoke"] = 0
        t1 = time.time()
        second_msgs = EG._build_dialogue_messages(dict(state))
        if time.time() - t1 > 0.1:
            bad.append("第二轮拿到缓存摘要还花了 >0.1s，缓存没生效")
        if calls["invoke"]:
            bad.append("缓存命中了还去调模型")
        if not any("那几周" in str(getattr(m, "content", "")) for m in second_msgs):
            bad.append("后台补好的摘要没能进入下一句的 prompt")

        # ④ 反向对照：把摘要改回"同步等"，①那条计时必须当场抓得住——不然整项是空过的
        EG._summary_cache.pop(user, None)
        real_cached = EG._get_cached_summary
        EG._get_cached_summary = lambda uid, rounds: None
        real_async = EG._request_summary_async
        EG._request_summary_async = lambda uid, early, rounds: (
            EG._set_cached_summary(uid, EG._generate_summary(SlowClient(), early), rounds) or True)
        try:
            t2 = time.time()
            EG._build_dialogue_messages(dict(state))
            sync_cost = time.time() - t2
        finally:
            EG._get_cached_summary = real_cached
            EG._request_summary_async = real_async
        if sync_cost < RELAY_SLEEP / 2:
            bad.append(f"反向对照失效：摘要改回同步只花 {sync_cost:.2f}s，这条门禁量不出退化")
    finally:
        EG.get_chat_client, EG.get_summary_service = real_client, real_service
        EG._summary_cache.clear()
        EG._summary_cache.update(saved_cache)
        EG._summary_lock = saved_lock
    return "; ".join(bad) or True


def rewrite_off_first_token_path():
    """查询改写不许站在首字路径上：上限 1.2 秒，而中转实测中位 25 秒——每轮必然超时白等。

    钉法是让 `rewrite_query` 一被调用就炸。改造前那 1.2 秒是**每轮固定要付**的，
    而且 `future.cancel()` 取消不了已经在跑的 HTTP，后台还多占一份额度。
    """
    import shutil
    import tempfile

    import emotion_graph as EG
    import memory_manager as MM

    bad = []
    d = tempfile.mkdtemp()
    real_rewrite = EG.rewrite_query
    try:
        def explode(q):
            raise AssertionError("首字路径上又去调查询改写了")

        EG.rewrite_query = explode
        mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "r.db"))
        mm.embedding_service.get_embedding = lambda t: None
        mm.add_memory("rewrite-user", "用户习惯周六早上去滨江那家馆子",
                      category=MM.MemoryCategory.FACT)
        seen = {}
        real_search = mm.search_memories

        def spy(user_id, text, **kw):
            seen.update(kw)
            return real_search(user_id, text, **kw)

        mm.search_memories = spy
        state = {"user_id": "rewrite-user", "user_message": "周六想去吃那家馆子",
                 "conversation_id": "c1"}
        t0 = time.time()
        out = EG._run_memory_retrieval(state, mm, None)
        cost = time.time() - t0
        if cost > 0.5:
            bad.append(f"检索这一段花了 {cost:.2f}s，首字路径上还有东西在等")
        if not out.get("retrieved_memories"):
            bad.append("检索没拿到东西，这条快检根本没跑到路径")
        if seen.get("queries") not in (None, [state["user_message"]]):
            bad.append(f"检索收到的 queries 不是原句：{seen.get('queries')}")

        # 反向对照：把改写塞回首字路径，上面的桩必须炸得起来
        real_retrieval = EG._run_memory_retrieval

        def with_rewrite(st, m, w=None):
            EG.rewrite_query(st["user_message"])
            return real_retrieval(st, m, w)

        try:
            with_rewrite(state, mm, None)
            bad.append("反向对照失效：改写站回首字路径也抓不出来，这项是空过的")
        except AssertionError:
            pass
    finally:
        EG.rewrite_query = real_rewrite
        close_db_conn(mm)
        shutil.rmtree(d, ignore_errors=True)
    return "; ".join(bad) or True


def http_pool_shared():
    """每轮新建 ChatOpenAI 就等于每轮重做一遍 TCP+TLS（实测到中转中位 222ms）。

    钉三件事：① 同一进程里建出来的客户端共用**同一个** httpx 连接池；
    ② 换模型/换渠道/换 key 也不会各建一个（密钥不在 httpx 上，是每次请求的 header，
       所以复用不会把旧 key 带进新渠道）；③ 全进程真的只**构造**了一次池子——
       数"构造了几次"不能看交出去几次，所以直接数 `httpx.Client(...)` 这个表达式跑了几遍。
    """
    import httpx
    import langchain_openai

    import llm_config as LC

    bad = []
    real_cls = langchain_openai.ChatOpenAI
    real_shared, real_fn = LC._shared_http, LC._shared_http_client
    real_httpx_client = httpx.Client
    captured = []
    made = []

    try:
        class FakeChatOpenAI:
            def __init__(self, **kw):
                captured.append(kw)

        class CountingClient(httpx.Client):
            """数"真的构造了几次池子"。只换 httpx.Client 这个类，
            `_shared_http_client` 一个字不动——上一版把函数本身换成替身，
            量的就成了我自己的替身，是假通过。"""

            def __init__(self, *a, **kw):
                made.append(self)
                super().__init__(*a, **kw)

        langchain_openai.ChatOpenAI = FakeChatOpenAI
        httpx.Client = CountingClient
        LC._shared_http = None          # 当它是刚启动的进程
        LC.get_llm_client(temperature=0.8, top_p=0.9, use_thinking=False)
        LC.get_llm_client(temperature=0.3, use_thinking=False)
        LC.get_llm_client(model="some-other-model", base_url="https://other.invalid/v1",
                          api_key="another-key")

        if len(captured) != 3:
            return f"只建出 {len(captured)} 个客户端，这项没跑到路径"
        if any("http_client" not in kw for kw in captured):
            bad.append("有客户端没带 http_client，等于还在各建各的连接池")
        first = captured[0]["http_client"]
        if any(kw["http_client"] is not first for kw in captured):
            bad.append("三次建客户端拿到的是不同的连接池（换模型/换 key 时也不该重开一个）")
        if len(made) != 1:
            bad.append(f"连接池被构造了 {len(made)} 次，应该全进程只构造一次")

        # 反向对照：把"共用"改成"每次都新建"，上面两条断言必须立刻红
        captured.clear()
        made.clear()
        LC._shared_http = None
        LC._shared_http_client = lambda: httpx.Client(timeout=None)
        LC.get_llm_client(temperature=0.8, top_p=0.9)
        LC.get_llm_client(temperature=0.3, use_thinking=False)
        if captured[0]["http_client"] is captured[1]["http_client"] or len(made) < 2:
            bad.append("反向对照失效：改成每次新建客户端，这条门禁却什么都看不见")
    finally:
        langchain_openai.ChatOpenAI = real_cls
        httpx.Client = real_httpx_client
        LC._shared_http_client = real_fn
        for client in made + [LC._shared_http]:
            if client is not None and client is not real_shared:
                try:
                    client.close()
                except Exception:
                    pass
        LC._shared_http = real_shared
    return "; ".join(bad) or True


def thinking_param_reaches_wire():
    """"关思考"必须真的变成线上的参数——不然对照就是拿同一个请求比自己。

    第八轮/第十九轮那两条"关思考没用"的结论，是 `use_thinking=False` 和 `=True`
    发出**同一份 payload** 时量出来的（`MODEL_PROFILES` 里查不到 MiMo，extra_body 直接 None）。
    现在钉三件事：① 当前模型声明了关的写法，两份 payload 就必须不一样；
    ② 发没发出去要能在指标里看到（`thinking_param_sent/absent`）；
    ③ 反向对照：把 profile 摘掉（=改造前），两份就必须又变回一样——
       证明这条门禁盯的就是"profile 里到底声明没声明"，不是盯一个恒真的字符串比较。
    """
    import langchain_openai

    import emotion_state as ES
    import llm_config as LC
    import model_config as MC

    bad = []
    real_cls = langchain_openai.ChatOpenAI
    real_cfg, real_key = LC.load_active_config, LC.get_chat_api_key
    real_profiles = dict(MC.MODEL_PROFILES)
    captured = []
    try:
        class FakeChatOpenAI:
            def __init__(self, **kw):
                captured.append(kw)

        langchain_openai.ChatOpenAI = FakeChatOpenAI
        # llm_config 是 `from model_config import load_active_config`，所以要打在 LC 上，
        # 打在 MC 上它根本看不见（第一版就踩了这个）
        LC.load_active_config = lambda: {"model": "MiMo-V2.6-Flash",
                                         "base_url": "https://relay.invalid/v1",
                                         "api_key": "k", "use_thinking": True,
                                         "multimodal": None}
        LC.get_chat_api_key = lambda: "k"

        before = dict(ES.thinking_stats())
        captured.clear()
        LC.get_llm_client(temperature=0.8, top_p=0.9, use_thinking=False)
        off = dict(captured[-1])
        captured.clear()
        LC.get_llm_client(temperature=0.8, top_p=0.9, use_thinking=True)
        on = dict(captured[-1])

        if off.get("extra_body") != {"reasoning_effort": "none"}:
            bad.append(f'当前模型"关思考"发出去的是 {off.get("extra_body")}，'
                       '应该是 {"reasoning_effort": "none"}（中转实测只认这个）')
        if off == on:
            bad.append("开/关两个 use_thinking 发的是同一个 payload：对照又白做了")
        if "temperature" in on and on.get("temperature") == 1.0 and off.get("temperature") != 0.8:
            bad.append("关思考那次被 glm/qwen 的温度锁 1.0 连带改了")
        sent = ES.thinking_stats()["thinking_param_sent"] - before.get("thinking_param_sent", 0)
        if sent < 1:
            bad.append("发没发参数没有计数，静默失效还是看不见")

        # 用户 2026-09-29 看过两臂并排原话后定的：回话默认关掉「先想后说」。
        # 钉住这个决定，别让哪次顺手改回去——改回去是看得见的（首字慢十几秒），但没人会想起是这行。
        import emotion_graph as EG
        if EG.CHAT_USE_THINKING:
            bad.append("回话默认又变回「先想后说」了（用户定的是默认关掉；要开请设 MOZ_CHAT_THINKING=1）")
        if "use_thinking=CHAT_USE_THINKING" not in (ROOT / "backend" / "emotion_graph.py").read_text(
                encoding="utf-8"):
            bad.append("回话那一次没把 CHAT_USE_THINKING 传给客户端，那这个开关是假的")

        # ③ 反向对照
        MC.MODEL_PROFILES.pop("mimo", None)
        captured.clear()
        LC.get_llm_client(temperature=0.8, top_p=0.9, use_thinking=False)
        stripped_off = dict(captured[-1])
        if stripped_off != on:
            bad.append("反向对照失效：摘掉 profile 后「关」仍然和「开」不一样，这条门禁没盯着 profile")
    finally:
        langchain_openai.ChatOpenAI = real_cls
        LC.load_active_config, LC.get_chat_api_key = real_cfg, real_key
        MC.MODEL_PROFILES.clear()
        MC.MODEL_PROFILES.update(real_profiles)
    return "; ".join(bad) or True


def stage_timings_add_up():
    """指标里那个"首字"必须是用户看到的数：分段加起来要和自建秒表对得上，段段嵌套。

    改造前唯一在量的数是 `stream_started → 首个 token`（只算中转那一段），
    所以会出现"P50 1.5 秒"和用户等 25 秒同时成立的场面。这里两头都钉：
    ① 数值：local_prep + relay_ttfb ≈ 自己掐表到首字；
    ② 接线：first_token 必须由 server 在收到请求那一刻起算，且不许再指回 relay_ttfb。
    """
    import asyncio
    from types import SimpleNamespace

    import emotion_graph as EG
    import emotion_state as ES

    bad = []
    RELAY_SLEEP, PREP_SLEEP = 0.35, 0.2
    real_client = EG.get_chat_client

    class SlowStreamClient:
        def stream(self, messages):
            def _gen():
                time.sleep(RELAY_SLEEP)
                for t in ("嗯", "，", "我在"):
                    yield SimpleNamespace(content=t)
            return _gen()

        def invoke(self, messages):
            return SimpleNamespace(content="{}")

    class SlowStore:
        """本地读被我们故意拖慢 0.2 秒：起点要是挪错了，这 0.2 秒就会从账上消失。"""

        def prompt_line(self, user_id, stamp=None):
            time.sleep(PREP_SLEEP)
            return ""

        def get(self, *a, **kw):
            return None

    before = {name: len(ES._stage_samples.get(name, ())) for name in ES.STAGES}

    def run_once():
        async def drain():
            gen = EG.run_emotion_workflow_streaming(
                memory_manager=None, user_id="stage-user",
                user_message="今天加班到十点，好累", conversation_history=[],
                emotion_store=SlowStore())
            try:
                async for ev in gen:
                    if ev.get("type") == "token":
                        return ev
            finally:
                await gen.aclose()

        EG.get_chat_client = lambda **kw: SlowStreamClient()
        t0 = time.time()
        asyncio.run(drain())
        return time.time() - t0

    try:
        total = run_once()

        def newly(name):
            seq = list(ES._stage_samples.get(name, ()))
            return seq[before[name]] if len(seq) > before[name] else None

        prep, relay, build = newly("local_prep"), newly("relay_ttfb"), newly("prompt_build")
        if prep is None or relay is None:
            bad.append(f"分段没记全：local_prep={prep} relay_ttfb={relay}")
            return "; ".join(bad)
        if not (relay - 0.1 <= RELAY_SLEEP <= relay + 0.3):
            bad.append(f"relay_ttfb 记的是 {relay}s，和中转那 {RELAY_SLEEP}s 对不上")
        if not (prep - 0.05 <= PREP_SLEEP <= prep + 0.3):
            bad.append(f"local_prep 记的是 {prep}s，本地那 {PREP_SLEEP}s 没算进去（起点挪错了）")
        if abs(total - (prep + relay)) > 0.2:
            bad.append(f"分段加起来 {prep + relay:.2f}s ≠ 自己掐表的 {total:.2f}s，中间漏了一段埋点")
        if total - relay < PREP_SLEEP / 2:
            bad.append("首字总数里量不出开口之前的本地开销，等于还在只报中转那一段")
        if build is not None and build > prep:
            bad.append(f"段与段不嵌套：prompt_build {build}s > local_prep {prep}s")
        if newly("first_token") is not None:
            bad.append("流式路径自己声称了端到端首字（那一段该由 server 从收到请求起算）")

        # 接线检查（python 侧扫源码：vitest 那套读不到后端）。
        # 扫的是 /api/metrics 那个 dict 的**那一节**，不是整个文件——别处出现
        # stage_stats("relay_ttfb") 是正当的（它就是被单独认领出来的旧口径）。
        import ast as _ast
        src_bytes = (ROOT / "backend" / "server.py").read_bytes()
        src = src_bytes.decode("utf-8")
        metrics_src = ""
        for node in _ast.walk(_ast.parse(src_bytes)):
            if isinstance(node, _ast.AsyncFunctionDef) and node.name == "get_metrics":
                metrics_src = "\n".join(src.splitlines()[node.lineno - 1: node.end_lineno])
        if not metrics_src:
            bad.append("找不到 /api/metrics 的处理函数，接线检查等于没跑")
        if 'record_stage("first_token", time.time() - request_started)' not in src:
            bad.append('server.py 没有从"收到请求"那一刻起算 first_token')
        if '"first_token": emotion_state.stage_stats("first_token")' not in metrics_src:
            bad.append("/api/metrics 的 first_token 没有指向端到端那一段")
        if '"first_token_relay": emotion_state.stage_stats("relay_ttfb")' not in metrics_src:
            bad.append("/api/metrics 里旧口径（只量中转那一段）没有单独认领一个名字")
        graph_src = (ROOT / "backend" / "emotion_graph.py").read_text(encoding="utf-8")
        if "record_ttft" in graph_src:
            bad.append("emotion_graph 还在用旧的 record_ttft 冒充首字")
    finally:
        EG.get_chat_client = real_client
    return "; ".join(bad) or True


def retrieval_pruning_parity():
    """检索剪枝只许跳过"必然 0 分"的记忆——结果序列必须和全扫一模一样。

    剪枝省掉的是每条记忆一遍的 24 次子串查找（2 万条时占单查询的三分之一）。
    这里用同一批查询跑两遍：一遍允许剪枝、一遍强制全扫，逐条比对 (id, 分数)。
    语料刻意掺了不好处理的形状：单字成词、跨标点才相邻、纯英文、极短正文、
    moz 自己说过的话、提问形状的记忆。
    """
    import random
    import shutil
    import tempfile

    import memory_manager as MM

    bad = []
    d = tempfile.mkdtemp()
    real_prep = MM.MemoryManager.__dict__["_prepare_query"]
    try:
        mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "p.db"))
        mm.embedding_service.get_embedding = lambda t: None
        mm.embedding_service.get_embeddings_batch = lambda ts: [None] * len(ts)
        rng = random.Random(11)
        corpus = []
        topics = ["体检", "答辩", "驾照", "香菜", "团子", "老郑", "青柠计划", "滨江", "复查", "搬家"]
        shapes = [
            "用户{t}那件事定在{n}号",
            "用户提过{t}，细节是{t}{n}",           # 与查询共享 2 字连写
            "用 户 {t} 的 安 排",                   # 单字成词：必须关掉剪枝
            "用户说{t}……到底要不要去呢？",           # 提问形状
            "user mentioned {t}3{t}2",              # 纯英文词元
            "{t}",                                   # 极短正文（清洗后不足 2 字）
            "续约，到时候再说{n}",                    # 跨标点才相邻
            "用户后来在{n}号完成了{t}的事情",
        ]
        for i in range(320):
            t = rng.choice(topics)
            corpus.append(rng.choice(shapes).format(t=t, n=rng.randint(1, 30)) + f"（第{i}条）")
        for text in corpus:
            mm.add_memory("prune-user", text, category=MM.MemoryCategory.FACT)
        if len([m for m in mm._get_user_memories("prune-user").values() if m.active()]) < 200:
            return "语料没种够（被去重或降级了），这项没跑到路径"

        queries = []
        for _ in range(90):
            base = rng.choice(corpus)
            if rng.random() < 0.45:
                base = rng.choice(topics) + rng.choice(["什么时候", "怎么办", "谁负责", "32"])
            queries.append("".join(rng.sample(base, min(len(base), rng.randint(2, 8)))))
        queries += ["团子", "续 约", "用 户 答 辩 的 安 排", "user mentioned 体检3体检2",
                    "青柠计划的负责人是老郑", "滨江车管所"]

        def raw_scores(force_full: bool):
            def prep(q):
                fn = real_prep.__func__ if hasattr(real_prep, "__func__") else real_prep
                out = dict(fn(q))
                if force_full:
                    out["prunable"] = False
                return out
            MM.MemoryManager._prepare_query = staticmethod(prep)
            mem = dict(mm._get_user_memories("prune-user"))
            out = []
            for q in queries:
                pairs = mm._keyword_search_raw(mem, q, None, 0.0, user_id="prune-user")
                out.append([(m.id, round(s, 12)) for s, m in pairs])
            return out

        full = raw_scores(force_full=True)
        pruned = raw_scores(force_full=False)
        # 至少要有查询真的走了剪枝，否则这项是空过的
        sample = mm._prepare_query("青柠计划的负责人是老郑")
        if not sample.get("prunable"):
            bad.append("连正常中文查询都没被判成可剪枝，这条门禁没测到东西")

        diffs = [(q, a, b) for q, a, b in zip(queries, full, pruned) if a != b]
        if diffs:
            q, a, b = diffs[0]
            bad.append(f"剪枝改变了结果（{len(diffs)}/{len(queries)} 条查询）："
                       f"「{q}」全扫 {len(a)} 条 / 剪枝 {len(b)} 条，"
                       f"前三个 {a[:3]} vs {b[:3]}")

        # 反向对照：故意把剪枝条件写坏（漏掉"只共享一个字"的那一路），必须被上面抓到
        def broken_prep(q):
            fn = real_prep.__func__ if hasattr(real_prep, "__func__") else real_prep
            out = dict(fn(q))
            if out.get("prunable"):
                out["g2"] = frozenset()          # 假装没有任何 2 字连写命中
            return out
        MM.MemoryManager._prepare_query = staticmethod(broken_prep)
        mem = dict(mm._get_user_memories("prune-user"))
        broken = [[(m.id, round(s, 12)) for s, m in
                   mm._keyword_search_raw(mem, q, None, 0.0, user_id="prune-user")]
                  for q in queries]
        if broken == full:
            bad.append("反向对照失效：把剪枝写坏也没让结果变化，这个对拍根本量不到东西")
    finally:
        MM.MemoryManager._prepare_query = real_prep
        close_db_conn(mm)
        shutil.rmtree(d, ignore_errors=True)
    return "; ".join(bad) or True


def backfill_off_the_clock():
    """补算向量不许挡在本轮检索前面；失败要越退越远，成功一次立刻复位。

    这台机器上向量令牌是过期的，原来每过一次冷却就有一轮对话替整库付一次注定失败的往返
    （一批最多 64 条文本打进一个请求）。全程打桩，绝不真连向量服务。
    """
    import shutil
    import tempfile

    import memory_manager as MM

    bad = []
    d = tempfile.mkdtemp()
    mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "b.db"))
    svc = mm.embedding_service
    saved = (svc._client, getattr(svc, "_model", None), svc._failure_cooldown,
             svc._failure_cooldown_max, svc._disabled_until, svc._consecutive_failures,
             svc.tried, svc.succeeded, svc.failed)

    def wait_free_backfill(cap=6.0):
        deadline = time.time() + cap
        while time.time() < deadline:
            if MM.MemoryManager._backfill_lock.acquire(blocking=False):
                MM.MemoryManager._backfill_lock.release()
                return True
            time.sleep(0.02)
        return False

    def pin_open():
        """等上一批后台补算收干净，再把冷却状态摆正。

        补算现在是后台线程，它失败会替整个进程 arm 冷却——不先排空就量到的是上一段的尾巴。
        """
        wait_free_backfill()
        svc._disabled_until, svc._consecutive_failures = 0.0, 0

    try:
        for text in ["用户的体检安排在三月十二号", "用户的妈妈喜欢养花",
                     "用户在滨江车管所换了驾照", "用户下周还要去复查"]:
            mm.add_memory("backfill-user", text, category=MM.MemoryCategory.FACT)
            # 种记忆时保留前面门禁留下的"绝不打上游"的桩：先摘桩再种就会真的发一次 401
            # （上一版就这么漏过一手，日志里那条 401 是这条快检自己打的）
            svc.get_embedding = lambda t: None
        # 前面好几项快检会给这个**单例**服务挂上 `get_embeddings_batch = lambda ...` 却不还原，
        # 于是后面的门禁量到的是别人的桩（不是真代码）。要量真路径就得先把这个桩摘掉。
        svc.__dict__.pop("get_embeddings_batch", None)
        # 冷却故意设长（30 秒、封顶 1 小时），让"冷却期内不再尝试"那段是确定的：
        # 不会因为 30 次检索跑过了几十毫秒而误报；退避那段单独看倍数。
        svc._failure_cooldown, svc._failure_cooldown_max = 30.0, 3600.0
        tries = {"query": 0, "backfill": 0}

        # 打桩打在**上游客户端**上，而不是替换 `get_embeddings_batch`：
        # 冷却判断就写在那个方法里，把方法整个换掉等于把它一起绕过了（第一版就这么自欺过）
        class FakeEmbeddings:
            def create(self, model=None, input=None, **kw):
                n = len(input) if isinstance(input, (list, tuple)) else 1
                if n > 1:
                    tries["backfill"] += 1
                    time.sleep(1.0)       # 补库那一批原来就站在用户面前；故意拉大到"绝不可能被噪声盖住"
                else:
                    tries["query"] += 1
                raise RuntimeError("401 invalid authentication（打桩，不真连）")

        class FakeClient:
            embeddings = FakeEmbeddings()

        svc._model = "fake-embed"
        svc._client = FakeClient()
        pin_open()
        t0 = time.perf_counter()
        hits = mm.search_memories("backfill-user", "体检", limit=5)
        cost = (time.perf_counter() - t0) * 1000
        if not hits:
            return "连「体检」都检索不到东西，这项没跑到路径"
        if cost > 300:
            bad.append(f"本轮检索花了 {cost:.0f}ms——补库那一批（桩里 1000ms）还挡在用户面前")
        if tries["backfill"]:
            bad.append(f"本轮替整库发了 {tries['backfill']} 批向量请求，应该一批都不发")
        if tries["query"] != 1:
            bad.append(f"查询向量打了 {tries['query']} 次，本轮应该只打一次")

        # 补库那一批本身：直接问它，必须"立刻接走、后台去跑"
        pin_open()
        missing = [m for m in mm._get_user_memories("backfill-user").values() if m.active()]
        t1 = time.perf_counter()
        started = mm._request_backfill_async("backfill-user", missing)
        handoff = (time.perf_counter() - t1) * 1000
        if not started:
            bad.append("_request_backfill_async 没接单（单飞锁没释放，或服务被判成不可用）")
        if handoff > 80:
            bad.append(f"把补库排进后台就花了 {handoff:.0f}ms，它自己就在挡路")
        if not wait_free_backfill():
            bad.append("后台补算的线程超过 6 秒还没收尾，单飞锁的释放路径有问题")
        if tries["backfill"] < 1:
            bad.append("后台那一批根本没发出去（接了单却没干活）")

        # 冷却期内再检索 30 次：一次都不许再打
        before_q, before_b = tries["query"], tries["backfill"]
        for _ in range(30):
            mm.search_memories("backfill-user", "体检", limit=5)
        if (tries["query"], tries["backfill"]) != (before_q, before_b):
            bad.append(f"冷却期内还是又打了 {tries['query'] - before_q} 次查询 /"
                       f" {tries['backfill'] - before_b} 批补库")

        # 退避要越退越长、要有封顶
        base = svc._failure_cooldown
        pin_open()
        svc._note_failure(RuntimeError("a"))
        first = svc._disabled_until - time.monotonic()
        svc._note_failure(RuntimeError("b"))
        svc._note_failure(RuntimeError("c"))
        later = svc._disabled_until - time.monotonic()
        if not (later > first and first >= base - 0.5 and later <= svc._failure_cooldown_max + 0.5):
            bad.append(f"退避不对（基准 {base:.0f}s，首次 {first:.1f}s，三次后 {later:.1f}s，"
                       f"封顶 {svc._failure_cooldown_max:.0f}s）")
        # 成功一次必须复位，否则服务真恢复了也要多等一截
        svc._note_success()
        if svc._consecutive_failures or not svc.available() or svc.succeeded < 1:
            bad.append("成功之后没复位：冷却时间/失败计数/成功计数三样都得归位")

        # 反向对照：把退避写死成"不冷却"，上面那句"冷却期内不再尝试"必须测出差别
        svc._failure_cooldown = svc._failure_cooldown_max = 0.0
        pin_open()
        before_q = tries["query"]
        for _ in range(6):
            mm.search_memories("backfill-user", "体检", limit=5)
        if tries["query"] - before_q < 3:
            bad.append("反向对照失效：冷却设成 0 也只打了一次，那些计数是假的")
    finally:
        wait_free_backfill()
        (svc._client, svc._model, svc._failure_cooldown, svc._failure_cooldown_max,
         svc._disabled_until, svc._consecutive_failures, svc.tried,
         svc.succeeded, svc.failed) = saved
        # 出去的时候给后面的门禁留"绝不打上游"的桩（快检铁律），别留真客户端出去乱跑
        svc.get_embedding = lambda t: None
        svc.get_embeddings_batch = lambda ts: [None] * len(ts)
        close_db_conn(mm)
        shutil.rmtree(d, ignore_errors=True)
    return "; ".join(bad) or True


def emotion_baseline_honest():
    """L1 情感基线：依据不够就不许总结，模型给的东西必须消毒，刷新要有节奏。

    全程用假客户端：快检门禁不许碰中转（这一条铁律是第八轮踩出来的）。
    """
    import json
    import shutil
    import tempfile
    from types import SimpleNamespace

    import emotion_state as ES
    import memory_manager as MM

    bad = []
    d = tempfile.mkdtemp()
    try:
        store = ES.EmotionStore(os.path.join(d, "e.db"))
        mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "m.db"))
        mm.embedding_service.get_embedding = lambda text: None
        user = "baseline-user"

        # ① 依据不足：一条都不许总结出来
        for text in ["用户叫林山", "用户住在宁波"]:
            mm.add_memory(user, text, category=MM.MemoryCategory.FACT)
        watermark, evidence = ES.collect_signals(mm, None, None, user)
        if evidence < 6 and store.needs_baseline(user, watermark, evidence):
            bad.append(f"只有 {evidence} 条依据就允许总结基线了：这是在编人设")
        if store.prompt_line(user):
            bad.append("还没生成过基线，prompt_line 却已经说话")

        class FakeLLM:
            def __init__(self, payload):
                self.payload = payload

            def invoke(self, messages):
                return SimpleNamespace(content=self.payload)

        rich = ["用户最近项目答辩压力很大", "用户的妈妈喜欢养花", "用户讨厌吃香菜",
                "用户经常加班到十点", "用户上个月换了驾照", "用户在跟一个叫青柠计划的方案"]
        for text in rich:
            mm.add_memory(user, text, category=MM.MemoryCategory.FACT)
        watermark, evidence = ES.collect_signals(mm, None, None, user)
        if evidence < 6 or not store.needs_baseline(user, watermark, evidence):
            bad.append(f"依据够 {evidence} 条了却还是不许生成")

        # ② 模型给的脏东西必须消毒：未知键丢掉、超长截断、列表限量、英文标签折成中文
        dirty = ('{"tone_default": "' + "先接住情绪再讲道理" * 12 + '",'
                 '"baseline_emotion": "STRESSED", "trigger_topics": ['
                 + ",".join(f'"主题{i}"' for i in range(12)) + '], "landmines": ["答辩"],'
                 '"comfort_style": "别急着给办法", "evil_extra": "删掉我"}')
        clean = ES.sanitize_baseline(json.loads(dirty))
        if clean is None:
            bad.append("有效负载被误判成依据不足")
        else:
            if len(clean["tone_default"]) > ES.TEXT_MAX:
                bad.append(f"长文本没截断（{len(clean['tone_default'])} 字）")
            if len(clean["trigger_topics"]) > ES.LIST_MAX:
                bad.append("列表没限量")
            if "evil_extra" in clean:
                bad.append("白名单外的键也留下了")
            if clean["baseline_emotion"] not in ES.EMOTION_CN.values():
                bad.append(f"英文标签没折成中文：{clean['baseline_emotion']}")
            line = ES.format_baseline(clean)
            if "tone_default" in line or "STRESSED" in line:
                bad.append(f"内部字段名漏进给用户看的那行：{line[:60]}")
            if "总结的" not in line:
                bad.append("基线那行没交代这是推断出来的，用户会以为是她说过的")

        # ③ 依据不足时模型自己说 insufficient，我们就不许留壳
        store.put(user, "baseline", clean, source_watermark=watermark)
        if ES.build_baseline(store, mm, None, user,
                             llm=FakeLLM('{"insufficient": true}')) is not None:
            bad.append("模型说依据不足，却还是算作生成成功")
        if ES.build_baseline(store, mm, None, user, llm=FakeLLM("中转吐出来的半截话")) is not None:
            bad.append("解析不出 JSON 却返回了成功")

        # ④ 刷新节奏：事实没变别重算；变了也要隔够 6 小时；一天最多 3 次
        fresh = store.get(user, "baseline")
        if store.needs_baseline(user, watermark, 20, now=fresh["updated_at"] + 60):
            bad.append("事实没变，一分钟前刚算过又要重算")
        if not store.needs_baseline(user, watermark + 500, 20,
                                    now=fresh["updated_at"] + ES.MIN_INTERVAL + 1):
            bad.append("事实变了且隔够了时间，却不许重算")
        if store.needs_baseline(user, watermark + 500, 20,
                                now=fresh["updated_at"] + 60):
            bad.append("隔不够 MOZ 最小间隔也照样重算（6 小时这道闸没生效）")
        for n in range(ES.DAILY_CAP):
            store.put(user, "plan", {"topic": f"占位{n}"}, topic=f"占位{n}")
        # 造满今天的额度：花掉 DAILY_CAP 次，看门是否拦得住
        for _ in range(ES.DAILY_CAP):
            store.spend(user)
        if store.used_today(user) < ES.DAILY_CAP:
            bad.append(f"used_today 只数出 {store.used_today(user)}，额度上限形同虚设")
        if store.needs_baseline(user, watermark + 900, 20, now=time.time() + ES.MAX_AGE):
            bad.append("今天已经算满 3 次了还允许再算")

        # ⑤ 清得掉：这是猜的，用户说不准就该能一把抹掉，且不许碰事实记忆
        before = len(mm._get_user_memories(user))
        deleted = store.clear(user, "baseline")
        if deleted < 1 or store.get(user, "baseline"):
            bad.append("清不掉基线")
        if len(mm._get_user_memories(user)) != before:
            bad.append("清基线的时候把长期记忆也带走了（红线）")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(bad) or True


def emotion_plan_double_gate():
    """L2 临时对策：主题不对不许用、过期不许用，而且**绝不进长期记忆**。

    全打桩：预热也是模型调用，快检门禁不许碰中转。
    """
    import json
    import shutil
    import tempfile
    from types import SimpleNamespace

    import emotion_state as ES
    import memory_manager as MM
    from save_queue import SaveQueue

    bad = []
    d = tempfile.mkdtemp()
    try:
        ES.reset_plan_stats()
        store = ES.EmotionStore(os.path.join(d, "e.db"))
        mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "m.db"))
        mm.embedding_service.get_embedding = lambda t: None
        user = "plan-user"
        for text in ["用户的妈妈喜欢养花", "用户的妈妈生日是10月5号",
                     "用户经常加班到十点才到家"]:
            mm.add_memory(user, text, category=MM.MemoryCategory.FACT)
        before = len(mm._get_user_memories(user))

        class FakeLLM:
            def __init__(self, payload):
                self.payload = payload

            def invoke(self, messages):
                return SimpleNamespace(content=self.payload)

        payload = json.dumps({"say": "先问阿姨那盆花现在怎么样", "avoid": ["别拿加班打趣"],
                              "tone": "轻一点", "followup": "这周还加班吗",
                              "insufficient": False}, ensure_ascii=False)
        plan = ES.build_plan(store, mm, None, user, "家人", llm=FakeLLM(payload))
        if not plan:
            bad.append("有效对策被判成不成立")
        line = ES.plan_for(store, user, ["家人"], now=time.time())
        if not line or "先问阿姨那盆花" not in line:
            bad.append(f"备好且没过期的对策没被用上：{line!r}")
        # ① 主题闸：家人备的对策不许串到工作上
        if ES.plan_for(store, user, ["工作"], now=time.time()):
            bad.append("工作那句话吃到了「家人」的对策（主题闸没关）")
        # ② 时间闸：过期就作废，而且要计数（不计数等于闸门是否在闸都不知道）
        skipped0 = ES.plan_stats()["expired_skipped"]
        if ES.plan_for(store, user, ["家人"], now=time.time() + ES.PLAN_TTL + 60):
            bad.append("过期的对策还在用")
        if ES.plan_stats()["expired_skipped"] <= skipped0:
            bad.append("过期被拦下却没计数，等于这项门禁是摆设")
        # ②' 依据的事实变了就要闭嘴：删记忆、改档案都算
        #（红线：用户删掉的东西不该还在影响措辞——TTL 30 分钟挡不住挂 7 天的基线）
        stale_before = ES.plan_stats()["stale_skipped"]
        if ES.plan_for(store, user, ["家人"], now=time.time(), watermark=999999.0):
            bad.append("事实水位变了还在用旧对策")
        if ES.plan_stats()["stale_skipped"] <= stale_before:
            bad.append("水位拦下对策却没计数，等于不知道闸在不在闸")
        thin = ES.sanitize_baseline({"tone_default": "轻一点", "baseline_emotion": "sad",
                                     "trigger_topics": [], "landmines": [],
                                     "comfort_style": "", "insufficient": False})
        if not thin or thin["baseline_emotion"] != "难过":
            bad.append(f"只有一句实在话的合法基线被误杀：{thin}")

        # ③ 依据不足 / 空对策不许留壳
        if ES.build_plan(store, mm, None, user, "宠物", llm=FakeLLM('{"insufficient": true}')):
            bad.append("模型说读不出细节，却还是存了对策")
        if ES.sanitize_plan({"say": "", "followup": ""}, "健康"):
            bad.append("空对策被消毒成可用（宁可没有）")
        # 模型爱写"别打趣她妈妈养花"，前面再挂"这会儿避开"就是双重否定（真界面抓到过）
        dirty = ES.sanitize_plan({"say": "先问睡眠", "followup": "", "tone": "轻一点",
                                  "avoid": ["别打趣她妈妈养花的事", "不要追问失眠原因",
                                            "勿提生日还早"]}, "家人")
        if not dirty or any(a.startswith(("别", "不要", "勿")) for a in dirty["avoid"]):
            bad.append(f'avoid 开头的"别"没剥掉，读起来是双重否定：{(dirty or {}).get("avoid")}')
        if "、".join(dirty["avoid"]) != "打趣她妈妈养花的事、追问失眠原因、提生日还早":
            bad.append(f"剥完的样子不对：{dirty['avoid']}")
        # ④ 红线：这一切不许碰长期记忆，也不许被检索命中
        if len(mm._get_user_memories(user)) != before:
            bad.append("预热往 memories 里写了行——推断冒充事实")
        hits = [m.content for m in mm.search_memories(user, "先问阿姨那盆花现在怎么样")]
        if any("先问阿姨" in h for h in hits):
            bad.append(f"对策能被检索当成事实召回：{hits}")
        # ⑤ 排队：同主题不排第二遍；超过每小时额度直接不排
        queue = SaveQueue(os.path.join(d, "q.db"))
        topics = ES.schedule_prewarm(store, queue, None, user, "我妈生日快到了", ["家人"])
        if topics:
            bad.append("已经有活的对策还去排队预热")
        queued = ES.schedule_prewarm(store, queue, None, user, "经常加班到十点", ["工作"])
        if queued != ["工作"] or queue.hourly_count("prewarm") != 1:
            bad.append(f"该排的没排进去：{queued}")
        ES.schedule_prewarm(store, queue, None, user, "又提工作", ["工作"])
        if queue.hourly_count("prewarm") != 1:
            bad.append("同主题排了第二遍（去重没生效）")
        many = ES.schedule_prewarm(store, queue, None, user, "加班和考试",
                                   ["工作", "考试", "钱"], cap=1)
        if many:
            bad.append(f"每小时上限 1 却还排进了 {many}")
        if ES.plan_stats()["quota_skipped"] < 1:
            bad.append("额度拦下却没计数")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    return "; ".join(bad) or True


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
        return '没设头像还回 404：那是控制台里一条假报错，该回 {"avatar": null}'
    if code != 200:
        return f"头像读取 {code}"
    if payload[:3] == b"\xff\xd8\xff":
        return True
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        return f"既不是 JPEG 也不是 JSON：{payload[:24]!r}"
    return True if body.get("avatar") is None else f"JSON 里带了头像：{body}"


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
    body = {"message": "自测探针：请用一句话回答你好", "conversation_id": cid,
            "conversation_history": [], "image_data": None}
    t0 = time.time()
    req = urllib.request.Request(f"{API}/chat/{USER}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    got_reply, err = False, None
    try:
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
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        # 第十五轮实测：另一种死法是**客户端自己**等满 300 秒（TimeoutError: timed out），
        # 连 SSE 的 error 事件都没拿到。那也是供给，别报成产品缺陷——但要带上等了多久。
        return f"warn: 等了 {time.time() - t0:.0f}s 客户端先超时（{type(e).__name__}），接口一个字都没回"
    if err:
        # 中转慢/断/5xx 折成的那几句人话是**产品按预期在说话**，不该把这项记成代码缺陷
        # （第十四轮实测：一句"回答你好"被拖到 383 秒后由我们自己的 120 秒空档守卫报超时）。
        # 但也不能一句"没事"就打消：warn 里带上等了多久，连着几轮好看趋势。
        supply = ("想得太久", "等模型回话等超时", "中转限流", "连不上模型中转",
                  "中转那边出了点问题", "连接中途断", "服务没正常响应")
        if any(mark in str(err) for mark in supply):
            return f"warn: 中转没回话（等了 {time.time() - t0:.0f}s）：{str(err)[:60]}"
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
        try:
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
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # 断流是中转的常态，不该把这项记成"产品失败"（沙箱那边第十四轮也修过同一课）
            return f"«连接断了：{type(e).__name__}»"
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
    check("安静时段判定", engine_logic)
    check("日期抽取与去噪", extractor_logic)
    check("相对日期落在对的日子", relative_dates_land_right)
    check("聊到日子自己记下", care_harvest_roundtrip)
    check("生日不许记成事件提醒", birthday_not_mislabeled)
    check("一件事换个说法不攒成两条", same_fact_one_item)
    check("档案卡不收空壳条目", profile_no_empty_shell)
    check("追话头不说指令", followup_not_a_prompt)
    check("存储与预算与ack竞态", store_logic)
    check("关心两开关独立", care_switch_logic)
    check("主动关心不轰炸", care_no_dump)
    check("数据库不变量", db_invariants)
    check("不遗忘只降到最低权重", capacity_policy_check)
    check("检索提速不改排序", keyword_parity_check)
    check("检索剪枝不丢命中", retrieval_pruning_parity)
    check("补向量不挡本轮", backfill_off_the_clock)
    check("落库队列重启不丢", save_queue_survives_restart)
    check("记忆质量底线", memory_quality_floor)
    check("首字路径没有上游往返", first_token_path_clear)
    check("后台摘要不挡首字", session_summary_off_the_clock)
    check("查询改写不挡首字", rewrite_off_first_token_path)
    check("关思考的参数真的发出去", thinking_param_reaches_wire)
    check("连接池不每轮重握手", http_pool_shared)
    check("分段计时不自证", stage_timings_add_up)
    check("情感基线不乱编", emotion_baseline_honest)
    check("临时对策双闸", emotion_plan_double_gate)
    check("系统文案不进长期记忆", system_copy_not_memory)
    check("提问的回答不单独存成事实", question_answer_not_memory)
    check("同一句话不存两条重复事实", same_turn_no_duplicate_facts)
    check("事件关联成图", care_link_graph_check)
    check("事件先后链", care_chain_link_check)
    check("探针清理器不错杀", probe_cleaner_works)
    check("天气源可用", weather_probe)
    check("头像字节流", avatar_probe)
    check("前端高度用dvh", css_uses_dvh)
    check("前端无孤儿杂物", frontend_no_junk)
    # 这项会连发 80 次打满自己的 IP 桶，必须排在最后，否则紧接着的检查会被限流打掉
    check("限流不误伤本地", rate_limit_not_hostile)
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

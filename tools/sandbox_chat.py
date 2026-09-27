"""端到端记忆回测：在一份**隔离副本**上真起一个后端，喂事实、再问回去。

为什么不能直接打 8000 端口：那是用户正在用的实例，写进去的话就是他真实长期记忆。
这里把 backend/ 代码 + .env + runtime_model_config.json 复制到临时目录，
用一个空 moz.db 起第二个后端（默认 8123 端口），跑完连目录一起删。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/sandbox_chat.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/sandbox_chat.py --chain   # 只验先后链
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

FACTS = [
    "记一下：我家猫叫团子，今年五岁，是只橘猫",
    "我妈生日是农历十月初五，她喜欢养花",
    "我下周三下午两点要去做项目答辩，在总部三楼",
    "我最近在跟一个叫青柠计划的方案，负责人是老郑",
    "我讨厌吃香菜，以后别推荐我带香菜的东西",
    "我上个月刚把驾照换证，在滨江那家车管所办的",
]
QUESTIONS = [
    "我家猫叫什么来着？几岁了？",
    "我妈生日是什么时候？她喜欢什么？",
    "我周三下午有什么安排？在哪儿？",
    "青柠计划的负责人是谁？",
    "有什么吃的我不能吃来着？",
    "我驾照是在哪儿换的？",
]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def api(port, path, method="GET", body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
        return json.loads(raw) if raw else {}


def chat(port, user, text, timeout=200):
    """走真实 /api/chat 的 SSE，取回完整回复文本。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/chat/{user}",
        data=json.dumps({"message": text, "conversation_history": []}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    out, reply = [], ""
    with urllib.request.urlopen(req, timeout=timeout) as res:
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
            elif ev.get("type") == "reply":
                reply = ev.get("text") or reply
            elif ev.get("type") == "error":
                return "«错误：" + str(ev.get("text")) + "»"
            elif ev.get("type") == "done":
                break
    return (reply or "".join(out)).strip()


CHAIN_SENTENCE = "项目答辩下周三，答辩之后一周出结果，出结果我再决定要不要续约"


def chain_probe(port: int, user: str) -> int:
    """端到端验"先后链"：真聊天 → 后台抽取 → 链进图 → dry-run 说得出人话。

    链只能在抽取时产生，所以这一条必须走真后端；用沙箱副本是因为它会写库。
    """
    print("\n== 只测先后链：说一句带两件事的话 ==")
    t0 = time.time()
    print(f"  说了：{CHAIN_SENTENCE}\n  回了：{chat(port, user, CHAIN_SENTENCE)[:120]}  ({time.time() - t0:.0f}s)")
    # 后台落库是串行的 4 次模型调用（工作记忆→长期记忆→档案卡→关心抽取），
    # 抽取常常要到对话结束后一分多钟才完成，轮询要给足时间，否则会误判成"没记下"。
    items, stats, edges = [], {}, []
    for _ in range(60):
        time.sleep(5)
        items = api(port, f"/care/items?user_id={user}&status=all").get("items", [])
        graph_dump = api(port, f"/care/graph?user_id={user}")
        stats, edges = graph_dump.get("stats", {}), [
            e for e in graph_dump.get("edges", []) if e.get("rel") == "after"]
        if edges:
            break
    print(f"  记下的事：{[(i['title'], '有日期' if i['due_at'] else '没日期') for i in items]}")
    print(f"  关联图：{stats}")
    print(f"  链边：{[(e['label'], e['offset_days'], e['source']) for e in edges]}")
    dry = api(port, f"/care/dry-run?user_id={user}", method="POST").get("would_say", [])
    print(f"  演练：{[(d.get('kind'), d.get('text'), d.get('chain')) for d in dry]}")
    if not edges:
        print("  ✗ 链没建出来：看沙箱 server.log 里 [关心抽取] 那几行")
        return 1
    print("  ✓ 链建出来了" + ("，且 dry-run 里出现了 chain 候选" if any(
        d.get("kind") == "chain" for d in dry) else "（今天还没到期，dry-run 不追问属正常）"))
    return 0


def main():
    user = "sandbox_user"
    tmp = Path(tempfile.mkdtemp(prefix="moz-sandbox-"))
    work = tmp / "backend"
    shutil.copytree(BACKEND, work, ignore=shutil.ignore_patterns(
        "__pycache__", "memory_store", "conversations", "avatars",
        "moz.db", "moz.db-wal", "moz.db-shm", "saved_models.json", "tests"))
    # 密钥要能用才测得动真链路
    for name in ("runtime_model_config.json",):
        src = BACKEND / name
        if src.exists():
            shutil.copy2(src, work / name)
    if (ROOT / ".env").exists():
        shutil.copy2(ROOT / ".env", tmp / ".env")

    port = free_port()
    print(f"沙箱后端 {work} 端口 {port}（空库，真实 moz.db 没被复制过来）")
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(work), stdout=log, stderr=log,
        # 沙箱用独立 user_id：normalize_user_id 有白名单，不在名单里一律 403
        # PYTHONUNBUFFERED：子进程往文件里写日志默认是块缓冲，terminate 时会把尾部丢掉，
        # 排查"到底跑没跑"就不能靠一个被截断的日志
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
             "MOZ_ALLOWED_USER_IDS": user},
    )
    try:
        for _ in range(60):
            try:
                if api(port, "/health", timeout=3).get("status") == "ok":
                    break
            except Exception:
                time.sleep(1)
        else:
            print("后端没起来，看沙箱里的 server.log")
            return 1

        print("\n== 第一遍：说事实 ==")
        if "--chain" in sys.argv:
            return chain_probe(port, user)
        for text in FACTS:
            t0 = time.time()
            chat(port, user, text)
            print(f"  说了：{text}   ({time.time() - t0:.0f}s)")
            time.sleep(2)   # 抽取是异步的，给落库留点时间

        print("\n== 第二遍：问回去（新开会话，只靠长期记忆）==")
        stats = api(port, f"/memory/{user}/stats")
        print(f"  库里记忆 {stats.get('total')} 条")
        score = 0
        for q in QUESTIONS:
            ans = chat(port, user, q)
            print(f"  问：{q}\n  答：{ans[:160]}")
            score += 1 if ans and "«错误" not in ans else 0
        print(f"\n答出 {score}/{len(QUESTIONS)} 条")

        print("\n== 它到底存成了什么 ==")
        detail = api(port, f"/memory/{user}/detail")
        for layer, items in (detail.get("layers") or {}).items():
            for m in items[:40]:
                print(f"  [{layer}] {m['content'][:70]}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        log.close()
        print(f"\n沙箱日志留在 {tmp}（可以直接删）")


if __name__ == "__main__":
    sys.exit(main())

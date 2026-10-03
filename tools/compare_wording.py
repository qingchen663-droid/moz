"""措辞对照：同一句话，同时问"开着先想后说"和"关掉"的两份沙箱后端，把原话并排打出来。

为什么要有这个（2026-09-29）：关掉思考能省掉看不见的 reasoning，首字从 2.1~13.4 秒掉到 0.9~2.5 秒
（`tools/probe_first_token.py` 实测），但**moz 说话像不像人话归用户判**，不归速度数字判。
`tools/sandbox_chat.py` 量的是"记不记得"，那种答案本来就短，恰好是最不需要思考的一类，
拿它当措辞证据会偏。所以这里挑会挑出语气的句子，两臂**交替**问（同一个几分钟的窗口里问，
免得把中转的时段漂移当成参数效果），每句都记首个字到达时间和整句回复。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/compare_wording.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/compare_wording.py --sentences 5

两臂各自起一份**隔离副本**（临时目录 + 空 moz.db），绝不碰 backend/moz.db，也不写真实用户库；
跑完连目录一起删。会花真中转账度：每句 2 次（默认 10 次）。
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
USER = "web_user_001"

# 挑那种"关掉思考最容易被看出来"的句子：要接情绪、要分寸、要记得回避
SENTENCES = [
    "我妈下周要去医院做复查，我有点担心她",
    "中午和同事吵了一架，现在心里还挺堵的",
    "项目答辩的结果还没出，我这会儿什么也干不进去",
]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_sandbox(thinking: bool) -> tuple:
    """复制一份后端到临时目录，用给定的 MOZ_CHAT_THINKING 起一个空库实例。"""
    tmp = Path(tempfile.mkdtemp(prefix="moz-wording-"))
    work = tmp / "backend"
    shutil.copytree(BACKEND, work, ignore=shutil.ignore_patterns(
        "__pycache__", "memory_store", "conversations", "avatars",
        "moz.db", "moz.db-wal", "moz.db-shm", "saved_models.json", "tests"))
    for name in ("runtime_model_config.json",):
        if (BACKEND / name).exists():
            shutil.copy2(BACKEND / name, work / name)
    if (ROOT / ".env").exists():
        shutil.copy2(ROOT / ".env", tmp / ".env")
    port = free_port()
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(work), stdout=log, stderr=log,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
             "MOZ_CHAT_THINKING": "1" if thinking else "0"})
    return proc, port, tmp, log


def wait_ready(port: int, cap: int = 90) -> bool:
    for _ in range(cap):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as res:
                if json.loads(res.read()).get("status") == "ok":
                    return True
        except Exception:
            time.sleep(1)
    return False


def ask(port: int, text: str, timeout: int = 300) -> dict:
    """走真 /api/chat 的 SSE：记首个字到达、整句耗时、原话、这一轮有没有等过模型。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/chat/{USER}",
        data=json.dumps({"message": text, "conversation_history": []}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    t0, first, reply, statuses = time.time(), None, "", []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            for raw in res:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data: "):
                    continue
                el = time.time() - t0
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                if kind == "status":
                    statuses.append(f"{ev.get('text')}@{el:.0f}s")
                elif kind == "token" and first is None:
                    first = el
                elif kind == "reply":
                    reply = ev.get("text") or ""
                    if first is None:
                        first = el
                elif kind == "error":
                    reply = f"（报错）{ev.get('text')}"
    except Exception as e:
        reply = reply or f"（没回话）{type(e).__name__}"
    return {"first": first, "total": time.time() - t0, "reply": reply.strip(),
            "statuses": statuses}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sentences", type=int, default=len(SENTENCES),
                    help="问几句（每句两臂各一次，2 次中转账度）")
    a = ap.parse_args()
    sentences = SENTENCES[:max(1, a.sentences)]

    print("起两份隔离沙箱（空库，不碰 backend/moz.db）：一份开思考、一份关思考……")
    print("注意：这里的\"首字\"只能当参考——两份沙箱聊完都会在后台跑落库（也是模型调用），"
          "抢的就是同一条中转。要干净的耗时请用 tools/bench_latency.py（它会先等队列排空）。"
          "这个工具要比的是**原话**。")
    arms = {}
    for name, thinking in (("开着想", True), ("关掉想", False)):
        proc, port, tmp, log = start_sandbox(thinking)
        if not wait_ready(port):
            print(f"✗ {name} 那份沙箱没起来，看 {tmp / 'server.log'}")
            proc.terminate()
            return 1
        arms[name] = (proc, port, tmp, log)
        print(f"  {name}: 127.0.0.1:{port}（MOZ_CHAT_THINKING={'1' if thinking else '0'}）")

    try:
        for text in sentences:
            print(f"\n问：{text}")
            got = {}
            # 两臂交替问：同一个窗口里比，别把中转的时段漂移当成参数效果
            for name in ("开着想", "关掉想"):
                got[name] = ask(arms[name][1], text)
            for name in ("开着想", "关掉想"):
                r = got[name]
                head = "—" if r["first"] is None else f"{r['first']:.1f}s"
                print(f"  [{name}] 首字 {head} ／ 整句 {r['total']:.1f}s")
                if r["statuses"]:
                    print(f"           中间状态：{' → '.join(r['statuses'][:3])}")
                print(f"           {r['reply'] or '（一个字都没回）'}")
            if got["开着想"]["reply"] and got["关掉想"]["reply"]:
                print("  ↑ 哪句更像人话，你挑一句；这条只能你看，我说了不算")

        for name in ("开着想", "关掉想"):
            print(f"\n服务端账（{name}那份）：")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{arms[name][1]}/api/metrics",
                                            timeout=10) as res:
                    m = json.loads(res.read())
                stages = {k: v.get("p50") for k, v in (m.get("first_token_stages") or {}).items()
                          if v.get("samples")}
                print("   分段中位（秒）：", json.dumps(stages, ensure_ascii=False))
                print("   first_token：", json.dumps(m.get("first_token"), ensure_ascii=False),
                      "／ first_token_relay：", json.dumps(m.get("first_token_relay"), ensure_ascii=False))
                emo = m.get("emotion") or {}
                print("   sensitive_wait：", json.dumps(emo.get("sensitive_wait"), ensure_ascii=False),
                      "／ thinking：", json.dumps(emo.get("thinking"), ensure_ascii=False))
            except Exception as e:
                print(f"   没读到指标：{type(e).__name__}（/api/metrics 可能要管理员钥匙）")
    finally:
        for proc, _port, tmp, log in arms.values():
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()
            log.close()
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

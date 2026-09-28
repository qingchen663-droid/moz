"""回话响应速度基准：分"本地能控的部分"和"含中转的部分"两层量。

为什么要分两层：2026-09-28 实测同一句探针话在中转那边能从 31 秒跳到 376 秒，
把两者混在一个数里量，得出的结论全是运气。

    # ① 只量本地：情感判档 + 水位扫描 + 记忆检索，随记忆条数怎么长（不碰中转、秒级）
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/bench_latency.py --local
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/bench_latency.py --local --sizes 0,10,100,1000,5000

    # ② 含中转：复制后端到临时目录、按条数起沙箱，逐种情况量 ttft 与总时长（慢、耗额度）
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/bench_latency.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/bench_latency.py --sizes 0,100

② 全部在临时目录的沙箱里跑，不碰 backend/moz.db；每轮之后会等落库队列排空再量下一句
（否则是在跟自己的后台任务抢中转），等不到就明说"没等完"，不当没事。
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
USER = "web_user_001"

# 探针句子：普通、敏感、长历史、带图四种"情况"
PLAIN = "我今天加班到十点才回家，好累"
SENSITIVE = "我妈下周要去医院做复查，我有点担心她"
TOPICS = ["工作", "健康", "家人", "考试", "钱", "宠物", "关系", "日常"]


def seeded_texts(n: int):
    """造 n 条互不重复、话题分散的记忆（别让探针句子的关键词全撞上）。"""
    out = []
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        out.append(f"用户提过{topic}相关的第{i}件小事，细节{i % 7}和{i % 3}号有关")
    return out


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def api(port, path, method="GET", body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
        return json.loads(raw) if raw else {}


def chat_timed(port, text, history=None, image=None, timeout=420):
    """走真 /api/chat 的 SSE，量：状态推进、首个 token、说完、总时长。"""
    payload = {"message": text, "conversation_history": history or []}
    if image:
        payload["image_data"] = image
    t0 = time.time()
    marks = []
    first = None
    done = None
    tokens = 0
    chars = 0
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat/{USER}",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
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
                if kind == "token":
                    tokens += 1
                    chars += len(ev.get("text") or "")
                    if first is None:
                        first = el
                        marks.append(f"首字{el:.1f}s")
                elif kind == "status":
                    marks.append(f"{ev.get('text')}@{el:.1f}s")
                elif kind == "reply":
                    done = el
                    chars = len(ev.get("text") or "")
                elif kind == "error":
                    marks.append(f"错误@{el:.1f}s {str(ev.get('text'))[:38]}")
                    return {"first": None, "done": el, "total": time.time() - t0,
                            "error": str(ev.get("text"))[:70], "marks": marks, "chars": 0}
    except Exception as e:
        return {"first": first, "done": None, "total": time.time() - t0,
                "error": f"{type(e).__name__}: {e}"[:70], "marks": marks, "chars": 0}
    return {"first": first, "done": done, "total": time.time() - t0, "error": "",
            "marks": marks, "tokens": tokens, "chars": chars}


def wait_queue_drained(port, cap=240):
    """等后台落库排空，免得下一句和自己的后台任务抢同一条中转。"""
    t0 = time.time()
    while time.time() - t0 < cap:
        try:
            q = api(port, f"/care/save-queue?user_id={USER}", timeout=8)
        except Exception:
            time.sleep(3)
            continue
        if not (q.get("pending", 0) + q.get("running", 0)):
            return round(time.time() - t0, 1), q
        time.sleep(3)
    return None, None


def local_bench(sizes):
    """只量本地：情感判档 / 水位扫描 / 检索。全打桩向量，一次中转都不发。"""
    sys.path.insert(0, str(BACKEND))
    import emotion_state as ES
    import memory_manager as MM

    print(f"{'记忆条数':>8} {'判档L0':>9} {'水位+证据':>10} {'检索1次':>10} {'检索5句':>10}  说明")
    rows = []
    for n in sizes:
        d = tempfile.mkdtemp()
        try:
            mm = MM.MemoryManager(storage_path=d, db_path=os.path.join(d, "b.db"))
            mm.embedding_service.get_embedding = lambda t: None
            mm.embedding_service.get_embeddings_batch = lambda ts: [None] * len(ts)
            for text in seeded_texts(n):
                mm.add_memory(USER, text, category=MM.MemoryCategory.FACT)

            t0 = time.perf_counter()
            for _ in range(200):
                ES.live_signal(PLAIN)
            sig_ms = (time.perf_counter() - t0) / 200 * 1000

            t0 = time.perf_counter()
            for _ in range(20):
                ES.collect_signals(mm, None, None, USER)
            stamp_ms = (time.perf_counter() - t0) / 20 * 1000
            ES.gather_evidence(mm, None, USER)          # 冷缓存那一次也算在检索外的账上
            t0 = time.perf_counter()
            ES.gather_evidence(mm, None, USER)
            ev_ms = (time.perf_counter() - t0) * 1000

            queries = [PLAIN, "我妈复查", "青柠计划谁负责", "周末干什么", "香菜"]
            t0 = time.perf_counter()
            hits = [len(mm.search_memories(USER, q)) for q in queries[:1]]
            one_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            for q in queries:
                mm.search_memories(USER, q)
            five_ms = (time.perf_counter() - t0) * 1000
            rows.append((n, sig_ms, stamp_ms + ev_ms, one_ms, five_ms, hits[0]))
            print(f"{n:>8} {sig_ms:>8.2f}ms {stamp_ms + ev_ms:>9.2f}ms {one_ms:>9.1f}ms "
                  f"{five_ms:>9.1f}ms  首句命中 {hits[0]} 条")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return rows


def tiny_jpeg():
    from io import BytesIO

    from PIL import Image
    img = Image.new("RGB", (64, 64), (200, 40, 40))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    import base64
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def relay_bench(sizes, with_extra=True, repeats=2):
    """含中转：每个条数起一个沙箱后端，量真 ttft。"""
    sys.path.insert(0, str(BACKEND))
    import memory_manager as MM

    rows = []
    for n in sizes:
        tmp = Path(tempfile.mkdtemp(prefix="moz-bench-"))
        work = tmp / "backend"
        shutil.copytree(BACKEND, work, ignore=shutil.ignore_patterns(
            "__pycache__", "memory_store", "conversations", "avatars",
            "moz.db", "moz.db-wal", "moz.db-shm", "saved_models.json", "tests"))
        for name in ("runtime_model_config.json",):
            if (BACKEND / name).exists():
                shutil.copy2(BACKEND / name, work / name)
        if (ROOT / ".env").exists():
            shutil.copy2(ROOT / ".env", tmp / ".env")
        mm = MM.MemoryManager(storage_path=str(work / "memory_store"),
                              db_path=str(work / "moz.db"))
        mm.embedding_service.get_embedding = lambda t: None
        for text in seeded_texts(n):
            mm.add_memory(USER, text, category=MM.MemoryCategory.FACT)
        if hasattr(mm, "close"):
            mm.close()

        port = free_port()
        log = open(tmp / "server.log", "wb")
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=str(work), stdout=log, stderr=log,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                     "PYTHONUNBUFFERED": "1"})
        try:
            ready = False
            for _ in range(60):
                try:
                    if api(port, "/health", timeout=3).get("status") == "ok":
                        ready = True
                        break
                except Exception:
                    time.sleep(1)
            if not ready:
                print(f"  记忆 {n} 条：沙箱没起来，看 {tmp / 'server.log'}")
                continue
            for i in range(repeats):
                # 第 1 句含索引冷启动，后面的算热缓存；同一句重复量才能把中转噪声摊开
                r = chat_timed(port, PLAIN)
                rows.append((n, f"普通·第{i + 1}句", r))
                wait_queue_drained(port)
            if with_extra and n == sizes[len(sizes) // 2]:
                hist = []
                for i in range(30):
                    hist.append({"role": "user", "content": f"第{i}句闲聊记录，说了一点工作{i}和家里{i}"})
                    hist.append({"role": "assistant", "content": f"嗯嗯收到{i}，那件事我记得{i}"})
                r_hist = chat_timed(port, "我周末想回去看看她", history=hist)
                wait_queue_drained(port)
                r_sens = chat_timed(port, SENSITIVE)
                wait_queue_drained(port)
                r_img = chat_timed(port, "这张图是什么颜色？只回颜色名", image=tiny_jpeg())
                rows.append((n, "30 条长历史", r_hist))
                rows.append((n, "敏感（等一次）", r_sens))
                rows.append((n, "带图", r_img))
                wait_queue_drained(port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
            log.close()
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'记忆条数':>8} {'情况':<14} {'首字':>8} {'说完':>8} {'总时长':>8} {'字数':>5}  备注")
    for n, case, r in rows:
        first = "—" if r["first"] is None else f"{r['first']:.1f}s"
        done = "—" if r.get("done") is None else f"{r['done']:.1f}s"
        print(f"{n:>8} {case:<14} {first:>8} {done:>8} {r['total']:>7.1f}s "
              f"{r.get('chars', 0):>5}  {r.get('error') or ''}")
    ok = [r["first"] for _n, _c, r in rows if r["first"]]
    if ok:
        ok.sort()
        print(f"\n拿到首字的 {len(ok)} 次：最快 {ok[0]:.1f}s ／ 中位 {ok[len(ok) // 2]:.1f}s ／ 最慢 {ok[-1]:.1f}s")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="只量本地部分（不发中转请求）")
    ap.add_argument("--sizes", default="", help="逗号分隔的记忆条数，如 0,10,100,1000")
    ap.add_argument("--no-extra", action="store_true", help="跳过敏感/长历史/带图三种情况")
    ap.add_argument("--repeats", type=int, default=2, help="每档重复几句普通探针（摊开中转噪声）")
    a = ap.parse_args()
    sizes = [int(x) for x in a.sizes.split(",")] if a.sizes else (
        [0, 10, 100, 1000] if a.local else [0, 10, 100])
    if a.local:
        local_bench(sizes)
    else:
        relay_bench(sizes, with_extra=not a.no_extra, repeats=a.repeats)


if __name__ == "__main__":
    main()

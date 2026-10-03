"""首字时间探测：直接裸连中转，把"开口之前"那一段拆开量，并打印**真的发出去了什么**。

为什么要有这个脚本（2026-09-29）：
- `docs/情感预热系统设计.md` 结论 2 和 RUNLOG:1922 都写着"关思考能快是空想"，
  但那两组对照其实**发的是同一个 payload**——`llm_config._build_extra_body()` 对
  `MiMo-V2.6-Flash` 返回 `None`（`MODEL_PROFILES` 里没有它的条目），
  `use_thinking=False` 从来没落到线上。两个相同请求比出来的差是 0，不是"没用"。
- 所以这个脚本每条样本都把实际 payload 的键打出来，杜绝再跑一遍同 payload 的假对照。

它只问一个问题：**中转吐第一个"看得见的字"之前，时间在花哪儿？**
  headers  →  首个任意帧  →  首个 reasoning 帧  →  首个正文帧  →  说完
中间如果有 reasoning 帧而界面一个字都不出（生产里 `_consume_stream` 只取 `chunk.content`），
那这段就是"看不见的先想后说"，是能砍的；如果 headers 本身就很久，那是排队，砍不掉。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/probe_first_token.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/probe_first_token.py --repeats 3
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/probe_first_token.py --only-baseline

不写任何库、不碰 backend/moz.db；默认那一趟大约 11 次中转请求（`--repeats/--long` 可调）。
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from model_config import get_chat_api_key, load_active_config  # noqa: E402

PLAIN = "我今天加班到十点才回家，好累"
# 和 tools/bench_latency.py 里长历史那一档同量级（那一次实测 121.3 秒没回）
LONG_ROUNDS = 30


def long_history() -> List[Dict[str, str]]:
    hist: List[Dict[str, str]] = []
    for i in range(LONG_ROUNDS):
        hist.append({"role": "user", "content": f"第{i}句闲聊记录，说了一点工作{i}和家里{i}"})
        hist.append({"role": "assistant", "content": f"嗯嗯收到{i}，那件事我记得{i}"})
    return hist


# 关闭"先想后说"的候选写法：各家网关叫法不一，逐个试，**失败也要原样记下来**。
# candidate 为 None 的那一行就是当前生产实际在发的东西（基准）。
CANDIDATES: List[Dict[str, Any]] = [
    {"label": "基准（今天线上发的那份）", "extra": None},
    {"label": "thinking=disabled", "extra": {"thinking": {"type": "disabled"}}},
    {"label": "reasoning_effort=none", "extra": {"reasoning_effort": "none"}},
    {"label": "enable_thinking=false", "extra": {"enable_thinking": False}},
    {"label": "chat_type=chat", "extra": {"chat_type": "chat"}},
]


def build_payload(model: str, messages: List[Dict[str, str]], extra: Optional[dict]) -> dict:
    payload = {"model": model, "messages": messages, "stream": True,
               "temperature": 0.8, "top_p": 0.9, "stream_options": {"include_usage": True}}
    if extra:
        payload.update(extra)
    return payload


def probe(client, base_url, api_key, payload, timeout) -> Dict[str, Any]:
    """逐帧打点：headers / 首个任意帧 / 首个 reasoning 帧 / 首个正文帧 / 说完 + usage。"""
    out = {"ok": False, "status": None, "error": "", "headers": None, "first_frame": None,
           "first_reasoning": None, "first_content": None, "done": None,
           "reasoning_chars": 0, "content_chars": 0, "reasoning_tokens": None,
           "finish_reason": "", "keys_seen": set()}
    t0 = time.time()
    partial = ""          # 分帧跨行的残包：拼起来再试一次，别默默丢掉一整帧
    try:
        with client.stream("POST", f"{base_url}/chat/completions", json=payload,
                           headers={"Authorization": f"Bearer {api_key}"},
                           timeout=timeout) as res:
            out["status"] = res.status_code
            out["headers"] = time.time() - t0
            if res.status_code != 200:
                body = res.read().decode("utf-8", "ignore")
                out["error"] = f"HTTP {res.status_code}: {body[:160]}"
                return out
            for line in res.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if partial:
                    data = partial + data
                    partial = ""
                try:
                    frame = json.loads(data)
                except json.JSONDecodeError:
                    partial = data
                    continue
                now = time.time() - t0
                if out["first_frame"] is None:
                    out["first_frame"] = now
                choices = frame.get("choices") or []
                if not choices:
                    usage = frame.get("usage") or {}
                    details = usage.get("completion_tokens_details") or {}
                    if details:
                        out["reasoning_tokens"] = details.get("reasoning_tokens")
                    continue
                delta = choices[0].get("delta") or {}
                out["keys_seen"].update(delta.keys())
                content = delta.get("content") or ""
                # 各家把"先想后说"的中间态放在不同字段里，见到哪个算哪个
                reasoning = (delta.get("reasoning_content") or delta.get("reasoning")
                             or delta.get("thinking") or "")
                if reasoning:
                    out["reasoning_chars"] += len(reasoning)
                    if out["first_reasoning"] is None:
                        out["first_reasoning"] = now
                if content:
                    out["content_chars"] += len(content)
                    if out["first_content"] is None:
                        out["first_content"] = now
                if choices[0].get("finish_reason"):
                    out["finish_reason"] = choices[0]["finish_reason"]
            out["done"] = time.time() - t0
            out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:160]
        out["done"] = time.time() - t0
    return out


def fmt(v) -> str:
    return "  —  " if v is None else f"{v:6.1f}"


def report(label: str, payload: dict, r: Dict[str, Any]) -> None:
    sent = sorted(k for k in payload if k not in ("messages", "model", "stream"))
    print(f"\n[{label}]")
    print(f"  实际发出的非 messages 键: {sent}")
    if not r["ok"]:
        print(f"  ✗ {r['error'] or ('HTTP ' + str(r['status']))}")
        return
    print(f"  headers {fmt(r['headers'])}s → 首帧 {fmt(r['first_frame'])}s → "
          f"首个 reasoning {fmt(r['first_reasoning'])}s → 首个正文 {fmt(r['first_content'])}s → 说完 {fmt(r['done'])}s")
    print(f"  reasoning {r['reasoning_chars']} 字 / 正文 {r['content_chars']} 字 / "
          f"usage reasoning_tokens={r['reasoning_tokens']} / delta 里见过的键={sorted(r['keys_seen'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3,
                    help="基准短话打几句（同一档至少 3 次才敢看中位）")
    ap.add_argument("--only-baseline", action="store_true", help="只打基准，跳过候选参数")
    ap.add_argument("--long", type=int, default=2, help="长历史那几行打几次（基准 + 1 个候选）")
    ap.add_argument("--timeout", type=float, default=180.0)
    a = ap.parse_args()

    cfg = load_active_config()
    api_key = get_chat_api_key()
    if not api_key:
        print("没拿到 LLM_API_KEY，探不了。")
        return 1
    print(f"渠道 {cfg['base_url']} ／ 模型 {cfg['model']} ／ 配置里 use_thinking={cfg.get('use_thinking')}")
    print("注意：配置里的 use_thinking 会不会真的发出去，看下面每行的\"实际发出的键\"。")

    short_msgs = [{"role": "user", "content": PLAIN}]
    long_msgs = long_history() + [{"role": "user", "content": "我周末想回去看看她"}]
    base_rows: List[Dict[str, Any]] = []
    planned = a.repeats + (0 if a.only_baseline else len(CANDIDATES) - 1 + max(a.long, 0) * 2)
    print(f"这一趟大约打 {planned} 次中转。")

    def ask(client, label, msgs, extra):
        payload = build_payload(cfg["model"], msgs, extra)
        r = probe(client, cfg["base_url"], api_key, payload, a.timeout)
        report(label, payload, r)
        return r

    with httpx.Client() as client:
        # A 基准：同一句短话重复打，先给"今天到底多久开口"一个能比较的底
        for i in range(a.repeats):
            r = ask(client, f"基准·短话 第{i + 1}次", short_msgs, None)
            base_rows.append(r)
        if not a.only_baseline:
            # B 候选参数：各打一次，看有没有哪个能把"看不见的先想后说"关掉
            for cand in CANDIDATES[1:]:
                ask(client, f"候选 {cand['label']}·短话", short_msgs, cand["extra"])
            # C 长历史：基准和一个最像有戏的候选各打，分辨"参数生效"与"话长"
            best = next((c for c in CANDIDATES[1:] if c["label"] == "reasoning_effort=none"),
                        CANDIDATES[1])
            for i in range(max(a.long, 0)):
                ask(client, f"基准·{LONG_ROUNDS} 轮长历史 第{i + 1}次", long_msgs, None)
                ask(client, f"候选 {best['label']}·{LONG_ROUNDS} 轮长历史", long_msgs, best["extra"])

    got = [r["first_content"] for r in base_rows if r.get("first_content")]
    heads = [r["headers"] for r in base_rows if r.get("headers")]
    reason = [r for r in base_rows if r.get("reasoning_chars")]
    print("\n" + "=" * 68)
    if got:
        print(f"基准（短话 ×{len(base_rows)}）首个正文：中位 {statistics.median(got):.1f}s ／ "
              f"最快 {min(got):.1f}s ／ 最慢 {max(got):.1f}s")
        if heads:
            print(f"  其中响应头就花了中位 {statistics.median(heads):.1f}s —— 这段是排队/握手，砍不掉")
        print(f"  出现看不见的 reasoning 的样本：{len(reason)}/{len(base_rows)}"
              f"{'（有的话，那才是能砍的一段）' if reason else '（没有：thinking 不在这条链上）'}")
    else:
        print("基准一次都没拿到正文，别下结论——先看上面的错误。")
    print("判读：候选参数那几行如果『首个正文』明显比基准早，就值得把该参数写进 MODEL_PROFILES；"
          "如果全 400 或跟基准一样，『关思考』这条整方向作废。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

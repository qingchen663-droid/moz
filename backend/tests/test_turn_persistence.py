"""中转报错那一轮不能凭空消失：登记、落库重试、对话记录三处都要成立。

第十轮实测：沙箱说 6 件事实，其中 3 问撞上中转 500（"分组 auto 下模型…可用渠道不存在"），
那两轮用户说过的话既没进队列也没进对话记录（6 句只登记了 4 句，"青柠计划/老郑"就这么丢了）。
"""
import asyncio
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emotion_graph as EG  # noqa: E402
from emotion_graph import SaveDeps, run_save_job  # noqa: E402

FACT = "记一下：我最近在跟一个叫青柠计划的方案，负责人是老郑"


class RecordingQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


class BoomStream:
    """像中转那样，token 一个都没吐就先报错。"""

    def stream(self, messages):
        raise RuntimeError("分组 auto 下模型 MiMo-V2.6-Flash 的可用渠道不存在")
        yield None  # pragma: no cover


class OkStream:
    def stream(self, messages):
        for token in ("好", "的"):
            yield type("C", (), {"content": token})()


def _drive(monkeypatch, llm, queue):
    monkeypatch.setattr(EG, "get_chat_client", lambda **kw: llm)
    monkeypatch.setattr(EG, "emotion_analysis_node", lambda state: {})
    monkeypatch.setattr(EG, "_run_memory_retrieval", lambda state, mm, wm=None: {})
    monkeypatch.setattr(EG, "_build_dialogue_messages", lambda state: [])

    async def collect():
        return [c async for c in EG.run_emotion_workflow_streaming(
            memory_manager=None, user_id="u", user_message=FACT,
            save_queue=queue, save_worker=None)]

    return asyncio.run(collect())


def test_relay_error_still_registers_the_turn(monkeypatch):
    queue = RecordingQueue()
    events = _drive(monkeypatch, BoomStream(), queue)
    assert any(e.get("type") == "error" for e in events), "错误还是要如实告诉前端"
    assert len(queue.calls) == 1, f"报错那一轮也该登记，实得 {len(queue.calls)}"
    call = queue.calls[0]
    assert "老郑" in call["user_message"]
    assert call["reply"] == "", "半截/没有回复都不该被当成她说过的话存进长期记忆"


def test_successful_turn_registers_once(monkeypatch):
    queue = RecordingQueue()
    events = _drive(monkeypatch, OkStream(), queue)
    reply = [e for e in events if e.get("type") == "reply"]
    assert reply and reply[0]["text"] == "好的"
    assert len(queue.calls) == 1
    assert queue.calls[0]["reply"] == "好的"


def test_queue_accepts_empty_reply_but_not_empty_message():
    from save_queue import SaveQueue

    # Windows：sqlite 连接不关，目录就删不掉（HANDOFF §7 第 18 条）——第十四轮起真的关掉再删
    d = tempfile.mkdtemp(prefix="moz-turn-queue-")
    q = None
    try:
        q = SaveQueue(os.path.join(d, "moz.db"))
        assert q.enqueue("u1", FACT, "") is not None, "moz 没答上来那一轮也要排队"
        assert q.enqueue("u1", "", "她自己说了一堆") is None, "用户什么都没说不占队列"
        assert q.pending_for("u1") == 1
    finally:
        if q is not None:
            q._conn().close()
        shutil.rmtree(d, ignore_errors=True)


def _job():
    return {"user_id": "u1", "user_message": FACT, "reply": "", "conversation_id": "c1"}


class AllBad:
    def extract_and_store_facts(self, *a, **kw):
        raise RuntimeError("中转 500")


class AllGood:
    def __init__(self):
        self.calls = 0

    def extract_and_store_facts(self, *a, **kw):
        self.calls += 1

    def attach_temporal_metadata(self, **kw):
        return 0


def test_all_steps_failed_retries_but_partial_does_not(monkeypatch):
    """全挂＝这一轮等于没落下，必须抛回队列；只挂一步别重跑，否则记忆会存成重复。"""
    import care_extractor

    def boom(*_a, **_kw):
        raise RuntimeError("关心抽取挂了")

    monkeypatch.setattr(care_extractor, "harvest", boom)   # 别让它真去打中转

    bad = AllBad()
    with pytest.raises(RuntimeError):
        asyncio.run(run_save_job(SaveDeps(memory_manager=bad), _job()))

    good = AllGood()
    asyncio.run(run_save_job(SaveDeps(memory_manager=good), _job()))    # 不抛
    assert good.calls == 1

    # 两步里挂一步：吞掉，让已经存好的部分留着
    mixed = SaveDeps(memory_manager=AllGood(), care_store=object())
    asyncio.run(run_save_job(mixed, _job()))


def test_temporal_labels_really_attach():
    """第八轮把落库改成并行之后，时间标签一直在静默 NameError——
    TemporalExtractor 只在别的函数里局部导入过。这条钉住"别再静默吞掉"。"""

    class Tagged(AllGood):
        def __init__(self):
            super().__init__()
            self.tagged = []

        def attach_temporal_metadata(self, **kw):
            self.tagged.append(kw)
            return 1

    mm = Tagged()
    job = {"user_id": "u1", "conversation_id": "c1", "reply": "记下了",
           "user_message": "我下周三下午两点要去做项目答辩"}
    asyncio.run(run_save_job(SaveDeps(memory_manager=mm), job))
    assert mm.tagged, "时间标签没挂上：TemporalExtractor 那条路又断了"
    event_time = (mm.tagged[0]["temporal_data"] or {}).get("event_time") or {}
    assert event_time.get("timestamp"), "说了下周三，时间标签里却没有时间点"

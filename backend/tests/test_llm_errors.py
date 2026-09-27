"""中转报错翻译：每条都必须回答"下一步点哪里"，且不许把内部结构摊给用户。

这些报文是第九、十轮从真沙箱里抄下来的原样（openai-python 把异常 str 成
`Error code: 500 - {'error': {'message': '…'}}`，键值是**单引号的字典**）。
以前 _short 只认 `"error": "字符串"` 那种形状，于是用户气泡里出现字典字面量和 request id。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_errors import friendly_llm_error  # noqa: E402

INTERNAL_MARKS = ("{'", "'}", "Error code:", "error code:", "request id", "{\"", "Traceback")


def say(raw, has_image=False):
    return friendly_llm_error(RuntimeError(raw), has_image)


def assert_human(text):
    for mark in INTERNAL_MARKS:
        assert mark not in text, f"给用户的话里漏出了内部结构：{mark} → {text}"
    assert len(text) <= 160, f"太长了，气泡里读不完：{len(text)} 字 → {text}"


def test_503_model_not_found_gives_a_next_step():
    # 第十轮故意把沙箱模型名改死时，中转原样返回的那条
    text = say("Error code: 503 - {'error': {'code': 'model_not_found', "
               "'message': 'No available channel for model Qoder-No-Such under group auto (retry)', "
               "'type': 'model_not_found'}}")
    assert_human(text)
    assert "模型" in text and ("设置" in text or "列表" in text)


def test_500_channel_missing_in_chinese():
    # 第十轮沙箱 6 问里 3 问撞的那条
    text = say("Error code: 500 - {'error': {'message': '分组 auto 下模型 MiMo-V2.6-Flash 的可用渠道不存在（retry） "
               "(request id: 20260928011122334455)', 'type': 'server_error', 'code': 'internal_error'}}")
    assert_human(text)
    assert "request id" not in text
    assert "模型" in text


def test_500_other_payload_keeps_the_message_only():
    text = say("Error code: 500 - {'error': {'message': '上游过载，请重试', 'code': 'overload'}}")
    assert "上游过载" in text
    assert_human(text)


def test_401_and_404_point_at_the_settings_button():
    assert "密钥" in say("Error code: 401 - {'error': {'code': '401', 'message': '令牌已过期或验证不正确'}}")
    assert "模型列表" in say("Error code: 404 - {'error': {'message': 'The model `Foo` does not exist'}}")


def test_connection_and_timeout_stay_actionable():
    assert_human(say("Connection error."))
    assert_human(say("Request timed out."))


def test_api_key_never_leaks():
    # 401 那条整句被换掉，密钥根本到不了用户面前；会带出 detail 的分支必须打码
    text = say("Error code: 500 - {'error': {'message': 'upstream key sk-ABCDEFGHIJKLMNOP rejected'}}")
    assert "sk-ABC" not in text
    assert "***" in text


def test_image_400_only_mentions_vision_when_there_is_an_image():
    raw = "Error code: 400 - {'error': {'message': 'this model does not support image input'}}"
    assert "图片" in say(raw, has_image=True)
    # 没带图时同样的 400 不该提看图
    assert "图片" not in say("Error code: 400 - {'error': {'message': 'max_tokens too large'}}")


def test_504_upstream_timeout_does_not_claim_a_number():
    """本轮普通模式最后一条真报文。message 的值本身就是英文码，
    而且"超过两分钟"是我们自己的墙钟，不能安到中转的 504 头上。"""
    text = say("Error code: 504 - {'error': {'message': 'openai_error', "
               "'type': 'bad_response_status_code', 'param': None, 'code': 'upstream_timeout'}}")
    assert_human(text)
    assert "两分钟" not in text


def test_our_own_timeout_keeps_the_number():
    from llm_errors import friendly_llm_error as f
    assert "两分钟" in f(TimeoutError())


def test_system_copy_never_becomes_a_memory():
    """她自己的兜底句和报错文案不许被当成"关于用户的事实"存进长期记忆。

    量过：改前 4 句里存下 7 条，其中两条是「抱歉，我暂时无法回复」
    和「这个模型名在中转那边没有可用的渠道」——后者会被以后检索当成用户的事回忆出来。
    """
    from llm_errors import looks_like_system_copy

    junk = ["", "  ", "抱歉，我暂时无法回复。",
            "这个模型名在中转那边暂时没有可用的渠道——可能名字写错了。等十几秒再发一次。",
            "模型密钥被中转拒绝了。打开「模型」设置，重新填一次 API Key 再发。",
            "回复没生成出来（上游过载）。可以先重试一次。",
            "这句我没接住：后台出了点意外（AttributeError）。再发一次试试。"]
    for text in junk:
        assert looks_like_system_copy(text), f"这条会被存成长期记忆：{text}"
    real = ["好的，我记下了：你下周三要答辩", "团子呀，五岁的橘猫", "香菜！你之前说过讨厌吃"]
    for text in real:
        assert not looks_like_system_copy(text), f"这是她正常说过的话，不该被拦：{text}"

"""
LLM 报错翻译
───────────
中转返回的原文（"Error code: 401 - {'error': {...}}"、"Connection error."）对用户
没有任何可执行性。界面里这句话往往是用户唯一的线索，所以每条都必须回答"下一步点哪里"。
"""

import re

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
_STATUS_RE = re.compile(r"error code:\s*(\d{3})|status(?:_code)?[\"']?\s*[:=]\s*(\d{3})")
_RELAY_MSG_RE = re.compile(r"\"error\"\s*:\s*\"([^\"]{4,})\"")
# openai-python 把异常 str 成 `Error code: 500 - {'error': {'message': '…'}}`：
# 键值是单引号的**字典**，不是字符串。只认 `"error": "..."` 的话，用户气泡里就会出现字典字面量。
_NESTED_MSG_RE = re.compile(r"['\"](?:message|msg|detail)['\"]\s*:\s*['\"]([^'\"]{4,})['\"]")
_REQUEST_ID_RE = re.compile(r"\s*[（(]\s*request id[^）)]*[）)]", re.I)
_RETRY_RE = re.compile(r"\s*[（(]retry[）)]", re.I)


def _short(raw: str, limit: int = 100) -> str:
    text = _KEY_RE.sub("***", " ".join(raw.split()))
    relay = _RELAY_MSG_RE.search(text) or _NESTED_MSG_RE.search(text)
    if relay:
        # 中转常常把真正的原因放在 error/message 里，剥掉 JSON 外壳更好读
        text = relay.group(1)
    text = _RETRY_RE.sub("", _REQUEST_ID_RE.sub("", text)).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _status_code(raw: str) -> int:
    for m in _STATUS_RE.finditer(raw.lower()):
        for g in m.groups():
            if g:
                return int(g)
    return 0


def friendly_llm_error(error, has_image: bool = False, waited: float | None = None) -> str:
    raw = str(error)
    low = raw.lower()
    status = _status_code(raw)

    if has_image and (
        status in (400, 415, 422)
        or re.search(r"image_url|multi_?modal|do(es)? not support|unsupported.*(image|file)", low)
    ):
        # 只有在确实带了图片时才提"看图"，否则同样的 400 会让人一头雾水
        return (
            "这个模型看不了图片。打开「模型」设置，取消勾选「能看懂图片」，"
            "或者从「获取模型列表」里换一个支持视觉的模型，再把这张图发一次。"
        )

    if status in (401, 403) or "invalid_api_key" in low or "authentication" in low:
        return "模型密钥被中转拒绝了。打开「模型」设置，重新填一次 API Key 再发。"

    if status == 404 or "does not exist" in low or "invalid model" in low:
        return "模型名在中转上不存在。打开「模型」设置，点「获取模型列表」，从列表里选一个。"

    if "no available channel" in low or "model_not_found" in low \
            or "可用渠道不存在" in raw or "渠道不存在" in raw:
        # 同一个毛病（这个名字现在用不了），但中转一会儿 404、一会儿 500/503，
        # 落进通用 500 分支就只剩"出了点问题"，用户不知道下一步点哪里
        return ("这个模型名在中转那边暂时没有可用的渠道——可能名字写错了，也可能那个渠道刚下线。"
                "等十几秒再发一次；一直不行的话打开「模型」设置，点「获取模型列表」换一个。")

    if status == 429 or "rate limit" in low or "too many requests" in low:
        return "中转限流了。等十几秒再发一次就好。"

    if "connectionerror" in low or "connection error" in low or "cannot connect" in low \
            or "unreachable" in low or "getaddrinfo" in low or "ssl" in low and "error" in low:
        return (
            "连不上模型中转。可能是网络或代理断了 —— 先确认能打开那个地址，"
            "稍等几秒再把这句话发一次。"
        )

    if isinstance(error, TimeoutError):
        # 守卫是"120 秒没吐出一个字"，不是总时长上限：第十四轮实测一句探针对话
        # 拖到 383 秒才走到这里。只说"超过两分钟"就把用户等到的时间报小了 3 倍。
        if waited is not None and waited >= 1:
            return (f"这次想得太久了——等了 {int(waited)} 秒还没想完（中间两分钟没吐出一个字），"
                    "答案没出来。把问题拆短一点再发一次，或者稍等几秒重发。")
        return "这次想得太久了（超过两分钟），答案没出来。把问题拆短一点再发一次。"
    if "timeout" in low or "timed out" in low or "取消原因" in raw or "cancel" in low:
        # 504/openai 的超时不一定是"我们那两分钟"，也别断言是谁超时——说不成立的细节比不说更糟
        return "这一轮等模型回话等超时了。稍等几秒再把这句话发一次；经常这样的话，把问题拆短一点或换个模型。"

    if status >= 500 or "渠道" in raw:
        return f"中转那边出了点问题（{_short(raw)}）。稍等几秒再发一次，一直不行的话就换个模型。"

    detail = _short(raw)
    return f"回复没生成出来（{detail}）。可以先重试一次，不行就到「模型」设置里换个模型。"


# 这些句子是她自己的兜底/报错文案，不是用户的事实。要是存进长期记忆，
# moz 以后会把"这个模型名在中转那边没有可用的渠道"当成一条关于用户的事来回忆。
SYSTEM_COPY_MARKS = (
    "抱歉，我暂时无法回复",
    "这个模型名在中转",
    "模型名在中转上不存在",
    "中转那边出了点问题",
    "模型密钥被中转拒绝",
    "连不上模型中转",
    "中转限流了",
    "这次想得太久",
    "回复没生成出来",
    "这句我没接住",
)


def looks_like_system_copy(text: str) -> bool:
    """空回复、以及上面这些文案，都不配占用一条长期记忆。"""
    t = (text or "").strip()
    if len(t) < 2:
        return True
    return any(mark in t for mark in SYSTEM_COPY_MARKS)

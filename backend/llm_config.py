"""
LLM 客户端工厂模块
支持运行时动态读取模型配置与特性
"""

from typing import Optional
from model_config import (
    load_active_config,
    get_chat_api_key,
    get_model_profile,
)

def _build_extra_body(profile: dict, use_thinking: bool) -> Optional[dict]:
    """按 profile 组装 extra_body——**"关思考"这个意图也必须真的发到线上**。

    老写法第一行是 `if not profile or not use_thinking: return None`，于是"关"被整个吞掉：
    没在 MODEL_PROFILES 里登记的模型（当前生效的 MiMo 正是）开和关发的是同一个 payload，
    那两轮"关思考没用"的对照其实是拿同一个请求比的自己。
    现在各家自己声明开/关两套键；没声明"关"的写法就维持不发（不许瞎猜参数名，
    猜错了要么被中转静默忽略、要么直接 400）。
    """
    if not profile:
        return None
    if not use_thinking:
        off = profile.get("thinking_off")
        return dict(off) if off else None

    extra_body = {}
    if "thinking" in profile:
        extra_body["thinking"] = profile["thinking"]
    if "reasoning_effort" in profile:
        extra_body["reasoning_effort"] = profile["reasoning_effort"]
    if "extra_body" in profile:
        extra_body.update(profile["extra_body"])
    return extra_body or None

def get_llm_client(
    temperature: float = 0.7,
    top_p: Optional[float] = None,
    use_thinking: Optional[bool] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_retries: int = 1,
):
    from langchain_openai import ChatOpenAI

    active = load_active_config()
    target_model = model or active["model"]
    target_base_url = base_url or active["base_url"]
    target_api_key = api_key or get_chat_api_key()
    
    thinking_enabled = active.get("use_thinking", False) if use_thinking is None else use_thinking

    kwargs = {
        "model": target_model,
        "base_url": target_base_url,
        "api_key": target_api_key,
        "temperature": temperature,
        "max_retries": max_retries,
    }

    if top_p is not None:
        kwargs["top_p"] = top_p

    profile = get_model_profile(target_model)
    extra_body = _build_extra_body(profile, thinking_enabled)
    # 静默是这套改动最大的敌人：到底发没发出去，先给自己记一笔账
    import emotion_state
    emotion_state.bump_counter("thinking_param_sent" if extra_body else "thinking_param_absent")
    if extra_body:
        kwargs["extra_body"] = extra_body
        if thinking_enabled and ("thinking" in extra_body or "enable_thinking" in extra_body):
            # 只有开着思考的几家（glm/qwen）要求温度锁 1.0；关思考那次不该跟着改
            kwargs["temperature"] = 1.0
            kwargs.pop("top_p", None)

    return ChatOpenAI(**kwargs)

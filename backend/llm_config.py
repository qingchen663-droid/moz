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
    if not profile or not use_thinking:
        return None

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
    if extra_body:
        kwargs["extra_body"] = extra_body
        if "thinking" in extra_body or "enable_thinking" in extra_body:
            kwargs["temperature"] = 1.0
            kwargs.pop("top_p", None)

    return ChatOpenAI(**kwargs)

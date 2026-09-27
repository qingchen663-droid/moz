"""
模型配置中心
───────────
支持从 runtime_model_config.json 动态读写配置，也可回退到硬编码默认值。
提供商自动识别与 API Key 管理。
"""

import os
import json
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "runtime_model_config.json")
_SAVED_PATH = os.path.join(os.path.dirname(__file__), "saved_models.json")
SAVED_MAX = 12

# 默认配置
DEFAULT_CHAT_MODEL = "deepseek-v4-flash"
DEFAULT_CHAT_BASE_URL = "https://api.deepseek.com/v1"

def load_active_config() -> dict:
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "model": data.get("model", DEFAULT_CHAT_MODEL),
                    "base_url": data.get("base_url", DEFAULT_CHAT_BASE_URL),
                    "api_key": data.get("api_key", ""),
                    "use_thinking": data.get("use_thinking", False),
                    # None = 未声明，按模型名猜；True/False = 用户显式声明
                    "multimodal": data.get("multimodal"),
                }
        except Exception:
            pass
    return {
        "model": DEFAULT_CHAT_MODEL,
        "base_url": DEFAULT_CHAT_BASE_URL,
        "api_key": "",
        "use_thinking": False,
        "multimodal": None,
    }

def save_active_config(model: str, base_url: str, api_key: str = "", use_thinking: bool = False,
                       multimodal: Optional[bool] = None) -> None:
    data = {
        "model": model.strip(),
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "use_thinking": use_thinking,
        "multimodal": multimodal,
    }
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_current_model() -> str:
    return load_active_config()["model"]


# ================================================================
# 我存的模型：同一套地址/密钥下想切来切去的几份配置
# ================================================================

def load_saved_models() -> List[Dict[str, Any]]:
    """里面带 API Key，所以 saved_models.json 和 runtime_model_config.json 一样不入版本库。"""
    if not os.path.exists(_SAVED_PATH):
        return []
    try:
        with open(_SAVED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and str(d.get("name") or "").strip()]


def _write_saved_models(items: List[Dict[str, Any]]) -> None:
    tmp = _SAVED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SAVED_PATH)  # 原子替换，别留半截 JSON


def save_current_as(name: str) -> Dict[str, Any]:
    """把"当前生效的那套"存成一个可点回来的名字。

    Key 由后端自己从生效配置里读，绝不从浏览器传上来——界面上不该出现密钥。
    """
    label = (name or "").strip()[:20]
    if not label:
        raise ValueError("给这套配置起个名字，比如「中转 deepseek」")
    cfg = load_active_config()
    entry = {
        "name": label,
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "api_key": cfg.get("api_key") or "",
        "use_thinking": bool(cfg.get("use_thinking")),
        "multimodal": cfg.get("multimodal"),
        "saved_at": time.time(),
    }
    items = [i for i in load_saved_models() if i["name"] != label]
    items.append(entry)
    _write_saved_models(items[-SAVED_MAX:])
    return entry


def delete_saved_model(name: str) -> bool:
    items = load_saved_models()
    left = [i for i in items if i["name"] != (name or "").strip()[:20]]
    if len(left) == len(items):
        return False
    _write_saved_models(left)
    return True


def apply_saved_model(name: str) -> Dict[str, Any]:
    label = (name or "").strip()[:20]
    entry = next((i for i in load_saved_models() if i["name"] == label), None)
    if not entry:
        raise KeyError(label)
    save_active_config(
        model=entry.get("model", ""),
        base_url=entry.get("base_url", ""),
        api_key=entry.get("api_key", "") or "",
        use_thinking=bool(entry.get("use_thinking")),
        multimodal=entry.get("multimodal"),
    )
    return entry


def get_current_base_url() -> str:
    return load_active_config()["base_url"]

# 为了保持对老代码的兼容
CHAT_MODEL = get_current_model()
CHAT_BASE_URL = get_current_base_url()

# ================================================================
# 模型特性配置
# ================================================================

MODEL_PROFILES = {
    "glm-4.6v": {"thinking": {"type": "enabled"}},
    "glm-4.6v-flash": {"thinking": {"type": "enabled"}},
    "glm-4.6": {"thinking": {"type": "enabled"}},
    "glm-5.1": {"thinking": {"type": "enabled"}},
    "deepseek": {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    },
    "gpt-4o": {},
    "gpt-5.5": {},
    "qwen": {
        "extra_body": {"enable_thinking": True, "return_reasoning": True},
    },
}

def get_model_profile(model_name: str) -> dict:
    model_lower = model_name.lower()
    if model_lower in MODEL_PROFILES:
        return MODEL_PROFILES[model_lower]
    for key in sorted(MODEL_PROFILES.keys(), key=len, reverse=True):
        if model_lower.startswith(key.lower()):
            return MODEL_PROFILES[key]
    return {}

EMBED_MODEL = "embedding-3"
EMBED_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
EMBED_PROVIDER = "zhipu"

PROVIDER_KEY_MAP = {
    "deepseek":    "DEEPSEEK_API_KEY",
    "openai":      "OPENAI_API_KEY",
    "zhipu":       "ZHIPU_API_KEY",
    "qwen":        "DASHSCOPE_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
}

_PROVIDER_KEY_ALIASES = {
    "DEEPSEEK_API_KEY": "Deepseek_API_KEY",
    "ZHIPU_API_KEY": "ZhipuAI_API_KEY",
}

def detect_provider(model: str = None, base_url: str = None) -> str:
    cfg = load_active_config()
    m = (model or cfg["model"]).lower()
    u = (base_url or cfg["base_url"]).lower()

    if "deepseek" in m or "deepseek" in u:
        return "deepseek"
    elif "openai.com" in u or m.startswith(("gpt-", "o1", "o3")):
        return "openai"
    elif "bigmodel.cn" in u or "zhipu" in m or "glm" in m:
        return "zhipu"
    elif "qwen" in m or "dashscope" in u:
        return "qwen"
    elif "siliconflow" in u or "silicon" in m:
        return "siliconflow"
    return "other"

def resolve_api_key(provider: str) -> str:
    cfg = load_active_config()
    if cfg.get("api_key"):
        return cfg["api_key"]

    if provider not in PROVIDER_KEY_MAP:
        return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("API_KEY", "")

    key_name = PROVIDER_KEY_MAP[provider]
    api_key = os.environ.get(key_name, "")
    if not api_key and key_name in _PROVIDER_KEY_ALIASES:
        api_key = os.environ.get(_PROVIDER_KEY_ALIASES[key_name], "")
    return api_key

def get_chat_api_key() -> str:
    cfg = load_active_config()
    if cfg.get("api_key"):
        return cfg["api_key"]
    provider = detect_provider(cfg["model"], cfg["base_url"])
    return resolve_api_key(provider)

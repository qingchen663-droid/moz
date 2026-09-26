"""天气适配：用于"要下雨了记得带伞"这类主动关心。

选型说明：本机访问 Open-Meteo 与 wttr.in 均超时不通（被网络侧拦掉），
腾讯天气 wis.qq.com 可达、免 key、返回 day/night 天气文本，够用。
这是非官方接口，字段变了就回落到"拿不到天气"，不影响其他关心功能。
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

API = "https://wis.qq.com/weather/common"
CACHE_TTL = 3600  # 1 小时
TIMEOUT = 10
RAIN_WORDS = ("雨", "雪", "冰雹", "阵雨", "雷")

_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


def _has_rain(text: str) -> bool:
    return any(w in (text or "") for w in RAIN_WORDS)


def _request(province: str, city: str) -> Dict[str, Any]:
    params = {
        "source": "pc",
        "weather_type": "observe|forecast_24h",
        "province": province,
        "city": city,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Referer": "https://weather.qq.com/"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8"))


def _parse(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = (payload or {}).get("data") or {}
    obs = data.get("observe") or {}
    forecast = data.get("forecast_24h") or {}

    def day(offset: str) -> Dict[str, str]:
        d = forecast.get(offset) if isinstance(forecast, dict) else None
        if not d:
            return {}
        return {
            "min": str(d.get("min_degree", "")),
            "max": str(d.get("max_degree", "")),
            "day": str(d.get("day_weather", "")),
            "night": str(d.get("night_weather", "")),
        }

    today = day("0")
    tomorrow = day("1")
    # 城市名不存在时接口会回一具空壳，必须当成失败，否则下游会以为"今天不下雨"
    if not (obs.get("degree") or obs.get("weather") or today.get("day")):
        return None
    return {
        "ok": True,
        "degree": str(obs.get("degree", "")),
        "humidity": str(obs.get("humidity", "")),
        "weather": str(obs.get("weather", "")),
        "wind": str(obs.get("wind_power", "")),
        "update_time": str(obs.get("update_time", "")),
        "today": today,
        "tomorrow": tomorrow,
        "rain_today": _has_rain(today.get("day", "")) or _has_rain(today.get("night", "")),
        "rain_tomorrow": _has_rain(tomorrow.get("day", "")) or _has_rain(tomorrow.get("night", "")),
        "checked_at": time.time(),
    }


def get_weather(province: str, city: str, force: bool = False) -> Optional[Dict[str, Any]]:
    """取城市天气；命中缓存直接返回。拿不到时返回 None（调用方需容错）。"""
    city = (city or "").strip()
    if not city:
        return None
    province = (province or "").strip()
    key = f"{province}|{city}"
    now = time.time()

    with _lock:
        cached = _cache.get(key)
        if cached and not force and now - cached[0] < CACHE_TTL:
            return cached[1]

    try:
        parsed = _parse(_request(province, city))
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        logger.warning("[天气] 获取失败 %s：%s", key, type(e).__name__)
        with _lock:
            cached = _cache.get(key)
        if cached:
            return dict(cached[1], stale=True)  # 过期数据也比没有强
        return None

    if not parsed:
        return None
    with _lock:
        _cache[key] = (now, parsed)
    return parsed

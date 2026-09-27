"""
时间标签模块 - 记忆的时间维度

为记忆添加时间上下文，让模型理解"昨天"、"去年夏天"等时间表达。
"""

from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime, timedelta
import calendar
import re
import time


# ================================================================
# 时间标签数据结构
# ================================================================

@dataclass
class TemporalMetadata:
    """记忆的时间标签元数据"""
    
    event_time: Dict[str, any] = field(default_factory=dict)
    time_context: Dict[str, str] = field(default_factory=dict)
    recurrence: Dict[str, any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "event_time": self.event_time,
            "time_context": self.time_context,
            "recurrence": self.recurrence,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "TemporalMetadata":
        return cls(
            event_time=data.get("event_time", {}),
            time_context=data.get("time_context", {}),
            recurrence=data.get("recurrence", {}),
        )
    
    def get_age_in_days(self) -> Optional[float]:
        """获取记忆的年龄（天数）"""
        if not self.event_time.get("timestamp"):
            return None
        
        now = time.time()
        age_seconds = now - self.event_time["timestamp"]
        return age_seconds / 86400
    
    def is_recent(self, days: int = 7) -> bool:
        """判断是否是近期记忆"""
        age = self.get_age_in_days()
        return age is not None and age <= days


# ================================================================
# 时间信息提取器
# ================================================================

# 星期几：weekday() 0=周一。"日"和"天"都指周日；"1".."7"是"周五=5"这种写法
_WEEK_CHARS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
               "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_NUM_CHARS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10}
_WEEK_RE = re.compile(r"(上+|这|本|下+)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")
_MONTH_DAY_RE = re.compile(r"(上|这|本|下)\s*个?月\s*(\d{1,2})\s*[号日]")
_CLOCK_RE = re.compile(r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
                       r"(\d{1,2}|[一二两三四五六七八九十]+)\s*点(半)?")
_PERIOD_RE = re.compile(r"凌晨|早上|上午|中午|下午|傍晚|晚上")
_MONTH_SHIFT = {"上": -1, "这": 0, "本": 0, "下": 1}
# "每周三体检"是重复发生，不落在某一个日子；硬折成日期就会到点说错话
_RECURRING_WORDS = ("每天", "每周", "每月", "每年", "每逢")


def _cn_int(text: str) -> Optional[int]:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        return (10 if not head else _NUM_CHARS.get(head, 0) * 10) + (
            _NUM_CHARS.get(tail, 0) if tail else 0)
    return _NUM_CHARS.get(text)


class TemporalExtractor:
    """时间信息提取器（修复季节映射）"""
    
    # 复合时间表达（优先匹配）- 修复：统一使用"夏天"
    COMPOUND_TIME_MAP = {
        "去年夏天": (-365, "夏天"),
        "去年冬天": (-365, "冬天"),
        "今年夏天": (0, "夏天"),
        "今年冬天": (0, "冬天"),
        "上个暑假": (-365, "暑假"),
        "这个暑假": (0, "暑假"),
    }
    
    # 简单相对时间
    RELATIVE_TIME_MAP = {
        "今天": 0,
        "昨天": -1,
        "前天": -2,
        "明天": 1,
        "后天": 2,
        "上周": -7,
        "这周": 0,
        "下周": 7,
        "上个月": -30,
        "这个月": 0,
        "下个月": 30,
        "去年": -365,
        "今年": 0,
        "明年": 365,
    }

    # 光秃秃的相对词（标题要去掉它们，"明天复诊"不该留成标题）
    _WORDS_RE = re.compile("|".join(sorted(RELATIVE_TIME_MAP, key=len, reverse=True)))
    
    # 季节映射 - 修复：同时包含"夏天"和"夏季"、"冬天"和"冬季"
    SEASON_MAP = {
        "春天": (3, 5),
        "夏季": (6, 8),
        "夏天": (6, 8),  # 新增
        "秋天": (9, 11),
        "冬季": (12, 2),
        "冬天": (12, 2),  # 新增
        "暑假": (7, 8),
        "寒假": (1, 2),
    }
    
    @classmethod
    def _named_weekday(cls, text: str, ref_dt: datetime):
        """下周三 / 这周五 / 周日 → 真的那个星期三（(日期, 说法)），认不出返回 None。

        词表里"下周"只会 +7 天，于是"下周三"被记成下周一——差两天，
        moz 会在错的日子当面说错话，所以这条要单独算。
        """
        m = _WEEK_RE.search(text)
        if not m:
            return None
        weekday = _WEEK_CHARS.get(m.group(2))
        if weekday is None:
            return None
        prefix = m.group(1) or ""
        if prefix.startswith("上"):
            weeks = -len(prefix)
        elif prefix.startswith("下"):
            weeks = len(prefix)
        else:
            weeks = 0
        monday = ref_dt.date() - timedelta(days=ref_dt.weekday())
        day = monday + timedelta(weeks=weeks, days=weekday)
        if not prefix and day < ref_dt.date():
            day += timedelta(days=7)   # 光说"周五"指的是下一个周五
        return datetime(day.year, day.month, day.day), m.group(0)

    @classmethod
    def _month_day(cls, text: str, ref_dt: datetime):
        """下个月3号 → 真的是下个月 3 号，不是"今天+30 天"。"""
        m = _MONTH_DAY_RE.search(text)
        if not m:
            return None
        day = int(m.group(2))
        year, month = ref_dt.year, ref_dt.month + _MONTH_SHIFT[m.group(1)]
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        # datetime(2027,2,29) 会自己滚成 3 月 1 日，所以要自己核对天数，别把没有的一天记成有
        if day > calendar.monthrange(year, month)[1]:
            return None
        return datetime(year, month, day), m.group(0)

    @classmethod
    def _clock(cls, text: str):
        """句子里给了钟点就用人家的，别拿"用户敲字那一刻"当事件时间。"""
        m = _CLOCK_RE.search(text)
        if not m:
            return None
        hour = _cn_int(m.group(2))
        if hour is None or not 0 <= hour <= 24:
            return None
        period = m.group(1) or ""
        if period in ("下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        return hour, 30 if m.group(3) else 0, m.group(0)

    @classmethod
    def strip_phrases(cls, text: str) -> str:
        """把时间说法挖掉，只留事本身——标题不该是"我下周三要去体检"这种半句话。"""
        out = _WEEK_RE.sub(" ", text or "")
        out = _MONTH_DAY_RE.sub(" ", out)
        out = _CLOCK_RE.sub(" ", out)
        out = _PERIOD_RE.sub(" ", out)
        return cls._WORDS_RE.sub(" ", out)

    @classmethod
    def extract_from_text(cls, text: str, reference_time: Optional[float] = None) -> TemporalMetadata:
        """从文本中提取时间信息（修复复合时间表达覆盖问题）"""
        ref_time = reference_time or time.time()
        ref_dt = datetime.fromtimestamp(ref_time)
        
        metadata = TemporalMetadata()
        
        # 1. 优先匹配复合时间表达（如"去年夏天"）
        for compound, (days_offset, season) in cls.COMPOUND_TIME_MAP.items():
            if compound in text:
                target_year = ref_dt.year + (days_offset // 365)
                season_months = cls.SEASON_MAP[season]
                target_dt = datetime(target_year, season_months[0], 1)
                
                metadata.event_time = {
                    "timestamp": target_dt.timestamp(),
                    "description": compound,
                    "precision": "relative",
                }
                metadata.time_context["season"] = season
                return metadata  # 匹配到复合表达后直接返回
        
        # 2. 点名了星期几或几号的先算（"下周三""下个月3号"），必须走在词表前面：
        #    词表只会 +7/+30 天，把"下周三"记成下周一、"下个月3号"记成本月十几号
        month_day_said = bool(_MONTH_DAY_RE.search(text))
        if not any(w in text for w in _RECURRING_WORDS):
            named = cls._named_weekday(text, ref_dt) or cls._month_day(text, ref_dt)
            if named:
                day, phrase = named
                clock = cls._clock(text)
                target_dt = day.replace(hour=clock[0] if clock else 9,
                                        minute=clock[1] if clock else 0)
                metadata.event_time = {
                    "timestamp": target_dt.timestamp(),
                    "description": phrase + ((" " + clock[2]) if clock else ""),
                    "precision": "relative",
                }
        
        # 3. 匹配简单相对时间。说了"下个月31号"这种根本没有的一天时，别再退回词表
        #    拿"今天+30天"糊一个日子出来——宁可这条不记。
        if not metadata.event_time and not month_day_said:
            for keyword, days_offset in cls.RELATIVE_TIME_MAP.items():
                if keyword in text:
                    clock = cls._clock(text)
                    target_dt = ref_dt + timedelta(days=days_offset)
                    if clock:
                        target_dt = target_dt.replace(hour=clock[0], minute=clock[1])
                    elif days_offset >= 0:
                        # "明天/后天"别带上用户敲字那一刻的钟点：那只是他打字的时刻
                        target_dt = target_dt.replace(hour=9, minute=0)
                    metadata.event_time = {
                        "timestamp": target_dt.timestamp(),
                        "description": keyword + ((" " + clock[2]) if clock else ""),
                        "precision": "relative",
                    }
                    break
        
        # 4. 匹配季节（如果前面没有匹配到复合表达）
        if not metadata.event_time:
            for season, (start_month, end_month) in cls.SEASON_MAP.items():
                if season in text:
                    metadata.time_context["season"] = season
                    year = ref_dt.year
                    if start_month > ref_dt.month:
                        year -= 1
                    metadata.event_time["timestamp"] = datetime(year, start_month, 1).timestamp()
                    metadata.event_time["precision"] = "approximate"
                    break
        
        # 5. 提取生活阶段
        life_stages = ["小时候", "童年", "学生时代", "大学时期", "工作后"]
        for stage in life_stages:
            if stage in text:
                metadata.time_context["life_stage"] = stage
                break
        
        # 6. 提取重复性
        recurrence_keywords = ["每天", "每周", "每月", "每年", "经常", "总是"]
        for kw in recurrence_keywords:
            if kw in text:
                metadata.recurrence["is_recurring"] = True
                if "每天" in text:
                    metadata.recurrence["frequency"] = "daily"
                elif "每周" in text:
                    metadata.recurrence["frequency"] = "weekly"
                elif "每月" in text:
                    metadata.recurrence["frequency"] = "monthly"
                elif "每年" in text:
                    metadata.recurrence["frequency"] = "yearly"
                else:
                    metadata.recurrence["frequency"] = "irregular"
                break
        
        return metadata

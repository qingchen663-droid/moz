"""Memory governance primitives for grading, provenance and lifecycle."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Dict, Optional


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp a numeric value and coerce obvious bad input to the safe range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:  # NaN
        return low
    return max(low, min(high, number))


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MemoryGrade(IntEnum):
    EPHEMERAL = 0
    TRACE = 1
    REGULAR = 2
    IMPORTANT = 3
    CORE = 4

    @classmethod
    def from_score(cls, score: float) -> "MemoryGrade":
        value = clamp(score)
        if value < 0.18:
            return cls.EPHEMERAL
        if value < 0.38:
            return cls.TRACE
        if value < 0.60:
            return cls.REGULAR
        if value < 0.80:
            return cls.IMPORTANT
        return cls.CORE

    @property
    def legacy_layer(self) -> int:
        if self >= MemoryGrade.CORE:
            return 1
        if self >= MemoryGrade.IMPORTANT:
            return 2
        return 3


GRADE_PROMOTION_HYSTERESIS = float(os.environ.get("MOZ_GRADE_PROMOTION_HYSTERESIS", "0.03"))
GRADE_DEMOTION_OBSERVATIONS = int(os.environ.get("MOZ_GRADE_DEMOTION_OBSERVATIONS", "2"))
MENTION_MIN_GAP_SECONDS = int(os.environ.get("MOZ_GRADE_MENTION_MIN_GAP_MINUTES", "30")) * 60

_THRESHOLD_NAMES = {
    1: "MOZ_GRADE_G1_THRESHOLD",
    2: "MOZ_GRADE_G2_THRESHOLD",
    3: "MOZ_GRADE_G3_THRESHOLD",
    4: "MOZ_GRADE_G4_THRESHOLD",
}


def grade_threshold(grade: MemoryGrade) -> float:
    default = {1: 0.18, 2: 0.38, 3: 0.60, 4: 0.80}[int(grade)]
    return clamp(os.environ.get(_THRESHOLD_NAMES[int(grade)], str(default)))


def normalized_content(content: str) -> str:
    """Normalize punctuation, width, case and common extraction prefixes."""
    value = unicodedata.normalize("NFKC", content or "").strip().lower()
    value = re.sub(r"^\[(?:关于用户|用户情感状态|AI互动|对话摘要|AI回复要点)\]\s*", "", value)
    value = re.sub(r"^用户(?:说|提到|表示)[：:]?\s*", "", value)
    value = re.sub(r"[\s，。！？；：、“”‘’,.!?;:'\"()（）\[\]{}<>《》-]+", "", value)
    return value


_GOAL_WORDS = (
    "面试", "考试", "计划", "目标", "准备", "申请", "搬家", "离职", "入职", "截止",
)
_RELATIONSHIP_WORDS = (
    "妈妈", "爸爸", "父母", "家人", "朋友", "同事", "同学", "女朋友", "男朋友",
    "伴侣", "恋人", "老师", "导师", "孩子",
)


@dataclass(frozen=True)
class DegreeFactors:
    impact: float
    emotion_intensity: float
    explicitness: float
    goal_impact: float
    relationship_relevance: float
    recurrence_hint: float

    def score(self) -> float:
        return clamp(
            self.impact * 0.30
            + self.emotion_intensity * 0.20
            + self.explicitness * 0.18
            + self.goal_impact * 0.14
            + self.relationship_relevance * 0.10
            + self.recurrence_hint * 0.08
        )


class DegreeScorer:
    """Calculate the initial persistence degree for one candidate memory."""

    @classmethod
    def calculate(
        cls,
        *,
        content: str,
        category_value: str,
        emotion_intensity: float,
        confidence: float,
        temporal_data: Optional[Dict[str, Any]] = None,
        source_type: str = "chat",
    ) -> tuple[DegreeFactors, float]:
        text = content or ""
        lowered = text.lower()

        category_weight = {
            "fact": 0.55,
            "preference": 0.62,
            "event": 0.68,
            "relationship": 0.72,
            "goal": 0.76,
            "concern": 0.70,
            "emotion": 0.52,
            "semantic": 0.64,
            "summary": 0.35,
        }.get(category_value, 0.45)

        high_impact_words = ("永远", "核心", "很重要", "重要", "边界", "不要", "害怕", "喜欢")
        impact = category_weight + sum(0.06 for word in high_impact_words if word in lowered)
        impact = clamp(impact)

        emotion = clamp(emotion_intensity)
        if source_type == "user_edit":
            explicitness = 1.0
        elif source_type == "reflection":
            explicitness = 0.65
        else:
            explicitness = clamp(confidence)

        goal_impact = 0.72 if category_value in {"goal", "concern"} else 0.0
        goal_impact = max(goal_impact, sum(0.28 for word in _GOAL_WORDS if word in lowered))
        goal_impact = clamp(goal_impact)

        relationship_relevance = 0.75 if category_value == "relationship" else 0.0
        relationship_relevance = max(
            relationship_relevance,
            sum(0.24 for word in _RELATIONSHIP_WORDS if word in lowered),
        )
        relationship_relevance = clamp(relationship_relevance)

        temporal = temporal_data or {}
        recurrence = 0.8 if temporal.get("recurrence", {}).get("is_recurring") else 0.0
        if any(word in lowered for word in ("每天", "每周", "每月", "每年", "总是", "经常")):
            recurrence = max(recurrence, 0.7)
        recurrence = clamp(recurrence)

        factors = DegreeFactors(
            impact=impact,
            emotion_intensity=emotion,
            explicitness=explicitness,
            goal_impact=goal_impact,
            relationship_relevance=relationship_relevance,
            recurrence_hint=recurrence,
        )
        return factors, factors.score()


def initial_grade(
    score: float,
    *,
    confidence: float,
    source_type: str,
    category_value: str,
) -> MemoryGrade:
    """Apply conservative caps so inferred chatter cannot become core memory."""
    grade = MemoryGrade.from_score(score)
    confidence = clamp(confidence)

    # Inferred/reflected material needs explicit user confirmation before G3+.
    if source_type in {"reflection", "system", "import"} and grade > MemoryGrade.REGULAR:
        grade = MemoryGrade.REGULAR

    if source_type != "user_edit" and grade == MemoryGrade.CORE:
        stable_identity = category_value in {"fact", "preference", "relationship"}
        if not (stable_identity and confidence >= 0.82):
            grade = MemoryGrade.IMPORTANT

    return grade


def frequency_score(
    *,
    independent_conversation_count: int,
    mentions_last_30d: int,
    distinct_mention_days: int,
) -> float:
    return clamp(
        min(1.0, independent_conversation_count / 4) * 0.45
        + min(1.0, mentions_last_30d / 6) * 0.35
        + min(1.0, distinct_mention_days / 5) * 0.20
    )


@dataclass(frozen=True)
class GradeDecision:
    old_grade: MemoryGrade
    new_grade: MemoryGrade
    changed: bool
    reason: str


def decide_grade(
    *,
    old_grade: MemoryGrade,
    new_degree_score: float,
    confidence: float,
    independent_conversation_count: int,
    event_type: str,
    locked: bool,
    promotion_streak: int,
    demotion_streak: int,
) -> GradeDecision:
    """Resolve an observed score into a conservative grade transition."""
    new_degree_score = clamp(new_degree_score)
    confidence = clamp(confidence)
    target = MemoryGrade.from_score(new_degree_score)
    hysteresis = GRADE_PROMOTION_HYSTERESIS
    required_streak = max(1, GRADE_DEMOTION_OBSERVATIONS)

    # A user's explicit statement is the strongest normal signal.
    if event_type in {"manual", "user_confirmed"}:
        allowed = MemoryGrade.CORE if confidence >= 0.80 else MemoryGrade.IMPORTANT
        target = min(target, allowed) if target >= MemoryGrade.IMPORTANT else target
        if target >= MemoryGrade.IMPORTANT and new_degree_score >= grade_threshold(MemoryGrade.IMPORTANT):
            target = MemoryGrade.CORE if confidence >= 0.80 and new_degree_score >= grade_threshold(MemoryGrade.CORE) else MemoryGrade.IMPORTANT
        return GradeDecision(old_grade, target, target != old_grade, f"explicit_{event_type}")

    # Ordinary observations cannot invent a core memory.
    if target > MemoryGrade.IMPORTANT:
        target = MemoryGrade.IMPORTANT

    # Inferred material remains capped until explicitly confirmed elsewhere.
    if event_type in {"reflection"} and target > MemoryGrade.REGULAR:
        target = MemoryGrade.REGULAR

    if target > old_grade:
        promoted = False
        if new_degree_score >= grade_threshold(target) + hysteresis:
            if target <= MemoryGrade.REGULAR:
                promoted = independent_conversation_count >= 2
            elif target == MemoryGrade.IMPORTANT:
                promoted = independent_conversation_count >= 2 and confidence >= 0.70
        reason = "promoted_by_observations" if promoted else "promotion_deferred"
        return GradeDecision(old_grade, target if promoted else old_grade, promoted, reason)

    if target < old_grade:
        if locked:
            return GradeDecision(old_grade, old_grade, False, "locked_core_retained")
        if demotion_streak + 1 >= required_streak or event_type in {"user_denied", "outdated"}:
            return GradeDecision(old_grade, target, True, "demoted_by_observations")
        return GradeDecision(old_grade, old_grade, False, "demotion_deferred")

    promotion_streak = promotion_streak
    demotion_streak = demotion_streak
    return GradeDecision(old_grade, old_grade, False, "grade_retained")


VALID_EVENT_TYPES = {
    "created",
    "mentioned",
    "recalled_useful",
    "recalled_irrelevant",
    "user_confirmed",
    "user_denied",
    "conflict_resolved",
    "reflection",
    "manual",
}

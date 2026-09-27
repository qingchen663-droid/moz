"""
情感记忆管理器 - 高级记忆系统

功能：
1. 情感标签系统：自动识别和标记记忆的情感属性
2. 记忆重要性评分：基于多维度评估记忆的重要性
3. 遗忘曲线机制：基于艾宾浩斯遗忘曲线的记忆衰减与巩固
4. 记忆合并与清理：自动合并相似记忆，清理低价值记忆
5. 事实提取与存储：从对话中提取关键事实并存储为记忆

设计目标：
- 完整的本地记忆系统，无需外部依赖
- 模块化设计，各组件可独立使用
"""

import os
import json
import time
import math
import hashlib
import logging
import sqlite3
import threading
import re
from functools import wraps
import numpy as np
from typing import List, Dict, Optional, Tuple
from enum import Enum
from dataclasses import dataclass, field
from dotenv import load_dotenv

from memory_governance import (
    DegreeScorer,
    MENTION_MIN_GAP_SECONDS,
    MemoryGrade,
    MemoryStatus,
    clamp,
    decide_grade,
    frequency_score,
    grade_threshold,
    initial_grade,
    normalized_content,
)

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)


# ================================================================
# 枚举定义
# ================================================================

class EmotionType(Enum):
    """情感标签枚举。"""
    HAPPY = "happy"
    SAD = "sad"
    ANXIOUS = "anxious"
    ANGRY = "angry"
    NEUTRAL = "neutral"
    EXCITED = "excited"
    FEARFUL = "fearful"
    GRATEFUL = "grateful"
    LONELY = "lonely"
    HOPEFUL = "hopeful"
    STRESSED = "stressed"
    RELIEVED = "relieved"

    @classmethod
    def from_string(cls, emotion: str) -> "EmotionType":
        mapping = {
            # 中文映射
            "开心": cls.HAPPY, "快乐": cls.HAPPY, "高兴": cls.HAPPY, "愉快": cls.HAPPY, "兴奋": cls.EXCITED, "激动": cls.EXCITED,
            "难过": cls.SAD, "悲伤": cls.SAD, "伤心": cls.SAD, "失落": cls.SAD, "沮丧": cls.SAD, "孤独": cls.LONELY,
            "焦虑": cls.ANXIOUS, "紧张": cls.ANXIOUS, "压力": cls.STRESSED, "担忧": cls.ANXIOUS, "害怕": cls.FEARFUL,
            "生气": cls.ANGRY, "愤怒": cls.ANGRY, "烦躁": cls.ANGRY,
            "感恩": cls.GRATEFUL, "感谢": cls.GRATEFUL, "谢谢": cls.GRATEFUL,
            "希望": cls.HOPEFUL, "期待": cls.HOPEFUL, "放心": cls.RELIEVED,
            "平静": cls.NEUTRAL, "一般": cls.NEUTRAL, "没事": cls.NEUTRAL,
            # 英文映射（情感分析 Agent 返回英文标签）
            "happy": cls.HAPPY, "sad": cls.SAD, "anxious": cls.ANXIOUS, "angry": cls.ANGRY,
            "neutral": cls.NEUTRAL, "excited": cls.EXCITED, "fearful": cls.FEARFUL,
            "grateful": cls.GRATEFUL, "lonely": cls.LONELY, "hopeful": cls.HOPEFUL,
            "stressed": cls.STRESSED, "relieved": cls.RELIEVED,
        }
        return mapping.get(emotion, cls.NEUTRAL)

    def to_emoji(self) -> str:
        emoji_map = {
            EmotionType.HAPPY: "😊", EmotionType.SAD: "😢", EmotionType.ANXIOUS: "😰",
            EmotionType.ANGRY: "😠", EmotionType.NEUTRAL: "😐", EmotionType.EXCITED: "🤩",
            EmotionType.FEARFUL: "😨", EmotionType.GRATEFUL: "🙏", EmotionType.LONELY: "😔",
            EmotionType.HOPEFUL: "🌟", EmotionType.STRESSED: "😫", EmotionType.RELIEVED: "😌",
        }
        return emoji_map.get(self, "😐")


class MemoryCategory(Enum):
    """记忆分类。"""
    EMOTION = "emotion"       # 情感状态/事件
    FACT = "fact"             # 事实信息（职业、喜好等）
    RELATIONSHIP = "relationship"  # 人际关系
    EVENT = "event"           # 重要事件
    PREFERENCE = "preference"     # 个人偏好
    GOAL = "goal"             # 目标/愿望
    CONCERN = "concern"       # 担忧/困扰


# ================================================================
# 数据模型
# ================================================================

@dataclass
class MemoryItem:
    """A persistent memory with governance, provenance and lifecycle fields."""

    id: str
    content: str
    emotion: EmotionType = EmotionType.NEUTRAL
    category: MemoryCategory = MemoryCategory.FACT
    importance: float = 0.5
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = 0.0
    emotion_intensity: float = 0.5
    tags: List[str] = field(default_factory=list)
    is_consolidated: bool = False
    embedding: Optional[List[float]] = None

    # v2 compatibility.
    layer: int = 3
    temporal_data: Dict = field(default_factory=dict)

    # v3 governance and provenance.
    status: MemoryStatus = MemoryStatus.ACTIVE
    confidence: float = 0.70
    source_type: str = "chat"
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    extractor_version: str = "memory-governance-v1"
    supersedes_id: Optional[str] = None

    grade: MemoryGrade = MemoryGrade.REGULAR
    degree_score: float = 0.40
    base_degree_score: float = 0.40
    locked: bool = False

    mention_count: int = 0
    independent_conversation_count: int = 0
    last_mentioned_at: float = 0.0
    mention_events: List[Dict] = field(default_factory=list)

    useful_count: int = 0
    harmful_count: int = 0
    promotion_streak: int = 0
    demotion_streak: int = 0
    last_regraded_at: float = 0.0
    regrade_reason: str = "created"

    version: int = 1
    updated_at: float = field(default_factory=time.time)

    def active(self) -> bool:
        return self.status in {MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE}

    def public_dict(self) -> Dict:
        data = self.to_dict()
        data.pop("embedding", None)
        return data

    def to_dict(self) -> Dict:
        d = {
            "id": self.id,
            "content": self.content,
            "emotion": self.emotion.value,
            "category": self.category.value,
            "importance": clamp(self.importance),
            "access_count": self.access_count,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "emotion_intensity": clamp(self.emotion_intensity),
            "tags": self.tags,
            "is_consolidated": self.is_consolidated,
            "layer": self.layer,
            "temporal_data": self.temporal_data,
            "status": self.status.value,
            "confidence": clamp(self.confidence),
            "source_type": self.source_type,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "extractor_version": self.extractor_version,
            "supersedes_id": self.supersedes_id,
            "grade": int(self.grade),
            "degree_score": clamp(self.degree_score),
            "base_degree_score": clamp(self.base_degree_score),
            "locked": self.locked,
            "mention_count": self.mention_count,
            "independent_conversation_count": self.independent_conversation_count,
            "last_mentioned_at": self.last_mentioned_at,
            "mention_events": self.mention_events[-100:],
            "useful_count": self.useful_count,
            "harmful_count": self.harmful_count,
            "promotion_streak": self.promotion_streak,
            "demotion_streak": self.demotion_streak,
            "last_regraded_at": self.last_regraded_at,
            "regrade_reason": self.regrade_reason,
            "version": self.version,
            "updated_at": self.updated_at,
        }
        if self.embedding is not None:
            d["embedding"] = self.embedding
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "MemoryItem":
        data = dict(data)
        data["emotion"] = EmotionType(data.get("emotion", EmotionType.NEUTRAL.value))
        data["category"] = MemoryCategory(data.get("category", MemoryCategory.FACT.value))
        try:
            data["status"] = MemoryStatus(data.get("status", MemoryStatus.ACTIVE.value))
        except ValueError:
            data["status"] = MemoryStatus.ARCHIVED
        data["grade"] = MemoryGrade(int(data.get("grade", MemoryGrade.REGULAR.value)))
        if "embedding" not in data:
            data["embedding"] = None
        if "layer" not in data:
            data["layer"] = 3
        if "temporal_data" not in data:
            data["temporal_data"] = {}
        return cls(**data)

# ================================================================
# 遗忘曲线
# ================================================================

class EbbinghausCurve:
    """
    艾宾浩斯遗忘曲线实现。
    
    遗忘率公式: R = e^(-t/S)
    R: 记忆保留率
    t: 经过时间（小时）
    S: 记忆强度系数（与重要性和复习次数相关）
    """

    @staticmethod
    def retention_rate(created_at: float, last_accessed: float, importance: float, access_count: int) -> float:
        """
        计算当前记忆保留率。
        
        参数：
        - created_at: 记忆创建时间戳
        - last_accessed: 最后访问时间戳
        - importance: 重要性评分
        - access_count: 访问次数
        
        返回：
        - 记忆保留率 (0-1)
        """
        now = time.time()
        elapsed_hours = (now - (last_accessed or created_at)) / 3600

        # 记忆强度系数：基础强度 + 重要性加成 + 复习次数加成
        # 复习次数越多，遗忘越慢（间隔重复效应）
        base_strength = 0.3
        importance_factor = importance * 0.5
        review_factor = min(access_count * 0.15, 1.0)
        strength = base_strength + importance_factor + review_factor

        # 艾宾浩斯遗忘曲线
        retention = math.exp(-elapsed_hours / (strength * 24 + 1))
        return max(0.0, min(1.0, retention))

    @staticmethod
    def should_consolidate(memory: MemoryItem, threshold: float = 0.3) -> bool:
        """
        判断是否需要巩固（重新复习）该记忆。
        
        当记忆保留率低于阈值且不是已巩固状态时，需要巩固。
        """
        if memory.is_consolidated:
            return False
        retention = EbbinghausCurve.retention_rate(
            memory.created_at, memory.last_accessed,
            memory.importance, memory.access_count
        )
        return retention < threshold

    @staticmethod
    def decay_importance(memory: MemoryItem) -> float:
        """
        根据遗忘曲线衰减记忆的重要性。
        
        被频繁访问的记忆重要性会提升，长期未被访问的记忆重要性会下降。
        """
        retention = EbbinghausCurve.retention_rate(
            memory.created_at, memory.last_accessed,
            memory.importance, memory.access_count
        )
        # 新的重要性 = 原重要性 * 保留率 + 基础值
        new_importance = memory.importance * retention * 0.8 + 0.1
        return max(0.0, min(1.0, new_importance))


# ================================================================
# 语义向量服务
# ================================================================

class EmbeddingService:
    """
    语义向量服务：调用智谱 Embedding API 生成文本向量。
    
    用于记忆的语义检索，替代纯关键词匹配。
    API 不可用时自动降级到关键词匹配。
    """

    _instance = None
    _client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._client is not None:
            return
        try:
            from model_config import EMBED_MODEL, EMBED_BASE_URL, EMBED_PROVIDER, resolve_api_key
            api_key = resolve_api_key(EMBED_PROVIDER)
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key, base_url=EMBED_BASE_URL.rsplit("/", 1)[0])
            self._model = EMBED_MODEL
            logger.info(f"[EmbeddingService] 初始化成功, model={EMBED_MODEL}")
        except Exception as e:
            logger.warning(f"[EmbeddingService] 初始化失败: {e}, 将降级到关键词匹配")
            self._client = None

    def get_embedding(self, text: str) -> Optional[List[float]]:
        """获取文本的语义向量。"""
        if self._client is None:
            return None
        try:
            resp = self._client.embeddings.create(
                model=self._model,
                input=text,
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.warning(f"[EmbeddingService] 获取向量失败: {e}")
            return None

    def get_embeddings_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """批量获取文本的语义向量。"""
        if self._client is None:
            return [None] * len(texts)
        try:
            resp = self._client.embeddings.create(
                model=self._model,
                input=texts,
            )
            sorted_data = sorted(resp.data, key=lambda x: x.index)
            return [d.embedding for d in sorted_data]
        except Exception as e:
            logger.warning(f"[EmbeddingService] 批量获取向量失败: {e}")
            return [None] * len(texts)

    @staticmethod
    def cosine_similarity(a: List[float], b: List[float]) -> float:
        """计算两个向量的余弦相似度。"""
        va = np.array(a)
        vb = np.array(b)
        norm_a = np.linalg.norm(va)
        norm_b = np.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))


# ================================================================
# 情感分析器（基于规则的轻量级实现）
# ================================================================

class EmotionAnalyzer:
    """
    基于关键词的情感分析器。
    
    用于从对话文本中识别情感标签和情感强度。
    生产环境可以替换为 LLM 调用或专门的 NLP 模型。
    """

    EMOTION_KEYWORDS = {
        EmotionType.HAPPY: [
            "开心", "快乐", "高兴", "愉快", "幸福", "满足", "太好了", "棒", "爽",
            "笑", "好玩", "有趣", "惊喜", "顺利", "成功",
        ],
        EmotionType.SAD: [
            "难过", "悲伤", "伤心", "失落", "沮丧", "哭", "痛苦", "绝望",
            "无奈", "失望", "心碎", "眼泪", "不开心", "郁闷", "低落",
            "失败", "挫败", "遗憾", "错过", "被拒", "碰壁", "落空",
            "没戏", "泡汤", "完了", "凉了",
        ],
        EmotionType.ANXIOUS: [
            "焦虑", "紧张", "担心", "害怕", "不安", "惶恐", "压力", "喘不过气",
            "忐忑", "不安", "忧虑", "发愁", "纠结",
        ],
        EmotionType.ANGRY: [
            "生气", "愤怒", "恼火", "烦躁", "烦", "讨厌", "恨", "不爽",
            " rage", "发火", "暴躁",
        ],
        EmotionType.EXCITED: [
            "兴奋", "激动", "迫不及待", "振奋", "狂热",
        ],
        EmotionType.FEARFUL: [
            "害怕", "恐惧", "恐慌", "吓死", "不敢", "畏惧", "胆怯",
        ],
        EmotionType.GRATEFUL: [
            "感谢", "谢谢", "感恩", "感激", "多亏", "幸好",
        ],
        EmotionType.LONELY: [
            "孤独", "寂寞", "孤单", "一个人", "没人陪", "冷落",
        ],
        EmotionType.HOPEFUL: [
            "希望", "相信", "会好的", "未来",
        ],
        EmotionType.STRESSED: [
            "压力", "累", "疲惫", "受不了", "崩溃", "撑不住", "喘不过气",
            "加班", "考试", " deadline",
        ],
        EmotionType.RELIEVED: [
            "放心", "安心", "松了一口气", "还好", "总算", "解脱",
        ],
    }

    # 语境翻转规则：当正向词与负面语境共现时，翻转为 SAD
    # 格式：(正向关键词, 负面语境词列表, 翻转目标情感)
    CONTEXT_FLIPS = [
        ("喜欢", ["没牵", "没拉", "没在一起", "不喜欢我", "拒绝", "没结果", "没回应", "单相思", "暗恋", "手都没牵"], EmotionType.SAD),
        ("爱", ["不爱", "分手", "离开", "拒绝", "单相思", "没结果"], EmotionType.SAD),
        ("努力", ["失败", "没用", "白费", "不行", "被拒", "碰壁", "落空", "没结果", "找不到", "还是没"], EmotionType.SAD),
        ("期待", ["落空", "失望", "没实现", "泡汤", "没了"], EmotionType.SAD),
        ("希望", ["破灭", "没了", "失望", "落空"], EmotionType.SAD),
        ("成功", ["没成功", "不成功", "失败"], EmotionType.SAD),
    ]

    INTENSIFIERS = ["非常", "特别", "超级", "极其", "万分", "太", "真的很", "特别", "格外", "十分"]
    NEGATORS = ["不", "没", "别", "别", "别要", "没有", "并非", "并不"]

    @classmethod
    def analyze(cls, text: str) -> Tuple[EmotionType, float]:
        """
        分析文本情感，返回情感标签和强度。
        
        参数：
        - text: 待分析的文本
        
        返回：
        - (情感标签, 情感强度 0-1)
        """
        # 第一步：检查语境翻转规则（优先级最高）
        for positive_kw, negative_contexts, flip_emotion in cls.CONTEXT_FLIPS:
            if positive_kw in text:
                for ctx in negative_contexts:
                    if ctx in text:
                        # 正向词 + 负面语境 → 翻转
                        return flip_emotion, 0.6

        # 第二步：常规关键词匹配
        scores = {}
        for emotion, keywords in cls.EMOTION_KEYWORDS.items():
            score = 0.0
            for kw in keywords:
                if kw in text:
                    # 基础分
                    score += 0.3
                    # 检查是否有程度副词
                    for intensifier in cls.INTENSIFIERS:
                        if intensifier + kw in text or intensifier in text:
                            score += 0.2
                    # 检查是否有否定词（简单处理）
                    for negator in cls.NEGATORS:
                        if negator + kw in text:
                            score -= 0.2
            scores[emotion] = max(0.0, score)

        # 找到最高分的情感
        if not scores or max(scores.values()) == 0:
            return EmotionType.NEUTRAL, 0.0

        dominant_emotion = max(scores, key=scores.get)
        intensity = min(1.0, scores[dominant_emotion])
        return dominant_emotion, intensity


# ================================================================
# 记忆重要性评估器
# ================================================================

class ImportanceScorer:
    """
    记忆重要性评分器。
    
    评分维度：
    1. 情感强度：情感越强烈，记忆越重要
    2. 信息密度：包含的事实信息越多，越重要
    3. 用户关注度：被检索次数越多，越重要
    4. 时间衰减：新记忆初始分高，老记忆需要维持
    """

    INFO_INDICATORS = [
        "叫", "是", "喜欢", "讨厌", "工作", "住", "在", "有", "毕业于",
        "来自", "年龄", "岁", "职业", "专业", " hobby", "兴趣",
        "家人", "朋友", "同事", "同学",
    ]

    @classmethod
    def calculate(cls, content: str, emotion: EmotionType, emotion_intensity: float, access_count: int = 0) -> float:
        """
        计算记忆重要性评分。
        
        参数：
        - content: 记忆内容
        - emotion: 情感标签
        - emotion_intensity: 情感强度
        - access_count: 被访问次数
        
        返回：
        - 重要性评分 (0-1)
        """
        # 1. 情感强度分（0-0.4）
        emotion_score = emotion_intensity * 0.4

        # 2. 信息密度分（0-0.3）
        info_score = 0.0
        for indicator in cls.INFO_INDICATORS:
            if indicator in content:
                info_score += 0.05
        info_score = min(0.3, info_score)

        # 3. 用户关注度分（0-0.2）
        access_score = min(0.2, access_count * 0.04)

        # 4. 内容长度分（0-0.1）
        length_score = min(0.1, len(content) / 200)

        total = emotion_score + info_score + access_score + length_score
        return max(0.0, min(1.0, round(total, 3)))


# ================================================================
# 查询改写（Query Rewriting）
# ================================================================

QUERY_REWRITE_PROMPT = """你是一个搜索查询改写专家。请将用户的口语化输入改写为2-3个更适合检索记忆的查询。

规则：
1. 保留原始查询的核心意图
2. 将口语化表达转为更正式的描述性语句
3. 从不同角度扩展查询（情感状态、具体事实、相关事件）
4. 每个查询不超过30字
5. 如果原始查询已经很清晰，只需微调

用户输入：{query}

以 JSON 数组格式返回改写后的查询（包含原始意图的改写版本）：
["查询1", "查询2"]"""


def rewrite_query(query: str) -> List[str]:
    """
    使用 LLM 将用户口语化输入改写为多个检索查询。

    LLM 不可用或超时时降级返回原始查询。
    """
    if not query or len(query.strip()) < 2:
        return [query]

    try:
        from llm_config import get_llm_client
        from langchain_core.messages import HumanMessage

        llm = get_llm_client(temperature=0.0, use_thinking=False)
        prompt = QUERY_REWRITE_PROMPT.format(query=query)

        # 5 秒超时，避免 LLM 慢响应阻塞检索
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(llm.invoke, [HumanMessage(content=prompt)])
            response = future.result(timeout=5)

        content = response.content.strip()

        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        queries = json.loads(content)
        if isinstance(queries, list) and len(queries) > 0:
            # 确保原始查询也在列表中
            result = [query] + [q for q in queries if q != query]
            return result[:4]  # 最多 4 个查询（原始 + 3 个改写）
    except concurrent.futures.TimeoutError:
        logger.warning("[查询改写] LLM 超时(5s)，使用原始查询")
    except Exception as e:
        logger.warning(f"[查询改写] 失败，使用原始查询: {e}")

    return [query]


# ================================================================
# 记忆管理器（核心）
# ================================================================

class MemoryManager:
    """
    高级记忆管理器。
    
    功能：
    - 记忆的创建、检索、更新、删除
    - 情感标签自动标注
    - 重要性评分
    - 遗忘曲线管理
    - 记忆合并与清理
    - 持久化存储
    """

    def _synchronized(func):
        """Serialize access to the in-memory index and dirty set."""
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            with self._state_lock:
                return func(self, *args, **kwargs)
        return wrapper

    def __init__(self, storage_path: str = "./memory_store", db_path: Optional[str] = None):
        self.storage_path = storage_path
        self.db_path = (
            os.path.abspath(db_path)
            if db_path
            else os.path.join(os.path.abspath(os.path.dirname(__file__)), "moz.db")
        )
        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self.emotion_analyzer = EmotionAnalyzer()
        self.importance_scorer = ImportanceScorer()
        self.embedding_service = EmbeddingService()
        self.memories: Dict[str, Dict[str, MemoryItem]] = {}
        self._dirty: set = set()
        # Search caches are process-local: persisted memories remain the source of truth.
        self._search_index_cache: Dict[str, Dict] = {}
        self._query_embedding_cache: Dict[str, Tuple[float, List[float]]] = {}
        self._query_embedding_cache_ttl = float(os.environ.get("MOZ_QUERY_EMBED_CACHE_TTL", "60"))
        self._query_embedding_cache_max = int(os.environ.get("MOZ_QUERY_EMBED_CACHE_SIZE", "128"))
        self._search_metrics = {
            "searches_total": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "stage_ms": {
                "embedding": 0.0,
                "semantic": 0.0,
                "keyword": 0.0,
                "rerank": 0.0,
                "total": 0.0,
            },
        }
        self._init_db()
        self._load_from_disk()

    def _get_user_memories(self, user_id: str) -> Dict[str, MemoryItem]:
        if user_id not in self.memories:
            self.memories[user_id] = {}
        return self.memories[user_id]

    def _ensure_search_state(self) -> None:
        """Initialize search-only state for lightweight test/factory instances."""
        if not hasattr(self, "_search_index_cache"):
            self._search_index_cache = {}
        if not hasattr(self, "_query_embedding_cache"):
            self._query_embedding_cache = {}
        if not hasattr(self, "_query_embedding_cache_ttl"):
            self._query_embedding_cache_ttl = float(os.environ.get("MOZ_QUERY_EMBED_CACHE_TTL", "60"))
        if not hasattr(self, "_query_embedding_cache_max"):
            self._query_embedding_cache_max = int(os.environ.get("MOZ_QUERY_EMBED_CACHE_SIZE", "128"))
        if not hasattr(self, "_search_metrics"):
            self._search_metrics = {
                "searches_total": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "stage_ms": {key: 0.0 for key in ("embedding", "semantic", "keyword", "rerank", "total")},
            }

    def _generate_id(self, content: str, user_id: str) -> str:
        """生成记忆唯一 ID。"""
        raw = f"{user_id}:{content}:{time.time()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @_synchronized
    def add_memory(
        self,
        user_id: str,
        content: str,
        category: MemoryCategory = MemoryCategory.FACT,
        emotion: Optional[EmotionType] = None,
        emotion_intensity: Optional[float] = None,
        confidence: float = 0.70,
        source_type: str = "chat",
        conversation_id: Optional[str] = None,
        message_id: Optional[str] = None,
        supersedes_id: Optional[str] = None,
    ) -> MemoryItem:
        """Add a governed memory, or reinforce an equivalent active memory."""
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content must not be empty")

        confidence = clamp(confidence)
        if emotion is None or emotion_intensity is None:
            auto_emotion, auto_intensity = self.emotion_analyzer.analyze(content)
            emotion = emotion or auto_emotion
            emotion_intensity = auto_intensity if emotion_intensity is None else clamp(emotion_intensity)
        emotion_intensity = clamp(emotion_intensity)

        importance = self.importance_scorer.calculate(content, emotion, emotion_intensity)
        tags = []
        for kw in self.importance_scorer.INFO_INDICATORS:
            if kw in content:
                tags.append(kw)
        for etype, keywords in self.emotion_analyzer.EMOTION_KEYWORDS.items():
            for kw in keywords:
                if kw in content and kw not in tags:
                    tags.append(kw)
                    break
        tags = tags[:5]

        user_memories = self._get_user_memories(user_id)
        normalized = normalized_content(content)

        # Exact normalization catches punctuation/alias-prefix duplicates even
        # when the embedding service is unavailable.
        for memory in user_memories.values():
            if memory.active() and normalized_content(memory.content) == normalized:
                return self.reinforce_memory(
                    user_id=user_id,
                    memory_id=memory.id,
                    event_type="mentioned",
                    conversation_id=conversation_id,
                    message_id=message_id,
                )

        factors, degree_score = DegreeScorer.calculate(
            content=content,
            category_value=category.value,
            emotion_intensity=emotion_intensity,
            confidence=confidence,
            source_type=source_type,
        )
        grade = initial_grade(
            degree_score,
            confidence=confidence,
            source_type=source_type,
            category_value=category.value,
        )

        memory_id = self._generate_id(content, user_id)
        now = time.time()
        memory = MemoryItem(
            id=memory_id,
            content=content,
            emotion=emotion,
            category=category,
            importance=importance,
            emotion_intensity=emotion_intensity,
            last_accessed=now,
            tags=tags,
            status=MemoryStatus.CANDIDATE if confidence < 0.45 else MemoryStatus.ACTIVE,
            confidence=confidence,
            source_type=source_type,
            conversation_id=conversation_id,
            message_id=message_id,
            supersedes_id=supersedes_id,
            grade=grade,
            degree_score=degree_score,
            base_degree_score=degree_score,
            layer=grade.legacy_layer,
            last_regraded_at=now,
            regrade_reason="created",
        )

        embedding = self.embedding_service.get_embedding(content)
        if embedding is not None:
            memory.embedding = embedding
            threshold = float(os.environ.get("MOZ_MEMORY_DEDUPE_SIMILARITY", "0.88"))
            semantic_duplicate = None
            best_similarity = 0.0
            for candidate in user_memories.values():
                if not candidate.active() or candidate.embedding is None:
                    continue
                similarity = EmbeddingService.cosine_similarity(embedding, candidate.embedding)
                if similarity >= threshold and similarity > best_similarity:
                    semantic_duplicate = candidate
                    best_similarity = similarity
            if semantic_duplicate is not None:
                logger.info(
                    "[记忆去重] 合并候选 %s 到 %s (similarity=%.3f)",
                    memory_id, semantic_duplicate.id, best_similarity,
                )
                return self.reinforce_memory(
                    user_id=user_id,
                    memory_id=semantic_duplicate.id,
                    event_type="mentioned",
                    importance_value=max(semantic_duplicate.importance, importance),
                    conversation_id=conversation_id,
                    message_id=message_id,
                )

        user_memories[memory_id] = memory
        self._dirty.add((user_id, memory_id))

        if supersedes_id and supersedes_id in user_memories:
            old = user_memories[supersedes_id]
            old.status = MemoryStatus.SUPERSEDED
            old.updated_at = now
            old.version += 1
            self._dirty.add((user_id, supersedes_id))

        count = len(user_memories)
        if count >= self.CONSOLIDATION_TRIGGER:
            self.auto_maintain(user_id)
        elif count > 50 and count % 10 == 0:
            self.prune_memories(user_id)
        self._invalidate_search_cache(user_id)

        self._save_to_disk()
        self._record_grade_event(
            user_id,
            memory_id,
            "created",
            old_grade=None,
            new_grade=grade,
            old_score=None,
            new_score=degree_score,
            reason=f"impact={factors.impact:.2f},explicit={factors.explicitness:.2f}",
            source_memory_id=supersedes_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        return memory
    @_synchronized
    def _record_grade_event(
        self,
        user_id: str,
        memory_id: str,
        event_type: str,
        *,
        old_grade: Optional[MemoryGrade] = None,
        new_grade: Optional[MemoryGrade] = None,
        old_score: Optional[float] = None,
        new_score: Optional[float] = None,
        observation_value: Optional[float] = None,
        frequency_value: Optional[float] = None,
        importance_value: Optional[float] = None,
        reason: str = "",
        source_memory_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        event_id = hashlib.md5(
            f"{user_id}:{memory_id}:{event_type}:{time.time_ns()}".encode()
        ).hexdigest()[:16]
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO memory_grade_events (
                id, user_id, memory_id, event_type, old_grade, new_grade,
                old_degree_score, new_degree_score, observation_score, frequency_score,
                importance_score, reason, source_memory_id, conversation_id,
                message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, user_id, memory_id, event_type,
                int(old_grade) if old_grade is not None else None,
                int(new_grade) if new_grade is not None else None,
                old_score, new_score, observation_value, frequency_value,
                importance_value,
                reason, source_memory_id, conversation_id, message_id, time.time(),
            ),
        )
        conn.commit()

    @_synchronized
    def get_memory(self, user_id: str, memory_id: str) -> Optional[MemoryItem]:
        return self._get_user_memories(user_id).get(memory_id)

    def _observation_score(self, memory: MemoryItem, frequency_value: float, now: float) -> float:
        last_seen = memory.last_mentioned_at or memory.created_at
        recency = math.exp(-max(0.0, now - last_seen) / (90 * 86400))
        category_impact = {
            MemoryCategory.GOAL: 0.85,
            MemoryCategory.CONCERN: 0.80,
            MemoryCategory.RELATIONSHIP: 0.75,
            MemoryCategory.EVENT: 0.70,
            MemoryCategory.PREFERENCE: 0.65,
            MemoryCategory.FACT: 0.55,
            MemoryCategory.EMOTION: 0.50,
        }.get(memory.category, 0.50)
        return clamp(
            frequency_value * 0.35
            + memory.importance * 0.30
            + recency * 0.15
            + memory.emotion_intensity * 0.10
            + category_impact * 0.10
        )

    @_synchronized
    def reinforce_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        event_type: str = "mentioned",
        conversation_id: Optional[str] = None,
        message_id: Optional[str] = None,
        observation_score: Optional[float] = None,
        record_mention: bool = True,
    ) -> Optional[MemoryItem]:
        """Observe one reuse of a memory and apply conservative regrading."""
        memory = self._get_user_memories(user_id).get(memory_id)
        if memory is None or not memory.active():
            return None

        now = time.time()
        last_mention_conversation = (
            memory.mention_events[-1].get("conversation_id") or ""
            if memory.mention_events
            else memory.conversation_id or ""
        )
        should_count = (
            record_mention
            and (
                memory.mention_count == 0
                or now - memory.last_mentioned_at >= MENTION_MIN_GAP_SECONDS
                or (conversation_id or "") != last_mention_conversation
            )
        )
        if should_count:
            memory.mention_events.append({
                "at": now,
                "conversation_id": conversation_id,
                "message_id": message_id,
            })
            memory.mention_events = memory.mention_events[-100:]
            memory.mention_count += 1
            memory.last_mentioned_at = now

        conversations = {
            memory.conversation_id or "",
        } | {
            event.get("conversation_id") or ""
            for event in memory.mention_events
        }
        memory.independent_conversation_count = len(conversations)
        days = {
            time.strftime("%Y-%m-%d", time.gmtime(event["at"]))
            for event in memory.mention_events
        }
        mentions_30d = sum(1 for event in memory.mention_events if now - event["at"] <= 30 * 86400)
        frequency = frequency_score(
            independent_conversation_count=len(conversations),
            mentions_last_30d=mentions_30d,
            distinct_mention_days=len(days),
        )
        observation = clamp(
            observation_score
            if observation_score is not None
            else self._observation_score(memory, frequency, now)
        )
        old_degree = memory.degree_score
        memory.degree_score = clamp(old_degree * 0.65 + observation * 0.35)
        decision = decide_grade(
            old_grade=memory.grade,
            new_degree_score=memory.degree_score,
            confidence=memory.confidence,
            independent_conversation_count=len(conversations),
            event_type=event_type,
            locked=memory.locked,
            promotion_streak=memory.promotion_streak,
            demotion_streak=memory.demotion_streak,
        )

        old_grade = memory.grade
        memory.grade = decision.new_grade
        memory.layer = decision.new_grade.legacy_layer
        if memory.status == MemoryStatus.CANDIDATE and memory.grade >= MemoryGrade.REGULAR:
            memory.status = MemoryStatus.ACTIVE
        if decision.changed:
            memory.promotion_streak = 1 if decision.new_grade > old_grade else 0
            memory.demotion_streak = 1 if decision.new_grade < old_grade else 0
            memory.last_regraded_at = now
        elif decision.reason == "promotion_deferred":
            memory.promotion_streak += 1
        elif decision.reason == "demotion_deferred":
            memory.demotion_streak += 1
        memory.regrade_reason = decision.reason
        memory.version += 1
        memory.updated_at = now
        self._dirty.add((user_id, memory_id))
        self._save_to_disk()
        self._record_grade_event(
            user_id,
            memory_id,
            event_type,
            old_grade=old_grade,
            new_grade=decision.new_grade,
            old_score=old_degree,
            new_score=memory.degree_score,
            observation_value=observation,
            frequency_value=frequency,
            importance_value=memory.importance,
            reason=decision.reason,
            source_memory_id=memory.supersedes_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        return memory

    @_synchronized
    def correct_memory(
        self,
        user_id: str,
        memory_id: str,
        corrected_content: str,
        category: Optional[MemoryCategory] = None,
        emotion: Optional[EmotionType] = None,
        emotion_intensity: Optional[float] = None,
    ) -> MemoryItem:
        old = self._get_user_memories(user_id).get(memory_id)
        if old is None or not old.active():
            raise ValueError("active memory not found")
        corrected_content = (corrected_content or "").strip()
        if not corrected_content:
            raise ValueError("corrected content must not be empty")

        old.status = MemoryStatus.SUPERSEDED
        old.updated_at = time.time()
        old.version += 1
        self._dirty.add((user_id, memory_id))
        return self.add_memory(
            user_id=user_id,
            content=corrected_content,
            category=category or old.category,
            emotion=emotion or old.emotion,
            emotion_intensity=emotion_intensity if emotion_intensity is not None else old.emotion_intensity,
            confidence=max(old.confidence, 0.95),
            source_type="user_edit",
            supersedes_id=memory_id,
        )

    @_synchronized
    def soft_delete_memory(self, user_id: str, memory_id: str) -> bool:
        memory = self._get_user_memories(user_id).get(memory_id)
        if memory is None or memory.status == MemoryStatus.DELETED:
            return False
        memory.status = MemoryStatus.DELETED
        memory.version += 1
        memory.updated_at = time.time()
        self._dirty.add((user_id, memory_id))
        self._save_to_disk()
        self._record_grade_event(
            user_id,
            memory_id,
            "manual",
            old_grade=memory.grade,
            new_grade=memory.grade,
            old_score=memory.degree_score,
            new_score=memory.degree_score,
            reason="soft_deleted",
        )
        return True

    @_synchronized
    def apply_feedback(
        self,
        user_id: str,
        memory_id: str,
        feedback: str,
    ) -> Optional[MemoryItem]:
        memory = self._get_user_memories(user_id).get(memory_id)
        if memory is None or not memory.active():
            return None
        adjustments = {
            "helpful": ("recalled_useful", 0.06, "useful"),
            "irrelevant": ("recalled_irrelevant", -0.08, "harmful"),
            "wrong": ("user_denied", -0.12, "harmful"),
            "outdated": ("outdated", -0.10, "harmful"),
        }
        if feedback not in adjustments:
            raise ValueError("feedback must be helpful, irrelevant, wrong or outdated")
        event_type, adjustment, counter = adjustments[feedback]
        if counter == "useful":
            memory.useful_count += 1
        else:
            memory.harmful_count += 1
        return self.reinforce_memory(
            user_id=user_id,
            memory_id=memory_id,
            event_type=event_type,
            observation_score=clamp(memory.degree_score + adjustment),
            record_mention=False,
        )

    @_synchronized
    def set_manual_grade(self, user_id: str, memory_id: str, grade: int, locked: bool = True) -> bool:
        memory = self._get_user_memories(user_id).get(memory_id)
        if memory is None or not memory.active():
            return False
        try:
            new_grade = MemoryGrade(int(grade))
        except (TypeError, ValueError):
            return False
        now = time.time()
        old_grade = memory.grade
        old_score = memory.degree_score
        memory.grade = new_grade
        memory.layer = new_grade.legacy_layer
        memory.degree_score = max(memory.degree_score, grade_threshold(new_grade) if int(new_grade) else 0.0)
        memory.locked = bool(locked)
        memory.promotion_streak = 0
        memory.demotion_streak = 0
        memory.last_regraded_at = now
        memory.regrade_reason = "manual"
        memory.version += 1
        memory.updated_at = now
        self._dirty.add((user_id, memory_id))
        self._save_to_disk()
        self._record_grade_event(
            user_id,
            memory_id,
            "manual",
            old_grade=old_grade,
            new_grade=new_grade,
            old_score=old_score,
            new_score=memory.degree_score,
            reason="manual_grade_locked" if locked else "manual_grade_unlocked",
        )
        return True

    @_synchronized
    def get_grade_history(self, user_id: str, memory_id: str, limit: int = 100) -> List[Dict]:
        rows = self._get_conn().execute(
            """SELECT id, event_type, old_grade, new_grade, old_degree_score,
                      new_degree_score, observation_score, frequency_score,
                      importance_score, reason, source_memory_id, conversation_id,
                      message_id, created_at
               FROM memory_grade_events
               WHERE user_id = ? AND memory_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, memory_id, max(1, min(limit, 500))),
        ).fetchall()
        keys = [
            "id", "event_type", "old_grade", "new_grade", "old_degree_score",
            "new_degree_score", "observation_score", "frequency_score",
            "importance_score", "reason", "source_memory_id", "conversation_id",
            "message_id", "created_at",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def search_memories(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        emotion_filter: Optional[EmotionType] = None,
        min_importance: float = 0.0,
        queries: Optional[List[str]] = None,
        conversation_id: Optional[str] = None,
    ) -> List[MemoryItem]:
        """
        混合检索：语义 + 关键词多路召回 → RRF 融合 → Reranking。

        参数：
        - queries: 可选的改写查询列表，用于多查询检索
        """
        self._ensure_search_state()
        started = time.perf_counter()
        # Snapshot references under the lock, then do network-backed embedding work
        # outside it so one slow provider call does not block unrelated users.
        with self._state_lock:
            user_memories = dict(self._get_user_memories(user_id))
        if not user_memories:
            self._record_search_metrics((time.perf_counter() - started) * 1000, 0.0, 0.0, 0.0, 0.0)
            return []

        search_queries = queries or [query]

        # 多路召回 + RRF 融合
        rrf_scores: Dict[str, float] = {}  # memory_id -> rrf_score
        rrf_k = 60

        query_embeddings = self._get_query_embeddings(search_queries)
        embedding_elapsed = (time.perf_counter() - started) * 1000
        semantic_elapsed = 0.0
        keyword_elapsed = 0.0
        for q in search_queries:
            # 语义检索
            semantic_started = time.perf_counter()
            semantic_results = self._semantic_search_raw(
                user_memories, q, emotion_filter, min_importance,
                user_id=user_id, query_embedding=query_embeddings.get(q),
            )
            semantic_elapsed += (time.perf_counter() - semantic_started) * 1000
            # 关键词检索
            keyword_started = time.perf_counter()
            keyword_results = self._keyword_search_raw(user_memories, q, emotion_filter, min_importance)
            keyword_elapsed += (time.perf_counter() - keyword_started) * 1000

            # RRF: score = 1 / (k + rank + 1)
            for rank, (score, memory) in enumerate(semantic_results):
                rrf_scores[memory.id] = rrf_scores.get(memory.id, 0) + 1.0 / (rrf_k + rank + 1)
            for rank, (score, memory) in enumerate(keyword_results):
                rrf_scores[memory.id] = rrf_scores.get(memory.id, 0) + 1.0 / (rrf_k + rank + 1)

        # 构建候选列表
        candidates = []
        for mid, rrf_score in rrf_scores.items():
            if mid in user_memories:
                candidates.append((rrf_score, user_memories[mid]))

        if not candidates:
            self._record_search_metrics(
                (time.perf_counter() - started) * 1000,
                embedding_elapsed,
                semantic_elapsed,
                keyword_elapsed,
                0.0,
            )
            return []

        # Reranking
        rerank_started = time.perf_counter()
        ranked = self._rerank(candidates, query, emotion_filter)
        rerank_elapsed = (time.perf_counter() - rerank_started) * 1000

        # 更新访问统计
        selected = ranked[:limit]
        with self._state_lock:
            current_memories = self._get_user_memories(user_id)
            selected = [current_memories[m.id] for m in selected if m.id in current_memories]
            for m in selected:
                m.access_count += 1
                m.last_accessed = time.time()
                self._dirty.add((user_id, m.id))
                if conversation_id:
                    self.reinforce_memory(
                        user_id=user_id,
                        memory_id=m.id,
                        event_type="mentioned",
                        conversation_id=conversation_id,
                    )
            self._save_to_disk()
            total_elapsed = (time.perf_counter() - started) * 1000
            self._record_search_metrics(
                total_elapsed, embedding_elapsed, semantic_elapsed, keyword_elapsed, rerank_elapsed
            )

        return selected

    def _build_search_index(self, user_id: str) -> tuple:
        """Build (or reuse cached) normalized embedding matrix for batch search.
        
        Returns (ids_list, normalized_matrix). Matrix rows align with ids_list.
        Cache invalidated when any memory for this user is modified.
        """
        self._ensure_search_state()
        with self._state_lock:
            cached = self._search_index_cache.get(user_id)
            user_memories = dict(self._get_user_memories(user_id))
        signature = tuple(sorted(
            (m.id, m.status.value, id(m.embedding), len(m.embedding) if m.embedding is not None else 0)
            for m in user_memories.values() if m.active() and m.embedding is not None
        ))
        if cached is not None and cached["signature"] == signature:
            with self._state_lock:
                self._search_metrics["cache_hits"] += 1
            return cached["ids"], cached["matrix"]
        with self._state_lock:
            self._search_metrics["cache_misses"] += 1
        
        ids = []
        vectors = []
        for m in user_memories.values():
            if m.active() and m.embedding is not None:
                ids.append(m.id)
                vectors.append(m.embedding)
        
        if not vectors:
            with self._state_lock:
                self._search_index_cache[user_id] = {"ids": [], "matrix": None, "signature": signature}
            return [], None
        
        matrix = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix /= norms
        
        with self._state_lock:
            self._search_index_cache[user_id] = {"ids": ids, "matrix": matrix, "signature": signature}
        logger.debug("[SearchIndex] rebuilt for %s: %d vectors", user_id, len(ids))
        return ids, matrix

    def _invalidate_search_cache(self, user_id: str):
        self._ensure_search_state()
        with self._state_lock:
            self._search_index_cache.pop(user_id, None)

    def _record_search_metrics(
        self,
        total_ms: float,
        embedding_ms: float,
        semantic_ms: float,
        keyword_ms: float,
        rerank_ms: float,
    ) -> None:
        self._ensure_search_state()
        with self._state_lock:
            self._search_metrics["searches_total"] += 1
            self._search_metrics["stage_ms"]["embedding"] += embedding_ms
            self._search_metrics["stage_ms"]["semantic"] += semantic_ms
            self._search_metrics["stage_ms"]["keyword"] += keyword_ms
            self._search_metrics["stage_ms"]["rerank"] += rerank_ms
            self._search_metrics["stage_ms"]["total"] += total_ms

    def _get_query_embeddings(self, queries: List[str]) -> Dict[str, Optional[List[float]]]:
        """Resolve query vectors in one batch, with a short-lived process cache."""
        self._ensure_search_state()
        now = time.time()
        unique = list(dict.fromkeys(q for q in queries if q))
        result: Dict[str, Optional[List[float]]] = {}
        missing = []
        with self._state_lock:
            for query in unique:
                cached = self._query_embedding_cache.get(query)
                if cached and now - cached[0] < self._query_embedding_cache_ttl:
                    result[query] = cached[1]
                else:
                    missing.append(query)
        if missing:
            embeddings = self.embedding_service.get_embeddings_batch(missing)
            for query, embedding in zip(missing, embeddings):
                if embedding is not None:
                    with self._state_lock:
                        self._query_embedding_cache[query] = (now, embedding)
                    result[query] = embedding
        with self._state_lock:
            if len(self._query_embedding_cache) > self._query_embedding_cache_max:
                oldest = sorted(self._query_embedding_cache.items(), key=lambda item: item[1][0])
                for query, _ in oldest[:len(self._query_embedding_cache) - self._query_embedding_cache_max]:
                    self._query_embedding_cache.pop(query, None)
        return result

    def _semantic_search_raw(
        self,
        user_memories: Dict[str, MemoryItem],
        query: str,
        emotion_filter: Optional[EmotionType],
        min_importance: float,
        user_id: Optional[str] = None,
        query_embedding: Optional[List[float]] = None,
    ) -> List[Tuple[float, MemoryItem]]:
        """语义匹配检索（NumPy 批量矩阵运算，O(1) 矩阵乘替代 O(n) 循环）。"""
        # 补算缺失 embedding
        memories_without_embedding = [
            m for m in user_memories.values()
            if m.embedding is None and m.importance >= min_importance and m.active()
        ]
        if memories_without_embedding:
            texts = [m.content for m in memories_without_embedding]
            embeddings = self.embedding_service.get_embeddings_batch(texts)
            backfilled = []
            for m, emb in zip(memories_without_embedding, embeddings):
                if emb is not None:
                    m.embedding = emb
                    backfilled.append(m)
            if user_id and backfilled:
                with self._state_lock:
                    self._dirty.update((user_id, m.id) for m in backfilled)
                    self._save_to_disk()

        if memories_without_embedding:
            if user_id:
                self._invalidate_search_cache(user_id)
        ids, matrix = self._build_search_index(user_id) if user_id else self._build_search_index_from(user_memories)
        if matrix is None or len(ids) == 0:
            return []

        # search_memories pre-resolves every query in one batch. Direct callers
        # without a user id retain the single-query fallback for compatibility.
        if query_embedding is None and user_id is None:
            query_embedding = self._get_query_embeddings([query]).get(query)
        if query_embedding is None:
            return []

        query_vec = np.array(query_embedding, dtype=np.float32)
        qnorm = np.linalg.norm(query_vec)
        if qnorm == 0:
            return []
        query_vec /= qnorm

        similarities = matrix @ query_vec

        filtered_indices = []
        for idx, mid in enumerate(ids):
            memory = user_memories.get(mid)
            if memory is None:
                continue
            if emotion_filter and memory.emotion != emotion_filter:
                continue
            if memory.importance < min_importance:
                continue
            if similarities[idx] > 0.1:
                filtered_indices.append((similarities[idx], memory))

        filtered_indices.sort(key=lambda x: x[0], reverse=True)
        return filtered_indices

    def _build_search_index_from(self, user_memories: Dict[str, MemoryItem]) -> tuple:
        """Build normalized embedding matrix from the given memory dict."""
        ids = []
        vectors = []
        for m in user_memories.values():
            if m.active() and m.embedding is not None:
                ids.append(m.id)
                vectors.append(m.embedding)

        if not vectors:
            return [], None

        matrix = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix /= norms
        return ids, matrix

    def _keyword_search_raw(
        self,
        user_memories: Dict[str, MemoryItem],
        query: str,
        emotion_filter: Optional[EmotionType],
        min_importance: float,
    ) -> List[Tuple[float, MemoryItem]]:
        """关键词匹配检索（无副作用），返回 (匹配分, MemoryItem) 列表。"""
        documents = []
        for memory in user_memories.values():
            if memory.status != MemoryStatus.ACTIVE:
                continue
            if memory.importance < min_importance:
                continue
            if emotion_filter and memory.emotion != emotion_filter:
                continue
            documents.append(memory)
        if not documents:
            return []

        query_tokens = self._search_tokens(query)
        doc_tokens = {memory.id: self._search_tokens(memory.content) for memory in documents}
        df: Dict[str, int] = {}
        for tokens in doc_tokens.values():
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        avgdl = sum(len(tokens) for tokens in doc_tokens.values()) / max(len(documents), 1)
        k1, b = 1.2, 0.75
        results = []
        for memory in documents:
            tokens = doc_tokens[memory.id]
            counts = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            bm25 = 0.0
            for token in query_tokens:
                if token not in counts:
                    continue
                idf = math.log(1 + (len(documents) - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
                tf = counts[token]
                bm25 += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(tokens) / max(avgdl, 1)))
            lexical = self._match_score(memory, query)
            score = lexical + min(0.4, bm25 / max(len(query_tokens), 1))
            if score > 0:
                results.append((score, memory))
        results.sort(key=lambda x: x[0], reverse=True)
        return results

    @staticmethod
    def _search_tokens(text: str) -> List[str]:
        """Tokenize mixed Chinese/English text into terms and adjacent Chinese bigrams."""
        lowered = (text or "").lower()
        words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", lowered)
        chinese = [token for token in words if len(token) == 1 and '\u4e00' <= token <= '\u9fff']
        bigrams = [chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1)]
        return words + bigrams

    @_synchronized
    def get_search_metrics(self) -> Dict:
        """Return a snapshot of retrieval counters and average stage timings."""
        self._ensure_search_state()
        total = self._search_metrics["searches_total"]
        stage_ms = self._search_metrics["stage_ms"]
        cache_lookups = self._search_metrics["cache_hits"] + self._search_metrics["cache_misses"]
        return {
            "searches_total": total,
            "cache_hits": self._search_metrics["cache_hits"],
            "cache_misses": self._search_metrics["cache_misses"],
            "cache_hit_rate": round(self._search_metrics["cache_hits"] / cache_lookups, 3) if cache_lookups else 0.0,
            "avg_stage_ms": {key: round(value / total, 3) if total else 0.0 for key, value in stage_ms.items()},
        }

    def _rerank(
        self,
        candidates: List[Tuple[float, MemoryItem]],
        query: str,
        emotion_filter: Optional[EmotionType] = None,
    ) -> List[MemoryItem]:
        """
        多特征 Reranking：RRF 分数 + 时间衰减 + 情感匹配 + 重要性 + layer 权重。

        权重（v2.0 修复版）：
        - RRF 分数: 0.50（检索质量，保持不变）
        - 时间衰减: 0.20（近期记忆更相关，保持不变）
        - 情感匹配: 0.15（情感状态一致的记忆更相关，保持不变）
        - 重要性:   0.10（重要记忆更相关，从 0.15 降到 0.10）
        - layer 权重: 0.05（新增，从重要性中分出）
        """
        from memory_layer import MemoryLayer  # 延迟导入，避免循环依赖
        
        now = time.time()
        query_emotion, _ = self.emotion_analyzer.analyze(query)

        # 归一化 RRF 分数
        max_rrf = max(c[0] for c in candidates) if candidates else 1.0
        if max_rrf == 0:
            max_rrf = 1.0

        scored = []
        for rrf_score, memory in candidates:
            # 归一化 RRF (0-1)
            norm_rrf = rrf_score / max_rrf

            # 时间衰减（使用 layer 的遗忘强度）
            layer = memory.layer if hasattr(memory, "layer") else 3
            strength = MemoryLayer(layer).get_forgetting_strength()
            elapsed_hours = (now - (memory.last_accessed or memory.created_at)) / 3600
            time_score = math.exp(-elapsed_hours / (strength * 24 + 1))

            # 情感匹配
            emotion_score = 0.0
            if query_emotion != EmotionType.NEUTRAL and memory.emotion == query_emotion:
                emotion_score = 1.0
            elif query_emotion != EmotionType.NEUTRAL and memory.emotion != EmotionType.NEUTRAL:
                emotion_score = 0.3  # 非中性情感有部分匹配

            # layer 权重
            layer_weight = MemoryLayer(layer).get_retrieval_weight()

            # 综合评分（修复版：保持 RRF 权重不变）
            final_score = (
                norm_rrf * 0.50          # 检索质量（保持不变）
                + time_score * 0.20      # 时间衰减（保持不变）
                + emotion_score * 0.15   # 情感匹配（保持不变）
                + memory.importance * 0.10  # 重要性（从 0.15 降到 0.10）
                + layer_weight * 0.05    # layer 权重（新增，从重要性中分出）
            )
            scored.append((final_score, memory))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored]

    def _match_score(self, memory: MemoryItem, query: str) -> float:
        """计算记忆与查询的匹配度。"""
        query_lower = query.lower()
        content_lower = memory.content.lower()

        # 中文字符级子串匹配（优先级最高）
        # 移除空格和标点，逐字符匹配
        clean_query = "".join(c for c in query_lower if c.isalnum())
        clean_content = "".join(c for c in content_lower if c.isalnum())

        # 1. 查询中的连续关键词在内容中出现
        if len(clean_query) >= 2 and clean_query in clean_content:
            return 0.8
        if len(clean_content) >= 2 and clean_content in clean_query:
            return 0.8

        # 2. 提取查询中的中文词（2-4字组合）进行匹配
        common_count = 0
        for word_len in range(2, min(5, len(clean_query) + 1)):
            for i in range(len(clean_query) - word_len + 1):
                sub = clean_query[i:i+word_len]
                if sub in clean_content:
                    common_count += 1

        if common_count > 0:
            ratio = common_count / max(len(clean_query), 1)
            return ratio * (0.6 + memory.importance * 0.4)

        # 3. 英文词匹配（回退）
        query_words = set(query_lower.replace("，", " ").replace("。", " ").replace("！", " ").replace("？", " ").split())
        content_words = set(content_lower.replace("，", " ").replace("。", " ").replace("！", " ").replace("？", " ").split())
        common = query_words & content_words
        if common:
            union = query_words | content_words
            jaccard = len(common) / len(union)
            return jaccard * (0.6 + memory.importance * 0.4)

        return 0.0

    @_synchronized
    def update_emotion(self, user_id: str, memory_id: str, emotion: EmotionType, intensity: float):
        user_memories = self._get_user_memories(user_id)
        if memory_id in user_memories:
            user_memories[memory_id].emotion = emotion
            user_memories[memory_id].emotion_intensity = intensity
            self._dirty.add((user_id, memory_id))
            self._save_to_disk()

    @_synchronized
    def consolidate_memory(self, user_id: str, memory_id: str) -> bool:
        user_memories = self._get_user_memories(user_id)
        if memory_id in user_memories:
            memory = user_memories[memory_id]
            memory.is_consolidated = True
            memory.access_count += 1
            memory.last_accessed = time.time()
            memory.importance = min(1.0, memory.importance + 0.05)
            self._dirty.add((user_id, memory_id))
            self._save_to_disk()
            return True
        return False

    @_synchronized
    def prune_memories(self, user_id: str, threshold: float = 0.1) -> List[str]:
        user_memories = self._get_user_memories(user_id)
        to_archive = []

        for mid, memory in user_memories.items():
            if memory.status not in {MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE}:
                continue
            new_importance = EbbinghausCurve.decay_importance(memory)
            memory.importance = new_importance

            if new_importance < threshold and not memory.is_consolidated:
                memory.status = MemoryStatus.ARCHIVED
                memory.version += 1
                memory.updated_at = time.time()
                memory.regrade_reason = "archived_by_decay"
                to_archive.append(mid)

        self._dirty.update((user_id, mid) for mid in to_archive)

        if to_archive:
            self._save_to_disk()
        return to_archive

    # 活跃记忆上限。以前是 300：一个每天聊十几句的人几周就撞顶，
    # 之后最低分的旧记忆会被静默归档（界面上只表现为"长期记忆 · 300 条"不再涨），
    # 归档再堆到 600 条还会被物理删除——用户故事就这么悄悄没了。
    # 实测检索代价是线性的（约 65µs/条/查询），5000 条时单次检索 ~0.3s、
    # 线上那种 3 路查询 ~1s，相比模型回一句 25~35s 完全可忽略，所以放宽到 5000。
    MAX_ACTIVE_MEMORIES = int(os.environ.get("MOZ_MAX_ACTIVE_MEMORIES", "5000"))
    # 巩固会同步调用模型（每次最多 5 个簇），触发点必须明显低于上限，也别低到天天触发
    CONSOLIDATION_TRIGGER = int(os.environ.get("MOZ_CONSOLIDATION_TRIGGER", "3000"))
    # 归档条数超过这个值才允许物理删除；0 = 永不硬删（一条归档才 ~180 字节，留着不心疼）
    ARCHIVE_DELETE_AFTER = int(os.environ.get("MOZ_ARCHIVE_DELETE_AFTER", "0"))

    def auto_maintain(self, user_id: str):
        """Auto-maintain memory count: consolidate first, then archive lowest-value.
        
        Called after add_memory when count exceeds soft threshold.
        Three-tier strategy:
        1. If > consolidation_trigger: merge related fragments via MemoryConsolidator
        2. If still > max_active: archive lowest degree_score unconsolidated memories  
        3. Hard-delete oldest archived memories beyond 2x max_active
        """
        user_memories = self._get_user_memories(user_id)
        active = [m for m in user_memories.values() if m.active()]
        
        # Tier 1: Consolidate related memories to reduce count naturally
        if len(active) >= self.CONSOLIDATION_TRIGGER:
            try:
                from memory_consolidation import MemoryConsolidator
                consolidator = MemoryConsolidator(self)
                merged = consolidator.consolidate_user(user_id)
                if merged:
                    logger.info("[自动维护] 巩固合并了 %d 组记忆, user=%s", merged, user_id)
                    active = [m for m in self._get_user_memories(user_id).values() if m.active()]
            except Exception as e:
                logger.warning("[自动维护] 巩固失败: %s", e)

        # Tier 2: Archive lowest-value unconsolidated memories over cap
        if len(active) > self.MAX_ACTIVE_MEMORIES:
            excess = len(active) - self.MAX_ACTIVE_MEMORIES
            unconsolidated = sorted(
                [m for m in active if not m.is_consolidated and not m.locked],
                key=lambda m: m.degree_score
            )
            to_archive = set()
            for m in unconsolidated[:excess]:
                m.status = MemoryStatus.ARCHIVED
                m.version += 1
                m.updated_at = time.time()
                m.regrade_reason = "archived_by_capacity"
                to_archive.add(m.id)
            
            if to_archive:
                self._dirty.update((user_id, mid) for mid in to_archive)
                logger.info("[自动维护] 归档 %d 条低价值记忆, user=%s", len(to_archive), user_id)

        # Tier 3: 默认**不**物理删除归档（0 = 永不删）。
        # 陪伴类产品的底线是"你说过的话不会凭空消失"，真要腾空间由用户自己在设置里清。
        delete_after = self.ARCHIVE_DELETE_AFTER
        if delete_after > 0:
            archived = [
                m for m in user_memories.values()
                if m.status == MemoryStatus.ARCHIVED
            ]
            if len(archived) > delete_after:
                archived.sort(key=lambda m: m.updated_at)
                to_delete = [m.id for m in archived[:len(archived) - delete_after]]
                for mid in to_delete:
                    del user_memories[mid]
                    conn = self._get_conn()
                    conn.execute("DELETE FROM memories WHERE user_id = ? AND memory_id = ?", (user_id, mid))
                conn.commit()
                logger.info("[自动维护] 删除 %d 条过期归档记忆, user=%s", len(to_delete), user_id)

    @_synchronized
    def export_memories(self, user_id: str) -> List[Dict]:
        """Export all memories for a user as a list of dicts (no embeddings)."""
        user_memories = self._get_user_memories(user_id)
        return [m.public_dict() for m in user_memories.values()]

    @_synchronized
    def import_memories(self, user_id: str, memories_data: List[Dict]) -> int:
        """Import memories from an export. Skips duplicates by content hash."""
        from memory_governance import normalized_content
        existing = {
            normalized_content(m.content)
            for m in self._get_user_memories(user_id).values()
        }
        count = 0
        for data in memories_data:
            content = data.get("content", "").strip()
            if not content:
                continue
            norm = normalized_content(content)
            if norm in existing:
                continue
            try:
                memory = MemoryItem.from_dict(data)
                memory.id = self._generate_id(content, user_id)
                if memory.embedding is None:
                    emb = self.embedding_service.get_embedding(content)
                    if emb is not None:
                        memory.embedding = emb
                self._get_user_memories(user_id)[memory.id] = memory
                self._dirty.add((user_id, memory.id))
                existing.add(norm)
                count += 1
            except Exception as e:
                logger.warning("[导入] 跳过无效记忆: %s", e)
        if count:
            self._save_to_disk()
        return count

    def get_memory_stats(self, user_id: str) -> Dict:
        """获取用户记忆统计信息。"""
        user_memories = self._get_user_memories(user_id)
        if not user_memories:
            return {"total": 0}

        emotion_dist = {}
        category_dist = {}
        importance_avg = 0.0

        active_memories = [
            m for m in user_memories.values() if m.status == MemoryStatus.ACTIVE
        ]
        if not active_memories:
            return {"total": 0}

        for m in active_memories:
            if m.status != MemoryStatus.ACTIVE:
                continue
            emotion_dist[m.emotion.value] = emotion_dist.get(m.emotion.value, 0) + 1
            category_dist[m.category.value] = category_dist.get(m.category.value, 0) + 1
            importance_avg += m.importance

        importance_avg /= len(active_memories)

        return {
            "total": len(active_memories),
            "emotion_distribution": emotion_dist,
            "category_distribution": category_dist,
            "avg_importance": round(importance_avg, 3),
            "consolidated_count": sum(1 for m in active_memories if m.is_consolidated),
        }


    @_synchronized
    def get_user_memory_items(
        self,
        user_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[tuple[str, MemoryItem]]:
        """Return a copy of the user's current memory items for read-only API use."""
        return [
            (memory_id, memory)
            for memory_id, memory in self._get_user_memories(user_id).items()
            if include_inactive or memory.active()
        ]

    @_synchronized
    def set_memory_layer(self, user_id: str, memory_id: str, layer: int) -> bool:
        memories = self._get_user_memories(user_id)
        if memory_id not in memories:
            return False
        memory = memories[memory_id]
        memory.layer = layer
        self._dirty.add((user_id, memory_id))
        self._save_to_disk()
        return True

    @_synchronized
    def attach_temporal_metadata(self, user_id: str, temporal_data: dict, max_age_seconds: float = 10) -> int:
        now = time.time()
        recent = [memory for memory in self._get_user_memories(user_id).values() if now - memory.created_at < max_age_seconds]
        for memory in recent:
            memory.temporal_data = temporal_data
            self._dirty.add((user_id, memory.id))
        if recent:
            self._save_to_disk()
        return len(recent)
    # ================================================================
    # 持久化（SQLite）
    # ================================================================

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            last_error = None
            for attempt in range(3):
                try:
                    with self._conn_lock:
                        conn = sqlite3.connect(self.db_path, timeout=5)
                        conn.execute("PRAGMA journal_mode=WAL")
                        self._local.conn = conn
                    return self._local.conn
                except sqlite3.Error as e:
                    last_error = e
                    logger.warning(f"数据库连接失败 (尝试 {attempt + 1}/3): {e}")
                    if attempt < 2:
                        time.sleep(0.5)
            raise last_error
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                user_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (user_id, memory_id)
            )
        """)
        conn.commit()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_grade_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                old_grade INTEGER,
                new_grade INTEGER,
                old_degree_score REAL,
                new_degree_score REAL,
                observation_score REAL,
                frequency_score REAL,
                importance_score REAL,
                reason TEXT,
                source_memory_id TEXT,
                conversation_id TEXT,
                message_id TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grade_events_memory_time
            ON memory_grade_events(memory_id, created_at DESC)
        """)
        conn.commit()

    def _save_to_disk(self):
        if not self._dirty:
            return
        conn = self._get_conn()
        for user_id, mid in list(self._dirty):
            if user_id in self.memories and mid in self.memories[user_id]:
                m = self.memories[user_id][mid]
                data = json.dumps(m.to_dict(), ensure_ascii=False)
                conn.execute(
                    "INSERT OR REPLACE INTO memories (user_id, memory_id, data) VALUES (?, ?, ?)",
                    (user_id, mid, data),
                )
        conn.commit()
        self._dirty.clear()

    def _load_from_disk(self):
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT user_id, memory_id, data FROM memories").fetchall()
            for user_id, mid, data_str in rows:
                if user_id not in self.memories:
                    self.memories[user_id] = {}
                try:
                    self.memories[user_id][mid] = MemoryItem.from_dict(json.loads(data_str))
                except Exception:
                    pass
        except sqlite3.OperationalError:
            pass

    @_synchronized
    def get_all_user_ids(self) -> List[str]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT DISTINCT user_id FROM memories").fetchall()
            return sorted([r[0] for r in rows])
        except sqlite3.OperationalError:
            return []

    @_synchronized
    def delete_user_memories(self, user_id: str):
        if user_id in self.memories:
            del self.memories[user_id]
        self._dirty = {(uid, mid) for uid, mid in self._dirty if uid != user_id}
        conn = self._get_conn()
        conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        conn.commit()

    # ================================================================
    # 事实提取与存储
    # ================================================================

    def _resolve_fact_conflicts(
        self,
        user_id: str,
        new_fact: str,
        category: MemoryCategory,
    ) -> Optional[str]:
        """Check new fact against similar active memories for semantic conflicts.

        Returns supersedes_id if the new fact should replace an existing one,
        or None if it's a genuinely new memory.
        Uses embedding similarity to find candidates, then LLM to decide.
        """
        user_memories = self._get_user_memories(user_id)
        if not user_memories:
            return None

        embedding = self.embedding_service.get_embedding(new_fact)
        if embedding is None:
            return None

        # Find semantically related candidates (lower threshold than dedup:
        # we want "related but not identical" memories for conflict check).
        conflict_threshold = float(os.environ.get("MOZ_CONFLICT_SIMILARITY", "0.65"))
        dedup_threshold = float(os.environ.get("MOZ_MEMORY_DEDUPE_SIMILARITY", "0.88"))
        candidates = []
        for m in user_memories.values():
            if not m.active() or m.embedding is None:
                continue
            sim = EmbeddingService.cosine_similarity(embedding, m.embedding)
            if sim >= conflict_threshold and sim < dedup_threshold:
                candidates.append((sim, m))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)

        # Use LLM to decide relationship between new fact and top candidates.
        try:
            from llm_config import get_llm_client
            from langchain_core.messages import HumanMessage
            import json as _json

            llm = get_llm_client(temperature=0.0, use_thinking=False)

            candidate_texts = "\n".join(
                f"  {i+1}. {m.content}" for i, (_, m) in enumerate(candidates[:3])
            )
            prompt = f"""判断新事实与已有记忆的关系。

已有记忆：
{candidate_texts}

新事实：{new_fact}

规则：
- UPDATE: 新事实是对某条已有记忆的更新/替代（用户换了工作、搬了家、改变了观点等）
- ADD: 新事实是全新信息，与已有记忆不矛盾
- NOOP: 新事实与已有记忆重复或无意义

只输出 JSON: {{"action": "UPDATE|ADD|NOOP", "target": 编号或null}}
"""

            response = llm.invoke([HumanMessage(content=prompt)])
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            result = _json.loads(content)
            action = result.get("action", "ADD")
            target_idx = result.get("target")

            if action == "UPDATE" and target_idx and isinstance(target_idx, int):
                idx = target_idx - 1
                if 0 <= idx < len(candidates[:3]):
                    old_memory = candidates[idx][1]
                    logger.info(
                        "[冲突处理] 新事实将替代记忆 %s: '%s' -> '%s'",
                        old_memory.id, old_memory.content[:50], new_fact[:50]
                    )
                    return old_memory.id
        except Exception as e:
            logger.warning("[冲突处理] LLM 决策失败，默认新增: %s", e)

        return None

    def extract_and_store_facts(
        self,
        user_id: str,
        user_msg: str,
        assistant_msg: str,
        category: MemoryCategory = MemoryCategory.FACT,
        emotion: Optional[EmotionType] = None,
        emotion_intensity: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> List[str]:
        """从对话中提取关键事实并存储为记忆。
        
        返回提取到的事实列表。
        """
        extracted_facts = self._extract_facts(user_msg, assistant_msg)

        if extracted_facts:
            for fact in extracted_facts:
                supersedes = self._resolve_fact_conflicts(user_id, fact, category)
                self.add_memory(
                    user_id=user_id,
                    content=fact,
                    category=category,
                    emotion=emotion,
                    emotion_intensity=emotion_intensity,
                    conversation_id=conversation_id,
                    supersedes_id=supersedes,
                )
        else:
            user_summary = user_msg.strip()[:60]
            ai_summary = assistant_msg.strip()[:80]
            if len(user_summary) >= 2:
                self.add_memory(
                    user_id=user_id,
                    content=f"[对话摘要] 用户说：{user_summary}",
                    category=category,
                    emotion=emotion,
                    emotion_intensity=emotion_intensity,
                    conversation_id=conversation_id,
                )
            if len(ai_summary) >= 4:
                self.add_memory(
                    user_id=user_id,
                    content=f"[对话摘要] AI回复要点：{ai_summary}",
                    category=category,
                    emotion=emotion,
                    emotion_intensity=emotion_intensity,
                    conversation_id=conversation_id,
                )

        return extracted_facts

    def _extract_facts(self, user_msg: str, assistant_msg: str) -> List[str]:
        """从对话中提取关于用户的关键事实。使用规则 + LLM 混合提取。"""
        facts = []
        logger.debug("[事实提取] 收到用户消息，长度=%d", len(user_msg))

        info_keywords = ["我叫", "我是", "我喜欢", "我不喜欢", "我的职业", "我的工作",
                         "我养了", "我有", "我在", "我从事", "我来自", "我结婚", "我单身",
                         "我希望", "我害怕", "我担心", "我讨厌"]
        ai_subject_patterns = ["你会", "你可以", "你能", "你总是", "你应该", "你说的", "你回", "你跟"]
        for kw in info_keywords:
            if kw in user_msg:
                sentences = user_msg.replace("。", "。SPLIT").replace("！", "！SPLIT").replace("？", "？SPLIT").replace("，", "，SPLIT").split("SPLIT")
                for s in sentences:
                    s = s.strip().rstrip("，。！？,.")
                    if kw in s and 4 <= len(s) <= 100:
                        if any(s.startswith(p) or f" {p}" in s or f"，{p}" in s for p in ai_subject_patterns):
                            continue
                        facts.append(f"[关于用户] {s}")
                break

        emotion_keywords = ["焦虑", "孤独", "悲伤", "痛苦", "迷茫",
                           "压力大", "崩溃", "失望", "愤怒", "抑郁"]
        for kw in emotion_keywords:
            if kw in user_msg:
                facts.append(f"[用户情感状态] 用户正在经历{kw}")
                break

        try:
            llm_facts = self._llm_extract_facts(user_msg, assistant_msg)
            existing = set(facts)
            for f in llm_facts:
                if f not in existing:
                    facts.append(f)
        except Exception as e:
            logger.warning(f"[事实提取] LLM 提取失败: {e}")

        facts = self._filter_low_quality_facts(facts)

        logger.info(f"[事实提取] 提取了 {len(facts)} 条事实")
        return facts[:5]

    def _filter_low_quality_facts(self, facts: List[str]) -> List[str]:
        """过滤低质量事实。"""
        filtered = []
        seen_prefixes = set()
        for f in facts:
            if not f or len(f) < 5:
                continue
            if f.rstrip().endswith("，") or f.rstrip().endswith(","):
                continue
            prefix = f[:20]
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            filtered.append(f)
        return filtered

    def _llm_extract_facts(self, user_msg: str, assistant_msg: str) -> List[str]:
        """调用 LLM 从对话中提取关键事实。"""
        try:
            from llm_config import get_llm_client
            from langchain_core.messages import HumanMessage
            import json as _json

            llm = get_llm_client(temperature=0.0, use_thinking=False)

            prompt = f"""你是一个情感陪伴助手的记忆提取模块。请从以下对话中提取值得长期记住的信息。

对话：
用户：{user_msg}
AI：{assistant_msg}

严格规则：
1. 只能提取用户**明确说出**的事实，禁止任何推测或引申
2. 如果用户没有表达任何值得记住的信息，返回 []
3. 禁止提取 AI 对用户的建议（除非用户明确表示采纳）
4. 禁止提取用户"可能想做"的事情，只提取用户明确说的
5. 情感状态类事实仅在用户明确表达情绪时提取

需要提取的（按优先级）：
1. 用户明确表达的当前情感状态（如"我感到孤独"）
2. 用户明确提到的具体事件
3. 用户明确提到的人际关系
4. 用户明确表达的偏好（如"我喜欢xxx"）
5. 用户明确提到的担忧或困扰
6. AI 向用户推荐的具体内容（歌曲、书籍等），格式为"AI曾向用户推荐了XXX"

不需要提取的：
- 纯粹的客套话或寒暄
- 与用户无关的通用知识
- AI 的建议（除非用户明确采纳）
- 任何推测性内容

以 JSON 数组格式返回，每条事实是完整的陈述句：
["用户今天很难过", "AI曾向用户推荐了歌曲《晴天》"]
如果没有值得提取的信息，返回 []。"""

            response = llm.invoke([HumanMessage(content=prompt)])
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            facts = _json.loads(content)
            if isinstance(facts, list):
                tagged = []
                for f in facts:
                    if not f or len(f) <= 5:
                        continue
                    if f.startswith("AI") or f.startswith("ai") or "AI曾" in f or "AI向" in f or "AI建议" in f or "AI推荐" in f:
                        tagged.append(f"[AI互动] {f}")
                    else:
                        tagged.append(f"[关于用户] {f}")
                return tagged
        except Exception as e:
            logger.warning(f"[事实提取] LLM 提取错误: {e}")
            pass

        return []

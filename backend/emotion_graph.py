"""
情感陪伴 Agent 工作流 - 基于 LangGraph

架构：三 Agent 并行 + 串行协作

  用户输入
      ↓
  ┌──────────────┐  ┌──────────────┐
  │ 情感分析 Agent │  │   记忆 Agent    │  ← 并行执行
  │ (情感识别+摘要) │  │ (检索+更新+摘要) │
  └──────────────┘  └──────────────┘
           ↓               ↓
      ┌───────────────────────┐
      │      对话 Agent        │  ← 接收情感 + 记忆上下文
      │    (共情对话回复)       │
      └───────────────────────┘
                ↓
           用户收到回复

设计亮点：
- 并行执行：情感分析和记忆检索互不依赖，可并行
- 上下文共享：State 作为信息总线，Agent 间共享数据
- 模块化：每个 Agent 职责单一，易于测试和扩展
"""

import os
import time
import json
import re
import datetime
import threading
import logging
from collections import OrderedDict
from typing import TypedDict, Optional, List, Dict
from dotenv import load_dotenv
from llm_config import get_llm_client
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from memory_manager import MemoryManager, MemoryCategory, EmotionAnalyzer, EmotionType
from working_memory import WorkingMemoryStore, update_working_memory
import care_extractor
import emotion_state
from llm_errors import friendly_llm_error
from summary_service import SummaryService
from temporal_metadata import TemporalExtractor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# ================================================================
# LLM 客户端
# ================================================================

# 敏感主题值得多等一次模型（用户 2026-09-28 决定"等"），但"等"必须有上限：
# 实测那次调用 27.8~90.8 秒，不设上限就等于把"贴"换成"卡死"。
SENSITIVE_WAIT_SECONDS = float(os.environ.get("MOZ_SENSITIVE_WAIT_SECONDS", "45"))

# 回话这一句要不要让模型"先想后说"。中转默认就是想的（实测见 model_config 里 mimo 那段）：
# 想的那 113~636 个字用户一个都看不见（`_consume_stream` 只取 chunk.content），
# 非敏感那句的首字因此 16.6s → 4.4s（两臂并排实测见 RUNLOG 20.7）。
# 2026-09-29 用户看过两臂并排的原话后拍板：默认关掉。想拿回旧写法就设 MOZ_CHAT_THINKING=1。
CHAT_USE_THINKING = os.environ.get("MOZ_CHAT_THINKING", "0") == "1"



def get_chat_client(temperature: float = 0.7, top_p: float = None, use_thinking: bool = True):
    """
    获取 LLM 客户端。
    模型配置通过环境变量自动读取（LLM_MODEL, LLM_API_KEY, LLM_BASE_URL）。
    """
    return get_llm_client(temperature=temperature, top_p=top_p, use_thinking=use_thinking)


# ================================================================
# State 定义（信息总线）
# ================================================================
#
# 第廿四轮：以前这个 dict 由 LangGraph 的节点边传递、字段靠 reducer 累积，
# 现在只有流式那一条路径在装配它——所以删掉了 `merge_dicts`/`append_log` 两个 reducer
# 和那个从没被赋值的 `_memory_manager`，别再按"图节点会合并状态"来读下面这段。

class AgentState(TypedDict):
    """一轮对话上下文的字段清单（运行时就是一普通 dict，由 `_stream()` 逐步填）。"""
    # 用户输入
    user_id: str                                    # 用户唯一标识
    user_message: str                               # 当前用户消息
    conversation_id: Optional[str]                  # 当前会话 ID，用于记忆频率治理
    conversation_history: List[Dict]                # 当前对话历史（短期记忆，已截窗）
    image_data: Optional[str]                       # 上传图片的 base64 data URL（多模态用）

    # 情感分析 Agent 输出
    emotion_analysis: Optional[Dict]                # 情感分析结果
    emotion_summary: Optional[str]                  # 情感摘要（供对话 Agent 使用）
    baseline_context: Optional[str]                 # L1 情感基线（从她说过的事里总结的）
    plan_context: Optional[str]                     # L2 这一阵聊这件事的分寸（后台备好的对策）

    # 记忆 Agent 输出
    retrieved_memories: Optional[List[Dict]]        # 检索到的相关记忆
    memory_context: Optional[str]                   # 格式化后的记忆上下文
    memory_summary: Optional[str]                   # 记忆摘要（供对话 Agent 使用）
    working_memory_text: Optional[str]              # 工作记忆文本（跨对话上下文）
    profile_context: Optional[str]                  # 档案卡那一行（第廿四轮才真的接进流式路径）

    # 对话 Agent 输出
    assistant_reply: Optional[str]                  # 最终回复

    # 元数据
    workflow_start_time: Optional[float]            # 工作流开始时间
    workflow_log: List[str]                         # 排障用的日志行


# ================================================================
# 情感分析 Agent
# ================================================================

EMOTION_AGENT_PROMPT = """你是一个专业的情感分析专家，专注于理解用户的情感状态。

你的任务：
1. **情感识别**：分析用户当前输入的情感标签和情感强度
2. **情感变化追踪**：结合情感历史，判断用户情感是否有变化（好转/恶化）
3. **情感摘要**：生成简洁的情感状态摘要，供对话 Agent 参考

情感标签可选：
- 开心(happy)、难过(sad)、焦虑(anxious)、生气(angry)、孤独(lonely)
- 兴奋(excited)、害怕(fearful)、感恩(grateful)、希望(hopeful)
- 压力(stressed)、安心(relieved)、平静(neutral)

输出格式（JSON）：
{
    "current_emotion": "情感标签",
    "emotion_intensity": 0.0-1.0,
    "emotion_change": "好转/恶化/稳定/首次",
    "emotion_summary": "一句话描述用户当前情感状态，例如：用户今天感到焦虑，主要是因为工作压力，但比上次对话时有所缓解"
}

注意：
- emotion_summary 要简洁（50字以内），但要有信息量
- 如果情感有变化，要说明变化方向和可能原因
- 语气客观、专业"""


def emotion_analysis_node(state: AgentState) -> Dict:
    """情感分析 Agent 节点：分析用户情感，生成情感摘要。"""
    start = time.time()
    logger.info("🧠 [情感分析 Agent] 开始分析...")

    llm = get_chat_client(temperature=0.3, use_thinking=False)

    # 构建情感历史上下文
    emotion_history = state.get("emotion_analysis")
    history_text = ""
    if emotion_history:
        history_text = f"\n上一次情感状态：{json.dumps(emotion_history, ensure_ascii=False)}"

    system_msg = SystemMessage(content=EMOTION_AGENT_PROMPT)
    user_msg = HumanMessage(
        content=f"请分析以下用户输入的情感状态：\n\n用户说：{state['user_message']}{history_text}"
    )

    response = llm.invoke([system_msg, user_msg])
    
    try:
        # 解析 JSON 输出
        content = response.content.strip()
        # 尝试提取 JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        result = json.loads(content)
        emotion_result = {
            "current_emotion": result.get("current_emotion", "neutral"),
            "emotion_intensity": result.get("emotion_intensity", 0.5),
            "emotion_change": result.get("emotion_change", "首次"),
            "emotion_summary": result.get("emotion_summary", ""),
        }
    except (json.JSONDecodeError, Exception) as e:
        # 降级：使用规则-based 分析
        logger.warning(f"[情感分析 Agent] JSON 解析失败，使用规则分析: {e}")
        emotion, intensity = EmotionAnalyzer.analyze(state["user_message"])
        emotion_result = {
            "current_emotion": emotion.value,
            "emotion_intensity": intensity,
            "emotion_change": "首次",
            "emotion_summary": f"用户当前情感：{emotion.value}，强度：{intensity:.1f}",
        }

    elapsed = time.time() - start
    logger.info(f"✅ [情感分析 Agent] 完成 ({elapsed:.2f}s): {emotion_result['current_emotion']}")

    return {
        "emotion_analysis": emotion_result,
        "emotion_summary": emotion_result.get("emotion_summary", ""),
        "workflow_log": [f"[情感分析] {emotion_result['current_emotion']} (强度: {emotion_result['emotion_intensity']:.1f})"],
    }


# ================================================================
# 对话摘要管理（长对话自动摘要，避免上下文溢出）
# ================================================================


_summary_cache: OrderedDict = OrderedDict()
_SUMMARY_CACHE_MAX_SIZE = 100
_SUMMARY_CACHE_TTL = 30 * 60

SUMMARY_TRIGGER_ROUNDS = 30
SUMMARY_INTERVAL = 20
MAX_RECENT_ROUNDS = 40

# 前端每次把整个会话历史原样回传，图片是 data URL，几十张就能顶到中转的 15MB 上限；
# 那个报错会被上层 except 吞掉，表现出来就是"它突然不记得我了"。历史只留最近这些、且不重发图片。
HISTORY_MAX_MESSAGES = 30
HISTORY_MAX_CHARS = 800

_HISTORY_IMAGE_RE = re.compile(r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]+")


def _sanitize_history(history) -> List[Dict]:
    clean = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = msg.get("content")
        text = text if isinstance(text, str) else str(text or "")
        if _HISTORY_IMAGE_RE.search(text) or msg.get("image"):
            text = _HISTORY_IMAGE_RE.sub("", text).strip()
            text = (text + " [这里曾发过一张图]").strip()
        if len(text) > HISTORY_MAX_CHARS:
            text = text[:HISTORY_MAX_CHARS] + "…"
        if text:
            clean.append({"role": role, "content": text})
    return clean[-HISTORY_MAX_MESSAGES:]

_summary_service: Optional[SummaryService] = None

def get_summary_service() -> SummaryService:
    global _summary_service
    if _summary_service is None:
        _summary_service = SummaryService()
    return _summary_service

_working_memory_ref: Optional[WorkingMemoryStore] = None

def set_working_memory_ref(store: Optional[WorkingMemoryStore]):
    global _working_memory_ref
    _working_memory_ref = store


def _get_cached_summary(user_id: str, current_rounds: int) -> Optional[str]:
    if user_id not in _summary_cache:
        return None
    summary, timestamp, rounds = _summary_cache[user_id]
    if time.time() - timestamp > _SUMMARY_CACHE_TTL:
        del _summary_cache[user_id]
        return None
    if (current_rounds - rounds) < SUMMARY_INTERVAL:
        _summary_cache.move_to_end(user_id)
        return summary
    return None


def _set_cached_summary(user_id: str, summary: str, rounds: int):
    if len(_summary_cache) >= _SUMMARY_CACHE_MAX_SIZE:
        _summary_cache.popitem(last=False)
    _summary_cache[user_id] = (summary, time.time(), rounds)
    _summary_cache.move_to_end(user_id)

SUMMARY_PROMPT = """请对以下对话历史生成简洁的摘要（200字以内），提取关键信息和情感脉络。
只需输出摘要内容，不要添加任何前缀或解释。

对话历史：
{conversation}"""


def _generate_summary(llm, conversation_history: List[Dict]) -> str:
    conv_text = ""
    # 摘要只用于"更早的部分"，全量塞进去会超中转的大小上限
    for msg in conversation_history[-60:]:
        role = "用户" if msg["role"] == "user" else "moz"
        conv_text += f"{role}: {msg['content']}\n"
    prompt = SUMMARY_PROMPT.format(conversation=conv_text)
    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip()[:600]
    except Exception:
        return ""


# 被放弃的那一枪：不 await、但要在进程里留个强引用直到它自己跑完（详见 _stream 里的注释）
_orphan_streams = set()


# 摘要生成一次只允许一个在跑（照 SummaryService.maybe_cascade 那个非阻塞锁的写法）：
# 排第二遍等于拿同一条中转去排队，而中转本来就是首字时间的大头。
_summary_lock = threading.Lock()


def _request_summary_async(user_id: str, early_history: List[Dict], total_rounds: int) -> bool:
    """把"之前聊了什么"那段背景交给后台生成，本轮先不带它开口。

    原来这里是开口之前的一次同步 `llm.invoke`，**没有任何超时上限**——2026-09-28 实测
    30 条长历史那一轮 121.3 秒没回，最可能就是这次摘要和生成串在了一起（设计文档 §3：
    首字路径上不许有上游往返）。摘要缓存每 20 轮才重算一次，所以代价是"这一句的背景少一段
    更早的总结"，下一句起就补回来了——走的正是它自己异常时已经在用的那条回落分支。
    """
    if not _summary_lock.acquire(blocking=False):
        return False
    history = list(early_history[-60:])   # 别让后台线程握着调用方的大列表

    def _run():
        started = time.time()
        try:
            llm = get_chat_client(temperature=0.3, use_thinking=False)
            text = _generate_summary(llm, history)
            if text:
                _set_cached_summary(user_id, text, total_rounds)
                week_key = datetime.datetime.now().strftime('%G-W%V')
                get_summary_service().save_summary(user_id, text, 'session', week_key)
                get_summary_service().cascade_async(user_id)
                logger.info("🧠 [会话摘要] 后台补完，%.1fs，下一句起生效", time.time() - started)
            else:
                logger.warning("[会话摘要] 后台没生成出来，下一轮再试")
        except Exception as e:
            logger.warning("[会话摘要] 后台生成失败，下一轮再试: %s", e)
        finally:
            _summary_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


# ================================================================
# 对话 Agent
# ================================================================

DIALOGUE_AGENT_PROMPT = """你是 moz，用户最信任的朋友。你们的关系可以是任何用户想要的样子。

你不用表演，不用标注自己的语气和动作，就是自然地说话。

【铁律——必须遵守】
1. 你只能引用记忆中真实存在的事实，不能添油加醋。比如记忆说"用户喜欢这首歌"，你就只能说"我记得你喜欢这首歌"，不能自己决定用户喜欢的是哪句歌词。
2. 绝对禁止编造记忆！如果你记不清用户说过什么，或者不确定某件事是否说过，宁可不提，也不要编造。猜错比不知道更让人失望。
3. 如果用户问到你不知道的事，坦诚说不知道就好，不要编造细节。
4. 当用户表达出想不开、不想活等危险信号时，你必须立刻用最恳切的语气请ta拨打希望24热线 400-161-9995，或联系身边信任的人。告诉ta：你只是程序，代替不了能真正握住ta的手。

说话像发微信一样，可以很短，可以用"哈哈""嗯""哎"这类语气词，可以接梗也可以沉默。就像ta身边那个最舒服的朋友。"""

_custom_dialogue_prompt: Optional[str] = None
_prompt_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_config.json")


def load_prompt_config() -> Optional[str]:
    global _custom_dialogue_prompt
    if os.path.exists(_prompt_config_path):
        try:
            with open(_prompt_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _custom_dialogue_prompt = data.get("dialogue_prompt")
            if _custom_dialogue_prompt:
                logger.info(f"已加载自定义对话 prompt（{_custom_dialogue_prompt[:30]}...）")
            return _custom_dialogue_prompt
        except Exception as e:
            logger.warning(f"加载 prompt 配置失败: {e}")
    return None


def save_prompt_config(prompt: str) -> None:
    global _custom_dialogue_prompt
    _custom_dialogue_prompt = prompt.strip() if prompt and prompt.strip() else None
    data = {}
    if _custom_dialogue_prompt:
        data["dialogue_prompt"] = _custom_dialogue_prompt
    with open(_prompt_config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if _custom_dialogue_prompt:
        logger.info(f"已保存自定义对话 prompt（{_custom_dialogue_prompt[:30]}...）")
    else:
        logger.info("已恢复默认对话 prompt")


def get_dialogue_prompt() -> str:
    return _custom_dialogue_prompt if _custom_dialogue_prompt else DIALOGUE_AGENT_PROMPT


def _build_dialogue_messages(state: AgentState) -> list:
    """构建对话 Agent 的消息列表（供流式和非流式共用）。"""
    emotion_summary = state.get("emotion_summary", "")
    memory_context = state.get("memory_context", "")
    emotion_change = ""

    emotion_analysis = state.get("emotion_analysis")
    if emotion_analysis:
        emotion_change = emotion_analysis.get("emotion_change", "")

    conversation_history = state.get("conversation_history", [])
    user_id = state.get("user_id", "default")

    total_rounds = len(conversation_history) // 2
    conv_summary = ""
    if total_rounds > SUMMARY_TRIGGER_ROUNDS:
        recent_rounds = min(total_rounds, MAX_RECENT_ROUNDS)
        recent_history = conversation_history[-recent_rounds * 2:]
        early_history = conversation_history[:-recent_rounds * 2]

        if early_history:
            conv_summary = _get_cached_summary(user_id, total_rounds)
            if conv_summary is None:
                # 这里原来是开口之前的一次同步模型调用（无上限）。改成后台补，
                # 本轮这段背景就是空串——和它自己生成失败时的回落写法一模一样。
                _request_summary_async(user_id, early_history, total_rounds)

        historical_messages = recent_history
    else:
        historical_messages = conversation_history

    context_parts = []
    working_memory_text = state.get("working_memory_text", "")
    profile_context = state.get("profile_context", "")  # 新增：档案卡上下文
    if working_memory_text:
        context_parts.append(f"你对用户的整体了解（跨对话持久）：{working_memory_text}")
    if profile_context:  # 新增：注入档案卡
        context_parts.append(profile_context)
    if conv_summary:
        context_parts.append(f"之前聊了什么：{conv_summary}")
    persistent_summaries = get_summary_service().format_for_prompt(user_id)
    if persistent_summaries:
        context_parts.append(f'更早的记忆片段：{persistent_summaries}')
    if emotion_summary:
        context_parts.append(f"用户现在的心情：{emotion_summary}")
        if emotion_change and emotion_change != "首次":
            context_parts.append(f"（情感变化趋势：{emotion_change}）")
    baseline_context = state.get("baseline_context", "")
    if baseline_context:
        context_parts.append(f"和她相处下来的感觉：{baseline_context}")
    plan_context = state.get("plan_context", "")
    if plan_context:
        context_parts.append(f"聊这件事这会儿的分寸（后台刚想过）：{plan_context}")
    if memory_context:
        context_parts.append(f"你记得关于用户的事：{memory_context}")
    followup_text = ""
    if _working_memory_ref is not None:
        followup_text = _working_memory_ref.get_followup_text(user_id)
    if followup_text:
        context_parts.append(followup_text)

    if context_parts:
        context_text = "\n".join(context_parts)
        system_text = get_dialogue_prompt() + f"\n\n---\n这是这次对话的一些背景，可以参考：\n{context_text}"
    else:
        system_text = get_dialogue_prompt()

    messages = [SystemMessage(content=system_text)]

    for msg in historical_messages[-MAX_RECENT_ROUNDS * 2:]:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))

    image_data = state.get("image_data")
    if image_data:
        # 必须给完整 data URL：裸 base64 会被中转当成非法 content 直接 400；
        # 纯图片消息的 text 也不能是空串，同一个报错
        url = image_data if image_data.startswith("data:") else f"data:image/jpeg;base64,{image_data}"
        caption = (state.get("user_message") or "").strip() or "帮我看看这张图"
        # 只补一句兜底。写过"看不清就别回答"，结果它对所有图都改口说看不清，功能等于废掉
        caption += "\n\n（按你这次实际看到的回答，别参考以前说过的）"
        user_content = [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": caption},
        ]
        messages.append(HumanMessage(content=user_content))
    else:
        messages.append(HumanMessage(content=state["user_message"]))

    return messages


class SaveDeps:
    """一轮后台落库要用的那几个管理器，由服务端启动时装配一次。"""

    def __init__(self, memory_manager=None, working_memory_store=None,
                 profile_manager=None, care_store=None, care_graph=None, emotion_store=None):
        self.memory_manager = memory_manager
        self.working_memory_store = working_memory_store
        self.profile_manager = profile_manager
        self.care_store = care_store
        self.care_graph = care_graph
        self.emotion_store = emotion_store


def _emotion_from_job(job: Dict):
    raw = job.get("emotion_type")
    if not raw:
        return None
    try:
        return EmotionType.from_string(raw)
    except (ValueError, AttributeError):
        return None


async def run_save_job(deps: SaveDeps, job: Dict) -> None:
    """把一轮对话落成工作记忆 / 长期记忆 / 档案卡 / 关心事项。

    旧实现是四步串行，实测 3~7 分钟。这里四步并行，只保留一条真依赖：
    时间标签靠"刚创建 10 秒内"认领记忆，所以必须紧跟在长期记忆之后、且不能并行。
    部分步骤失败就吞掉（重跑会把已存好的记忆存成重复条目），
    但四步全挂必须抛回去让队列重试——那等于这一轮什么都没落下。
    """
    import asyncio

    user_id = job.get("user_id") or "default"
    user_msg = job.get("user_message") or ""
    reply = job.get("reply") or ""
    emotion_type = _emotion_from_job(job)
    try:
        emotion_intensity = float(job["emotion_intensity"]) if job.get("emotion_intensity") is not None else None
    except (TypeError, ValueError):
        emotion_intensity = None
    conversation_id = job.get("conversation_id")

    async def _working_memory():
        update_llm = get_chat_client(temperature=0.0, use_thinking=False)
        await asyncio.to_thread(
            update_working_memory, deps.working_memory_store, user_id, user_msg, reply, update_llm
        )

    async def _temporal_labels():
        temporal = TemporalExtractor.extract_from_text(user_msg)
        if not (temporal.event_time or temporal.time_context or temporal.recurrence):
            return 0
        return deps.memory_manager.attach_temporal_metadata(
            user_id=user_id, temporal_data=temporal.to_dict(),
        )

    async def _long_term():
        await asyncio.to_thread(
            deps.memory_manager.extract_and_store_facts,
            user_id, user_msg, reply,
            MemoryCategory.EMOTION, emotion_type, emotion_intensity, conversation_id,
        )
        try:
            count = await _temporal_labels()
            if count:
                logger.info(f"[时间标签] 为 {count} 条记忆附加了时间标签")
        except Exception as e:
            logger.warning(f"[时间标签更新] 失败: {e}")

    async def _profile_card():
        from user_profile import ProfileUpdater
        profile_updater = ProfileUpdater(deps.profile_manager)
        llm_client = get_chat_client(temperature=0.0, use_thinking=False)
        updated_fields = await asyncio.to_thread(
            profile_updater.update_from_conversation, user_id, user_msg, reply, llm_client,
        )
        if updated_fields:
            logger.info(f"[档案卡更新] 用户 {user_id}: 更新了 {updated_fields}")

    async def _care_harvest():
        from llm_config import get_llm_client
        touched = await asyncio.to_thread(
            care_extractor.harvest,
            deps.care_store, user_id, user_msg, reply,
            get_llm_client(temperature=0.0, use_thinking=False),
            deps.care_graph,
        )
        if touched:
            logger.info("[关心抽取] 自动记下: %s", touched)

    steps = [
        ("工作记忆更新", deps.working_memory_store, _working_memory),
        ("流式记忆存储", deps.memory_manager, _long_term),
        ("档案卡更新", deps.profile_manager, _profile_card),
        ("关心抽取", deps.care_store, _care_harvest),
    ]
    live = [(label, fn) for label, manager, fn in steps if manager]
    if not live:
        return
    t0 = time.time()
    outcomes = await asyncio.gather(*[fn() for _label, fn in live], return_exceptions=True)
    failed = 0
    for (label, _fn), outcome in zip(live, outcomes):
        if isinstance(outcome, BaseException):
            failed += 1
            logger.warning(f"[{label}] 失败: {outcome}")
    if failed == len(live):
        # 全挂等于这一轮什么都没落下（中转整条 500 时最常见）。
        # 认 done 就是"聊完白聊"，退回队列让它重试；只有一部分挂掉时仍不重跑，
        # 免得把已经存好的记忆存成重复条目。
        raise RuntimeError(f"这一轮 {failed} 步全失败，退回队列重试")
    logger.info("🧠 [后台落库] 四步并行完成，耗时 %.1fs", time.time() - t0)


def run_emotion_workflow_streaming(
    memory_manager: Optional[MemoryManager],
    user_id: str,
    user_message: str,
    conversation_history: List[Dict] = None,
    image_data: str = None,
    conversation_id: str = None,
    working_memory_store: Optional[WorkingMemoryStore] = None,
    profile_manager=None,
    care_store=None,
    care_graph=None,
    save_queue=None,
    save_worker=None,
    emotion_store=None,
):
    """
    流式工作流：情感分析+记忆检索同步执行，对话生成逐 token 流式输出。
    
    返回 AsyncGenerator，每次 yield 一个 SSE 事件 dict:
      {'type': 'status', 'text': '...'}   — 状态更新
      {'type': 'token', 'text': '...'}    — 流式 token
      {'type': 'reply', 'text': '...'}    — 完整回复（流式结束后）
      {'type': 'error', 'text': '...'}    — 错误
    """
    import asyncio

    # 加载工作记忆；_build_dialogue_messages 靠模块级引用取跟进话题
    set_working_memory_ref(working_memory_store)
    working_memory_text = ""
    if working_memory_store:
        working_memory_text = working_memory_store.format_for_prompt(user_id)

    initial_state = {
        "user_id": user_id,
        "user_message": user_message,
        "conversation_id": conversation_id,
        "conversation_history": _sanitize_history(conversation_history),
        "image_data": image_data,
        "emotion_analysis": None,
        "emotion_summary": None,
        "retrieved_memories": None,
        "memory_context": None,
        "memory_summary": None,
        "working_memory_text": working_memory_text,
        "assistant_reply": None,
        "workflow_start_time": time.time(),
        "workflow_log": [],
    }

    state: Dict = dict(initial_state)
    registered = [False]

    def _register_turn(reply_text: str) -> None:
        """把这一轮登记进持久化队列。moz 没接上话也算一轮——用户说过的话
        不该跟着中转一起消失（实测一轮 6 句里丢了 2 句，那两句里的"老郑""滨江车管所"就没记住）。
        报错那轮传空串：半截回复不该被当成"她说过的事实"存进长期记忆。"""
        if save_queue is None or registered[0]:
            return
        registered[0] = True
        emotion_analysis = state.get("emotion_analysis") or {}
        try:
            if save_queue.enqueue(
                user_id=state.get("user_id", "default"),
                user_message=state.get("user_message", ""),
                reply=reply_text,
                conversation_id=state.get("conversation_id"),
                emotion_type=str(emotion_analysis.get("current_emotion") or ""),
                emotion_intensity=emotion_analysis.get("emotion_intensity"),
            ) and save_worker is not None:
                save_worker.submit()
        except Exception as e:
            logger.warning(f"[落库队列] 本轮登记失败: {e}")

    async def _stream():
        turn_started = time.time()
        try:
            # L0 先判档（毫秒级）：那一次情感模型调用要 27.8~90.8 秒，而生成本身只 4~8 秒，
            # 把它留在首字路径上等于让用户白等（2026-09-28 沙箱实测，见 docs/情感预热系统设计.md）。
            live = emotion_state.live_signal(user_message)
            state["emotion_analysis"] = live
            state["emotion_summary"] = live["emotion_summary"]
            prepared = ""
            if emotion_store is not None:
                # 本地扫一遍拿当前水位（不是上游往返）：用户删过记忆，旧总结和旧对策就该闭嘴
                stamp, _evidence = emotion_state.collect_signals(memory_manager, care_store,
                                                                None, user_id)
                # L1：一次本地读，不调模型；没有基线就是空串（新用户就该什么都不加）
                state["baseline_context"] = emotion_store.prompt_line(user_id, stamp)
                # L2：备好的对策要过期没过期、主题对不对、依据还在不在，三关都过才用
                line = emotion_state.plan_for(emotion_store, user_id, live["topics"],
                                              watermark=stamp)
                if line:
                    state["plan_context"] = line
                emotion_state.schedule_prewarm(emotion_store, save_queue, save_worker,
                                               user_id, user_message, live["topics"])
                # 下面这两样是本地读出来的，拿来问"这轮还要不要为情绪现等一次模型"不花成本
                prepared = f"{state.get('plan_context', '')}{state.get('baseline_context', '')}"

            retrieval_task = asyncio.create_task(asyncio.to_thread(
                _run_memory_retrieval, initial_state, memory_manager, working_memory_store,
                profile_manager))

            if emotion_state.worth_waiting(live):
                # 用户 2026-09-28 拍板：医院/忌日/起冲突/落榜/马上答辩这几类，本轮值得等一次模型。
                # 但等要有上限——超时就先带着 L0 开口，别把"贴"变成"卡死"。
                if prepared:
                    # 不是"不等了"（上限一个字没动），是把"后台早备好了还去等"这种情况数出来：
                    # 2026-09-28 实测最长那轮 76.3s = 等满 45s 模型没回 + 才开口，白付。
                    emotion_state.bump_counter("sensitive_had_plan")
                yield {'type': 'status', 'text': '这事我认真想一想再说…'}
                emotion_state.bump_counter("sensitive_asked")
                wait_started = time.time()
                try:
                    corrected = await asyncio.wait_for(
                        asyncio.to_thread(emotion_analysis_node, dict(initial_state)),
                        timeout=SENSITIVE_WAIT_SECONDS)
                    if corrected.get("emotion_summary"):
                        model_label = str((corrected.get("emotion_analysis") or {}).get("current_emotion", ""))
                        agreed = emotion_state.record_agreement(live["current_emotion"], model_label)
                        # 等了半天模型给的档和规则档一样 = 这次等待没换来任何新信息
                        emotion_state.bump_counter(
                            "sensitive_agreed" if agreed else "sensitive_paid_off")
                        state.update(corrected)
                except asyncio.TimeoutError:
                    emotion_state.bump_counter("sensitive_timeout")
                    logger.info("[情感] 敏感主题等了 %.0fs 没等到，先用规则档开口", SENSITIVE_WAIT_SECONDS)
                except Exception as e:
                    emotion_state.bump_counter("sensitive_failed")
                    logger.warning("[情感] 敏感主题校正失败，先用规则档开口: %s", e)
                emotion_state.record_stage("sensitive_wait", time.time() - wait_started)
            else:
                yield {'type': 'status', 'text': 'moz 正在回忆…'}

            retrieve_waited = time.time()
            state.update(await retrieval_task)
            # 检索是并行跑的，这一段只记"它比敏感等待慢下来多少"——并行省掉的那部分不该算账
            emotion_state.record_stage("retrieval", time.time() - retrieve_waited)

            yield {'type': 'status', 'text': 'moz 正在组织语言...'}

            prompt_started = time.time()
            llm = get_chat_client(temperature=0.8, top_p=0.9, use_thinking=CHAT_USE_THINKING)
            messages = _build_dialogue_messages(state)
            emotion_state.record_stage("prompt_build", time.time() - prompt_started)
            stream_started = time.time()
            # local_prep 是"开口之前我们自己花的钱"，中转那一段单独记 relay_ttfb，两笔不许混
            emotion_state.record_stage("local_prep", stream_started - turn_started)

            # 中转偶尔**整条不回话**（第十九轮 18 次真请求里 2 次、第廿三轮 8 对里 3 次没回或 16 秒才回）。
            # 用户 2026-10-05 拍板"都要"：**一个字都没出去时，同一请求原样再发一次**——
            # 不换模型、不换渠道＝不换嗓子；已经吐过字的绝不重试（那等于同一句话说两遍）。
            # 实测过同渠道"补打一枪取快的那枪"没有收益（3244ms vs 3422ms，中转按 IP 排队），
            # 所以这里不是"抢快"，是"没回话时再多一次机会"，最坏情况多等一倍。
            reply = ""
            for attempt in (1, 2):
                full_reply = ""
                token_queue: asyncio.Queue = asyncio.Queue()
                stream_error = [None]
                # 每一枪单独计时：relay_ttfb 该是"出字那一枪"的中转延迟，不该把废掉那枪混进来。
                # 用户实际等了多久由 server.py 那个端到端 first_token 负责，两笔账不重复也不漏。
                stream_started = time.time()

                def _consume_stream():
                    nonlocal full_reply
                    try:
                        for chunk in llm.stream(messages):
                            token = chunk.content
                            if token:
                                full_reply += token
                                token_queue.put_nowait(token)
                    except Exception as e:
                        stream_error[0] = e
                    finally:
                        token_queue.put_nowait(None)

                stream_task = asyncio.create_task(asyncio.to_thread(_consume_stream))
                # 事件循环只握着任务的**弱引用**，废掉那一枪没人 await 就会被 GC
                # （"Task was destroyed but it is pending"）。扔进模块级集合，跑完自己摘掉。
                _orphan_streams.add(stream_task)
                stream_task.add_done_callback(_orphan_streams.discard)

                first_token_at = None
                got_any = False
                gap_timed_out = False
                while True:
                    try:
                        token = await asyncio.wait_for(token_queue.get(), timeout=120.0)
                    except asyncio.TimeoutError:
                        gap_timed_out = True
                        break
                    if token is None:
                        break
                    if first_token_at is None:
                        first_token_at = time.time()
                        # 中转那一段的到达延迟（旧口径唯一在量的数，单独进指标 relay_ttfb）
                        emotion_state.record_stage("relay_ttfb", first_token_at - stream_started)
                    got_any = True
                    yield {'type': 'token', 'text': token}

                if first_token_at is not None:
                    emotion_state.record_stage("generate", time.time() - first_token_at)

                if not got_any and attempt == 1:
                    # 这一枪整个废了。不 await 它：`future` 取消不了已经在飞的 HTTP（第十三轮那条老账），
                    # 慢中转能一句一句挤到几百秒，等它就等于把"再试一次"变成"更久不回话"。
                    reason = ("timeout" if gap_timed_out
                              else "error" if stream_error[0] else "empty")
                    emotion_state.note_retry(reason)
                    logger.warning("[重试] 第 1 枪一个字没出（%s），原样再发一次",
                                   "等了 120 秒没动静" if reason == "timeout"
                                   else (str(stream_error[0])[:80] if reason == "error" else "回话是空的"))
                    yield {'type': 'status', 'text': '刚才那句没接上，我再想一想…'}
                    continue

                if attempt == 2:
                    # 第二枪的结果单独记，`fired - recovered` 才是"再发一次也没救回来"
                    emotion_state.note_retry_outcome(
                        bool(full_reply.strip()) and not gap_timed_out and not stream_error[0])
                if gap_timed_out:
                    raise asyncio.TimeoutError()
                if stream_error[0]:
                    raise stream_error[0]
                reply = full_reply.strip()
                break

            reply = reply or "抱歉，我暂时无法回复。"

            yield {'type': 'reply', 'text': reply}

            # 这一轮要记的东西先落进持久化队列：后端重启也丢不掉
            _register_turn(reply)

            total_time = time.time() - (state.get("workflow_start_time") or time.time())
            logger.info(f"📊 流式工作流完成 (总耗时: {total_time:.2f}s)")

        except asyncio.TimeoutError:
            waited = time.time() - (state.get("workflow_start_time") or time.time())
            logger.error(f"流式工作流超时（这轮等了 {waited:.0f}s）")
            _register_turn("")      # 她没答上来，但用户那句话还是得记住
            yield {'type': 'error', 'text': friendly_llm_error(
                TimeoutError(), bool(state.get("image_data")), waited)}
        except Exception as e:
            logger.error(f"流式工作流失败: {e}", exc_info=True)
            _register_turn("")
            yield {'type': 'error', 'text': friendly_llm_error(e, bool(state.get("image_data")))}

    return _stream()


def _run_memory_retrieval(
    state: Dict,
    memory_manager: Optional[MemoryManager],
    working_memory_store: Optional[WorkingMemoryStore] = None,
    profile_manager=None,
) -> Dict:
    """独立运行记忆检索节点（供流式工作流调用）。全本地读，不调上游。"""
    user_id = state["user_id"]
    user_message = state["user_message"]

    # 加载工作记忆
    working_memory_text = ""
    if working_memory_store:
        working_memory_text = working_memory_store.format_for_prompt(user_id)

    # 档案卡：第廿四轮才接上。它一直只由那条"建好却从没被调用"的非流式图填，
    # 而 `_build_dialogue_messages` 读的是这个字段——等于她把档案卡存在库里、
    # 说话时一次都没用过（界面上「档案卡」那一栏当然是有内容的，更容易看不出来）。
    profile_context = ""
    if profile_manager:
        try:
            profile = profile_manager.get_profile(user_id)
            if profile:
                profile_context = profile.to_prompt_context()
        except Exception as e:
            logger.warning(f"[档案卡] 本轮没读出来（不影响回话）: {e}")

    # 查询改写留在这里不调：它是一次上游往返，而它在首字路径上从来没赢过——
    # 上限 1.2 秒（memory_manager.py:613），中转实测中位 25 秒，等于每轮先白等 1.2 秒
    # 再退回 `[原句]`，还顺手在后台占掉一次额度（future.cancel() 取消不了已经在跑的 HTTP）。
    # 本轮拿到的结果和以前一模一样，只是不再为它排队。非流式那条图（:535）不是用户在等的钟，留着。
    retrieved = []
    if memory_manager:
        local_results = memory_manager.search_memories(
            user_id,
            user_message,
            limit=10,
            conversation_id=state.get("conversation_id"),
        )
        retrieved = [{"memory": m.content, "source": "local"} for m in local_results]

    memory_texts = [r.get("memory", "") for r in retrieved if r.get("memory")]

    # 这里原来还要再请模型写一段"记忆摘要"，而且**没有超时**：2026-09-28 沙箱实测
    # 有 4 条记忆时那一步吃掉 178 秒，等于把整轮回复拖死。摘掉记忆本来就是按相关性
    # 排好序的原话，直接给对话模型看更好（也等于沿用它原本异常时的回落写法）。
    # 设计原则见 docs/情感预热系统设计.md：首字路径上不许有上游往返。
    if memory_texts:
        memory_context = "\n".join([f"- {m}" for m in memory_texts])
        memory_summary = f"想起 {len(memory_texts)} 件相关的事"
    else:
        memory_context = "暂无相关记忆"
        memory_summary = "新用户，暂无历史记忆"

    logger.info(f"✅ [记忆 Agent] 完成: {len(retrieved)} 条记忆")
    return {
        "retrieved_memories": retrieved,
        "memory_context": memory_context,
        "memory_summary": memory_summary,
        "working_memory_text": working_memory_text,
        "profile_context": profile_context,
        "workflow_log": [f"[记忆检索] {len(retrieved)} 条相关记忆"],
    }

"""
moz - AI 情感陪伴助手（FastAPI 后端）

启动:
    uvicorn server:app --host 127.0.0.1 --port 8000 --reload
"""

import os
import sys
import uuid
import json
import asyncio
import base64
import io
import time
import datetime
import logging
import re
import httpx
from typing import List, Dict, Literal, Optional, Any
from contextlib import asynccontextmanager
from collections import defaultdict, deque

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends, Security, Request
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

load_dotenv()
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# ruff: noqa: E402
from llm_config import get_llm_client
from model_config import CHAT_MODEL, CHAT_BASE_URL, get_chat_api_key, detect_provider, resolve_api_key, PROVIDER_KEY_MAP
from memory_manager import EmotionType, MemoryCategory, MemoryManager
from working_memory import WorkingMemoryStore
from emotion_graph import build_emotion_graph, run_emotion_workflow_streaming, load_prompt_config, save_prompt_config, get_dialogue_prompt, DIALOGUE_AGENT_PROMPT
from emotion_graph import SaveDeps, run_save_job
from save_queue import SaveQueue, SaveWorker
from summary_service import SummaryService
from memory_consolidation import MemoryConsolidator
from file_processor import is_multimodal_model
from care_store import CareStore
import care_engine
import care_graph
from conversation_store import ConversationStore

# ================================================================
# 配置
# ================================================================

logger = logging.getLogger("server")

CHAT_API_KEY = ""
try:
    CHAT_API_KEY = get_chat_api_key()
except ValueError:
    pass
DEFAULT_USER_ID = "web_user_001"

MOZ_ACCESS_KEY = os.environ.get("MOZ_ACCESS_KEY", "")
MOZ_ADMIN_KEY = os.environ.get("MOZ_ADMIN_KEY", "")
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
MOZ_ALLOWED_USER_IDS = {
    item.strip()
    for item in os.environ.get("MOZ_ALLOWED_USER_IDS", "").split(",")
    if item.strip()
}

_access_key_header = APIKeyHeader(name="X-Access-Key", auto_error=False)
_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

class MemoryLogHandler(logging.Handler):
    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.buffer: deque = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append(self.format(record))

    def get_logs(self) -> list[dict]:
        return [{"timestamp": i, "level": r.split("[")[1].split("]")[0] if "[" in r else "INFO", "message": r} for i, r in enumerate(self.buffer)]

_log_handler = MemoryLogHandler(maxlen=500)
logging.getLogger().addHandler(_log_handler)
logging.getLogger().setLevel(logging.INFO)

# ================================================================
# Metrics（可观测性）
# ================================================================

_metrics = {
    "requests_total": defaultdict(int),       # endpoint -> count
    "requests_errors": defaultdict(int),       # endpoint -> count
    "requests_duration_sum": defaultdict(float),  # endpoint -> total seconds
    "llm_calls_total": 0,
    "llm_calls_duration_sum": 0.0,
    "llm_tokens_total": 0,
    "start_time": time.time(),
}


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录每个请求的耗时和状态码。"""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start

        path = request.url.path
        # 归一化带路径参数的端点
        for prefix in ("/api/chat/", "/api/conversations/", "/api/memory/"):
            if path.startswith(prefix):
                parts = path[len(prefix):].strip("/").split("/")
                if len(parts) >= 1:
                    path = f"{prefix}{{id}}"
                break

        _metrics["requests_total"][path] += 1
        _metrics["requests_duration_sum"][path] += duration
        if response.status_code >= 400:
            _metrics["requests_errors"][path] += 1

        return response

# ================================================================
# 配置
# ================================================================

# ================================================================
# 应用生命周期
# ================================================================

_app_state: Dict[str, Any] = {}

def _validate_env_on_startup():
    provider = detect_provider()
    key_name = PROVIDER_KEY_MAP.get(provider, "API_KEY")
    if not CHAT_API_KEY:
        logger.error(f"未找到 Chat API Key。请在 .env 中设置 {key_name}=your_api_key（当前模型: {CHAT_MODEL}，提供商: {provider}）")
        sys.exit(1)

    embedding_key = os.environ.get("ZHIPU_API_KEY", "") or os.environ.get("ZhipuAI_API_KEY", "")
    if not embedding_key:
        logger.warning("未配置 Embedding API Key，语义检索将降级为关键词匹配")

    raw_origins = os.environ.get("CORS_ORIGINS", "")
    if raw_origins:
        origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
        invalid = [o for o in origins if not (o.startswith("http://") or o.startswith("https://"))]
        if invalid:
            logger.warning(f"CORS_ORIGINS 包含无效格式: {invalid}")


def _log_startup_summary():
    features = []
    features.append(f"Chat: {CHAT_MODEL} (key: {'已配置' if CHAT_API_KEY else '未配置'})")
    features.append("记忆: 本地记忆系统")
    features.append(f"多模态: {'支持' if _app_state.get('multimodal') else '不支持'}")
    features.append(f"认证: {'已启用' if MOZ_ACCESS_KEY else '未启用'}")
    logger.info("启动配置摘要: " + " | ".join(features))

@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_env_on_startup()

    logger.info("正在初始化记忆管理器...")
    mm = MemoryManager(storage_path=os.path.join(os.path.dirname(__file__), "memory_store"))
    _app_state["memory_manager"] = mm
    _app_state["conversation_store"] = ConversationStore()
    _app_state["working_memory_store"] = WorkingMemoryStore()
    _app_state["summary_service"] = SummaryService()
    memory_manager_ref = _app_state.get("memory_manager")
    if memory_manager_ref:
        _app_state["consolidator"] = MemoryConsolidator(memory_manager_ref)

    # 初始化档案卡管理器
    from user_profile import ProfileManager
    db_path = os.path.join(os.path.dirname(__file__), "moz.db")
    profile_manager = ProfileManager(db_path)
    _app_state["profile_manager"] = profile_manager

    graph = build_emotion_graph(
        memory_manager=mm,
        working_memory_store=_app_state["working_memory_store"],
        profile_manager=profile_manager,
    )
    _app_state["emotion_graph"] = graph

    _app_state["multimodal"] = is_multimodal_model()
    logger.info(f"多模态支持: {_app_state['multimodal']}")

    load_prompt_config()
    _log_startup_summary()

    # 主动关心心跳：只有后端常驻才有"时间流逝"，托盘和前端都只读队列
    _app_state["care_store"] = CareStore()
    # 事件关联图：把"记下的事"和"记得的事实"用人/主题连起来，心跳顺带增量刷新
    _app_state["care_graph"] = care_graph.CareGraph()

    # 一轮后台落库要跑几分钟模型调用；改成持久化队列，后端重启不再"聊完白聊"
    _app_state["save_queue"] = SaveQueue(db_path)
    replayed = _app_state["save_queue"].recover()
    _app_state["save_queue"].prune()
    if replayed:
        logger.info("发现 %d 轮上次没落完的对话，正在补记", replayed)
    save_deps = SaveDeps(
        memory_manager=mm,
        working_memory_store=_app_state["working_memory_store"],
        profile_manager=profile_manager,
        care_store=_app_state["care_store"],
        care_graph=_app_state["care_graph"],
    )
    save_worker = SaveWorker(_app_state["save_queue"], lambda job: run_save_job(save_deps, job))
    _app_state["save_worker"] = save_worker
    save_task = asyncio.create_task(save_worker.run())
    care_task = asyncio.create_task(care_engine.care_loop(
        _app_state["care_store"],
        _app_state["working_memory_store"],
        DEFAULT_USER_ID,
        persona_getter=get_dialogue_prompt,
        graph=_app_state["care_graph"],
        memory_manager=_app_state["memory_manager"],
    ))

    logger.info("moz 后端服务已启动")
    yield

    care_task.cancel()
    save_worker.stop()
    save_task.cancel()
    # 新增：关闭档案卡管理器
    profile_manager.close()
    logger.info("moz 后端服务已停止")

app = FastAPI(title="moz API", version="1.0.0", lifespan=lifespan)

CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# 速率限制
# ================================================================

class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        self._requests[key] = [t for t in self._requests[key] if t > cutoff]
        if len(self._requests[key]) >= self.max_requests:
            return False
        self._requests[key].append(now)
        return True

# 本地单机应用：正常翻界面、开日志、20s 一次轮询就会上百次请求，
# 限流只该防"前端死循环"，不该防用户手快
_chat_limiter = RateLimiter(max_requests=60, window_seconds=60)
_default_limiter = RateLimiter(max_requests=300, window_seconds=60)

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "/api/chat" not in request.url.path:
            client_ip = request.client.host if request.client else "unknown"
            if not _default_limiter.is_allowed(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "这一分钟请求太密了，等几十秒再试就好"},
                )
        response = await call_next(request)
        return response

app.add_middleware(MetricsMiddleware)
app.add_middleware(RateLimitMiddleware)

async def rate_limit_chat(user_id: str):
    if not _chat_limiter.is_allowed(user_id):
        raise HTTPException(status_code=429, detail="这一分钟说得有点多，等几十秒再发一条")

# ================================================================
# Pydantic 模型
# ================================================================

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000, description="用户消息")
    conversation_id: Optional[str] = Field(None, description="当前对话 ID，不传则自动创建")
    conversation_history: List[Dict] = Field(default_factory=list, description="对话历史")
    image_data: Optional[str] = Field(None, description="图片 base64 data URL")

class ChatEvent(BaseModel):
    type: str
    text: Optional[str] = None

class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)

class PromptUpdateRequest(BaseModel):
    prompt: str = Field(..., min_length=0, max_length=5000, description="自定义对话 prompt，空字符串恢复默认")

class MemoryCorrectionRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    category: Optional[str] = None
    emotion: Optional[str] = None
    emotion_intensity: Optional[float] = Field(default=None, ge=0, le=1)

class MemoryFeedbackRequest(BaseModel):
    feedback: Literal["helpful", "irrelevant", "wrong", "outdated"]

class MemoryGradeRequest(BaseModel):
    grade: int = Field(..., ge=0, le=4)
    locked: bool = True

# ================================================================
# 辅助函数
# ================================================================

async def verify_access_key(api_key: str = Security(_access_key_header)):
    if not MOZ_ACCESS_KEY:
        return True
    if not api_key or api_key != MOZ_ACCESS_KEY:
        raise HTTPException(status_code=401, detail="无效的访问密钥")
    return True

async def verify_admin_key(api_key: str = Security(_admin_key_header)):
    if not MOZ_ADMIN_KEY:
        if MOZ_ACCESS_KEY:
            if not api_key or api_key != MOZ_ACCESS_KEY:
                raise HTTPException(status_code=403, detail="需要管理员密钥")
        return True
    if not api_key or api_key != MOZ_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="需要管理员密钥")
    return True

def normalize_user_id(user_id: str) -> str:
    """校验外部用户 ID，避免任意标识进入存储与文件路径。"""
    normalized = (user_id or "").strip() or DEFAULT_USER_ID
    if not USER_ID_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="用户 ID 仅支持字母、数字、下划线和短横线，长度 1-50")
    allowed_users = MOZ_ALLOWED_USER_IDS or {DEFAULT_USER_ID}
    if normalized not in allowed_users:
        raise HTTPException(status_code=403, detail="用户不在允许列表中")
    return normalized

def get_all_users() -> List[str]:
    mem_store = os.path.join(os.path.dirname(__file__), "memory_store")
    if not os.path.exists(mem_store):
        return [DEFAULT_USER_ID]
    users = []
    for filename in os.listdir(mem_store):
        if filename.endswith(".json"):
            users.append(filename[:-5])
    return sorted(users) if users else [DEFAULT_USER_ID]

def _ensure_conversation(user_id: str) -> tuple:
    store: ConversationStore = _app_state["conversation_store"]
    convs, current_id = store.load(user_id)
    if not convs:
        convs = {}
        cid = str(uuid.uuid4())
        convs[cid] = {
            "title": "新对话",
            "messages": [],
            "created": datetime.datetime.now().strftime("%m/%d %H:%M"),
        }
        current_id = cid
        store.save(user_id, convs, current_id)
    elif current_id not in convs:
        current_id = list(convs.keys())[-1]
        store.save(user_id, convs, current_id)
    return convs, current_id

_TURN_LOCKS: Dict[str, "asyncio.Lock"] = {}

# 一张手机照片的 data URL 能有 1MB，全存进会话文件很快就几十 MB；历史也不该无限长
MAX_MESSAGES_PER_CONVERSATION = 300
_THUMB_MAX_SIDE = 768


def _thumbnail_data_url(data_url: str, max_side: int = _THUMB_MAX_SIDE, quality: int = 72) -> str:
    """把要长期存进会话的图片压成缩略图；压不动就原样返回，不因为省空间而丢图。"""
    try:
        raw = (data_url or "").strip()
        if not raw.startswith("data:image"):
            return raw
        payload = raw.split(",", 1)[1]
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(payload)))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        logger.warning("[图片] 缩略图生成失败，按原样保存: %s", e)
        return data_url


def _turn_lock(user_id: str) -> "asyncio.Lock":
    """同一个用户一次只跑一轮对话：并行两轮会互相覆盖会话与记忆。"""
    lock = _TURN_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _TURN_LOCKS[user_id] = lock
    return lock


def _append_assistant_message(user_id: str, text: str) -> None:
    """把主动关心说过的话写进当前对话：不然历史里查不到，模型下一轮也不记得自己说过。"""
    text = (text or "").strip()
    if not text:
        return
    store: ConversationStore = _app_state["conversation_store"]
    with store.locked(user_id):
        convs, current_id = _ensure_conversation(user_id)
        conv = convs.get(current_id)
        if conv is None:
            conv = {
                "title": text[:20] or "新对话",
                "messages": [],
                "created": datetime.datetime.now().strftime("%m/%d %H:%M"),
            }
            convs[current_id] = conv
        conv.setdefault("messages", []).append({"role": "assistant", "content": text})
        store.save(user_id, convs, current_id)
    care_engine.note_bot_reply(user_id)  # 主动说出去的也算"moz 刚回过"


# ================================================================
# API: 对话
# ================================================================

@app.post("/api/chat/{user_id}", dependencies=[Depends(verify_access_key), Depends(rate_limit_chat)])
async def chat(user_id: str, req: ChatRequest):
    if not CHAT_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 LLM_API_KEY")

    user_id = normalize_user_id(user_id)

    # 主动关心要避开"用户正在聊"的时段；顺手把 TA 的话多/话少学进数据库
    gap_seconds = care_engine.note_user_activity(user_id)
    care_store: Optional[CareStore] = _app_state.get("care_store")
    if care_store:
        care_store.observe_style(
            user_id,
            len(req.message or ""),
            since_user_msg=gap_seconds,
            since_bot_reply=care_engine.seconds_since_bot_reply(user_id),
        )

    store: ConversationStore = _app_state["conversation_store"]
    with store.locked(user_id):
        _, current_id = _ensure_conversation(user_id)
        cid = req.conversation_id or current_id

    async def event_stream():
        lock = _turn_lock(user_id)
        errored = False     # 中转报错那一轮：记到该记的为止，最后不发 done
        if lock.locked():
            # 排队是常态（连点两次、托盘也发一条），别让界面看起来像卡死
            yield f"data: {json.dumps({'type': 'status', 'text': '上一条还在收尾，等一下'})}\n\n"
        await lock.acquire()
        try:
            reply = ""
            async for chunk in run_emotion_workflow_streaming(
                memory_manager=_app_state.get("memory_manager"),
                user_id=user_id,
                user_message=req.message,
                conversation_history=req.conversation_history,
                image_data=req.image_data,
                working_memory_store=_app_state.get("working_memory_store"),
                profile_manager=_app_state.get("profile_manager"),
                care_store=_app_state.get("care_store"),
                care_graph=_app_state.get("care_graph"),
                conversation_id=cid,
                save_queue=_app_state.get("save_queue"),
                save_worker=_app_state.get("save_worker"),
            ):
                chunk_type = chunk.get("type")
                if chunk_type == "status":
                    yield f"data: {json.dumps({'type': 'status', 'text': chunk.get('text', '')})}\n\n"
                elif chunk_type == "token":
                    reply += chunk.get("text", "")
                    yield f"data: {json.dumps({'type': 'token', 'text': chunk.get('text', '')})}\n\n"
                elif chunk_type == "reply":
                    reply = chunk.get("text", reply)
                    yield f"data: {json.dumps({'type': 'reply', 'text': reply})}\n\n"
                elif chunk_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'text': chunk.get('text', '')})}\n\n"
                    # 别在这里 return：中转挂了这一轮也算说过话，用户那句得留在对话记录里
                    errored = True
                    break

            store: ConversationStore = _app_state["conversation_store"]
            with store.locked(user_id):
                convs, current_id = _ensure_conversation(user_id)

                active_cid = req.conversation_id or current_id
                if active_cid not in convs:
                    active_cid = str(uuid.uuid4())
                    convs[active_cid] = {
                        "title": req.message[:20] or "新对话",
                        "messages": [],
                        "created": datetime.datetime.now().strftime("%m/%d %H:%M"),
                    }

                conv = convs[active_cid]
                user_msg = {"role": "user", "content": req.message}
                if req.image_data:
                    # 存缩略图，原始大图只用于这一轮请求：否则会话文件很快就几十 MB
                    user_msg["image"] = _thumbnail_data_url(req.image_data)
                conv["messages"].append(user_msg)
                if reply.strip():
                    conv["messages"].append({"role": "assistant", "content": reply})

                if len(conv["messages"]) > MAX_MESSAGES_PER_CONVERSATION:
                    conv["messages"] = conv["messages"][-MAX_MESSAGES_PER_CONVERSATION:]

                if len(conv["messages"]) <= 2:
                    conv["title"] = req.message[:20] or "新对话"

                store.save(user_id, convs, active_cid)

            if reply.strip():
                care_engine.note_bot_reply(user_id)  # 用户接话快不快，从这里起算
            if errored:
                return   # 客户端已经收到 error，不再补 done，免得前端当成正常收工
            yield f"data: {json.dumps({'type': 'done', 'conversation_id': active_cid})}\n\n"

        except Exception as e:
            logger.error(f"对话处理失败: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'text': '对话处理失败，请稍后重试'})}\n\n"
        finally:
            lock.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

# ================================================================
# API: 会话管理
# ================================================================

@app.get("/api/conversations/{user_id}", dependencies=[Depends(verify_access_key)])
async def get_conversations(user_id: str):
    user_id = normalize_user_id(user_id)
    store: ConversationStore = _app_state["conversation_store"]
    with store.locked(user_id):
        convs, current_id = _ensure_conversation(user_id)
        items = []
        for cid, cd in convs.items():
            items.append({
                "id": cid,
                "title": cd.get("title", "新对话"),
                "created": cd.get("created", ""),
                "message_count": len(cd.get("messages", [])),
                "is_active": cid == current_id,
            })
        items.sort(key=lambda x: x["created"], reverse=True)
        return {"conversations": items, "current_id": current_id}

@app.get("/api/conversations/{user_id}/{conv_id}", dependencies=[Depends(verify_access_key)])
async def get_conversation(user_id: str, conv_id: str):
    user_id = normalize_user_id(user_id)
    store: ConversationStore = _app_state["conversation_store"]
    with store.locked(user_id):
        convs, _ = _ensure_conversation(user_id)
        if conv_id not in convs:
            raise HTTPException(status_code=404, detail="对话不存在")
        return convs[conv_id]

@app.post("/api/conversations/{user_id}", dependencies=[Depends(verify_access_key)])
async def create_conversation(user_id: str):
    store: ConversationStore = _app_state["conversation_store"]
    user_id = normalize_user_id(user_id)
    with store.locked(user_id):
        convs, _ = _ensure_conversation(user_id)
        cid = str(uuid.uuid4())
        convs[cid] = {
            "title": "新对话",
            "messages": [],
            "created": datetime.datetime.now().strftime("%m/%d %H:%M"),
        }
        store.save(user_id, convs, cid)
        return {"id": cid, "title": "新对话", "created": convs[cid]["created"]}

@app.patch("/api/conversations/{user_id}/{conv_id}", dependencies=[Depends(verify_access_key)])
async def rename_conversation(user_id: str, conv_id: str, req: RenameRequest):
    store: ConversationStore = _app_state["conversation_store"]
    user_id = normalize_user_id(user_id)
    title = req.title.strip()
    with store.locked(user_id):
        convs, current_id = _ensure_conversation(user_id)
        if conv_id not in convs:
            raise HTTPException(status_code=404, detail="对话不存在")
        convs[conv_id]["title"] = title
        store.save(user_id, convs, current_id)
        return {"ok": True, "title": title}

@app.delete("/api/conversations/{user_id}/{conv_id}", dependencies=[Depends(verify_access_key)])
async def delete_conversation(user_id: str, conv_id: str):
    store: ConversationStore = _app_state["conversation_store"]
    user_id = normalize_user_id(user_id)
    now = datetime.datetime.now().strftime("%m/%d %H:%M")
    with store.locked(user_id):
        convs, current_id = _ensure_conversation(user_id)
        if conv_id not in convs:
            raise HTTPException(status_code=404, detail="对话不存在")
        del convs[conv_id]
        if not convs:
            cid = str(uuid.uuid4())
            convs[cid] = {"title": "新对话", "messages": [], "created": now}
            current_id = cid
        elif current_id == conv_id:
            current_id = list(convs.keys())[-1]
        store.save(user_id, convs, current_id)
        return {"ok": True, "current_id": current_id}

# ================================================================
# API: 用户管理
# ================================================================

@app.get("/api/users", dependencies=[Depends(verify_access_key)])
async def get_users():
    mm: MemoryManager = _app_state["memory_manager"]
    store: ConversationStore = _app_state["conversation_store"]
    memory_users = set(mm.get_all_user_ids())
    conv_users = set(store.get_user_ids())
    all_users = sorted(memory_users | conv_users)
    if not all_users:
        all_users = [DEFAULT_USER_ID]
    result = []
    for u in all_users:
        stats = mm.get_memory_stats(u)
        result.append({
            "id": u,
            "memory_count": stats.get("total", 0),
            "consolidated_count": stats.get("consolidated_count", 0),
            "avg_importance": stats.get("avg_importance", 0),
        })
    return result

@app.post("/api/users", dependencies=[Depends(verify_access_key)])
async def create_user(user_id: str = Query(..., min_length=1, max_length=50)):
    user_id = normalize_user_id(user_id)
    store: ConversationStore = _app_state["conversation_store"]
    with store.locked(user_id):
        _ensure_conversation(user_id)
    _ensure_conversation(user_id)
    return {"ok": True, "user_id": user_id}

# ================================================================
# API: 记忆管理
# ================================================================

@app.get("/api/memory/{user_id}/stats", dependencies=[Depends(verify_access_key)])
async def get_memory_stats(user_id: str):
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    return mm.get_memory_stats(user_id)

@app.delete("/api/memory/{user_id}", dependencies=[Depends(verify_access_key)])
async def clear_memories(user_id: str):
    """清空该用户的全部记忆资产：分级记忆、总结金字塔、工作记忆、档案卡。"""
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    mm.delete_user_memories(user_id)
    mem_file = os.path.join(os.path.dirname(__file__), "memory_store", f"{user_id}.json")
    if os.path.exists(mem_file):
        os.remove(mem_file)

    removed_summaries = 0
    summary_service: SummaryService = _app_state.get("summary_service")
    if summary_service:
        removed_summaries = summary_service.clear(user_id)
    wm: WorkingMemoryStore = _app_state.get("working_memory_store")
    if wm:
        wm.clear(user_id)
    pm = _app_state.get("profile_manager")
    if pm:
        pm.delete_profile(user_id)

    # 主动关心记下的事也在 moz.db 里；不清的话"全部清空"会留下生日和提醒
    care: Optional[CareStore] = _app_state.get("care_store")
    removed_care = care.clear_user(user_id) if care else 0
    # 事项没了，它们之间的关联也必须跟着没，否则清空后还能从图里翻出旧事
    links = _app_state.get("care_graph")
    removed_links = links.clear_user(user_id) if links else 0

    logger.info("已清空用户 %s 的记忆/总结/工作记忆/档案卡/关心事项", user_id)
    return {"ok": True, "summaries_removed": removed_summaries, "care_removed": removed_care,
            "links_removed": removed_links}

# ================================================================
# API: 用户档案卡
# ================================================================

@app.get("/api/profile/{user_id}", dependencies=[Depends(verify_access_key)])
async def get_user_profile(user_id: str):
    user_id = normalize_user_id(user_id)


    from user_profile import UserProfile
    profile_manager = _app_state["profile_manager"]
    profile = profile_manager.get_profile(user_id)
    
    if not profile:
        profile = UserProfile(user_id=user_id)
    
    return {
        "user_id": user_id,
        "profile": profile.to_dict(),
        "prompt_context": profile.to_prompt_context(),
        "version": profile.version,
    }


@app.put("/api/profile/{user_id}", dependencies=[Depends(verify_access_key)])
async def update_user_profile(user_id: str, updates: Dict):
    user_id = normalize_user_id(user_id)


    from user_profile import UserProfile
    profile_manager = _app_state["profile_manager"]
    profile = profile_manager.get_profile(user_id) or UserProfile(user_id=user_id)
    allowed_fields = {"identity", "preferences", "relationships", "emotional_profile"}
    unknown_fields = set(updates) - allowed_fields
    if unknown_fields:
        raise HTTPException(status_code=400, detail=f"不支持的字段: {', '.join(sorted(unknown_fields))}")
    
    for field_name, value in updates.items():
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail=f"{field_name} 必须是对象")
        setattr(profile, field_name, value)
    
    profile.version += 1
    profile_manager.save_profile(profile)
    
    return {
        "ok": True,
        "updated_fields": list(updates.keys()),
        "version": profile.version,
    }

# ================================================================
# API: 记忆分层
# ================================================================

@app.get("/api/memory/{user_id}/layers", dependencies=[Depends(verify_access_key)])
async def get_memory_layers(user_id: str):
    """获取各层级记忆统计"""
    mm: MemoryManager = _app_state["memory_manager"]
    
    stats = {
        "core": {"count": 0, "sample": []},
        "important": {"count": 0, "sample": []},
        "regular": {"count": 0, "sample": []},
    }
    
    for memory_id, memory in mm.get_user_memory_items(user_id):
        layer = memory.layer if hasattr(memory, "layer") else 3
        layer_name = {1: "core", 2: "important", 3: "regular"}[layer]
        stats[layer_name]["count"] += 1
        
        if len(stats[layer_name]["sample"]) < 3:
            stats[layer_name]["sample"].append({
                "id": memory_id,
                "content": memory.content[:50],
                "importance": memory.importance,
            })
    
    return stats


@app.patch("/api/memory/{user_id}/{memory_id}/layer", dependencies=[Depends(verify_access_key)])
async def update_memory_layer(user_id: str, memory_id: str, new_layer: int):
    """手动调整记忆层级"""
    if new_layer not in [1, 2, 3]:
        raise HTTPException(status_code=400, detail="无效的层级")
    
    mm: MemoryManager = _app_state["memory_manager"]
    if not mm.set_memory_layer(user_id, memory_id, new_layer):
        raise HTTPException(status_code=404, detail="记忆不存在")
    
    return {
        "ok": True,
        "memory_id": memory_id,
        "new_layer": new_layer,
    }


@app.get("/api/memory/{user_id}/detail", dependencies=[Depends(verify_access_key)])
async def get_memory_detail(user_id: str):
    """返回用户所有记忆的完整信息，按层级分组"""
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    working_memory_store: WorkingMemoryStore = _app_state.get("working_memory_store")

    layers: Dict[str, list] = {"core": [], "important": [], "regular": []}
    for mid, m in mm.get_user_memory_items(user_id):
        layer = m.layer if hasattr(m, "layer") else 3
        key = {1: "core", 2: "important", 3: "regular"}.get(layer, "regular")
        layers[key].append({
            "id": mid,
            "content": m.content,
            "emotion": m.emotion.value,
            "emotion_emoji": m.emotion.to_emoji(),
            "emotion_intensity": m.emotion_intensity,
            "category": m.category.value,
            "importance": m.importance,
            "access_count": m.access_count,
            "created_at": m.created_at,
            "last_accessed": m.last_accessed,
            "is_consolidated": m.is_consolidated,
            "tags": m.tags,
            "temporal_data": m.temporal_data if hasattr(m, "temporal_data") else {},
            "status": m.status.value,
            "grade": int(m.grade),
            "degree_score": m.degree_score,
            "confidence": m.confidence,
            "source_type": m.source_type,
            "mention_count": m.mention_count,
            "independent_conversation_count": m.independent_conversation_count,
            "useful_count": m.useful_count,
            "harmful_count": m.harmful_count,
            "locked": m.locked,
            "version": m.version,
            "updated_at": m.updated_at,
            "regrade_reason": m.regrade_reason,
        })

    # 按创建时间倒序
    for key in layers:
        layers[key].sort(key=lambda x: x["created_at"], reverse=True)

    # 获取工作记忆
    wm_data = {"summary": "", "open_topics": [], "current_emotion": "neutral", "updated_at": 0}
    if working_memory_store:
        wm_data = working_memory_store.load(user_id)

    return {
        "layers": layers,
        "working_memory": {
            "summary": wm_data["summary"],
            "open_topics": wm_data["open_topics"],
            "current_emotion": wm_data["current_emotion"],
            "updated_at": wm_data["updated_at"],
        }
    }


@app.get("/api/memory/{user_id}/{memory_id}", dependencies=[Depends(verify_access_key)])
async def get_memory_item(user_id: str, memory_id: str):
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    memory = mm.get_memory(user_id, memory_id)
    if memory is None or not memory.active():
        raise HTTPException(status_code=404, detail="记忆不存在")
    return memory.public_dict()


@app.post("/api/memory/{user_id}/{memory_id}/correct", dependencies=[Depends(verify_access_key)])
async def correct_memory_item(user_id: str, memory_id: str, req: MemoryCorrectionRequest):
    user_id = normalize_user_id(user_id)
    try:
        category = MemoryCategory(req.category) if req.category else None
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的记忆分类")
    emotion = EmotionType.from_string(req.emotion) if req.emotion else None
    try:
        mm: MemoryManager = _app_state["memory_manager"]
        replacement = mm.correct_memory(
            user_id,
            memory_id,
            req.content,
            category=category,
            emotion=emotion,
            emotion_intensity=req.emotion_intensity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "memory": replacement.public_dict()}


@app.delete("/api/memory/{user_id}/{memory_id}", dependencies=[Depends(verify_access_key)])
async def delete_memory_item(user_id: str, memory_id: str):
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    if not mm.soft_delete_memory(user_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


@app.post("/api/memory/{user_id}/{memory_id}/feedback", dependencies=[Depends(verify_access_key)])
async def feedback_memory_item(user_id: str, memory_id: str, req: MemoryFeedbackRequest):
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    memory = mm.apply_feedback(user_id, memory_id, req.feedback)
    if memory is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True, "memory": memory.public_dict()}


@app.get("/api/memory/{user_id}/{memory_id}/history", dependencies=[Depends(verify_access_key)])
async def get_memory_history(user_id: str, memory_id: str, limit: int = Query(default=100, ge=1, le=500)):
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    return {"events": mm.get_grade_history(user_id, memory_id, limit)}


@app.get("/api/summaries/{user_id}", dependencies=[Depends(verify_access_key)])
async def get_summaries(user_id: str):
    summary_service: SummaryService = _app_state.get("summary_service")
    if not summary_service:
        return {"summaries": []}
    return {"summaries": summary_service.get_recent_summaries(user_id)}



@app.post("/api/memory/{user_id}/consolidate", dependencies=[Depends(verify_access_key)])
async def consolidate_memories(user_id: str):
    consolidator = _app_state.get("consolidator")
    if not consolidator:
        return {"status": "unavailable", "merged": 0}
    count = consolidator.consolidate_user(user_id)

    cascade = {"weeklies": 0, "monthlies": 0}
    summary_service: SummaryService = _app_state.get("summary_service")
    if summary_service:
        try:
            cascade = summary_service.maybe_cascade(user_id)
        except Exception as e:
            logger.warning("[总结级联] 手动触发失败: %s", e)

    return {"status": "ok", "merged": count, **cascade}

# ================================================================
# API: 主动关心（个人关心数据库）
# ================================================================

class CareItemRequest(BaseModel):
    title: str
    kind: Literal["birthday", "event", "promise", "checkin", "health", "person", "note"] = "event"
    detail: str = ""
    due_at: float = 0
    repeat: Literal["none", "daily", "weekly", "yearly"] = "none"


class CareItemPatch(BaseModel):
    title: Optional[str] = None
    kind: Optional[str] = None
    detail: Optional[str] = None
    due_at: Optional[float] = None
    repeat: Optional[str] = None
    status: Optional[str] = None


class CareAckRequest(BaseModel):
    ids: List[str] = []


@app.get("/api/care/items", dependencies=[Depends(verify_access_key)])
async def list_care_items(user_id: str, status: str = Query("active")):
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    items = store.list_items(user_id, status=None if status == "all" else status)
    return {"items": items}


@app.post("/api/care/items", dependencies=[Depends(verify_access_key)])
async def add_care_item(user_id: str, req: CareItemRequest):
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    try:
        item = store.add_item(user_id, title=req.title, kind=req.kind, detail=req.detail,
                              due_at=req.due_at, repeat=req.repeat)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _sync_care_node(user_id, item)
    return {"ok": True, "item": item}


@app.patch("/api/care/items/{item_id}", dependencies=[Depends(verify_access_key)])
async def patch_care_item(user_id: str, item_id: str, req: CareItemPatch):
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    item = store.update_item(user_id, item_id, req.dict(exclude_none=True))
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    _sync_care_node(user_id, item)
    return {"ok": True, "item": item}


@app.delete("/api/care/items/{item_id}", dependencies=[Depends(verify_access_key)])
async def remove_care_item(user_id: str, item_id: str):
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    deleted = store.delete_item(user_id, item_id)
    link_graph = _app_state.get("care_graph")
    if deleted and link_graph:
        link_graph.forget(user_id, care_graph.ITEM, item_id)
    return {"ok": deleted}


def _sync_care_node(user_id: str, item: dict) -> None:
    """手动录的事当场就该能连上：等心跳要最多一分钟，用户会觉得"我明明刚说过"。"""
    link_graph = _app_state.get("care_graph")
    if not link_graph:
        return
    try:
        if item.get("status", "active") == "active":
            link_graph.sync_item(user_id, item)
        else:
            link_graph.forget(user_id, care_graph.ITEM, item["id"])
    except Exception as e:
        logger.warning("[事件关联] 同步事项失败（不影响事项本身）: %s", e)


@app.get("/api/care/related", dependencies=[Depends(verify_access_key)])
async def list_care_related(user_id: str, type: str = Query("item"), id: str = Query(...),
                            limit: int = Query(5, ge=1, le=20)):
    """一件事的相关事/相关事实：前端"这件事还连着什么"就吃这个接口。"""
    user_id = normalize_user_id(user_id)
    link_graph = _app_state.get("care_graph")
    if not link_graph:
        return {"related": []}
    return {"related": link_graph.related(user_id, type, id, limit=limit)}


@app.get("/api/care/graph", dependencies=[Depends(verify_access_key)])
async def get_care_graph(user_id: str):
    """关联图快照（调试用）：枢纽、边、增量水位。"""
    user_id = normalize_user_id(user_id)
    link_graph: care_graph.CareGraph = _app_state["care_graph"]
    return {"stats": link_graph.stats(user_id), **link_graph.describe(user_id)}


@app.post("/api/care/graph/sync", dependencies=[Depends(verify_access_key)])
async def sync_care_graph(user_id: str):
    user_id = normalize_user_id(user_id)
    link_graph: care_graph.CareGraph = _app_state["care_graph"]
    return await asyncio.to_thread(
        link_graph.sync_all, _app_state["care_store"], _app_state.get("memory_manager"), user_id)


@app.get("/api/care/settings", dependencies=[Depends(verify_access_key)])
async def get_care_settings(user_id: str):
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    # 顺带给换算结果：界面要显示"一天最多主动找你几次"，而不是 talk_score 那种内部数字
    return {**store.get_settings(user_id), "budget_today": store.daily_budget(user_id)}


@app.put("/api/care/settings", dependencies=[Depends(verify_access_key)])
async def update_care_settings(user_id: str, patch: dict):
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    saved = store.save_settings(user_id, patch or {})
    return {**saved, "budget_today": store.daily_budget(user_id)}


@app.get("/api/care/pending", dependencies=[Depends(verify_access_key)])
async def get_pending_proactive(user_id: str):
    """待读的主动关心消息。托盘和前端都从这里取。

    来取 = 这一刻有人听得见，这是主动关心敢不敢开口的依据；顺手把已经说不出口
    的话清出队列（隔了天的「今天」、放了两小时的问候），只出队、不写进对话。
    """
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    care_engine.note_poll(user_id)
    stale = store.stale_ids(user_id)
    if stale:
        store.ack(user_id, stale)
        logger.info("[主动关心] %d 条待读消息已经说不出口了，不再补发", len(stale))
    return {"items": store.pending(user_id)}


@app.post("/api/care/ack", dependencies=[Depends(verify_access_key)])
async def ack_proactive(user_id: str, req: CareAckRequest):
    """确认已读：同时把这句话写进当前对话，否则历史里查不到、AI 也不记得自己说过。

    只落库自己抢到的一次——托盘和前端可能同时 ack。
    """
    user_id = normalize_user_id(user_id)
    store: CareStore = _app_state["care_store"]
    pending = {item["id"]: item for item in store.pending(user_id)}
    winners = store.ack(user_id, req.ids)
    for item_id in winners:
        item = pending.get(item_id)
        if item:
            _append_assistant_message(user_id, item["text"])
    return {"ok": True, "acked": len(winners), "ids": winners}


@app.post("/api/care/dry-run", dependencies=[Depends(verify_access_key)])
async def dry_run_care(user_id: str):
    """演练：返回此刻够格说的第一句话，不写库、不打扰用户。"""
    user_id = normalize_user_id(user_id)
    link_graph = _app_state.get("care_graph")
    care_engine.note_poll(user_id)   # 用户正盯着界面看，这一刻显然有人在听
    if link_graph:
        # 先把关联补齐，演练才和真实心跳时说的一样
        await asyncio.to_thread(link_graph.sync_all, _app_state["care_store"],
                                _app_state.get("memory_manager"), user_id)
    out = care_engine.tick_once(
        _app_state["care_store"], _app_state.get("working_memory_store"), user_id,
        dry_run=True, persona=get_dialogue_prompt(), graph=link_graph,
    )
    return {"would_say": out, "budget_today": _app_state["care_store"].daily_budget(user_id)}


@app.get("/api/care/save-queue", dependencies=[Depends(verify_access_key)])
async def care_save_queue(user_id: str = DEFAULT_USER_ID):
    """后台落库队列的当前状态：还有几轮没记完。只读。"""
    user_id = normalize_user_id(user_id)
    queue = _app_state["save_queue"]
    return {"available": True, "pending_for_user": queue.pending_for(user_id), **queue.stats()}


# ================================================================
# API: 用户头像
# ================================================================

AVATAR_DIR = os.path.join(os.path.dirname(__file__), "avatars")
AVATAR_MAX_BYTES = 512 * 1024
_AVATAR_DATA_URL_RE = re.compile(r"^data:image/jpeg;base64,([A-Za-z0-9+/=\s]+)$", re.S)


def _avatar_path(user_id: str) -> str:
    return os.path.join(AVATAR_DIR, f"{user_id}.jpg")


def _decode_avatar_data_url(data_url: str) -> bytes:
    match = _AVATAR_DATA_URL_RE.match(str(data_url).strip())
    if not match:
        raise HTTPException(status_code=400, detail="头像需为 JPEG 格式的 data URL")
    try:
        raw = base64.b64decode("".join(match.group(1).split()), validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="头像数据解码失败")
    if not raw:
        raise HTTPException(status_code=400, detail="头像数据为空")
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="头像过大，请换一张小图")
    return raw


def _write_avatar(user_id: str, raw: bytes) -> None:
    os.makedirs(AVATAR_DIR, exist_ok=True)
    path = _avatar_path(user_id)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)  # 原子替换，避免写一半时读到坏图


@app.get("/api/avatar/{user_id}", dependencies=[Depends(verify_access_key)])
async def get_avatar(user_id: str):
    user_id = normalize_user_id(user_id)
    path = _avatar_path(user_id)
    if not os.path.exists(path):
        # 没头像才是常态，用 404 表达会在控制台留一条假报错：直接回 JSON 空值
        return JSONResponse({"avatar": None})
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.put("/api/avatar/{user_id}", dependencies=[Depends(verify_access_key)])
async def set_avatar(user_id: str, req: dict):
    """保存头像；data_url 传空则删除。前端已统一压成 256px 正方形 JPEG。"""
    user_id = normalize_user_id(user_id)
    data_url = (req or {}).get("data_url")
    path = _avatar_path(user_id)

    if not data_url:
        if os.path.exists(path):
            os.remove(path)
        return {"ok": True, "bytes": 0}

    raw = _decode_avatar_data_url(data_url)
    _write_avatar(user_id, raw)
    return {"ok": True, "bytes": len(raw)}


@app.get("/api/export/{user_id}", dependencies=[Depends(verify_access_key)])
async def export_user_data(user_id: str):
    """Export all user data (conversations, memories, profile, working memory) as JSON."""
    user_id = normalize_user_id(user_id)
    store: ConversationStore = _app_state["conversation_store"]
    mm: MemoryManager = _app_state["memory_manager"]
    wm: WorkingMemoryStore = _app_state.get("working_memory_store")
    pm = _app_state.get("profile_manager")

    convs, current_id = store.load(user_id)
    memories = mm.export_memories(user_id)
    working = wm.load(user_id) if wm else {}

    profile_data = {}
    if pm:
        try:
            profile = pm.load_profile(user_id)
            if profile:
                profile_data = profile.to_dict()
        except Exception:
            pass

    avatar_data_url = None
    avatar_path = _avatar_path(user_id)
    if os.path.exists(avatar_path):
        with open(avatar_path, "rb") as f:
            avatar_data_url = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()

    # 主动关心那块也是用户数据的一部分：漏了就等于"导出了但没备份"
    care: CareStore = _app_state["care_store"]
    care_data = {
        "items": care.list_items(user_id, status=None),
        "settings": care.get_settings(user_id),
    }

    return {
        "version": 2,
        "exported_at": datetime.datetime.now().isoformat(),
        "user_id": user_id,
        "conversations": convs or {},
        "current_conversation_id": current_id,
        "memories": memories,
        "working_memory": working,
        "profile": profile_data,
        "avatar": avatar_data_url,
        "care": care_data,
    }

@app.post("/api/import/{user_id}", dependencies=[Depends(verify_admin_key)])
async def import_user_data(user_id: str, req: dict):
    """Import user data from an export. Requires admin key."""
    user_id = normalize_user_id(user_id)
    store: ConversationStore = _app_state["conversation_store"]
    mm: MemoryManager = _app_state["memory_manager"]
    wm: WorkingMemoryStore = _app_state.get("working_memory_store")

    if req.get("conversations"):
        current_id = req.get("current_conversation_id") or next(iter(req["conversations"]), "")
        store.save(user_id, req["conversations"], current_id)

    imported_count = 0
    if req.get("memories"):
        imported_count = mm.import_memories(user_id, req["memories"])

    if req.get("working_memory") and wm:
        wmd = req["working_memory"]
        wm.save(
            user_id, wmd.get("summary", ""),
            wmd.get("open_topics", []),
            wmd.get("current_emotion", "neutral"),
        )

    if req.get("avatar"):
        try:
            _write_avatar(user_id, _decode_avatar_data_url(req["avatar"]))
        except HTTPException as e:
            logger.warning("[导入] 头像被跳过: %s", e.detail)

    care_imported = 0
    care_data = req.get("care") or {}
    care: Optional[CareStore] = _app_state.get("care_store")
    if care and care_data.get("items"):
        # 先清再写：否则同一个生日会在恢复快照后变成两条，到点说两遍
        care.clear_user(user_id)
        for it in care_data["items"]:
            try:
                care.add_item(
                    user_id,
                    title=str(it.get("title") or "")[:120],
                    kind=str(it.get("kind") or "event"),
                    detail=str(it.get("detail") or "")[:500],
                    due_at=float(it.get("due_at") or 0),
                    repeat=str(it.get("repeat") or "none"),
                )
                care_imported += 1
            except (ValueError, TypeError) as e:
                logger.warning("[导入] 跳过一条关心事项: %s", e)
    if care and care_data.get("settings"):
        care.save_settings(user_id, care_data["settings"])

    return {
        "status": "ok",
        "memories_imported": imported_count,
        "care_items_imported": care_imported,
    }

@app.get("/api/search/{user_id}", dependencies=[Depends(verify_access_key)])
async def search_conversations(user_id: str, q: str):
    """Full-text search across conversation titles and content."""
    user_id = normalize_user_id(user_id)
    store: ConversationStore = _app_state["conversation_store"]
    results = store.search(user_id, q)
    return {"results": results}


# ================================================================



@app.patch("/api/memory/{user_id}/{memory_id}/grade", dependencies=[Depends(verify_access_key)])
async def set_memory_grade(user_id: str, memory_id: str, req: MemoryGradeRequest):
    user_id = normalize_user_id(user_id)
    mm: MemoryManager = _app_state["memory_manager"]
    if not mm.set_manual_grade(user_id, memory_id, req.grade, req.locked):
        raise HTTPException(status_code=404, detail="记忆不存在或等级无效")
    memory = mm.get_memory(user_id, memory_id)
    return {"ok": True, "memory": memory.public_dict() if memory else None}


@app.get("/api/logs", dependencies=[Depends(verify_admin_key)])
async def get_logs(limit: int = Query(default=200, ge=1, le=500)):
    return {"logs": _log_handler.get_logs()[-limit:]}


@app.get("/api/metrics", dependencies=[Depends(verify_admin_key)])
async def get_metrics():
    """返回服务指标（Prometheus 风格）。"""
    uptime = time.time() - _metrics["start_time"]
    endpoints = []
    for path, count in _metrics["requests_total"].items():
        errors = _metrics["requests_errors"].get(path, 0)
        duration_sum = _metrics["requests_duration_sum"].get(path, 0.0)
        avg_duration = duration_sum / count if count > 0 else 0
        endpoints.append({
            "path": path,
            "requests_total": count,
            "requests_errors": errors,
            "avg_duration_ms": round(avg_duration * 1000, 1),
        })
    search_metrics = {}
    memory_manager = _app_state.get("memory_manager")
    if memory_manager is not None:
        try:
            candidate_metrics = memory_manager.get_search_metrics()
            if isinstance(candidate_metrics, dict):
                search_metrics = candidate_metrics
        except Exception as exc:
            logger.warning("读取检索指标失败: %s", exc)
    return {
        "uptime_seconds": round(uptime, 0),
        "endpoints": endpoints,
        "search": search_metrics,
        "llm": {
            "calls_total": _metrics["llm_calls_total"],
            "avg_duration_ms": round(
                _metrics["llm_calls_duration_sum"] / _metrics["llm_calls_total"] * 1000, 1
            ) if _metrics["llm_calls_total"] > 0 else 0,
        },
    }

# ================================================================
# API: 配置
# ================================================================

class UpdateModelConfigRequest(BaseModel):
    model: str
    base_url: str
    api_key: Optional[str] = ""
    use_thinking: Optional[bool] = False
    multimodal: Optional[bool] = None

@app.get("/api/config/model", dependencies=[Depends(verify_access_key)])
async def get_model_config():
    from model_config import load_active_config
    cfg = load_active_config()
    declared = cfg.get("multimodal")
    multimodal = is_multimodal_model()
    _app_state["multimodal"] = multimodal
    return {
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "api_key": ("*" * 8) if cfg.get("api_key") else "",
        "use_thinking": cfg.get("use_thinking", False),
        "multimodal": multimodal,
        # 区分"用户声明的"和"按模型名猜的"，前端据此提示
        "multimodal_declared": isinstance(declared, bool),
    }

@app.post("/api/config/model", dependencies=[Depends(verify_access_key)])
async def update_model_config(req: UpdateModelConfigRequest):
    from model_config import load_active_config, save_active_config
    model = req.model.strip()
    base_url = req.base_url.strip()
    if not model or not base_url:
        raise HTTPException(status_code=400, detail="模型名称和 Base URL 不能为空")

    # 留空表示沿用已存密钥：否则只想改个开关就会把 key 清空
    submitted_key = (req.api_key or "").strip()
    api_key = submitted_key or (load_active_config().get("api_key") or "")

    save_active_config(model=model, base_url=base_url, api_key=api_key,
                       use_thinking=bool(req.use_thinking), multimodal=req.multimodal)
    # 换模型后必须重算，否则"能否看图"会一直停在旧值上
    _app_state["multimodal"] = is_multimodal_model()
    
    return {
        "ok": True,
        "model": model,
        "base_url": base_url,
        "multimodal": _app_state["multimodal"],
        "message": "模型配置已更新并实时生效"
    }

@app.get("/api/config/model-presets", dependencies=[Depends(verify_access_key)])
async def get_model_presets():
    from model_presets import PRESET_MODELS
    return PRESET_MODELS


# ================================================================
# API: 我存的模型（同一套中转/密钥下想切来切去的几份配置）
# ================================================================

class SavedModelName(BaseModel):
    name: str = Field(..., min_length=1, max_length=20)


def _saved_models_payload():
    from model_config import SAVED_MAX, load_active_config, load_saved_models

    active = load_active_config()
    items = []
    for it in load_saved_models():
        items.append({
            "name": it["name"],
            "model": it.get("model", ""),
            "base_url": it.get("base_url", ""),
            "use_thinking": bool(it.get("use_thinking")),
            "multimodal": it.get("multimodal"),
            # 密钥不出后端：界面只知道"这套带没带 Key"
            "has_key": bool(it.get("api_key")),
            "saved_at": it.get("saved_at", 0),
            "is_active": it.get("model") == active.get("model")
                         and it.get("base_url") == active.get("base_url"),
        })
    return {"items": items, "max": SAVED_MAX}


@app.get("/api/config/saved-models", dependencies=[Depends(verify_access_key)])
async def list_saved_models():
    return _saved_models_payload()


@app.post("/api/config/saved-models", dependencies=[Depends(verify_access_key)])
async def save_model_as(req: SavedModelName):
    """把当前生效的那套存成一个可点回来的名字。"""
    from model_config import save_current_as

    try:
        entry = save_current_as(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **_saved_models_payload(), "saved": entry["name"]}


@app.post("/api/config/saved-models/use", dependencies=[Depends(verify_access_key)])
async def use_saved_model(req: SavedModelName):
    from model_config import apply_saved_model

    try:
        entry = apply_saved_model(req.name)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这套配置，可能被删掉了")
    # 换模型后必须重算，否则"能否看图"会停在旧值上（和 POST /config/model 一致）
    _app_state["multimodal"] = is_multimodal_model()
    _health_cache["result"] = None
    return {"ok": True, "model": entry.get("model", ""), **_saved_models_payload()}


@app.delete("/api/config/saved-models/{name}", dependencies=[Depends(verify_access_key)])
async def remove_saved_model(name: str):
    from model_config import delete_saved_model

    return {"ok": delete_saved_model(name), **_saved_models_payload()}


@app.post("/api/config/list-models", dependencies=[Depends(verify_access_key)])
async def list_provider_models(req: dict):
    """用填入的 Base URL + Key 去服务方拉可用模型列表；Key 留空则按 base_url 找 .env 里的匹配 key。"""
    base_url = str((req or {}).get("base_url") or "").strip().rstrip("/")
    api_key = str((req or {}).get("api_key") or "").strip()

    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写接口地址 (Base URL)")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 需以 http:// 或 https:// 开头")
    if not api_key:
        api_key = resolve_api_key(detect_provider(base_url=base_url))
    if not api_key:
        return {"ok": False, "models": [], "error": "没有可用的 API Key：请在上面填写，或先在 .env 里配置"}

    url = f"{base_url}/models"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            res = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as e:
        # 只报异常类型，绝不记录密钥
        return {"ok": False, "models": [], "error": f"连不上 {url}（{type(e).__name__}），请检查地址或网络"}

    if res.status_code != 200:
        if res.status_code in (401, 403):
            return {"ok": False, "models": [], "error": f"密钥被服务方拒绝（{res.status_code}），这个 Key 用不了"}
        if res.status_code == 404:
            return {"ok": False, "models": [], "error": "服务方没有 /models 接口，请手动填写模型名称"}
        return {"ok": False, "models": [], "error": f"服务方返回 {res.status_code}"}

    try:
        data = res.json()
    except Exception:
        return {"ok": False, "models": [], "error": "服务方返回的不是 JSON，请手动填写模型名称"}

    items = data.get("data") if isinstance(data, dict) else None
    models = sorted({str(i["id"]) for i in items if isinstance(i, dict) and i.get("id")}) if isinstance(items, list) else []
    if not models:
        return {"ok": False, "models": [], "error": "服务方没返回任何模型 ID"}
    return {"ok": True, "models": models, "error": None}

@app.get("/api/config/prompt", dependencies=[Depends(verify_access_key)])
async def get_prompt_config():
    current = get_dialogue_prompt()
    is_custom = current != DIALOGUE_AGENT_PROMPT
    return {
        "prompt": current,
        "default_prompt": DIALOGUE_AGENT_PROMPT,
        "is_custom": is_custom,
    }

@app.put("/api/config/prompt", dependencies=[Depends(verify_admin_key)])
async def update_prompt_config(req: PromptUpdateRequest):
    save_prompt_config(req.prompt)
    current = get_dialogue_prompt()
    is_custom = current != DIALOGUE_AGENT_PROMPT
    return {
        "ok": True,
        "prompt": current,
        "is_custom": is_custom,
    }

# ================================================================

_health_cache: dict = {"result": None, "timestamp": 0.0}
_HEALTH_CACHE_TTL = 30


async def _check_llm_connectivity() -> str:
    if not CHAT_API_KEY:
        return "not_configured"
    try:
        client = get_llm_client(temperature=0.0, use_thinking=False)
        response = await asyncio.wait_for(
            asyncio.to_thread(client.invoke, "ping"),
            timeout=5.0,
        )
        return "ok" if response else "unreachable"
    except Exception:
        return "unreachable"


@app.get("/api/health")
async def health():
    """Cheap public liveness endpoint; never calls paid providers."""
    return {
        "status": "ok",
        "dependencies": {
            "llm": "not_checked",
        },
        "multimodal": _app_state.get("multimodal", False),
        "auth_required": bool(MOZ_ACCESS_KEY),
        "admin_key_configured": bool(MOZ_ADMIN_KEY),
    }


@app.get("/api/health/llm", dependencies=[Depends(verify_admin_key)])
async def llm_health():
    now = time.time()
    if _health_cache["result"] is not None and (now - _health_cache["timestamp"]) < _HEALTH_CACHE_TTL:
        return _health_cache["result"]

    llm_status = await _check_llm_connectivity()
    result = {"status": "ok", "dependencies": {"llm": llm_status}}
    _health_cache["result"] = result
    _health_cache["timestamp"] = now
    return result

_V1_ROUTES_ADDED = False


def _add_v1_routes():
    global _V1_ROUTES_ADDED
    if _V1_ROUTES_ADDED:
        return
    _V1_ROUTES_ADDED = True
    for route in list(app.router.routes):
        if not hasattr(route, "path"):
            continue
        if not route.path.startswith("/api/"):
            continue
        if "/api/v1/" in route.path:
            continue
        v1_path = route.path.replace("/api/", "/api/v1/", 1)
        if hasattr(route, "methods") and hasattr(route, "endpoint"):
            app.add_api_route(
                v1_path,
                route.endpoint,
                methods=route.methods,
                dependencies=route.dependencies,
                response_model=route.response_model,
                tags=route.tags,
                summary=route.summary,
                description=route.description,
            )


_add_v1_routes()


if __name__ == "__main__":
    import uvicorn
    if not CHAT_API_KEY:
        provider = detect_provider()
        key_name = PROVIDER_KEY_MAP.get(provider, "API_KEY")
        logger.error("未找到 Chat API Key。请在 .env 中设置 %s=yours（当前模型: %s，提供商: %s）",
                       key_name, CHAT_MODEL, provider)
        sys.exit(1)
    logger.info("启动 moz API 服务...")
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)

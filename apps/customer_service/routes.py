"""Customer Service API routes — sync + SSE streaming."""
import asyncio
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from utils import ConversationManager
from infrastructure.auth import get_current_user, create_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["customer_service"])

# Injected by main.py lifespan
agent = None
conversation_manager: ConversationManager | None = None


APP_ENV = (os.getenv("APP_ENV") or "development").strip().lower()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ⚠️ 安全开关：/api/token 是「无凭证签发任意身份」的 demo 入口。
#   POST /api/token {"user_id": "2"} → 直接拿到可冒充用户 2 的有效 JWT。
# 它不是「越权访问他人会话」而是「变成受害者本人」，因此会**绕过所有 IDOR
# 校验**（_assert_conversation_owner 形同虚设）。生产环境必须关闭。
# 默认策略：非 production 开启（保证 demo 开箱即用），production 强制关闭。
ALLOW_DEMO_TOKEN = _env_bool("ALLOW_DEMO_TOKEN", APP_ENV != "production")


class ChatRequest(BaseModel):
    conversation_id: str = ""
    message: str


class ChatResponse(BaseModel):
    type: str
    content: str
    conversation_id: str


def _check_conversation_owner(conversation_id: str, user_id: str) -> None:
    """越权校验：会话必须属于当前登录用户（防 IDOR）。

    不存在也返回 404 而不是 403，避免泄露「该会话是否存在」。

    这是**同步**函数：async 路由必须用 ``_assert_conversation_owner`` 包一层
    ``to_thread``，否则这里面的 PostgreSQL 往返会阻塞整个事件循环。
    """
    if not conversation_id:
        return
    if conversation_manager is None:
        raise HTTPException(500, "conversation_manager not initialized")
    owner = conversation_manager.get_owner(conversation_id)
    if owner is None:
        raise HTTPException(404, "conversation not found")
    if str(owner) != str(user_id):
        logger.warning("IDOR attempt: user=%s tried conversation=%s owned by %s",
                       user_id, conversation_id, owner)
        raise HTTPException(403, "forbidden")


async def _assert_conversation_owner(conversation_id: str, user_id: str) -> None:
    """async 版本：把同步 DB 查询丢到线程池，避免阻塞事件循环。"""
    await asyncio.to_thread(_check_conversation_owner, conversation_id, user_id)


# ── Token ──

class TokenRequest(BaseModel):
    user_id: str


@router.post("/token")
def generate_token(req: TokenRequest):
    """Generate a JWT demo token for the given user_id.

    DEMO-ONLY and **无凭证**：只签发 ``sub``（无 role，故够不到 admin 接口）。
    真实账号请走 /api/shop/auth/login（校验 bcrypt 密码）。
    生产环境通过 ``APP_ENV=production`` 或 ``ALLOW_DEMO_TOKEN=0`` 关闭。
    """
    if not ALLOW_DEMO_TOKEN:
        # 返回 404 而不是 403，避免对外暴露该端点存在
        raise HTTPException(404, "not found")
    logger.warning("DEMO token issued for user_id=%r — never enable this in production",
                   req.user_id)
    token = create_access_token(req.user_id)
    return {"access_token": token, "token_type": "bearer"}


# ── Chat ──

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user_id: str = Depends(get_current_user)):
    if not req.message.strip():
        raise HTTPException(400, "message is required")
    if agent is None:
        raise HTTPException(500, "agent not initialized")

    await _assert_conversation_owner(req.conversation_id, user_id)

    # async：checkpointer 是 AsyncSqliteSaver，其 asyncio.Lock 绑定在启动时的事件
    # 循环上，因此必须在主事件循环内 await，不能丢到线程池里跑同步 invoke。
    result = await agent.arun(req.message, req.conversation_id, user_id=user_id)
    return ChatResponse(
        type=result.type,
        content=result.content,
        conversation_id=result.conversation_id,
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, user_id: str = Depends(get_current_user)):
    """SSE streaming endpoint: yields node-level events during agent execution."""
    if not req.message.strip():
        raise HTTPException(400, "message is required")
    if agent is None:
        raise HTTPException(500, "agent not initialized")
    if not hasattr(agent, "run_stream"):
        raise HTTPException(500, "agent does not support streaming")

    await _assert_conversation_owner(req.conversation_id, user_id)

    async def event_generator():
        async for event in agent.run_stream(req.message, req.conversation_id, user_id=user_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Conversations ──

@router.post("/conversations")
def create_conversation(user_id: str = Depends(get_current_user)):
    if conversation_manager is None:
        raise HTTPException(500, "conversation_manager not initialized")
    conv_id = conversation_manager.create(user_id)
    return {"conversation_id": conv_id}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user_id: str = Depends(get_current_user)):
    if conversation_manager is None:
        raise HTTPException(500, "conversation_manager not initialized")
    _check_conversation_owner(conversation_id, user_id)
    history = conversation_manager.get_history(conversation_id)
    return {"conversation_id": conversation_id, "messages": history}


@router.get("/conversations")
def list_conversations(user_id: str = Depends(get_current_user)):
    if conversation_manager is None:
        raise HTTPException(500, "conversation_manager not initialized")
    return conversation_manager.list_conversations(user_id)


# ── Observability ──

@router.get("/traces")
def list_traces(limit: int = 50, user_id: str = Depends(get_current_user)):
    """当前用户的 Agent 执行轨迹 + 全局聚合指标（用于排障与答辩演示）。"""
    from domain.customer_service.tracing import recent_traces, trace_stats

    return {
        "code": 0,
        "stats": trace_stats(),
        "items": recent_traces(limit=limit, user_id=user_id),
    }

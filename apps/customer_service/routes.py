"""Customer Service API routes — sync + SSE streaming."""
import json
import asyncio
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from utils import ConversationManager
from infrastructure.auth import get_current_user, create_access_token

router = APIRouter(prefix="/api", tags=["customer_service"])

# Injected by main.py
agent = None
conversation_manager: ConversationManager | None = None


class ChatRequest(BaseModel):
    conversation_id: str = ""
    message: str


class ChatResponse(BaseModel):
    type: str
    content: str
    conversation_id: str


# ── Token ──

class TokenRequest(BaseModel):
    user_id: str


@router.post("/token")
def generate_token(req: TokenRequest):
    """Generate a JWT demo token for the given user_id."""
    token = create_access_token(req.user_id)
    return {"access_token": token, "token_type": "bearer"}


# ── Chat ──

@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user_id: str = Depends(get_current_user)):
    if not req.message.strip():
        raise HTTPException(400, "message is required")
    if agent is None:
        raise HTTPException(500, "agent not initialized")

    result = agent.run(req.message, req.conversation_id, user_id=user_id)
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
    history = conversation_manager.get_history(conversation_id)
    return {"conversation_id": conversation_id, "messages": history}


@router.get("/conversations")
def list_conversations(user_id: str = Depends(get_current_user)):
    if conversation_manager is None:
        raise HTTPException(500, "conversation_manager not initialized")
    return conversation_manager.list_conversations(user_id)


# ── Order management ──

class CreateOrderRequest(BaseModel):
    product: str
    recipient: str
    address: str
    amount: float = 0.0


@router.post("/orders")
def create_order(req: CreateOrderRequest, user_id: str = Depends(get_current_user)):
    """Create a new order (demo endpoint)."""
    from tools.order_search import place_order
    result = place_order(req.product, req.recipient, req.address, req.amount)
    return {"result": result}


@router.get("/orders")
def list_orders(user_id: str = Depends(get_current_user)):
    """List orders for the authenticated user."""
    from infrastructure.order_repository import MockOrderRepository
    repo = MockOrderRepository()
    orders = repo.list_orders_by_user(user_id)
    result = []
    for o in orders:
        result.append({
            "order_id": o.order_id, "product": o.product,
            "amount": o.amount, "status": o.status,
            "recipient": o.recipient, "address": o.address,
            "order_time": o.order_time,
        })
    return {"orders": result}

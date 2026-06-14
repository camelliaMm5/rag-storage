"""FastAPI entry point — dependency assembly and router registration."""
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from llm import chat_service, LangChainChatModel
from utils import ConversationManager
from tools import (
    search_faq_tool, query_order_tool, query_logistics_tool, place_order_tool,
)
from domain.customer_service import MasterAgent

from apps.customer_service.routes import router as cs_router

# ── Dependency assembly ──
lc_llm = LangChainChatModel(chat_service=chat_service)
conversation_manager = ConversationManager(max_context_turns=10)
agent = MasterAgent(
    llm=lc_llm,
    conversation_manager=conversation_manager,
    tools=[search_faq_tool, query_order_tool, query_logistics_tool, place_order_tool],
)

# Inject into routes module
import apps.customer_service.routes as cs_routes
cs_routes.agent = agent
cs_routes.conversation_manager = conversation_manager

# ── FastAPI app ──
app = FastAPI(title="RAG Customer Service Agent")
app.include_router(cs_router)

# ── Static files ──
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "RAG Customer Service Agent", "docs": "/docs"}


@app.get("/api/health")
def health():
    try:
        from infrastructure.rag import rag_store
        return {
            "status": "ok",
            "chunks": rag_store.count(),
            "llm_available": chat_service.available,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("  Multi-Agent Customer Service (Supervisor)")
    print("  Chat UI:  http://localhost:8000")
    print("  Stream:   POST /api/chat/stream")
    print("  API Docs: http://localhost:8000/docs")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)

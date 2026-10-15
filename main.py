"""FastAPI entry point — dependency assembly and router registration."""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from infrastructure.middleware import register_middlewares, setup_logging

setup_logging()
logger = logging.getLogger("app")

from llm import chat_service, LangChainChatModel
from utils import ConversationManager
from tools import (
    search_faq_tool, query_order_tool, query_logistics_tool,
    query_after_sale_tool, query_cart_tool, list_my_orders_tool,
    search_products_tool, list_products_tool,
)
from domain.customer_service import MasterAgent
from domain.customer_service.checkpointer import persistent_checkpointer

from apps.customer_service.routes import router as cs_router
from apps.shop.c_endpoint.auth import router as shop_auth_router
from apps.shop.c_endpoint.product import router as shop_product_router
from apps.shop.c_endpoint.order import router as shop_order_router
from apps.shop.b_endpoint.admin import router as shop_admin_router
from apps.shop.internal.routes import router as shop_internal_router

CHECKPOINT_DB_PATH = os.getenv("CHECKPOINT_DB_PATH", "./checkpoints.db")
ORDER_TIMEOUT_MINUTES = int(os.getenv("ORDER_TIMEOUT_MINUTES", "30"))
ORDER_SWEEP_INTERVAL_SEC = int(os.getenv("ORDER_SWEEP_INTERVAL_SEC", "300"))

APP_ENV = (os.getenv("APP_ENV") or "development").strip().lower()

# 内置默认密钥 —— 只应在本地/demo 使用。生产环境必须显式替换。
_WEAK_SECRETS = {
    "JWT_SECRET": ("demo-secret-change-in-production", "change-me-in-production", ""),
    "INTERNAL_API_TOKEN": ("dev-internal-token", ""),
}


def _audit_secrets() -> None:
    """启动期安全体检。

    这些默认值一旦带上生产环境，等于把「签发任意身份」和「/internal 全量数据」
    直接公开。策略：非生产只告警（保证 demo 开箱即用），生产**拒绝启动**。
    """
    problems: list[str] = []
    for name, weak in _WEAK_SECRETS.items():
        if name == "JWT_SECRET":
            from infrastructure.auth import JWT_SECRET as _val
        else:
            _val = os.getenv(name, "dev-internal-token")
        if _val in weak:
            problems.append(f"{name} 仍是默认值")

    if not problems:
        return
    detail = "；".join(problems)
    if APP_ENV == "production":
        raise RuntimeError(
            f"生产环境（APP_ENV=production）拒绝启动：{detail}。"
            f"请在 .env 中替换为随机长密钥后重试。"
        )
    logger.warning("⚠️  安全体检：%s —— 仅限本地/demo，上线前务必替换", detail)


# ── Background: 超时未支付订单自动取消 ────────────────────────────────
async def _order_timeout_sweeper() -> None:
    """Periodically cancel pending orders that exceeded the payment window.

    用 asyncio 任务而非 APScheduler —— 项目未引入该依赖，单进程 demo 场景下
    asyncio 循环已足够；多副本部署时应换成外部分布式调度。
    """
    from domain.shop.order import OrderService

    while True:
        try:
            await asyncio.sleep(ORDER_SWEEP_INTERVAL_SEC)
            cancelled = await asyncio.to_thread(
                OrderService.cancel_expired, ORDER_TIMEOUT_MINUTES
            )
            if cancelled:
                logger.info("order sweeper: cancelled %d expired order(s): %s",
                            len(cancelled), cancelled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("order sweeper iteration failed")


# ── Lifespan: 依赖装配（AsyncSqliteSaver 必须在这里创建） ──────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    import apps.customer_service.routes as cs_routes

    _audit_secrets()

    sweep_task = None
    try:
        conversation_manager = ConversationManager(max_context_turns=10)
    except Exception:
        logger.exception("ConversationManager init failed — is PostgreSQL up?")
        raise

    async with persistent_checkpointer(CHECKPOINT_DB_PATH) as checkpointer:
        lc_llm = LangChainChatModel(chat_service=chat_service)
        agent = MasterAgent(
            llm=lc_llm,
            conversation_manager=conversation_manager,
            # 只注册「图真正会绑定」的工具。下单不在这里 —— 它走确定性的
            # place_order_prepare / commit 两阶段节点（金额与库存不经 LLM），
            # 由 build_master_graph 依据 search_products 是否可用自动装配。
            tools=[search_faq_tool, query_order_tool, query_logistics_tool,
                   query_after_sale_tool, query_cart_tool,
                   list_my_orders_tool, search_products_tool,
                   list_products_tool],
            checkpointer=checkpointer,
        )

        cs_routes.agent = agent
        cs_routes.conversation_manager = conversation_manager
        app.state.agent = agent
        app.state.conversation_manager = conversation_manager

        if not chat_service.available:
            logger.warning("LLM_API_KEY is not configured — chat will run in "
                           "degraded mode (friendly message, no 500)")

        if cs_routes.ALLOW_DEMO_TOKEN and APP_ENV != "production":
            logger.warning("⚠️  /api/token 已启用：它无凭证签发任意 user_id 的 JWT，"
                           "demo 专用。生产请设 APP_ENV=production 或 ALLOW_DEMO_TOKEN=0")

        sweep_task = asyncio.create_task(_order_timeout_sweeper())
        logger.info("startup complete")
        try:
            yield
        finally:
            if sweep_task:
                sweep_task.cancel()
                try:
                    await sweep_task
                except asyncio.CancelledError:
                    pass
            # 优雅释放数据库连接池
            try:
                from infrastructure.shop_database import close_pool

                close_pool()
                logger.info("shop db pool closed")
            except Exception:
                logger.warning("closing shop db pool failed", exc_info=True)
            logger.info("shutdown complete")


# ── FastAPI app ──
app = FastAPI(title="RAG Customer Service Agent", lifespan=lifespan)
register_middlewares(app)

app.include_router(cs_router)
app.include_router(shop_auth_router)
app.include_router(shop_product_router)
app.include_router(shop_order_router)
app.include_router(shop_admin_router)
app.include_router(shop_internal_router)

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
    """Liveness + dependency readiness (DB / vector store / LLM / checkpointer)."""
    detail: dict = {"llm_available": chat_service.available}
    status = "ok"

    try:
        from infrastructure.rag import rag_store
        detail["chunks"] = rag_store.count()
    except Exception as e:
        detail["chunks_error"] = f"{type(e).__name__}: {e}"
        status = "degraded"

    try:
        from infrastructure.cache import cache
        detail["cache"] = cache.stats()
        # Redis 是可选优化：不可用只降命中率，不算 degraded
    except Exception as e:
        detail["cache_error"] = f"{type(e).__name__}: {e}"

    try:
        agent = getattr(app.state, "agent", None)
        detail["agent_ready"] = agent is not None
        detail["checkpointer"] = type(agent.checkpointer).__name__ if agent else None
    except Exception as e:
        detail["agent_error"] = str(e)
        status = "degraded"

    try:
        from domain.customer_service.tracing import trace_stats
        detail["traces"] = trace_stats()
    except Exception as e:
        detail["traces_error"] = f"{type(e).__name__}: {e}"

    if not chat_service.available:
        status = "degraded"

    return {"status": status, **detail}


if __name__ == "__main__":
    import uvicorn

    print("=" * 50)
    print("  Multi-Agent Customer Service (Supervisor)")
    print("  Chat UI:  http://localhost:8000")
    print("  Stream:   POST /api/chat/stream")
    print("  API Docs: http://localhost:8000/docs")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)

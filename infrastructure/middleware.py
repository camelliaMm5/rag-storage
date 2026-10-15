"""HTTP middleware & logging setup — request-id tracing, access log, CORS,
global exception handler.

设计要点：
- request_id 通过 ContextVar 贯穿「中间件 → 日志 Filter → 业务日志」链路，
  使一次请求产生的所有日志（含 Agent 节点日志）都能关联起来。
- 访问日志只输出 stdout（不写文件），容器环境下交给 docker 日志轮转。
- 全局异常处理器保证任何未捕获异常都返回结构化 JSON + request_id，
  而不是 FastAPI 默认的裸 500 —— Agent 系统必须能定位失败原因。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── request id plumbing ───────────────────────────────────────────────
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-7s | %(name)s | rid=%(request_id)s | %(message)s"
)


class RequestIdFilter(logging.Filter):
    """Inject the current request id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(level: str | None = None) -> None:
    """Configure root logging once, with request-id support."""
    root = logging.getLogger()
    if any(isinstance(f, RequestIdFilter) for f in root.filters):
        return  # already configured (avoid duplicate handlers on reload)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(RequestIdFilter())

    root.handlers = [handler]
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── middleware registration ───────────────────────────────────────────
access_logger = logging.getLogger("access")


def register_middlewares(app: FastAPI) -> None:
    """Attach CORS, request-id and access-log middleware to the app."""

    origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
    allow_origins = [o.strip() for o in origins.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,  # "*" 与 credentials 不能共存
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            access_logger.exception(
                "%s %s -> unhandled (%0.1fms)", request.method, request.url.path, elapsed
            )
            raise
        else:
            elapsed = (time.perf_counter() - started) * 1000
            response.headers["X-Request-ID"] = rid
            access_logger.info(
                "%s %s -> %s (%0.1fms)",
                request.method, request.url.path, response.status_code, elapsed,
            )
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        rid = request_id_var.get()
        logging.getLogger("app").exception(
            "unhandled error on %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": "服务内部错误，请稍后重试",
                "detail": f"{type(exc).__name__}: {exc}",
                "request_id": rid,
            },
        )

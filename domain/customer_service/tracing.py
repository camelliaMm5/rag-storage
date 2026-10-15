"""Agent 执行轨迹（可观测性）—— 落库到 customer_service.agent_traces。

为什么这项对「Agent 项目」是刚需：
没有 trace，就无法回答「这个请求为什么答错了」——是路由判错、工具查空、
还是 LLM 编造？也就无法把线上失败样本沉淀成回归评测集。业务 KPI（路由准确率、
工具成功率、澄清率、P95 延迟）全部依赖它。

设计原则：**trace 永远不能拖垮主流程**。任何写库异常都被吞掉并记 warning——
可观测性是旁路，不是关键路径。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from infrastructure.database import engine

logger = logging.getLogger("trace")

_TABLE = "agent_traces"
_is_sqlite = engine.dialect.name == "sqlite"
_table_ready = False


def _ensure_table() -> None:
    """SQLite（离线测试）下自建表；PostgreSQL 由 migrations/001 建好。"""
    global _table_ready
    if _table_ready:
        return
    if _is_sqlite:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id   VARCHAR(36),
            user_id           VARCHAR(128),
            user_input        TEXT,
            route             VARCHAR(32),
            extract           TEXT,
            agent_nodes       TEXT,
            tool_calls        TEXT,
            final_content     TEXT,
            status            VARCHAR(16) DEFAULT 'ok',
            error             TEXT,
            duration_ms       INTEGER,
            llm_calls         INTEGER DEFAULT 0,
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        with engine.begin() as conn:
            conn.execute(text(ddl))
    _table_ready = True


@dataclass
class TraceRecorder:
    """收集一次请求的执行轨迹，最后统一落库。"""

    conversation_id: str = ""
    user_id: str = ""
    user_input: str = ""
    route: str = ""
    extract: str = ""
    status: str = "ok"
    error: str = ""
    final_content: str = ""
    nodes: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    # ── 采集 ──
    def add_node(self, name: str) -> None:
        self.nodes.append({
            "node": name,
            "at_ms": int((time.perf_counter() - self._t0) * 1000),
        })

    def add_tools(self, steps: list[dict] | None) -> None:
        for s in steps or []:
            self.tool_calls.append(s)

    def set_route(self, route: str, extract: str = "") -> None:
        self.route = route or ""
        if extract:
            self.extract = extract

    def fail(self, err: object) -> None:
        self.status = "error"
        self.error = f"{type(err).__name__}: {err}"[:2000]

    # ── 落库 ──
    def save(self, usage=None) -> None:
        usage = usage.as_dict() if usage is not None else {}
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        payload = {
            "conversation_id": self.conversation_id or None,
            "user_id": self.user_id or None,
            "user_input": (self.user_input or "")[:4000],
            "route": (self.route or "")[:32],
            "extract": (self.extract or "")[:2000],
            "agent_nodes": json.dumps(self.nodes, ensure_ascii=False),
            "tool_calls": json.dumps(self.tool_calls, ensure_ascii=False),
            "final_content": (self.final_content or "")[:8000],
            "status": self.status,
            "error": self.error or None,
            "duration_ms": duration_ms,
            "llm_calls": usage.get("llm_calls", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }
        if _is_sqlite:
            sql = text(f"""
                INSERT INTO {_TABLE}
                    (conversation_id, user_id, user_input, route, extract,
                     agent_nodes, tool_calls, final_content, status, error,
                     duration_ms, llm_calls, prompt_tokens, completion_tokens)
                VALUES
                    (:conversation_id, :user_id, :user_input, :route, :extract,
                     :agent_nodes, :tool_calls, :final_content, :status, :error,
                     :duration_ms, :llm_calls, :prompt_tokens, :completion_tokens)
            """)
        else:
            sql = text(f"""
                INSERT INTO customer_service.{_TABLE}
                    (conversation_id, user_id, user_input, route, extract,
                     agent_nodes, tool_calls, final_content, status, error,
                     duration_ms, llm_calls, prompt_tokens, completion_tokens)
                VALUES
                    (:conversation_id, :user_id, :user_input, :route, :extract,
                     CAST(:agent_nodes AS JSONB), CAST(:tool_calls AS JSONB),
                     :final_content, :status, :error,
                     :duration_ms, :llm_calls, :prompt_tokens, :completion_tokens)
            """)
        try:
            _ensure_table()
            with engine.begin() as conn:
                conn.execute(sql, payload)
        except Exception:
            # 旁路功能：绝不影响主流程
            logger.warning("trace save failed (conv=%s)", self.conversation_id,
                           exc_info=True)


def recent_traces(limit: int = 50, user_id: str | None = None) -> list[dict]:
    """最近的执行轨迹（用于 /api/traces 排障）。

    传入 ``user_id`` 时只返回该用户自己的轨迹 —— trace 含用户原话与订单号，
    不能跨用户暴露。
    """
    try:
        _ensure_table()
        prefix = "" if _is_sqlite else "customer_service."
        where = "WHERE user_id = :user_id " if user_id else ""
        params: dict = {"limit": max(1, min(limit, 200))}
        if user_id:
            params["user_id"] = user_id
        with engine.begin() as conn:
            rows = conn.execute(text(
                f"SELECT id, conversation_id, user_id, route, status, error, "
                f"duration_ms, llm_calls, prompt_tokens, completion_tokens, "
                f"tool_calls, created_at "
                f"FROM {prefix}{_TABLE} {where}ORDER BY id DESC LIMIT :limit"
            ), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        logger.warning("trace query failed", exc_info=True)
        return []


def trace_stats() -> dict:
    """按状态与 route 聚合的概览指标（答辩/巡检用）。"""
    try:
        _ensure_table()
        prefix = "" if _is_sqlite else "customer_service."
        with engine.begin() as conn:
            total = conn.execute(text(f"SELECT count(*) FROM {prefix}{_TABLE}")).scalar() or 0
            by_status = conn.execute(text(
                f"SELECT status, count(*) c FROM {prefix}{_TABLE} GROUP BY status"
            )).mappings().all()
            by_route = conn.execute(text(
                f"SELECT route, count(*) c FROM {prefix}{_TABLE} "
                f"GROUP BY route ORDER BY c DESC LIMIT 10"
            )).mappings().all()
            avg_ms = conn.execute(text(
                f"SELECT avg(duration_ms) FROM {prefix}{_TABLE}"
            )).scalar()
        return {
            "total": total,
            "by_status": {r["status"]: r["c"] for r in by_status},
            "by_route": {r["route"]: r["c"] for r in by_route},
            "avg_duration_ms": round(float(avg_ms), 1) if avg_ms is not None else None,
        }
    except Exception:
        logger.warning("trace stats failed", exc_info=True)
        return {"total": 0, "by_status": {}, "by_route": {}, "avg_duration_ms": None}

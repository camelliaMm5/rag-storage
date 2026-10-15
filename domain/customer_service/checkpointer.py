"""Checkpointer 工厂 —— LangGraph 状态持久化。

踩坑记录（为什么必须只在异步上下文里创建）：

1. ``AsyncSqliteSaver.__init__`` 内部会调用 ``asyncio.get_running_loop()``
   （langgraph-checkpoint-sqlite 3.x ``aio.py``），因此它**只能在运行中的事件
   循环内构造**。在 import 期 / 模块级构造必然抛
   ``RuntimeError: no running event loop``，若被 ``except`` 吞掉就会静默退化成
   内存态 —— 这正是旧版 ``_create_checkpointer()`` 的问题：checkpoints.db 有表
   结构但 0 行数据，持久化从未生效。

2. 同步的 ``SqliteSaver`` 能配合 ``graph.invoke()``，但 ``graph.astream()`` 会抛
   ``NotImplementedError: The SqliteSaver does not support async methods``。
   旧版因此被迫维护「同步 run + 异步 arun」两条链路，而这两条链路又没有各自的
   checkpointer，冲突不可避免。

现在的策略是**只保留异步一条链路**（节点全部 ``async def``），从根上消除冲突：
- 应用运行时（FastAPI lifespan）→ ``persistent_checkpointer()``
- 明确不需要持久化（一次性脚本）→ ``memory_checkpointer()``
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


def ensure_parent_dir(path: str) -> str:
    """确保 DB 文件的父目录存在。

    否则路径不存在时会在启动期抛 ``sqlite3.OperationalError: unable to open
    database file``，错误信息完全看不出是「目录没建」。
    """
    if path in (":memory:", "") or path.startswith("file:"):
        return path
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


@asynccontextmanager
async def persistent_checkpointer(path: str):
    """异步、落盘的 checkpointer。**必须在运行中的事件循环里创建。**

    用法（FastAPI lifespan）::

        async with persistent_checkpointer("./checkpoints.db") as saver:
            agent = MasterAgent(..., checkpointer=saver)
            yield
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = ensure_parent_dir(path)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        logger.info("checkpointer: AsyncSqliteSaver ready at %s", path)
        yield saver


def memory_checkpointer():
    """显式内存态 checkpointer —— 进程重启即丢状态。

    仅在确实不需要持久化时使用；会打 warning，避免「静默丢失耐久性」。
    """
    from langgraph.checkpoint.memory import MemorySaver

    logger.warning(
        "checkpointer: using in-memory MemorySaver — conversation state will NOT "
        "survive a process restart"
    )
    return MemorySaver()

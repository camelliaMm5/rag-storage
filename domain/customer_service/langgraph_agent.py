"""MasterAgent —— Supervisor 多智能体的唯一对外封装。

对外只暴露异步入口（``arun`` / ``run_stream``），因为运行时的 checkpointer 是
``AsyncSqliteSaver``：它的 ``asyncio.Lock`` 绑定在 FastAPI 启动时的事件循环上，
同步 ``invoke`` 无法复用它。旧版同时维护「同步 run + 异步 arun」两条链路，
结果就是同步链路一用 stream 就 ``NotImplementedError``——现已统一。

本模块负责三件「图之外」的事：
1. **并发隔离**：同一个 conversation 的并发请求串行化（LangGraph 的 state 是
   按 thread_id 存的，并发写会互相覆盖）。
2. **记忆补齐**：checkpoint 是「热记忆」，PostgreSQL 是「耐久记忆」。当
   checkpoint 里查不到该会话（首次/换库/被清理）时，用 PG 历史回填，避免
   重启后多轮上下文凭空消失。
3. **可观测性**：每次请求落一条 trace（路由、节点、工具、耗时、token、错误）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from domain.customer_service.master_graph import (
    HISTORY_WINDOW,
    MAX_SUB_AGENT_STEPS,
    build_master_graph,
)
from domain.customer_service.tracing import TraceRecorder
from llm.usage import estimate_tokens, start_usage

logger = logging.getLogger(__name__)

LLM_UNAVAILABLE_MSG = (
    "抱歉，AI 服务暂时不可用（未配置 LLM_API_KEY 或上游超时）。\n"
    "您可以稍后重试；如需人工协助，请通过客服热线联系我们。"
)

GENERIC_FAILURE_MSG = (
    "抱歉，处理您的请求时出现了异常。请稍后重试，或换一种说法再问我一次。"
)

# 这些节点的输出是「面向用户的回答」，token 可以流给前端
TOKEN_NODES = frozenset({
    "faq_agent", "order_agent", "logistics_agent", "after_sale_agent",
    "cart_query_agent", "list_orders_agent", "product_agent", "finish",
})

# 节点 -> (进度文案, SSE 事件名)
# 注意：supervisor 的进度事件名不能再用 "routing" —— 语义化的 routing 事件
# （带 route/extract）另有一条，同名会导致前端收到两条 routing、其中一条没有 route。
_NODE_DISPLAY: dict[str, tuple[str, str]] = {
    "supervisor": ("正在分析意图...", "supervisor"),
    "faq_agent": ("FAQ Agent 检索知识库中...", "faq_agent"),
    "order_agent": ("订单 Agent 查询中...", "order_agent"),
    "logistics_agent": ("物流 Agent 查询中...", "logistics_agent"),
    "after_sale_agent": ("售后 Agent 查询中...", "after_sale_agent"),
    "cart_query_agent": ("购物车 Agent 查询中...", "cart_query_agent"),
    "list_orders_agent": ("订单列表 Agent 查询中...", "list_orders_agent"),
    "product_agent": ("商品浏览 Agent 查询中...", "product_agent"),
    "place_order_prepare": ("正在核对商品与价格...", "place_order_agent"),
    "place_order_commit": ("正在提交订单...", "place_order_agent"),
    "place_order_cancel": ("正在取消下单...", "place_order_agent"),
    "clarify": ("需要您补充信息...", "clarify"),
    "finish": ("正在生成回复...", "finish"),
}


@dataclass
class AgentResponse:
    type: str                       # final_answer / degraded / ask_user
    content: str
    conversation_id: str
    metadata: dict | None = field(default=None)


class MasterAgent:
    def __init__(self, llm, conversation_manager, tools: list,
                 checkpointer=None, max_steps: int = MAX_SUB_AGENT_STEPS):
        self.llm = llm
        self.conversation_manager = conversation_manager
        self.checkpointer = checkpointer
        self.max_steps = max_steps
        self.graph = build_master_graph(
            llm=llm,
            tools_by_name={t.name: t for t in tools},
            checkpointer=checkpointer,
            max_steps=max_steps,
        )
        self._locks: dict[str, asyncio.Lock] = {}

    # ── helpers ────────────────────────────────────────────────────────
    @property
    def llm_available(self) -> bool:
        return bool(getattr(self.llm, "available", True))

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        """同一会话串行化；顺带做一次惰性回收，避免锁表无限增长。"""
        lock = self._locks.get(conversation_id)
        if lock is not None:
            return lock
        if len(self._locks) >= 1024:
            for key in [k for k, l in self._locks.items() if not l.locked()][:256]:
                self._locks.pop(key, None)
        lock = asyncio.Lock()
        self._locks[conversation_id] = lock
        return lock

    async def _ensure_conversation(self, conversation_id: str, user_id: str) -> str:
        """确保会话存在。

        **必须走 to_thread**：``create()`` 内部是同步 SQLAlchemy 往返，
        直接在事件循环里调用会让整进程停摆（所有并发请求一起卡）。
        """
        if conversation_id:
            return conversation_id
        return await asyncio.to_thread(
            self.conversation_manager.create, user_id
        )

    async def _snapshot_values(self, config: dict) -> dict:
        """读取该 thread 的 checkpoint 状态值。

        必须用 ``aget_state``：运行时的 checkpointer 是 ``AsyncSqliteSaver``，
        同步的 ``get_state`` 会在它上面抛 ``NotImplementedError``。
        """
        try:
            snapshot = await self.graph.aget_state(config)
            return getattr(snapshot, "values", None) or {}
        except Exception:
            logger.warning("aget_state failed; treating checkpoint as empty", exc_info=True)
            return {}

    async def _prepare(self, user_input: str, conversation_id: str, user_id: str):
        """建会话、落用户消息、组装输入 state（PG 历史回填）。

        注意：输入 state **只放 messages / user_query / user_id**。
        ``pending_intent`` / ``pending_order`` / ``slots`` 这些必须由 checkpoint
        承载——如果每轮都显式传 ``None``，会把上一轮存的草稿覆盖掉，
        下单确认就永远走不通。

        返回值里的 ``prior_steps`` 是「进入本轮之前已累计的工具调用数」，
        用于把结果里的 ``tool_steps`` 裁剪成本轮增量。
        """
        conversation_id = await self._ensure_conversation(conversation_id, user_id)
        # get_context / add_message 都是同步 PG 往返 —— 一律 to_thread。
        # （工具调用早已走 to_thread，唯独会话管理这条路径漏了，会阻塞事件循环）
        prior = await asyncio.to_thread(
            self.conversation_manager.get_context, conversation_id)
        await asyncio.to_thread(
            self.conversation_manager.add_message, conversation_id, "user", user_input)

        config = {"configurable": {"thread_id": conversation_id}}
        values = await self._snapshot_values(config)
        prior_steps = len(values.get("tool_steps") or [])

        state: dict = {
            "messages": [HumanMessage(content=user_input)],
            "user_query": user_input,
            "user_id": user_id,
        }

        if prior and not values.get("messages"):
            seeded = []
            for m in prior[-HISTORY_WINDOW:]:
                if m["role"] == "user":
                    seeded.append(HumanMessage(content=m["content"]))
                else:
                    seeded.append(AIMessage(content=m["content"]))
            if seeded:
                logger.info("seeding %d history message(s) from PostgreSQL (conv=%s)",
                            len(seeded), conversation_id)
                state["messages"] = seeded + state["messages"]
        return conversation_id, config, state, prior_steps

    async def _fallback_response(self, conversation_id: str, user_id: str,
                                 user_input: str = "") -> AgentResponse:
        conversation_id = await self._ensure_conversation(conversation_id, user_id)
        if user_input:
            await asyncio.to_thread(
                self.conversation_manager.add_message, conversation_id, "user", user_input)
        await asyncio.to_thread(
            self.conversation_manager.add_message,
            conversation_id, "assistant", LLM_UNAVAILABLE_MSG,
            {"action_type": "degraded", "reason": "llm_unavailable"},
        )
        return AgentResponse(type="degraded", content=LLM_UNAVAILABLE_MSG,
                             conversation_id=conversation_id)

    @staticmethod
    def _final_from_result(result: dict) -> tuple[str, str, list[dict]]:
        msgs = result.get("messages") or []
        content = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and m.content:
                content = m.content if isinstance(m.content, str) else str(m.content)
                break
        return content, result.get("route", "") or "", result.get("tool_steps") or []

    # ── 异步一次问答 ───────────────────────────────────────────────────
    async def arun(self, user_input: str, conversation_id: str = "",
                   user_id: str = "default") -> AgentResponse:
        if not self.llm_available:
            return await self._fallback_response(conversation_id, user_id, user_input)

        # 先确保会话存在（这样锁的 key 才稳定），再进入会话锁。
        # 锁必须覆盖「落用户消息 → 跑图 → 落助手消息」全过程：否则并发时
        # 每个请求的 user/assistant 两条消息会交错，turn_number 也会错配。
        conversation_id = await self._ensure_conversation(conversation_id, user_id)
        tracer = TraceRecorder(conversation_id=conversation_id, user_id=user_id,
                               user_input=user_input)
        usage = start_usage()

        async with self._lock(conversation_id):
            prep = await self._prepare(user_input, conversation_id, user_id)
            conversation_id, config, state, prior_steps = prep
            try:
                result = await self.graph.ainvoke(state, config)
            except Exception as e:
                logger.exception("agent arun failed (conv=%s)", conversation_id)
                tracer.fail(e)
                await asyncio.to_thread(tracer.save, usage)
                await asyncio.to_thread(
                    self.conversation_manager.add_message,
                    conversation_id, "assistant", GENERIC_FAILURE_MSG,
                    {"action_type": "error"})
                return AgentResponse(type="degraded", content=GENERIC_FAILURE_MSG,
                                     conversation_id=conversation_id)

            content, route, steps = self._final_from_result(result)
            # tool_steps 是累计通道（跨轮保留用于审计），这里裁成本轮增量
            steps = steps[prior_steps:]
            if not content:
                content = GENERIC_FAILURE_MSG

            tracer.set_route(route, result.get("extract", "") or "")
            tracer.add_tools(steps)
            tracer.final_content = content
            await asyncio.to_thread(tracer.save, usage)

            await asyncio.to_thread(
                self.conversation_manager.add_message,
                conversation_id, "assistant", content,
                {"action_type": "final_answer", "route": route,
                 "pending_order": bool(result.get("pending_order"))})

        return AgentResponse(
            type="final_answer", content=content, conversation_id=conversation_id,
            metadata={"route": route, "tool_calls": steps,
                      "usage": usage.as_dict()},
        )

    # ── SSE 流式 ───────────────────────────────────────────────────────
    async def run_stream(self, user_input: str, conversation_id: str = "",
                         user_id: str = "default"):
        """逐 token 产出事件；同时保留节点级进度事件。"""
        if not self.llm_available:
            conversation_id = await self._ensure_conversation(conversation_id, user_id)
            yield {"event": "error",
                   "message": "AI 服务未配置（缺少 LLM_API_KEY），请检查服务端 .env",
                   "conversation_id": conversation_id}
            return

        conversation_id = await self._ensure_conversation(conversation_id, user_id)
        tracer = TraceRecorder(conversation_id=conversation_id, user_id=user_id,
                               user_input=user_input)
        usage = start_usage()

        final_content = ""
        final_route = ""
        final_extract = ""
        streamed: set[str] = set()

        try:
            async with self._lock(conversation_id):
                prep = await self._prepare(user_input, conversation_id, user_id)
                conversation_id, config, state = prep[0], prep[1], prep[2]
                async for mode, payload in self.graph.astream(
                    state, config, stream_mode=["updates", "messages", "custom"]
                ):
                    # ① token 增量
                    # 只转发 AIMessageChunk（真正的流式片），过滤掉节点 return 的
                    # 整条 AIMessage —— 否则「逐字 + 整条」会被推两遍，前端出现重影。
                    if mode == "messages":
                        chunk, meta = payload
                        if not isinstance(chunk, AIMessageChunk):
                            continue
                        node = (meta or {}).get("langgraph_node", "")
                        text = getattr(chunk, "content", "") or ""
                        has_tc = bool(getattr(chunk, "tool_call_chunks", None))
                        if text and not has_tc and node in TOKEN_NODES:
                            streamed.add(node)
                            yield {"event": "token", "node": node, "content": text}
                        continue

                    # ② 自定义事件（工具调用进度）
                    if mode == "custom":
                        if isinstance(payload, dict) and payload.get("event"):
                            yield payload
                        continue

                    # ③ 节点级更新
                    for node_name, node_state in payload.items():
                        tracer.add_node(node_name)
                        display, event_type = _NODE_DISPLAY.get(
                            node_name, (f"执行中: {node_name}", node_name))
                        yield {"event": event_type, "node": node_name,
                               "status": "executing", "message": display}

                        if not isinstance(node_state, dict):
                            continue
                        if node_name == "supervisor":
                            final_route = node_state.get("route", "") or final_route
                            final_extract = node_state.get("extract", "") or final_extract
                            yield {"event": "routing", "node": "supervisor",
                                   "status": "routed", "route": final_route,
                                   "extract": final_extract}
                        tracer.add_tools(node_state.get("tool_steps"))

                        msgs = node_state.get("messages") or []
                        if msgs:
                            last = msgs[-1]
                            text = getattr(last, "content", None) or ""
                            if isinstance(text, str) and text:
                                final_content = text
                                tracer.final_content = text
                                if node_name not in streamed:
                                    yield {"event": "agent_result", "node": node_name,
                                           "status": "done", "content": text,
                                           "route": final_route}

                if streamed:
                    # 流式路径拿不到 usage（未开 stream_options），按字符数估算并标记
                    usage.add(completion_tokens=0,
                              estimated=estimate_tokens(final_content))

                # 收尾也放在会话锁内：保证「用户消息 + 助手消息」成对写入，
                # 并发下 turn_number 才不会错配。
                tracer.set_route(final_route, final_extract)
                if not tracer.final_content:
                    tracer.final_content = final_content
                await asyncio.to_thread(tracer.save, usage)

                if final_content:
                    await asyncio.to_thread(
                        self.conversation_manager.add_message,
                        conversation_id, "assistant", final_content,
                        {"action_type": "final_answer", "route": final_route})

            yield {"event": "done", "conversation_id": conversation_id,
                   "route": final_route, "usage": usage.as_dict()}

        except Exception as e:
            logger.exception("run_stream failed (conv=%s)", conversation_id)
            tracer.fail(e)
            await asyncio.to_thread(tracer.save, usage)
            yield {"event": "error", "message": str(e),
                   "conversation_id": conversation_id}

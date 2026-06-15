"""LangGraphAgent + MasterAgent — single and multi-agent graph wrappers."""
import asyncio
import json
import os
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

from domain.customer_service.graph import build_graph
from domain.customer_service.master_graph import build_master_graph
from domain.customer_service.agent import AgentResponse
from domain.customer_service.prompts import LANGRAPH_SYSTEM_PROMPT

CHECKPOINT_DB_PATH = os.getenv("CHECKPOINT_DB_PATH", "./checkpoints.db")


def _create_checkpointer():
    """Create a persistent AsyncSqliteSaver (supports sync + async), fallback to MemorySaver."""
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
        saver = AsyncSqliteSaver(conn)
        saver.setup()
        return saver
    except Exception:
        return MemorySaver()


class LangGraphAgent:
    def __init__(
        self,
        llm,
        conversation_manager,
        tools: list,
        system_prompt: str | None = None,
        max_steps: int = 5,
        checkpointer=None,
    ):
        self.conversation_manager = conversation_manager
        self.system_prompt = system_prompt or LANGRAPH_SYSTEM_PROMPT
        self.max_steps = max_steps
        self.llm = llm  # LangChainChatModel (already wrapped)
        self.langchain_tools = [t.to_langchain_tool() for t in tools]
        self.llm_with_tools = self.llm.bind_tools(self.langchain_tools)
        self.checkpointer = checkpointer or _create_checkpointer()
        self.graph = build_graph(
            llm_with_tools=self.llm_with_tools,
            langchain_tools=self.langchain_tools,
            system_prompt=self.system_prompt,
            max_steps=max_steps,
            checkpointer=self.checkpointer,
        )

    def run(self, user_input: str, conversation_id: str = "") -> AgentResponse:
        # ── 1. Ensure conversation exists ──
        if not conversation_id:
            conversation_id = self.conversation_manager.create(user_id="default")

        # ── 2. Persist user message ──
        self.conversation_manager.add_message(
            conversation_id, "user", user_input
        )

        # ── 3. Build state and invoke ──
        config = {"configurable": {"thread_id": conversation_id}}
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "step_count": 0,
            "max_steps": self.max_steps,
        }

        result = self.graph.invoke(initial_state, config)

        # ── 4. Extract final answer ──
        messages = result.get("messages", [])
        final_msg = messages[-1] if messages else None
        content = ""
        response_type = "final_answer"

        if final_msg:
            tc = getattr(final_msg, "tool_calls", None)
            if tc:
                # LLM ended with tool_calls but no more steps — fallback
                content = "抱歉，我暂时无法完成您的请求，请再试一次。"
            else:
                content = final_msg.content or "抱歉，没有获取到有效回复。"

        # ── 5. Persist assistant message ──
        self.conversation_manager.add_message(
            conversation_id, "assistant", content,
            {"action_type": response_type},
        )

        return AgentResponse(
            type=response_type,
            content=content,
            conversation_id=conversation_id,
        )


# ── Node-to-display mapping ──
_NODE_DISPLAY = {
    "supervisor": ("正在分析意图...", "routing"),
    "faq_agent": ("FAQ Agent 检索知识库中...", "faq_agent"),
    "order_agent": ("订单 Agent 查询中...", "order_agent"),
    "logistics_agent": ("物流 Agent 查询中...", "logistics_agent"),
    "after_sale_agent": ("售后 Agent 查询中...", "after_sale_agent"),
    "cart_query_agent": ("购物车 Agent 查询中...", "cart_query"),
    "list_orders_agent": ("订单列表 Agent 查询中...", "list_orders"),
    "place_order_agent": ("下单 Agent 处理中...", "place_order_agent"),
    "finish": ("正在生成回复...", "finish"),
}


class MasterAgent:
    """Supervisor-based multi-agent that orchestrates FAQ/Order/Logistics sub-agents.

    Provides both sync run() and async run_stream() for SSE streaming.
    """

    def __init__(self, llm, conversation_manager, tools: list, checkpointer=None):
        self.llm = llm
        self.conversation_manager = conversation_manager
        tool_map = {t.name: t for t in tools}
        self.search_faq_tool = tool_map.get("search_faq")
        self.query_order_tool = tool_map.get("query_order")
        self.query_logistics_tool = tool_map.get("query_logistics")
        self.query_after_sale_tool = tool_map.get("query_after_sale")
        self.query_cart_tool = tool_map.get("query_cart")
        self.list_my_orders_tool = tool_map.get("list_my_orders")
        self.place_order_tool = tool_map.get("place_order")
        self.checkpointer = checkpointer or _create_checkpointer()

        if not all([self.search_faq_tool, self.query_order_tool, self.query_logistics_tool]):
            missing = [n for n, t in [("search_faq", self.search_faq_tool),
                                      ("query_order", self.query_order_tool),
                                      ("query_logistics", self.query_logistics_tool)] if not t]
            raise ValueError(f"Missing required tools: {missing}")

        self.graph = build_master_graph(
            llm=self.llm,
            search_faq_tool=self.search_faq_tool,
            query_order_tool=self.query_order_tool,
            query_logistics_tool=self.query_logistics_tool,
            query_after_sale_tool=self.query_after_sale_tool,
            query_cart_tool=self.query_cart_tool,
            list_my_orders_tool=self.list_my_orders_tool,
            place_order_tool=self.place_order_tool,
            checkpointer=self.checkpointer,
        )

    def run(self, user_input: str, conversation_id: str = "", user_id: str = "default") -> AgentResponse:
        if not conversation_id:
            conversation_id = self.conversation_manager.create(user_id=user_id)

        self.conversation_manager.add_message(conversation_id, "user", user_input)

        config = {"configurable": {"thread_id": conversation_id}}
        state = {
            "messages": [HumanMessage(content=user_input)],
            "route": "",
            "extract": "",
            "user_query": user_input,
            "user_id": user_id,
        }
        result = self.graph.invoke(state, config)

        msgs = result.get("messages", [])
        content = msgs[-1].content if msgs else "抱歉，处理失败。"
        response_type = "final_answer"

        self.conversation_manager.add_message(
            conversation_id, "assistant", content, {"action_type": response_type})

        return AgentResponse(type=response_type, content=content,
                             conversation_id=conversation_id)

    async def run_stream(self, user_input: str, conversation_id: str = "", user_id: str = "default"):
        """Async generator yielding SSE events per graph node."""
        if not conversation_id:
            conversation_id = self.conversation_manager.create(user_id=user_id)

        self.conversation_manager.add_message(conversation_id, "user", user_input)

        config = {"configurable": {"thread_id": conversation_id}}
        state = {
            "messages": [HumanMessage(content=user_input)],
            "route": "",
            "extract": "",
            "user_query": user_input,
            "user_id": user_id,
        }

        final_content = ""
        final_route = ""

        try:
            async for chunk in self.graph.astream(state, config):
                for node_name, node_state in chunk.items():
                    display_msg, event_type = _NODE_DISPLAY.get(
                        node_name, (f"执行中: {node_name}", node_name))

                    # Emit node-start event
                    yield {
                        "event": event_type,
                        "node": node_name,
                        "status": "executing",
                        "message": display_msg,
                    }

                    # Emit routing event if supervisor
                    if node_name == "supervisor":
                        final_route = node_state.get("route", "")
                        yield {
                            "event": "routing",
                            "node": "supervisor",
                            "status": "routed",
                            "route": final_route,
                            "extract": node_state.get("extract", ""),
                        }

                    # Emit result if sub-agent or finish
                    msgs = node_state.get("messages", [])
                    if msgs:
                        last = msgs[-1]
                        text = last.content if hasattr(last, "content") else str(last)
                        if text:
                            final_content = text
                            yield {
                                "event": "agent_result",
                                "node": node_name,
                                "status": "done",
                                "content": text,
                            }

            # Persist assistant message
            if final_content:
                self.conversation_manager.add_message(
                    conversation_id, "assistant", final_content,
                    {"action_type": "final_answer", "route": final_route})

            yield {
                "event": "done",
                "conversation_id": conversation_id,
                "route": final_route,
            }

        except Exception as e:
            yield {
                "event": "error",
                "message": str(e),
                "conversation_id": conversation_id,
            }

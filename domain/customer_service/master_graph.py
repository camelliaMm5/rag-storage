"""Supervisor multi-agent graph: intent routing → sub-agents."""
import json
from typing import TypedDict, Annotated

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

from tools.base import Tool


# ── State ──
class MasterState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    route: str          # faq / order / logistics / place_order / finish
    extract: str        # extracted param (order_id or search query)
    user_query: str     # original user input
    user_id: str        # authenticated user for data isolation


# ── Supervisor prompt ──
SUPERVISOR_PROMPT = """你是一个智能客服的路由调度器。根据用户输入判断意图，输出 JSON 格式的路由决策。

## 路由规则
- 用户要购买/下单/订购商品 → route: "place_order", extract: "用户要买的商品和收货信息"
- 用户询问退款/退货/换货进度、售后工单状态 → route: "after_sale", extract: 订单号或售后工单号
- 用户询问退货政策、保修条款、故障排查、产品参数、使用方法等知识类问题 → route: "faq", extract: 用户问题的核心关键词
- 用户提供订单号并询问订单状态、详情 → route: "order", extract: 订单号
- 用户问"我有哪些订单""我的订单列表""查我的订单"（不带订单号）→ route: "list_orders", extract: ""
- 用户询问快递、物流进度、到哪了 → route: "logistics", extract: 订单号或快递单号
- 用户询问购物车有什么、购物车里有什么、查看购物车 → route: "cart_query", extract: ""
- 打招呼、闲聊、无法判断 → route: "finish", extract: ""

## 输出格式
必须严格输出 JSON，不要输出其他内容：
{{"route": "place_order|faq|order|logistics|after_sale|cart_query|list_orders|finish", "extract": "提取的关键参数"}}

## 用户输入
{user_input}"""


# ── Sub-agent prompts ──
FAQ_AGENT_PROMPT = """你是智能家居 FAQ 助手。根据检索结果回答问题。只基于检索内容回答，不要编造。"""

ORDER_AGENT_PROMPT = """你是订单查询助手。根据查询结果回复用户。"""

LOGISTICS_AGENT_PROMPT = """你是物流查询助手。根据查询结果回复用户。"""

AFTER_SALE_AGENT_PROMPT = """你是售后查询助手。根据查询结果回复用户，告知售后工单的处理状态和进度。"""

CART_QUERY_PROMPT = """你是购物车查询助手。根据查询结果回复用户，告知购物车中有哪些商品、各自数量和总金额。"""

LIST_ORDERS_PROMPT = """你是订单列表查询助手。根据查询结果回复用户，列出用户的所有订单及其状态。"""

PLACE_ORDER_PROMPT = """你是下单助手。用户想要购买商品，请根据用户提供的信息，调用工具帮用户下单。
从用户的输入中提取：商品名(product)、收件人(recipient)、地址(address)、金额(amount，可选)。

请以 JSON 格式输出下单参数：
{{"product": "商品名", "recipient": "收件人", "address": "地址", "amount": 金额}}"""


def _run_tool_and_format(tool: Tool, extract: str, user_id: str = "") -> str:
    """Run a tool with the extracted param and user_id for data isolation."""
    required = [p for p in tool.parameters if p.required]
    params = {required[0].name: extract} if required else {}
    params["user_id"] = user_id
    return tool.run(params)


def create_supervisor_node(llm, tools_by_name: dict):
    """Factory: supervisor node that does intent routing."""

    def supervisor_node(state: MasterState) -> dict:
        user_query = state.get("user_query", "")
        msgs = state.get("messages", [])
        if msgs and isinstance(msgs[-1], HumanMessage):
            user_query = msgs[-1].content

        prompt = SUPERVISOR_PROMPT.format(user_input=user_query)
        resp = llm.invoke([HumanMessage(content=prompt)])

        route = "finish"
        extract = ""
        try:
            text = resp.content if hasattr(resp, "content") else str(resp)
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(text[start:end])
                route = parsed.get("route", "finish")
                extract = parsed.get("extract", "")
        except (json.JSONDecodeError, KeyError):
            pass

        return {
            "route": route,
            "extract": extract,
            "user_query": user_query,
        }

    return supervisor_node


def create_sub_agent_node(tool: Tool, agent_name: str, llm, system_prompt: str):
    """Factory: sub-agent node that runs a tool and summarizes results."""

    def sub_agent_node(state: MasterState) -> dict:
        extract = state.get("extract", "")
        user_id = state.get("user_id", "")

        tool_result = _run_tool_and_format(tool, extract, user_id=user_id)

        summary_prompt = (
            f"{system_prompt}\n\n"
            f"用户想了解的信息如下。请用自然友好的语气回复：\n\n{tool_result}"
        )
        summary_resp = llm.invoke([HumanMessage(content=summary_prompt)])
        summary_text = summary_resp.content if hasattr(summary_resp, "content") else str(summary_resp)

        return {
            "messages": [AIMessage(content=summary_text)],
        }

    return sub_agent_node


def create_finish_node(llm):
    """Factory: finish node for greeting/chitchat/fallback."""

    def finish_node(state: MasterState) -> dict:
        user_query = state.get("user_query", "")
        msgs = state.get("messages", [])
        if msgs and isinstance(msgs[-1], HumanMessage):
            user_query = msgs[-1].content

        prompt = (
            "你是智能家居品牌的 AI 客服。用户说：" + user_query + "\n"
            "请友好回复，告知你可以帮助查询订单、物流，或回答产品使用和售后问题。"
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = resp.content if hasattr(resp, "content") else str(resp)

        return {
            "messages": [AIMessage(content=text)],
        }

    return finish_node


def route_decision(state: MasterState) -> str:
    """Conditional edge: route to the correct sub-agent."""
    return state.get("route", "finish")


def create_place_order_node(llm, place_order_tool: Tool):
    """Factory: place order node that extracts params and creates order."""

    def place_order_node(state: MasterState) -> dict:
        extract = state.get("extract", "")
        user_query = state.get("user_query", "")
        user_id = state.get("user_id", "")
        msgs = state.get("messages", [])
        if msgs and isinstance(msgs[-1], HumanMessage):
            user_query = msgs[-1].content

        parse_prompt = (
            PLACE_ORDER_PROMPT + f"\n\n用户输入：{user_query}\n说明：{extract}\n请输出JSON："
        )
        resp = llm.invoke([HumanMessage(content=parse_prompt)])
        text = resp.content if hasattr(resp, "content") else str(resp)

        params = {"product": "未知商品", "recipient": "用户", "address": "未知地址", "amount": 0}
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(text[start:end])
                params.update(parsed)
        except (json.JSONDecodeError, KeyError):
            pass

        params["user_id"] = user_id
        tool_result = place_order_tool.run(params)

        summary_resp = llm.invoke([HumanMessage(content=(
            "你是下单助手。以下是下单结果，请用热情友好的语气告知用户：\n\n" + tool_result
        ))])
        summary_text = summary_resp.content if hasattr(summary_resp, "content") else str(summary_resp)

        return {"messages": [AIMessage(content=summary_text)]}

    return place_order_node


def build_master_graph(
    llm,
    search_faq_tool: Tool,
    query_order_tool: Tool,
    query_logistics_tool: Tool,
    query_after_sale_tool: Tool | None = None,
    query_cart_tool: Tool | None = None,
    list_my_orders_tool: Tool | None = None,
    place_order_tool: Tool | None = None,
    checkpointer=None,
) -> object:
    """Build the Supervisor multi-agent graph with user data isolation."""
    workflow = StateGraph(MasterState)

    tools_by_name = {
        "faq": search_faq_tool,
        "order": query_order_tool,
        "logistics": query_logistics_tool,
    }
    route_map = {
        "faq": "faq_agent",
        "order": "order_agent",
        "logistics": "logistics_agent",
        "after_sale": "after_sale_agent",
        "cart_query": "cart_query_agent",
        "list_orders": "list_orders_agent",
        "finish": "finish",
    }

    workflow.add_node("supervisor", create_supervisor_node(llm, tools_by_name))
    workflow.add_node("faq_agent", create_sub_agent_node(
        search_faq_tool, "faq", llm, FAQ_AGENT_PROMPT))
    workflow.add_node("order_agent", create_sub_agent_node(
        query_order_tool, "order", llm, ORDER_AGENT_PROMPT))
    workflow.add_node("logistics_agent", create_sub_agent_node(
        query_logistics_tool, "logistics", llm, LOGISTICS_AGENT_PROMPT))
    workflow.add_node("finish", create_finish_node(llm))

    if query_after_sale_tool:
        workflow.add_node("after_sale_agent", create_sub_agent_node(
            query_after_sale_tool, "after_sale", llm, AFTER_SALE_AGENT_PROMPT))
    else:
        route_map.pop("after_sale", None)

    if query_cart_tool:
        workflow.add_node("cart_query_agent", create_sub_agent_node(
            query_cart_tool, "cart_query", llm, CART_QUERY_PROMPT))
    else:
        route_map.pop("cart_query", None)

    if list_my_orders_tool:
        workflow.add_node("list_orders_agent", create_sub_agent_node(
            list_my_orders_tool, "list_orders", llm, LIST_ORDERS_PROMPT))
    else:
        route_map.pop("list_orders", None)

    if place_order_tool:
        workflow.add_node("place_order_agent", create_place_order_node(llm, place_order_tool))
        route_map["place_order"] = "place_order_agent"

    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges("supervisor", route_decision, route_map)

    workflow.add_edge("faq_agent", END)
    workflow.add_edge("order_agent", END)
    workflow.add_edge("logistics_agent", END)
    workflow.add_edge("finish", END)
    if query_after_sale_tool:
        workflow.add_edge("after_sale_agent", END)
    if query_cart_tool:
        workflow.add_edge("cart_query_agent", END)
    if list_my_orders_tool:
        workflow.add_edge("list_orders_agent", END)
    if place_order_tool:
        workflow.add_edge("place_order_agent", END)

    return workflow.compile(checkpointer=checkpointer)

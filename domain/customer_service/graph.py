"""LangGraph graph definition: agent ⇄ tools ReAct cycle."""
from typing import TypedDict, Annotated

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import BaseMessage, SystemMessage


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    max_steps: int


def _has_system_message(messages: list[BaseMessage]) -> bool:
    return any(isinstance(m, SystemMessage) for m in messages)


def create_agent_node(llm, system_prompt: str):
    """Factory: returns an agent node function with llm and system_prompt in closure."""

    def agent_node(state: AgentState) -> dict:
        messages = list(state["messages"])

        if system_prompt and not _has_system_message(messages):
            messages = [SystemMessage(content=system_prompt)] + messages

        response = llm.invoke(messages)
        return {
            "messages": [response],
            "step_count": state.get("step_count", 0) + 1,
        }

    return agent_node


def should_continue(state: AgentState) -> str:
    """Conditional edge: check if LLM wants to call tools."""
    messages = state["messages"]
    if not messages:
        return END

    if state.get("step_count", 0) >= state.get("max_steps", 5):
        return END

    last = messages[-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


def build_graph(llm_with_tools, langchain_tools: list, system_prompt: str,
                max_steps: int = 5, checkpointer=None):
    """Build and compile the agent ⇄ tools graph."""
    workflow = StateGraph(AgentState)

    workflow.add_node("agent", create_agent_node(llm_with_tools, system_prompt))
    workflow.add_node("tools", ToolNode(langchain_tools))

    workflow.set_entry_point("agent")

    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", END: END},
    )
    workflow.add_edge("tools", "agent")

    return workflow.compile(checkpointer=checkpointer)

"""LangChain BaseChatModel 适配器 —— 把 ChatService 接进 LangGraph。

三个关键点：
1. ``_generate``：非流式（Supervisor 路由、工具决策的兜底路径）。
2. ``_stream``：**真流式**，逐 delta yield ``ChatGenerationChunk``。LangGraph 的
   ``astream(stream_mode="messages")`` 依赖它才能产出 token 级事件；
   工具调用的增量也必须原样透传（``tool_call_chunks``），否则带工具的流式响应
   会丢失 tool_calls。
3. ``_bound_tools`` 用 pydantic ``PrivateAttr`` 声明 —— 之前是用
   ``setattr`` 挂私有属性，在 pydantic v2 下属于未声明字段，行为依赖模型配置，
   不够稳。
"""
import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from llm.client import ChatService


class LangChainChatModel(BaseChatModel):
    chat_service: ChatService

    _bound_tools: list | None = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "langchain-adapter"

    @property
    def available(self) -> bool:
        """上游 LLM 是否已配置可用。

        MasterAgent 用它决定要不要直接走降级话术，避免没配 API Key 时抛 500。
        """
        return bool(getattr(self.chat_service, "available", True))

    # ── 消息转换 ───────────────────────────────────────────────────────
    def _convert_message(self, msg: BaseMessage) -> dict:
        """把单条 LangChain 消息转成 OpenAI JSON 格式。"""
        if isinstance(msg, HumanMessage):
            return {"role": "user", "content": msg.content}
        if isinstance(msg, AIMessage):
            d: dict = {"role": "assistant", "content": msg.content or ""}
            tc = getattr(msg, "tool_calls", None) or []
            if tc:
                d["tool_calls"] = [
                    {
                        "id": c.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("args") or {},
                                                    ensure_ascii=False),
                        },
                    }
                    for c in tc
                ]
            return d
        if isinstance(msg, SystemMessage):
            return {"role": "system", "content": msg.content}
        if isinstance(msg, ToolMessage):
            content = msg.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            return {
                "role": "tool",
                "content": content,
                "tool_call_id": msg.tool_call_id,
            }
        return {"role": "user", "content": str(msg.content)}

    @property
    def _openai_tools(self) -> list | None:
        return self._bound_tools or None

    # ── 非流式 ─────────────────────────────────────────────────────────
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        """注意：``run_manager`` 必须显式声明。

        langchain-core 的 ``_agenerate`` 默认实现会把它**作为位置参数**传给
        ``_generate``；签名里缺了它就会
        ``TypeError: _generate() takes from 2 to 3 positional arguments but 4 were given``。
        这个错误只在真实调用链上出现（离线脚本模型自己声明了该参数，测不出来）。
        """
        openai_messages = [self._convert_message(m) for m in messages]
        resp = self.chat_service.call_raw(openai_messages, tools=self._openai_tools)

        choice = resp.choices[0]
        content = choice.message.content or ""

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append({"name": tc.function.name, "args": args, "id": tc.id})

        payload = {"content": content}
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return ChatResult(generations=[ChatGeneration(message=AIMessage(**payload))])

    # ── 真流式 ─────────────────────────────────────────────────────────
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ):
        """逐 delta 产出 chunk。工具调用增量按 index 透传，交由 LangChain 合并。"""
        openai_messages = [self._convert_message(m) for m in messages]

        for delta in self.chat_service.stream_raw(openai_messages,
                                                  tools=self._openai_tools):
            content = delta.get("content") or ""
            chunks = []
            for tc in delta.get("tool_calls") or []:
                chunks.append({
                    "name": tc.get("name") or None,
                    "args": tc.get("arguments") or "",
                    "id": tc.get("id") or None,
                    "index": tc.get("index", 0),
                })
            if content or chunks:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=content, tool_call_chunks=chunks)
                )

    # ── 工具绑定 ───────────────────────────────────────────────────────
    def bind_tools(self, tools: list[StructuredTool], **kwargs: Any) -> "LangChainChatModel":
        """返回一个绑定了 OpenAI function-calling schema 的新实例。"""
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.args_schema.model_json_schema()
                    if hasattr(t.args_schema, "model_json_schema")
                    else t.args_schema,
                },
            }
            for t in tools
        ]
        new_model = self.__class__(chat_service=self.chat_service)
        new_model._bound_tools = openai_tools
        return new_model

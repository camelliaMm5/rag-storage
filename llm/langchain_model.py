"""LangChain BaseChatModel adapter — wraps ChatService for use in LangGraph."""
import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

from llm.client import ChatService


class LangChainChatModel(BaseChatModel):
    chat_service: ChatService

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "langchain-adapter"

    def _convert_message(self, msg: BaseMessage) -> dict:
        """Convert a single LangChain message to OpenAI JSON format."""
        if isinstance(msg, HumanMessage):
            return {"role": "user", "content": msg.content}
        if isinstance(msg, AIMessage):
            d: dict = {"role": "assistant", "content": msg.content}
            tc = getattr(msg, "tool_calls", None) or []
            if tc:
                d["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["args"], ensure_ascii=False),
                        },
                    }
                    for tc in tc
                ]
            return d
        if isinstance(msg, SystemMessage):
            return {"role": "system", "content": msg.content}
        if isinstance(msg, ToolMessage):
            return {
                "role": "tool",
                "content": msg.content,
                "tool_call_id": msg.tool_call_id,
            }
        return {"role": "user", "content": str(msg.content)}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        openai_messages = [self._convert_message(m) for m in messages]

        tools = None
        if hasattr(self, "_bound_tools") and self._bound_tools:
            tools = self._bound_tools

        resp = self.chat_service.call_raw(openai_messages, tools=tools)

        choice = resp.choices[0]
        raw_content = choice.message.content
        content = raw_content if raw_content else ""

        tool_calls = []
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments),
                    "id": tc.id,
                }
                for tc in choice.message.tool_calls
            ]

        kwargs = {"content": content}
        if tool_calls:
            kwargs["tool_calls"] = tool_calls
        msg = AIMessage(**kwargs)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools: list[StructuredTool], **kwargs: Any) -> "LangChainChatModel":
        """Return a new instance with tools bound as OpenAI function-calling schemas."""
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

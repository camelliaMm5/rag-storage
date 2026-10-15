"""脚本化 LLM —— 用于离线（不接真实模型）驱动 Agent 图。

放在这里而不是各个测试里各写一份：测试与演示脚本都依赖它，
「同一份脚本模型」也保证了离线行为在各处一致。

脚本元素可以是：
  - ``"文本"``                                   → 直接作为回答
  - ``{"tool": "query_order", "args": {...}}``   → 发起一次工具调用
  - ``{"echo": True}``                           → 回显收到的 prompt
    （用于断言「上一步到底喂了什么给 LLM」，这是验证历史注入最直接的证据）
"""
import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class ScriptedChatModel(BaseChatModel):
    script: list = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        """绑定工具后仍返回自身 —— 脚本顺序需要在整条链路上共享。"""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        idx = min(self.calls, len(self.script) - 1) if self.script else 0
        item = self.script[idx] if self.script else ""
        self.calls += 1

        if isinstance(item, dict) and item.get("echo"):
            text = "\n".join(str(getattr(m, "content", m)) for m in messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

        if isinstance(item, dict) and item.get("tool"):
            msg = AIMessage(
                content=item.get("content", ""),
                tool_calls=[{
                    "name": item["tool"],
                    "args": item.get("args") or {},
                    "id": f"call_{self.calls}",
                }],
            )
            return ChatResult(generations=[ChatGeneration(message=msg)])

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=str(item)))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """真流式：逐字符 yield AIMessageChunk。

        为什么必须实现：``run_stream`` 只转发 ``AIMessageChunk``（用来剔除节点
        return 的整条 ``AIMessage`` 造成的重影）。若离线模型不流式，LangChain 的
        默认兜底会产出 ``AIMessage``，token 事件就一个都不会有 —— T12 会直接红。
        这里与生产适配器 ``LangChainChatModel._stream`` 保持同构。
        """
        item = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        msg = item.generations[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=msg.content or "",
                tool_call_chunks=[{
                    "name": tc.get("name"),
                    "args": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                    "id": tc.get("id") or f"call_{i}",
                    "index": i,
                } for i, tc in enumerate(tool_calls)],
            ))
            return
        for ch in (msg.content or ""):
            yield ChatGenerationChunk(message=AIMessageChunk(content=ch))

"""假 OpenAI 客户端 —— 让 LangChainChatModel 的真实调用链可以离线跑。

作用：``LangChainChatModel`` 是自研适配器，它和 langchain-core 之间有一份
隐式契约（``_generate`` 的形参、``_stream`` 的 chunk 形态、tool_call 增量拼接）。
这份契约一旦漂移，脚本化模型是测不出来的 —— 例如 ``_generate`` 少声明一个
``run_manager``，只有在真实 ``ainvoke`` 上才会 TypeError。
用这个假客户端把契约固定下来，就不必依赖网络与 API Key。
"""
from __future__ import annotations


class Obj:
    """极简的属性包（模拟 openai SDK 的返回对象）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _content_chunks(parts: list[str]):
    for p in parts:
        yield Obj(choices=[Obj(delta=Obj(content=p, tool_calls=None))])
    yield Obj(choices=[])


def _tool_call_chunks(fragments: list[str], name: str = "query_order",
                      call_id: str = "call_1"):
    """按 OpenAI 增量格式切分 arguments，用于验证片段拼接。"""
    yield Obj(choices=[Obj(delta=Obj(
        content="",
        tool_calls=[Obj(index=0, id=call_id,
                        function=Obj(name=name, arguments=fragments[0]))],
    ))])
    for frag in fragments[1:]:
        yield Obj(choices=[Obj(delta=Obj(
            content=None,
            tool_calls=[Obj(index=0, id=None, function=Obj(name=None, arguments=frag))],
        ))])
    yield Obj(choices=[])


class FakeCompletions:
    def __init__(self, *, stream_parts=None, stream_tool_args=None):
        self.stream_parts = stream_parts or ["流", "式", "回答"]
        self.stream_tool_args = stream_tool_args

    def create(self, **kwargs):
        if kwargs.get("stream"):
            if self.stream_tool_args is not None:
                return _tool_call_chunks(self.stream_tool_args)
            return _content_chunks(self.stream_parts)

        if kwargs.get("tools"):
            msg = Obj(
                content=None,
                tool_calls=[Obj(id="call_1", function=Obj(
                    name="query_order",
                    arguments='{"order_id": "1001"}',
                ))],
            )
        else:
            msg = Obj(content="非流式回答", tool_calls=None)

        return Obj(
            choices=[Obj(message=msg)],
            usage=Obj(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = Obj(completions=FakeCompletions(**kwargs))


def fake_chat_service(**kwargs):
    """返回一个已注入假客户端的 ChatService（不触发任何网络调用）。"""
    from llm.client import ChatService

    service = ChatService()
    service.api_key = "fake-key"
    service._client = FakeClient(**kwargs)
    return service

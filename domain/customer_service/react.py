"""受限 ReAct 循环 —— 子 Agent 的决策内核。

设计取舍（为什么不直接用 ``langgraph.prebuilt.create_react_agent`` 做子图）：
- 子图会继承父图 checkpointer，state 命名空间需要额外处理，而本项目的
  子 Agent 只是「一次请求内的短循环」，不需要独立持久化；
- 本项目工具是自定义 ``Tool`` 包装（非 LangChain 原生 tool），需要一个能被
  离线 ``ScriptedChatModel`` 确定性驱动的循环，便于回归测试。

关键点：**为什么用 ``astream`` 而不是 ``ainvoke``**
外层 ``run_stream`` 依赖 ``graph.astream(stream_mode="messages")`` 拿到 token
增量才能实现打字机效果。``ainvoke`` 走 ``_generate``，不会产生 stream 事件；
只有 ``astream``（走 ``_stream``）才会。工具决策调用（要调工具的那次）通常
``content`` 为空，因此不会把「我要调用 query_order」这类内部计划泄露给用户。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 4
DEFAULT_WINDOW = 12

# 单次工具调用的对外超时（秒）。工具是同步函数、跑在 asyncio 的默认线程池里，
# 池上限是 min(32, cpu+4)；若某次 DB 查询卡住（锁等待 / 连接半死），不设超时
# 就会永久占用一个工作线程，几次之后工具层整体饿死。
TOOL_TIMEOUT_SEC = float(os.getenv("TOOL_TIMEOUT_SEC", "8"))

FALLBACK_AFTER_MAX_STEPS = (
    "抱歉，我试了几次仍没能拿到需要的信息。\n"
    "可以再具体一点吗？例如直接给我订单号，或者告诉我商品名称。"
)
TOOL_ERROR_TEMPLATE = "查询暂时不可用（{err}），请稍后重试。"
TOOL_TIMEOUT_TEMPLATE = "查询超时（超过 {sec:.0f} 秒），请稍后重试。"


@dataclass
class ToolStep:
    """一次工具调用的完整记录（同时用于 trace 落库）。"""

    name: str
    args: dict
    observation: str
    duration_ms: int

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "args": self.args,
            "observation": (self.observation or "")[:500],
            "duration_ms": self.duration_ms,
        }


@dataclass
class ReactOutcome:
    content: str
    steps: list[ToolStep] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def used_tools(self) -> bool:
        return bool(self.steps)

    def tool_calls(self) -> list[dict]:
        return [s.as_dict() for s in self.steps]


def _stream_writer():
    """LangGraph 自定义流事件写入器；不在流式上下文时返回 None。"""
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # pragma: no cover - 取决于 langgraph 版本/调用上下文
        return None


def _inject_context(tool, args: dict, context: dict | None) -> dict:
    """把服务端可信上下文（如 user_id）注入工具参数。

    安全要点：**只注入工具自己声明的参数名**，且以服务端值为准覆盖 LLM 给的
    同名参数。这样：
    - 鉴别主体永远来自 JWT（``state["user_id"]``），LLM 无法伪造越权身份；
    - 不会把 user_id 硬塞给不需要它的工具（如 search_faq）导致参数校验失败。
    """
    if not context:
        return args
    declared = {p.name for p in getattr(tool, "parameters", []) if p.name in context}
    if not declared:
        return args
    return {**args, **{k: context[k] for k in declared}}


async def _call_tool(tool, args: dict, context: dict | None = None) -> tuple[str, int]:
    """在子线程执行同步工具（工具内有 DB/网络 I/O，不能阻塞事件循环）。

    超时说明：``asyncio.to_thread`` 的线程**无法被真正取消**，``wait_for``
    到期只是让调用方快速失败、不再堆积请求；被卡住的线程会随查询自身结束而回收。
    因此这里必须配 ``statement_timeout``（见 shop_database 连接池）双保险。
    """
    call_args = _inject_context(tool, args, context)
    name = getattr(tool, "name", "?")
    t0 = time.perf_counter()
    try:
        obs = await asyncio.wait_for(
            asyncio.to_thread(tool.run, call_args), timeout=TOOL_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        logger.warning("tool %s timed out after %.1fs", name, TOOL_TIMEOUT_SEC)
        obs = TOOL_TIMEOUT_TEMPLATE.format(sec=TOOL_TIMEOUT_SEC)
    except Exception as e:
        logger.exception("tool %s failed", name)
        obs = TOOL_ERROR_TEMPLATE.format(err=type(e).__name__)
    return str(obs), int((time.perf_counter() - t0) * 1000)


async def run_react(
    *,
    llm,
    tools_by_name: dict,
    system_prompt: str,
    messages: list[BaseMessage],
    max_steps: int = DEFAULT_MAX_STEPS,
    window: int = DEFAULT_WINDOW,
    user_context: dict | None = None,
) -> ReactOutcome:
    """跑一次受限 ReAct 循环，返回最终回答与工具调用轨迹。

    ``llm`` 必须是已绑定工具的模型（bind_tools 后的实例）。
    ``user_context`` 是服务端可信上下文（目前仅 ``user_id``），会注入到声明了
    该参数的工具上——鉴权主体必须来自服务端，不能听 LLM 的。
    """
    convo: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    convo.extend(list(messages)[-window:])

    steps: list[ToolStep] = []
    writer = _stream_writer()
    llm_calls = 0

    for _ in range(max(1, max_steps)):
        aggregated = None
        async for chunk in llm.astream(convo):
            aggregated = chunk if aggregated is None else aggregated + chunk
        llm_calls += 1

        if aggregated is None:
            return ReactOutcome("抱歉，我没有生成任何回复，请再试一次。", steps, llm_calls)

        calls = getattr(aggregated, "tool_calls", None) or []
        if not calls:
            # 没有工具调用 → 这一轮就是最终回答
            return ReactOutcome(aggregated.content or "", steps, llm_calls)

        convo.append(aggregated)
        for call in calls:
            name = call.get("name") or ""
            args = call.get("args") or {}
            if writer:
                try:
                    writer({"event": "tool", "name": name, "status": "start",
                            "args": args})
                except Exception:
                    pass

            tool = tools_by_name.get(name)
            if tool is None:
                observation, ms = f"未知工具 {name}", 0
            else:
                observation, ms = await _call_tool(tool, args, user_context)

            # 记录「生效参数」（含注入的 user_id），便于 trace 复盘
            effective = _inject_context(tool, args, user_context) if tool else args
            steps.append(ToolStep(name=name, args=effective,
                                  observation=observation, duration_ms=ms))
            if writer:
                try:
                    writer({"event": "tool", "name": name, "status": "done",
                            "duration_ms": ms})
                except Exception:
                    pass
            convo.append(ToolMessage(content=observation,
                                     tool_call_id=call.get("id") or name))

    logger.warning("react loop hit max_steps=%s without a final answer", max_steps)
    return ReactOutcome(FALLBACK_AFTER_MAX_STEPS, steps, llm_calls)

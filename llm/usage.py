"""LLM 调用计量 —— 通过 ContextVar 把「本次请求的用量」传给适配器。

为什么用 ContextVar 而不是把计数器挂到 ChatService 实例上：
``ChatService`` 是进程级单例，多个并发请求共用一个实例，挂实例上会互相污染；
而 ``_generate`` 可能在 ``run_in_executor`` 的线程里执行，直接用线程局部变量
又拿不到调用方设置的值。

ContextVar 的语义正好合适：``asyncio`` 任务之间互相隔离，且复制上下文时
**复制的是对象引用**，因此子线程里对同一个 ``Usage`` 对象做累加，父任务能读到。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Usage:
    """一次请求内的 LLM 用量累计。"""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_tokens: int = 0
    calls: list[dict] = field(default_factory=list)

    def add(self, *, prompt_tokens: int = 0, completion_tokens: int = 0,
            model: str = "", streamed: bool = False, estimated: int = 0) -> None:
        self.llm_calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.estimated_tokens += estimated
        self.calls.append({
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "streamed": streamed,
            "estimated": bool(estimated),
        })

    def as_dict(self) -> dict:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_tokens": self.estimated_tokens,
        }


usage_var: ContextVar[Usage | None] = ContextVar("llm_usage", default=None)


def start_usage() -> Usage:
    """开始一次计量，返回可累加的 Usage 对象。"""
    u = Usage()
    usage_var.set(u)
    return u


def record_usage(**kwargs) -> None:
    """由 LLM 适配器调用；没有开启计量时静默忽略。"""
    u = usage_var.get()
    if u is not None:
        u.add(**kwargs)


def estimate_tokens(text: str) -> int:
    """流式响应拿不到 usage 时的粗略估算。

    中文约 1 token ≈ 1.5 字符，英文 1 token ≈ 4 字符，这里按 2 字符/token 折中，
    并在 trace 里标记 ``estimated=True``，避免被当成精确值使用。
    """
    if not text:
        return 0
    return max(1, len(text) // 2)

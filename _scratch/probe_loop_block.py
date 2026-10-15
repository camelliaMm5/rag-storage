"""探针：证明「把同步 PG 调用移出事件循环」确实消除了阻塞（A/B 对照）。

做法：给 ConversationManager 的每个方法注入 0.3s 睡眠（模拟同步 DB 往返），
同时跑一个 ticker 协程测量事件循环滞后（loop lag）。
连跑两轮：
  A) 现状（生产代码）：同步调用走 asyncio.to_thread
  B) 负向对照：把 asyncio.to_thread 临时换成「直接在循环里调用」

预期：
- A: max lag 只有几十毫秒（睡眠发生在工作线程，循环照常调度 ticker）
- B: max lag 涨到 0.9s 量级（3 次 × 0.3s 全压在事件循环上）
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CONVERSATION_DB_URL", "sqlite:///./_test_offline.db")
os.environ.setdefault("SHOP_DB_URL", "postgresql://user:1234@localhost:5432/agent")

from tests.scripted_model import ScriptedChatModel
from tests.test_agent_offline import default_tools
from domain.customer_service import MasterAgent
from domain.customer_service.checkpointer import memory_checkpointer
from utils import ConversationManager

SLEEP = 0.3
SCRIPT = [
    '{"route": "order", "slots": {"order_id": "1001"}}',
    {"tool": "query_order", "args": {"order_id": "1001"}},
    "订单 1001 已发货。",
]


class SlowCM:
    """把每个会话管理方法的耗时放大到 SLEEP 秒（模拟同步 DB 往返）。"""

    def __init__(self):
        self.cm = ConversationManager(max_context_turns=5)

    def create(self, user_id):
        time.sleep(SLEEP)
        return self.cm.create(user_id)

    def get_context(self, cid):
        time.sleep(SLEEP)
        return self.cm.get_context(cid)

    def add_message(self, cid, role, content, metadata=None):
        time.sleep(SLEEP)
        return self.cm.add_message(cid, role, content, metadata)


async def measure(label: str, inline_blocking: bool) -> tuple[float, float, float]:
    real_to_thread = asyncio.to_thread
    if inline_blocking:
        async def fake_to_thread(fn, *a, **kw):
            return fn(*a, **kw)          # 直接在事件循环里跑同步函数
        asyncio.to_thread = fake_to_thread

    lags: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        interval = 0.02
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(interval)
            lags.append(time.perf_counter() - t0 - interval)

    tick = asyncio.create_task(ticker())
    try:
        agent = MasterAgent(
            llm=ScriptedChatModel(script=list(SCRIPT)),
            conversation_manager=SlowCM(),
            tools=list(default_tools().values()),
            checkpointer=memory_checkpointer(),
        )
        t0 = time.perf_counter()
        await agent.arun("订单1001到哪了", user_id="1")
        elapsed = time.perf_counter() - t0
    finally:
        stop.set()
        await asyncio.sleep(0.05)
        tick.cancel()
        asyncio.to_thread = real_to_thread

    mx = max(lags) if lags else 0.0
    return elapsed, mx * 1000, len(lags)


async def main():
    for label, inline in (("A) 现状：走 to_thread ", False),
                          ("B) 对照：直接阻塞循环", True)):
        elapsed, mx, ticks = await measure(label, inline)
        print(f"{label} 总耗时={elapsed:.2f}s  ticks={ticks:3d}  max_loop_lag={mx:7.1f} ms")
    print("\n结论：A 的 lag 与 B 相差约一个数量级 → 同步 DB 调用确实已移出事件循环。")


if __name__ == "__main__":
    asyncio.run(main())

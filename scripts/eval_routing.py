"""路由准确率评测 —— 用标注样本量化 Supervisor 的意图分类与抽槽能力。

为什么这比单元测试更重要：
单测只能证明「拿到某个 route 之后图走对了」，证明不了「LLM 给的 route 对不对」。
路由准确率是这套 Agent 最核心的业务指标，也是「改了 prompt 会不会退化」的
回归闸门 —— 每次改 prompt / 换模型后跑一遍，对比准确率即可。

用法：
    python scripts/eval_routing.py            # 全部样本
    python scripts/eval_routing.py -v         # 打印每条明细

说明：需要真实 LLM_API_KEY；未配置时脚本会提示并退出（不会假装通过）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage

from domain.customer_service.master_graph import create_supervisor_node
from llm import LangChainChatModel, chat_service

# (用户输入, 期望 route, 期望槽位子集)
CASES: list[tuple[str, str, dict]] = [
    ("帮我查一下订单1001的状态", "order", {"order_id": "1001"}),
    ("订单 1001 用了多少钱", "order", {"order_id": "1001"}),
    ("订单1001的快递到哪了", "logistics", {"order_id": "1001"}),
    ("我的包裹什么时候能到", "logistics", {}),
    ("我有哪些订单", "list_orders", {}),
    ("看看我的订单列表", "list_orders", {}),
    ("我的购物车有什么", "cart_query", {}),
    ("你当前有什么商品在售", "product", {}),
    ("你们卖什么商品", "product", {}),
    ("退货政策是什么", "faq", {}),
    ("这个门锁保修多久", "faq", {}),
    ("门锁连不上网怎么办", "faq", {}),
    ("我的售后工单处理到哪一步了", "after_sale", {}),
    ("退款什么时候能到账", "after_sale", {}),
    ("我想买一个智能门锁", "place_order", {"product": "智能门锁"}),
    ("帮我下单 X1 智能门锁，寄到广州市天河区体育西路1号", "place_order",
     {"address": "广州市天河区体育西路1号"}),
    ("你好", "finish", {}),
    ("今天天气不错", "finish", {}),
]


async def main():
    verbose = "-v" in sys.argv
    if not chat_service.available:
        print("未配置 LLM_API_KEY —— 无法评测路由准确率。")
        print("请在 .env 中配置后重跑：")
        print("  LLM_API_KEY=sk-...\n  LLM_BASE_URL=https://api.deepseek.com/v1\n"
              "  LLM_MODEL=deepseek-chat")
        return 2

    llm = LangChainChatModel(chat_service=chat_service)
    node = create_supervisor_node(llm)

    route_hit = 0
    slot_total = 0
    slot_hit = 0
    detail_lines = []

    for text, want_route, want_slots in CASES:
        try:
            out = await node({"messages": [HumanMessage(content=text)],
                              "user_query": text, "user_id": "1"})
        except Exception as e:
            out = {"route": f"<error: {type(e).__name__}>", "slots": {}}

        got_route = out.get("route")
        got_slots = out.get("slots") or {}
        ok = got_route == want_route
        route_hit += int(ok)

        slot_ok = True
        for k, v in want_slots.items():
            slot_total += 1
            if str(got_slots.get(k) or "").strip() == v:
                slot_hit += 1
            else:
                slot_ok = False

        mark = "OK " if (ok and slot_ok) else "MISS"
        detail_lines.append(
            f"  [{mark}] {text[:34]:<36} want={want_route:<12} got={got_route:<12}"
            f" slots={got_slots}"
        )

    if verbose:
        print("\n".join(detail_lines))

    n = len(CASES)
    print()
    print("=" * 66)
    print(f"路由准确率: {route_hit}/{n} = {route_hit / n:.1%}")
    if slot_total:
        print(f"抽槽准确率: {slot_hit}/{slot_total} = {slot_hit / slot_total:.1%}")
    print("=" * 66)
    if not verbose:
        print("（加 -v 查看每条明细）")
    return 0 if route_hit == n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""场景验收 —— 用脚本化 LLM 离线回放真实图（不接真实模型、不起服务）。

为什么保留这个脚本（而不是只留单元测试）：
它输出的是**可读的多轮对话剧本**，用来向人演示「Agent 到底会怎么走」——
路由到哪个节点、调了哪个工具、反问还是下单。答辩/交接时比一堆 assert 更好用。

用法：
    python scripts/scenario_test.py            # 全部离线场景
    python scripts/scenario_test.py --full     # 额外打印在线手测步骤
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CONVERSATION_DB_URL", "sqlite:///./_scenario.db")
os.environ.setdefault("SHOP_DB_URL", "postgresql://user:1234@localhost:5432/agent")

from domain.customer_service import MasterAgent, persistent_checkpointer
from domain.customer_service.routing import normalize_route, resolve_target
from tests.scripted_model import ScriptedChatModel
from tools.base import Tool, ToolParameter
from tools.faq_search import search_faq_tool
from tools.order_search import (
    list_my_orders_tool, query_cart_tool, query_logistics_tool, query_order_tool,
    search_products_tool,
)
from tools.after_sale_search import query_after_sale_tool
from utils import ConversationManager

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


def head(title):
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def show(turn, user, resp):
    route = (resp.metadata or {}).get("route", "")
    tools = [s["name"] for s in ((resp.metadata or {}).get("tool_calls") or [])]
    print(f"  用户> {user}")
    print(f"  客服> {resp.content.strip()[:220]}")
    print(f"        └ route={route or '-'}  tools={tools or '-'}")


def stub(name, result, param="q", required=True):
    return Tool(name=name, description=f"{name} (stub)", func=lambda **kw: result,
                parameters=[ToolParameter(param, "string", "param", required=required,
                                          default=None if required else "")])


STUB_TOOLS = [
    stub("search_faq", "退货需在签收后 7 天内发起申请，商品需保持完好。", "query"),
    stub("query_order", "订单号 1001  状态 已支付  金额 ¥1599.00", "order_id"),
    stub("query_logistics", "顺丰快递 SF1234567890  运输中（广州分拣中心）", "order_id"),
    stub("list_my_orders", "#1001 [已支付] ¥1599.00", "user_id", required=False),
    stub("query_cart", "您的购物车是空的。", "user_id", required=False),
    stub("query_after_sale", "该订单暂无售后工单。", "order_id", required=False),
]

REAL_TOOLS = [
    search_faq_tool, query_order_tool, query_logistics_tool, list_my_orders_tool,
    query_cart_tool, query_after_sale_tool, search_products_tool,
]


def agent_with(llm, saver, tools=None):
    return MasterAgent(llm=llm, conversation_manager=ConversationManager(max_context_turns=6),
                       tools=tools or STUB_TOOLS, checkpointer=saver)


def pg_ok():
    try:
        from infrastructure.shop_database import query_one
        query_one("SELECT 1 AS ok")
        return True
    except Exception:
        return False


async def run_scenarios(saver):
    head("场景 1  订单查询 + 指代消解（「它到哪了」）")
    llm = ScriptedChatModel(script=[
        '{"route": "order", "slots": {"order_id": "1001"}}',
        {"tool": "query_order", "args": {"order_id": "1001"}},
        "订单 1001 已支付，金额 ¥1599.00，正在安排发货。",
        '{"route": "logistics", "slots": {}}',
        {"tool": "query_logistics", "args": {"order_id": "1001"}},
        "您的包裹由顺丰承运，当前在广州分拣中心，运输中。",
    ])
    agent = agent_with(llm, saver)
    r1 = await agent.arun("订单1001到哪了？", user_id="1")
    show(0, "订单1001到哪了？", r1)
    check("第一轮路由到查询类 Agent 并调用了工具",
          bool((r1.metadata or {}).get("tool_calls")))
    r2 = await agent.arun("那它到哪了？", conversation_id=r1.conversation_id, user_id="1")
    show(1, "那它到哪了？", r2)
    steps = (r2.metadata or {}).get("tool_calls") or []
    check("第二轮靠跨轮槽位记忆继承了订单号 1001",
          bool(steps) and str(steps[0]["args"].get("order_id")) == "1001", str(steps))

    head("场景 2  澄清闭环：缺订单号先反问，补参后续跑")
    llm2 = ScriptedChatModel(script=[
        '{"route": "logistics", "slots": {}}',
        '{"route": "finish", "slots": {}}',
        {"tool": "query_logistics", "args": {"order_id": "1001"}},
        "查到了：顺丰 SF1234567890，运输中。",
    ])
    agent2 = agent_with(llm2, saver)
    c1 = await agent2.arun("我的快递到哪了？", user_id="1")
    show(0, "我的快递到哪了？", c1)
    check("反问而不是拿空订单号去查", "订单号" in c1.content or "工单号" in c1.content,
          c1.content[:80])
    c2 = await agent2.arun("1001", conversation_id=c1.conversation_id, user_id="1")
    show(1, "1001", c2)
    check("只回一个订单号就能续跑",
          bool((c2.metadata or {}).get("tool_calls")), str(c2.metadata))

    head("场景 3  下单两阶段：先回显确认，确认后才落库")
    if not pg_ok():
        print("  [SKIP] Postgres 不可用，跳过下单场景")
    else:
        from infrastructure.shop_database import query_one
        from domain.shop.order import OrderService

        uid = query_one("SELECT id FROM shop.users ORDER BY id LIMIT 1")["id"]
        prod = query_one(
            "SELECT id, name, stock FROM shop.products "
            "WHERE status='on_sale' AND stock > 3 ORDER BY id LIMIT 1")
        before = query_one("SELECT count(*) c FROM shop.orders")["c"]

        llm3 = ScriptedChatModel(script=[
            '{"route": "place_order", "slots": {"product": "%s", '
            '"address": "广州市天河区体育西路 1 号", "recipient": "camelliam"}}' % prod["name"],
            '{"route": "place_order", "slots": {}}',
        ])
        agent3 = agent_with(llm3, saver, tools=REAL_TOOLS)
        o1 = await agent3.arun(f"我要买一个 {prod['name']}，寄到广州市天河区体育西路 1 号，"
                               f"收件人 camelliam", user_id=str(uid))
        show(0, "我要买一个 …", o1)
        check("prepare 阶段要求确认", "确认" in o1.content, o1.content[:80])
        check("prepare 阶段未写库",
              query_one("SELECT count(*) c FROM shop.orders")["c"] == before)

        o2 = await agent3.arun("确认", conversation_id=o1.conversation_id, user_id=str(uid))
        show(1, "确认", o2)
        check("确认后下单成功", "下单成功" in o2.content, o2.content[:80])
        row = query_one("SELECT id, recipient FROM shop.orders ORDER BY id DESC LIMIT 1")
        check("收件人已落库（旧版会被丢弃）", row["recipient"] == "camelliam",
              str(row["recipient"]))
        await asyncio.to_thread(OrderService.cancel, row["id"], uid)

        head("场景 4  数据隔离：A 的订单号，B 查不到")
        llm4 = ScriptedChatModel(script=[
            '{"route": "order", "slots": {"order_id": "%s"}}' % row["id"],
            {"tool": "query_order", "args": {"order_id": str(row["id"])}},
            "抱歉，未找到该订单。",
        ])
        other = query_one("SELECT id FROM shop.users WHERE id <> %s ORDER BY id LIMIT 1", (uid,))
        if other:
            agent4 = agent_with(llm4, saver, tools=REAL_TOOLS)
            o = await agent4.arun(f"订单{row['id']}什么状态？", user_id=str(other["id"]))
            show(0, f"订单{row['id']}什么状态？(他人账号)", o)
            obs = ((o.metadata or {}).get("tool_calls") or [{}])[0].get("observation", "")
            check("跨用户查询被数据隔离挡住", "未找到" in str(obs) or "未找到" in o.content,
                  str(obs)[:80])

    head("场景 5  闲聊兜底")
    llm5 = ScriptedChatModel(script=[
        '{"route": "finish", "slots": {}}',
        "你好呀！我可以帮你查订单、查物流、查售后、看购物车，也能直接帮你下单～",
    ])
    agent5 = agent_with(llm5, saver)
    r = await agent5.arun("你好", user_id="1")
    show(0, "你好", r)
    check("闲聊走 finish 且不调工具",
          (r.metadata or {}).get("route") == "finish"
          and not (r.metadata or {}).get("tool_calls"), str(r.metadata))


def run_static_checks():
    head("静态检查  route 归一化 / resolve_target / 图结构")
    check("order_status → order", normalize_route("order_status") == "order")
    check("未知值 → finish（不 KeyError）", normalize_route("banana") == "finish")
    check("缺槽位 → clarify", resolve_target({"route": "order", "slots": {}}) == "clarify")
    check("commit → place_order_commit",
          resolve_target({"route": "place_order", "order_action": "commit", "slots": {}})
          == "place_order_commit")

    from domain.customer_service.master_graph import AGENT_SPECS, build_master_graph
    from domain.customer_service.prompts import SUPERVISOR_SYSTEM_PROMPT

    graph = build_master_graph(llm=ScriptedChatModel(script=["x"]),
                               tools_by_name={t.name: t for t in REAL_TOOLS})
    nodes = set(getattr(graph, "nodes", {}).keys())
    expected = {"supervisor", "finish", "clarify", "faq_agent", "order_agent",
                "logistics_agent", "after_sale_agent", "cart_query_agent",
                "list_orders_agent", "place_order_prepare", "place_order_commit",
                "place_order_cancel"}
    check("图中包含全部预期节点", expected.issubset(nodes),
          f"missing={sorted(expected - nodes)}")
    for route in ["order", "logistics", "after_sale", "faq", "place_order", "finish"]:
        check(f"SUPERVISOR_SYSTEM_PROMPT 覆盖 route '{route}'",
              route in SUPERVISOR_SYSTEM_PROMPT)
    check("子 Agent 配置非空", len(AGENT_SPECS) >= 6, str(sorted(AGENT_SPECS)))

    from infrastructure.auth import create_access_token, decode_token
    import jwt as _jwt

    token = create_access_token("zhangsan")
    check("JWT 生成与解码", decode_token(token).get("sub") == "zhangsan")
    try:
        decode_token("invalid.token.here")
        check("非法 token 抛错", False, "未抛异常")
    except _jwt.InvalidTokenError:
        check("非法 token 抛错", True)


async def main():
    tmp = os.path.join(tempfile.gettempdir(), "scenario_ckpt.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    run_static_checks()
    async with persistent_checkpointer(tmp) as saver:
        await run_scenarios(saver)

    head(f"结果：{PASSED}/{PASSED + FAILED} 通过，{FAILED} 失败")
    if "--full" in sys.argv:
        print()
        print("在线手测（需先 `python main.py` 起服务）：")
        print("  1) TOKEN=$(curl -s -X POST localhost:8000/api/token "
              "-H 'Content-Type: application/json' -d '{\"user_id\":\"1\"}' "
              "| python -c \"import sys,json;print(json.load(sys.stdin)['access_token'])\")")
        print("  2) curl -N -X POST localhost:8000/api/chat/stream "
              "-H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' "
              "-d '{\"message\":\"订单1001到哪了\"}'")
        print("  3) 观察 SSE：routing → <agent> → tool → token… → done")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

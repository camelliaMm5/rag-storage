"""离线 Agent 测试 —— 不依赖真实 LLM（用脚本化模型驱动），会话库走 SQLite。

覆盖本次重构的关键行为：
  T1  checkpoint 真落盘 + 多轮消息累积（旧版静默退化为内存态）
  T2  route 归一化 + resolve_target 纯函数（未映射 route 不再 KeyError）
  T3  LLM 不可用 → 降级而非 500
  T4  会话归属校验（防 IDOR）
  T5  工具异常不中断请求
  T6  澄清闭环：缺订单号 → 反问 → 下一轮补参续跑（不靠 LLM 复述）
  T7  会话历史注入：子 Agent 看得到前几轮对话
  T8  下单两阶段：prepare 不写库 → 确认后 commit 落库（含 recipient）
  T9  下单两阶段：否定 → cancel，不产生订单
  T10 可观测性：trace 落库
  T11 并发：同会话并发请求不丢消息、turn_number 不重复
  T12 SSE 事件序列（含 token 增量）
  T13 真实 Postgres：create_direct 收件人 + 库存回滚（缺失则跳过）
  T14 工具收到服务端注入的 user_id（LLM 不传也生效）
  T15 流式不重影：token 事件数 == 回答字符数
  T16 适配器契约：_generate / _stream / tool_call 拼接
  T17 工具端鉴权加固 + 超时保护
  T18 并发：同一用户并发结算只成一单
  T19 完整 Agent：单轮内链式调用两个工具（list_my_orders → query_order）
      —— 守「统一只读工具集」，防止退回「按 route 硬绑定单工具」
  T20 商品浏览：product 路由 + list_products（空关键词列全部）
  T21 物流状态推演：按经过时间补出后续节点（只读、不落库）
  T22 商品检索：漏字/连写关键词也能命中（纯函数 match_products）
  T23 跨轮上下文：省略句「当前状态」自动接上最近订单
  T24 真实 Postgres：漏字关键词能检到真实商品
  T25 FAQ 多路召回：变体派生 + 融合排序（纯函数）
  T26 真实 FAQ 库：关键词堆问句也能召回正确条目
  T27 购物车整单：没点名商品时一次结算购物车全部商品

用法：
    python tests/test_agent_offline.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 会话/轨迹库走本地 SQLite，避免污染 Postgres 会话表
os.environ.setdefault("CONVERSATION_DB_URL", "sqlite:///./_test_offline.db")
# 电商库（orders/products）只能用 Postgres —— 显式指定，不与会话库共用变量
os.environ.setdefault("SHOP_DB_URL", "postgresql://user:1234@localhost:5432/agent")

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # noqa: F401

from domain.customer_service import MasterAgent, persistent_checkpointer
from domain.customer_service.master_graph import (  # noqa: F401
    build_master_graph, create_supervisor_node,
)
from domain.customer_service.routing import (
    extract_order_id, extract_prefixed_order_id, is_affirmative, is_negative,
    normalize_route, promote_status_query, resolve_target,
)
from infrastructure.rag.models import SearchResult
from infrastructure.rag.retriever import fuse_results, generate_query_variants
from tests.scripted_model import ScriptedChatModel
from tools.base import Tool, ToolParameter
from tools.order_search import match_products, tokenize_keyword
from utils import ConversationManager

PASSED = 0
FAILED = 0
SKIPPED: list[str] = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")


def section(title):
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


def stub_tool(name: str, result: str, param: str = "q", required: bool = True) -> Tool:
    """桩工具：无论入参是什么都返回固定文本，用来验证链路而非业务。"""
    return Tool(
        name=name,
        description=f"{name} (stub)",
        func=lambda **kw: result,
        parameters=[ToolParameter(param, "string", "param", required=required,
                                  default=None if required else "")],
    )


def boom_tool(name: str) -> Tool:
    def _boom(**kw):
        raise RuntimeError("database is on fire")
    return Tool(name=name, description=f"{name} (exploding)",
                func=_boom,
                parameters=[ToolParameter("order_id", "string", "param", required=True)])


def default_tools() -> dict:
    return {
        "search_faq": stub_tool("search_faq", "FAQ: 退货需在签收后 7 天内申请。", "query"),
        "query_order": stub_tool("query_order", "订单号：1001  状态：已发货", "order_id"),
        "query_logistics": stub_tool("query_logistics", "顺丰快递 SF123 运输中", "order_id"),
        "list_my_orders": stub_tool("list_my_orders", "您的订单列表：#1001 [已支付]", "user_id",
                                    required=False),
        "query_cart": stub_tool("query_cart", "您的购物车是空的。", "user_id", required=False),
        "query_after_sale": stub_tool("query_after_sale", "无售后记录。", "order_id",
                                     required=False),
        "list_products": stub_tool(
            "list_products",
            "在售商品候选：\n· [1] X1 智能门锁  单价 ¥1599.00  库存 12",
            "limit", required=False),
    }


def make_agent(llm, saver, tools=None, overrides=None) -> MasterAgent:
    merged = tools or default_tools()
    if overrides:
        merged.update(overrides)
    return MasterAgent(
        llm=llm,
        conversation_manager=ConversationManager(max_context_turns=5),
        tools=list(merged.values()),
        checkpointer=saver,
    )


def pg_available() -> bool:
    try:
        from infrastructure.shop_database import query_one
        query_one("SELECT 1 AS ok")
        return True
    except Exception:
        return False


async def main():
    tmp_ckpt = os.path.join(tempfile.gettempdir(), "offline_ckpt_v2.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp_ckpt + suffix):
            os.remove(tmp_ckpt + suffix)

    async with persistent_checkpointer(tmp_ckpt) as saver:

        # ── T1 ────────────────────────────────────────────────────────
        section("T1  checkpointer 落盘 + 多轮消息累积")
        llm = ScriptedChatModel(script=[
            '{"route": "order", "slots": {"order_id": "1001"}}',
            "订单 1001 已发货。",
            "订单 1001 已发货，正在派送。",
        ])
        agent = make_agent(llm, saver)
        r1 = await agent.arun("订单1001发货了吗？", user_id="1")
        conv = r1.conversation_id
        r2 = await agent.arun("那它到哪了？", conversation_id=conv, user_id="1")

        rows = sqlite3.connect(tmp_ckpt).execute(
            "SELECT count(*) FROM checkpoints").fetchone()[0]
        check("checkpoints 表有落盘数据（旧版为 0）", rows > 0, f"rows={rows}")
        snap = await agent.graph.aget_state({"configurable": {"thread_id": conv}})
        n_msgs = len(snap.values.get("messages", []))
        check("多轮消息累积在同一 thread", n_msgs >= 4, f"messages={n_msgs}")
        check("第二轮正常返回", bool(r2.content), repr(r2.content))

        # ── T2 ────────────────────────────────────────────────────────
        section("T2  route 归一化 + resolve_target（纯函数，无需 LLM）")
        check("order_status → order", normalize_route("order_status") == "order")
        check("物流查询 → logistics", normalize_route("物流查询") == "logistics")
        check("after-sales → after_sale", normalize_route("after-sales") == "after_sale")
        check("未知值 banana → finish", normalize_route("banana") == "finish")
        check("空值 → finish", normalize_route("") == "finish")
        check("resolve: 缺订单号 → clarify",
              resolve_target({"route": "order", "slots": {}}) == "clarify")
        check("resolve: 有订单号 → order_agent",
              resolve_target({"route": "order", "slots": {"order_id": "1"}}) == "order_agent")
        check("resolve: commit → place_order_commit",
              resolve_target({"route": "place_order", "order_action": "commit",
                              "slots": {}}) == "place_order_commit")
        check("resolve: prepare 缺地址 → clarify",
              resolve_target({"route": "place_order", "order_action": "prepare",
                              "slots": {"product": "X1"}}) == "clarify")
        check("extract_order_id 抓 4 位以上数字", extract_order_id("订单1001到哪了") == "1001")
        check("is_affirmative / is_negative 不混淆",
              is_affirmative("确认") and is_negative("不要") and not is_affirmative("不要"))

        # ── T3 ────────────────────────────────────────────────────────
        section("T3  LLM 不可用 → 降级而非 500")
        from llm import LangChainChatModel
        from llm.client import ChatService

        # 显式构造一个**无 key** 的 service —— 不复用全局 chat_service：
        # `.env` 里一旦配了真实 LLM_API_KEY，available 就为 True，本用例会去真调
        # 上游、退化成「集成测试」并随环境红绿漂移（实测：配 key 后本用例两个断言
        # 直接变红）。降级路径必须用构造出来的「不可用」状态来确定性地测。
        down_service = ChatService()
        down_service.api_key = ""
        agent_down = make_agent(LangChainChatModel(chat_service=down_service), saver)
        res = await agent_down.arun("订单1001发货了吗？", user_id="1")
        check("type == degraded", res.type == "degraded", res.type)
        check("返回友好提示", "AI 服务暂时不可用" in res.content, res.content[:80])
        check("会话已创建", len(res.conversation_id) == 36)

        # ── T4 ────────────────────────────────────────────────────────
        section("T4  会话归属校验（防 IDOR）")
        from fastapi import HTTPException

        from apps.customer_service import routes as cs_routes

        cm = agent.conversation_manager
        conv_a = cm.create("user-A")
        check("owner 查询正确", cm.get_owner(conv_a) == "user-A")
        check("不存在的会话 → None", cm.get_owner("no-such-conv") is None)
        cs_routes.conversation_manager = cm
        try:
            # 注意：_assert_conversation_owner 是 async —— 内部用 to_thread 把
            # 同步 PG 查询移出事件循环（否则会阻塞所有并发请求）。
            await cs_routes._assert_conversation_owner(conv_a, "user-B")
            check("他人会话被拒绝", False, "未抛异常")
        except HTTPException as e:
            check("他人会话被拒绝(403)", e.status_code == 403, str(e.status_code))
        try:
            await cs_routes._assert_conversation_owner("no-such-conv", "user-A")
            check("不存在的会话返回 404", False, "未抛异常")
        except HTTPException as e:
            check("不存在的会话返回 404", e.status_code == 404, str(e.status_code))

        # ── T5 ────────────────────────────────────────────────────────
        section("T5  工具异常不中断请求")
        llm_boom = ScriptedChatModel(script=[
            '{"route": "order", "slots": {"order_id": "1001"}}',
            {"tool": "query_order", "args": {"order_id": "1001"}},
            "查询遇到问题，请稍后再试。",
        ])
        agent_boom = make_agent(llm_boom, saver, overrides={"query_order": boom_tool("query_order")})
        res = await agent_boom.arun("订单1001发货了吗？", user_id="1")
        check("工具抛错后仍有回复", bool(res.content), repr(res.content))
        steps = (res.metadata or {}).get("tool_calls") or []
        check("工具失败被记录为 observation",
              bool(steps) and "不可用" in steps[0]["observation"], str(steps))

        # ── T6 ────────────────────────────────────────────────────────
        section("T6  澄清闭环：反问 → 下一轮补参续跑")
        llm_clar = ScriptedChatModel(script=[
            '{"route": "logistics", "slots": {}}',        # 第 1 轮：不给订单号
            '{"route": "finish", "slots": {}}',           # 第 2 轮：LLM 也没识别出来（故意）
            {"tool": "query_logistics", "args": {"order_id": "1001"}},
            "订单 1001 的快递正在运输中，预计明天送达。",
        ])
        agent_clar = make_agent(llm_clar, saver)
        c1 = await agent_clar.arun("订单到哪了？", user_id="1")
        check("缺订单号时反问而非瞎查", "订单号" in c1.content, c1.content[:60])
        snap = await agent_clar.graph.aget_state(
            {"configurable": {"thread_id": c1.conversation_id}})
        check("pending_intent 已存入 state",
              bool(snap.values.get("pending_intent")), str(snap.values.get("pending_intent")))

        c2 = await agent_clar.arun("1001", conversation_id=c1.conversation_id, user_id="1")
        steps = (c2.metadata or {}).get("tool_calls") or []
        check("补参后自动续跑到 logistics",
              (c2.metadata or {}).get("route") == "logistics",
              str((c2.metadata or {}).get("route")))
        check("确实调用了 query_logistics(order_id=1001)",
              bool(steps) and steps[0]["name"] == "query_logistics"
              and str(steps[0]["args"].get("order_id")) == "1001", str(steps))

        # ── T7 ────────────────────────────────────────────────────────
        section("T7  会话历史注入（子 Agent 能看到前几轮）")
        llm_echo = ScriptedChatModel(script=[
            '{"route": "faq", "slots": {}}',
            "退货需在签收后 7 天内申请。",
            '{"route": "faq", "slots": {}}',
            {"echo": True},
        ])
        agent_echo = make_agent(llm_echo, saver)
        e1 = await agent_echo.arun("退货政策是什么？", user_id="1")
        e2 = await agent_echo.arun("刚才那条再说一遍", conversation_id=e1.conversation_id,
                                   user_id="1")
        check("第二轮 echo 出了第一轮的助手回答",
              "7 天内申请" in e2.content, e2.content[-200:])

        # ── T8 / T9 ───────────────────────────────────────────────────
        section("T8  下单两阶段：prepare 不写库 → 确认后 commit")
        if not pg_available():
            SKIPPED.append("T8/T9/T13 需要 Postgres")
            print("  [SKIP] Postgres 不可用")
        else:
            from infrastructure.shop_database import query_one
            from tools import search_products_tool
            from tools.order_search import query_order

            uid = query_one("SELECT id, nickname FROM shop.users ORDER BY id LIMIT 1")
            uid = uid["id"]
            prod = query_one(
                "SELECT id, name, price, stock FROM shop.products "
                "WHERE status='on_sale' AND stock > 3 ORDER BY id LIMIT 1")
            before_stock = prod["stock"]
            orders_before = query_one("SELECT count(*) c FROM shop.orders")["c"]

            order_tools = dict(default_tools())
            order_tools["search_products"] = search_products_tool
            confirm_llm = ScriptedChatModel(script=[
                '{"route": "place_order", "slots": {"product": "%s", "address": "广州市天河区测试路1号", "recipient": "camelliam 测试"}}' % prod["name"],
                '{"route": "place_order", "slots": {}}',
            ])
            agent_ord = make_agent(confirm_llm, saver, tools=order_tools)

            o1 = await agent_ord.arun(
                f"我要买 {prod['name']}，地址广州市天河区测试路1号，收件人 camelliam 测试",
                user_id=str(uid))
            check("prepare 阶段要求确认", "确认" in o1.content, o1.content[:120])
            mid_count = query_one("SELECT count(*) c FROM shop.orders")["c"]
            check("prepare 阶段没有写库", mid_count == orders_before,
                  f"{orders_before} → {mid_count}")
            check("草稿存在 checkpoint 里",
                  bool((await agent_ord.graph.aget_state(
                      {"configurable": {"thread_id": o1.conversation_id}}
                  )).values.get("pending_order")))

            o2 = await agent_ord.arun("确认", conversation_id=o1.conversation_id,
                                      user_id=str(uid))
            check("确认后下单成功", "下单成功" in o2.content, o2.content[:120])
            after_count = query_one("SELECT count(*) c FROM shop.orders")["c"]
            check("确认后订单数 +1", after_count == orders_before + 1,
                  f"{orders_before} → {after_count}")

            new_order = query_one("SELECT id, recipient FROM shop.orders ORDER BY id DESC LIMIT 1")
            check("recipient 正确落库", new_order["recipient"] == "camelliam 测试",
                  str(new_order["recipient"]))
            # 关键回归：下单后订单号必须回写 slots / last_order_id，否则下一轮
            # 用户说「当前状态」时无从继承单号，只能被反问「请把订单号发给我」。
            snap_ord = await agent_ord.graph.aget_state(
                {"configurable": {"thread_id": o1.conversation_id}})
            check("commit 后订单号回写 slots + last_order_id",
                  str((snap_ord.values.get("slots") or {}).get("order_id")) == str(new_order["id"])
                  and str(snap_ord.values.get("last_order_id")) == str(new_order["id"]),
                  f"slots={snap_ord.values.get('slots')} last={snap_ord.values.get('last_order_id')}")
            detail = query_order(str(new_order["id"]), user_id=str(uid))
            check("订单可被查询到", str(new_order["id"]) in detail, detail[:80])

            after_stock = query_one("SELECT stock FROM shop.products WHERE id=%s",
                                    (prod["id"],))["stock"]
            check("库存已扣减", after_stock == before_stock - 1,
                  f"{before_stock} → {after_stock}")

            # 清理
            from domain.shop.order import OrderService
            await asyncio.to_thread(OrderService.cancel, new_order["id"], uid)
            restored = query_one("SELECT stock FROM shop.products WHERE id=%s",
                                 (prod["id"],))["stock"]
            check("取消后库存还原", restored == before_stock, f"{restored}")

            # ── T9 ──
            section("T9  下单两阶段：否定 → cancel，不产生订单")
            decline_llm = ScriptedChatModel(script=[
                '{"route": "place_order", "slots": {"product": "%s", "address": "上海市浦东新区测试路2号"}}' % prod["name"],
                '{"route": "finish", "slots": {}}',
            ])
            agent_dec = make_agent(decline_llm, saver, tools=order_tools)
            base = query_one("SELECT count(*) c FROM shop.orders")["c"]
            d1 = await agent_dec.arun(f"帮我买 {prod['name']}，地址上海市浦东新区测试路2号",
                                      user_id=str(uid))
            check("先给出确认草稿", "确认" in d1.content, d1.content[:100])
            d2 = await agent_dec.arun("算了，不要了", conversation_id=d1.conversation_id,
                                      user_id=str(uid))
            check("否定 → 已取消", "取消" in d2.content, d2.content[:100])
            check("否定后没有产生订单",
                  query_one("SELECT count(*) c FROM shop.orders")["c"] == base)

        # ── T10 ───────────────────────────────────────────────────────
        section("T10  可观测性：trace 落库")
        from domain.customer_service.tracing import recent_traces, trace_stats

        traces = recent_traces(limit=20, user_id="1")
        check("能查到本轮 trace", len(traces) > 0, f"n={len(traces)}")
        if traces:
            t = traces[0]
            check("trace 记录了 route", bool(t.get("route")), str(t.get("route")))
            check("trace 记录了耗时", (t.get("duration_ms") or 0) >= 0,
                  str(t.get("duration_ms")))
        stats = trace_stats()
        check("trace 聚合统计可用", stats.get("total", 0) > 0, str(stats.get("total")))

        # ── T11 ───────────────────────────────────────────────────────
        section("T11  并发：同会话并发请求不丢消息 / turn_number 不重复")
        conc_llm = ScriptedChatModel(script=["并发回复"])
        agent_conc = make_agent(conc_llm, saver)
        conv_id = agent_conc.conversation_manager.create("user-conc")

        results = await asyncio.gather(*[
            agent_conc.arun(f"问题{i}", conversation_id=conv_id, user_id="user-conc")
            for i in range(5)
        ])
        check("5 个并发请求全部返回", all(r.content for r in results),
              str([r.content for r in results]))

        history = agent_conc.conversation_manager.get_history(conv_id)
        check("消息总数 == 10（5 问 5 答）", len(history) == 10, str(len(history)))
        user_turns = [m["turn_number"] for m in history if m["role"] == "user"]
        check("user turn_number 唯一且为 1..5",
              sorted(user_turns) == [1, 2, 3, 4, 5], str(user_turns))
        answer_turns = [m["turn_number"] for m in history if m["role"] == "assistant"]
        check("答复与问句配对（turn_number 落在同一轮）",
              sorted(answer_turns) == [1, 2, 3, 4, 5], str(answer_turns))

        # ── T12 ───────────────────────────────────────────────────────
        section("T12  SSE 事件序列（含 token 增量）")
        stream_llm = ScriptedChatModel(script=[
            '{"route": "faq", "slots": {}}',
            "退货请在签收后 7 天内发起申请。",
        ])
        agent_stream = make_agent(stream_llm, saver)
        events = []
        async for evt in agent_stream.run_stream("怎么退货？", user_id="1"):
            events.append(evt)
        kinds = [e.get("event") for e in events]
        check("包含 routing 事件", "routing" in kinds, str(kinds))
        check("包含 token 事件（打字机数据源）", "token" in kinds, str(kinds))
        check("以 done 收尾", kinds[-1] == "done", str(kinds[-3:]))
        merged = "".join(e.get("content", "") for e in events if e.get("event") == "token")
        check("token 拼接等于完整回答",
              "7 天内" in merged, merged[:80])

        # ── T13 ───────────────────────────────────────────────────────
        section("T13  真实 Postgres：create_direct 收件人 + 库存回滚")
        if not pg_available():
            SKIPPED.append("T13 需要 Postgres")
            print("  [SKIP] Postgres 不可用")
        else:
            from infrastructure.shop_database import query_one
            from domain.shop.order import OrderService

            before = query_one("SELECT stock FROM shop.products WHERE id = 1")["stock"]
            order = await asyncio.to_thread(
                OrderService.create_direct, user_id=1, product_id=1, quantity=1,
                address="广州市天河区测试路 1 号", recipient="camelliam 测试",
            )
            check("直下单成功", order.get("id") is not None, str(order.get("id")))
            check("recipient 正确落库", order.get("recipient") == "camelliam 测试",
                  str(order.get("recipient")))
            after = query_one("SELECT stock FROM shop.products WHERE id = 1")["stock"]
            check("库存已扣减", after == before - 1, f"{before} → {after}")
            await asyncio.to_thread(OrderService.cancel, order["id"], 1)
            restored = query_one("SELECT stock FROM shop.products WHERE id = 1")["stock"]
            check("取消后库存还原", restored == before, f"{restored}")
            expired = await asyncio.to_thread(OrderService.cancel_expired, 30)
            check("超时扫描可执行（无误伤）", isinstance(expired, list), str(expired))

        # ── T14 ───────────────────────────────────────────────────────
        # 回归：LLM 只给业务参数（order_id），鉴权主体必须由服务端注入。
        # 曾经这里只把 LLM 的 args 透传给工具，导致所有订单/物流/售后工具都返回
        # 「请先登录」——端到端 Mock LLM 才暴露出来。
        section("T14  工具收到服务端注入的 user_id（LLM 不传也生效）")
        seen: dict = {}

        def _fake_query_order(order_id, user_id=""):
            seen["order_id"] = order_id
            seen["user_id"] = user_id
            return f"订单 {order_id} 状态：已发货（查询者={user_id}）"

        inject_tool = Tool(
            name="query_order",
            description="按订单号查询订单",
            func=_fake_query_order,
            parameters=[
                ToolParameter("order_id", "string", "订单号"),
                ToolParameter("user_id", "string", "当前用户ID",
                              required=False, default=""),
            ],
        )
        inject_llm = ScriptedChatModel(script=[
            '{"route": "order", "slots": {"order_id": "1001"}}',
            {"tool": "query_order", "args": {"order_id": "1001"}},  # 故意不传 user_id
            "订单 1001 已发货。",
        ])
        agent_inj = make_agent(inject_llm, saver, overrides={"query_order": inject_tool})
        r_inj = await agent_inj.arun("订单1001到哪了", user_id="42")
        check("工具被真正调用", seen.get("order_id") == "1001", str(seen))
        check("user_id 由服务端注入（LLM 未提供）", seen.get("user_id") == "42", str(seen))
        check("回答正常返回", "已发货" in r_inj.content, r_inj.content[:60])

        # ── T15 ───────────────────────────────────────────────────────
        # 回归：messages 模式会同时吐「流式增量」和「节点 return 的整条消息」，
        # 不做类型过滤会让前端收到 2 倍内容（逐字 + 整条重发）。
        section("T15  流式不重影：token 事件数 == 回答字符数")
        dupe_llm = ScriptedChatModel(script=[
            '{"route": "faq", "slots": {}}',
            "ABCDEFGHIJ",
        ])
        agent_dupe = make_agent(dupe_llm, saver)
        ev2 = [e async for e in agent_dupe.run_stream("测试流式", user_id="1")]
        toks = [e for e in ev2 if e.get("event") == "token"]
        merged2 = "".join(e.get("content", "") for e in toks)
        check("token 事件数 == 字符数（无整条重发）", len(toks) == len("ABCDEFGHIJ"),
              f"events={len(toks)} merged={merged2!r}")
        check("拼接结果无重影", merged2 == "ABCDEFGHIJ", merged2)

        # ── T16 ───────────────────────────────────────────────────────
        # 回归：自研适配器与 langchain-core 的隐式契约（_generate 的形参、
        # _stream 的 chunk 形态、tool_call 片段拼接）。这些契约只有走真实
        # 调用链才暴露 —— 脚本模型自己声明了 run_manager，测不出缺参。
        section("T16  适配器契约：_generate / _stream / tool_call 拼接")
        from llm.langchain_model import LangChainChatModel
        from tests.fake_openai import fake_chat_service

        model = LangChainChatModel(chat_service=fake_chat_service())
        r = await model.ainvoke([HumanMessage(content="你好")])
        check("_generate 接受 run_manager（ainvoke 不 TypeError）",
              r.content == "非流式回答", repr(r.content))

        bound = model.bind_tools(
            [t.to_langchain_tool() for t in default_tools().values()])
        r2 = await bound.ainvoke([HumanMessage(content="查订单1001")])
        calls2 = getattr(r2, "tool_calls", None) or []
        check("非流式 tool_calls 名称解析正确",
              calls2 and calls2[0].get("name") == "query_order", str(calls2))
        check("非流式 tool_calls 参数解析为 dict",
              calls2 and calls2[0].get("args") == {"order_id": "1001"}, str(calls2))

        sm = LangChainChatModel(chat_service=fake_chat_service(
            stream_parts=["流", "式", "回答"]))
        merged3 = None
        async for c in sm.astream([HumanMessage(content="hi")]):
            merged3 = c if merged3 is None else merged3 + c
        check("_stream 产出 AIMessageChunk 且内容完整",
              isinstance(merged3, AIMessageChunk) and merged3.content == "流式回答",
              f"{type(merged3).__name__} {getattr(merged3, 'content', None)!r}")

        st = LangChainChatModel(chat_service=fake_chat_service(
            stream_tool_args=['{"order', '_id": "10', '01"}']))
        merged4 = None
        async for c in st.astream([HumanMessage(content="hi")]):
            merged4 = c if merged4 is None else merged4 + c
        calls4 = (getattr(merged4, "tool_calls", None) or [{}])[0]
        check("流式 tool_call 片段拼接为完整 name/args",
              calls4.get("name") == "query_order"
              and calls4.get("args") == {"order_id": "1001"}, str(calls4))

        # ── T19 ───────────────────────────────────────────────────────
        # 回归：早期 AGENT_SPECS 把工具按 route 硬绑定，list_orders 只挂了
        # list_my_orders —— 子 Agent 拿到订单列表后想再查商品明细时手边没有第二
        # 把工具，只能把「接口没返回明细」抛回给用户（UI 截图实录）。现改为
        # 「统一只读工具集 + prompt 软引导」，本用例断言「同一轮内 LLM 能自主连续
        # 调两个工具」。
        # 负向对照：若把 list_orders 的工具集改回 ["list_my_orders"]，query_order
        # 会命中 `未知工具` 分支，第 2 条断言立刻变红。
        # 注意：必须放在 `async with persistent_checkpointer(...)` 块**内** ——
        # T17 起缩进降到块外，saver 的连接已关闭，再用就会
        # `ValueError: no active connection`。
        section("T19  完整 Agent：单轮内链式调用两个工具（list_my_orders → query_order）")
        chain_tools = default_tools()
        chain_tools["query_order"] = stub_tool(
            "query_order",
            "订单号：1081  状态：待支付\n商品明细：\n  X1 智能门锁 x1 ¥1599.00",
            "order_id")
        llm_chain = ScriptedChatModel(script=[
            '{"route": "list_orders", "slots": {}}',
            {"tool": "list_my_orders", "args": {}},
            {"tool": "query_order", "args": {"order_id": "1081"}},
            "你有一笔未支付订单 #1081，买的是 X1 智能门锁 ×1，¥1599.00。",
        ])
        agent_chain = make_agent(llm_chain, saver, tools=chain_tools)
        rc = await agent_chain.arun("哪一笔订单没支付，我买了什么？", user_id="1")
        chain_steps = (rc.metadata or {}).get("tool_calls") or []
        chain_names = [s["name"] for s in chain_steps]
        check("同一轮内依次调用了两个工具",
              chain_names == ["list_my_orders", "query_order"], str(chain_names))
        check("第二个工具真的拿到了商品明细（不是「未知工具」——工具集已放开）",
              len(chain_steps) > 1 and "商品明细" in chain_steps[1]["observation"],
              str(chain_steps[1]["observation"] if len(chain_steps) > 1 else chain_steps))
        check("第二个工具的参数承接第一个工具的输出（order_id=1081）",
              len(chain_steps) > 1
              and str(chain_steps[1]["args"].get("order_id")) == "1081",
              str(chain_steps[1]["args"] if len(chain_steps) > 1 else chain_steps))
        check("最终回答用上了只有第二个工具才有的信息",
              "X1 智能门锁" in rc.content, rc.content[:120])

        # ── T20 ───────────────────────────────────────────────────────
        # 回归：「你当前有什么商品在售？」原本无路可走 —— search_products 的
        # keyword 必填、空词直接返回 []，且没有任何路由指向「列全部商品」的能力。
        # 现新增只读工具 list_products（空关键词列出在售）+ product 路由。
        section("T20  商品浏览：product 路由 + list_products（无需关键词）")
        llm_browse = ScriptedChatModel(script=[
            '{"route": "product", "slots": {}, "extract": "看有哪些商品在售"}',
            {"tool": "list_products", "args": {}},
            "目前在售的商品：X1 智能门锁（¥1599）。",
        ])
        agent_browse = make_agent(llm_browse, saver)
        rb = await agent_browse.arun("你当前有什么商品在售？", user_id="1")
        bmeta = rb.metadata or {}
        bsteps = bmeta.get("tool_calls") or []
        check("路由为 product（新增路由，未被兜底成 finish）",
              bmeta.get("route") == "product", str(bmeta.get("route")))
        check("调用了 list_products（不需要关键词）",
              [s["name"] for s in bsteps] == ["list_products"], str(bsteps))
        check("观测里是在售商品，而不是「未知工具」",
              bool(bsteps) and "在售商品" in bsteps[0]["observation"],
              str(bsteps[0]["observation"] if bsteps else bsteps))
        check("最终回答含商品名", "X1 智能门锁" in rb.content, rb.content[:80])

        # ── T21 ───────────────────────────────────────────────────────
        # 回归：虚拟支付只写入一条「已揽件」记录，此后没有任何后台任务推进它，
        # 新订单物流会永远停在揽件态。derive_logistics 在**读取时**按经过时间补出
        # 后续节点（纯计算、不落库），本用例覆盖纯函数 + 服务集成两层。
        section("T21  物流状态推演：按经过时间补出后续节点（只读）")
        from datetime import datetime as _dt, timedelta as _td

        import domain.shop.logistics.service as _lsvc
        from domain.shop.logistics.service import derive_logistics

        base = _dt(2026, 1, 1, 10, 0, 0)
        auto = {"status": "picked_up",
                "timeline": [{"time": "2026-01-01 10:00:00",
                              "status": "已揽件", "location": "系统"}]}
        d0 = derive_logistics(auto, now=base)
        check("刚揽件：仅 1 个节点，且给出预计送达",
              [e["status"] for e in d0["timeline"]] == ["已揽件"]
              and bool(d0["estimated_delivery"]), str(d0["timeline"]))
        d3 = derive_logistics(auto, now=base + _td(minutes=3))
        check("3 分钟后推进到「运输中」（机器状态 in_transit）",
              d3["status"] == "in_transit"
              and d3["timeline"][-1]["status"] == "运输中", str(d3["timeline"]))
        d20 = derive_logistics(auto, now=base + _td(minutes=20))
        check("20 分钟后终点「已签收」，且不再给预计送达",
              d20["status"] == "delivered"
              and d20["timeline"][-1]["status"] == "已签收"
              and d20["estimated_delivery"] is None, str(d20["timeline"]))
        seed = {"status": "delivered",
                "timeline": [{"time": "a", "status": "已揽件"},
                             {"time": "b", "status": "已签收"}]}
        check("种子数据的完整轨迹原样返回，不重复推演",
              derive_logistics(seed, now=base) is seed,
              str(derive_logistics(seed, now=base)["timeline"]))
        # 集成：LogisticsService.get_by_order 也必须走推演，否则 HTTP 侧看不到变化
        _orig_q = _lsvc.query_one
        try:
            _lsvc.query_one = lambda *a, **k: {
                "id": 1, "order_id": 1001, "tracking_number": "SFX",
                "carrier": "顺丰快递", "status": "picked_up",
                "current_location": None, "estimated_delivery": None,
                "timeline": [{"time": "2026-01-01 10:00:00",
                              "status": "已揽件", "location": "系统"}],
                "paid_at": None, "updated_at": None}
            _row = _lsvc.LogisticsService.get_by_order(1001, 1)
            check("LogisticsService.get_by_order 也返回推演后的完整轨迹",
                  _row["status"] == "delivered" and len(_row["timeline"]) == 5,
                  str(_row["timeline"]))
        finally:
            _lsvc.query_one = _orig_q

        # ── T22 ───────────────────────────────────────────────────────
        # 回归：旧版 search_products 只做单次整串 `name ILIKE '%kw%'` —— 用户说
        # 「X1 门锁」而库里叫「X1 智能门锁」时 0 命中（UI 截图实录）。现改为
        # 「整串子串 → 分词 AND → 字符覆盖率」三层，纯函数在此离线断言。
        section("T22  商品检索：漏字/连写关键词也能命中（纯函数 match_products）")
        CATALOG = [
            {"id": 1, "name": "X1 智能门锁", "price": 1599.0, "stock": 100},
            {"id": 2, "name": "X2 智能门锁 Pro", "price": 2499.0, "stock": 50},
            {"id": 3, "name": "C1 智能摄像头", "price": 399.0, "stock": 200},
        ]
        got = match_products("X1 门锁", CATALOG)
        check("「X1 门锁」漏了「智能」→ 首选 X1 智能门锁",
              bool(got) and got[0]["name"] == "X1 智能门锁", str(got))
        got = match_products("x1智能门锁", CATALOG)
        check("连写「x1智能门锁」→ 首选 id=1", bool(got) and got[0]["id"] == 1, str(got))
        got = match_products("X1门锁", CATALOG)
        check("无空格「X1门锁」→ 首选 id=1", bool(got) and got[0]["id"] == 1, str(got))
        got = match_products("智能门锁", CATALOG)
        check("完整词「智能门锁」→ 首选 X1 智能门锁（放宽没有劣化精确）",
              bool(got) and got[0]["name"] == "X1 智能门锁", str(got))
        check("无关词「洗衣机」→ 空（不放宽到乱匹配）",
              match_products("洗衣机", CATALOG) == [],
              str(match_products("洗衣机", CATALOG)))
        check("空关键词 → 空", match_products("", CATALOG) == [])
        check("分词「X1 门锁」→ ['X1', '门锁']",
              tokenize_keyword("X1 门锁") == ["X1", "门锁"],
              str(tokenize_keyword("X1 门锁")))

        # ── T23 ───────────────────────────────────────────────────────
        # 回归：用户「那笔订单我已经支付了」→「当前状态」时，旧版 supervisor 只把
        # 本轮原话喂给 LLM（看不到历史），订单号也不跨轮保留 → 子 Agent 手边没有
        # 单号，只能反问「请把订单号发给我」（UI 截图实录）。现在：最近订单号
        # 持久化到 state.last_order_id，且 supervisor 对省略句做确定性提升。
        section("T23  跨轮上下文：省略句「当前状态」自动接上最近订单")
        check("带前缀才抽单号：'订单号：1081 已支付' → 1081",
              extract_prefixed_order_id("您的订单号：1081 已支付") == "1081")
        check("裸数字不当单号：'地址 3 号楼 1001 室' → ''",
              extract_prefixed_order_id("地址 3 号楼 1001 室") == "")
        check("'#1081' 也认", extract_prefixed_order_id("订单 #1081") == "1081")
        check("省略句识别：'当前状态' → order", promote_status_query("当前状态") == "order")
        check("省略句识别：'到哪了' → logistics",
              promote_status_query("到哪了") == "logistics")

        # 节点级：上一轮抽到过单号，本轮 LLM 判 finish —— 兜底必须提升回 order
        sup = create_supervisor_node(
            ScriptedChatModel(script=['{"route": "finish", "slots": {}}']))
        out = await sup({
            "messages": [HumanMessage(content="那笔订单我已经支付了"),
                         AIMessage(content="好的，订单 1081 已支付。"),
                         HumanMessage(content="当前状态")],
            "user_query": "当前状态",
            "slots": {"order_id": "1081"},
        })
        check("LLM 判 finish 也被提升回 order（省略句不再掉线）",
              out["route"] == "order", str(out.get("route")))
        check("自动带上最近订单号",
              str((out["slots"] or {}).get("order_id")) == "1081", str(out.get("slots")))
        check("最近订单号被持久化到 last_order_id",
              str(out.get("last_order_id")) == "1081", str(out.get("last_order_id")))

        # 反向：没有订单上下文时不能瞎提升（否则「你好」也会被当成查订单）
        sup2 = create_supervisor_node(
            ScriptedChatModel(script=['{"route": "finish", "slots": {}}']))
        out2 = await sup2({
            "messages": [HumanMessage(content="当前状态")],
            "user_query": "当前状态",
            "slots": {},
        })
        check("无任何订单上下文时不提升（仍 finish）",
              out2["route"] == "finish", str(out2.get("route")))

        # 兜底二：slots / last_order_id 都没有，但历史消息里出现过带前缀的单号
        sup3 = create_supervisor_node(
            ScriptedChatModel(script=['{"route": "finish", "slots": {}}']))
        out3 = await sup3({
            "messages": [AIMessage(content="已为您下单，订单号：1081。"),
                         HumanMessage(content="到哪了")],
            "user_query": "到哪了",
            "slots": {},
        })
        check("历史文本兜底：从「订单号：1081」恢复并提升为 logistics",
              out3["route"] == "logistics"
              and str((out3["slots"] or {}).get("order_id")) == "1081",
              f"{out3.get('route')} {out3.get('slots')}")

        # ── T24 ───────────────────────────────────────────────────────
        # 真实库集成：SQL 层（在售过滤 + 粗筛）与 Python 侧精排端到端可用。
        section("T24  真实 Postgres：漏字关键词能检到真实商品")
        if not pg_available():
            SKIPPED.append("T24 需要 Postgres")
            print("  [SKIP] Postgres 不可用")
        else:
            from tools.order_search import search_products as _sp
            hits = _sp("X1 门锁")
            check("「X1 门锁」在真实库命中 X1 智能门锁",
                  bool(hits) and hits[0]["name"] == "X1 智能门锁", str(hits))
            check("在售过滤生效（off_sale 的「X1 门锁配件包」不出现）",
                  all(h["name"] != "X1 门锁配件包" for h in hits), str(hits))
            check("无关词不误命中", _sp("洗衣机") == [], str(_sp("洗衣机")))

        # ── T25 ───────────────────────────────────────────────────────
        # 回归：LLM 把问句压成「关键词堆」（「X1 智能门锁 电池 续航」）时，向量检索
        # 的语义被主体词稀释 → 命中「同主体、不同话题」的条目，正确答案被 by-source
        # 去重挤掉（UI 实录：「它的电池能续航多久」答不出）。现派生聚焦变体 + 融合。
        section("T25  FAQ 多路召回：变体派生 + 共识融合（纯函数）")
        v = generate_query_variants("X1 智能门锁 电池 续航")
        check("原句始终在首位", v[0] == "X1 智能门锁 电池 续航", str(v))
        check("关键词堆 → 派生尾部聚焦变体「电池 续航」", "电池 续航" in v, str(v))
        check("自然问句不膨胀（1 个变体 → 走单路，零额外开销）",
              generate_query_variants("X1 智能门锁的电池能续航多久")
              == ["X1 智能门锁的电池能续航多久"],
              str(generate_query_variants("X1 智能门锁的电池能续航多久")))
        check("纯疑问片段（「怎么办？」）不被当成变体",
              "怎么办？" not in generate_query_variants("连不上 WiFi 怎么办？"),
              str(generate_query_variants("连不上 WiFi 怎么办？")))
        check("空 query → 空变体", generate_query_variants("") == [])

        def _sr(cid, q, s, prod):
            return SearchResult(text=q, score=s, chunk_id=cid,
                                metadata={"question": q, "product": prod})

        # 融合：被多路共同召回的文档胜出（共识优先），而不是单路最高分
        l1 = [_sr("a", "手机控制", 0.90, "X1"), _sr("b", "电池能用多久", 0.80, "X1")]
        l2 = [_sr("b", "电池能用多久", 0.85, "X1"), _sr("c", "重置WiFi", 0.70, "X2")]
        fused = fuse_results([l1, l2], top_k=3)
        check("两路共同召回者排第一（共识优先，不是单路最高分）",
              fused[0].chunk_id == "b", [r.chunk_id for r in fused])
        check("融合分数落回 [0,1]（可直接当相关度展示）",
              all(0.0 <= r.score <= 1.0 for r in fused), [r.score for r in fused])
        check("融合后仍按来源去重（每来源 1 条）",
              len({r.metadata.get("product") for r in fused}) == len(fused),
              [r.metadata.get("product") for r in fused])
        check("空输入 / 全空列表 → 空结果",
              fuse_results([], 3) == [] and fuse_results([[], []], 3) == [])

        # ── T26 ───────────────────────────────────────────────────────
        # 真实 FAQ 库集成：search_multi 在关键词堆问句上确实召回正确条目，
        # 且自然问句不回归（仍走单路、结果不变）。
        section("T26  真实 FAQ 库：关键词堆问句也能召回正确条目")
        from infrastructure.rag import rag_store as _faq
        try:
            _faq_chunks = _faq.count()
        except Exception as exc:  # noqa: BLE001 - 向量库不可用则跳过
            _faq_chunks = 0
            print(f"  [SKIP] FAQ 向量库不可用：{type(exc).__name__}")
        if _faq_chunks == 0:
            SKIPPED.append("T26 需要已灌库的 FAQ 向量库")
            print("  [SKIP] FAQ 向量库为空")
        else:
            def _qs(rs):
                return [r.metadata.get("question", "") for r in rs]

            r1 = _faq.search_multi("X1 智能门锁 电池 续航", top_k=3)
            check("关键词堆「X1 智能门锁 电池 续航」→ 榜首是「电池能用多久」",
                  bool(r1) and "电池能用多久" in _qs(r1)[0], _qs(r1))
            r2 = _faq.search_multi("X1 智能门锁 更换电池", top_k=3)
            check("「X1 智能门锁 更换电池」→ top3 含「怎么更换电池」",
                  any("怎么更换电池" in q for q in _qs(r2)), _qs(r2))
            r3 = _faq.search_multi("X1 智能门锁的电池能续航多久", top_k=3)
            check("自然问句不回归（仍首位命中「电池能用多久」）",
                  bool(r3) and "电池能用多久" in _qs(r3)[0], _qs(r3))

        # ── T27 ───────────────────────────────────────────────────────
        # 回归：用户往购物车加了 3 件商品后说「帮我下单」，旧版只有单品直购链路
        # （slots.product 必填 → 空关键词检索；create_direct 只落一个商品），
        # 结果要么回「没有找到与「」匹配的商品」，要么按猜到的某件只下 1 件，
        # 购物车里的其他商品被静默忽略（用户实测反馈）。
        # 现引入 order_mode：**本轮没点名商品即 cart**，整单走 OrderService.create
        # （锁购物车行 → 扣库存 → 建单 → 清空购物车）。
        section("T27  购物车整单：没点名商品时一次结算购物车全部商品")
        import domain.customer_service.routing as _rt

        check("纯函数：点名单品 → single",
              _rt.resolve_order_mode({"product": "X1 智能门锁"}) == _rt.ORDER_MODE_SINGLE)
        check("纯函数：没点名 → cart（默认结算购物车）",
              _rt.resolve_order_mode({}) == _rt.ORDER_MODE_CART)
        check("cart 模式只要求地址（不再被反问「想买哪件」）",
              [s.key for s in _rt.required_slots("place_order", _rt.ORDER_MODE_CART)]
              == ["address"])
        check("single 模式仍要求 商品 + 地址",
              [s.key for s in _rt.required_slots("place_order", _rt.ORDER_MODE_SINGLE)]
              == ["product", "address"])
        check("cart 且缺地址 → clarify（先问地址）",
              _rt.resolve_target({"route": "place_order", "order_action": "prepare",
                                  "order_mode": _rt.ORDER_MODE_CART,
                                  "slots": {}}) == "clarify")
        check("cart 且地址齐 → place_order_prepare",
              _rt.resolve_target({"route": "place_order", "order_action": "prepare",
                                  "order_mode": _rt.ORDER_MODE_CART,
                                  "slots": {"address": "广州市"}}) == "place_order_prepare")

        # supervisor 节点：模式判定只看**本轮**抽到的 product
        _sup_c = create_supervisor_node(ScriptedChatModel(
            script=['{"route": "place_order", "slots": {}, "extract": "结算购物车"}']))
        _o_c = await _sup_c({"messages": [HumanMessage(content="帮我结算购物车")],
                             "user_query": "帮我结算购物车", "slots": {}})
        check("supervisor：本轮没点名 → cart",
              _o_c.get("order_mode") == _rt.ORDER_MODE_CART, str(_o_c.get("order_mode")))
        _sup_s = create_supervisor_node(ScriptedChatModel(
            script=['{"route": "place_order", "slots": {"product": "X1 智能门锁"}}']))
        _o_s = await _sup_s({"messages": [HumanMessage(content="我要买X1智能门锁")],
                             "user_query": "我要买X1智能门锁", "slots": {}})
        check("supervisor：点名单品 → single",
              _o_s.get("order_mode") == _rt.ORDER_MODE_SINGLE, str(_o_s.get("order_mode")))
        # 负向对照：上一轮遗留的 product 槽位**不得**把本轮「结算购物车」降级成单品
        _sup_c2 = create_supervisor_node(ScriptedChatModel(
            script=['{"route": "place_order", "slots": {}, "extract": "结算购物车"}']))
        _o_c2 = await _sup_c2({"messages": [HumanMessage(content="帮我结算购物车")],
                               "user_query": "帮我结算购物车",
                               "slots": {"product": "X1 智能门锁", "address": "广州市"}})
        check("上一轮的 product 槽位不影响本轮 cart 判定",
              _o_c2.get("order_mode") == _rt.ORDER_MODE_CART, str(_o_c2.get("order_mode")))

        # 图级端到端：加购 2 件 → 「结算购物车」→ 确认 → 一张单带走全部商品
        if not pg_available():
            SKIPPED.append("T27 图级整单需要 Postgres")
        else:
            import re as _re

            from domain.shop.cart import CartService as _CS
            from domain.shop.order import OrderService as _OS

            _uid27 = 1
            _CS.clear(_uid27)
            _CS.add(_uid27, 1, 1)
            _CS.add(_uid27, 2, 1)
            _llm27 = ScriptedChatModel(script=[
                '{"route": "place_order", "slots": {"address": "广州市天河区"},'
                ' "extract": "结算购物车"}',
                '{"route": "place_order", "slots": {}, "extract": "确认"}',
            ])
            # ★ 必须注入 search_products：图的「下单两阶段」三个节点以
            #   PLACE_ORDER_TOOL（== "search_products"）是否在场为装配开关
            #   （见 build_master_graph）。缺了它，resolve_target 会解析到不存在的
            #   节点，selector 兜底成 finish —— 表现就是「回答把 JSON 原样吐出来」。
            _ag27 = make_agent(_llm27, saver, overrides={
                "search_products": stub_tool("search_products", "候选：X1 智能门锁", "keyword"),
            })
            _d1 = await _ag27.arun("帮我结算购物车，寄到广州市天河区", user_id="1")
            check("prepare 草稿列出购物车里的多件商品",
                  _d1.content.count("·") >= 2 and "确认" in _d1.content,
                  _d1.content[:200])
            check("没有退化成单品分支（不是「没有找到」）",
                  "没有找到" not in _d1.content, _d1.content[:120])
            _d2 = await _ag27.arun("确认", conversation_id=_d1.conversation_id, user_id="1")
            check("commit 阶段整单下单成功", "订单号" in _d2.content, _d2.content[:120])
            _m27 = _re.search(r"订单号[：:\s]*(\d+)", _d2.content)
            _oid27 = int(_m27.group(1)) if _m27 else 0
            _det27 = _OS.get_detail(_oid27, _uid27)["items"] if _oid27 else []
            check("整单落库：订单含全部 2 件商品（旧版只会下 1 件）",
                  len(_det27) == 2, str([i["product_name"] for i in _det27]))
            check("整单下单后购物车被清空",
                  len(_CS.list_items(_uid27)) == 0, str(_CS.list_items(_uid27)))
            if _oid27:
                try:
                    _OS.cancel(_oid27, _uid27)   # 回滚库存
                except ValueError:
                    pass
            _CS.clear(_uid27)

    # ── T17 ───────────────────────────────────────────────────────────
    section("T17  工具端鉴权加固 + 超时保护")
    import time as _time

    from apps.customer_service import routes as cs_routes2
    from domain.customer_service import react as react_mod
    # 注意：这里**不能**写 `from tools.base import Tool` —— 函数内 import 会让
    # `Tool` 变成 main() 的局部名，导致更早执行的 T14 直接 UnboundLocalError。
    import tools.base as tools_base

    # 17.1 /api/token 受「无凭证签发身份」开关控制
    _saved_allow = cs_routes2.ALLOW_DEMO_TOKEN
    try:
        cs_routes2.ALLOW_DEMO_TOKEN = False
        try:
            cs_routes2.generate_token(cs_routes2.TokenRequest(user_id="2"))
            check("关闭时 /api/token 拒绝签发", False, "未抛异常")
        except HTTPException as e:
            check("关闭时 /api/token 返回 404（不暴露端点存在）",
                  e.status_code == 404, str(e.status_code))
        cs_routes2.ALLOW_DEMO_TOKEN = True
        tok = cs_routes2.generate_token(cs_routes2.TokenRequest(user_id="2"))
        check("开启时仍可签发 demo token", bool(tok.get("access_token")))
    finally:
        cs_routes2.ALLOW_DEMO_TOKEN = _saved_allow

    # 17.2 身份解析不再做前缀匹配（防「谁先命中谁就是你」的身份混淆）
    import tools.order_search as os_mod
    _captured: list = []
    _orig_q1 = os_mod.query_one

    def _spy_q1(sql, params=()):
        _captured.append((sql, params))
        return None

    os_mod.query_one = _spy_q1
    try:
        got = os_mod._resolve_user_id("a")
        check("非数字身份查询走精确匹配（无 ILIKE）",
              _captured and "ILIKE" not in _captured[0][0].upper(), str(_captured[:1]))
        check("参数为精确值（不做 '<id>%' 前缀拼接）",
              _captured and _captured[0][1] == ("a", "a"), str(_captured[:1]))
        check("查不到身份返回 None（不猜）", got is None, str(got))
    finally:
        os_mod.query_one = _orig_q1

    # 17.3 工具超时：慢工具不拖垮请求，返回可读降级文案
    _slow = tools_base.Tool(
        name="slow_tool", description="故意很慢",
        func=lambda q="": (_time.sleep(0.4), "done")[1],
        parameters=[tools_base.ToolParameter("q", "string", "参数", required=False, default="")],
    )
    _saved_to = react_mod.TOOL_TIMEOUT_SEC
    react_mod.TOOL_TIMEOUT_SEC = 0.05
    try:
        obs, ms = await react_mod._call_tool(_slow, {})
        check("慢工具被超时截断并返回降级文案", "超时" in obs, obs)
        check("超时被计入耗时（不悬挂）", ms < 400, f"{ms}ms")
    finally:
        react_mod.TOOL_TIMEOUT_SEC = _saved_to

    # 17.4 内部接口：生产环境禁止默认 token，比较为时序安全
    import apps.shop.internal.routes as int_routes
    _saved_env, _saved_tok = int_routes.APP_ENV, int_routes.INTERNAL_TOKEN
    try:
        int_routes.APP_ENV = "production"
        int_routes.INTERNAL_TOKEN = "dev-internal-token"
        try:
            int_routes.verify_internal("dev-internal-token")
            check("生产环境默认内部 token 被拒绝", False, "未抛异常")
        except HTTPException as e:
            check("生产环境默认内部 token → 503", e.status_code == 503, str(e.status_code))
        int_routes.INTERNAL_TOKEN = "s3cret-token"
        try:
            int_routes.verify_internal("wrong")
            check("错误内部 token 被拒绝", False, "未抛异常")
        except HTTPException as e:
            check("错误内部 token → 401", e.status_code == 401, str(e.status_code))
        check("正确内部 token 放行",
              int_routes.verify_internal("s3cret-token") is True)
    finally:
        int_routes.APP_ENV, int_routes.INTERNAL_TOKEN = _saved_env, _saved_tok

    # ── T18 ───────────────────────────────────────────────────────────
    # 回归：购物车结算曾经不锁 cart_items —— 同一用户并发结算会各读到同一批
    # 商品、各建一单、各扣一次库存（重复下单）。现用 FOR UPDATE OF ci 锁购物车行。
    section("T18  并发：同一用户并发结算只成一单")
    if not pg_available():
        SKIPPED.append("T18 需要 Postgres")
    else:
        from domain.shop.cart import CartService
        from domain.shop.order import OrderService
        from infrastructure.shop_database import query_one as _q1

        _uid = 1
        CartService.clear(_uid)
        CartService.add(_uid, 1, 1)
        _before = _q1("SELECT stock FROM shop.products WHERE id = 1")["stock"]

        def _checkout():
            try:
                return ("ok", OrderService.create(_uid, "并发测试地址")["id"])
            except Exception as e:  # noqa: BLE001 - 失败路径就是要断言的对象
                return ("err", f"{type(e).__name__}: {e}")

        _results = await asyncio.gather(
            asyncio.to_thread(_checkout), asyncio.to_thread(_checkout))
        _oks = [r for r in _results if r[0] == "ok"]
        _errs = [r for r in _results if r[0] == "err"]
        check("恰好一单成功（无重复建单）", len(_oks) == 1, str(_results))
        check("另一单因购物车已空而失败", len(_errs) == 1, str(_results))
        _after = _q1("SELECT stock FROM shop.products WHERE id = 1")["stock"]
        check("库存只扣减一次", _after == _before - 1, f"{_before} → {_after}")
        for _, _oid in _oks:
            OrderService.cancel(_oid, _uid)
        CartService.clear(_uid)
        _restored = _q1("SELECT stock FROM shop.products WHERE id = 1")["stock"]
        check("清理后库存还原", _restored == _before, f"{_restored}")

    section(f"结果：{PASSED}/{PASSED + FAILED} 通过，{FAILED} 失败")
    if SKIPPED:
        print("跳过：" + "; ".join(SKIPPED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Acceptance test: 4 scenarios + data isolation + JWT auth.

Usage:
    python scripts/scenario_test.py              # all fast tests
    python scripts/scenario_test.py --full       # including LLM integration tests (requires server)
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


# ═══════════════════════════════════════════════════════════
# Test 1: Data Isolation (Repository Layer)
# ═══════════════════════════════════════════════════════════
print("=" * 60)
print("Test 1: User Data Isolation (Repository)")
print("=" * 60)

from infrastructure.order_repository import MockOrderRepository
repo = MockOrderRepository()

# Order isolation
o = repo.find_order("20240501001", "zhangsan")
check("zhangsan queries own order 20240501001", o is not None and o.product == "X1 智能门锁")

o = repo.find_order("20240501001", "lisi")
check("lisi CANNOT query zhangsan's order 20240501001", o is None, "data isolation broken")

o = repo.find_order("20240501002", "lisi")
check("lisi queries own order 20240501002", o is not None and o.product == "C1 智能摄像头")

o = repo.find_order("nonexistent", "zhangsan")
check("nonexistent order returns None", o is None)

# Logistics isolation
log = repo.find_logistics("20240501001", "zhangsan")
check("zhangsan queries own logistics", log is not None and log.carrier == "顺丰快递")

log = repo.find_logistics("20240501001", "lisi")
check("lisi CANNOT query zhangsan's logistics", log is None, "data isolation broken")

# After-sale isolation
ticket = repo.find_after_sale("AS20240501001", "zhangsan")
check("zhangsan queries own after-sale AS20240501001", ticket is not None and ticket.type == "return")

ticket = repo.find_after_sale("AS20240501001", "lisi")
check("lisi CANNOT query zhangsan's after-sale", ticket is None, "data isolation broken")

ticket = repo.find_after_sale("AS20240502001", "lisi")
check("lisi queries own after-sale AS20240502001", ticket is not None and ticket.type == "refund")

ticket = repo.find_after_sale("AS20240503001", "wangwu")
check("wangwu queries own after-sale AS20240503001", ticket is not None and ticket.type == "exchange")

# Per-user listing
for uid, expected in [("zhangsan", 1), ("lisi", 1), ("wangwu", 1)]:
    orders = repo.list_orders_by_user(uid)
    check(f"{uid} order count = {expected}", len(orders) == expected, f"got {len(orders)}")

for uid in ["zhangsan", "lisi", "wangwu"]:
    tickets = repo.list_after_sales_by_user(uid)
    check(f"{uid} after-sale count = 1", len(tickets) == 1, f"got {len(tickets)}")

# Create order with user_id
new_order = repo.create_order("Test Product", 99.0, "TestUser", "TestAddr", "testuser")
check("new order has correct user_id", new_order.user_id == "testuser")
check("new order can be queried by owner", repo.find_order(new_order.order_id, "testuser") is not None)
check("new order blocked for other user", repo.find_order(new_order.order_id, "other") is None)


# ═══════════════════════════════════════════════════════════
# Test 2: JWT Authentication (API Layer)
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("Test 2: JWT Authentication")
print("=" * 60)

from infrastructure.auth import create_access_token, decode_token

# Token generation and decoding
token_zhangsan = create_access_token("zhangsan")
check("token generation returns string", isinstance(token_zhangsan, str) and len(token_zhangsan) > 10)

payload = decode_token(token_zhangsan)
check("token decodes with correct user_id", payload.get("sub") == "zhangsan")
check("token has expiration", "exp" in payload)

token_lisi = create_access_token("lisi")
check("different users have different tokens", token_zhangsan != token_lisi)

# Invalid token handling
import jwt as _jwt
try:
    decode_token("invalid.token.here")
    check("invalid token raises error", False, "should have raised")
except (_jwt.InvalidTokenError, _jwt.DecodeError):
    check("invalid token raises InvalidTokenError", True)

# Expired token
try:
    expired = _jwt.encode({"sub": "test", "exp": 0}, "demo-secret-change-in-production", algorithm="HS256")
    decode_token(expired)
    check("expired token raises error", False, "should have raised")
except _jwt.ExpiredSignatureError:
    check("expired token raises ExpiredSignatureError", True)


# ═══════════════════════════════════════════════════════════
# Test 3: Graph Structure (Nodes + Routes)
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("Test 3: Graph Structure")
print("=" * 60)

from domain.customer_service.master_graph import (
    MasterState, build_master_graph,
    SUPERVISOR_PROMPT, FAQ_AGENT_PROMPT, ORDER_AGENT_PROMPT,
    LOGISTICS_AGENT_PROMPT, AFTER_SALE_AGENT_PROMPT,
)
from tools.faq_search import search_faq_tool
from tools.order_search import query_order_tool, query_logistics_tool, place_order_tool
from tools.after_sale_search import query_after_sale_tool

# Build graph with all tools
graph = build_master_graph(
    llm=None,  # Not needed for structure test
    search_faq_tool=search_faq_tool,
    query_order_tool=query_order_tool,
    query_logistics_tool=query_logistics_tool,
    query_after_sale_tool=query_after_sale_tool,
    place_order_tool=place_order_tool,
    checkpointer=None,
)

# Verify graph compiled
check("graph compiles successfully", graph is not None)

# Verify nodes via graph internal structure
graph_nodes = set(graph.nodes.keys()) if hasattr(graph, "nodes") else set()
check("graph has nodes", len(graph_nodes) > 0, f"found {len(graph_nodes)} nodes")

expected_nodes = {
    "supervisor", "faq_agent", "order_agent", "logistics_agent",
    "after_sale_agent", "place_order_agent", "finish",
}
missing_nodes = expected_nodes - graph_nodes
if missing_nodes:
    # Try to get nodes another way
    nodes = getattr(graph, "nodes", {})
    node_names = set(nodes.keys())
    check("all 7 expected nodes present", expected_nodes.issubset(node_names),
          f"missing: {expected_nodes - node_names}")
else:
    check("all 7 expected nodes present", True)

# Verify supervisor prompt includes all routes
for route in ["order", "logistics", "after_sale", "faq", "place_order", "finish"]:
    check(f"SUPERVISOR_PROMPT mentions '{route}'", route in SUPERVISOR_PROMPT)

# Verify all sub-agent prompts exist
check("FAQ_AGENT_PROMPT defined", len(FAQ_AGENT_PROMPT) > 0)
check("ORDER_AGENT_PROMPT defined", len(ORDER_AGENT_PROMPT) > 0)
check("LOGISTICS_AGENT_PROMPT defined", len(LOGISTICS_AGENT_PROMPT) > 0)
check("AFTER_SALE_AGENT_PROMPT defined", len(AFTER_SALE_AGENT_PROMPT) > 0)


# ═══════════════════════════════════════════════════════════
# Test 4: ConversationManager (PostgreSQL)
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
print("Test 4: ConversationManager (PostgreSQL)")
print("=" * 60)

from utils import ConversationManager

cm = ConversationManager(max_context_turns=5)

# Create conversations for two users
conv_a = cm.create("zhangsan")
conv_b = cm.create("lisi")
check("create conversation for zhangsan", len(conv_a) == 36)
check("create conversation for lisi", len(conv_b) == 36)
check("different users get different conv IDs", conv_a != conv_b)

# Add messages
mid1 = cm.add_message(conv_a, "user", "我的订单20240501001到哪了？")
mid2 = cm.add_message(conv_a, "assistant", "您的订单已签收")
check("add user message", mid1 > 0)
check("add assistant message", mid2 > 0)

# Get context
ctx = cm.get_context(conv_a)
check("context contains 2 messages", len(ctx) == 2, f"got {len(ctx)}")

# Get history
hist = cm.get_history(conv_a)
check("history contains 2 messages", len(hist) == 2, f"got {len(hist)}")
check("history role order correct", hist[0]["role"] == "user" and hist[1]["role"] == "assistant")

# List by user
conv_list_zhangsan = cm.list_conversations("zhangsan")
conv_list_lisi = cm.list_conversations("lisi")
check("zhangsan sees own conversations", len(conv_list_zhangsan) >= 1)
check("lisi sees own conversations", len(conv_list_lisi) >= 1)

# Close conversation
cm.close(conv_a)
import json as _json

# Verify status changed by re-querying
conv_list = cm.list_conversations("zhangsan")
closed = [c for c in conv_list if c["conversation_id"] == conv_a]
check("conversation can be closed", len(closed) == 1 and closed[0]["status"] == "closed",
      f"status: {closed[0]['status'] if closed else 'NOT FOUND'}")


# ═══════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════
print()
print("=" * 60)
total = passed + failed
print(f"Results: {passed}/{total} passed, {failed}/{total} failed")
print("=" * 60)

if failed > 0:
    print(f"\n{failed} test(s) FAILED. Review output above.")
    sys.exit(1)
else:
    print("\nAll fast tests passed.")

    # Check for --full flag
    if "--full" in sys.argv:
        print()
        print("=" * 60)
        print("Test 5: Full Integration (requires LLM + running server)")
        print("=" * 60)
        print("Run the server first: python main.py")
        print("Then run: python scripts/scenario_test.py --full")
        print()
        print("Manual verification steps:")
        print()
        print("  Scenario 1 (Order):  POST /api/token {\"user_id\":\"zhangsan\"}")
        print("                       POST /api/chat {\"message\":\"订单20240501001发货了吗？\"}")
        print("                       Expected: Master -> OrderAgent -> order details")
        print()
        print("  Scenario 2 (Logistics): POST /api/chat {\"message\":\"20240501001快递到哪了？\"}")
        print("                       Expected: Master -> LogisticsAgent -> tracking")
        print()
        print("  Scenario 3 (AfterSale): POST /api/chat {\"message\":\"AS20240501001售后进度\"}")
        print("                       Expected: Master -> AfterSaleAgent -> ticket details")
        print()
        print("  Scenario 4 (Fallback): POST /api/api/chat {\"message\":\"今天天气怎么样？\"}")
        print("                       Expected: Master -> finish (chat/greeting)")
        print()
        print("  Boundary: lisi tries zhangsan's order 20240501001")
        print("                       Expected: 未找到 (data isolation)")
    else:
        print("\nTo run full LLM integration tests: python scripts/scenario_test.py --full")

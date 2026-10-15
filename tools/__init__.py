from .base import Tool, ToolParameter
from .faq_search import search_faq, search_faq_tool
from .order_search import (
    query_order, query_logistics, place_order, query_cart,
    list_my_orders, search_products, list_products_for_agent,
    list_all_products, match_products, tokenize_keyword,
    query_order_tool, query_logistics_tool, place_order_tool, query_cart_tool,
    list_my_orders_tool, search_products_tool, list_products_tool,
)
from .after_sale_search import query_after_sale, query_after_sale_tool

# 注意 1：**不再导出 list_all_orders**。旧版 tools/order_search.py 里有一个
# 无 user 过滤、无鉴权的「列出全站订单」函数并被 re-export —— 一旦被误注册成
# Agent 工具，就是「一句话导出全站订单」。真正的管理员列表在
# ``apps/shop/b_endpoint/admin.py``，带 ``Depends(require_admin)``。
# 注意 2：``list_all_products``（**在售商品**，公开信息）与上面的
# ``list_all_orders`` 是两回事，前者只读、只返回 status='on_sale' 的商品概览。
__all__ = [
    "Tool", "ToolParameter",
    "search_faq", "search_faq_tool",
    "query_order", "query_logistics", "place_order",
    "query_cart", "list_my_orders", "search_products", "list_products_for_agent",
    "list_all_products", "match_products", "tokenize_keyword",
    "query_order_tool", "query_logistics_tool", "place_order_tool",
    "query_cart_tool", "list_my_orders_tool", "search_products_tool",
    "list_products_tool",
    "query_after_sale", "query_after_sale_tool",
]

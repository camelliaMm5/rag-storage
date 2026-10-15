"""路由白名单、归一化、槽位规格 —— Supervisor 的「确定性内核」。

为什么要单独一个模块：
1. LangGraph 的条件边在 ``path_map`` 未命中时**不会**回落默认分支，而是直接
   ``KeyError``（实测 langgraph 1.2.2）。LLM 输出不受控，可能给出
   ``order_status`` / ``物流查询`` / 甚至不存在路由名。因此所有 route 必须先
   归一化到白名单再交给条件边。
2. 路由到哪个节点，不能只看 route —— 还要看「槽位是否齐」「用户是否在回答
   上一轮的反问」「是否有待确认的订单草稿」。把这些判断从 LLM 手里收回来，
   做成纯函数，既可以离线单测，也杜绝了 LLM 的随机性直接决定图走向。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── 路由白名单 ────────────────────────────────────────────────────────
DEFAULT_ROUTE = "finish"

# route -> 图节点名（一个 route 可能对应多个节点，由 resolve_target 决定）
ROUTE_TO_NODE: dict[str, str] = {
    "faq": "faq_agent",
    "order": "order_agent",
    "logistics": "logistics_agent",
    "after_sale": "after_sale_agent",
    "cart_query": "cart_query_agent",
    "list_orders": "list_orders_agent",
    "product": "product_agent",
    "place_order": "place_order_prepare",
    "finish": "finish",
}

_ROUTE_ALIASES: dict[str, str] = {
    # faq
    "faq": "faq", "faq_agent": "faq", "knowledge": "faq", "kb": "faq",
    "faq_search": "faq", "知识库": "faq", "知识": "faq", "问答": "faq",
    "policy": "faq", "warranty": "faq", "troubleshoot": "faq",
    "政策": "faq", "保修": "faq", "故障": "faq", "说明": "faq",
    # order
    "order": "order", "orders": "order", "order_query": "order",
    "order_detail": "order", "order_status": "order", "订单": "order",
    "订单查询": "order", "订单详情": "order", "订单状态": "order",
    # logistics
    "logistics": "logistics", "logistic": "logistics", "shipping": "logistics",
    "express": "logistics", "delivery": "logistics", "物流": "logistics",
    "快递": "logistics", "物流查询": "logistics", "到哪了": "logistics",
    # after sale
    "after_sale": "after_sale", "aftersale": "after_sale",
    "after_sales": "after_sale", "after_sale_query": "after_sale",
    "refund": "after_sale", "return": "after_sale", "exchange": "after_sale",
    "售后": "after_sale", "退款": "after_sale", "退货": "after_sale",
    "换货": "after_sale", "工单": "after_sale",
    # cart
    "cart": "cart_query", "cart_query": "cart_query", "cart_items": "cart_query",
    "购物车": "cart_query", "购物车查询": "cart_query",
    # order list
    "list_orders": "list_orders", "my_orders": "list_orders",
    "order_list": "list_orders", "订单列表": "list_orders", "我的订单": "list_orders",
    # product browse（浏览在售商品，与 search_products 的「找具体某款」区分）
    "product": "product", "products": "product", "product_list": "product",
    "product_browse": "product", "catalog": "product", "browse": "product",
    "在售商品": "product", "商品列表": "product", "有哪些商品": "product",
    "有什么商品": "product", "卖什么": "product", "商品浏览": "product",
    # place order
    "place_order": "place_order", "order_create": "place_order",
    "create_order": "place_order", "buy": "place_order", "purchase": "place_order",
    "下单": "place_order", "购买": "place_order", "订购": "place_order",
    # finish
    "finish": "finish", "chat": "finish", "greeting": "finish",
    "fallback": "finish", "other": "finish", "闲聊": "finish", "问候": "finish",
}


def normalize_route(raw: str | None) -> str:
    """把 LLM 给出的任意 route 字符串映射到白名单；未识别则兜底闲聊。"""
    if not raw:
        return DEFAULT_ROUTE
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    if key in _ROUTE_ALIASES:
        return _ROUTE_ALIASES[key]
    logger.warning("unknown route from LLM: %r -> falling back to %s", raw, DEFAULT_ROUTE)
    return DEFAULT_ROUTE


# ── 槽位规格 ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Slot:
    """一个必填槽位。``alt`` 用于「二选一」场景（如订单号或工单号）。"""

    key: str
    label: str
    question: str
    alt: tuple[str, ...] = ()

    def present(self, slots: dict) -> bool:
        for k in (self.key, *self.alt):
            v = slots.get(k)
            if v is not None and str(v).strip():
                return True
        return False


ORDER_ID = Slot(
    key="order_id",
    label="订单号",
    question="请把订单号发给我～（纯数字，例如 1001）",
)

AFTER_SALE_REF = Slot(
    key="order_id",
    label="订单号或售后工单号",
    question="请提供订单号或售后工单号（纯数字），我来帮您查售后进度～",
    alt=("ticket_id",),
)

PRODUCT = Slot(
    key="product",
    label="商品名称",
    question="您想买哪件商品呢？告诉我商品名称即可（例如「X1 智能门锁」）。",
)

ADDRESS = Slot(
    key="address",
    label="收货地址",
    question="请把收货地址发我，我核对完就为您下单。",
)

# route -> 必填槽位
REQUIRED_SLOTS: dict[str, tuple[Slot, ...]] = {
    "order": (ORDER_ID,),
    "logistics": (ORDER_ID,),
    "after_sale": (AFTER_SALE_REF,),
    "place_order": (PRODUCT, ADDRESS),
}

# ── 下单模式：单品直购 vs 购物车整单结算 ────────────────────────────────
# 两种模式的差别只在「商品从哪来」：
#   single —— 用户点了名（「我要买 X1 智能门锁」），商品由 product 槽位模糊匹配得到；
#   cart   —— 用户没点名，但明确要「结算/把购物车里的东西下单」，商品来自购物车。
# 旧版只有 single：用户加购 3 件后说「帮我下单」，prepare 只能按一个关键词下一个
# 商品，购物车里的另外两件被静默忽略（用户实测反馈：只能下单一件商品）。
ORDER_MODE_SINGLE = "single"
ORDER_MODE_CART = "cart"

def resolve_order_mode(llm_slots: dict | None) -> str:
    """判定下单模式（纯函数，可离线单测）。

    规则：**本轮点名了商品 → single；没点名 → cart（购物车整单）**。

    「没说买什么却要下单」在电商语境里只有一种解释：结算购物车。旧版没有 cart
    模式，这类表述会落到单品分支用空关键词去检索 —— 要么回一句「没有找到与「」
    匹配的在售商品」，要么按 LLM 猜到的某个商品只下一件（用户实测反馈：购物车里
    放了 3 件，AI 只下了 1 件）。

    只依据**本轮** LLM 抽到的 product：上一轮遗留的 product 槽位不算数，否则
    用户先问「X1 门锁多少钱」再问「帮我下单」，会被误判成买那一件。
    """
    if str((llm_slots or {}).get("product") or "").strip():
        return ORDER_MODE_SINGLE
    return ORDER_MODE_CART


def required_slots(route: str, mode: str = "") -> tuple[Slot, ...]:
    """按 route（+ 下单模式）给出必填槽位。

    ``order_mode="cart"`` 时商品来自购物车，因此**不要求 product** —— 否则
    「结算购物车」会在 clarify 里被反问「您想买哪件商品」，永远走不到 prepare。
    """
    if route == "place_order" and mode == ORDER_MODE_CART:
        return (ADDRESS,)
    return REQUIRED_SLOTS.get(route, ())


def missing_slots(route: str, slots: dict, mode: str = "") -> list[Slot]:
    """返回该 route 下尚未填写的槽位（保持声明顺序）。"""
    slots = slots or {}
    return [s for s in required_slots(route, mode) if not s.present(slots)]


def clarify_question(route: str, missing: list[Slot]) -> str:
    """把缺失槽位转成一句自然的反问（确定性模板，不依赖 LLM）。"""
    if not missing:
        return "请补充一下所需信息～"
    if len(missing) == 1:
        return missing[0].question
    labels = "、".join(s.label for s in missing)
    return f"还差两项信息：{labels}。\n" + "\n".join(f"· {s.question}" for s in missing)


def merge_slots(base: dict | None, new: dict | None) -> dict:
    """合并槽位，新值覆盖旧值，忽略空值。"""
    merged = dict(base or {})
    for k, v in (new or {}).items():
        if v is not None and str(v).strip():
            merged[k] = v
    return merged


# ── 确定性参数抽取（LLM 漏抽时的兜底） ─────────────────────────────────
_ORDER_ID_RE = re.compile(r"\d{4,}")
_GREETING_RE = re.compile(r"^\s*(你好|您好|hi|hello|嗨|哈喽|在吗|早上好|晚上好)\s*[!！。.~]*$",
                          re.IGNORECASE)


def extract_order_id(text: str) -> str:
    """从文本里抓第一个「4 位以上连续数字」当作订单号。

    中文语境下 ``\\b`` 不可靠（汉字与数字之间没有词边界），因此不使用 \\b。
    """
    m = _ORDER_ID_RE.search(text or "")
    return m.group(0) if m else ""


def is_greeting(text: str) -> bool:
    return bool(_GREETING_RE.match(text or ""))


# ── 上下文指代：恢复「最近订单号」 ──────────────────────────────────────
# 购物对话里用户常用省略句（「那笔订单」「当前状态」「到哪了」）指代上文，不会再
# 重复订单号。这里给出**确定性**的恢复手段；宁可多问一句也不瞎猜 —— 因此只认
# 带前缀（订单号 / 单号 / #）的编号，不认裸数字（地址、金额里的数字太多了）。
_PREFIXED_ORDER_ID_RE = re.compile(
    r"(?:订单号|订单编号|单号|工单号|order[_ ]?id)\s*[:：#]?\s*(\d{4,})"
    r"|#\s*(\d{4,})",
    re.IGNORECASE,
)

# 省略句的意图提示词。仅在「上文已知订单号」时才用于把 finish 提升回具体意图。
_STATUS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("logistics", ("物流", "快递", "包裹", "到哪", "发货", "签收", "派送",
                   "运输", "揽件")),
    ("order", ("状态", "订单", "进度", "付款", "支付", "金额")),
)


def extract_prefixed_order_id(text: str) -> str:
    """只从「订单号：1234 / 单号 1234 / #1234」这类带前缀的文本里抽订单号。

    与 ``extract_order_id``（裸抓第一个 4+ 位数字）的分工：那个用于**用户本轮
    主动报单号**的场景（此时整句就是单号），本函数用于**扫历史消息**——历史里
    混着地址、金额、时间，只能靠前缀判断哪些数字才是订单号。
    """
    m = _PREFIXED_ORDER_ID_RE.search(text or "")
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def recent_order_id(messages, fallback: str = "") -> str:
    """倒序扫历史消息，取最近一个带前缀的订单号；都没有则用 ``fallback``。"""
    for m in reversed(list(messages or [])):
        content = getattr(m, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        oid = extract_prefixed_order_id(content)
        if oid:
            return oid
    return fallback or ""


def promote_status_query(text: str) -> str:
    """省略句 → 意图提示（「当前状态」→ order，「到哪了」→ logistics）。

    只返回**建议**，不代表一定提升：调用方必须确认「上文确有订单号」再采用，
    否则「你好，最近怎么样」也会被误判成查订单。
    """
    t = text or ""
    for route, words in _STATUS_HINTS:
        if any(w in t for w in words):
            return route
    return ""


# ── 确认 / 否定识别（下单两阶段的闸门） ────────────────────────────────
_AFFIRMATIVE = (
    "确认", "确定", "好的", "好嘞", "好的吧", "可以", "没问题", "没问题了",
    "是", "对", "嗯", "行", "下单", "提交", "买", "要",
    "yes", "yep", "ok", "okay", "sure", "confirm",
)

_NEGATIVE = (
    "取消", "不要", "不用", "算了", "不下单", "别", "否", "不是", "重新",
    "no", "nope", "cancel",
)


def is_affirmative(text: str) -> bool:
    """是否是「确认」类回答。

    注意必须**先排除否定**：中文里「不要」包含「要」、「不是」包含「是」，
    只做包含匹配会把否定误判成确认。旧版在下单确认里正好踩了这个坑。
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(k in t for k in _NEGATIVE):
        return False
    return any(k in t for k in _AFFIRMATIVE)


def is_negative(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(k in t for k in _NEGATIVE)


# ── 目标节点解析（纯函数，可离线单测） ─────────────────────────────────
def resolve_target(state) -> str:
    """决定 supervisor 之后进入哪个节点。

    优先级：下单动作 > 槽位是否齐全 > route 映射。
    任何情况下都只会返回图中的真实节点名，因此条件边永不 KeyError。
    """
    slots = state.get("slots") or {}
    mode = state.get("order_mode") or ""

    action = state.get("order_action")
    if action == "commit":
        return "place_order_commit"
    if action == "cancel":
        return "place_order_cancel"
    if action == "prepare":
        return ("clarify" if missing_slots("place_order", slots, mode)
                else "place_order_prepare")

    route = state.get("route") or DEFAULT_ROUTE
    if route in REQUIRED_SLOTS and missing_slots(route, slots, mode):
        return "clarify"
    return ROUTE_TO_NODE.get(route, "finish")

"""Order + Logistics search tools — reads from shop schema PostgreSQL."""
import os
import re

from tools.base import Tool, ToolParameter
from infrastructure.shop_database import query_one, query_all, get_conn
from domain.shop.logistics.service import derive_logistics


def _resolve_user_id(user_id: str) -> int | None:
    """把 JWT 身份映射为 shop.users.id。

    安全约定（**勿改回模糊匹配**）：
    - 只接受精确身份：数字 id，或与 ``email`` / ``nickname`` **完全相等**的值。
    - 旧版用 ``email ILIKE '<user_id>%'`` 做前缀匹配，等于「谁先命中谁就是你」——
      ``sub="a"`` 会匹配到任意以 a 开头的邮箱，属身份混淆（privilege confusion）。
    - 鉴权主体永远来自服务端 JWT（见 ``react._inject_context``），本函数只做映射，
      不做可信度判断。
    """
    if not user_id or user_id == "default":
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        pass
    row = query_one(
        "SELECT id FROM shop.users WHERE email = %s OR nickname = %s",
        (user_id, user_id),
    )
    return row["id"] if row else None


def query_order(order_id: str, user_id: str = "") -> str:
    """Query order by ID from shop.orders."""
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再查询订单。示例：用邮箱注册/登录后获取 Token。"

    try:
        oid = int(order_id)
    except ValueError:
        return f"订单号格式错误：{order_id}。订单号为纯数字。"

    row = query_one(
        "SELECT o.id, o.total_amount, o.status, o.address, o.created_at, o.paid_at, "
        "o.cancelled_at "
        "FROM shop.orders o WHERE o.id = %s AND o.user_id = %s",
        (oid, uid),
    )
    if not row:
        return f"未找到订单 {order_id}。请确认订单号是否正确，或该订单是否属于您的账号。"

    items = query_all(
        "SELECT product_name, price, quantity FROM shop.order_items WHERE order_id = %s",
        (oid,),
    )
    lines = [
        f"订单号：{row['id']}",
        f"状态：{row['status']}",
        f"金额：¥{row['total_amount']:.2f}",
        f"地址：{row['address']}",
        f"创建时间：{str(row['created_at'])[:19]}",
    ]
    if row["paid_at"]:
        lines.append(f"支付时间：{str(row['paid_at'])[:19]}")
    if row["cancelled_at"]:
        lines.append(f"取消时间：{str(row['cancelled_at'])[:19]}")
    lines.append("")
    lines.append("商品明细：")
    for item in items:
        lines.append(f"  {item['product_name']} x{item['quantity']} ¥{item['price']:.2f}")
    return "\n".join(lines)


def query_logistics(order_id: str, user_id: str = "") -> str:
    """Query logistics by order ID from shop.logistics_records."""
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再查询物流。"

    try:
        oid = int(order_id)
    except ValueError:
        return f"订单号格式错误：{order_id}。"

    row = query_one(
        "SELECT lr.tracking_number, lr.carrier, lr.status, lr.current_location, "
        "lr.estimated_delivery, lr.timeline, o.paid_at "
        "FROM shop.logistics_records lr "
        "JOIN shop.orders o ON o.id = lr.order_id "
        "WHERE lr.order_id = %s AND o.user_id = %s",
        (oid, uid),
    )
    if not row:
        return f"未找到订单 {order_id} 的物流信息。请确认订单号或物流是否已生成。"

    # 支付时只生成一条「已揽件」记录，此后无人推进 —— 读取时按经过时间补后续节点
    row = derive_logistics(row)

    lines = [
        f"物流单号：{row['tracking_number']}",
        f"快递公司：{row['carrier']}",
        f"当前状态：{row['status']}",
    ]
    if row["current_location"]:
        lines.append(f"当前位置：{row['current_location']}")
    if row["estimated_delivery"]:
        lines.append(f"预计送达：{str(row['estimated_delivery'])[:19]}")

    timeline = row.get("timeline")
    if timeline:
        lines.append("")
        lines.append("物流轨迹：")
        for t in (timeline if isinstance(timeline, list) else []):
            loc = f"（{t['location']}）" if t.get("location") else ""
            lines.append(f"  {t['time']}  {t['status']} {loc}")
    return "\n".join(lines)


# ── 商品名模糊匹配 ─────────────────────────────────────────────────────
# 背景（UI 截图实录）：用户说「X1 门锁」，库里叫「X1 智能门锁」——旧版是单次
# ``name ILIKE '%X1 门锁%'`` 的**整串子串**匹配，直接 0 命中，于是下单被回
# 「没有找到与「X1 门锁」匹配的在售商品」。购物对话里用户极少一字不差地打出
# 商品全名，因此改成三层：整串子串 → 分词 AND → 字符覆盖率兜底，逐层放宽。
_TOKEN_SPLIT_RE = re.compile(r"[\s,，、;；/|·+]+")

# 字符覆盖率下限：关键词（去空白、忽略大小写）的字符被商品名覆盖的比例。
# 0.6 能容纳「X1 门锁」(4/4=1.0)、「智能门锁1个」(5/6≈0.83) 这类漏字/多字，
# 又不会把「摄像头」错配到「门锁」上。
COVERAGE_MIN = float(os.getenv("PRODUCT_MATCH_COVERAGE_MIN", "0.6"))

_PRODUCT_COLS = "id, name, price, stock"
_ON_SALE = "status = 'on_sale'"


def _norm(text: str) -> str:
    """归一化：去掉所有空白字符并转小写。"""
    return re.sub(r"\s+", "", text or "").lower()


def tokenize_keyword(keyword: str) -> list[str]:
    """把关键词切成检索 token（「X1 门锁」→ ["X1", "门锁"]）。"""
    return [t for t in _TOKEN_SPLIT_RE.split((keyword or "").strip()) if t]


def _coverage(kw_norm: str, name_norm: str) -> float:
    """关键词字符被商品名覆盖的比例（按去重字符集算，对漏字/多字鲁棒）。"""
    if not kw_norm:
        return 0.0
    return len(set(kw_norm) & set(name_norm)) / len(set(kw_norm))


def match_products(keyword: str, rows: list[dict], limit: int = 5) -> list[dict]:
    """在候选商品里按「整串命中 > 字符覆盖率」排序（纯函数，便于离线单测）。

    - 层 2：归一化后关键词是商品名的子串（含连写「x1智能门锁」）→ 命中，
      同层按 ``len(kw)/len(name)`` 降序（越接近全名越靠前）；
    - 层 1：字符覆盖率 ≥ ``COVERAGE_MIN``（关键词长度 ≥ 2 才启用，避免单字乱命中）。
    """
    kw_norm = _norm(keyword)
    if not kw_norm:
        return []
    scored: list[tuple[float, float, int, dict]] = []
    for r in rows or []:
        name_norm = _norm(str(r.get("name") or ""))
        if not name_norm:
            continue
        if kw_norm in name_norm:
            coverage = 1.0                      # 整串子串：字符必然全覆盖
        elif len(kw_norm) >= 2:
            coverage = _coverage(kw_norm, name_norm)
            if coverage < COVERAGE_MIN:
                continue
        else:
            continue
        # 次级键 = 关键词占商品名的长度比：同名次下「漏得越少」越靠前。
        # 例：「X1 门锁」对「X1 智能门锁」覆盖率 1.0、长度比 0.67；
        #     对「X1 门锁配件包」覆盖率也 1.0、长度比 0.5 → 前者胜出。
        ratio = len(kw_norm) / max(len(name_norm), 1)
        scored.append((coverage, ratio, int(r.get("id") or 0), r))
    # 覆盖率 → 长度比 降序；同分按 id 升序（稳定，便于测试）
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [r for _, _, _, r in scored[:limit]]


def search_products(keyword: str, limit: int = 5) -> list[dict]:
    """按名称检索在售商品，返回 [{id, name, price, stock}]（名称模糊匹配）。

    这是**确定性**的检索函数（不经过 LLM），用于：
    - 下单前核对商品与库存（金额绝不能由 LLM 计算）
    - 导购场景下给用户列候选

    策略（见 ``match_products``）：粗筛用「任一 token 命中」把候选集缩小，
    再在 Python 侧按覆盖率精排；粗筛为空时退回全量在售商品（商品表很小，
    且「连写漏字」的关键词往往一个 token 都命中不了）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5

    tokens = tokenize_keyword(keyword) or [keyword]
    like_any = " OR ".join(["name ILIKE %s"] * len(tokens))
    rows = query_all(
        f"SELECT {_PRODUCT_COLS} FROM shop.products "
        f"WHERE {_ON_SALE} AND ({like_any}) ORDER BY id LIMIT 200",
        tuple(f"%{t}%" for t in tokens),
    )
    if not rows:
        rows = query_all(
            f"SELECT {_PRODUCT_COLS} FROM shop.products "
            f"WHERE {_ON_SALE} ORDER BY id LIMIT 200"
        )
    return match_products(keyword, rows, limit)


def format_products(rows: list[dict]) -> str:
    """把商品列表格式化成给 LLM / 用户看的文本。"""
    if not rows:
        return "未找到在售商品。"
    lines = ["在售商品候选：", ""]
    for p in rows:
        lines.append(f"· [{p['id']}] {p['name']}  单价 ¥{float(p['price']):.2f}  "
                     f"库存 {p['stock']}")
    return "\n".join(lines)


def list_products_for_agent(keyword: str) -> str:
    """给 Agent 用的商品检索工具实现。"""
    try:
        return format_products(search_products(keyword))
    except Exception as e:
        return f"商品检索失败：{type(e).__name__}"


def list_all_products(limit: int = 10) -> str:
    """列出当前在售的全部商品（无需关键词）。

    与 ``search_products`` 的分工：那个要求关键词、空词直接返回 ``[]``（服务的是
    下单前核对商品/库存，空词误列全库会有副作用）；本函数专门服务「有什么商品
    在售 / 你们卖什么」这类**浏览**诉求，按 id 升序返回在售商品概览。
    """
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10
    return format_products(query_all(
        "SELECT id, name, price, stock FROM shop.products "
        "WHERE status = 'on_sale' ORDER BY id LIMIT %s",
        (limit,),
    ))


def place_order(product: str, recipient: str, address: str,
                quantity: int = 1, amount: float = 0.0, user_id: str = "") -> str:
    """直接下单（单事务）。

    修复记录：旧版走「CartService.add → OrderService.create」两步，既非原子
    （可重复下单 / 购物车残留），又把 LLM 抽取的 ``recipient`` **整个丢掉**
    （只写了 address）。现在改为 ``OrderService.create_direct`` 单事务，
    并把 recipient 一起落库。
    """
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再下单。"

    try:
        quantity = max(1, int(quantity or 1))
    except (TypeError, ValueError):
        quantity = 1

    try:
        candidates = search_products(product)
        if not candidates:
            return f"未找到与 '{product}' 匹配的在售商品。请确认商品名称。"
        matched = next((p for p in candidates if p["name"] == product), candidates[0])

        from domain.shop.order import OrderService

        order = OrderService.create_direct(
            user_id=uid, product_id=int(matched["id"]), quantity=quantity,
            address=address or "用户未提供地址", recipient=recipient or "",
        )
        return (
            f"下单成功！\n"
            f"订单号：{order['id']}\n"
            f"商品：{matched['name']} × {quantity}\n"
            f"金额：¥{float(order['total_amount']):.2f}\n"
            f"收件人：{order.get('recipient') or '未填写'}\n"
            f"状态：{order['status']}\n\n"
            f"请保存订单号，后续可用于查询订单状态和物流。"
        )
    except ValueError as e:
        return f"下单失败：{e}"
    except Exception as e:
        return f"下单失败（{type(e).__name__}），请稍后重试或联系人工客服。"


def query_cart(user_id: str = "") -> str:
    """Query current user's shopping cart contents."""
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再查看购物车。"

    try:
        from domain.shop.cart import CartService
        items = CartService.list_items(uid)
    except Exception as e:
        return f"查询购物车失败: {e}"

    if not items:
        return "您的购物车是空的。可以去商品浏览页面挑选商品加入购物车。"

    lines = ["您的购物车：", ""]
    total = 0
    for i, it in enumerate(items, 1):
        subtotal = it["price"] * it["quantity"]
        total += subtotal
        lines.append(f"[{i}] {it['product_name']} x{it['quantity']}  ¥{subtotal:.2f} (单价 ¥{it['price']})")
    lines.append("")
    lines.append(f"合计：¥{total:.2f}，共 {len(items)} 种商品")
    return "\n".join(lines)


query_cart_tool = Tool(
    name="query_cart",
    description="查询当前用户的购物车内容。当用户询问'我的购物车有什么'、'购物车里有啥'、'帮我看看购物车'时调用。",
    func=query_cart,
    parameters=[
        ToolParameter("user_id", "string", "当前用户ID（由系统自动注入，无需填写）", required=False, default=""),
    ],
)


query_order_tool = Tool(
    name="query_order",
    description="根据订单号查询订单详情（状态、商品、金额、收件人、地址、支付时间）。",
    func=query_order,
    parameters=[
        ToolParameter("order_id", "string", "订单号（数字）", required=True),
        ToolParameter("user_id", "string", "当前用户ID（由系统自动注入，无需填写）", required=False, default=""),
    ],
)


def list_my_orders(user_id: str = "") -> str:
    """List all orders for the current user."""
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再查询订单。"

    rows = query_all(
        "SELECT o.id, o.total_amount, o.status, o.created_at "
        "FROM shop.orders o WHERE o.user_id = %s "
        "ORDER BY o.created_at DESC LIMIT 20",
        (uid,),
    )
    if not rows:
        return "您目前还没有订单。可以去商品浏览页面选购商品下单。"

    lines = ["您的订单列表：", ""]
    status_map = {"pending": "待支付", "paid": "已支付", "cancelled": "已取消"}
    for o in rows:
        s = status_map.get(o["status"], o["status"])
        t = str(o["created_at"])[:19] if o["created_at"] else ""
        lines.append(f"  #{o['id']}  [{s}]  ¥{o['total_amount']:.2f}  {t}")
    return "\n".join(lines)


list_my_orders_tool = Tool(
    name="list_my_orders",
    description="列出当前用户的所有订单。当用户询问'我有哪些订单'、'我的订单列表'、'我下了哪些单'、'查我的订单'时调用此工具。",
    func=list_my_orders,
    parameters=[
        ToolParameter("user_id", "string", "当前用户ID（由系统自动注入，无需填写）", required=False, default=""),
    ],
)


query_logistics_tool = Tool(
    name="query_logistics",
    description="根据订单号查询物流轨迹（快递公司、运单号、各节点状态）。",
    func=query_logistics,
    parameters=[
        ToolParameter("order_id", "string", "订单号（数字）", required=True),
        ToolParameter("user_id", "string", "当前用户ID（由系统自动注入，无需填写）", required=False, default=""),
    ],
)

place_order_tool = Tool(
    name="place_order",
    description="帮用户下单购买商品。从输入中提取商品名、收件人、地址、数量。",
    func=place_order,
    parameters=[
        ToolParameter("product", "string", "商品名称", required=True),
        ToolParameter("recipient", "string", "收件人姓名", required=True),
        ToolParameter("address", "string", "收货地址", required=True),
        ToolParameter("quantity", "integer", "购买数量", required=False, default=1),
        ToolParameter("amount", "number", "商品金额（元）", required=False, default=0.0),
        ToolParameter("user_id", "string", "当前用户ID（由系统自动注入，无需填写）", required=False, default=""),
    ],
)
# 注意：**刻意不注册进 Master 图**。
# Master 的下单走确定性的两阶段节点（place_order_prepare 只读回显 →
# place_order_commit 单事务落库），金额 / 库存 / 收件人都不经 LLM 计算。
# 保留这个工具是为了给「非图路径」（脚本、将来的其它前端）一个可直接调用的
# 单事务下单入口；不要把它塞回 main.py 的工具列表，否则会与两阶段路径语义重叠。


search_products_tool = Tool(
    name="search_products",
    description=(
        "按关键词检索在售商品，返回商品编号、名称、单价与库存。"
        "当用户想买某件商品、需要你确认具体是哪一款或列候选时调用。"
    ),
    func=list_products_for_agent,
    parameters=[
        ToolParameter("keyword", "string", "商品名称关键词", required=True),
    ],
)


list_products_tool = Tool(
    name="list_products",
    description=(
        "列出当前在售的全部商品（商品编号、名称、单价、库存）。"
        "当用户询问「有什么商品在售」「你们卖什么」「有哪些商品」等"
        "**浏览类**问题时调用；只是想找某一件具体商品时用 search_products。"
    ),
    func=list_all_products,
    parameters=[
        ToolParameter("limit", "integer", "返回数量上限", required=False, default=10),
    ],
)

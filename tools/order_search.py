"""Order + Logistics search tools — reads from shop schema PostgreSQL."""
from tools.base import Tool, ToolParameter
from infrastructure.shop_database import query_one, query_all, get_conn


def _resolve_user_id(user_id: str) -> int | None:
    """Resolve user_id (string or int) to shop.users.id."""
    if not user_id or user_id == "default":
        return None
    # If numeric, use directly
    try:
        return int(user_id)
    except ValueError:
        pass
    # Look up by nickname (for old demo tokens)
    row = query_one(
        "SELECT id FROM shop.users WHERE nickname = %s OR email ILIKE %s",
        (user_id, f"{user_id}%"),
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
        "lr.estimated_delivery, lr.timeline "
        "FROM shop.logistics_records lr "
        "JOIN shop.orders o ON o.id = lr.order_id "
        "WHERE lr.order_id = %s AND o.user_id = %s",
        (oid, uid),
    )
    if not row:
        return f"未找到订单 {order_id} 的物流信息。请确认订单号或物流是否已生成。"

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


def place_order(product: str, recipient: str, address: str,
                amount: float = 0.0, user_id: str = "") -> str:
    """Create a new order via the shop database."""
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再下单。示例：发送'帮我下单 X1智能门锁 收件人张三 地址广州天河'。"

    # Find matching product
    products = query_all(
        "SELECT id, name, price, stock FROM shop.products "
        "WHERE status = 'on_sale' AND name ILIKE %s ORDER BY id LIMIT 5",
        (f"%{product}%",),
    )
    if not products:
        return f"未找到与 '{product}' 匹配的商品。请确认商品名称。示例：X1 智能门锁。"

    # Pick best match (first exact match, or first result)
    matched = next((p for p in products if p["name"] == product), products[0])

    if matched["stock"] <= 0:
        return f"商品 '{matched['name']}' 当前库存不足。"

    # Add to cart and create order in one step
    try:
        from domain.shop.cart import CartService
        from domain.shop.order import OrderService

        CartService.add(uid, matched["id"], 1)
        order = OrderService.create(uid, address or "用户未提供地址")
        return (
            f"下单成功！\n"
            f"订单号：{order['id']}\n"
            f"商品：{matched['name']}\n"
            f"金额：¥{matched['price']:.2f}\n"
            f"状态：{order['status']}\n\n"
            f"请保存订单号，后续可用于查询订单状态和物流。"
        )
    except ValueError as e:
        return f"下单失败：{e}"


def list_all_orders() -> str:
    """List all orders (admin)."""
    rows = query_all(
        "SELECT o.id, o.total_amount, o.status, u.nickname "
        "FROM shop.orders o JOIN shop.users u ON u.id = o.user_id "
        "ORDER BY o.created_at DESC LIMIT 20"
    )
    if not rows:
        return "暂无订单。"
    lines = []
    for o in rows:
        lines.append(f"[{o['id']}] {o['nickname']} ¥{o['total_amount']:.2f} {o['status']}")
    return "\n".join(lines)


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
        ToolParameter("user_id", "string", "当前用户ID", required=False, default=""),
    ],
)


query_order_tool = Tool(
    name="query_order",
    description="根据订单号查询订单详情（状态、商品、金额、收件人、地址、支付时间）。",
    func=query_order,
    parameters=[
        ToolParameter("order_id", "string", "订单号（数字）", required=True),
        ToolParameter("user_id", "string", "当前用户ID", required=False, default=""),
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
        ToolParameter("user_id", "string", "当前用户ID", required=False, default=""),
    ],
)


query_logistics_tool = Tool(
    name="query_logistics",
    description="根据订单号查询物流轨迹（快递公司、运单号、各节点状态）。",
    func=query_logistics,
    parameters=[
        ToolParameter("order_id", "string", "订单号（数字）", required=True),
        ToolParameter("user_id", "string", "当前用户ID", required=False, default=""),
    ],
)

place_order_tool = Tool(
    name="place_order",
    description="帮用户下单购买商品。从输入中提取商品名、收件人、地址。",
    func=place_order,
    parameters=[
        ToolParameter("product", "string", "商品名称", required=True),
        ToolParameter("recipient", "string", "收件人姓名", required=True),
        ToolParameter("address", "string", "收货地址", required=True),
        ToolParameter("amount", "number", "商品金额（元）", required=False, default=0.0),
        ToolParameter("user_id", "string", "当前用户ID", required=False, default=""),
    ],
)

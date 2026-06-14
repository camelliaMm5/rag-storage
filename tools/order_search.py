"""Order + Logistics search tools for Agent dispatch."""

from infrastructure.order_repository import MockOrderRepository
from tools.base import Tool, ToolParameter

_repo = MockOrderRepository()


def query_order(order_id: str) -> str:
    """Query order by ID. Returns formatted order info or not-found message."""
    order = _repo.find_order(order_id)
    if order is None:
        return f"未找到订单 {order_id}。请确认订单号是否正确（示例：20240501001）。"

    return (
        f"订单号：{order.order_id}\n"
        f"状态：{order.status}\n"
        f"商品：{order.product}\n"
        f"金额：¥{order.amount:.2f}\n"
        f"下单时间：{order.order_time}\n"
        f"收件人：{order.recipient}\n"
        f"地址：{order.address}"
    )


def query_logistics(order_id: str) -> str:
    """Query logistics by order ID. Returns formatted tracking info or not-found."""
    logistics = _repo.find_logistics(order_id)
    if logistics is None:
        return f"未找到订单 {order_id} 的物流信息。请确认订单号是否正确。"

    lines = [
        f"物流单号：{logistics.tracking_no}",
        f"快递公司：{logistics.carrier}",
        f"当前状态：{logistics.status}",
        "",
        "物流轨迹：",
    ]
    for t in logistics.traces:
        loc = f"（{t.location}）" if t.location else ""
        lines.append(f"  {t.time}  {t.status} {loc}")

    return "\n".join(lines)


query_order_tool = Tool(
    name="query_order",
    description="根据订单号查询订单详情（状态、商品、金额、收件人、地址）。"
                "用户提供订单号时调用此工具。",
    func=query_order,
    parameters=[
        ToolParameter("order_id", "string", "订单号，如 20240501001", required=True),
    ],
)

def place_order(product: str, recipient: str, address: str, amount: float = 0.0) -> str:
    """Create a new order. Returns order_id and order info."""
    try:
        order = _repo.create_order(
            product=product, amount=amount, recipient=recipient, address=address,
        )
        return (
            f"下单成功！\n"
            f"订单号：{order.order_id}\n"
            f"商品：{order.product}\n"
            f"金额：¥{order.amount:.2f}\n"
            f"收件人：{order.recipient}\n"
            f"地址：{order.address}\n"
            f"下单时间：{order.order_time}\n"
            f"状态：{order.status}\n\n"
            f"请保存订单号，后续可用于查询订单状态和物流。"
        )
    except Exception as e:
        return f"下单失败：{e}"


place_order_tool = Tool(
    name="place_order",
    description="帮用户下单购买商品。需要商品名、收件人、地址，可选金额。"
                "当用户说'我要买...'、'帮我下单...'、'我想订购...'时调用此工具。",
    func=place_order,
    parameters=[
        ToolParameter("product", "string", "商品名称", required=True),
        ToolParameter("recipient", "string", "收件人姓名", required=True),
        ToolParameter("address", "string", "收货地址", required=True),
        ToolParameter("amount", "number", "商品金额（元）", required=False, default=0.0),
    ],
)


def list_all_orders() -> str:
    """List all orders for the API."""
    orders = _repo.list_orders()
    if not orders:
        return "暂无订单。"
    lines = []
    for o in orders:
        lines.append(f"[{o.order_id}] {o.product} ¥{o.amount:.2f} {o.status} ({o.recipient})")
    return "\n".join(lines)


query_logistics_tool = Tool(
    name="query_logistics",
    description="根据订单号查询物流轨迹（快递公司、运单号、各节点状态）。"
                "用户询问快递/物流进度时调用此工具。",
    func=query_logistics,
    parameters=[
        ToolParameter("order_id", "string", "订单号，如 20240501001", required=True),
    ],
)

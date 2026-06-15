"""After-sale search tool — reads from shop.after_sale_requests."""
from tools.base import Tool, ToolParameter
from tools.order_search import _resolve_user_id
from infrastructure.shop_database import query_one, query_all

AFTER_SALE_TYPE_LABELS = {
    "refund": "退款",
    "return": "退货",
    "exchange": "换货",
}


def query_after_sale(order_id: str = "", ticket_id: str = "", user_id: str = "") -> str:
    """Query after-sale tickets from shop schema."""
    uid = _resolve_user_id(user_id)
    if uid is None:
        return "请先登录后再查询售后。"

    if not order_id and not ticket_id:
        return "请提供订单号或售后工单号。"

    if ticket_id:
        try:
            tid = int(ticket_id)
        except ValueError:
            return f"工单号格式错误：{ticket_id}"
        row = query_one(
            "SELECT id, order_id, type, reason, status, created_at, updated_at "
            "FROM shop.after_sale_requests WHERE id = %s AND user_id = %s",
            (tid, uid),
        )
        tickets = [row] if row else []
    else:
        try:
            oid = int(order_id)
        except ValueError:
            return f"订单号格式错误：{order_id}"
        tickets = query_all(
            "SELECT id, order_id, type, reason, status, created_at, updated_at "
            "FROM shop.after_sale_requests WHERE order_id = %s AND user_id = %s",
            (oid, uid),
        )

    if not tickets:
        search_key = ticket_id or order_id
        return (
            f"未找到 {search_key} 的售后记录。\n"
            f"可能原因：工单号/订单号有误，或该售后不属于您的账号。"
        )

    lines = []
    for t in tickets:
        type_label = AFTER_SALE_TYPE_LABELS.get(t["type"], t["type"])
        lines.append(
            f"售后工单：{t['id']}\n"
            f"关联订单：{t['order_id']}\n"
            f"售后类型：{type_label}\n"
            f"处理状态：{t['status']}\n"
            f"申请原因：{t['reason']}\n"
            f"创建时间：{str(t['created_at'])[:19]}\n"
            f"更新时间：{str(t['updated_at'])[:19]}"
        )
        lines.append("")

    return "\n".join(lines).strip()


query_after_sale_tool = Tool(
    name="query_after_sale",
    description="查询售后工单（退款/退货/换货）。根据订单号或售后工单号查询售后进度。",
    func=query_after_sale,
    parameters=[
        ToolParameter("order_id", "string", "关联的订单号", required=False, default=""),
        ToolParameter("ticket_id", "string", "售后工单号", required=False, default=""),
        ToolParameter("user_id", "string", "当前用户ID", required=False, default=""),
    ],
)

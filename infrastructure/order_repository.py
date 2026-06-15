"""OrderRepository — Demo stage with user-isolated order/logistics/after-sale support."""
import uuid
from datetime import datetime

from domain.order.entity import Order, Logistics, LogisticsTrace, AfterSale
from domain.order.repository import OrderRepository


_orders: dict[str, Order] = {
    "20240501001": Order(
        order_id="20240501001", status="已签收", product="X1 智能门锁",
        amount=1599.00, order_time="2024-05-01 10:30:00",
        recipient="张三", address="广东省广州市天河区体育西路 123 号",
        user_id="zhangsan",
    ),
    "20240501002": Order(
        order_id="20240501002", status="运输中", product="C1 智能摄像头",
        amount=399.00, order_time="2024-05-03 14:20:00",
        recipient="李四", address="北京市朝阳区望京街道 456 号",
        user_id="lisi",
    ),
    "20240501003": Order(
        order_id="20240501003", status="待发货", product="G2 智能网关",
        amount=699.00, order_time="2024-05-05 09:15:00",
        recipient="王五", address="上海市浦东新区张江路 789 号",
        user_id="wangwu",
    ),
}

_logistics: dict[str, Logistics] = {
    "20240501001": Logistics(
        order_id="20240501001", carrier="顺丰快递", tracking_no="SF1234567890",
        status="已签收",
        traces=[
            LogisticsTrace("2024-05-01 12:00", "已揽件", "广州天河营业点"),
            LogisticsTrace("2024-05-02 08:30", "运输中", "广州分拣中心"),
            LogisticsTrace("2024-05-03 06:00", "到达派送点", "广州天河派送站"),
            LogisticsTrace("2024-05-03 10:15", "派送中", ""),
            LogisticsTrace("2024-05-03 14:30", "已签收", "本人签收"),
        ],
    ),
    "20240501002": Logistics(
        order_id="20240501002", carrier="京东物流", tracking_no="JD9876543210",
        status="运输中",
        traces=[
            LogisticsTrace("2024-05-03 16:00", "已揽件", "北京朝阳营业点"),
            LogisticsTrace("2024-05-04 02:00", "运输中", "北京分拣中心"),
        ],
    ),
}

_after_sales: dict[str, AfterSale] = {
    "AS20240501001": AfterSale(
        ticket_id="AS20240501001", order_id="20240501001",
        type="return", status="已通过",
        reason="门锁面板有划痕，不满意",
        created_at="2024-05-10 09:30:00", updated_at="2024-05-11 14:00:00",
        user_id="zhangsan",
    ),
    "AS20240502001": AfterSale(
        ticket_id="AS20240502001", order_id="20240501002",
        type="refund", status="待审核",
        reason="摄像头频繁断连，无法正常使用",
        created_at="2024-05-08 16:20:00", updated_at="2024-05-08 16:20:00",
        user_id="lisi",
    ),
    "AS20240503001": AfterSale(
        ticket_id="AS20240503001", order_id="20240501003",
        type="exchange", status="已完成",
        reason="网关型号发错，G2普通版换G2 Pro版",
        created_at="2024-05-12 10:00:00", updated_at="2024-05-14 18:30:00",
        user_id="wangwu",
    ),
}


def _gen_order_id() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d") + str(uuid.uuid4().int)[-4:]


def _gen_tracking_no() -> str:
    return "SF" + str(uuid.uuid4().int)[:10]


class MockOrderRepository(OrderRepository):
    def find_order(self, order_id: str, user_id: str) -> Order | None:
        order = _orders.get(order_id)
        if order is None:
            return None
        if order.user_id and order.user_id != user_id:
            return None
        return order

    def find_logistics(self, order_id: str, user_id: str) -> Logistics | None:
        order = _orders.get(order_id)
        if order is None:
            return None
        if order.user_id and order.user_id != user_id:
            return None
        return _logistics.get(order_id)

    def create_order(self, product: str, amount: float, recipient: str,
                     address: str, user_id: str) -> Order:
        order_id = _gen_order_id()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        order = Order(
            order_id=order_id, status="待发货", product=product,
            amount=amount, order_time=now,
            recipient=recipient, address=address,
            user_id=user_id,
        )
        _orders[order_id] = order

        tracking_no = _gen_tracking_no()
        _logistics[order_id] = Logistics(
            order_id=order_id, carrier="顺丰快递", tracking_no=tracking_no,
            status="待发货",
            traces=[
                LogisticsTrace(now, "订单已创建", "系统"),
            ],
        )
        return order

    def list_orders(self) -> list[Order]:
        return sorted(_orders.values(), key=lambda o: o.order_time, reverse=True)

    def list_orders_by_user(self, user_id: str) -> list[Order]:
        return [o for o in _orders.values() if o.user_id == user_id]

    def find_after_sale(self, ticket_id: str, user_id: str) -> AfterSale | None:
        ticket = _after_sales.get(ticket_id)
        if ticket is None:
            return None
        if ticket.user_id and ticket.user_id != user_id:
            return None
        return ticket

    def list_after_sales_by_user(self, user_id: str) -> list[AfterSale]:
        return [a for a in _after_sales.values() if a.user_id == user_id]

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LogisticsTrace:
    time: str
    status: str
    location: str | None = None


@dataclass(frozen=True)
class Order:
    order_id: str
    status: str
    product: str
    amount: float
    order_time: str
    recipient: str
    address: str
    user_id: str = ""


@dataclass(frozen=True)
class Logistics:
    order_id: str
    carrier: str
    tracking_no: str
    status: str
    traces: list[LogisticsTrace] = field(default_factory=list)


@dataclass(frozen=True)
class AfterSale:
    ticket_id: str
    order_id: str
    type: str        # refund / return / exchange
    status: str      # 待审核 / 已通过 / 已拒绝 / 已完成
    reason: str
    created_at: str
    updated_at: str
    user_id: str = ""

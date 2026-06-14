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


@dataclass(frozen=True)
class Logistics:
    order_id: str
    carrier: str
    tracking_no: str
    status: str
    traces: list[LogisticsTrace] = field(default_factory=list)

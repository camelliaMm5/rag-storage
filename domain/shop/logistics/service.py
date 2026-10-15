"""LogisticsService — query logistics by order_id.

物流状态推演
------------
虚拟支付时只写入一条「已揽件」记录（见 ``domain/shop/order/service.py`` 的
``OrderService.pay``），此后**没有任何后台任务推进它** —— 所以新下的订单物流会
永远停在揽件态。为了让 demo 有「物流会动」的观感，这里在**读取时**按距揽件的
经过时间补出后续节点：纯计算、不回写数据库，不影响任何既有数据。

只对「自动生成的初始态」推演（``status == 'picked_up'`` 且轨迹不超过 1 条）；
``init.sql`` 种子数据的丰富轨迹（1001 delivered / 1002 in_transit）原样返回。

阈值默认按「演示尺度」给到分钟级，可用 ``LOGISTICS_STAGE_MINUTES`` 覆盖
（逗号分隔的 5 个递增分钟数，与下方状态一一对应）。
"""
import os
from datetime import datetime, timedelta

from infrastructure.shop_database import query_one


# (距揽件分钟数, 轨迹文案, 机器状态值, 位置)
# 机器状态值刻意沿用前端已认识的英文枚举（static/index.html 的状态映射）：
# picked_up / in_transit / out_for_delivery / delivered。
_STAGES: tuple[tuple[int, str, str, str], ...] = (
    (0,  "已揽件",     "picked_up",        "发件地营业点"),
    (2,  "运输中",     "in_transit",       "发件地分拣中心"),
    (5,  "到达派送点", "in_transit",       "目的地分拨中心"),
    (8,  "派送中",     "out_for_delivery", ""),
    (12, "已签收",     "delivered",        "本人签收"),
)

_TIME_FORMATS = ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M"))


def stage_minutes() -> tuple[int, ...]:
    """各阶段的时间阈值（分钟）。默认内置，可用环境变量覆盖。"""
    raw = (os.getenv("LOGISTICS_STAGE_MINUTES") or "").strip()
    if raw:
        try:
            nums = [int(x) for x in raw.split(",")]
            if len(nums) == len(_STAGES) and all(
                    a <= b for a, b in zip(nums, nums[1:])):
                return tuple(nums)
        except ValueError:
            pass
    return tuple(m for m, _label, _status, _loc in _STAGES)


def _to_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip()
        for size, fmt in _TIME_FORMATS:
            if len(text) >= size:
                try:
                    return datetime.strptime(text[:size], fmt)
                except ValueError:
                    continue
    return None


def derive_logistics(state: dict, now: datetime | None = None) -> dict:
    """按经过时间补出后续物流节点（只读，不落库）。返回新 dict，不改入参。"""
    entries = state.get("timeline")
    entries = list(entries) if isinstance(entries, list) else []

    if state.get("status") != "picked_up" or len(entries) > 1:
        return state  # 非初始态：种子数据 / 已有完整轨迹，原样返回

    base = _to_datetime(entries[0].get("time")) if entries else None
    if base is None:
        base = _to_datetime(state.get("paid_at")) or (now or datetime.now())
    now = now or datetime.now()
    elapsed = max(0.0, (now - base).total_seconds() / 60.0)

    minutes = stage_minutes()
    reached = [i for i, m in enumerate(minutes) if elapsed >= m] or [0]

    timeline = []
    for idx in reached:
        m, label, _status, loc = _STAGES[idx]
        ts = (entries[0].get("time") if idx == 0 and entries
              else (base + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"))
        timeline.append({"time": ts, "status": label, "location": loc})

    last = reached[-1]
    result = dict(state)
    result["status"] = _STAGES[last][2]
    result["current_location"] = _STAGES[last][3] or None
    result["timeline"] = timeline
    result["estimated_delivery"] = (
        (base + timedelta(minutes=minutes[-1])).strftime("%Y-%m-%d %H:%M:%S")
        if last < len(_STAGES) - 1 else None
    )
    return result


class LogisticsService:

    @staticmethod
    def get_by_order(order_id: int, user_id: int) -> dict | None:
        """Get logistics for an order. Validates order ownership."""
        row = query_one(
            "SELECT lr.id, lr.order_id, lr.tracking_number, lr.carrier, "
            "lr.status, lr.current_location, lr.estimated_delivery, "
            "lr.timeline, lr.updated_at, o.paid_at "
            "FROM shop.logistics_records lr "
            "JOIN shop.orders o ON o.id = lr.order_id "
            "WHERE lr.order_id = %s AND o.user_id = %s",
            (order_id, user_id),
        )
        return derive_logistics(row) if row else None

    @staticmethod
    def get_by_order_internal(order_id: int) -> dict | None:
        """Get logistics (internal, no ownership check)."""
        row = query_one(
            "SELECT lr.id, lr.order_id, lr.tracking_number, lr.carrier, "
            "lr.status, lr.current_location, lr.estimated_delivery, "
            "lr.timeline, lr.updated_at, o.paid_at "
            "FROM shop.logistics_records lr "
            "JOIN shop.orders o ON o.id = lr.order_id "
            "WHERE lr.order_id = %s",
            (order_id,),
        )
        return derive_logistics(row) if row else None

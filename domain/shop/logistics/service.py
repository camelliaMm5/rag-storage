"""LogisticsService — query logistics by order_id."""
from infrastructure.shop_database import query_one


class LogisticsService:

    @staticmethod
    def get_by_order(order_id: int, user_id: int) -> dict | None:
        """Get logistics for an order. Validates order ownership."""
        row = query_one(
            "SELECT lr.id, lr.order_id, lr.tracking_number, lr.carrier, "
            "lr.status, lr.current_location, lr.estimated_delivery, "
            "lr.timeline, lr.updated_at "
            "FROM shop.logistics_records lr "
            "JOIN shop.orders o ON o.id = lr.order_id "
            "WHERE lr.order_id = %s AND o.user_id = %s",
            (order_id, user_id),
        )
        return row

    @staticmethod
    def get_by_order_internal(order_id: int) -> dict | None:
        """Get logistics (internal, no ownership check)."""
        return query_one(
            "SELECT id, order_id, tracking_number, carrier, status, "
            "current_location, estimated_delivery, timeline, updated_at "
            "FROM shop.logistics_records WHERE order_id = %s",
            (order_id,),
        )

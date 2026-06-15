"""AfterSaleService — create and query after-sale requests."""
from infrastructure.shop_database import query_one, query_all, execute


class AfterSaleService:

    @staticmethod
    def create(user_id: int, order_id: int, typ: str, reason: str) -> dict:
        """Submit an after-sale request (refund/return)."""
        if typ not in ("refund", "return"):
            raise ValueError("售后类型仅支持 refund / return")

        order = query_one(
            "SELECT id, user_id, status FROM shop.orders WHERE id = %s",
            (order_id,),
        )
        if not order:
            raise ValueError("订单不存在")
        if order["user_id"] != user_id:
            raise ValueError("只能对自己的订单申请售后")
        if order["status"] != "paid":
            raise ValueError("仅已支付订单可申请售后")

        execute(
            "INSERT INTO shop.after_sale_requests (user_id, order_id, type, reason) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, order_id, typ, reason),
        )
        # Get the created record
        row = query_one(
            "SELECT id, order_id, type, reason, status, created_at "
            "FROM shop.after_sale_requests "
            "WHERE user_id = %s AND order_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, order_id),
        )
        return row

    @staticmethod
    def list_by_user(user_id: int) -> list[dict]:
        return query_all(
            "SELECT id, order_id, type, reason, status, created_at, updated_at "
            "FROM shop.after_sale_requests "
            "WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )

    @staticmethod
    def list_by_user_internal(user_id: int) -> list[dict]:
        """Internal API: list after-sales for a user."""
        return query_all(
            "SELECT id, order_id, type, reason, status, created_at, updated_at "
            "FROM shop.after_sale_requests "
            "WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )

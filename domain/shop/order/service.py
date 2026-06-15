"""OrderService — create order (transaction), pay, cancel, query."""
from datetime import datetime

from infrastructure.shop_database import get_conn, query_one, query_all


class OrderService:

    @staticmethod
    def create(user_id: int, address: str) -> dict:
        """Create order from cart. One transaction: lock stock → deduct →
        create order + items → clear cart."""
        if not address:
            raise ValueError("请填写收货地址")

        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Get cart items
                cur.execute(
                    "SELECT ci.product_id, ci.quantity, p.name, p.price, p.stock "
                    "FROM shop.cart_items ci JOIN shop.products p ON p.id = ci.product_id "
                    "WHERE ci.user_id = %s "
                    "ORDER BY ci.product_id",
                    (user_id,),
                )
                items = cur.fetchall()
                if not items:
                    raise ValueError("购物车为空")

                # 2. Lock product rows and check stock
                product_ids = [row[0] for row in items]
                cur.execute(
                    "SELECT id, stock, name FROM shop.products "
                    "WHERE id IN %s AND status = 'on_sale' FOR UPDATE",
                    (tuple(product_ids),),
                )
                locked = {row[0]: {"stock": row[1], "name": row[2]} for row in cur.fetchall()}

                for pid, qty, name, price, stock in items:
                    current = locked.get(pid)
                    if not current:
                        raise ValueError(f"商品 '{name}' 已下架")
                    if current["stock"] < qty:
                        raise ValueError(f"商品 '{current['name']}' 库存不足 (库存{current['stock']}, 需要{qty})")

                # 3. Deduct stock
                for pid, qty, name, price, _stock in items:
                    cur.execute(
                        "UPDATE shop.products SET stock = stock - %s WHERE id = %s AND stock >= %s",
                        (qty, pid, qty),
                    )
                    if cur.rowcount == 0:
                        raise ValueError(f"商品 '{name}' 扣减库存失败")

                # 4. Create order
                total = sum(price * qty for _pid, qty, _n, price, _s in items)
                cur.execute(
                    "INSERT INTO shop.orders (user_id, total_amount, status, address) "
                    "VALUES (%s, %s, 'pending', %s) RETURNING id",
                    (user_id, total, address),
                )
                order_id = cur.fetchone()[0]

                # 5. Create order items (snapshot)
                for pid, qty, name, price, _stock in items:
                    cur.execute(
                        "INSERT INTO shop.order_items (order_id, product_id, product_name, price, quantity) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (order_id, pid, name, price, qty),
                    )

                # 6. Clear cart
                cur.execute(
                    "DELETE FROM shop.cart_items WHERE user_id = %s", (user_id,),
                )

            conn.commit()

        return OrderService.get_detail(order_id, user_id)

    @staticmethod
    def pay(order_id: int, user_id: int) -> dict:
        """Mock payment. Idempotent via FOR UPDATE + status check."""
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, status, total_amount FROM shop.orders "
                    "WHERE id = %s FOR UPDATE",
                    (order_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("订单不存在")
                oid, o_uid, status, amount = row
                if o_uid != user_id:
                    raise ValueError("只能支付自己的订单")
                if status == "paid":
                    raise ValueError("请勿重复支付")
                if status == "cancelled":
                    raise ValueError("订单已取消")

                cur.execute(
                    "UPDATE shop.orders SET status = 'paid', paid_at = NOW() WHERE id = %s",
                    (order_id,),
                )
                # Payment record
                cur.execute(
                    "INSERT INTO shop.payment_records (order_id, amount) VALUES (%s, %s)",
                    (order_id, amount),
                )
                # Auto-generate logistics record
                import uuid
                import json as _json
                tracking = "SF" + str(uuid.uuid4().int)[:10]
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                timeline = _json.dumps([
                    {"time": now_str, "status": "已揽件", "location": "系统"}
                ], ensure_ascii=False)
                cur.execute(
                    "INSERT INTO shop.logistics_records "
                    "(order_id, tracking_number, carrier, status, timeline) "
                    "VALUES (%s, %s, %s, 'picked_up', %s::jsonb)",
                    (order_id, tracking, "顺丰快递", timeline),
                )
            conn.commit()

        return OrderService.get_detail(order_id, user_id)

    @staticmethod
    def cancel(order_id: int, user_id: int) -> dict:
        """Cancel pending order, rollback stock."""
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, status FROM shop.orders WHERE id = %s FOR UPDATE",
                    (order_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("订单不存在")
                oid, o_uid, status = row
                if o_uid != user_id:
                    raise ValueError("只能取消自己的订单")
                if status != "pending":
                    raise ValueError(f"订单状态为 {status}，不可取消")

                cur.execute(
                    "UPDATE shop.orders SET status = 'cancelled', cancelled_at = NOW() WHERE id = %s",
                    (order_id,),
                )
                # Rollback stock
                cur.execute(
                    "SELECT product_id, quantity FROM shop.order_items WHERE order_id = %s",
                    (order_id,),
                )
                for pid, qty in cur.fetchall():
                    cur.execute(
                        "UPDATE shop.products SET stock = stock + %s WHERE id = %s",
                        (qty, pid),
                    )
            conn.commit()

        return OrderService.get_detail(order_id, user_id)

    @staticmethod
    def list_orders(user_id: int, status: str = "", page: int = 1, size: int = 20) -> dict:
        conditions = ["o.user_id = %s"]
        params: list = [user_id]
        if status:
            conditions.append("o.status = %s")
            params.append(status)

        where = " AND ".join(conditions)
        total = query_one(
            f"SELECT count(*) as cnt FROM shop.orders o WHERE {where}", tuple(params)
        )["cnt"]
        offset = (page - 1) * size
        rows = query_all(
            f"SELECT o.id, o.total_amount, o.status, o.address, o.created_at, o.paid_at "
            f"FROM shop.orders o WHERE {where} "
            f"ORDER BY o.created_at DESC LIMIT %s OFFSET %s",
            tuple(params) + (size, offset),
        )
        return {"items": rows, "total": total, "page": page, "size": size}

    @staticmethod
    def get_detail(order_id: int, user_id: int) -> dict:
        order = query_one(
            "SELECT id, user_id, total_amount, status, address, created_at, paid_at, cancelled_at "
            "FROM shop.orders WHERE id = %s",
            (order_id,),
        )
        if not order:
            raise ValueError("订单不存在")
        if order["user_id"] != user_id:
            raise ValueError("只能查看自己的订单")
        items = query_all(
            "SELECT product_id, product_name, price, quantity "
            "FROM shop.order_items WHERE order_id = %s",
            (order_id,),
        )
        order["items"] = items
        return order

    @staticmethod
    def list_all(status: str = "", page: int = 1, size: int = 20) -> dict:
        """Admin: list all orders."""
        conditions = []
        params = []
        if status:
            conditions.append("status = %s")
            params.append(status)
        where = " AND ".join(conditions) if conditions else "1=1"
        total = query_one(
            f"SELECT count(*) as cnt FROM shop.orders WHERE {where}", tuple(params)
        )["cnt"]
        offset = (page - 1) * size
        rows = query_all(
            f"SELECT o.id, o.user_id, o.total_amount, o.status, o.address, "
            f"o.created_at, o.paid_at, u.nickname "
            f"FROM shop.orders o JOIN shop.users u ON u.id = o.user_id "
            f"WHERE {where} ORDER BY o.created_at DESC "
            f"LIMIT %s OFFSET %s",
            tuple(params) + (size, offset),
        )
        return {"items": rows, "total": total, "page": page, "size": size}

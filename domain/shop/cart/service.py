"""CartService — add/update/remove/view cart items with UPSERT."""
from infrastructure.shop_database import query_one, query_all, execute


class CartService:

    @staticmethod
    def add(user_id: int, product_id: int, quantity: int = 1) -> dict:
        """Add product to cart. UPSERT: duplicate adds stack quantity."""
        product = query_one(
            "SELECT id, status, stock FROM shop.products WHERE id = %s", (product_id,)
        )
        if not product:
            raise ValueError("商品不存在")
        if product["status"] != "on_sale":
            raise ValueError("商品已下架")

        execute(
            "INSERT INTO shop.cart_items (user_id, product_id, quantity) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, product_id) "
            "DO UPDATE SET quantity = shop.cart_items.quantity + EXCLUDED.quantity",
            (user_id, product_id, quantity),
        )
        return CartService._get_item(user_id, product_id)

    @staticmethod
    def update(user_id: int, product_id: int, quantity: int) -> dict:
        """Update cart item quantity. quantity=0 removes the item."""
        if quantity <= 0:
            execute(
                "DELETE FROM shop.cart_items WHERE user_id = %s AND product_id = %s",
                (user_id, product_id),
            )
            return {}
        execute(
            "UPDATE shop.cart_items SET quantity = %s "
            "WHERE user_id = %s AND product_id = %s",
            (quantity, user_id, product_id),
        )
        return CartService._get_item(user_id, product_id)

    @staticmethod
    def remove(user_id: int, product_id: int):
        execute(
            "DELETE FROM shop.cart_items WHERE user_id = %s AND product_id = %s",
            (user_id, product_id),
        )

    @staticmethod
    def list_items(user_id: int) -> list[dict]:
        return query_all(
            "SELECT ci.product_id, ci.quantity, p.name as product_name, "
            "p.price, p.image_url, p.stock, p.status "
            "FROM shop.cart_items ci "
            "JOIN shop.products p ON p.id = ci.product_id "
            "WHERE ci.user_id = %s "
            "ORDER BY ci.created_at DESC",
            (user_id,),
        )

    @staticmethod
    def clear(user_id: int):
        execute("DELETE FROM shop.cart_items WHERE user_id = %s", (user_id,))

    @staticmethod
    def _get_item(user_id: int, product_id: int) -> dict:
        row = query_one(
            "SELECT ci.product_id, ci.quantity, p.name as product_name, p.price "
            "FROM shop.cart_items ci JOIN shop.products p ON p.id = ci.product_id "
            "WHERE ci.user_id = %s AND ci.product_id = %s",
            (user_id, product_id),
        )
        return row or {}

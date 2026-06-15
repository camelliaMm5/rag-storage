"""ProductService — category tree, product listing, search, detail."""
from infrastructure.shop_database import query_one, query_all


class ProductService:

    @staticmethod
    def get_category_tree() -> list[dict]:
        """Return two-level category tree."""
        parents = query_all(
            "SELECT id, name, sort_order FROM shop.categories "
            "WHERE parent_id IS NULL ORDER BY sort_order"
        )
        children = query_all(
            "SELECT id, name, parent_id, sort_order FROM shop.categories "
            "WHERE parent_id IS NOT NULL ORDER BY sort_order"
        )
        child_map: dict = {}
        for c in children:
            child_map.setdefault(c["parent_id"], []).append(c)
        for p in parents:
            p["children"] = child_map.get(p["id"], [])
        return parents

    @staticmethod
    def list_products(category_id: int | None = None, keyword: str = "",
                      page: int = 1, size: int = 20) -> dict:
        """List on_sale products with optional category filter and keyword search."""
        conditions = ["p.status = 'on_sale'"]
        params: list = []

        if category_id:
            # Include sub-categories
            sub_ids = [category_id]
            subs = query_all(
                "SELECT id FROM shop.categories WHERE parent_id = %s", (category_id,)
            )
            sub_ids.extend(s["id"] for s in subs)
            placeholders = ",".join(["%s"] * len(sub_ids))
            conditions.append(f"p.category_id IN ({placeholders})")
            params.extend(sub_ids)

        if keyword:
            conditions.append("p.name ILIKE %s")
            params.append(f"%{keyword}%")

        where = " AND ".join(conditions)
        count_sql = f"SELECT count(*) as cnt FROM shop.products p WHERE {where}"
        total = query_one(count_sql, tuple(params))["cnt"]

        offset = (page - 1) * size
        data_sql = (
            f"SELECT p.id, p.name, p.description, p.price, p.image_url, p.stock, "
            f"p.category_id, p.status, c.name as category_name "
            f"FROM shop.products p "
            f"JOIN shop.categories c ON c.id = p.category_id "
            f"WHERE {where} "
            f"ORDER BY p.created_at DESC "
            f"LIMIT %s OFFSET %s"
        )
        rows = query_all(data_sql, tuple(params) + (size, offset))

        return {
            "items": rows,
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size if total > 0 else 0,
        }

    @staticmethod
    def get_product(product_id: int) -> dict | None:
        """Get product detail by id (including off_sale for admin)."""
        row = query_one(
            "SELECT p.id, p.name, p.description, p.price, p.image_url, p.stock, "
            "p.category_id, p.status, c.name as category_name "
            "FROM shop.products p "
            "JOIN shop.categories c ON c.id = p.category_id "
            "WHERE p.id = %s",
            (product_id,),
        )
        return row

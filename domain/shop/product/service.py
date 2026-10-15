"""ProductService — category tree, product listing, search, detail.

All three read paths are Cache-Aside cached (namespace ``shop``). They are
read-mostly and the list endpoint costs a count query + a join, so caching
pays off. Any stock change (order create / cancel) invalidates the whole
namespace so callers never see a stale ``stock`` value.

Note on types: ``price`` comes back from psycopg2 as ``Decimal``, which JSON
cannot represent. ``_jsonable`` normalises Decimal → float *on both paths*
(hit and miss) so cached and uncached responses have identical types. FastAPI
already serialises Decimal to a JSON number, so the wire format is unchanged.
"""
from decimal import Decimal

from infrastructure.cache import cache, NS_SHOP, TTL_CATEGORY, TTL_PRODUCT
from infrastructure.shop_database import query_one, query_all


def _jsonable(value):
    """Recursively convert Decimal → float so cached rows round-trip cleanly."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    return value


class ProductService:

    @staticmethod
    def get_category_tree() -> list[dict]:
        """Return two-level category tree (cached — categories change rarely)."""
        key = cache.key(NS_SHOP, "category", "tree")
        cached = cache.get(key)
        if cached is not None:
            return cached

        tree = _jsonable(ProductService._build_category_tree())
        cache.set(key, tree, TTL_CATEGORY)
        return tree

    @staticmethod
    def _build_category_tree() -> list[dict]:
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
        key = cache.key(NS_SHOP, "list", category_id or 0, keyword or "", page, size)
        cached = cache.get(key)
        if cached is not None:
            return cached

        result = _jsonable(
            ProductService._query_products(category_id, keyword, page, size)
        )
        cache.set(key, result, TTL_PRODUCT)
        return result

    @staticmethod
    def _query_products(category_id: int | None, keyword: str,
                        page: int, size: int) -> dict:
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
        """Get product detail by id (including off_sale for admin). Cached."""
        key = cache.key(NS_SHOP, "product", product_id)
        cached = cache.get(key)
        if cached is not None:
            return cached

        row = ProductService._query_product(product_id)
        if row is None:
            return None  # don't cache negative lookups
        row = _jsonable(row)
        cache.set(key, row, TTL_PRODUCT)
        return row

    @staticmethod
    def _query_product(product_id: int) -> dict | None:
        return query_one(
            "SELECT p.id, p.name, p.description, p.price, p.image_url, p.stock, "
            "p.category_id, p.status, c.name as category_name "
            "FROM shop.products p "
            "JOIN shop.categories c ON c.id = p.category_id "
            "WHERE p.id = %s",
            (product_id,),
        )

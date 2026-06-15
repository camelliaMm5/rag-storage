"""B-endpoint: admin product/category/order management."""
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from infrastructure.auth import require_admin
from infrastructure.shop_database import query_one, query_all, execute
from domain.shop.product import ProductService
from domain.shop.order import OrderService

router = APIRouter(prefix="/api/shop/admin", tags=["shop-admin"])


# ── Category ──

class CategoryRequest(BaseModel):
    name: str
    parent_id: int | None = None
    sort_order: int = 0


@router.post("/categories")
def create_category(req: CategoryRequest, admin: dict = Depends(require_admin)):
    cid = execute(
        "INSERT INTO shop.categories (name, parent_id, sort_order) VALUES (%s, %s, %s)",
        (req.name, req.parent_id, req.sort_order),
    )
    return {"code": 0, "message": "分类已创建"}


@router.put("/categories/{category_id}")
def update_category(category_id: int, req: CategoryRequest, admin: dict = Depends(require_admin)):
    execute(
        "UPDATE shop.categories SET name=%s, parent_id=%s, sort_order=%s WHERE id=%s",
        (req.name, req.parent_id, req.sort_order, category_id),
    )
    return {"code": 0, "message": "分类已更新"}


@router.delete("/categories/{category_id}")
def delete_category(category_id: int, admin: dict = Depends(require_admin)):
    refs = query_one(
        "SELECT count(*) as cnt FROM shop.products WHERE category_id = %s", (category_id,)
    )
    if refs["cnt"] > 0:
        raise HTTPException(422, "该分类下有商品，无法删除")
    execute("DELETE FROM shop.categories WHERE id = %s", (category_id,))
    return {"code": 0, "message": "分类已删除"}


# ── Product ──

class ProductRequest(BaseModel):
    name: str
    description: str = ""
    price: float
    image_url: str = ""
    stock: int = 0
    category_id: int


@router.post("/products")
def create_product(req: ProductRequest, admin: dict = Depends(require_admin)):
    execute(
        "INSERT INTO shop.products (name, description, price, image_url, stock, category_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (req.name, req.description, req.price, req.image_url, req.stock, req.category_id),
    )
    return {"code": 0, "message": "商品已发布"}


@router.put("/products/{product_id}")
def update_product(product_id: int, req: ProductRequest, admin: dict = Depends(require_admin)):
    execute(
        "UPDATE shop.products SET name=%s, description=%s, price=%s, image_url=%s, "
        "stock=%s, category_id=%s, updated_at=NOW() WHERE id=%s",
        (req.name, req.description, req.price, req.image_url,
         req.stock, req.category_id, product_id),
    )
    return {"code": 0, "message": "商品已更新"}


@router.put("/products/{product_id}/status")
def toggle_product_status(product_id: int, status: str = Query(...),
                          admin: dict = Depends(require_admin)):
    if status not in ("on_sale", "off_sale"):
        raise HTTPException(400, "status 仅支持 on_sale / off_sale")
    execute("UPDATE shop.products SET status=%s, updated_at=NOW() WHERE id=%s",
            (status, product_id))
    return {"code": 0, "message": f"商品已{ '上架' if status == 'on_sale' else '下架'}"}


# ── Orders (view only) ──

@router.get("/orders")
def list_all_orders(
    status: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    admin: dict = Depends(require_admin),
):
    result = OrderService.list_all(status, page, size)
    return {"code": 0, "data": result}

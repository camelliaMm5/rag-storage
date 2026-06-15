"""C-endpoint product routes — browse, search, detail."""
from fastapi import APIRouter, HTTPException, Query

from domain.shop.product import ProductService

router = APIRouter(prefix="/api/shop", tags=["shop-products"])


@router.get("/categories")
def get_categories():
    """Return two-level category tree."""
    return {"code": 0, "data": ProductService.get_category_tree()}


@router.get("/products")
def list_products(
    category_id: int | None = Query(None),
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """List on_sale products (no login required)."""
    result = ProductService.list_products(category_id, keyword, page, size)
    return {"code": 0, "data": result}


@router.get("/products/{product_id}")
def get_product(product_id: int):
    """Product detail. Only on_sale for unauthenticated users."""
    product = ProductService.get_product(product_id)
    if not product or product["status"] == "off_sale":
        raise HTTPException(404, "商品不存在")
    return {"code": 0, "data": product}

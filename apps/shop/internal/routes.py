"""Internal API — for AI customer service to query business data.

Authentication: X-Internal-Token header.
"""
import os
from fastapi import APIRouter, Depends, HTTPException, Header, Query

from infrastructure.shop_database import query_one, query_all
from domain.shop.product import ProductService
from domain.shop.order import OrderService
from domain.shop.logistics import LogisticsService
from domain.shop.after_sale import AfterSaleService

INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "dev-internal-token")

router = APIRouter(prefix="/internal", tags=["internal"])


def verify_internal(x_internal_token: str = Header(...)):
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(401, "Invalid internal token")
    return True


@router.get("/orders")
def internal_orders(
    user_id: int = Query(...),
    status: str = Query(""),
    _auth: bool = Depends(verify_internal),
):
    result = OrderService.list_orders(user_id, status)
    return {"code": 0, "data": result}


@router.get("/orders/{order_id}")
def internal_order_detail(
    order_id: int,
    user_id: int = Query(...),
    _auth: bool = Depends(verify_internal),
):
    order = OrderService.get_detail(order_id, user_id)
    return {"code": 0, "data": order}


@router.get("/logistics")
def internal_logistics(
    order_id: int = Query(...),
    _auth: bool = Depends(verify_internal),
):
    log = LogisticsService.get_by_order_internal(order_id)
    if not log:
        raise HTTPException(404, "物流信息不存在")
    return {"code": 0, "data": log}


@router.get("/after-sales")
def internal_after_sales(
    user_id: int = Query(...),
    _auth: bool = Depends(verify_internal),
):
    tickets = AfterSaleService.list_by_user_internal(user_id)
    return {"code": 0, "data": tickets}


@router.get("/products/search")
def internal_product_search(
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _auth: bool = Depends(verify_internal),
):
    result = ProductService.list_products(keyword=keyword, page=page, size=size)
    return {"code": 0, "data": result}


@router.get("/products/{product_id}")
def internal_product_detail(
    product_id: int,
    _auth: bool = Depends(verify_internal),
):
    product = ProductService.get_product(product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    return {"code": 0, "data": product}


@router.get("/users/{user_id}")
def internal_user(
    user_id: int,
    _auth: bool = Depends(verify_internal),
):
    from domain.shop.user import UserService
    u = UserService.get_user_by_id(user_id)
    if not u:
        raise HTTPException(404, "用户不存在")
    return {"code": 0, "data": u}

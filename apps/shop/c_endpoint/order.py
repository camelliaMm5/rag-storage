"""C-endpoint: cart, order, payment, logistics, after-sale routes."""
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from infrastructure.auth import get_current_shop_user
from domain.shop.cart import CartService
from domain.shop.order import OrderService
from domain.shop.logistics import LogisticsService
from domain.shop.after_sale import AfterSaleService

router = APIRouter(prefix="/api/shop", tags=["shop-c"])


# ── Cart ──

class CartAddRequest(BaseModel):
    product_id: int
    quantity: int = 1


@router.get("/cart")
def list_cart(user: dict = Depends(get_current_shop_user)):
    items = CartService.list_items(user["id"])
    total = sum(i["price"] * i["quantity"] for i in items if i["status"] == "on_sale")
    return {"code": 0, "data": {"items": items, "total_amount": round(total, 2)}}


@router.post("/cart")
def add_cart(req: CartAddRequest, user: dict = Depends(get_current_shop_user)):
    try:
        item = CartService.add(user["id"], req.product_id, req.quantity)
        return {"code": 0, "data": item, "message": "已加入购物车"}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.put("/cart/{product_id}")
def update_cart(product_id: int, quantity: int = Query(ge=1),
                user: dict = Depends(get_current_shop_user)):
    item = CartService.update(user["id"], product_id, quantity)
    return {"code": 0, "data": item}


@router.delete("/cart/{product_id}")
def remove_cart(product_id: int, user: dict = Depends(get_current_shop_user)):
    CartService.remove(user["id"], product_id)
    return {"code": 0, "message": "已删除"}


# ── Order ──

class CreateOrderRequest(BaseModel):
    address: str


@router.post("/orders")
def create_order(req: CreateOrderRequest, user: dict = Depends(get_current_shop_user)):
    try:
        order = OrderService.create(user["id"], req.address)
        return {"code": 0, "data": order, "message": "下单成功"}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/orders")
def list_orders(
    status: str = Query(""),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_shop_user),
):
    result = OrderService.list_orders(user["id"], status, page, size)
    return {"code": 0, "data": result}


@router.get("/orders/{order_id}")
def get_order(order_id: int, user: dict = Depends(get_current_shop_user)):
    try:
        order = OrderService.get_detail(order_id, user["id"])
        return {"code": 0, "data": order}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/orders/{order_id}/pay")
def pay_order(order_id: int, user: dict = Depends(get_current_shop_user)):
    try:
        order = OrderService.pay(order_id, user["id"])
        return {"code": 0, "data": order, "message": "支付成功"}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, user: dict = Depends(get_current_shop_user)):
    try:
        order = OrderService.cancel(order_id, user["id"])
        return {"code": 0, "data": order, "message": "订单已取消"}
    except ValueError as e:
        raise HTTPException(422, str(e))


# ── Logistics ──

@router.get("/logistics/{order_id}")
def get_logistics(order_id: int, user: dict = Depends(get_current_shop_user)):
    log = LogisticsService.get_by_order(order_id, user["id"])
    if not log:
        raise HTTPException(404, "物流信息不存在")
    return {"code": 0, "data": log}


# ── After-Sale ──

class AfterSaleRequest(BaseModel):
    order_id: int
    type: str  # refund / return
    reason: str = ""


@router.post("/after-sales")
def create_after_sale(req: AfterSaleRequest, user: dict = Depends(get_current_shop_user)):
    try:
        ticket = AfterSaleService.create(user["id"], req.order_id, req.type, req.reason)
        return {"code": 0, "data": ticket, "message": "售后申请已提交"}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/after-sales")
def list_after_sales(user: dict = Depends(get_current_shop_user)):
    tickets = AfterSaleService.list_by_user(user["id"])
    return {"code": 0, "data": tickets}

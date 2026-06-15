"""C-endpoint auth routes — register, login."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from domain.shop.user import UserService

router = APIRouter(prefix="/api/shop/auth", tags=["shop-auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str
    nickname: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict


@router.post("/register")
def register(req: RegisterRequest):
    try:
        user = UserService.register(req.email, req.password, req.nickname)
        return {"code": 0, "data": user, "message": "注册成功"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/login")
def login(req: LoginRequest):
    try:
        result = UserService.login(req.email, req.password)
        return {"code": 0, "data": result, "message": "登录成功"}
    except ValueError as e:
        raise HTTPException(401, str(e))

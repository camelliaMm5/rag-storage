"""UserService — registration, login, JWT issuance."""
import os
from datetime import datetime, timedelta

import bcrypt
import jwt

from infrastructure.shop_database import query_one, execute

JWT_SECRET = os.getenv("JWT_SECRET", "demo-secret-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_MINUTES", "1440")) // 60  # 24h


class UserService:

    @staticmethod
    def register(email: str, password: str, nickname: str) -> dict:
        """Register a new user. Returns user dict or raises ValueError."""
        if not email or "@" not in email:
            raise ValueError("邮箱格式不正确")
        if len(password) < 6:
            raise ValueError("密码至少6位")

        existing = query_one("SELECT id FROM shop.users WHERE email = %s", (email,))
        if existing:
            raise ValueError("该邮箱已注册")

        pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        execute(
            "INSERT INTO shop.users (email, password, nickname, role) VALUES (%s, %s, %s, 'user')",
            (email, pwd_hash, nickname),
        )
        user = query_one(
            "SELECT id, email, nickname, role, address FROM shop.users WHERE email = %s",
            (email,),
        )
        return user

    @staticmethod
    def login(email: str, password: str) -> dict:
        """Validate credentials. Returns dict with access_token and user info."""
        user = query_one(
            "SELECT id, email, password, nickname, role, address FROM shop.users WHERE email = %s",
            (email,),
        )
        if not user:
            raise ValueError("邮箱或密码错误")

        if not bcrypt.checkpw(password.encode(), user["password"].encode()):
            raise ValueError("邮箱或密码错误")

        token = _create_token(user)
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "nickname": user["nickname"],
                "role": user["role"],
            },
        }

    @staticmethod
    def get_user_by_id(user_id: int) -> dict | None:
        return query_one(
            "SELECT id, email, nickname, role, address FROM shop.users WHERE id = %s",
            (user_id,),
        )


def _create_token(user: dict) -> str:
    payload = {
        "user_id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

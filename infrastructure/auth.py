"""JWT Bearer Token authentication — FastAPI dependency.

Supports two JWT payload formats:
- Old (demo): {"sub": "zhangsan", "exp": ...}
- New (shop): {"user_id": 1, "email": "...", "role": "user", "exp": ...}
"""
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

JWT_SECRET = os.getenv("JWT_SECRET", "demo-secret-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

security = HTTPBearer()


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def create_access_token(user_id: str) -> str:
    """Generate a JWT demo token (for testing / backwards compat)."""
    payload = {
        "sub": user_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Extract user_id (string) from Bearer token. Compatible with old demo tokens."""
    token = credentials.credentials
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token")

    # New format: user_id int → cast to str
    uid = payload.get("user_id")
    if uid is not None:
        return str(uid)

    # Old format: sub string
    uid = payload.get("sub")
    if uid is not None:
        return str(uid)

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Token missing user identity")


def get_current_shop_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Extract full user info (id, email, role) from Bearer token.

    Usage:
        def my_route(user: dict = Depends(get_current_shop_user)):
            assert user["role"] == "admin"
    """
    token = credentials.credentials
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token")

    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token missing user_id")

    return {
        "id": user_id,
        "email": payload.get("email", ""),
        "role": payload.get("role", "user"),
    }


def require_admin(user: dict = Depends(get_current_shop_user)) -> dict:
    """Dependency: require admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin only")
    return user

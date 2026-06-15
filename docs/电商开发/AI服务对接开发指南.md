# AI 客服服务接入 shop-service 开发指南

## 1. 架构概览

```
用户浏览器
    │ POST /api/ai/chat
    │ Authorization: Bearer <user_jwt>
    │ Body: {"question": "我的订单到哪了？"}
    ▼
Nginx (:83)
    │ proxy_pass → ai-service
    ▼
AI Service
    │ ① 解码 user_jwt → user_id
    │ ② 调用 shop-service 内部接口
    │ ③ 用数据 + LLM 生成自然语言回复
    ▼
shop-service (:8004, Docker 内网)
    │ GET /internal/orders?user_id=5
    │ Header: X-Internal-Token: dev-internal-token
    ▼
返回业务数据 JSON
```

**核心原则：** user_id 必须由 AI 服务从 JWT 中解码获得，**绝对不可由前端传入**。

---

## 2. 可复用的代码片段（本项目中的参考位置）

### 2.1 JWT 解码 — 提取 user_id

shop-service 中的参考实现：
- 签发逻辑：[shop-service/domain/user/user_service.py](file:///d:/code/py_project/PythonProject/eshop_spec/shop-service/domain/user/user_service.py#L74-L82) — 查看 JWT payload 结构
- 解码逻辑：[shop-service/apps/common/auth.py](file:///d:/code/py_project/PythonProject/eshop_spec/shop-service/apps/common/auth.py#L16-L27) — `get_current_user` 中的 `jwt.decode` 调用

**AI 服务中需要的代码（直接复用上述解码逻辑）：**

```python
# ai-service/utils/auth.py
import os
import jwt

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")


def extract_user_from_token(authorization: str) -> dict:
    """
    从 Authorization Header 中解码用户 JWT
    返回 {"user_id": int, "email": str, "role": str}
    参考: shop-service/apps/common/auth.py:16-27
    """
    if not authorization:
        raise ValueError("缺失 Authorization Header")
    scheme, token = authorization.split(" ", 1)
    if scheme.lower() != "bearer":
        raise ValueError("Authorization 格式错误，应为 Bearer <token>")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return {
            "user_id": payload["user_id"],
            "email": payload["email"],
            "role": payload["role"],
        }
    except jwt.ExpiredSignatureError:
        raise ValueError("Token 已过期")
    except jwt.InvalidTokenError:
        raise ValueError("Token 无效")
```

**JWT payload 结构（参考 [user_service.py:L76-L81](file:///d:/code/py_project/PythonProject/eshop_spec/shop-service/domain/user/user_service.py#L76-L81)）：**

```python
{
    "user_id": 5,                    # int, 用户 ID
    "email": "user@example.com",     # str
    "role": "user",                  # str, "user" 或 "admin"
    "exp": 1716768000,               # int, 过期时间戳（24h 后）
}
```

### 2.2 统一响应格式

shop-service 参考：[internal/router.py](file:///d:/code/py_project/PythonProject/eshop_spec/shop-service/apps/internal/router.py) 所有端点

```json
{
    "code": 0,           // 0=成功, 非0=失败
    "data": { ... },     // 业务数据，失败时为 null
    "message": "success"  // 提示信息
}
```

---

## 3. 依赖

只需 `httpx` 和 `pyjwt`，均在 shop-service 的 [requirements.txt](file:///d:/code/py_project/PythonProject/eshop_spec/requirements.txt) 中：

```
httpx==0.28.1
pyjwt==2.10.1
```

---

## 4. ShopInternalClient — 内部接口客户端封装

复制以下代码到 AI 服务中，等价于 Spring Cloud 的 `@FeignClient`：

```python
# ai-service/clients/shop_client.py
import os
import httpx


SHOP_URL = os.getenv("SHOP_INTERNAL_URL", "http://shop-service:8004/internal")
TOKEN = os.getenv("INTERNAL_API_TOKEN", "dev-internal-token")


class ShopInternalClient:
    """shop-service 内部接口同步客户端"""

    def __init__(self, timeout: float = 5.0):
        self._client = httpx.Client(timeout=timeout)
        self._headers = {"X-Internal-Token": TOKEN}

    def _get(self, path: str, params: dict = None) -> dict:
        r = self._client.get(f"{SHOP_URL}{path}", params=params, headers=self._headers)
        r.raise_for_status()
        return r.json()

    # ---- 订单 ----

    def get_orders(self, user_id: int, page: int = 1, size: int = 20) -> dict:
        return self._get("/orders", {"user_id": user_id, "page": page, "size": size})

    def get_order_detail(self, order_id: int) -> dict:
        return self._get(f"/orders/{order_id}")

    # ---- 物流 ----

    def get_logistics(self, user_id: int) -> dict:
        return self._get("/logistics", {"user_id": user_id})

    # ---- 售后 ----

    def get_after_sales(self, user_id: int) -> dict:
        return self._get("/after-sales", {"user_id": user_id})

    # ---- 商品 ----

    def search_products(self, keyword: str, page: int = 1, size: int = 20) -> dict:
        return self._get("/products/search", {"keyword": keyword, "page": page, "size": size})

    def get_product(self, product_id: int) -> dict:
        return self._get(f"/products/{product_id}")

    # ---- 用户 ----

    def get_user(self, user_id: int) -> dict:
        return self._get(f"/users/{user_id}")

    def close(self):
        self._client.close()
```

---

## 5. AI 聊天接口实现示例

```python
# ai-service/routers/chat.py
from fastapi import APIRouter, Header
from pydantic import BaseModel
from clients.shop_client import ShopInternalClient
from utils.auth import extract_user_from_token

router = APIRouter(prefix="/api/ai", tags=["AI客服"])
shop = ShopInternalClient()


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str
    data: dict | None = None


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    authorization: str = Header(...),
):
    # ① 解码 JWT 获取用户身份
    try:
        user = extract_user_from_token(authorization)
    except ValueError as e:
        return ChatResponse(answer=str(e))
    user_id = user["user_id"]

    # ② 根据问题意图调用对应内部接口
    question = body.question
    if "订单" in question or "买了" in question:
        result = shop.get_orders(user_id=user_id)
    elif "物流" in question or "快递" in question or "到哪" in question:
        result = shop.get_logistics(user_id=user_id)
    elif "售后" in question or "退货" in question or "退款" in question:
        result = shop.get_after_sales(user_id=user_id)
    elif "推荐" in question or "有什么" in question:
        result = shop.search_products(keyword="热门", size=5)
    else:
        result = shop.get_orders(user_id=user_id)

    # ③ 用 LLM 生成回复（伪代码，替换为实际 LLM 调用）
    answer = f"已为您查到 {len(result.get('data', {}).get('items', []))} 条记录"
    return ChatResponse(answer=answer, data=result.get("data"))
```

---

## 6. 环境变量清单

| 变量 | 说明 | 默认值 |
|---|---|---|
| `JWT_SECRET` | JWT 签名密钥，与 shop-service 相同 | `dev-secret-change-in-production` |
| `INTERNAL_API_TOKEN` | 调用 shop-service 内部接口的 Token | `dev-internal-token` |
| `SHOP_INTERNAL_URL` | shop-service 内部地址（Docker 容器名） | `http://shop-service:8004/internal` |

---

## 7. Docker Compose 集成

AI 服务加入同一 `docker-compose.yml`（与 shop-service 同网络）：

```yaml
ai-service:
  build: ./ai-service
  ports:
    - "8003:8003"
  environment:
    - JWT_SECRET=${JWT_SECRET:-dev-secret-change-in-production}
    - INTERNAL_API_TOKEN=${INTERNAL_API_TOKEN:-dev-internal-token}
    - SHOP_INTERNAL_URL=http://shop-service:8004/internal
  depends_on:
    - shop-service
  restart: unless-stopped
```

Nginx 代理规则（参考 [nginx.conf](file:///d:/code/py_project/PythonProject/eshop_spec/nginx/nginx.conf#L34-L37)）：

```nginx
location /api/ai/ {
    proxy_pass http://ai-service:8003/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

---

## 8. 接口速查表

| 端点 | 方法 | 参数 | 返回 |
|---|---|---|---|
| `/internal/orders` | GET | `user_id`(必填), `page`, `size` | 订单列表 `{items, total, page, size}` |
| `/internal/orders/{id}` | GET | path `id` | 订单详情含 `items` 明细 |
| `/internal/logistics` | GET | `user_id`(必填) | 物流记录列表 |
| `/internal/after-sales` | GET | `user_id`(必填) | 售后申请列表 |
| `/internal/products/search` | GET | `keyword`(必填), `page`, `size` | 商品列表 |
| `/internal/products/{id}` | GET | path `id` | 商品详情 |
| `/internal/users/{id}` | GET | path `id` | 用户基本信息 |

---

## 9. 安全注意事项

| 规则 | 说明 |
|---|---|
| **禁止前端传 user_id** | user_id 必须从 JWT 中解码，不可信任前端参数 |
| **共享 JWT_SECRET** | shop-service 和 AI 服务必须使用相同的签名密钥 |
| **X-Internal-Token 不对外暴露** | Nginx 已配置 `/api/shop/internal/` 返回 403 |
| **同步调用** | 内部接口为同步 REST，LLM 调用前完成数据查询 |
# 智能家居商城 — AI 多智能体客服 + 电商平台

基于 LangGraph Supervisor 架构的 8-Agent 多智能体客服系统，集成简版电商后端。支持 FAQ 知识库检索（ChromaDB RAG）、订单/物流/售后/购物车查询、订单列表、自然语言下单。电商侧覆盖用户注册登录（JWT + bcrypt）、商品浏览、购物车、下单、支付、物流追踪、售后申请的完整交易闭环。

**LLM**：DeepSeek Chat（OpenAI 兼容接口）| **向量库**：ChromaDB + sentence-transformers | **数据库**：PostgreSQL 16 | **会话**：PostgreSQL + LangGraph Checkpointer

---

## 架构总览

```
用户浏览器 (http://localhost:8000)
    │
    ├── /api/chat/stream         → AI 智能客服（SSE 流式）
    ├── /api/shop/products       → 商品浏览
    ├── /api/shop/cart           → 购物车
    ├── /api/shop/orders         → 订单管理
    ├── /api/shop/after-sales    → 售后管理
    ├── /api/shop/admin/*        → 管理后台（admin only）
    └── /internal/*              → AI 内部查询接口
         │
         ▼
FastAPI (:8000)
    ├── apps/customer_service/   ← AI 客服路由
    ├── apps/shop/               ← 电商路由 (C端 + B端 + Internal)
    ├── domain/customer_service/ ← 多智能体（Master + 7 Sub-Agents）
    ├── domain/shop/             ← 电商业务逻辑
    ├── infrastructure/          ← DB 连接池、RAG、Auth
    └── tools/                   ← Agent 工具层
         │
         ▼
PostgreSQL (:5432)
    ├── shop.*                   ← 9 张电商表
    └── customer_service.*       ← 2 张AI客服表
```

## 多智能体拓扑

```
用户 → Master Agent（Supervisor 意图路由）
           ├── FAQ Agent          → ChromaDB RAG 知识库检索
           ├── Order Agent        → 查询单个订单详情 (shop.orders)
           ├── ListOrders Agent   → 列出当前用户所有订单
           ├── Logistics Agent    → 查询物流轨迹 (shop.logistics_records)
           ├── AfterSale Agent    → 查询售后工单 (shop.after_sale_requests)
           ├── CartQuery Agent    → 查询购物车内容 (shop.cart_items)
           ├── PlaceOrder Agent   → 自然语言下单（购物车 + 订单）
           └── Finish             → 闲聊兜底
```

---

## 快速开始

### 1. 前置条件

- Docker Desktop 已启动
- Python 3.10+

### 2. 启动 PostgreSQL

```bash
docker-compose up -d
```

### 3. 导入建表和数据

```bash
docker exec -i qa_agent_pgsql psql -U user -d agent < init.sql
```

### 4. 配置环境变量

编辑 `.env`，确认 LLM API Key：

```env
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
CONVERSATION_DB_URL=postgresql://user:1234@localhost:5432/agent
```

### 5. 安装依赖并启动

```bash
pip install -r requirements.txt
python main.py
```

浏览器打开 **http://localhost:8000**

---

## 测试账号

| 角色 | 邮箱 | 密码 |
|------|------|------|
| 管理员 | admin@shop.local | admin123 |
| 用户 | zhangsan@shop.local | user123 |
| 用户 | lisi@shop.local | lisi123 |

---

## API 清单

### AI 客服

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/chat` | JWT | 同步对话 |
| POST | `/api/chat/stream` | JWT | SSE 流式对话（含推理过程） |
| POST | `/api/conversations` | JWT | 创建会话 |
| GET | `/api/conversations/{id}` | JWT | 获取会话历史 |
| GET | `/api/conversations` | JWT | 列出用户会话 |
| GET | `/api/health` | — | 健康检查 |

**AI 对话能力**（自然语言触发，自动路由）：

| 用户说 | 路由到 | 返回 |
|--------|--------|------|
| "订单1001发货了吗？" | Order Agent | 订单详情（状态/金额/商品明细） |
| "我有哪些订单？" | ListOrders Agent | 当前用户全部订单列表 |
| "1001快递到哪了？" | Logistics Agent | 物流轨迹（快递公司/节点/位置） |
| "售后工单1进度" | AfterSale Agent | 售后工单状态/原因/时间 |
| "我的购物车有什么？" | CartQuery Agent | 购物车商品列表+金额 |
| "我要买X1智能门锁" | PlaceOrder Agent | 创建订单（扣库存+清购物车） |
| "怎么退货？" | FAQ Agent | RAG 检索知识库答案 |
| "今天天气怎么样？" | Finish | 闲聊兜底 |

### 认证

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/shop/auth/register` | — | 用户注册（email + password + nickname） |
| POST | `/api/shop/auth/login` | — | 用户登录，返回 JWT（含 role） |
| POST | `/api/token` | — | Demo Token（向后兼容） |

### C 端（普通用户）

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/api/shop/categories` | — | 分类树 |
| GET | `/api/shop/products` | — | 商品列表（分类筛选 + 关键词搜索 + 分页） |
| GET | `/api/shop/products/{id}` | — | 商品详情 |
| GET | `/api/shop/cart` | JWT | 查看购物车 |
| POST | `/api/shop/cart` | JWT | 加入购物车（UPSERT 幂等） |
| PUT | `/api/shop/cart/{product_id}` | JWT | 修改数量 |
| DELETE | `/api/shop/cart/{product_id}` | JWT | 删除购物车项 |
| POST | `/api/shop/orders` | JWT | 创建订单（锁库存 + 清购物车） |
| GET | `/api/shop/orders` | JWT | 我的订单列表 |
| GET | `/api/shop/orders/{id}` | JWT | 订单详情 |
| POST | `/api/shop/orders/{id}/pay` | JWT | 模拟支付（幂等） |
| POST | `/api/shop/orders/{id}/cancel` | JWT | 取消订单（回滚库存） |
| GET | `/api/shop/logistics/{order_id}` | JWT | 物流查询 |
| POST | `/api/shop/after-sales` | JWT | 申请售后 |
| GET | `/api/shop/after-sales` | JWT | 售后列表 |

### B 端（管理员）

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/shop/admin/categories` | admin | 创建分类 |
| PUT | `/api/shop/admin/categories/{id}` | admin | 编辑分类 |
| DELETE | `/api/shop/admin/categories/{id}` | admin | 删除分类 |
| POST | `/api/shop/admin/products` | admin | 发布商品 |
| PUT | `/api/shop/admin/products/{id}` | admin | 编辑商品 |
| PUT | `/api/shop/admin/products/{id}/status` | admin | 上下架 |
| GET | `/api/shop/admin/orders` | admin | 查看全部订单 |

### Internal（AI 服务内部调用）

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/internal/orders` | X-Internal-Token | 按 user_id 查订单 |
| GET | `/internal/orders/{id}` | X-Internal-Token | 订单详情 |
| GET | `/internal/logistics` | X-Internal-Token | 按 order_id 查物流 |
| GET | `/internal/after-sales` | X-Internal-Token | 按 user_id 查售后 |
| GET | `/internal/products/search` | X-Internal-Token | 商品搜索 |
| GET | `/internal/products/{id}` | X-Internal-Token | 商品详情 |
| GET | `/internal/users/{id}` | X-Internal-Token | 用户信息 |

---

## 项目结构

```
rag-storage/
├── main.py                         ← FastAPI 入口 + 依赖装配
├── init.sql                        ← shop schema DDL + 种子数据
├── docker-compose.yml              ← PostgreSQL 15
├── .env                            ← 环境变量
├── requirements.txt
│
├── apps/
│   ├── customer_service/routes.py  ← AI 客服路由
│   └── shop/
│       ├── c_endpoint/             ← C端: auth / product / order
│       ├── b_endpoint/admin.py     ← B端: 分类+商品+订单管理
│       └── internal/routes.py      ← Internal API: AI 服务查询
│
├── domain/
│   ├── customer_service/           ← 多智能体
│   │   ├── master_graph.py         ← Supervisor 图 (Master + 5 Sub-Agents)
│   │   ├── graph.py                ← ReAct agent ⇄ tools 循环
│   │   ├── langgraph_agent.py      ← Agent 封装 (run + run_stream)
│   │   └── prompts.py              ← System Prompt
│   └── shop/                       ← 电商业务逻辑
│       ├── user/service.py         ← 注册/登录 (bcrypt + JWT)
│       ├── product/service.py      ← 商品/分类查询
│       ├── cart/service.py         ← 购物车 UPSERT
│       ├── order/service.py        ← 下单(事务)/支付/取消
│       ├── logistics/service.py    ← 物流查询
│       └── after_sale/service.py   ← 售后申请/查询
│
├── tools/                          ← Agent 工具层
│   ├── base.py                     ← Tool + ToolParameter
│   ├── faq_search.py               ← FAQ RAG 检索
│   ├── order_search.py             ← 订单/物流/下单/购物车/列表工具
│   └── after_sale_search.py        ← 售后查询工具
│
├── llm/
│   ├── client.py                   ← DeepSeek API (OpenAI SDK)
│   └── langchain_model.py          ← ChatService → BaseChatModel 适配
│
├── infrastructure/
│   ├── database.py                 ← SQLAlchemy (customer_service)
│   ├── shop_database.py            ← psycopg2 连接池 (shop)
│   ├── auth.py                     ← JWT 验证 + 依赖注入
│   ├── models.py                   ← ORM (conversations + messages)
│   ├── order_repository.py         ← [保留] Mock 数据
│   └── rag/                        ← ChromaDB + embedding + BM25
│
├── utils/conversation.py           ← 会话 CRUD + 上下文窗口
├── scripts/
│   ├── smoke_test.py               ← PostgreSQL 冒烟测试
│   └── scenario_test.py            ← 验收测试 (49 cases)
├── static/index.html               ← 前端 SPA
├── docs/                           ← 文档
└── tests/                          ← 单元测试
```

---

## 关键设计

### 下单事务（FOR UPDATE 防超卖）

```
BEGIN
  SELECT ... FROM shop.products WHERE id IN (...) FOR UPDATE  ← 行级锁
  逐条校验 stock >= quantity                                   ← 库存不足 → ROLLBACK
  UPDATE shop.products SET stock = stock - quantity            ← 扣库存
  INSERT INTO shop.orders (status='pending')                   ← 创订单
  INSERT INTO shop.order_items (snapshot)                      ← 快照明细
  DELETE FROM shop.cart_items                                  ← 清空购物车
COMMIT
```

### 支付幂等

```
BEGIN
  SELECT ... FROM shop.orders WHERE id = ... FOR UPDATE
  校验 status = 'pending' AND user_id = 当前用户
  UPDATE shop.orders SET status = 'paid'
  INSERT INTO shop.payment_records
  INSERT INTO shop.logistics_records (自动生成物流)
COMMIT
```

### 数据隔离

- C 端：`user_id` 从 JWT 提取，所有查询带 `WHERE user_id = %s`
- B 端：`role = admin` 可查看全部订单，可管理商品/分类
- Internal：`X-Internal-Token` 认证，AI Agent 调用查询接口

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API 密钥 | — |
| `LLM_BASE_URL` | LLM 接口地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名称 | `deepseek-chat` |
| `CONVERSATION_DB_URL` | 数据库连接 | `postgresql://user:1234@localhost:5432/agent` |
| `CONVERSATION_MAX_CONTEXT_TURNS` | 上下文窗口轮数 | `5` |
| `JWT_SECRET` | JWT 签名密钥 | `demo-secret-change-in-production` |
| `JWT_ALGORITHM` | JWT 算法 | `HS256` |
| `JWT_EXPIRE_MINUTES` | JWT 有效期（分钟） | `60` |
| `INTERNAL_API_TOKEN` | Internal API 认证 Token | `dev-internal-token` |
| `CHECKPOINT_DB_PATH` | LangGraph Checkpoint 路径 | `./checkpoints.db` |
| `EMBEDDING_MODEL` | 向量化模型 | `paraphrase-multilingual-MiniLM-L12-v2` |
| `CHROMA_DB_PATH` | 向量库路径 | `./chroma_db` |

---

## 运行测试

```bash
# 冒烟测试（PostgreSQL 连接 + 会话管理）
python scripts/smoke_test.py

# 验收测试（数据隔离 + JWT + 图结构 + 会话 49 cases）
python scripts/scenario_test.py
```

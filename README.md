# 智能家居商城 — AI 多智能体客服 + 电商平台

基于 **LangGraph Supervisor** 架构的多智能体客服系统，集成简版电商后端。支持 FAQ 知识库检索（ChromaDB RAG）、订单 / 物流 / 售后 / 购物车查询、订单列表、自然语言下单。电商侧覆盖注册登录（JWT + bcrypt）、商品浏览、购物车、下单、支付、物流追踪、售后申请的完整交易闭环。

**LLM**：OpenAI 兼容端点（默认 DeepSeek Chat）| **向量库**：ChromaDB + sentence-transformers | **数据库**：PostgreSQL 15 | **缓存**：Redis 7（可选）| **图持久化**：AsyncSqliteSaver

> 📖 详细文档：[项目详细说明](docs/项目详细说明.md) · [重构改动说明](docs/重构改动说明.md)

---

## 它为什么是一个「完整的 Agent 项目」

| 要素 | 落点 |
|------|------|
| 自主决策循环 | 受限 ReAct（`domain/customer_service/react.py`）：**统一只读工具集 + prompt 软引导**，子 Agent 自行选工具、可在单轮内多步链式调用（如 `list_my_orders` → `query_order`），有步数上限与兜底 |
| 可用记忆 | checkpoint 热记忆 + PostgreSQL 耐久记忆 + 跨轮 `pending_intent`（澄清闭环） |
| 韧性 | 路由降级 / 工具异常隔离 / Redis 熔断降级 / LLM 未配置走友好话术（不 500） |
| 可观测性 | `agent_traces` 表 + `GET /api/traces` + LLM 用量计量 + `/api/health` |
| 安全 | JWT + IDOR 防护 + **服务端注入鉴权主体** + prompt 与用户输入隔离 + Internal Token |

---

## 架构总览

```
用户浏览器 (http://localhost:8000)
    │
    ├── /api/chat/stream              → AI 智能客服（SSE token 打字机）
    ├── /api/traces                   → 可观测性（按用户隔离）
    ├── /api/shop/*                   → 商品 / 购物车 / 订单 / 支付 / 售后
    ├── /api/shop/admin/*             → 管理后台（admin only）
    └── /internal/*                   → AI 内部查询接口（X-Internal-Token）
         │
         ▼
FastAPI (:8000)
    ├── apps/customer_service/        ← AI 客服路由
    ├── apps/shop/                    ← 电商路由（C 端 + B 端 + Internal）
    ├── domain/customer_service/      ← Supervisor 图：1 Master + 7 子 Agent + 下单/澄清节点
    ├── domain/shop/                  ← 电商业务逻辑
    ├── infrastructure/               ← DB 连接池 / RAG / Auth / 缓存 / 中间件
    └── tools/                        ← Agent 工具层
         │
         ▼
PostgreSQL (:5432) ── shop.* / customer_service.* / agent_traces
ChromaDB (本地)    ── faq_knowledge
Redis (:6379)      ── 可选，Cache-Aside（不可用自动熔断降级）
checkpoints.db     ── LangGraph 图状态（AsyncSqliteSaver）
```

### 多智能体拓扑

```
用户 → supervisor（只做分类 + 抽槽 + 上下文恢复，不回答）
         │  resolve_target() ← 纯函数决定走向，条件边永不 KeyError
         ├── faq_agent           → ChromaDB 混合检索（向量 + BM25）
         ├── order_agent         → 单订单详情 / 我的订单列表
         ├── logistics_agent     → 物流轨迹
         ├── after_sale_agent    → 售后工单进度
         ├── cart_query_agent    → 购物车内容
         ├── list_orders_agent   → 订单列表
         ├── product_agent       → 在售商品浏览（list_products / search_products）
         ├── place_order_prepare → 只读校验 + 草稿回显（不写库）
         │   place_order_commit  → 用户确认后单事务落库
         │   place_order_cancel  → 丢弃草稿
         ├── clarify             → 缺参反问（下一轮续跑）
         └── finish              → 闲聊兜底（流式）
```

---

## 快速开始

### 1. 前置条件

- Docker Desktop 已启动
- Python 3.10+

### 2. 启动 PostgreSQL 与 Redis

```bash
docker-compose up -d
```

> Redis 是**可选优化**：不启动也能跑，缓存自动熔断降级为直查，只是命中率归零。

首次创建数据卷时 `init.sql` 会被自动导入；若数据卷已存在，手动导入：

```bash
docker exec -i qa_agent_pgsql psql -U user -d agent < init.sql
```

### 3. 配置环境变量

编辑 `.env`，至少配置 LLM Key：

```env
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
CONVERSATION_DB_URL=postgresql://user:1234@localhost:5432/agent

# 上线前务必设置（production 会关闭 demo token 并对默认密钥拒绝启动）
APP_ENV=development
JWT_SECRET=换成随机长串
INTERNAL_API_TOKEN=换成随机长串
```

### 4. 安装依赖并启动

```bash
pip install -r requirements.txt
python main.py
```

浏览器打开 **http://localhost:8000**

> **便携式 Python 注意**：若 `python -m pip` 报 `No module named pip`，说明该解释器的
> `site-packages` 不在 `sys.path` 上（pip 自身就装在 `site-packages` 里，所以连它都找不到）。
> 请改用项目自带的 **`pip.bat`**，它等价于 `set PYTHONPATH=<site-packages>` + `python -m pip`：
>
> ```bat
> pip.bat install -r requirements.txt
> ```
>
> 运行应用同理需带上 `PYTHONPATH`（见 `run.bat`）。

### 前端交互约定（`static/index.html`）

单文件原生 JS，不引入任何 UI 库。两条硬约定：

1. **不用原生 `alert` / `prompt` / `confirm`。** 在 Electron 内嵌 webview 或沙箱 iframe
   （`sandbox` 未开 `allow-modals`）里，浏览器会**静默屏蔽**这三个 API：`alert` 不弹、
   `prompt` 直接返回 `null`。旧版结算按钮用 `prompt` 收地址，`if (!addr) return;` 于是把
   「结算下单」变成一个点了毫无反应的死按钮。现在统一走页面内组件：
   `toast()` / `askText()` / `askConfirm()` / `showInfo()`。
2. **不吞异常。** 所有数据加载都走 `req()`，失败时把「后端给出的原因」显示出来，并附
   「重新加载」入口。旧版清一色 `catch(e) {}`：JWT 一过期（默认 60 分钟）接口全返 401，
   页面却毫无提示，表现就是「购物车空了 / 结算按钮不见了 / 点了没反应」。

错误信息透传规则：业务失败返回 `{"code":N,"message":...}`，FastAPI 抛的是 `{"detail":...}`，
`msgOf()` 两者都读（`detail` 为数组时展开 `msg[].msg`）。遇到 401 → 清身份并提示重新登录；
登录接口自身的 401（账号密码错误）用 `skipAuthRedirect` 排除在外，避免误报「登录过期」。

结算 / 支付按钮经 `withBusy()` 加忙碌态，防止连点产生重复订单。

---

## 测试账号

| 角色 | 邮箱 | 密码 |
|------|------|------|
| 管理员 | admin@shop.local | admin123 |
| 用户 | zhangsan@shop.local | user123 |
| 用户 | lisi@shop.local | lisi123 |

---

## 运行测试（四层）

```bash
# ① 离线单元/集成 —— 127 项，进程内异步，不花钱
python tests/test_agent_offline.py

# ② 多轮剧本回放 —— 24 项
python scripts/scenario_test.py

# ③ 端到端 —— 54 项（真实 HTTP + Mock LLM，需三个终端）
python scripts/mock_llm_server.py                                   # A: Mock LLM :8099
LLM_API_KEY=mock LLM_BASE_URL=http://127.0.0.1:8099/v1 LLM_MODEL=mock-model \
  python -m uvicorn main:app --port 8131                            # B: 应用
python scripts/e2e_mock.py                                          # C: 验证

# ④ 路由评测 —— 18 条标注样本（需真实 LLM_API_KEY）
python scripts/eval_routing.py

# 附：PostgreSQL 冒烟
python scripts/smoke_test.py
```

> 前两层能验证图结构与状态机，但**测不出「自研适配器 × 框架契约」和「SSE/前端契约」**——
> 端到端层才会暴露这类问题（例如 `_generate` 缺 `run_manager`、`user_id` 未注入工具、
> async 校验函数漏写 `await` 导致越权校验静默失效）。

> 另有一个非测试型的验证手段：`_scratch/probe_loop_block.py` 用 A/B 对照实证
> 「同步 DB 调用是否真的移出了事件循环」（走 `to_thread` 14.9ms vs 直接阻塞 428.3ms）。

---

## API 一览

### AI 客服

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/chat` | JWT | 同步对话 |
| POST | `/api/chat/stream` | JWT | SSE 流式对话（token 打字机 + 过程事件） |
| POST | `/api/conversations` | JWT | 创建会话 |
| GET | `/api/conversations` | JWT | 列出我的会话 |
| GET | `/api/conversations/{id}` | JWT | 会话历史（IDOR 防护） |
| GET | `/api/traces` | JWT | 可观测性（按 user_id 过滤） |
| POST | `/api/token` | — | Demo Token（只发 sub，够不到 admin） |
| GET | `/api/health` | — | 健康检查 |

**SSE 事件协议**

| `event` | 关键字段 | 前端行为 |
|---------|----------|----------|
| `supervisor` | `status=executing` | 正在分析意图 |
| `routing` | `route`, `extract` | 已路由到 xxx |
| `tool` | `name`, `status`, `duration_ms` | 调用工具 xxx |
| `token` | `content` | **逐字追加**（打字机） |
| `agent_result` | `content`, `route` | 未流式过的节点兜底渲染 |
| `done` | `route`, `usage` | 收尾 |
| `error` | `message` | 错误提示 |

**对话能力**（自然语言触发，自动路由）

| 用户说 | 路由 | 返回 |
|--------|------|------|
| 订单1001发货了吗？ | `order_agent` | 订单详情（状态/金额/地址/明细） |
| 我有哪些订单？ | `list_orders_agent` | 当前用户全部订单 |
| 1001快递到哪了？ | `logistics_agent` | 物流轨迹 |
| 你当前有什么商品在售？ | `product_agent` | 在售商品清单（编号/名称/单价/库存） |
| 售后工单1进度 | `after_sale_agent` | 售后状态/原因/时间 |
| 我的购物车有什么？ | `cart_query_agent` | 购物车商品 + 金额 |
| 我要买X1智能门锁，寄到… | `place_order_prepare` → `commit` | 草稿回显 → 确认后建单 |
| 怎么退货？ | `faq_agent` | RAG 检索知识库答案 |
| 你好 | `finish` | 闲聊兜底 |
| 帮我查订单（缺号） | `clarify` | 反问补参，下一轮续跑 |

### 认证 / C 端 / B 端 / Internal

| 分组 | 路径 | 认证 | 说明 |
|------|------|------|------|
| 认证 | `POST /api/shop/auth/register`、`POST /api/shop/auth/login` | — | 注册 / 登录（返回含 role 的 JWT） |
| C 端 | `/api/shop/categories`、`/api/shop/products`、`/api/shop/cart`、`/api/shop/orders`、`/api/shop/orders/{id}/pay`、`/api/shop/orders/{id}/cancel`、`/api/shop/logistics/{order_id}`、`/api/shop/after-sales` | JWT | 完整购物闭环 |
| B 端 | `/api/shop/admin/categories`、`/api/shop/admin/products`、`/api/shop/admin/orders` | admin | 分类 + 商品 + 订单管理 |
| Internal | `/internal/orders`、`/internal/logistics`、`/internal/after-sales`、`/internal/products/*`、`/internal/users/{id}` | X-Internal-Token | AI 侧查询接口 |

---

## 项目结构

```
rag-storage/
├── main.py                          ← FastAPI 入口：lifespan 装配 + 健康检查
├── init.sql                         ← shop schema DDL + 种子数据
├── docker-compose.yml               ← postgres + redis（+ 可选 app）
├── Dockerfile / .dockerignore       ← 应用镜像（含 HEALTHCHECK）
├── pip.bat / run.bat                ← 便携 Python 入口
│
├── apps/
│   ├── customer_service/routes.py   ← AI 客服路由（chat / stream / traces / 会话）
│   └── shop/
│       ├── c_endpoint/              ← C 端: auth / product / order
│       ├── b_endpoint/admin.py      ← B 端: 分类 + 商品 + 订单管理
│       └── internal/routes.py       ← Internal API
│
├── domain/
│   ├── customer_service/            ← 多智能体（★ = 本轮重构核心）
│   │   ├── routing.py               ★ 路由内核（纯函数：白名单/槽位/澄清/走向）
│   │   ├── react.py                 ★ 受限 ReAct 循环 + user_id 注入
│   │   ├── master_graph.py          ★ Supervisor 图 + 全部节点 + MasterState
│   │   ├── langgraph_agent.py       ★ MasterAgent（锁 / 记忆补齐 / 流式 / trace）
│   │   ├── prompts.py               ← 各 Agent prompt + 确定性话术
│   │   ├── tracing.py               ← agent_traces 落库与查询
│   │   └── checkpointer.py          ← 异步 checkpointer 工厂
│   └── shop/                        ← user / product / cart / order / logistics / after_sale
│
├── tools/                           ← Agent 工具层
│   ├── base.py                      ← Tool + ToolParameter（→ StructuredTool）
│   ├── faq_search.py                ← FAQ 混合检索（带缓存）
│   ├── order_search.py              ← 订单/物流/下单/购物车/列表/商品检索
│   └── after_sale_search.py         ← 售后查询
│
├── llm/
│   ├── client.py                    ← ChatService（call_raw / stream_raw 真流式）
│   ├── langchain_model.py           ← BaseChatModel 适配器（_generate / _stream / bind_tools）
│   └── usage.py                     ← Usage 计量（ContextVar 跨线程）
│
├── infrastructure/                  ← DB 连接池 / RAG / Auth / 缓存 / 中间件 / ORM
├── utils/conversation.py            ← 会话 CRUD + 上下文窗口 + KeyedLocks
├── static/index.html                ← 前端 SPA（SSE 逐字渲染）
├── tests/                           ← 离线测试 + 脚本模型 + 假 OpenAI
├── scripts/                         ← 冒烟 / 剧本 / Mock LLM / 端到端 / 路由评测
└── docs/                            ← 项目详细说明 + 重构改动说明 + 工具端审计报告 + FAQ 语料
```

---

## 关键设计

### 单跳路由，条件边永不 KeyError

`supervisor` 只做「分类 + 抽槽 + 上下文恢复」，图走向由纯函数 `resolve_target()` 决定：

```
优先级：下单动作（commit/cancel/prepare） > 槽位是否齐全 > route 映射
任何情况都只返回图中真实节点名 → create_selector 不会再遇到 KeyError
```

路由决策被提炼成纯函数，因此可离线单测（归一化、缺参、否定优先等边界都能一条断言覆盖）。

### 澄清闭环（缺参不断线）

```
"帮我查订单"（缺订单号） → clarify 反问 + pending_intent 存进 state
"1001"                  → 恢复 pending → 正则兜底补单号 → 走 order_agent
```

正则补参必须在「恢复 pending」之后、按最终 route 判断 —— 否则用户只回 `"1001"` 时 LLM 判成
`finish`，永远补不上参数。

### 跨轮上下文与指代消解

用户第二句常常是省略句：「那笔订单」「当前状态」「到哪了」——都不带订单号。链路里两道保障：

1. **Supervisor 带历史**：判意图时把最近几轮一并交给 LLM（`SUPERVISOR_HISTORY_WINDOW`），
   「那笔订单 / 它 / 当前状态」才消解得开；只给本轮原话时这类句子会直接掉到 `finish`。
2. **确定性订单号恢复**：最近订单号持久化在 `state.last_order_id`（下单 commit 即刻回写，
   同时写回 `slots.order_id`）；若本轮 route 掉到 `finish` 而话里含「状态 / 到哪 / 发货」
   等提示词，就提升回 `order` / `logistics` 并补上单号。兜底还会扫历史消息里
   「订单号：xxxx」这类**带前缀**的编号（不认裸数字，避免把地址、金额误当单号）。

没有上下文时**不提升** —— 「你好」不会被误判成查订单。

### 商品名模糊匹配（商品检索三层）

用户极少一字不差地打出商品全名（截图实录：说「X1 门锁」，库里是「X1 智能门锁」）。
`search_products` 因此分三层，逐层放宽：

```
① 整串子串（SQL 粗筛，任一 token 命中） → ② 分词 AND → ③ 字符覆盖率 Python 侧精排
```

排序键 = `(覆盖率, 长度比)` 降序、同分按 id 升序，因此「X1 门锁」的首选是「X1 智能门锁」
而不是更长（但只是整串子串）的「X1 门锁配件包」。无关词（如「洗衣机」）覆盖率不足阈值，
仍返回空 —— 放宽不等于乱匹配。阈值 `PRODUCT_MATCH_COVERAGE_MIN`（默认 0.6）可调。

### FAQ 多路召回（「关键词堆」兜底）

RAG 检索对「空格分隔的关键词堆」语义表达很差。截图实录：LLM 把「它的电池能续航多久」
写成 `X1 智能门锁 电池 续航`，主体词占满语义权重、诉求被稀释，第一条命中「X1 可以同时被
几台手机控制？」，正确答案「电池能用多久？」掉到第三 —— 再被「按来源去重」（每商品保留
1 条）挤掉，于是 LLM 只能答「没有相关资料」。

对策分两层（软引导 + 硬兜底）：

```
① prompt 软引导：FAQ_AGENT_PROMPT / QUERY_DISCIPLINE 要求「用自然语言问句检索，
   不要把词用空格拼成关键词串；一次不中就换更聚焦的短问句重试」
② 工具层兜底：search_faq 走 RAGStore.search_multi —— 把 query 派生为聚焦变体
   （尾部两段 / 尾段 / 分句 / 去空白紧凑版）各召回一次，再按「共识」融合
```

融合用**归一化分数均值**：每个变体先按自身最高分归一（消除量纲差异），同一 chunk 被越多
变体共同召回就越靠前；融合分除以变体数落回 `[0,1]`，可直接当相关度展示，最后仍走按来源
去重。关键点：

- **自然问句零额外开销**：变体数为 1 时 `search_multi` 直接委托单路 `search`（同缓存、同结果）。
- 只在疑似关键词堆 / 多分句时才扩展（最多 5 路）。
- 融合发生在**去重之前**（`_candidates` 与 `_search_impl` 拆分）—— 否则正确答案还没参与排序
  就被按来源去重丢掉。

实测（真实 FAQ 语料 81 chunks）：关键词堆问句命中 11/16 → **13/16**，其中「X1 智能门锁 电池
续航」榜首从错误的「手机控制」变为「电池能用多久」。回归由 T25（纯函数）/ T26（真实库）守。

### 下单两阶段（human-in-the-loop）

```
prepare : 确定性检索商品与库存（不经 LLM）+ 生成草稿回显 —— 不写库
commit  : 用户明确确认 → 单品走 create_direct() / 整单走 OrderService.create()，单事务落库
cancel  : 用户否定 → 丢弃草稿（话题漂移也丢弃，避免挂过期确认）
```

金额与库存**不经 LLM 计算**；`is_affirmative` **否定优先**（中文「不要」包含「要」，
只做包含匹配会误判）。

**两种下单模式**（`order_mode`，由纯函数 `resolve_order_mode` 判定，只看**本轮**抽到的
`product` 槽位）：

| 模式 | 触发 | 商品来源 | 落库 |
|------|------|----------|------|
| `single` | 点名了商品（「我要买 X1 智能门锁」） | `search_products` 模糊匹配 | `create_direct`（不碰购物车） |
| `cart` | 没点名商品（「帮我下单」「结算购物车」） | 用户购物车里的全部商品 | `OrderService.create`（锁购物车行 → 建单 → 清空购物车） |

为什么需要 `cart`：旧版只有单品链路，购物车里放了 3 件却说「帮我下单」时，要么被反问
「您想买哪件商品呢」，要么按猜到的某一件只下 1 件、其余商品被静默忽略。`cart` 模式下
`product` 不再参与必填校验（只问地址），避免在 clarify 阶段被反问商品名。

确认期间购物车若被改动（前端删了商品 / 改了数量），commit 会**先比对再落库**，不一致就
取消本次结算并提示重新确认 —— 保证「和用户确认的」与「实际下单的」一致。

回归由 T27 守（纯函数 + supervisor 节点 + 图级整单）。注意：图的三个下单节点以
`search_products` 是否在场为装配开关，`cart` 模式虽然不用它，裁剪工具集时也会一起被关掉。

### 工具鉴权主体服务端注入

ReAct 只把 LLM 给的 args 透传给工具，`user_id` 由服务端注入且**只注入工具声明的参数名**：

```python
declared = {p.name for p in tool.parameters if p.name in context}
return {**args, **{k: context[k] for k in declared}}   # 服务端值覆盖 LLM 值
```

鉴权主体永远来自 JWT，LLM 无法伪造越权身份；也不会污染不需要 `user_id` 的工具（如 `search_faq`）。

工具调用**必须带超时**（默认 8s），否则一次卡住的 DB 查询会永久占用工作线程、工具层逐渐饿死：

```python
obs = await asyncio.wait_for(
    asyncio.to_thread(tool.run, call_args), timeout=TOOL_TIMEOUT_SEC)
```

### 身份解析与 demo token（安全要点）

- **身份解析必须精确匹配**：`_resolve_user_id` 只接受数字 id，或与 `email` / `nickname`
  **完全相等**的值。禁止 `ILIKE '<id>%'` 前缀匹配 —— 那等于「谁先命中谁就是你」（身份混淆）。
- **`/api/token` 是 demo 入口**：它无凭证签发任意 `user_id` 的 JWT，会**绕过全部 IDOR 校验**
  （攻击者是「变成受害者」而非「越权访问」）。由 `ALLOW_DEMO_TOKEN` 控制，
  `APP_ENV=production` **强制关闭**；关闭时返回 404。
- **启动体检**：`JWT_SECRET` / `INTERNAL_API_TOKEN` 若仍是默认值，非生产告警、
  **production 拒绝启动**。
- **内部接口**用 `hmac.compare_digest` 做时序安全比较，生产环境仍是默认 token 则 503。

### 下单事务（FOR UPDATE 防超卖）

```
BEGIN
  SELECT ... FROM shop.cart_items ... FOR UPDATE OF ci        ← 锁购物车行（防重复结算）
  SELECT ... FROM shop.products WHERE id IN (...) FOR UPDATE  ← 行级锁
  逐条校验 stock >= quantity                                   ← 不足 → ROLLBACK
  UPDATE shop.products SET stock = stock - quantity
  INSERT INTO shop.orders (status='pending', recipient, ...)
  INSERT INTO shop.order_items (快照)
  DELETE FROM shop.cart_items
COMMIT
```

> 购物车行锁是后加的：没有它，同一用户并发结算会各读到同一批 cart_items，
> **各建一单、各扣一次库存**（测试 T18 用负向对照验证过：无锁时建出两单、库存 97→95）。

### 支付幂等

```
BEGIN
  SELECT ... FROM shop.orders WHERE id = ... FOR UPDATE
  校验 status = 'pending' AND user_id = 当前用户
  UPDATE shop.orders SET status = 'paid'
  INSERT INTO shop.payment_records
  INSERT INTO shop.logistics_records（自动生成物流）
COMMIT
```

### Redis Cache-Aside

```
读路径：  cache.get(key) ──命中──→ 直接返回
                        └─未命中─→ 查源（DB / RAG）→ cache.set(key, ttl)

命名空间  rag:faq:*   FAQ 混合检索结果，key = hash(query, top_k, product)，TTL 600s
          rag:shop:*  商品详情 / 列表 / 分类树，TTL 300~600s
失效      知识库 re-ingest → invalidate("faq")    下单 / 取消（库存变化）→ invalidate("shop")
```

**降级设计（关键）**：Redis 不可用时**熔断 30 秒**，期间缓存操作全部 no-op、直接查源头 ——
请求既不变慢也不报错。Redis 是纯优化，**不是硬依赖**。

### 并发与竞态

| 场景 | 手段 |
|------|------|
| 同会话并发请求 | per-conversation `asyncio.Lock`，串行化整轮（图状态按 thread_id 存，并发写会互相覆盖） |
| 「用户消息 + 助手消息」成对写入 | 锁范围**覆盖成对写入**（只锁单条 INSERT 不够） |
| `turn_number` 竞态 | `KeyedLocks` 按 conversation_id 互斥，锁内计算序号 |
| 同步 I/O 阻塞事件循环 | **所有**同步调用走 `asyncio.to_thread`：工具 + 会话管理（create/get_context/add_message）+ trace 落库。新增代码若在 async 路径里直接调同步函数，就是 bug |
| 工具/查询失控 | 工具层 `TOOL_TIMEOUT_SEC` 超时 + 连接池 `statement_timeout` 双保险 |
| 购物车重复结算 | `SELECT ... FROM shop.cart_items ... FOR UPDATE OF ci` |
| 连接池初始化 | 双检加锁；池上限**故意**小于线程池上限，让池耗尽成为背压 |

> 排查提示：把同步路由误改成调用 async 函数却**忘写 `await`**，不会报错 ——
> 协程被创建但从不执行，表现为「该校验静默失效」。端到端场景 6 抓到过一次。

### 数据隔离

- C 端：`user_id` 从 JWT 提取，所有查询带 `WHERE user_id = %s`
- 工具层：`user_id` 由服务端注入（见上）
- B 端：`role = admin` 可查看全部订单
- Internal：`X-Internal-Token` 认证
- trace：`/api/traces` 强制按 `user_id` 过滤

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API 密钥 | — |
| `LLM_BASE_URL` | LLM 接口地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名称 | `deepseek-chat` |
| `CONVERSATION_DB_URL` | 会话库 | `postgresql://user:1234@localhost:5432/agent` |
| `SHOP_DB_URL` | 电商库（可分离） | 同上 |
| `CONVERSATION_MAX_CONTEXT_TURNS` | 上下文窗口轮数 | `5` |
| `APP_ENV` | 运行环境；`production` 强制关闭 demo token 并对默认密钥拒绝启动 | `development` |
| `ALLOW_DEMO_TOKEN` | `/api/token`（无凭证签发任意身份）总开关 | 非 production 为 `1` |
| `JWT_SECRET` / `JWT_ALGORITHM` / `JWT_EXPIRE_MINUTES` | JWT | `demo-secret-change-in-production` / `HS256` / `60` |
| `INTERNAL_API_TOKEN` | Internal API 认证 | `dev-internal-token` |
| `SHOP_DB_POOL_MIN` / `SHOP_DB_POOL_MAX` | 电商库连接池容量 | `2` / `10` |
| `SHOP_DB_STATEMENT_TIMEOUT_MS` | 单条 SQL 超时（掐断慢查询） | `5000` |
| `SHOP_DB_IDLE_TX_TIMEOUT_MS` | 空闲事务回收（防连接泄漏） | `30000` |
| `TOOL_TIMEOUT_SEC` | 单次工具调用对外超时 | `8` |
| `CHECKPOINT_DB_PATH` | 图持久化路径 | `./checkpoints.db` |
| `AGENT_MAX_STEPS` | ReAct 步数上限 | `4` |
| `AGENT_HISTORY_WINDOW` | 子 Agent 历史窗口 | `12` |
| `ORDER_TIMEOUT_MINUTES` / `ORDER_SWEEP_INTERVAL_SEC` | 超时订单 | `30` / `300` |
| `EMBEDDING_MODEL` / `CHROMA_DB_PATH` / `COLLECTION_NAME` | RAG | `paraphrase-multilingual-MiniLM-L12-v2` / `./chroma_db` / `faq_knowledge` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 文本切片 | `500` / `50` |
| `REDIS_URL` / `CACHE_ENABLED` / `CACHE_TTL_FAQ` / `CACHE_TTL_PRODUCT` / `CACHE_TTL_CATEGORY` | 缓存 | `redis://localhost:6379/0` / `1` / `600` / `300` / `600` |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | HuggingFace 离线 | `1` |

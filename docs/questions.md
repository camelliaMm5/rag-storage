# 关于项目整体与架构：

## 1. 为什么选择 DDD 分层而不是 MVC？

**答**：MVC 的 M（Model）通常直接耦合 ORM 或数据库，Controller 里容易堆积业务逻辑。DDD 的核心区别在于**依赖倒置**——领域层定义接口，基础设施层提供实现，应用层只依赖接口。

这个项目的核心说服点是"Demo 验证通过后快速产品化"。DDD 分层天然支撑这一点：`OrderRepository` 接口在 `domain/order/repository.py` 定义，当前 Demo 阶段用 `MockOrderRepository`（3 条硬编码数据）。后期接入真实电商 API 时，新建一个 `RealOrderRepository` 实现同一接口，`main.py` 改一行注入，上层 `OrderApplicationService`、Agent 工具层**零改动**。

如果 MVC，Mock 数据和真实 API 的切换需要改 Controller 里的数据获取代码，牵一发动全身。

**举例**（[domain/order/repository.py](domain/order/repository.py)）：
```python
class OrderRepository(ABC):
    @abstractmethod
    def find_order(self, order_id: str) -> Optional[Order]: ...
    @abstractmethod
    def find_logistics(self, order_id: str) -> Optional[Logistics]: ...
```

`MockOrderRepository` 和未来的 `RealOrderRepository` 都实现这两个方法，上层 `query_order_tool` 不关心数据来源。

---

## 2. DDD 中 domain 层不依赖任何框架，具体怎么做到的？带来了什么好处？

**答**：domain 层只用 Python 标准库——`dataclass` 定义实体、`ABC`/`abstractmethod` 定义接口。不 import FastAPI、LangChain、SQLAlchemy。

**具体做法**：
- 实体用 `@dataclass(frozen=True)` 定义不可变值对象（[domain/order/entity.py](domain/order/entity.py)）：`Order`、`Logistics`、`LogisticsTrace` 都是纯数据结构。
- 仓储接口用 `ABC` + `@abstractmethod` 定义契约（[domain/order/repository.py](domain/order/repository.py)）：只声明方法签名，不引入任何 ORM 或数据库依赖。

**好处**：
1. **框架无关**：未来从 FastAPI 迁移到其他框架（如 Litestar），domain 层零改动。
2. **测试友好**：单元测试可以直接 import domain 层的实体和接口，不需要启动任何服务。
3. **边界清晰**：新成员看代码从 domain 开始，能快速理解"系统做什么"而不被技术细节干扰。

---

## 3. 仓储接口在 Demo 阶段只有 mock 实现，后期对接真实 API 需要改哪些代码？

**答**：只改两个地方：

**第一步**：新建 `infrastructure/real_order_repository.py`，实现 `OrderRepository` 接口，内部调用外部电商系统 API（HTTP 请求 + 数据格式转换）。

```python
class RealOrderRepository(OrderRepository):
    def find_order(self, order_id: str) -> Optional[Order]:
        resp = requests.get(f"{API_BASE}/orders/{order_id}")
        return Order(**resp.json()) if resp.ok else None
    ...
```

**第二步**：`main.py` 改一行注入代码：
```python
# 改前
from infrastructure.order_repository import MockOrderRepository as OrderRepoImpl
# 改后
from infrastructure.real_order_repository import RealOrderRepository as OrderRepoImpl
```

**上层零改动**：`OrderApplicationService` → `query_order_tool` → Agent → API 路由，整条链路只依赖 `OrderRepository` 接口。

---

## 4. 如果 FAQ 知识库从 10 篇涨到 10 万篇，现有方案哪些地方会出问题？

**答**：

1. **ChromaDB 性能瓶颈**：ChromaDB 是嵌入式向量库，10 万篇文档假设每篇拆 5 个 chunk = 50 万条向量。ChromaDB 的 HNSW 索引在这个规模下检索延迟会显著上升（从 <10ms 到 100ms+），且内存占用大。**方案**：迁移到 Milvus 或 pgvector + IVFFlat/HNSW 索引。

2. **BM25 索引内存膨胀**：自研 BM25 索引是纯内存结构（`self.docs`、`self.tf`、`self.df` 全在内存），50 万条 chunk 的倒排索引内存可能到 1-2GB。**方案**：换用 Elasticsearch 的内置 BM25，或把 BM25 索引落盘（SQLite FTS5）。

3. **Embedding 批量处理**：当前 `embed()` 一次编码全量文本，10 万篇的全量 re-index 会非常慢。**方案**：增量索引 + 后台队列，只对新文档做 Embedding。

4. **全量 re-index 耗时**：当前 `ingest_dir()` 是同步阻塞的，大量文档导入时 FastAPI 的 event loop 会被阻塞。**方案**：改为后台任务（FastAPI BackgroundTasks 或 Celery）。

5. **去重逻辑**：当前 `_dedup_by_source` 只在 Top-K 结果内去重，文档量大了之后同一产品可能有几十个 chunk 命中，需要更精细的分组聚合。

---

## 5. 这个项目从 Demo 到生产，你会在哪些环节做改造？优先级是什么？

**答**：按紧急度和影响面排序：

| 优先级 | 改造项 | 具体做法 |
|--------|--------|----------|
| P0 | **LLM 容错** | tenacity 库加重试（指数退避）、主备模型 fallback、请求超时 30s → 可配置 |
| P0 | **安全加固** | CORS 从 `*` 改为具体域名、输入清洗防 prompt injection、API 鉴权（JWT） |
| P1 | **会话持久化** | MemorySaver → PostgresSaver 或 SqliteSaver，服务重启不丢 Graph 状态 |
| P1 | **向量库升级** | ChromaDB → pgvector（直接用已有 PG 基础设施，减少组件数） |
| P1 | **可观测性** | 接入 LangFuse/LangSmith 追踪 Agent 各节点耗时、token 消耗、tool call 成功率 |
| P2 | **容器化** | Docker Compose：FastAPI + PostgreSQL/pgvector + Nginx |
| P2 | **日志系统** | structlog 结构化日志 + 请求级别 trace_id 贯穿全链路 |
| P3 | **降级策略** | LLM 不可用时切规则引擎兜底（关键词匹配 + 固定回复模板） |

---

# 关于LLM/Agent：

## 6. LangGraph 的 llm_node → tools_node 循环是怎么工作的？什么时候退出循环？

**答**：这是一个标准的 **ReAct 循环**，代码在 [domain/customer_service/graph.py](domain/customer_service/graph.py)。

**图结构**：
```
START → agent(llm_node) → [条件判断] → tools(tools_node) → agent(llm_node) → ...
                              ↓
                           END（退出）
```

**执行流程**：

1. **agent 节点**：`create_agent_node` 工厂函数生成。首次调用时在消息列表头插入 SystemMessage，然后 `llm.invoke(messages)`。LLM 返回 `AIMessage`，其中 `tool_calls` 字段可能为空（直接回复）或包含工具调用请求。

2. **条件判断**（`should_continue` 函数，[graph.py:38-50](domain/customer_service/graph.py#L38-L50)）：
   ```python
   def should_continue(state: AgentState) -> str:
       if state.get("step_count", 0) >= state.get("max_steps", 5):
           return END  # 条件1: 达到最大步数
       last = messages[-1]
       if hasattr(last, "tool_calls") and last.tool_calls:
           return "tools"  # 条件2: LLM 想调工具
       return END  # 条件3: LLM 直接回复，无工具调用
   ```

3. **tools 节点**：LangGraph 内置 `ToolNode` 执行工具调用，结果包装成 `ToolMessage` 追加到消息列表，然后回到 agent 节点。

4. **终止条件**：
   - LLM 判断不需要工具（直接回复），条件判断返回 `END`。
   - 达到 `max_steps=5`，强制退出防死循环。

**关键设计**：`add_messages` reducer 自动合并同类型消息（追加而非覆盖），确保 agent 看到完整的对话+工具调用历史。

---

## 7. LangGraph 和 LangChain 的 AgentExecutor 有什么区别？为什么选 LangGraph？

**答**：

| 维度 | AgentExecutor (LangChain) | LangGraph |
|------|--------------------------|-----------|
| **控制流** | 隐式循环（内部 while 循环），定制困难 | 显式状态图，节点和边可精确控制 |
| **可观测性** | 黑盒执行，中间状态不可见 | `astream()` 原生支持节点级流式输出 |
| **中断/恢复** | 不支持 | Checkpointer 支持暂停、恢复、分支回退 |
| **多 Agent** | 需手写编排逻辑 | `StateGraph` 天然支持多节点路由 |
| **生产就绪** | 更适合原型 | 支持 persistence、streaming、human-in-the-loop |

**选择 LangGraph 的原因**：

1. **流式输出需求**：Demo 需要前端实时显示"正在分析意图→FAQ Agent 检索中→结果"，LangGraph 的 `astream()` 直接满足，AgentExecutor 做不到。
2. **演进路径**：从单 Agent（langgraph_agent.py）演进到 Supervisor 多 Agent（master_graph.py），LangGraph 的 `StateGraph` 只需加节点和条件边，不需要推倒重来。
3. **Checkpointer**：`MemorySaver` 开箱即用，后续切换到 `PostgresSaver` 只需改一行初始化代码，Graph 定义不变。

---

## 8. LLM 是怎么决定「该调用哪个工具」的？底层原理是什么？

**答**：通过 **OpenAI Function Calling** 协议（现在叫 Tool Use）。

**底层机制**：

1. **工具定义转 JSON Schema**：`Tool.to_langchain_tool()` 用 Pydantic `create_model` 动态构建 args_schema，LangChain 的 `bind_tools()` 将每个工具转成 OpenAI Function Calling 格式的 JSON：
   ```json
   {
     "type": "function",
     "function": {
       "name": "search_faq",
       "description": "检索FAQ知识库...",
       "parameters": {
         "type": "object",
         "properties": {
           "query": {"type": "string", "description": "检索关键词或用户问题"}
         },
         "required": ["query"]
       }
     }
   }
   ```

2. **注入到 API 请求**：`bind_tools()` 后，每次 `llm.invoke(messages)` 时，工具 schema 作为 `tools` 参数随请求发送给 LLM。

3. **LLM 端决策**：模型在训练时已经学过 Function Calling 能力。它根据 system prompt（工具的使用场景描述）+ 用户消息 + tool schema（name + description + parameters），预测最合适的 `tool_calls`。

4. **返回格式**：LLM 返回的 `AIMessage` 中 `tool_calls` 字段包含工具名和参数 JSON，框架侧直接执行。

**核心影响因素**：
- **工具描述质量**：`search_faq` 的描述里写了"当用户询问退换货政策、保修、使用说明等知识类问题时"——这直接告诉 LLM 什么场景该选这个工具。
- **参数描述**：`query` 描述为"检索关键词或用户问题"——LLM 据此决定从用户消息中提取什么内容填入参数。

---

## 9. 如果 LLM 选错了工具（比如用户问物流，LLM 调了订单查询），你会怎么处理？

**答**：这个项目有两层防御机制：

**第一层 — Supervisor 路由**（当前主力方案，[master_graph.py:21-35](domain/customer_service/master_graph.py#L21-L35)）：先让 Supervisor 做意图分类输出 JSON `{"route": "logistics", "extract": "xxx"}`，再分发给对应子 Agent。每个子 Agent 只绑 1 个工具，**根本不存在选错工具的可能**——只要 Supervisor 路由正确即可。而 Supervisor 是专门为路由任务设计的，准确率远高于让一个通用 Agent 从多个工具中选。

**第二层 — 单 Agent 兜底**（Legacy 方案，[graph.py:38-50](domain/customer_service/graph.py#L38-L50)）：如果不用 Supervisor、走单 Agent 多工具的方案：
1. 选错工具→工具返回空结果（如 `query_order` 搜不到用户给的物流单号）→结果回传 LLM → LLM 看到空结果后会尝试换工具。
2. `max_steps=5` 限制循环次数，即便 LLM 一直选错也不会死循环。
3. 最终 fallback：Agent 返回"抱歉，我暂时无法完成您的请求，请再试一次。"

**实际发生过的例子**：早期测试时用户说"我的快递到哪了"，LLM 没提取订单号就调了 `query_order`，返回"未找到订单"。LLM 在下一步中改问"请提供订单号"，走 AskUser 流程。

---

## 10. 工具的描述（docstring）对 Agent 行为有什么影响？如果多写或少写会发生什么？

**答**：**影响巨大**——工具描述是 LLM 判断"什么时候该用这个工具"的唯一依据。

**多写的影响**：
- 描述过于宽泛（如 `search_faq` 描述写"用于搜索信息"），LLM 会把所有问题都路由到 FAQ 检索，订单查询和物流查询永远不会被调用。
- 描述之间重叠（如 `query_order` 和 `query_logistics` 都写"查询订单相关信息"），LLM 在两个工具间随机选择，准确率对半。

**少写的影响**：
- 描述过于简略（如只写"查询"），LLM 不知道这个工具能做什么，可能完全不调用。
- 缺少使用场景提示（如 `query_logistics` 没写"用户询问快递/物流进度时"），LLM 只在显式提到"物流"二字时才调用，遗漏同义表述（"到哪了""快递""运输"）。

**这个项目的做法**（[tools/order_search.py:46-49](tools/order_search.py#L46-L49)）：
```python
query_order_tool = Tool(
    name="query_order",
    description="根据订单号查询订单详情（状态、商品、金额、收件人、地址）。"
                "用户提供订单号时调用此工具。",
    ...
)
```

描述包含两部分：**功能说明**（做什么）+ **触发场景**（什么时候用）。功能说明帮 LLM 理解工具能力，触发场景帮 LLM 做意图匹配。

**反面案例**：早期 `search_faq` 的描述只写了"检索FAQ知识库"，LLM 在用户问"我的订单发货了吗"时也调了 FAQ 检索（因为"发货"在 FAQ 里有）。后来加上"当用户询问退换货政策、保修、使用说明等**知识类问题**时调用"，才把订单/物流类问题排除出去。

---

## 11. 系统 Prompt 你是怎么设计的？放了哪些内容？为什么？

**答**：两级 Prompt 体系：

**第一级 — Supervisor Prompt**（[master_graph.py:21-35](domain/customer_service/master_graph.py#L21-L35)）：
```
你是智能客服的路由调度器。根据用户意图判断，输出 JSON。
路由规则：
- 退货/保修/故障/参数 → faq
- 提供订单号询问状态 → order
- 快递/物流 → logistics
- 打招呼/闲聊 → finish
输出格式：{"route": "...", "extract": "关键参数"}
```

**设计原则**：
- **只做路由，不回答问题**：职责单一，prompt 短（~200 tokens），LLM 分类准确率高。
- **结构化输出**：要求输出 JSON，程序直接解析，不走正则。
- **关键参数提取**：`extract` 字段让 Supervisor 顺便提取订单号/搜索词，子 Agent 拿到直接执行，不再二次解析。

**第二级 — 子 Agent Prompt**（[prompts.py:46-66](domain/customer_service/prompts.py#L46-L66)）：
```
你是智能家居品牌 AI 客服。工具：
- search_faq：检索 FAQ（退货/保修/故障排查/产品参数）
- query_order：查询订单详情
- query_logistics：查询物流轨迹

回答规则：
- 综合工具返回信息，自然友好回答
- 信息不足时反问澄清
- 只基于工具内容回答，不编造
- 答案简洁，步骤分条
```

**设计原则**：
- **工具场景绑定**：每个工具后面标注了适用场景，强化 LLM 的意图-工具映射。
- **约束前置**："不编造""信息不足反问"——减少幻觉，提升用户体验。
- **输出风格控制**："自然友好""简洁分条"——统一回复风格，避免 LLM 自由发挥。

**为什么不用一个长 Prompt 包办所有**：单 Prompt 既要路由又要回答还要选工具，职责混杂，LLM 容易在意图判断阶段就分心。拆分后每个 Prompt < 300 tokens，准确率和响应速度都更好。

---

## 12. MemorySaver 是内存存储，服务重启后会话丢失。如果升级到 PostgresSaver，需要改哪些地方？

**答**：改动极小，因为 Checkpointer 在 Graph 编译时注入，与 Graph 定义解耦。

**只改一处**（[langgraph_agent.py:29](domain/customer_service/langgraph_agent.py#L29)）：
```python
# 改前
from langgraph.checkpoint.memory import MemorySaver
self.checkpointer = checkpointer or MemorySaver()

# 改后
from langgraph.checkpoint.postgres import PostgresSaver
self.checkpointer = checkpointer or PostgresSaver(conn_string=DATABASE_URL)
```

**Graph 定义零改动**：`build_graph()` 和 `build_master_graph()` 的 `checkpointer` 参数类型是协议（`BaseCheckpointSaver`），不依赖具体实现。

**另一层保护**：这个项目的会话管理（对话历史 CRUD）用的是 SQLAlchemy + SQLite（[conversation.py](utils/conversation.py)），已经持久化到 `conversations.db`。MemorySaver 只存 LangGraph 内部的 state（消息列表的增量状态），即使丢失也不影响对话历史查询。升级到 PostgresSaver 后，两个层都用 PG，组件统一。

---

## 13. 多轮对话的上下文是怎么管理的？消息历史会一直增长吗，有没有截断策略？

**答**：两层管理 + 截断：

**LangGraph 层**：`MemorySaver` 通过 `thread_id` 持久化完整消息历史。Graph 的 `add_messages` reducer 自动追加新消息，**不做截断**——这部分理论上会无限增长。

**ConversationManager 层**（[conversation.py:64-80](utils/conversation.py#L64-L80)）：构造 prompt 时只取最近 `max_context_turns=10` 轮对话（20 条消息）：
```python
def get_context(self, conversation_id: str) -> list[dict]:
    limit = self.max_context_turns * 2  # 10轮 × 每轮2条(用户+助手)
    rows = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(desc(Message.turn_number), desc(Message.created_at))
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [{"role": r.role, "content": r.content, ...} for r in rows]
```

**Token 测算**：10 轮对话 ≈ 2000-3000 tokens + system prompt 200 tokens + 工具定义 300 tokens ≈ 3000 tokens，在 gpt-4o-mini 的 128K 窗口内安全。

**为什么截断 10 轮**：客服场景的对话通常短平快（用户问→Agent 答→结束），10 轮覆盖绝大多数场景。超过 10 轮的老消息通常是已解决的问题，保留没有增量价值反而稀释 LLM 注意力。

---

# 向量检索/pgvector相关的：

> 注：实际项目使用 **ChromaDB + Sentence-Transformers 本地 Embedding**，非 pgvector + API Embedding。以下回答以实际实现为准，同时对比原始设计。

## 14. 为什么选 ChromaDB 而不是 pgvector 或 Elasticsearch 或 Milvus？

**答**：实际实现选了 ChromaDB，三个考量：

1. **Demo 阶段零运维**：`pip install chromadb` 即用，数据持久化到 `./chroma_db` 目录，无需单独部署数据库服务。pgvector 需要 PostgreSQL 15+ 安装 pgvector 扩展，Elasticsearch 需要 JVM + 集群配置，对 Demo 来说过重。

2. **HNSW 索引 + cosine 距离足够**：[store.py:25-29](infrastructure/rag/store.py#L25-L29) 创建 collection 时配置 `hnsw:space: cosine`，在万级 chunk 规模下检索延迟 < 10ms，精度和速度都满足需求。

3. **metadata 过滤**：`collection.query(where={"product_code": "X1"})` 支持按产品编码硬过滤，这在多产品 FAQ 场景下很重要——用户提到具体型号时直接限定检索范围。

**如果数据量上到 10 万+ 文档**：迁移到 pgvector（利用已有 PG 基础设施，减少额外组件）或 Milvus（分布式向量检索）。

| 方案 | 适合场景 | 不适合 |
|------|----------|--------|
| ChromaDB | Demo/原型/小规模 | 生产大规模 |
| pgvector | 已有 PG 的中等规模 | 无 PG 基础设施的轻量项目 |
| Milvus | 百万+ 向量，分布式 | Demo 阶段过重 |
| Elasticsearch | 全文+向量混合检索 | 纯向量检索场景性价比低 |

---

## 15. cosine distance 的原理是什么？和欧氏距离、内积有什么适用差异？

**答**：

**cosine distance = 1 - cosine similarity**。公式：`cos(θ) = (A·B) / (|A| × |B|)`。

度量的是**方向**差异，不是绝对距离。两个向量指向同一方向时 cosine similarity = 1（distance = 0），方向相反时 similarity = -1（distance = 2）。

**与欧氏距离的关键区别**：

| 度量 | 关注点 | 适用场景 |
|------|--------|----------|
| Cosine | 方向（语义相似度） | 文本 Embedding 检索 |
| 欧氏距离 | 绝对值（空间距离） | 图像特征、坐标 |
| 内积 | 方向 + 模长 | 需考虑"信息量"的场景 |

**为什么文本检索用 cosine**：
- "如何重置设备"和"设备怎么重置"语义相同，但词数不同会导致向量模长不同。如果算欧氏距离，模长差异会造成假阴性。
- Embedding 模型输出归一化后（`normalize_embeddings=True`，[embedding.py:17](infrastructure/rag/embedding.py#L17)），cosine 等价于内积，计算更简单。

**实际配置**：[store.py:27-29](infrastructure/rag/store.py#L27-L29) `hnsw:space: cosine` 和 ChromaDB 返回的 `1.0 - distance` 转为 similarity score。

---

## 16. 你设置了相似度阈值过滤，这个阈值是怎么确定的？太高或太低分别会有什么问题？

**答**：这个项目没有硬设一个固定阈值，而是用了两种软策略：

**策略 1 — Recall K + Rerank**（[retriever.py:219-241](infrastructure/rag/retriever.py#L219-L241)）：先召回 `recall_k=20` 条候选，经过混合重排序（0.6 vector + 0.4 BM25）后取 Top-K。低分结果自然沉底，而不是被阈值一刀切。

**策略 2 — 空结果通知**（[tools/faq_search.py:14-15](tools/faq_search.py#L14-L15)）：如果检索结果为空（ChromaDB 返回 0 条），返回"未在FAQ知识库中找到相关内容。"Agent 据此告知用户或走反问流程。

**如果设硬阈值**：
- **太高（如 0.9）**：过滤掉有效但表述不同的结果。"设备怎么重启"和"重启设备方法"的 cosine 相似度可能在 0.8 左右，阈值设 0.9 会漏掉正确答案。
- **太低（如 0.3）**：噪音混入，无关文档排在前面，Agent 基于错误信息回答，产生幻觉。

**生产环境做法**：用标注数据跑一轮评估，画 precision-recall 曲线，选 F1 最高点对应的阈值。text-embedding-ada-002 的经验阈值在 0.75-0.8，MiniLM 在 0.6-0.7（模型语义区分度不同）。

---

## 17. Embedding 模型选的是哪个？维度是多少？为什么选这个？

**答**：实际使用 **`paraphrase-multilingual-MiniLM-L12-v2`**，Sentence-Transformers 库加载本地运行。

| 属性 | 值 |
|------|-----|
| 模型 | paraphrase-multilingual-MiniLM-L12-v2 |
| 维度 | **384** |
| 归一化 | 是（`normalize_embeddings=True`） |
| 推理方式 | CPU 本地（`sentence_transformers` 库） |
| 单次编码耗时 | < 20ms |

**选择理由**：

1. **本地部署，零成本、零延迟**：Demo 阶段避免 Embedding API 的网络延迟和调用费用。后续可按需切换 API 模型（改 [embedding.py:10](infrastructure/rag/embedding.py#L10) 一行代码）。

2. **多语言**：模型名称中的 `multilingual` 表示支持 50+ 语言，中文 FAQ 中混有英文术语（如 "WiFi"、"2.4GHz"、"Type-C"）也能正确处理。

3. **384 维够用**：MiniLM 在 MTEB 中文检索基准上的表现与 768 维模型差距不大，但存储和计算量减半。对 Demo 规模完全足够。

**与 API Embedding 对比**：
- `text-embedding-ada-002`：1536 维，语义区分度更好，但有网络延迟（~100ms）和费用。
- `text-embedding-3-small`：512 维，性价比高的 API 方案，但同样有网络依赖。

**后续切换**：只需改 `config.py` 的 `EMBEDDING_MODEL` 和环境变量，上层检索代码零改动。

---

## 18. FAQ 数据更新了怎么办？需要重新 Embedding 吗？流程是怎样的？

**答**：当前是**全量 re-index** 策略，每次启动时调用 `ingest_dir()`：

**流程**（[retriever.py:204-209](infrastructure/rag/retriever.py#L204-L209) + `_index` 方法）：
```
1. 读取 docs/ 目录下所有 .md 文件
2. 对每个文件: delete_by_doc_id(旧数据) → split(新数据) → embed(文本) → add(向量+元数据)
3. 同步重建 BM25 索引
```

**delete_by_doc_id 的作用**（[store.py:52-59](infrastructure/rag/store.py#L52-L59)）：通过 `doc_id` 找到旧 chunk，先删再写，避免重复。

**问题**：全量处理的效率不高，每次启动都要重新 Embedding 所有文档。

**改进方案**（生产环境）：
1. **增量索引**：监听文件变更（watchdog 库），只处理新增/修改的文档。
2. **文件哈希比对**：存 `{doc_id: md5_hash}`，启动时对比哈希，只 re-index 变化的文件。
3. **后台队列**：新增文档放队列，异步 Embedding，不阻塞 API 服务。

---

## 19. 如果 ChromaDB 检索结果的相关性不够好，你会从哪些方向优化？

**答**：按成本从低到高排列优化方向：

**1. 查问题侧优化**（成本最低）：
- **Query Rewriting**：已有实现（[llm/client.py:74-82](llm/client.py#L74-L82) `rewrite_query`），用 LLM 把口语化问题改写为规范化检索词。"我买的那个东西坏了咋办"→"产品故障 售后处理"。
- **HyDE（假设性文档嵌入）**：让 LLM 先生成一段假设性答案，用假设答案做 Embedding 检索，而非直接用用户问题。效果通常比直接检索好 5-10%。

**2. 文档侧优化**（成本中等）：
- **Chunk 策略调参**：当前 `chunk_size=350, chunk_overlap=50`，可以实验不同组合（如 256/64、512/100），跑评估脚本找最优。
- **元数据增强**：FAQ 文档手动标注 product、category、intent 等字段，检索时做 metadata 过滤和加权。
- **多级 chunk**：短 chunk（检索用）+ 长 chunk（上下文扩展用），检索时用短 chunk 匹配，返回时拼接前后上下文。

**3. 检索侧优化**（成本较高）：
- **混合权重调参**：当前 0.6 vector + 0.4 BM25 是经验值，可以用标注数据 grid search 找最优权重。
- **Rerank 模型**：在混合检索结果之后，再加一层 Cross-Encoder Reranker（如 `bge-reranker-base`），对 Top-20 做精细排序。语义区分度远高于 cosine 距离。
- **Fine-tune Embedding**：收集真实 FAQ 的 query-doc 配对数据，fine-tune Embedding 模型，让它更适应电商客服领域。

---

## 20. 为什么是 384 维？Embedding 模型输出是多少维？表定义和实际模型不匹配会怎样？

**答**：

**原始需求文档**定义 `vector(1024)` + 默认模型 `text-embedding-ada-002`（1536 维）——这是潜在的 bug，1024 ≠ 1536，INSERT 时会报维度不匹配错误。

**实际项目**不存在这个问题，因为：
- 使用 ChromaDB（无 schema 定义，向量维度自动适配模型输出）。
- Embedding 模型 `paraphrase-multilingual-MiniLM-L12-v2` 输出 **384 维**（[embedding.py:10](infrastructure/rag/embedding.py#L10)），ChromaDB 自动接受。

**如果用 pgvector 且维度不匹配**：
```sql
-- 表定义 1024 维，但模型输出 1536 维
INSERT INTO faq_embeddings (embedding) VALUES ('[1536个浮点数]');
-- 报错: expected 1024 dimensions, got 1536
```

**解决方式**：
1. `ALTER TABLE faq_embeddings ALTER COLUMN embedding TYPE vector(1536);`
2. 或换一个 1024 维的 Embedding 模型（但没必要，应该让表适配模型，而非反之）。

**教训**：建表时维度应该从 Embedding 模型配置中读取，而非硬编码。生产环境应该在初始化脚本中动态生成 DDL。

---

# FastAPI/工程相关的：

## 21. FastAPI 的异步是怎么工作的？async def 和 def 端点有什么区别？

**答**：

**`async def` 端点**：FastAPI 在 asyncio event loop 中直接 await 执行。适合 I/O 密集操作（数据库查询、LLM API 调用），await 期间 event loop 可以处理其他请求，不阻塞。

**`def` 端点**：FastAPI 把函数丢进 `run_in_threadpool`（线程池）执行。适合 CPU 密集操作或有阻塞调用的同步代码，不阻塞 event loop 但占用线程。

**这个项目的选择**：

| 端点 | 类型 | 原因 |
|------|------|------|
| `/api/chat` | `def` | Agent `run()` 是同步方法（LangGraph 的 `invoke` 同步），内部可能阻塞 |
| `/api/chat/stream` | `async def` | `run_stream()` 是 async generator，需要 await `astream()` |

**关键理解**：`async def` 不等于快。如果函数内部是纯 CPU 计算（如 Embedding），用 `async def` 反而阻塞 event loop。正确做法是用 `def` + `run_in_threadpool`，或显式 `await asyncio.to_thread(cpu_task)`。

**项目中 Embedding 的问题**：[embedding.py:14-18](infrastructure/rag/embedding.py#L14-L18) 的 `embed()` 调用 `model.encode()` 是 CPU 密集且同步的。生产环境应包装为 `await asyncio.to_thread(embed, texts)` 避免阻塞 event loop。

---

## 22. 数据库连接池你是怎么管理的？连接池大小怎么设置？

**答**：这个项目有两层"数据库"：

**ChromaDB**（向量存储）：嵌入式，`PersistentClient` 单例模式（[store.py:11-18](infrastructure/rag/store.py#L11-L18)），客户端内部管理连接，无需额外配置连接池。

**SQLite**（会话存储）：通过 SQLAlchemy `SessionLocal`（[conversation.py](utils/conversation.py)），`with SessionLocal() as db:` 模式每次创建短生命周期 session，用完即关。SQLite 是文件数据库，连接池意义不大，`check_same_thread=False` 允许多线程访问。

**如果用 PostgreSQL（如需求文档设计的 pgvector）**：
- `asyncpg` 创建连接池，默认 `min_size=10, max_size=10`。
- 经验公式：`max_size = (CPU 核心数 × 2) + 有效并发连接数`。
- Demo 阶段 5-10 个连接足够。
- 这个项目的需求文档设计：`get_pool()` 返回全局单例连接池（[需求文档 4.3.6](需求文档中 database.py)），FastAPI lifespan 中初始化，全应用共享。

**当前项目不需要连接池调优**（ChromaDB + SQLite 都是嵌入式），这是 Demo 阶段的合理取舍。生产环境换 pgvector + PostgreSQL 后才需要关注连接池参数。

---

## 23. 依赖注入你是在 main.py 手动组装的，为什么没用 FastAPI 的 Depends？

**答**：因为这个项目的依赖图是**应用级单例**（全应用共享一个 Agent 实例），而非请求级依赖。

**FastAPI `Depends` 的适用场景**：每个请求需要独立的依赖实例（如从请求 header 解析用户身份 → 查数据库 → 注入 User 对象），或需要请求级别的生命周期管理。

**这个项目的依赖**（[main.py:22-28](main.py#L22-L28)）：
```python
lc_llm = LangChainChatModel(chat_service=chat_service)
conversation_manager = ConversationManager(max_context_turns=10)
agent = MasterAgent(llm=lc_llm, conversation_manager=conversation_manager, tools=[...])
```

Agent、LLM、ConversationManager 都是应用级别单例，启动时构造一次，全局复用。用 `Depends` 反而增加无意义开销（每个请求都解析依赖树）。

**如果要用 `Depends`**：需要把依赖包装成工厂函数，加上 `lru_cache` 或 scope 控制：
```python
@lru_cache()
def get_agent():
    return MasterAgent(...)

@router.post("/chat")
def chat(req, agent=Depends(get_agent)):
    ...
```

但这实际上就是 singleton 模式的另一种写法，没有本质区别。**手动组装更直观**——在 `main.py` 一眼看清完整的依赖拓扑，新成员读代码时不需要跳转到各个 Depends 函数。

---

## 24. 这个项目有没有做异常处理？LLM API 超时或向量库查询失败时会怎样？

**答**：做了多层防御，但仍有改进空间：

**LLM 层**（[llm/client.py:32-44](llm/client.py#L32-L44)）：
```python
try:
    resp = self.client.chat.completions.create(...)
    return resp.choices[0].message.content
except Exception as e:
    print(f"[LLM Error] {type(e).__name__}: {e}")
    return None
```
- 配置了 `timeout=30, max_retries=1`。
- 异常被捕获，返回 `None`，不会让整个请求崩溃。

**Agent 层**（[agent.py:158-168](domain/customer_service/agent.py#L158-L168)）：
```python
if llm_output is None:
    return AgentResponse(
        type="final_answer",
        content="抱歉，AI 服务暂时不可用，请稍后重试。",
        ...
    )
```
- LLM 返回 `None` 时，Agent 降级为用户友好的提示信息，不会抛 500。

**工具层**（[tools/faq_search.py:8-12](tools/faq_search.py#L8-L12)）：
```python
try:
    results = rag_store.search(query, top_k=top_k)
except Exception as e:
    return f"FAQ检索失败: {e}"
```
- 每个工具函数内部 try/except，失败返回错误信息字符串，不抛异常。Agent 拿到错误信息后走反问/兜底流程。

**API 层**（[routes.py:33-37](apps/customer_service/routes.py#L33-L37)）：
```python
if agent is None:
    raise HTTPException(500, "agent not initialized")
```
- 启动检查：agent 未初始化时返回明确错误。

**待改进的点**：
- `print()` 不是日志系统，生产环境应换成 `structlog` 结构化日志 + 请求级 trace_id。
- 没有 retry + circuit breaker，LLM 瞬时故障时直接降级，可以加 tenacity 指数退避重试。
- `MasterAgent.run_stream()` 的异常只 yield 一个 error 事件（[langgraph_agent.py:221-226](domain/customer_service/langgraph_agent.py#L221-L226)），前端需要正确处理 SSE 的 error 事件和断连重连。
- 没有全局 exception handler（FastAPI `@app.exception_handler`），未预期的异常会返回原始 500。

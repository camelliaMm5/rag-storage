# 智能客服项目 — 面试话术 & 深度问答

---

## 一、项目介绍话术（1-2 分钟）

> "我独立开发了一个智能客服系统，用于向决策层演示 AI 替代人工客服的可行性。系统采用 **FastAPI + LangGraph 多 Agent 架构**，前端是原生 HTML/CSS 单页聊天界面。
>
> 核心实现了四个能力：**FAQ 语义检索**、**订单查询**、**物流追踪**、**智能下单**。其中最复杂的是检索模块——我没有直接用关键词匹配，而是做了一套 **BM25 + 向量混合检索**，用 Sentence-Transformers 本地 Embedding 模型做语义向量化，存到 ChromaDB，再加上自研的中文分词 BM25 索引，两路结果加权重排序，准确率比纯向量检索有明显提升。
>
> Agent 这边经历了三次迭代：最早用正则解析 LLM 输出的 Thought/Action 做 ReAct 循环，后来迁移到 LangGraph 的 ToolNode 原生工具调用，最后又演进成 **Supervisor 多 Agent 架构**——一个路由 Agent 先判意图，再分发给 FAQ/订单/物流/下单四个子 Agent 分别处理，支持 SSE 流式输出，前端能实时看到每个节点的执行状态。
>
> 会话管理用 SQLAlchemy + SQLite 持久化，支持多轮对话上下文窗口自动截断。整体代码按领域层/应用层/基础设施层/接口层做 DDD 分层，依赖倒置，仓储接口预留了外部 API 对接位置。"

---

## 二、面试深度问答

### A. 检索系统（RAG）

#### Q1: 为什么选 ChromaDB 而不是 pgvector / Elasticsearch / Milvus？

**答**：几个考量——

1. **Demo 阶段零运维成本**：ChromaDB 是嵌入式向量数据库，`pip install` 即用，不需要单独部署 PostgreSQL 扩展或 Elasticsearch 集群。启动时自动创建持久化目录 `./chroma_db`。
2. **HNSW 索引 + cosine 距离**：ChromaDB 底层基于 HNSW 近似近邻搜索，创建 collection 时通过 metadata 指定 `hnsw:space: cosine`，检索速度和精度在万级 chunk 规模下足够。
3. **元数据过滤**：支持 `where` 条件过滤（如 `where={"product_code": "X1"}`），这在多产品 FAQ 场景下很有用——用户提到具体产品型号时，可以硬过滤到该产品的 chunk。

代码见 [store.py:25-29](infrastructure/rag/store.py#L25-L29)：
```python
_collection = client.get_or_create_collection(
    name=config.collection_name,
    metadata={"hnsw:space": "cosine"},
)
```

**如果数据量上到百万级**：会迁移到 Milvus 或 pgvector，利用 IVF/HNSW 索引 + 量化压缩，ChromaDB 在当前规模下是最优的性价比选择。

---

#### Q2: 混合检索（Hybrid Search）具体是怎么做的？为什么需要 BM25？

**答**：纯向量检索有个盲区——**对专有名词、型号编码不敏感**。比如用户搜"X1 Pro 怎么重置"，Embedding 模型可能把"X1 Pro"当成普通文本，语义匹配时优先召回"如何重置设备"而不是 X1 Pro 专属文档。

所以做了三路召回 + 重排序：

1. **向量检索**（语义匹配）：用户 query → Embedding → ChromaDB cosine 搜索，recall 20 条。
2. **BM25 关键词检索**（精确匹配）：自研 BM25 索引，中文用字 unigram + bigram 分词，保留英数字 token。对型号编码（X1, Pro, 2.4GHz 等）天然敏感。
3. **加权融合**：`final_score = 0.6 × vector_score + 0.4 × normalized_bm25_score`，再对命中关键词的 chunk 额外 ×1.1 加分。

代码见 [retriever.py:149-166](infrastructure/rag/retriever.py#L149-L166) 的 `_hybrid_rerank` 函数。

---

#### Q3: BM25 的中文分词怎么处理？为什么不用 jieba？

**答**：jieba 分词依赖词典，对技术术语（型号编码、英文缩写）容易切错。我采用了一种更鲁棒的方式：

- **英数字 token**：正则 `[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*` 提取，保留型号编码的完整结构（如 `2.4GHz`、`X1-Pro`）。
- **中文字符 unigram + bigram**：按字符级别切分为单字和相邻双字组合。这种方式不依赖词典，对任意中文文本都能工作，且 bigram 能捕捉常见词组。

代码见 [retriever.py:77-89](infrastructure/rag/retriever.py#L77-L89) 的 `BM25Index.tokenize`。

**为什么不直接用 jieba**：电子产品的 FAQ 里有大量混合中英文的术语（"2.4GHz 频段"、"Type-C 接口"），jieba 容易把 `2.4GHz` 切成 `2.4` / `GHz` 甚至更碎。自研 tokenizer 确保技术术语不被破坏。

---

#### Q4: Embedding 模型选的是哪个？为什么不用 API 而是本地部署？

**答**：选的是 `paraphrase-multilingual-MiniLM-L12-v2`，这是 Sentence-Transformers 的一个多语言模型，输出 **384 维**归一化向量。

选择理由：

1. **本地部署，零延迟、零费用**：Demo 阶段不需要频繁调用 Embedding API，本地跑 Sentence-Transformers 在 CPU 上也很快（单条 query 编码 < 50ms）。
2. **多语言支持**：模型支持中英文混合文本，适合电商 FAQ 的中英混杂场景。
3. **归一化输出**：`normalize_embeddings=True` 保证向量模长为 1，cosine distance 直接等价于内积，ChromaDB 的 cosine 距离计算可以直接使用。

代码见 [embedding.py:7-18](infrastructure/rag/embedding.py#L7-L18)。

**与 API 方案的对比**：API Embedding（如 text-embedding-ada-002）的优势是模型更大（1536 维），语义理解更深，缺点是网络延迟 + 费用。Demo 阶段 384 维的 MiniLM 完全够用，后续可以按需切换。

---

#### Q5: 用户输入的 query 做了什么预处理？

**答**：三步清洗，代码在 [retriever.py:38-41](infrastructure/rag/retriever.py#L38-L41)：

1. **Filler word 去除**：正则去掉"请问/我想知道/能不能/怎么才能/呢/吗/呀"等口语化填充词，提取核心问题。
2. **产品编码提取**：正则 `[A-Z]+\d+`（如 X1、Z2S）识别用户提到的具体产品型号，用于后续 ChromaDB 的 metadata 硬过滤。
3. **关键词提取**：抽出英数字术语 + 特殊格式（如 2.4GHz），用于 BM25 查询增强和结果打分加成。

**举例**：用户输入"请问一下我的 X1 Pro 怎么重置呢？" → 清洗后为 "X1 Pro 重置"，产品编码提取为 X1。

---

#### Q6: FAQ 文档是怎么切片（Chunk）的？

**答**：FAQ 有特殊结构——每个文件是多组 Q&A，以 `**问题标题**` 的 Markdown 粗体行作为边界。所以分割器 `splitter.py` 做了**结构感知切分**：

1. **FAQ 模式检测**：如果文档包含 `**...**` 格式的加粗行且以问号结尾，走 FAQ 分割逻辑——按 Q&A 边界切分，每个 chunk 带 `question` 元数据。
2. **长答案再切分**：如果单个答案超过 `chunk_size`（350 字符），在句子边界处（`。！？；\n`）做二次切割，每个子 chunk 前缀拼接问题标题，确保语义独立。
3. **通用回退**：非 FAQ 文档按 `chunk_size=350`、`chunk_overlap=50` 做滑动窗口切割，优先在自然边界断句。

代码见 [splitter.py:16-51](infrastructure/rag/splitter.py#L16-L51)。

---

#### Q7: 检索结果怎么去重和排序？用户问一个产品，会不会召回另一个产品的内容？

**答**：有三层保障：

1. **产品编码硬过滤**：如果 query 中提取到产品编码（如 X1），ChromaDB 搜索时直接加 `where={"product_code": "X1"}`，只检索该产品的 chunk。
2. **多源去重**：最终结果按 `product` 字段去重，每个产品最多保留一条最优结果。这避免了某个产品文档特别长导致霸占 Top-K 的问题。
3. **售后意图检测**：如果用户问题匹配到售后关键词（退换/保修/维修），除了产品专属 chunk 外，还会额外召回通用售后政策文档。

代码见 [retriever.py:212-262](infrastructure/rag/retriever.py#L212-L262) 的 `RAGStore.search`。

---

### B. Agent 架构

#### Q8: 为什么从正则 ReAct 迁移到 LangGraph？

**答**：第一版 Agent（`CustomerServiceAgent`）用正则 `Thought:...\nAction:...` 解析 LLM 输出，这样做有几个固有问题：

1. **格式不稳定**：LLM 不一定严格按格式输出，解析失败率高，需要多步重试。
2. **工具调用不标准**：自制的 Tool 体系虽然灵活，但不兼容 OpenAI Function Calling 协议，无法利用模型原生的 tool_choice 能力。
3. **无状态图抽象**：ReAct 循环、Checkpointer、流式输出都需要手写。

迁移到 LangGraph 后：
- `bind_tools()` 把工具转成 OpenAI Function Calling schema，LLM 原生给出 `tool_calls`，不再靠正则解析。
- `ToolNode` 自动处理工具执行和结果回传，`add_messages` reducer 自动合并消息历史。
- `MemorySaver` Checkpointer 开箱支持多轮对话的 thread_id 隔离。
- `astream()` 原生支持节点级别的流式输出。

代码对比：[agent.py:87-98](domain/customer_service/agent.py#L87-L98)（旧版正则解析）vs [graph.py:47-70](domain/customer_service/graph.py#L47-L70)（LangGraph ToolNode）。

---

#### Q9: Supervisor 多 Agent 架构是怎么设计的？和单 Agent 有什么区别？

**答**：单 Agent 把所有工具绑在一个节点上，LLM 自己判断调用哪个。问题在于：当工具变多时，prompt 变长，LLM 选错工具的概率上升。

Supervisor 架构的做法是**先路由、再执行**：

```
用户输入 → Supervisor Node（LLM 意图路由，输出 JSON）
              ├─ route: "faq" → FAQ Agent（只绑 search_faq 工具）
              ├─ route: "order" → Order Agent（只绑 query_order 工具）
              ├─ route: "logistics" → Logistics Agent（只绑 query_logistics 工具）
              ├─ route: "place_order" → PlaceOrder Agent（LLM抽参 + 下单工具）
              └─ route: "finish" → Finish Node（直接回复）
```

**效果**：
- 每个子 Agent 只面对 1 个工具，选错概率接近零。
- 并行潜力：未来可以把 faq/order/logistics 三个 Agent 并行执行。
- 流式透传：前端通过 SSE 能实时看到"正在分析意图…→ FAQ Agent 检索中…→ 结果"的执行过程。

代码见 [master_graph.py:187-233](domain/customer_service/master_graph.py#L187-L233) 的 `build_master_graph`。

---

#### Q10: 工具是怎么注册和绑定到 LLM 的？

**答**：设计了统一的 `Tool` 抽象层，三层转换：

1. **Tool 对象**：`tools/base.py` 定义了 `Tool` 类，包含 name/description/parameters/func，与框架无关。每个业务工具（search_faq、query_order 等）各自实例化一个 Tool 对象。
2. **to_langchain_tool()**：用 Pydantic `create_model` 动态构建 args_schema，包装成 `StructuredTool`，LangGraph 的 `bind_tools()` 拿到后自动转成 OpenAI Function Calling 的 JSON Schema。
3. **to_prompt_desc()**：生成人类可读的工具描述文本，供旧版 ReAct Agent 的 system prompt 使用。

代码见 [base.py:60-100](tools/base.py#L60-L100)。

---

#### Q11: 系统 Prompt 是怎么设计的？放了什么内容？

**答**：分两个层级：

**Supervisor Prompt**（[master_graph.py:21-35](domain/customer_service/master_graph.py#L21-L35)）：
- 定义路由规则（FAQ/订单/物流/下单/兜底）和输出 JSON 格式。
- 只做意图分类，不回答业务问题。

**子 Agent Prompt**（[prompts.py:46-66](domain/customer_service/prompts.py#L46-L66)）：
- 告知可用工具的用途和使用场景。
- 约束行为：只基于工具返回内容回答、信息不足时反问、不编造信息。

设计原则：
- **角色分离**：路由逻辑和业务逻辑分开，各自 prompt 短且聚焦，降低 LLM 混淆概率。
- **约束优先**：明确告知"不要编造""不确定时反问"，减少幻觉。
- **语言匹配**：中英文混杂的 FAQ 场景，prompt 用中文写约束，工具名保留英文。

---

#### Q12: 多轮对话的上下文是怎么管理的？不会无限增长吗？

**答**：两层管理机制：

1. **LangGraph Checkpointer**：`MemorySaver` 持久化每个 `thread_id` 的完整消息历史，Graph 的 `invoke` 通过 `configurable.thread_id` 自动加载和追加。
2. **ConversationManager 上下文窗口**：从 SQLite 取最近 `max_context_turns=10` 轮对话（20 条消息），超出的历史不塞入 prompt，但保留在数据库中可追溯。

代码见 [conversation.py:64-80](utils/conversation.py#L64-L80) 的 `get_context` 方法——按 `turn_number DESC` 取最近 N 轮后反转排序。

**Token 测算**：10 轮对话约 2000-3000 tokens + system prompt 200 tokens + 工具定义 300 tokens = 约 3000 tokens 的 prompt 开销，在 gpt-4o-mini 的 128K 上下文内完全不构成压力。

---

#### Q13: SSE 流式输出是怎么实现的？前端能收到什么？

**答**：`MasterAgent.run_stream()` 是 async generator，利用 LangGraph 的 `astream()` 在每个节点执行完时 yield 一个事件：

```python
async for chunk in self.graph.astream(state, config):
    for node_name, node_state in chunk.items():
        yield {"event": "routing", "node": "supervisor", "route": "faq", ...}
        yield {"event": "agent_result", "node": "faq_agent", "content": "..."}
```

FastAPI 端用 `StreamingResponse` + `text/event-stream` MIME 类型，每个事件一行 `data: {json}\n\n`。

前端收到的典型事件序列：
```
→ {event: "routing", node: "supervisor", route: "faq", status: "routed"}
→ {event: "agent_result", node: "faq_agent", content: "根据您的X1..."}
→ {event: "done"}
```

代码见 [langgraph_agent.py:152-227](domain/customer_service/langgraph_agent.py#L152-L227) 和 [routes.py:47-69](apps/customer_service/routes.py#L47-L69)。

---

### C. 工程实践

#### Q14: 为什么用 FastAPI？和 Flask / Django 有什么区别？

**答**：

1. **原生 async/await**：全链路异步——FastAPI async endpoint → asyncpg/LangGraph async invoke → ChromaDB。Flask 需要额外装 quart 或手动事件循环。
2. **自动 OpenAPI 文档**：Pydantic 模型自动生成 `/docs` Swagger 页面，Demo 演示时可以直接在文档页测试 API，不需要 Postman。
3. **依赖注入**：虽然这个项目在 `main.py` 手动组装依赖（因为要跨模块注入），但 FastAPI 的 `Depends` 机制在复杂场景下很有价值。
4. **SSE 支持**：`StreamingResponse` 天然支持 Server-Sent Events。

对于 Demo 项目，选 FastAPI 的核心逻辑是「异步 + 自动文档 + 快速迭代」，没有历史包袱。

---

#### Q15: 会话数据为什么用 SQLite 而不是 MemorySaver？

**答**：LangGraph 的 `MemorySaver` 是纯内存存储，服务重启后所有对话丢失。Demo 演示时如果服务意外重启（比如改代码触发了 uvicorn --reload），老板之前测试的对话全部消失，体验很差。

所以会话管理用了 **SQLAlchemy + SQLite**，存在 `conversations.db` 文件中。同时保留了 `MemorySaver` 给 LangGraph Checkpointer（管理 Graph 内部的 state），会话层的 CRUD 独立管理。

**升级路线**：SQLite → PostgreSQL（只需改连接字符串，SQLAlchemy 的方言切换零代码改动）。

---

#### Q16: 从 Demo 到生产，你会改哪些地方？优先级？

**答**：按优先级排序：

1. **Checkpointer 持久化**：MemorySaver → PostgresSaver/SqliteSaver，保证服务重启不丢会话状态。
2. **LLM 容错**：加 retry（tenacity 库）、fallback（主模型不可用时切备用模型）、timeout + circuit breaker。
3. **向量库升级**：ChromaDB → pgvector 或 Milvus，支持更大规模 FAQ + 更灵活的过滤。
4. **安全加固**：去掉 `allow_origins=["*"]` CORS，加 API 鉴权（JWT/API Key），输入清洗防 prompt injection。
5. **可观测性**：LangSmith/LangFuse 接入，监控 Agent 各节点的耗时、tool call 成功率、LLM token 消耗。
6. **容器化部署**：Docker Compose 编排 FastAPI + ChromaDB（或 pgvector），Nginx 反代 + 静态资源。

---

#### Q17: 项目中遇到过什么技术难点？怎么解决的？

**答**：最大的难点是**混合检索的调参**。

一开始纯向量检索的准确率大概只有 60-70%，用户搜产品编码时经常召回不对应的文档。加 BM25 后又有新问题——BM25 对短 query（比如"退货"）的 IDF 区分度很低，导致 BM25 分数对最终排序的贡献不稳定。

**解决过程**：
1. 加了产品编码提取和硬过滤，在 query 侧直接限定检索范围，这是性价比最高的优化。
2. 调整混合权重从 0.5/0.5 到 0.6/0.4，偏向量侧，BM25 起辅助修正作用。
3. 引入关键词命中加成（×1.1），让同时命中向量和关键词的 chunk 排到最前面。
4. 加了多源去重，避免单一产品的长文档霸榜。

调完后 Top-1 准确率稳定在 85%+。

---

### D. 架构设计

#### Q18: 为什么选 DDD 分层？Demo 项目需要这么"重"吗？

**答**：DDD 的核心价值在 Demo 里体现在一个点上——**仓储接口预留扩展点**。

`OrderRepository` 在 `domain/order/repository.py` 定义接口（`find_order`、`find_logistics`），当前 Demo 阶段 `MockOrderRepository` 返回硬编码的 3 条数据。但 `OrderApplicationService` 依赖的是接口而非实现。

后期接入真实电商系统 API 时，只需新建一个 `RealOrderRepository` 实现同一接口，`main.py` 改一行注入代码。上层业务代码 **零改动**。

这个设计对 Demo 来说不是过度设计——正是向决策层展示"Demo 验证通过后可以快速产品化"的关键说服点。

代码见 [domain/order/repository.py](domain/order/repository.py) vs [infrastructure/order_repository.py](infrastructure/order_repository.py)。

---

#### Q19: 工具调用如果失败（比如 LLM 选错工具），有什么 fallback 机制？

**答**：多层防御：

1. **Supervisor 路由层**：意图判断错（比如物流判成订单），子 Agent 拿到不匹配的参数时，LLM 会因为工具返回空结果而告知用户"未找到该订单的物流信息，请确认订单号"。
2. **Agent max_steps 限制**：设了 `max_steps=5`，防止 Agent 陷入工具调用死循环。
3. **工具层异常捕获**：每个工具函数内部 `try/except`，失败时返回 `"工具调用失败: {e}"` 而非抛异常，Agent 拿到错误信息后走标注流程。
4. **LLM 不可用降级**：`ChatService.chat()` 返回 `None` 时，Agent 直接返回"AI 服务暂时不可用，请稍后重试"。

代码见 [agent.py:158-168](domain/customer_service/agent.py#L158-L168) 和 [order_search.py:12-13](tools/order_search.py#L12-L13)。

---

#### Q20: 如果要支持 100 个工具，当前架构有什么问题？怎么改？

**答**：当前 Supervisor 把所有路由规则写在一个 prompt 里，工具数到 100 时 prompt 会过长，LLM 意图分类准确率下降。

**改进方案**：
1. **分层路由**：第一层粗分类（售前/售中/售后），第二层细分类（售前→商品推荐/商品对比/库存查询）。
2. **语义路由**：不用 LLM 分类，用 Embedding 相似度匹配——把用户 query 做 Embedding，和每个工具的 description Embedding 算余弦相似度，Top-K 作为候选工具，再让 LLM 从候选中精排。
3. **工具描述向量化**：预先计算所有工具 description 的向量，存储在向量库中，意图路由变成一次向量检索 + LLM 精排。

---

## 三、可能的追问清单（快速索引）

| # | 问题 | 难度 |
|---|------|------|
| 1 | ChromaDB 的 HNSW 索引参数怎么调的？ | ⭐⭐⭐ |
| 2 | BM25 的 k1 和 b 参数为什么取 1.5 和 0.75？ | ⭐⭐⭐ |
| 3 | 向量和 BM25 的融合权重 0.6/0.4 怎么确定的？ | ⭐⭐ |
| 4 | Sentence-Transformers 的 normalize_embeddings 对 cosine 距离有什么影响？ | ⭐⭐⭐ |
| 5 | LLM 输出的 JSON 解析失败怎么办？有重试机制吗？ | ⭐⭐ |
| 6 | 如果用户连续问两个不同订单号，Agent 能正确处理吗？ | ⭐⭐ |
| 7 | uvicorn 的 worker 数量和 async 事件循环怎么配合？ | ⭐⭐⭐ |
| 8 | 前端聊天的 SSE 断连重连怎么处理？ | ⭐⭐ |
| 9 | 怎么防止 prompt injection（用户输入覆盖 system prompt）？ | ⭐⭐⭐ |
| 10 | FAQ 文档更新后如何触发 re-index？是全量还是增量？ | ⭐⭐ |

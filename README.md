# RAG Customer Service Agent

基于 LangGraph 的 ReAct Agent 智能客服，支持 FAQ 工具调用、多轮对话和会话持久化。底层使用 DeepSeek（OpenAI 兼容接口）作为 LLM，ChromaDB + BM25 混合检索作为知识库。

## 架构

```
main.py                             ← FastAPI 启动入口 + 依赖装配
apps/customer_service/routes.py     ← API 路由 (POST /chat 等)
domain/customer_service/
    langgraph_agent.py              ← LangGraphAgent 封装层
    graph.py                        ← LangGraph StateGraph (agent ⇄ tools)
    agent.py                        ← [保留] 旧版 ReAct Agent (可回退)
    prompts.py                      ← System Prompt 模板
tools/
    base.py                         ← Tool + ToolParameter + to_langchain_tool()
    faq_search.py                   ← FAQ 检索工具
llm/
    client.py                       ← ChatService (OpenAI SDK 调用)
    langchain_model.py              ← BaseChatModel 适配层 (ChatService → LangChain)
utils/conversation.py               ← ConversationManager (会话 CRUD)
infrastructure/
    rag/                            ← RAG 向量检索 (ChromaDB + BM25 混合)
    database.py                     ← 数据库连接 (SQLite/PostgreSQL)
    models.py                       ← ORM 模型 (conversations + messages)
```

## 核心设计

### Tool Calling：协议原生而非文本约定

```
旧版 (agent.py):                   新版 (graph.py):
LLM 输出                              llm.bind_tools(tools)
"Action: search_faq[退货]"              → LLM 返回结构化 tool_calls JSON
正则解析 + _dispatch_tool()            → ToolNode 自动解析 + 执行
字符串拼接 Observation                 → add_messages reducer 自动累积
```

### 图结构

```
START → agent (LLM) → [有 tool_calls?]
            ↑              ↓ 是
            │         tools (ToolNode)
            └────────────┘
            ↓ 否
           END
```

- **State**：`messages`（add_messages reducer 累积）、`step_count`（覆盖）、`max_steps`（配置）
- **Checkpointer**：MemorySaver（开发），后续切换 PostgresSaver（生产）
- **thread_id** = `conversation_id`，框架自动恢复历史上下文

## 快速开始

```bash
# 1. 配置 .env 中的 LLM_API_KEY
# 2. 启动服务
D:\python.exe main.py
# 或: run.bat main.py
```

浏览器打开 http://localhost:8000

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 对话接口，body: `{conversation_id, message}` |
| POST | `/api/conversations` | 创建会话 |
| GET | `/api/conversations/{id}` | 会话历史 |
| GET | `/api/conversations?user_id=` | 会话列表 |
| GET | `/api/health` | 健康检查 |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API 密钥 | — |
| `LLM_BASE_URL` | LLM 接口地址 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名称 | `deepseek-chat` |
| `CONVERSATION_DB_URL` | 数据库连接 | `sqlite:///./conversations.db` |
| `EMBEDDING_MODEL` | 向量化模型 | `paraphrase-multilingual-MiniLM-L12-v2` |
| `CHROMA_DB_PATH` | 向量数据库路径 | `./chroma_db` |

## 项目文件

| 模块 | 文件 | 职责 |
|------|------|------|
| 入口 | `main.py` | FastAPI 启动 + 依赖装配 |
| API | `apps/customer_service/routes.py` | 路由：接收参数 → Agent → 格式化响应 |
| Agent | `domain/customer_service/langgraph_agent.py` | LangGraphAgent 封装 |
| 图 | `domain/customer_service/graph.py` | StateGraph 定义 (State + Node + Edge) |
| 工具 | `tools/faq_search.py` | FAQ 检索函数 + Tool 对象 |
| LLM | `llm/langchain_model.py` | ChatService → BaseChatModel 适配 |
| LLM | `llm/client.py` | OpenAI 兼容 SDK 调用 |
| 会话 | `utils/conversation.py` | 会话 CRUD + 上下文窗口 |
| RAG | `infrastructure/rag/` | 向量存储、混合检索、文档切分 |
| 数据 | `infrastructure/models.py` | conversations + messages 表 |

## 运行测试

```bash
D:\python.exe -m tests.test_rag
```

"""FAQ search tool: function + Tool object for Agent dispatch."""

from infrastructure.rag import rag_store
from tools.base import Tool, ToolParameter


def search_faq(query: str, top_k: int = 5) -> str:
    """Search FAQ knowledge base. Returns formatted results or empty-result message."""
    try:
        results = rag_store.search(query, top_k=top_k)
    except Exception as e:
        return f"FAQ检索失败: {e}"

    if not results:
        return "未在FAQ知识库中找到相关内容。"

    lines = []
    for i, r in enumerate(results, 1):
        question = r.metadata.get("question", "")
        answer = r.metadata.get("answer", r.text)
        product = r.metadata.get("product", "未知")
        score = r.metadata.get("distance", r.score)
        lines.append(f"[{i}] 来源: {product}  |  相关度: {score:.2%}")
        if question:
            lines.append(f"问题: {question}")
        lines.append(f"答案: {answer}")
        lines.append("")
    return "\n".join(lines)


search_faq_tool = Tool(
    name="search_faq",
    description="检索FAQ知识库。输入用户问题关键词，返回相关的常见问题及其答案。",
    func=search_faq,
    parameters=[
        ToolParameter("query", "string", "检索关键词或用户问题", required=True),
        ToolParameter("top_k", "integer", "返回结果数量", required=False, default=5),
    ],
)

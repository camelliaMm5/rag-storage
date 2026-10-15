"""FAQ search tool: function + Tool object for Agent dispatch."""

from infrastructure.rag import rag_store
from tools.base import Tool, ToolParameter


def search_faq(query: str, top_k: int = 5) -> str:
    """Search FAQ knowledge base. Returns formatted results or empty-result message.

    走 ``rag_store.search_multi``（多路召回融合）：即使调用方给的是「空格分隔的
    关键词串」，也会派生聚焦变体兜底召回；自然问句则退化为单路，零额外开销。
    """
    try:
        results = rag_store.search_multi(query, top_k=top_k)
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
    description=(
        "检索FAQ知识库。输入用户的问题本身（自然语言问句，如「X1 智能门锁的电池能续航多久」），"
        "返回相关的常见问题及其答案。不要把问题压成空格分隔的关键词串。"
    ),
    func=search_faq,
    parameters=[
        ToolParameter("query", "string", "用户的完整问题（自然语言问句，不要用空格拼关键词）",
                      required=True),
        ToolParameter("top_k", "integer", "返回结果数量", required=False, default=5),
    ],
)

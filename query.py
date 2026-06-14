"""Interactive RAG query console."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from infrastructure.rag import RAGStore


def main():
    store = RAGStore()

    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    if os.path.isdir(docs_dir):
        total = store.ingest_dir(docs_dir)
        print(f"已加载 {total} 条知识片段\n")

    print("=" * 50)
    print("  RAG 知识检索 — 输入问题，返回最佳答案")
    print("  输入 /quit 退出，/count 查看条目数")
    print("=" * 50)

    while True:
        try:
            query = input("\n> 请输入问题：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not query:
            continue
        if query == "/quit":
            print("再见！")
            break
        if query == "/count":
            print(f"当前知识库共 {store.count()} 条片段")
            continue

        results = store.search(query, top_k=1)
        if not results:
            print("\n未找到相关内容。")
            continue

        print()
        for i, r in enumerate(results, 1):
            source = r.metadata.get("product", "未知")
            question = r.metadata.get("question", "")
            if i == 1:
                print(f"  {'─' * 50}")
            else:
                print(f"  {'·' * 50}")
            print(f"  [{i}] 相关度: {r.score:.2%}  |  来源: {source}")
            if question:
                print(f"      问题: {question}")
            # Wrap long text
            text = r.text
            width = 56
            while len(text) > width:
                print(f"      {text[:width]}")
                text = text[width:]
            if text:
                print(f"      {text}")
        print(f"  {'─' * 50}")
        print()


if __name__ == "__main__":
    main()

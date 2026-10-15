"""Smoke test: ingest the 5 real FAQ documents and verify search returns relevant results."""
import sys
import os
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infrastructure.rag import RAGStore
from infrastructure.rag.store import reset as store_reset

# Use a temp directory for ChromaDB so we don't pollute the real one
TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_chroma_db")


def setup():
    if os.path.exists(TEST_DB_PATH):
        shutil.rmtree(TEST_DB_PATH)


def teardown():
    try:
        if os.path.exists(TEST_DB_PATH):
            shutil.rmtree(TEST_DB_PATH)
    except PermissionError:
        pass


def _ingest_faq(store, docs_dir) -> int:
    """只灌 5 个 FAQ 文档（``Doc*.md``）—— 与 docs/ 下的工程文档严格隔离。

    早先这里直接 ``ingest_dir(docs_dir)``：``docs/`` 后来混入了项目说明等 .md，
    于是把「项目文档」也灌进了知识库，既污染检索又让多面查询断言失真。
    """
    faq_files = sorted(
        f for f in os.listdir(docs_dir)
        if f.startswith("Doc") and f.endswith(".md")
    )
    return sum(store.ingest_file(os.path.join(docs_dir, f)) for f in faq_files)


def main():
    setup()

    # Override db path to test location
    from infrastructure.rag.config import config
    from infrastructure.rag.store import reset as store_reset2

    config.chroma_db_path = TEST_DB_PATH
    # 关键：store 的 client/collection 是**模块级单例**，import 时已按真实路径建好；
    # 只改 config 不会换库 —— 必须先 reset 让下一次连接落到临时库，否则测试会写进
    # 生产知识库（曾把 144 条工程文档灌进真实 chroma_db）。
    store_reset2()

    store = RAGStore()
    docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

    # --- Ingest ---
    total = _ingest_faq(store, docs_dir)
    print(f"[INGEST] Total chunks indexed: {total}")
    assert total > 0, "Expected at least 1 chunk"

    # --- Count ---
    c = store.count()
    print(f"[COUNT]  Chunks in store: {c}")
    assert c == total, "Count should match ingested total"

    # --- Search: exact FAQ question ---
    results = store.search("怎么重置 WiFi？")
    print(f"[SEARCH] '怎么重置 WiFi？' -> {len(results)} results")
    for r in results:
        print(f"  score={r.score:.4f}  source={r.metadata.get('product','?')}")
        print(f"  text={r.text[:80]}...")
    assert len(results) > 0, "Expected at least 1 search result"
    assert results[0].score > 0.3, f"Top result score {results[0].score:.4f} too low"

    # --- Search: product-specific MUST return correct product ---
    results_p = store.search("X1设置键没反应")
    print(f"[SEARCH] 'X1设置键没反应' -> {len(results_p)} results")
    for r in results_p:
        print(f"  score={r.score:.4f}  product={r.metadata.get('product','?')}")
    assert len(results_p) > 0
    assert any("X1" in (r.metadata.get("product") or "") for r in results_p), \
        "X1 query must return X1 results, not X2"

    # --- Search: multi-faceted query (symptom + warranty) ---
    # top_k 必须 >1：本用例要同时看到「商品域」与「政策域」两个来源，而
    # _dedup_by_source 保证「每来源 1 条」，默认 top_k=1 时永远只有 1 条结果。
    results_m = store.search("X1设置键没反应，保修期内，退换还是维修", top_k=5)
    print(f"[SEARCH] multi-facet 'X1设置键没反应保修退换' -> {len(results_m)} results")
    products = [r.metadata.get("product","?") for r in results_m]
    print(f"  sources: {products}")
    has_x1 = any("X1" in (p or "") for p in products)
    has_warranty = any("售后" in (p or "") for p in products)
    print(f"  has_x1={has_x1} has_warranty={has_warranty}")
    assert has_x1, "Multi-facet query must include product-specific result"
    assert has_warranty, "Multi-facet query must include warranty policy"

    # --- Search: semantic query ---
    results2 = store.search("门锁电池能用多长时间")
    print(f"[SEARCH] '门锁电池能用多长时间' -> {len(results2)} results")
    for r in results2:
        print(f"  score={r.score:.4f}  product={r.metadata.get('product','?')}")
    assert len(results2) > 0
    assert any("电池" in r.text for r in results2), "Expected battery-related results"

    # --- Search with product filter ---
    results3 = store.search("怎么重置", product="X1智能门锁")
    print(f"[SEARCH] '怎么重置' (product=X1) -> {len(results3)} results")
    for r in results3:
        print(f"  score={r.score:.4f}  product={r.metadata.get('product','?')}")
    assert all(r.metadata.get("product") == "X1智能门锁" for r in results3), \
        "All results should be from X1"

    # --- Search: warranty ---
    results4 = store.search("保修期多长")
    print(f"[SEARCH] '保修期多长' -> {len(results4)} results")
    for r in results4:
        print(f"  score={r.score:.4f}  product={r.metadata.get('product','?')}")
    assert any("保修" in r.text for r in results4), "Expected warranty-related results"

    # --- Re-ingest idempotency ---
    total2 = _ingest_faq(store, docs_dir)
    print(f"[REINGEST] Chunks after re-ingest: {total2}")
    assert store.count() == total2, "Re-ingest should maintain consistent count"
    assert total2 == total, f"Re-ingest count {total2} should match initial {total}"

    store_reset()
    teardown()
    print("\n=== ALL TESTS PASSED ===")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

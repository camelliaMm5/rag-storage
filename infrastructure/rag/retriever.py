import re
import math
from collections import defaultdict
from .config import config
from .models import Document, Chunk, SearchResult
from .loader import load_file, load_dir
from .splitter import split
from .embedding import embed, embed_query
from .store import (
    add, delete_by_doc_id, get_all,
    search as vector_search, count as store_count,
)

# ── product code extraction ──────────────────────────────────────────
# Use lookbehind/ahead instead of \b — \b fails between ASCII and CJK chars
_PRODUCT_CODE = re.compile(r"(?:^|[^A-Za-z])([A-Z]+\d+)(?:[^A-Za-z]|$)", re.IGNORECASE)

# ── warranty / after-sales intent keywords ────────────────────────────
_WARRANTY_TERMS = re.compile(
    r"(退换|换新|维修|保修|售后|退货|更换|修理|免费|收费|上门|保修期|延保|过保|寄修)"
)

# ── common filler words in Chinese queries ────────────────────────────
_FILLER = re.compile(
    r"(请问|请告诉我|我想知道|我想问|帮我查|能不能|可不可以|有没有|怎么才能|到底|应该|如何"
    r"|一下|一下吗|呢|吗|呀|啊|吧|了|的|该怎么|我该)",
)


def _extract_product_codes(text: str) -> list[str]:
    return _PRODUCT_CODE.findall(text.upper())


def _has_warranty_intent(text: str) -> bool:
    return bool(_WARRANTY_TERMS.search(text))


def _clean_query(query: str) -> str:
    """Remove filler words, normalize whitespace, extract core question."""
    q = _FILLER.sub(" ", query)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _extract_keywords(text: str) -> list[str]:
    """Extract salient keywords: product codes + technical terms + core words."""
    kw = []
    # Product codes are high-signal
    kw.extend(_PRODUCT_CODE.findall(text.upper()))
    # Technical/specific terms (English + digits)
    for m in re.finditer(r"[A-Za-z]+(?:[.-][A-Za-z0-9]+)*|\d+\.?\d*[A-Za-z]*", text):
        term = m.group().lower()
        if len(term) >= 2 and term not in ("wi", "fi", "hz"):
            kw.append(term)
        elif term == "wifi":
            kw.append("wifi")
    # Extract 2.4GHz style terms explicitly
    for m in re.finditer(r"\d+\.?\d*\s*[GM]?Hz", text, re.IGNORECASE):
        kw.append(m.group().lower())
    return list(set(kw))  # dedup


# ── BM25 Index ────────────────────────────────────────────────────────
class BM25Index:
    """Minimal BM25 for Chinese text: char unigrams + bigrams + alphanumeric tokens."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: list[tuple[str, str]] = []  # (chunk_id, text)
        self.doc_len: list[int] = []
        self.avg_len: float = 0
        self.df: defaultdict[str, int] = defaultdict(int)  # doc freq
        self.tf: list[dict[str, int]] = []  # term freq per doc
        self.N: int = 0

    @staticmethod
    def tokenize(text: str) -> list[str]:
        tokens: list[str] = []
        # Preserve alphanumeric tokens (model numbers, technical terms)
        for m in re.finditer(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", text):
            tokens.append(m.group().lower())
        # Chinese: character unigrams + bigrams
        chinese = re.sub(r"[A-Za-z0-9\s\.\-,;:!?()（）【】《》\"'＇]+", "", text)
        chinese = chinese.replace("\n", "").replace("\r", "")
        for i, ch in enumerate(chinese):
            tokens.append(f"c:{ch}")
            if i < len(chinese) - 1:
                tokens.append(f"b:{ch}{chinese[i + 1]}")
        return tokens

    def add(self, chunk_id: str, text: str):
        tokens = self.tokenize(text)
        tf = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        self.docs.append((chunk_id, text))
        self.doc_len.append(len(tokens))
        self.tf.append(dict(tf))
        for t in set(tokens):
            self.df[t] += 1
        self.N += 1
        self.avg_len = sum(self.doc_len) / self.N

    def remove(self, chunk_id: str):
        """Remove a document from the index by chunk_id."""
        for i, (cid, _) in enumerate(self.docs):
            if cid == chunk_id:
                for term in self.tf[i]:
                    self.df[term] -= 1
                    if self.df[term] <= 0:
                        del self.df[term]
                del self.docs[i]
                del self.doc_len[i]
                del self.tf[i]
                self.N -= 1
                if self.N > 0:
                    self.avg_len = sum(self.doc_len) / self.N
                else:
                    self.avg_len = 0
                return

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """BM25 search, returns list of (chunk_id, score)."""
        if self.N == 0:
            return []
        q_tokens = self.tokenize(query)
        scores: dict[int, float] = {}
        for qi, term in enumerate(q_tokens):
            df = self.df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            # Query term frequency boost (first occurrence weighted more)
            qtf = 1.0 / (1.0 + 0.5 * qi)
            for doc_i, tf_dict in enumerate(self.tf):
                if term not in tf_dict:
                    continue
                tf = tf_dict[term]
                doc_len_norm = 1 - self.b + self.b * (self.doc_len[doc_i] / self.avg_len)
                score = idf * (tf * (self.k1 + 1)) / (tf + self.k1 * doc_len_norm) * qtf
                scores[doc_i] = scores.get(doc_i, 0.0) + score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(self.docs[i][0], s) for i, s in ranked[:top_k]]


# ── Hybrid Re-ranking ─────────────────────────────────────────────────
def _hybrid_rerank(
    vec_results: list[SearchResult],
    bm25_ranked: list[tuple[str, float]],
) -> list[SearchResult]:
    """Re-rank vector results with BM25 keyword scores (weighted combination)."""
    if not vec_results:
        return []

    bm25_map = {cid: s for cid, s in bm25_ranked}
    max_bm25 = max((s for _, s in bm25_ranked), default=1.0)

    for r in vec_results:
        bm25_raw = bm25_map.get(r.chunk_id, 0.0)
        bm25_norm = bm25_raw / max_bm25 if max_bm25 > 0 else 0.0
        r.score = 0.6 * r.score + 0.4 * bm25_norm

    vec_results.sort(key=lambda r: r.score, reverse=True)
    return vec_results


def _dedup_by_source(
    results: list[SearchResult], top_k: int
) -> list[SearchResult]:
    """Pick top results ensuring distinct source products."""
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for r in results:
        src = r.metadata.get("product", "")
        if src not in seen:
            seen.add(src)
            deduped.append(r)
        if len(deduped) >= top_k:
            break
    return deduped


# ── RAGStore ──────────────────────────────────────────────────────────
class RAGStore:
    """RAG storage: ingest documents, search with hybrid BM25+vector."""

    def __init__(self):
        self._bm25 = BM25Index()
        self._rebuild_bm25()

    def _rebuild_bm25(self):
        self._bm25 = BM25Index()
        for chunk_id, text in get_all():
            self._bm25.add(chunk_id, text)

    def ingest_file(self, filepath: str) -> int:
        doc = load_file(filepath)
        if doc is None:
            return 0
        return self._index(doc)

    def ingest_dir(self, dirpath: str | None = None) -> int:
        path = dirpath or config.docs_dir
        documents = load_dir(path)
        total = 0
        for doc in documents:
            total += self._index(doc)
        return total

    def search(
        self, query: str, top_k: int = 1, product: str | None = None
    ) -> list[SearchResult]:
        """Hybrid search with product-code filtering and multi-source dedup."""
        query_clean = _clean_query(query)
        keywords = _extract_keywords(query)
        codes = _extract_product_codes(query.upper())
        recall_k = config.recall_k

        # ── 1. Vector search ──
        vec = embed_query(query_clean)
        vec_results: list[SearchResult] = []

        if product:
            # Explicit product filter (API caller override)
            vec_results = vector_search(vec, top_k=recall_k, where={"product": product})
        elif codes:
            # Query mentions a product code → hard-filter to that product
            vec_results = vector_search(
                vec, top_k=recall_k, where={"product_code": codes[0]}
            )
            # Also pull warranty/general docs if the query implies warranty intent
            if _has_warranty_intent(query):
                warranty = vector_search(
                    vec, top_k=max(3, recall_k // 3),
                    where={"product_code": ""},
                )
                vec_results.extend(warranty)
        else:
            vec_results = vector_search(vec, top_k=recall_k)

        # ── 2. BM25 keyword search ──
        bm25_query = query_clean
        if keywords:
            bm25_query = query_clean + " " + " ".join(keywords)
        bm25_ranked = self._bm25.search(bm25_query, top_k=recall_k)

        # ── 3. Hybrid re-rank ──
        results = _hybrid_rerank(vec_results, bm25_ranked)

        # ── 4. Keyword presence bonus ──
        if keywords:
            for r in results:
                for kw in keywords:
                    if kw.lower() in r.text.lower():
                        r.score *= 1.1
            results.sort(key=lambda r: r.score, reverse=True)

        # ── 5. Multi-source dedup ──
        results = _dedup_by_source(results, top_k)

        return results

    def count(self) -> int:
        return store_count()

    def _index(self, doc: Document) -> int:
        delete_by_doc_id(doc.doc_id or "")
        chunks = split(doc)
        if not chunks:
            return 0
        texts = [c.text for c in chunks]
        vectors = embed(texts)
        ids = add(chunks, vectors)
        # Sync BM25
        for i, chunk in enumerate(chunks):
            self._bm25.add(chunk.chunk_id or ids[i], chunk.text)
        return len(chunks)


rag_store = RAGStore()

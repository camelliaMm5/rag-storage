import re
import math
from collections import defaultdict
from infrastructure.cache import cache, NS_FAQ, TTL_FAQ
from .config import config
from .models import Document, Chunk, SearchResult
from .loader import load_file, load_dir
from .splitter import split
from .embedding import embed, embed_query
from .store import (
    add, delete_by_doc_id, get_all,
    search as vector_search, count as store_count,
)

# ── cache codec: SearchResult <-> plain dict (JSON-safe) ─────────────
def _to_dict(r: SearchResult) -> dict:
    return {
        "text": r.text,
        "score": r.score,
        "metadata": r.metadata,
        "chunk_id": r.chunk_id,
    }


def _to_search_result(d: dict) -> SearchResult:
    return SearchResult(
        text=d.get("text", ""),
        score=d.get("score", 0.0),
        metadata=d.get("metadata") or {},
        chunk_id=d.get("chunk_id", ""),
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


# ── multi-query recall (方案 B 轻量版) ────────────────────────────────
# 动机：LLM 有时把用户问句压成「空格分隔的关键词串」（如「X1 智能门锁 电池 续航」）。
# 向量检索对词串的语义表达很差 —— 主体词（X1 智能门锁）占满语义权重，真正的诉求
# （电池/续航）被稀释，于是命中「同主体、不同话题」的条目（实测把「手机控制」顶到
# 第一，正确答案「电池能用多久」掉到第三，再被 by-source 去重挤掉）。
# 对策：把 query 派生为几个**聚焦变体**各召回一次，再按「共识」融合 —— 即使原句
# 写得不好，只要有一个变体召回正确条目，就能把它捞回来。
_SPLIT_PUNCT = re.compile(r"[，。！？、；：,\.!\?;:]+")
# 纯疑问/虚词片段（去掉标点后不剩实义）—— 如「怎么办？」——不配当检索变体
_VARIANT_JUNK = re.compile(
    r"^[怎么如何为什么哪那个什么多是否能不能可不可以办样做法了呀啊吧的呢哦嘛么"
    r"？?！!，,。．.、；;：:\s]+$"
)


def generate_query_variants(query: str) -> list[str]:
    """把用户 query 派生为多个检索变体（去重、保序、最多 5 个）。

    - **原句始终在首位**：自然问句本就是最优形态，不该被变体替换掉；
    - 空格分段 ≥3 段（疑似关键词堆）时，另派生「尾部两段 / 尾段」两个聚焦子查询
      —— 中文问句里「诉求」通常排在「主体」之后，砍掉前缀主体即可提纯语义；
      再补一条「去空白紧凑版」，覆盖「X1门锁」这类连写；
    - 含标点时按标点切出各分句（多问句场景各自成一条）；
    - 长度 < 2 或纯疑问虚词（「怎么办？」）的片段会被丢弃，避免稀释融合结果。

    自然问句（无空格分段、无标点）只会得到 **1 个变体**，因此不产生额外召回开销
    —— 此时 :meth:`RAGStore.search_multi` 会直接走单路检索。
    """
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return []

    out: list[str] = []

    def _add(v: str) -> None:
        v = re.sub(r"\s+", " ", (v or "")).strip()
        if not v or v in out or len(v) < 2 or _VARIANT_JUNK.match(v):
            return
        out.append(v)

    _add(q)
    segs = q.split()
    if len(segs) >= 3:                       # 疑似「关键词堆」
        _add(" ".join(segs[-2:]))            # 尾部两段（多为诉求）
        _add(segs[-1])                       # 尾段
    if _SPLIT_PUNCT.search(q):               # 多分句 → 各自成条
        for seg in _SPLIT_PUNCT.split(q):
            _add(seg)
    if len(segs) >= 3:
        _add(re.sub(r"\s+", "", q))          # 去空白紧凑版
    return out[:5]


def fuse_results(
    result_lists: list[list[SearchResult]], top_k: int = 1
) -> list[SearchResult]:
    """多路召回融合：按「各变体归一化分数的均值」排序（共识优先）。

    每个变体先按**自身最高分**归一，消除变体间的分数量纲差异；同一 chunk 在多个
    变体里出现就累加 —— 被越多变体共同召回的文档越靠前。这正是关键词堆场景需要的：
    正确条目会在「聚焦变体」里被召回，错误条目只在原句里露脸，累加后正确者胜出。

    融合分再除以变体数 → 落回 ``[0, 1]``，可直接当相关度展示。最后仍走
    :func:`_dedup_by_source` 收敛为「每来源 1 条」，与单路行为保持一致。
    """
    if not result_lists:
        return []

    agg: dict[str, list] = {}   # chunk_id -> [累加归一化分, 代表 SearchResult]
    used = 0
    for lst in result_lists:
        if not lst:
            continue
        used += 1
        max_score = max(r.score for r in lst) or 1.0
        for r in lst:
            slot = agg.get(r.chunk_id)
            if slot is None:
                agg[r.chunk_id] = [r.score / max_score, r]
            else:
                slot[0] += r.score / max_score

    if not agg:
        return []

    ranked = sorted(agg.values(), key=lambda x: x[0], reverse=True)
    for acc, r in ranked:
        r.score = acc / used
    return _dedup_by_source([r for _, r in ranked], top_k)


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

    def _candidates(
        self, query: str, product: str | None = None
    ) -> list[SearchResult]:
        """召回 + 混合重排（**未按来源去重**）。

        自原 ``_search_impl`` 原样抽出，仅把「去重」一步留给调用方：单路
        :meth:`_search_impl` 去重后返回；多路 :meth:`search_multi` 需要先在
        **去重之前**融合各变体的候选池（否则同商品的多条 FAQ 会被提前压成一条，
        正确条目还没参与排序就被丢掉）。
        """
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

        return results

    def _search_impl(
        self, query: str, top_k: int = 1, product: str | None = None
    ) -> list[SearchResult]:
        """Hybrid search with product-code filtering and multi-source dedup."""
        return _dedup_by_source(self._candidates(query, product), top_k)

    def search(
        self, query: str, top_k: int = 1, product: str | None = None
    ) -> list[SearchResult]:
        """Cache-Aside wrapper around :meth:`_search_impl`.

        RAG retrieval is the most expensive read in the system (query embedding
        + vector recall + BM25 re-rank), so results are cached by
        (query, top_k, product). The whole namespace is dropped when the
        knowledge base is re-ingested. On a Redis outage this silently
        degrades to a plain call — Redis is an optimisation, not a dependency.
        """
        key = cache.key(NS_FAQ, query, top_k, product or "")
        cached = cache.get(key)
        if cached is not None:
            return [_to_search_result(d) for d in cached]

        results = self._search_impl(query, top_k, product)
        cache.set(key, [_to_dict(r) for r in results], TTL_FAQ)
        return results

    def search_multi(
        self, query: str, top_k: int = 1, product: str | None = None
    ) -> list[SearchResult]:
        """多路召回融合（Cache-Aside）——「关键词堆」兜底，供 ``search_faq`` 使用。

        把 query 派生为若干聚焦变体各召回一次（去重前候选池），再按「共识」融合。
        变体数在自然问句下为 1，此时**直接委托** :meth:`search` —— 与旧行为完全等价、
        零额外开销，也不会绕过既有缓存；只有疑似关键词堆 / 多分句时才真正多路召回
        （最多 5 路），融合结果整体缓存，键与单路隔离（带 ``"multi"`` 前缀）。
        """
        variants = generate_query_variants(query)
        if len(variants) <= 1:
            return self.search(query, top_k, product)

        key = cache.key(NS_FAQ, "multi", query, top_k, product or "")
        cached = cache.get(key)
        if cached is not None:
            return [_to_search_result(d) for d in cached]

        pools = [self._candidates(v, product) for v in variants]
        results = fuse_results(pools, top_k)
        cache.set(key, [_to_dict(r) for r in results], TTL_FAQ)
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
        # Knowledge base changed → drop stale FAQ answers
        cache.invalidate(NS_FAQ)
        return len(chunks)


rag_store = RAGStore()

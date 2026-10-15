"""原型：FAQ 多路召回融合（方案 B 轻量版）策略验证。

不改 rag-storage 任何模块，只在这里复现 `_search_impl` 的**去重前候选池**，
对比几种融合策略在真实语料上的表现，选出最稳的一种再落地（结论：norm-avg 共识融合）。

跑法：PYTHONPATH=D:/Lib/site-packages D:/python.exe _scratch/probe_faq_fusion.py
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infrastructure.rag import retriever as R
from infrastructure.rag.embedding import embed_query
from infrastructure.rag.store import search as vsearch
from infrastructure.rag.config import config


# ── 变体生成 ────────────────────────────────────────────────────────────
def variants(query: str) -> list[str]:
    q = re.sub(r"\s+", " ", (query or "").strip())
    if not q:
        return []
    out: list[str] = []

    def add(v):
        v = re.sub(r"\s+", " ", (v or "")).strip()
        if v and v not in out:
            out.append(v)

    add(q)                                   # V1 原句
    segs = q.split()
    if len(segs) >= 3:                       # V2/V3 空格片段聚焦（关键词堆）
        add(" ".join(segs[-2:]))
        add(segs[-1])
    for seg in re.split(r"[，。！？、；,\.!\?;:]+", q):   # V4 分句
        add(seg)
    add(re.sub(r"\s+", "", q))               # V5 紧凑
    return out[:5]


# ── 去重前候选池（复现 _search_impl，去掉最后的 dedup）──────────────────
def candidates(q: str, product=None) -> list:
    qc = R._clean_query(q)
    kws = R._extract_keywords(q)
    codes = R._extract_product_codes(q.upper())
    vec = embed_query(qc)
    if product:
        vr = vsearch(vec, top_k=config.recall_k, where={"product": product})
    elif codes:
        vr = vsearch(vec, top_k=config.recall_k, where={"product_code": codes[0]})
        if R._has_warranty_intent(q):
            vr = vr + vsearch(vec, top_k=max(3, config.recall_k // 3), where={"product_code": ""})
    else:
        vr = vsearch(vec, top_k=config.recall_k)
    bm = R.rag_store._bm25.search((qc + " " + " ".join(kws)).strip(), top_k=config.recall_k)
    res = R._hybrid_rerank(vr, bm)
    if kws:
        for r in res:
            for k in kws:
                if k.lower() in r.text.lower():
                    r.score *= 1.1
        res.sort(key=lambda r: r.score, reverse=True)
    return res


# ── 三种融合策略 ────────────────────────────────────────────────────────
def fuse_rrf(k=60):
    """纯 rank 融合。"""
    def _f(lists):
        agg: dict[str, list] = {}
        for lst in lists:
            for rank, r in enumerate(lst, 1):
                agg.setdefault(r.chunk_id, [0.0, r])[0] += 1.0 / (k + rank)
        return [v[1] for v in sorted(agg.values(), key=lambda x: -x[0])]
    _f.__name__ = f"fuse_rrf_k{k}"
    return _f


def fuse_norm_sum(lists):
    """各变体分数按自身 max 归一后求和。"""
    agg: dict[str, list] = {}
    for lst in lists:
        if not lst:
            continue
        mx = max(r.score for r in lst) or 1.0
        for r in lst:
            agg.setdefault(r.chunk_id, [0.0, r])[0] += r.score / mx
    return [v[1] for v in sorted(agg.values(), key=lambda x: -x[0])]


def fuse_best(lists):
    """取各变体内的最高原始分（保分不变形）。"""
    agg: dict[str, list] = {}
    for lst in lists:
        for r in lst:
            if r.chunk_id not in agg or r.score > agg[r.chunk_id][1].score:
                agg[r.chunk_id] = [r.score, r]
    return [v[1] for v in sorted(agg.values(), key=lambda x: -x[0])]


def fuse_norm_avg(lists):
    """归一化和 ÷ 变体数 → 分数落在 [0,1]，可直接当相关度展示。"""
    agg: dict[str, list] = {}
    for lst in lists:
        if not lst:
            continue
        mx = max(r.score for r in lst) or 1.0
        for r in lst:
            agg.setdefault(r.chunk_id, [0.0, r])[0] += r.score / mx
    n = max(len(lists), 1)
    for v in agg.values():
        v[1].score = v[0] / n
    return [v[1] for v in sorted(agg.values(), key=lambda x: -x[0])]


def dedup_by_source(results, top_k):
    seen, out = set(), []
    for r in results:
        src = r.metadata.get("product", "")
        if src not in seen:
            seen.add(src)
            out.append(r)
        if len(out) >= top_k:
            break
    return out


# ── 评测 ────────────────────────────────────────────────────────────────
BATTERY = [
    ("X1 智能门锁 电池 续航", "电池能用多久"),
    ("X1 智能门锁 更换电池", "怎么更换电池"),
    ("X1 智能门锁的电池能续航多久", "电池能用多久"),
    ("怎么更换电池", "怎么更换电池"),
    ("智能门锁 电池 更换步骤", "怎么更换电池"),
    ("门锁电池能用多长时间", "电池"),
    ("X1设置键没反应", "设置键"),
    ("怎么重置 WiFi？", "重置"),
    ("X1设置键没反应，保修期内，退换还是维修", None),
    ("连不上 WiFi 怎么办？", "WiFi"),
    ("门锁一直滴滴响？", "滴滴响"),
    ("保修期多长", "保修"),
    ("X1 支持远程临时密码吗", "临时密码"),
    ("怎么查看开锁记录", "开锁记录"),
]


def run(fuse, baseline=False):
    ok = 0
    print(f"\n{'='*78}\n策略: {('BASELINE（单 query，现行为）' if baseline else fuse.__name__)}")
    for q, expect in BATTERY:
        vlist = variants(q)
        lists = [candidates(vlist[0])] if baseline else [candidates(v) for v in vlist]
        fused = dedup_by_source(fuse(lists), 3)
        top1 = fused[0].metadata.get("question", "") if fused else "<none>"
        if expect is None:
            mark = "?"
        else:
            hit = expect in top1 or any(expect in r.metadata.get("question", "") for r in fused)
            mark = "OK" if hit else "MISS"
            ok += 1 if hit else 0
        print(f"  [{mark:4}] {q!r:34} -> {top1!r}")
    print(f"  ---- 命中 {ok}/{sum(1 for _, e in BATTERY if e)} ----")
    return ok


if __name__ == "__main__":
    print("变体示例:")
    for q in ["X1 智能门锁 电池 续航", "X1 智能门锁 更换电池", "X1 智能门锁的电池能续航多久"]:
        print(f"  {q!r} -> {variants(q)}")
    run(fuse_norm_sum, baseline=True)
    for f in (fuse_rrf(60), fuse_rrf(20), fuse_rrf(10), fuse_norm_sum, fuse_norm_avg):
        run(f)

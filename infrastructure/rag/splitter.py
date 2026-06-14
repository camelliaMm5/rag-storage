import re
from .models import Document, Chunk
from .config import config

_QA_BOUNDARY = re.compile(r"^\*\*[^*]+\*\*\s*$", re.MULTILINE)

# Filler characters/symbols to strip from queries
_SENTENCE_BOUNDARY = re.compile(r"[。！？；\n](?=\S)")


def _is_question(line: str) -> bool:
    inner = line.strip().strip("*").strip()
    return inner.endswith("?") or inner.endswith("？")


def _faq_split(text: str, doc_id: str, metadata: dict) -> list[Chunk]:
    """Split text by FAQ Q&A boundaries (bold question lines)."""
    lines = text.splitlines()
    chunks = []
    current_lines: list[str] = []
    current_question: str = ""

    for line in lines:
        stripped = line.strip()
        bold_match = _QA_BOUNDARY.match(stripped)
        if bold_match and _is_question(stripped):
            if current_lines:
                chunk_text = "\n".join(current_lines).strip()
                if chunk_text:
                    chunk_meta = dict(metadata)
                    if current_question:
                        chunk_meta["question"] = current_question.strip("*").strip()
                    chunks.append(Chunk(
                        text=chunk_text, doc_id=doc_id, metadata=chunk_meta,
                    ))
            current_question = stripped
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        chunk_text = "\n".join(current_lines).strip()
        if chunk_text:
            chunk_meta = dict(metadata)
            if current_question:
                chunk_meta["question"] = current_question.strip("*").strip()
            chunks.append(Chunk(
                text=chunk_text, doc_id=doc_id, metadata=chunk_meta,
            ))

    return chunks


def _split_text_by_sentences(text: str, max_len: int) -> list[str]:
    """Split text at sentence boundaries, keeping each piece <= max_len."""
    pieces = []
    remaining = text
    while len(remaining) > max_len:
        # Find the last sentence boundary within max_len
        cut = max_len
        for m in _SENTENCE_BOUNDARY.finditer(remaining):
            if m.end() <= max_len:
                cut = m.end()
            else:
                break
        if cut == max_len:
            # Fallback: break at any separator
            for sep in ("\n", "，", ",", " "):
                pos = remaining.rfind(sep, 0, max_len)
                if pos > max_len // 2:
                    cut = pos + len(sep)
                    break
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _sub_chunk_long_answer(
    question: str, answer: str, doc_id: str, metadata: dict
) -> list[Chunk]:
    """Split a long answer into sub-chunks, each prefixed with the question."""
    max_len = config.chunk_size
    if len(answer) <= max_len:
        return []

    pieces = _split_text_by_sentences(answer, max_len)
    if len(pieces) <= 1:
        return []

    prefix = question + "\n"
    chunks = []
    for piece in pieces:
        chunk_meta = dict(metadata)
        chunk_meta["question"] = question.strip("*").strip()
        chunks.append(Chunk(
            text=prefix + piece,
            doc_id=doc_id,
            metadata=chunk_meta,
        ))
    return chunks


def _auto_split(text: str, doc_id: str, metadata: dict) -> list[Chunk]:
    """Detect FAQ pattern and split accordingly; fall back to recursive."""
    if _QA_BOUNDARY.search(text):
        qa_chunks = _faq_split(text, doc_id, metadata)
        result = []
        for c in qa_chunks:
            q = c.metadata.get("question", "")
            q_line = f"**{q}**" if q else ""
            # Separate question line from answer body
            answer = c.text
            if q_line and answer.startswith(q_line):
                answer = answer[len(q_line):].strip()
            if len(c.text) > config.chunk_size:
                sub = _sub_chunk_long_answer(q_line, answer, doc_id, c.metadata)
                if sub:
                    result.extend(sub)
                    continue
            result.append(c)
        return result

    return _fallback_split(text, doc_id, metadata)


def _fallback_split(text: str, doc_id: str, metadata: dict) -> list[Chunk]:
    """Recursive character split for non-FAQ text."""
    chunk_size = config.chunk_size
    chunk_overlap = config.chunk_overlap
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(Chunk(text=text[start:].strip(), doc_id=doc_id, metadata=dict(metadata)))
            break
        search_start = max(start, end - chunk_size // 5)
        best = end
        for sep in ("\n\n", "\n", "。", "，", ".", ",", " "):
            pos = text.rfind(sep, search_start, end)
            if pos > start:
                best = pos + len(sep)
                break
        chunk_text = text[start:best].strip()
        if chunk_text:
            chunks.append(Chunk(text=chunk_text, doc_id=doc_id, metadata=dict(metadata)))
        start = best - chunk_overlap if best - chunk_overlap > start else best
    return chunks


def split(document: Document) -> list[Chunk]:
    """Split a Document into a list of Chunks, auto-detecting FAQ structure."""
    return _auto_split(document.text, document.doc_id or "", document.metadata)


def split_batch(documents: list[Document]) -> list[Chunk]:
    """Split multiple Documents into Chunks."""
    all_chunks = []
    for doc in documents:
        all_chunks.extend(split(doc))
    return all_chunks

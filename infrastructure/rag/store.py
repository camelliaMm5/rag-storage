import uuid
import chromadb
from chromadb.config import Settings as ChromaSettings
from .config import config
from .models import Chunk, SearchResult

_client = None
_collection = None


def _get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(
            path=config.chroma_db_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _client


def _get_collection():
    global _collection
    if _collection is None:
        client = _get_client()
        _collection = client.get_or_create_collection(
            name=config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def add(chunks: list[Chunk], embeddings: list[list[float]]) -> list[str]:
    """Store chunks with embeddings. Returns list of generated chunk IDs."""
    collection = _get_collection()

    ids = []
    documents = []
    metadatas = []
    for chunk in chunks:
        chunk_id = chunk.chunk_id or str(uuid.uuid4())
        chunk.chunk_id = chunk_id
        ids.append(chunk_id)
        documents.append(chunk.text)
        flat_meta = {k: str(v) for k, v in chunk.metadata.items()}
        flat_meta["doc_id"] = chunk.doc_id
        metadatas.append(flat_meta)

    collection.add(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)
    return ids


def delete_by_doc_id(doc_id: str) -> int:
    """Remove all chunks belonging to a document. Returns count of deleted items."""
    collection = _get_collection()
    results = collection.get(where={"doc_id": doc_id})
    if results["ids"]:
        collection.delete(ids=results["ids"])
        return len(results["ids"])
    return 0


def search(
    query_embedding: list[float],
    top_k: int = 5,
    where: dict | None = None,
) -> list[SearchResult]:
    """Search for chunks by embedding vector. Returns top_k results."""
    collection = _get_collection()
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where,
    )

    search_results = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            search_results.append(
                SearchResult(
                    text=results["documents"][0][i],
                    score=1.0 - results["distances"][0][i],
                    metadata=results["metadatas"][0][i] if results["metadatas"] else {},
                    chunk_id=results["ids"][0][i],
                )
            )
    return search_results


def get_all() -> list[tuple[str, str]]:
    """Return all (chunk_id, text) pairs in the collection."""
    collection = _get_collection()
    if collection.count() == 0:
        return []
    results = collection.get()
    if results["ids"]:
        return list(zip(results["ids"], results["documents"] or []))
    return []


def count() -> int:
    """Return total number of chunks in the collection."""
    return _get_collection().count()


def reset():
    """Release all connections (useful for testing/cleanup)."""
    global _client, _collection
    _collection = None
    _client = None

from sentence_transformers import SentenceTransformer
from .config import config

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(config.embedding_model)
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """Convert texts to embedding vectors. Returns a list of float lists."""
    model = _get_model()
    result = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    return [vec.tolist() for vec in result]


def embed_query(text: str) -> list[float]:
    """Convenience: embed a single query string, return one vector."""
    return embed([text])[0]

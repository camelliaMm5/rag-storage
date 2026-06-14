from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Document:
    text: str
    metadata: dict = field(default_factory=dict)
    doc_id: Optional[str] = None


@dataclass
class Chunk:
    text: str
    doc_id: str
    metadata: dict = field(default_factory=dict)
    chunk_id: Optional[str] = None


@dataclass
class SearchResult:
    text: str
    score: float
    metadata: dict = field(default_factory=dict)
    chunk_id: str = ""

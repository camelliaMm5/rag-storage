import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
    )
    chroma_db_path: str = os.getenv("CHROMA_DB_PATH", "./chroma_db")
    collection_name: str = os.getenv("COLLECTION_NAME", "faq_knowledge")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "350"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "50"))
    recall_k: int = int(os.getenv("RECALL_K", "20"))
    docs_dir: str = os.getenv("DOCS_DIR", "./docs")


config = Config()

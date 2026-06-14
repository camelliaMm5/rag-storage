"""Database connection management via SQLAlchemy (PostgreSQL/SQLite)."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

CONVERSATION_DB_URL = os.getenv(
    "CONVERSATION_DB_URL",
    "sqlite:///./conversations.db",
)

if CONVERSATION_DB_URL.startswith("sqlite"):
    engine = create_engine(
        CONVERSATION_DB_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        CONVERSATION_DB_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

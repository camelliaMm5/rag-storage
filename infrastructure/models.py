"""SQLAlchemy ORM models for conversation persistence."""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, JSON, CheckConstraint, Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _gen_uuid() -> str:
    return str(uuid.uuid4())


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(36), default=_gen_uuid, unique=True, nullable=False)
    user_id = Column(String(128), nullable=False)
    title = Column(Text, default="")
    status = Column(String(16), default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')"),
        Index("idx_messages_conv_turn", "conversation_id", "turn_number"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String(36), nullable=False)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    turn_number = Column(Integer, nullable=False)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

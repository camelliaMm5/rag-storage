"""ConversationManager — session CRUD and context window management."""
import uuid
from datetime import datetime

from sqlalchemy import desc, func

from infrastructure.database import SessionLocal
from infrastructure.models import Base, Conversation, Message


class ConversationManager:
    def __init__(self, max_context_turns: int = 10):
        self.max_context_turns = max_context_turns
        Base.metadata.create_all(bind=SessionLocal.kw["bind"])

    def create(self, user_id: str) -> str:
        """Create a new conversation. Returns the conversation_id."""
        conv_id = str(uuid.uuid4())
        with SessionLocal() as db:
            conv = Conversation(conversation_id=conv_id, user_id=user_id)
            db.add(conv)
            db.commit()
        return conv_id

    def add_message(
        self, conversation_id: str, role: str, content: str, metadata: dict | None = None
    ) -> int:
        """Append a message. Auto-calculates turn_number by role. Returns message id."""
        with SessionLocal() as db:
            max_turn = (
                db.query(func.max(Message.turn_number))
                .filter(Message.conversation_id == conversation_id)
                .scalar()
            ) or 0

            if role == "user":
                turn_number = max_turn + 1
            else:
                turn_number = max_turn

            msg = Message(
                conversation_id=conversation_id,
                role=role,
                content=content,
                turn_number=turn_number,
                metadata_=metadata or {},
            )
            db.add(msg)
            db.flush()

            # Auto-title from first user message
            conv = (
                db.query(Conversation)
                .filter(Conversation.conversation_id == conversation_id)
                .first()
            )
            if conv and not conv.title and role == "user":
                conv.title = content[:80]

            conv.updated_at = datetime.utcnow()
            db.commit()
            return msg.id

    def get_context(self, conversation_id: str) -> list[dict]:
        """Return recent messages within context window, ordered by time ascending."""
        limit = self.max_context_turns * 2
        with SessionLocal() as db:
            rows = (
                db.query(Message)
                .filter(Message.conversation_id == conversation_id)
                .filter(Message.role.in_(["user", "assistant"]))
                .order_by(desc(Message.turn_number), desc(Message.created_at))
                .limit(limit)
                .all()
            )
        rows.reverse()
        return [
            {"role": r.role, "content": r.content, "turn_number": r.turn_number}
            for r in rows
        ]

    def get_history(self, conversation_id: str) -> list[dict]:
        """Return full conversation history (all messages)."""
        with SessionLocal() as db:
            rows = (
                db.query(Message)
                .filter(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
                .all()
            )
        return [
            {"role": r.role, "content": r.content, "turn_number": r.turn_number}
            for r in rows
        ]

    def list_conversations(self, user_id: str) -> list[dict]:
        """List all conversations for a user."""
        with SessionLocal() as db:
            rows = (
                db.query(Conversation)
                .filter(Conversation.user_id == user_id)
                .order_by(desc(Conversation.updated_at))
                .all()
            )
        return [
            {
                "conversation_id": c.conversation_id,
                "title": c.title,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in rows
        ]

    def close(self, conversation_id: str):
        """Mark a conversation as closed."""
        with SessionLocal() as db:
            conv = (
                db.query(Conversation)
                .filter(Conversation.conversation_id == conversation_id)
                .first()
            )
            if conv:
                conv.status = "closed"
                conv.updated_at = datetime.utcnow()
                db.commit()

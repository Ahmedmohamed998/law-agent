"""
The AI domain's tables. Everything lives in the `ai` schema.

Two rules hold this apart from the product database and are worth stating where
they are easy to break:

  1. `user_id` and `organization_id` are opaque strings taken from a verified
     token. There is deliberately NO foreign key to `public.users` — a
     cross-schema FK would mean coordinated migrations forever and would turn a
     later physical split into a project rather than a connection-string change.

  2. Retrieval provenance is *snapshotted*, not joined. `chunk_id` points into
     Chroma, which is a build artifact regenerated from the PDFs; ids move when
     documents are re-chunked. `citation` stores the display string as it was at
     answer time so an old answer never renders a dangling reference.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.ids import new_id

SCHEMA = "ai"

SESSION_STATUSES = ("active", "escalated", "closed")
MESSAGE_ROLES = ("user", "assistant")
MESSAGE_STATUSES = ("streaming", "complete", "error")
# `refused` is operationally different from `model_knowledge` — "the corpus has
# no answer and I declined" versus "I answered from training data" — and you
# want to count them separately when deciding which documents to add next.
SOURCE_LABELS = ("documents", "model_knowledge", "mixed", "refused", "escalate")
FEEDBACK_RATINGS = ("up", "down")
INPUT_MODES = ("text", "voice")


def _in(column: str, allowed: tuple[str, ...]) -> str:
    values = ", ".join(f"'{v}'" for v in allowed)
    return f"{column} IN ({values})"


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(_in("status", SESSION_STATUSES), name="ck_sessions_status"),
        Index("ix_sessions_user", "organization_id", "user_id", "last_active_at"),
        # Partial: the session list never wants soft-deleted rows, and this
        # keeps the index off them entirely.
        Index(
            "ix_sessions_active",
            "organization_id",
            "last_active_at",
            postgresql_where="deleted_at IS NULL",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    lang: Mapped[str | None] = mapped_column(String(8))
    title: Mapped[str | None] = mapped_column(Text)

    # Turns older than the live window, rolled up. `summary_upto_seq` records
    # how far the summary covers so the two never overlap or leave a gap.
    summary: Mapped[str | None] = mapped_column(Text)
    summary_upto_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Message.seq"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Deterministic ordering. Two messages can land in the same
        # millisecond, and a client sorting by timestamp will eventually
        # render a question after its own answer.
        UniqueConstraint("session_id", "seq", name="uq_messages_seq"),
        # Idempotency. The frontend will double-submit — retries, impatient
        # double-clicks, effects firing twice — and without this you get
        # duplicate questions and duplicate Azure charges.
        UniqueConstraint(
            "session_id", "client_message_id", name="uq_messages_client_id"
        ),
        CheckConstraint(_in("role", MESSAGE_ROLES), name="ck_messages_role"),
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="ck_messages_status"),
        CheckConstraint(_in("input_mode", INPUT_MODES), name="ck_messages_input_mode"),
        CheckConstraint(
            f"source_label IS NULL OR {_in('source_label', SOURCE_LABELS)}",
            name="ck_messages_source_label",
        ),
        Index("ix_messages_session", "session_id", "seq"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(f"{SCHEMA}.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    client_message_id: Mapped[str | None] = mapped_column(String(64))
    # Typed or spoken. Informational: the turn is answered identically either
    # way, which is why it is safe to take from the client.
    input_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="text")

    # Denormalised from the session so tenant filtering never needs a join —
    # a policy that can be evaluated on the row itself is one that cannot be
    # forgotten in a query.
    organization_id: Mapped[str | None] = mapped_column(String(128))

    # -- assistant turns only ---------------------------------------------
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    source_label: Mapped[str | None] = mapped_column(String(24))
    # {"resolved": str, "queries": [...], "origin": "planned|cached|fallback"}
    # Diagnostic, but JSONB so it stays queryable if it ever stops being.
    planned_queries: Mapped[dict | None] = mapped_column(JSONB)
    index_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))

    session: Mapped[Session] = relationship(back_populates="messages")
    sources: Mapped[list["MessageSource"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageSource.rank",
    )


class MessageSource(Base):
    """What the answer was grounded in, as it read at the time."""

    __tablename__ = "message_sources"
    __table_args__ = (
        Index("ix_sources_chunk", "chunk_id"),
        {"schema": SCHEMA},
    )

    message_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(f"{SCHEMA}.messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    doc_title: Mapped[str | None] = mapped_column(Text)
    article_label: Mapped[str | None] = mapped_column(Text)
    # hybrid | semantic | lexical | article_lookup | regulation_join
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(128))

    message: Mapped[Message] = relationship(back_populates="sources")


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(_in("rating", FEEDBACK_RATINGS), name="ck_feedback_rating"),
        UniqueConstraint("message_id", name="uq_feedback_message"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(f"{SCHEMA}.messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    organization_id: Mapped[str | None] = mapped_column(String(128))


class MessageUsage(Base):
    """How many messages a user has sent, across every conversation.

    A counter rather than a COUNT over messages: deleting a conversation is a
    soft delete, and a limit derived from visible messages is one anyone can
    reset by deleting a chat. See migration 0002 for the atomicity argument.
    """

    __tablename__ = "message_usage"
    __table_args__ = (
        CheckConstraint("messages_used >= 0", name="ck_message_usage_nonneg"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_key: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    messages_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

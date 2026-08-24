"""The wire contract. Versioned under /v1 and deliberately flat.

The frontend never sees a chunk id's semantics, Chroma, or an internal
retrieval reason beyond an opaque badge string.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceLabel = Literal["documents", "model_knowledge", "mixed", "refused"]


class CreateSession(BaseModel):
    lang: str | None = Field(default=None, max_length=8)


class SessionOut(BaseModel):
    session_id: str
    status: str
    title: str | None
    lang: str | None
    created_at: datetime
    last_active_at: datetime


class PostMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    # Required, not optional: the frontend will double-submit on retries and
    # double-clicks, and without a key that costs a duplicate Azure call and a
    # duplicate row.
    client_message_id: str = Field(min_length=6, max_length=64)


class SourceOut(BaseModel):
    citation: str
    doc_title: str | None = None
    article_label: str | None = None
    reason: str


class MessageOut(BaseModel):
    message_id: str
    seq: int
    role: str
    content: str
    source_label: SourceLabel | None = None
    sources: list[SourceOut] = []
    created_at: datetime | None = None


class AnswerOut(BaseModel):
    message_id: str
    seq: int
    content: str
    source_label: SourceLabel
    sources: list[SourceOut]
    latency_ms: int


class FeedbackIn(BaseModel):
    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=2000)


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody

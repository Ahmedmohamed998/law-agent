"""The wire contract. Versioned under /v1 and deliberately flat.

The frontend never sees a chunk id's semantics, Chroma, or an internal
retrieval reason beyond an opaque badge string.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Must list every label the model can emit and the database accepts. `escalate`
# was missing, so any conversation where someone asked to book failed
# validation on reload and the history endpoint answered 500.
SourceLabel = Literal["documents", "model_knowledge", "mixed", "refused", "escalate"]
InputMode = Literal["text", "voice"]


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
    # Whether the text came from a recording. Informational only.
    input_mode: InputMode = "text"


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
    input_mode: InputMode = "text"


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


class UsageOut(BaseModel):
    used: int
    # None means unlimited (staff).
    limit: int | None
    remaining: int | None
    anonymous: bool
    # What a registered account allows in total. Lets the widget tell an
    # anonymous visitor at their limit how many more signing in would give.
    registered_limit: int


class TranscriptOut(BaseModel):
    text: str
    duration_seconds: float


class AdminMessageOut(BaseModel):
    """A transcript row for staff. No sources: the dashboard shows the
    conversation, not retrieval diagnostics."""

    seq: int
    role: str
    content: str
    source_label: str | None = None
    input_mode: str = "text"
    status: str
    created_at: datetime | None = None


class AdminSessionOut(BaseModel):
    session_id: str
    user_id: str
    status: str
    lang: str | None
    title: str | None
    created_at: datetime
    messages: list[AdminMessageOut]
    voice_messages: int
    messages_used: int
    # The registered allowance. Paying clients are always registered, so this
    # is the limit that applies to anyone whose case reaches the dashboard.
    message_limit: int


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody

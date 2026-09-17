"""
Every read and write of the AI domain goes through here, and every method takes
a `Principal`. There is deliberately no method that can query without one — the
tenant filter is a property of the API surface rather than a rule to remember at
each call site. Row-level security is the second line; this is the first.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Feedback, Message, MessageSource, MessageUsage, Session
from app.db.session import Principal


class NotFound(Exception):
    """The session does not exist, or does not belong to this principal."""


class SessionEscalated(Exception):
    """The session was handed to a lawyer; the model must not answer on it."""


class QuotaExhausted(Exception):
    """The user has sent every message their allowance permits."""

    def __init__(self, *, anonymous: bool, limit: int, used: int):
        super().__init__(f"message allowance exhausted ({used}/{limit})")
        self.anonymous = anonymous
        self.limit = limit
        self.used = used


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One retrieved chunk, as it read at answer time."""

    chunk_id: str
    citation: str
    reason: str
    doc_title: str | None = None
    article_label: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── sessions ──────────────────────────────────────────────────────────────


def create_session(
    db: SASession, principal: Principal, *, lang: str | None = None
) -> Session:
    row = Session(
        user_id=principal.user_id,
        organization_id=principal.organization_id,
        lang=lang,
    )
    db.add(row)
    db.flush()
    return row


def get_session(db: SASession, principal: Principal, session_id: str) -> Session:
    row = db.scalar(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == principal.user_id,
            Session.deleted_at.is_(None),
        )
    )
    if row is None:
        # Same error whether it never existed or belongs to someone else —
        # distinguishing them leaks which session ids are real.
        raise NotFound(session_id)
    return row


def list_sessions(
    db: SASession, principal: Principal, *, limit: int = 30, before: datetime | None = None
) -> list[Session]:
    stmt = (
        select(Session)
        .where(Session.user_id == principal.user_id, Session.deleted_at.is_(None))
        .order_by(Session.last_active_at.desc())
        .limit(min(limit, 100))
    )
    if before is not None:
        stmt = stmt.where(Session.last_active_at < before)
    return list(db.scalars(stmt))


def soft_delete_session(db: SASession, principal: Principal, session_id: str) -> None:
    row = get_session(db, principal, session_id)
    row.deleted_at = _now()


# ── history ───────────────────────────────────────────────────────────────


def messages(
    db: SASession, principal: Principal, session_id: str, *, limit: int = 100
) -> list[Message]:
    get_session(db, principal, session_id)  # authorises, and 404s if not ours
    return list(
        db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.seq)
            .limit(limit)
            .options(selectinload(Message.sources))
        )
    )


def recent_turns(
    db: SASession, principal: Principal, session_id: str, *, turns: int
) -> list[Message]:
    """The last `turns` exchanges, oldest first — what the query planner reads.

    Capped rather than unbounded: past about four turns an earlier scenario's
    story starts polluting the plan for a question about a new topic.
    """
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.session_id == session_id, Message.status == "complete")
            .order_by(Message.seq.desc())
            .limit(turns * 2)
            .options(selectinload(Message.sources))
        )
    )
    return list(reversed(rows))


# ── message allowance ─────────────────────────────────────────────────────


def message_limit(principal: Principal) -> int | None:
    """The total this caller may send. None means unlimited."""
    s = settings()
    if not principal.anonymous and principal.role in s.unlimited_roles:
        return None
    return s.anon_message_limit if principal.anonymous else s.registered_message_limit


def _usage_table():
    return MessageUsage.__table__


def message_usage(db: SASession, principal: Principal) -> tuple[int, int | None]:
    """(used, limit) for this caller."""
    t = _usage_table()
    used = db.scalar(
        select(t.c.messages_used).where(
            t.c.user_id == principal.user_id, t.c.org_key == principal.org_key
        )
    )
    return int(used or 0), message_limit(principal)


def reserve_message(db: SASession, principal: Principal) -> int:
    """Consume one message of allowance, or raise QuotaExhausted.

    One conditional UPDATE is the whole check. Two tabs sending at once both
    reach it; Postgres serialises them on the row lock and re-evaluates the
    WHERE for the second, so the limit cannot be overshot by a race the way a
    read-then-write would allow.
    """
    t = _usage_table()
    limit = message_limit(principal)

    db.execute(
        pg_insert(t)
        .values(user_id=principal.user_id, org_key=principal.org_key, messages_used=0)
        .on_conflict_do_nothing(index_elements=["user_id", "org_key"])
    )

    stmt = update(t).where(
        t.c.user_id == principal.user_id, t.c.org_key == principal.org_key
    )
    if limit is not None:
        stmt = stmt.where(t.c.messages_used < limit)
    used = db.scalar(
        stmt.values(messages_used=t.c.messages_used + 1, updated_at=func.now())
        .returning(t.c.messages_used)
    )
    if used is None:
        current, _ = message_usage(db, principal)
        raise QuotaExhausted(
            anonymous=principal.anonymous, limit=limit or 0, used=current
        )
    return int(used)


def refund_message(db: SASession, principal: Principal) -> None:
    """Give one back. For answers that failed: a visitor should not lose
    allowance to an outage they had no part in."""
    t = _usage_table()
    db.execute(
        update(t)
        .where(
            t.c.user_id == principal.user_id,
            t.c.org_key == principal.org_key,
            t.c.messages_used > 0,
        )
        .values(messages_used=t.c.messages_used - 1, updated_at=func.now())
    )


def get_answer(db: SASession, principal: Principal, message_id: str) -> Message:
    """A finished assistant message belonging to this caller, or NotFound."""
    row = db.scalar(
        select(Message)
        .join(Session, Session.id == Message.session_id)
        .where(
            Message.id == message_id,
            Message.role == "assistant",
            Message.status == "complete",
            Session.user_id == principal.user_id,
            Session.deleted_at.is_(None),
        )
    )
    if row is None:
        raise NotFound(message_id)
    return row


def session_for_staff(db: SASession, session_id: str) -> tuple[Session, list[Message], int]:
    """A conversation and its owner's message count, for the dashboard.

    Cross-tenant, like escalate_any: the reader is staff looking at a client's
    case, and the client has no organization. Includes soft-deleted
    conversations — a client deleting a chat does not remove it from a case a
    lawyer has already been paid to take.
    """
    session = db.scalar(select(Session).where(Session.id == session_id))
    if session is None:
        raise NotFound(session_id)
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.seq)
        )
    )
    t = _usage_table()
    used = db.scalar(
        select(t.c.messages_used).where(
            t.c.user_id == session.user_id,
            t.c.org_key == (session.organization_id or ""),
        )
    )
    return session, rows, int(used or 0)


def discard_failed_answer(db: SASession, principal: Principal, message_id: str) -> None:
    """Remove an assistant row whose generation failed, so a retry can answer
    afresh instead of replaying the failure."""
    row = db.scalar(
        select(Message)
        .join(Session, Session.id == Message.session_id)
        .where(
            Message.id == message_id,
            Message.status == "error",
            Session.user_id == principal.user_id,
        )
    )
    if row is not None:
        db.delete(row)
        db.flush()


# ── writing a turn ────────────────────────────────────────────────────────


def _next_seq(db: SASession, session_id: str) -> int:
    # Lock the session row first: two concurrent messages in the same session
    # would otherwise read the same max(seq) and collide on uq_messages_seq.
    db.execute(
        select(Session.id).where(Session.id == session_id).with_for_update()
    ).one()
    current = db.scalar(
        select(func.max(Message.seq)).where(Message.session_id == session_id)
    )
    return (current or 0) + 1


def add_user_message(
    db: SASession,
    principal: Principal,
    session_id: str,
    *,
    content: str,
    client_message_id: str,
    input_mode: str = "text",
) -> tuple[Message, bool]:
    """Append the user's turn. Returns (message, created).

    `created=False` means this exact `client_message_id` was already stored, so
    the caller should return the existing answer instead of paying Azure again.
    The frontend will double-submit; this is where that stops being expensive.
    """
    session = get_session(db, principal, session_id)
    if session.status == "escalated":
        raise SessionEscalated(session_id)

    existing = db.scalar(
        select(Message).where(
            Message.session_id == session_id,
            Message.client_message_id == client_message_id,
        )
    )
    if existing is not None:
        # A replay is not a new message: it returns an answer already paid
        # for, so it must neither consume allowance nor be refused for lack
        # of it.
        return existing, False

    # Checked here, beside the escalation guard and before anything costs
    # money. Raises QuotaExhausted; the increment rolls back with the rest of
    # the transaction if the insert below loses a race.
    reserve_message(db, principal)

    row = Message(
        session_id=session_id,
        organization_id=session.organization_id,
        seq=_next_seq(db, session_id),
        role="user",
        content=content,
        client_message_id=client_message_id,
        input_mode=input_mode if input_mode in ("text", "voice") else "text",
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with a concurrent identical submit; the other one wins.
        db.rollback()
        existing = db.scalar(
            select(Message).where(
                Message.session_id == session_id,
                Message.client_message_id == client_message_id,
            )
        )
        if existing is None:
            raise
        return existing, False

    session.last_active_at = _now()
    if session.title is None:
        session.title = content[:120]
    return row, True


def add_assistant_message(
    db: SASession,
    principal: Principal,
    session_id: str,
    *,
    content: str,
    source_label: str | None,
    sources: list[SourceRow],
    planned_queries: dict | None = None,
    index_version: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    status: str = "complete",
    error_code: str | None = None,
) -> Message:
    session = get_session(db, principal, session_id)
    row = Message(
        session_id=session_id,
        organization_id=session.organization_id,
        seq=_next_seq(db, session_id),
        role="assistant",
        content=content,
        source_label=source_label,
        planned_queries=planned_queries,
        index_version=index_version,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code,
    )
    db.add(row)
    db.flush()

    for rank, s in enumerate(sources, 1):
        db.add(
            MessageSource(
                message_id=row.id,
                rank=rank,
                chunk_id=s.chunk_id,
                citation=s.citation,
                doc_title=s.doc_title,
                article_label=s.article_label,
                reason=s.reason,
                organization_id=session.organization_id,
            )
        )
    session.last_active_at = _now()
    db.flush()
    return row


def set_suggestion(
    db: SASession,
    principal: Principal,
    message_id: str,
    *,
    service: str,
    reason: str,
    confidence: float,
) -> None:
    """Record what the assistant proposed under an answer it owns."""
    row = db.scalar(
        select(Message)
        .join(Session, Session.id == Message.session_id)
        .where(Message.id == message_id, Session.user_id == principal.user_id)
    )
    if row is None:
        raise NotFound(message_id)
    row.suggested_service = service
    row.suggestion_reason = reason
    row.suggestion_confidence = confidence


def suggested_slugs(db: SASession, principal: Principal, session_id: str) -> set[str]:
    """Every service already proposed in this conversation. Once each."""
    get_session(db, principal, session_id)
    rows = db.scalars(
        select(Message.suggested_service).where(
            Message.session_id == session_id,
            Message.suggested_service.is_not(None),
        )
    )
    return {r for r in rows if r}


def last_answer_suggested(db: SASession, principal: Principal, session_id: str) -> bool:
    """Did the previous assistant turn carry a suggestion? Two in a row is
    a sales pitch, not help."""
    row = db.scalar(
        select(Message.suggested_service)
        .where(Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.seq.desc())
        .limit(1)
    )
    return bool(row)


def mark_suggestion_clicked(db: SASession, principal: Principal, message_id: str) -> None:
    """The client pressed the card. First press only; a second is noise."""
    row = db.scalar(
        select(Message)
        .join(Session, Session.id == Message.session_id)
        .where(Message.id == message_id, Session.user_id == principal.user_id)
    )
    if row is None or not row.suggested_service:
        raise NotFound(message_id)
    if row.suggestion_clicked_at is None:
        row.suggestion_clicked_at = _now()


def finish_message(
    db: SASession,
    principal: Principal,
    message_id: str,
    *,
    content: str,
    source_label: str | None,
    latency_ms: int,
    status: str = "complete",
    error_code: str | None = None,
) -> Message:
    """Fill in a row that was reserved with status='streaming'.

    The placeholder is written before generation starts so the client gets a
    message_id in its first event; this is the other half of that.
    """
    row = db.scalar(
        select(Message)
        .join(Session, Session.id == Message.session_id)
        .where(Message.id == message_id, Session.user_id == principal.user_id)
    )
    if row is None:
        raise NotFound(message_id)
    row.content = content
    row.source_label = source_label
    row.latency_ms = latency_ms
    row.status = status
    row.error_code = error_code
    db.flush()
    return row


def escalate(db: SASession, principal: Principal, session_id: str) -> None:
    session = get_session(db, principal, session_id)
    session.status = "escalated"


def escalate_any(db: SASession, session_id: str) -> Session:
    """Escalate without a Principal. Cross-tenant by design.

    Takes no Principal because the caller is not a person: it is the payments
    service, reacting to a gateway webhook that carries no user identity at
    all. It runs under `admin_session()` for the same reason `purge_user` does.

    Idempotent — escalating an already-escalated session is a no-op, which
    matters because payment gateways retry their webhooks.
    """
    row = db.scalar(
        select(Session).where(
            Session.id == session_id, Session.deleted_at.is_(None)
        )
    )
    if row is None:
        raise NotFound(session_id)
    row.status = "escalated"
    return row


# ── feedback ──────────────────────────────────────────────────────────────


def add_feedback(
    db: SASession,
    principal: Principal,
    message_id: str,
    *,
    rating: str,
    reason: str | None = None,
) -> Feedback:
    # Join through the session so a message id from another tenant cannot be
    # rated — RLS would already block it, but failing here gives a clean 404.
    owned = db.scalar(
        select(Message.id)
        .join(Session, Session.id == Message.session_id)
        .where(Message.id == message_id, Session.user_id == principal.user_id)
    )
    if owned is None:
        raise NotFound(message_id)

    row = db.scalar(select(Feedback).where(Feedback.message_id == message_id))
    if row is None:
        row = Feedback(message_id=message_id, rating=rating, reason=reason)
        db.add(row)
    else:
        row.rating, row.reason = rating, reason
    db.flush()
    return row


# ── erasure ───────────────────────────────────────────────────────────────


def purge_user(db: SASession, user_id: str) -> int:
    """Hard-delete everything belonging to a user. Cross-tenant by design.

    Takes no Principal because it is not a request-path operation: it runs
    under `admin_session()` in response to the product backend telling us a
    user exercised erasure. There is no foreign key from `ai.sessions` to
    `public.users`, so nothing cascades on their side — this is the contract
    that replaces it.
    """
    rows = list(db.scalars(select(Session).where(Session.user_id == user_id)))
    for row in rows:
        db.delete(row)  # messages -> sources/feedback cascade on delete
    t = _usage_table()
    db.execute(t.delete().where(t.c.user_id == user_id))
    return len(rows)

"""
One conversational turn: history -> plan -> retrieve -> generate -> persist.

Deliberately free of FastAPI and HTTP types. The API layer is a caller of this
module, exactly like the Gradio harness could be, so the code path the eval
suite exercises is the code path the API runs.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from sqlalchemy.orm import Session as SASession

from app.rag.bedrock_client import (
    INFERENCE_PROFILE,
    bedrock_chat,
    bedrock_chat_stream,
)

from app.config import settings
from app.db import repository as repo
from app.db.repository import SourceRow
from app.db.session import Principal
from app.rag.answer import build_messages, split_label, split_partial
from app.rag.retriever import RetrievalRequest, RetrievedChunk, Retriever

# A callable opening one short transaction bound to this request's tenant —
# `functools.partial(scoped_session, principal)` in the API layer.
Scope = Callable[[], AbstractContextManager[SASession]]


@dataclass
class TurnResult:
    message_id: str
    seq: int
    content: str
    source_label: str
    sources: list[dict]
    latency_ms: int
    reused: bool = False  # idempotent replay of an already-answered submit


@dataclass
class _Prepared:
    """Everything decided before the model is called."""

    user_msg_id: str
    chunks: tuple[RetrievedChunk, ...] = ()
    sources: list[dict] = field(default_factory=list)
    dialogue: list[dict] = field(default_factory=list)
    plan: dict | None = None
    index_version: str | None = None
    reused: TurnResult | None = None


# Bedrock client is instantiated per-call inside bedrock_chat / bedrock_chat_stream.


def _source_dicts(chunks) -> list[dict]:
    return [
        {
            "chunk_id": c.chunk_id,
            "citation": c.citation,
            "reason": c.reason,
            "doc_title": c.metadata.get("doc_title"),
            "article_label": c.metadata.get("article_label"),
        }
        for c in chunks
    ]


def _planner_history(turns) -> tuple[tuple[str, str], ...]:
    """What the query planner sees: user turns verbatim, assistant turns
    reduced to the citations they leaned on.

    A previous answer is several hundred tokens of Arabic legal prose. Its
    citations are a couple of dozen tokens, carry the same topic signal, and
    cannot drag the old answer's wording into a new search query.
    """
    out: list[tuple[str, str]] = []
    for m in turns:
        if m.role == "user":
            out.append(("user", m.content))
        elif m.sources:
            out.append(("assistant", " · ".join(s.citation for s in m.sources[:4])))
    return tuple(out)


def _prepare(
    db: SASession,
    principal: Principal,
    retriever: Retriever,
    session_id: str,
    content: str,
    client_message_id: str,
) -> _Prepared:
    user_msg, created = repo.add_user_message(
        db, principal, session_id, content=content,
        client_message_id=client_message_id,
    )
    if not created:
        # Already answered: return the stored assistant turn rather than
        # paying Azure a second time for a double-click.
        prior = [
            m for m in repo.messages(db, principal, session_id)
            if m.role == "assistant" and m.seq > user_msg.seq
        ]
        if prior:
            a = prior[0]
            return _Prepared(
                user_msg_id=user_msg.id,
                reused=TurnResult(
                    message_id=a.id, seq=a.seq, content=a.content,
                    source_label=a.source_label or "documents",
                    sources=[
                        {"chunk_id": s.chunk_id, "citation": s.citation,
                         "reason": s.reason, "doc_title": s.doc_title,
                         "article_label": s.article_label}
                        for s in a.sources
                    ],
                    latency_ms=a.latency_ms or 0,
                    reused=True,
                ),
            )

    turns = [
        m for m in repo.recent_turns(
            db, principal, session_id, turns=settings().history_turns
        )
        if m.id != user_msg.id
    ]
    history = _planner_history(turns)
    # The last user message id fingerprints the history prefix: message rows
    # are immutable, so it is a sound cache key and costs nothing to compute.
    history_key = next((m.id for m in reversed(turns) if m.role == "user"), "")

    result = retriever.run(
        RetrievalRequest(raw=content, history=history, history_key=history_key)
    )
    return _Prepared(
        user_msg_id=user_msg.id,
        chunks=result.chunks,
        sources=_source_dicts(result.chunks),
        dialogue=[{"role": m.role, "content": m.content} for m in turns],
        plan=result.plan.as_dict(),
        index_version=result.index_version,
    )


def _persist(
    db: SASession,
    principal: Principal,
    session_id: str,
    prep: _Prepared,
    *,
    text: str,
    label: str,
    latency_ms: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    status: str = "complete",
    error_code: str | None = None,
):
    return repo.add_assistant_message(
        db, principal, session_id,
        content=text,
        source_label=label,
        sources=[
            SourceRow(
                chunk_id=s["chunk_id"], citation=s["citation"], reason=s["reason"],
                doc_title=s["doc_title"], article_label=s["article_label"],
            )
            for s in prep.sources
        ],
        planned_queries=prep.plan,
        index_version=prep.index_version,
        model=INFERENCE_PROFILE,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code,
    )


def _finish(
    db: SASession,
    principal: Principal,
    message_id: str,
    *,
    text: str,
    label: str | None,
    latency_ms: int,
    status: str = "complete",
    error_code: str | None = None,
):
    repo.finish_message(
        db, principal, message_id, content=text, source_label=label,
        latency_ms=latency_ms, status=status, error_code=error_code,
    )


def take_turn(
    scope: Scope,
    principal: Principal,
    retriever: Retriever,
    *,
    session_id: str,
    content: str,
    client_message_id: str,
) -> TurnResult:
    started = time.perf_counter()
    with scope() as db:
        prep = _prepare(db, principal, retriever, session_id, content,
                        client_message_id)
    if prep.reused is not None:
        return prep.reused

    raw = bedrock_chat(
        build_messages(content, list(prep.chunks), prep.dialogue),
    )
    label, text = split_label(raw, bool(prep.chunks))
    latency = int((time.perf_counter() - started) * 1000)

    with scope() as db:
        row = _persist(
            db, principal, session_id, prep, text=text, label=label,
            latency_ms=latency,
        )
        message_id, seq = row.id, row.seq
    return TurnResult(
        message_id=message_id, seq=seq, content=text, source_label=label,
        sources=prep.sources, latency_ms=latency,
    )


def stream_turn(
    scope: Scope,
    principal: Principal,
    retriever: Retriever,
    *,
    session_id: str,
    content: str,
    client_message_id: str,
) -> Iterator[dict]:
    """Yields SSE-shaped events: meta, sources, token…, done | error.

    Three transactions, none of them spanning the model call. Holding one open
    across ~17 seconds of generation would pin a pooled connection per
    in-flight chat and idle-in-transaction its way through the pool under any
    real concurrency.
    """
    started = time.perf_counter()
    with scope() as db:
        prep = _prepare(db, principal, retriever, session_id, content,
                        client_message_id)
        if prep.reused is None:
            # Reserve the row now, empty and marked `streaming`. It gives the
            # client a message_id in the first event, and it means an answer
            # the user watched half of still exists on reload if the
            # connection dies mid-stream.
            row = _persist(db, principal, session_id, prep, text="", label=None,
                           latency_ms=0, status="streaming")
            message_id, seq = row.id, row.seq

    if prep.reused is not None:
        r = prep.reused
        yield {"event": "meta", "data": {"message_id": r.message_id, "seq": r.seq,
                                         "replayed": True}}
        yield {"event": "sources", "data": {"sources": r.sources}}
        yield {"event": "token", "data": {"delta": r.content}}
        yield {"event": "done", "data": {"source_label": r.source_label,
                                         "latency_ms": r.latency_ms}}
        return

    yield {"event": "meta", "data": {"message_id": message_id, "seq": seq}}
    # Before the first token on purpose: retrieval takes under a second and
    # generation about seventeen, so citations render while the answer types.
    yield {"event": "sources", "data": {"sources": prep.sources}}

    acc = ""
    try:
        stream = bedrock_chat_stream(
            build_messages(content, list(prep.chunks), prep.dialogue),
        )
        emitted = 0
        for event in stream:
            if not event.choices:
                continue
            piece = getattr(event.choices[0].delta, "content", None)
            if not piece:
                continue
            acc += piece
            _, text, holding = split_partial(acc, bool(prep.chunks))
            if holding:
                continue  # the [[SOURCE: …]] marker is still arriving
            # Send only the new tail, so the client appends instead of
            # re-rendering the whole answer on every token.
            if len(text) > emitted:
                yield {"event": "token", "data": {"delta": text[emitted:]}}
                emitted = len(text)
    except Exception as exc:  # upstream failure mid-generation
        label, text = split_label(acc, bool(prep.chunks))
        with scope() as db:
            _finish(db, principal, message_id, text=text, label=label,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status="error", error_code=type(exc).__name__)
        yield {"event": "error",
               "data": {"code": "upstream_unavailable", "message": str(exc)[:200]}}
        return

    label, text = split_label(acc, bool(prep.chunks))
    latency = int((time.perf_counter() - started) * 1000)
    with scope() as db:
        _finish(db, principal, message_id, text=text, label=label,
                latency_ms=latency)
    yield {"event": "done", "data": {"source_label": label, "latency_ms": latency}}

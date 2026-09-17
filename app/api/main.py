"""
FastAPI surface for the AI service.

Two shape decisions that are easy to get wrong and expensive to undo:

  * The Retriever is built ONCE, in the lifespan handler, and hung on
    app.state. It holds a BM25 index over every chunk in the corpus, so a
    per-request build is seconds of latency and a `--workers 4` deploy is four
    copies of it in memory. Scale threads first, processes only after
    measuring.

  * Endpoints are `def`, not `async def`. The OpenAI and Chroma clients are
    synchronous; declaring these coroutines would block the event loop for the
    whole ~17s generation and stall every other request. As plain `def`,
    Starlette runs them in its threadpool, where blocking is fine.
"""

import functools
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.api.auth import admin_key, staff_principal
from app.api.auth import principal as current_principal
from app.api.schemas import (
    AdminMessageOut,
    AdminSessionOut,
    AnswerOut,
    CreateSession,
    FeedbackIn,
    MessageOut,
    PostMessage,
    SessionOut,
    SourceOut,
    TranscriptOut,
    UsageOut,
)
from app.chat import stream_turn, take_turn
from app.config import settings
from app.db import repository as repo
from app.db.repository import NotFound, QuotaExhausted, SessionEscalated
from app.db.session import Principal, admin_session, engine, scoped_session
from app.rag.retriever import Retriever
from app.rag.summarize import summarize_escalation
from app.voice.speech import SpeechUnavailable, audio_cache, synthesize, transcribe
from app.voice.text import BadAudio, read_wav

log = logging.getLogger("law_agent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine()  # fail fast on a bad DATABASE_URL rather than on first request
    app.state.retriever = Retriever()
    log.info("retriever ready, index_version=%s", app.state.retriever.index_version)
    yield
    app.state.retriever = None


app = FastAPI(
    title="Law Agent AI Service",
    version="1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

# The frontend calls this service directly for chat, because proxying SSE
# through the product API buffers it: the whole answer then lands at once
# after ~17s instead of streaming. That makes CORS ours to handle.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings().cors_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code,
                                                               "message": message}})


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    """One envelope for everything, as docs/API.md §4 promises.

    Without this, an HTTPException raised with a dict detail comes back as
    `{"detail": {"code": ...}}` rather than `{"error": {"code": ...}}` — so
    every 401 in this service contradicted its own documented contract, and a
    client branching on `error.code` silently saw undefined.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        body = {"error": {"code": detail["code"],
                          "message": detail.get("message", "")}}
    else:
        body = {"error": {"code": "error", "message": str(detail)}}
    return JSONResponse(status_code=exc.status_code, content=body,
                        headers=exc.headers)


@app.exception_handler(RequestValidationError)
def _validation_error(request: Request, exc: RequestValidationError):
    """422 in the same envelope.

    docs/API.md §4 lists this as a known gap and tells the frontend to branch
    on the HTTP status for it. That instruction can now be deleted.
    """
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(p) for p in first.get("loc", ())[1:]) or "body"
    return _error(422, "invalid_request",
                  f"{location}: {first.get('msg', 'invalid value')}")


@app.exception_handler(NotFound)
def _not_found(request: Request, exc: NotFound):
    return _error(404, "session_not_found", "no such session")


@app.exception_handler(SessionEscalated)
def _escalated(request: Request, exc: SessionEscalated):
    # The circuit breaker: once a session is with a lawyer the model must not
    # answer on it at all. Checked before any Azure call is made.
    return _error(409, "session_escalated",
                  "this conversation has been handed to a lawyer")


_QUOTA_MESSAGE = "message allowance exhausted"


@app.exception_handler(QuotaExhausted)
def _quota(request: Request, exc: QuotaExhausted):
    # 429, raised before retrieval and before the model: a refused message
    # costs nothing. The widget decides what to offer from whether the caller
    # is anonymous (sign in) or not (a paid service), which it already knows.
    return _error(429, "message_quota_exhausted", _QUOTA_MESSAGE)


def retriever(request: Request) -> Retriever:
    return request.app.state.retriever


def _sources(rows) -> list[SourceOut]:
    return [
        SourceOut(
            citation=s["citation"], doc_title=s.get("doc_title"),
            article_label=s.get("article_label"), reason=s["reason"],
        )
        for s in rows
    ]


# ── health ────────────────────────────────────────────────────────────────


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz(request: Request):
    """Ready means the index is loaded and the pool can hand out a connection —
    not merely that the process is up."""
    r = getattr(request.app.state, "retriever", None)
    if r is None:
        return _error(503, "not_ready", "retriever not built")
    try:
        with engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:
        return _error(503, "not_ready", f"database unavailable: {exc}"[:200])
    return {"ok": True, "index_version": r.index_version}


# ── sessions ──────────────────────────────────────────────────────────────


@app.post("/v1/sessions", response_model=SessionOut, status_code=201)
def create_session(body: CreateSession, p: Principal = Depends(current_principal)):
    with scoped_session(p) as db:
        s = repo.create_session(db, p, lang=body.lang)
        return SessionOut(
            session_id=s.id, status=s.status, title=s.title, lang=s.lang,
            created_at=s.created_at, last_active_at=s.last_active_at,
        )


@app.get("/v1/sessions", response_model=list[SessionOut])
def list_sessions(limit: int = 30, p: Principal = Depends(current_principal)):
    with scoped_session(p) as db:
        return [
            SessionOut(
                session_id=s.id, status=s.status, title=s.title, lang=s.lang,
                created_at=s.created_at, last_active_at=s.last_active_at,
            )
            for s in repo.list_sessions(db, p, limit=limit)
        ]


@app.delete("/v1/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, p: Principal = Depends(current_principal)):
    with scoped_session(p) as db:
        repo.soft_delete_session(db, p, session_id)


@app.get("/v1/sessions/{session_id}/messages", response_model=list[MessageOut])
def history(session_id: str, p: Principal = Depends(current_principal)):
    with scoped_session(p) as db:
        return [
            MessageOut(
                message_id=m.id, seq=m.seq, role=m.role, content=m.content,
                source_label=m.source_label, created_at=m.created_at,
                input_mode=m.input_mode or "text",
                suggested_service=m.suggested_service,
                suggestion_reason=m.suggestion_reason,
                sources=[
                    SourceOut(citation=s.citation, doc_title=s.doc_title,
                              article_label=s.article_label, reason=s.reason)
                    for s in m.sources
                ],
            )
            for m in repo.messages(db, p, session_id)
        ]


# ── the turn ──────────────────────────────────────────────────────────────


@app.post("/v1/sessions/{session_id}/messages", response_model=AnswerOut)
def post_message(
    session_id: str,
    body: PostMessage,
    p: Principal = Depends(current_principal),
    r: Retriever = Depends(retriever),
):
    result = take_turn(
        functools.partial(scoped_session, p), p, r,
        session_id=session_id, content=body.content,
        client_message_id=body.client_message_id, input_mode=body.input_mode,
    )
    return AnswerOut(
        message_id=result.message_id, seq=result.seq, content=result.content,
        source_label=result.source_label, sources=_sources(result.sources),
        latency_ms=result.latency_ms, suggestion=result.suggestion,
    )


@app.post("/v1/sessions/{session_id}/messages/stream")
def post_message_stream(
    session_id: str,
    body: PostMessage,
    p: Principal = Depends(current_principal),
    r: Retriever = Depends(retriever),
):
    def events():
        try:
            for ev in stream_turn(
                functools.partial(scoped_session, p), p, r,
                session_id=session_id, content=body.content,
                client_message_id=body.client_message_id,
                input_mode=body.input_mode,
            ):
                yield (
                    f"event: {ev['event']}\n"
                    f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
                )
        except NotFound:
            yield _sse_error("session_not_found", "no such session")
        except SessionEscalated:
            yield _sse_error("session_escalated",
                             "this conversation has been handed to a lawyer")
        except QuotaExhausted:
            yield _sse_error("message_quota_exhausted", _QUOTA_MESSAGE)
        except Exception as exc:  # never leave the client waiting on a dead stream
            log.exception("stream failed")
            yield _sse_error("internal_error", str(exc)[:200])

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers text/event-stream by default, which turns a
            # streamed answer into one lump after 17 seconds. This is the
            # header that stops it; `proxy_buffering off` is the other half.
            "X-Accel-Buffering": "no",
        },
    )


def _sse_error(code: str, message: str) -> str:
    payload = json.dumps({"code": code, "message": message}, ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n"


# ── feedback & escalation ─────────────────────────────────────────────────


@app.post("/v1/messages/{message_id}/feedback", status_code=204)
def feedback(message_id: str, body: FeedbackIn,
             p: Principal = Depends(current_principal)):
    with scoped_session(p) as db:
        repo.add_feedback(db, p, message_id, rating=body.rating, reason=body.reason)


@app.post("/v1/sessions/{session_id}/escalate", status_code=204)
def escalate(session_id: str, p: Principal = Depends(current_principal)):
    with scoped_session(p) as db:
        repo.escalate(db, p, session_id)


# ── message allowance ─────────────────────────────────────────────────────


@app.get("/v1/usage", response_model=UsageOut)
def usage(p: Principal = Depends(current_principal)):
    """How much of their allowance the caller has left, for the widget's counter."""
    with scoped_session(p) as db:
        used, limit = repo.message_usage(db, p)
    return UsageOut(
        used=used,
        limit=limit,
        remaining=None if limit is None else max(0, limit - used),
        anonymous=p.anonymous,
        registered_limit=settings().registered_message_limit,
    )


# ── voice ─────────────────────────────────────────────────────────────────

# 60 s of 16 kHz mono 16-bit PCM is 1.92 MB; a little headroom for the header,
# no more. nginx enforces a matching cap in front of this.
_MAX_AUDIO_BYTES = 2_200_000


def _remaining(p: Principal) -> int | None:
    with scoped_session(p) as db:
        used, limit = repo.message_usage(db, p)
    return None if limit is None else max(0, limit - used)


@app.post("/v1/transcribe", response_model=TranscriptOut)
async def transcribe_recording(request: Request, p: Principal = Depends(current_principal)):
    """A recording in, its text out. Stores nothing.

    Separate from sending a message on purpose. The text comes back to the
    widget, the visitor sees what was heard and can correct it, and only then
    does it go through the ordinary message path: the same allowance, the same
    idempotency, the same retrieval. A mis-heard legal question answered unseen
    is worse than one extra tap.

    `async`, unlike the rest of this file: the Transcribe SDK is asyncio-native,
    and the one blocking call (the allowance lookup) runs in the threadpool.
    """
    remaining = await run_in_threadpool(_remaining, p)
    if remaining == 0:
        # Transcription costs money too. No allowance, no transcription.
        raise QuotaExhausted(anonymous=p.anonymous, limit=0, used=0)

    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in ("audio/wav", "audio/x-wav", "audio/wave"):
        raise HTTPException(
            status_code=415,
            detail={"code": "unsupported_audio", "message": "send audio/wav"},
        )

    body = await request.body()
    if len(body) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "audio_too_large", "message": "recording too long"},
        )

    try:
        pcm = read_wav(body, max_seconds=settings().voice_max_seconds)
    except BadAudio as exc:
        raise HTTPException(
            status_code=422, detail={"code": "bad_audio", "message": str(exc)}
        ) from exc

    try:
        text = await transcribe(pcm)
    except SpeechUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "speech_unavailable", "message": str(exc)},
        ) from exc

    if not text:
        raise HTTPException(
            status_code=422,
            detail={"code": "no_speech", "message": "nothing intelligible was said"},
        )
    return TranscriptOut(text=text, duration_seconds=round(pcm.seconds, 1))


@app.post("/v1/messages/{message_id}/suggestion-click", status_code=204)
def suggestion_click(message_id: str, p: Principal = Depends(current_principal)):
    """The client pressed the suggested-service card under this answer.

    A measurement, nothing more: with the order's link to the conversation
    it closes the loop suggested -> clicked -> bought. Idempotent; the first
    press is the one that counts.
    """
    with scoped_session(p) as db:
        repo.mark_suggestion_clicked(db, p, message_id)


@app.get("/v1/messages/{message_id}/audio")
def message_audio(message_id: str, p: Principal = Depends(current_principal)):
    """An answer read aloud, as MP3.

    Only finished answers that belong to the caller. It reads the stored text
    and generates nothing new, so it cannot say what the page does not.
    """
    try:
        with scoped_session(p) as db:
            content = repo.get_answer(db, p, message_id).content
    except NotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "message_not_found", "message": "no such answer"},
        ) from None

    s = settings()
    key = f"{message_id}:{s.polly_voice_id}:{s.polly_engine}"
    audio = audio_cache.get(key)
    if audio is None:
        try:
            audio = synthesize(content)
        except SpeechUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "speech_unavailable", "message": str(exc)},
            ) from exc
        if not audio:
            raise HTTPException(
                status_code=422,
                detail={"code": "nothing_to_read",
                        "message": "answer has no speakable text"},
            )
        audio_cache.put(key, audio)
    # No Cache-Control: nginx sets no-store on this host, and a second header
    # would contradict it. The widget keeps its own copy for the page view.
    return Response(content=audio, media_type="audio/mpeg")


# ── staff ─────────────────────────────────────────────────────────────────


@app.get("/v1/staff/sessions/{session_id}", response_model=AdminSessionOut)
def staff_session(session_id: str, p: Principal = Depends(staff_principal)):
    """A client's conversation, for the dashboard.

    Authorized by the staff member's own token, forwarded by the product
    backend. Never by the admin key: the service-to-service endpoints below
    promise they cannot read a conversation, and that promise stays true.

    Cross-tenant through admin_session, because the reader (a firm admin) and
    the client (no organization) are in different tenants. Correct while this
    deployment serves one firm; a second firm would need the case to carry the
    firm, and this check to compare against it.
    """
    with admin_session() as db:
        session, rows, used = repo.session_for_staff(db, session_id)
        out = AdminSessionOut(
            session_id=session.id,
            user_id=session.user_id,
            status=session.status,
            lang=session.lang,
            title=session.title,
            created_at=session.created_at,
            messages=[
                AdminMessageOut(
                    seq=m.seq, role=m.role, content=m.content,
                    source_label=m.source_label,
                    input_mode=m.input_mode or "text",
                    status=m.status, created_at=m.created_at,
                    suggested_service=m.suggested_service,
                    suggestion_reason=m.suggestion_reason,
                    suggestion_confidence=m.suggestion_confidence,
                    suggestion_clicked=m.suggestion_clicked_at is not None,
                )
                for m in rows
            ],
            voice_messages=sum(
                1 for m in rows if m.role == "user" and m.input_mode == "voice"
            ),
            messages_used=used,
            message_limit=settings().registered_message_limit,
        )
    log.info("staff read session=%s by=%s", session_id, p.user_id)
    return out


# ── service-to-service ────────────────────────────────────────────────────
#
# Two endpoints the *other* services call, authenticated with a shared key
# rather than a user token. Both are deliberately outside the per-user model:
# erasure is cross-tenant by definition, and a payment webhook carries no user
# identity at all. Neither can read a conversation.


@app.delete("/v1/users/{user_id}/data", dependencies=[Depends(admin_key)])
def erase_user_data(user_id: str):
    """Delete everything this service holds for a user.

    The contract that replaces the missing foreign key. There is no FK from
    `ai.sessions` to the product backend's users table, so nothing cascades
    when a user is deleted there — this is how we are told.

    Idempotent: purging an already-purged user deletes nothing and still
    returns 200, so the caller can retry a failed delivery without special
    casing. `product/api/admin.py` relies on exactly that.
    """
    with admin_session() as db:
        sessions_deleted = repo.purge_user(db, user_id)
    log.info("erased user=%s sessions=%d", user_id, sessions_deleted)
    return {"user_id": user_id, "sessions_deleted": sessions_deleted}


@app.post("/v1/admin/sessions/{session_id}/escalate", status_code=200,
          dependencies=[Depends(admin_key)])
def admin_escalate(session_id: str):
    """Hand a session to a lawyer, on behalf of a service.

    This is what a paid consultation triggers. The payments service receives a
    gateway webhook — which carries no user token, and cannot, because the
    gateway does not know our users — and flips the session here.

    Idempotent, because payment gateways retry their webhooks and a second
    delivery must not be an error.
    """
    with admin_session() as db:
        session = repo.escalate_any(db, session_id)
        # Generate summary dynamically using the existing chat history
        summary_data = summarize_escalation(session.messages)
        # Store in db for AI history context (optional, but good)
        session.lang = summary_data.get("chat_language")
        # We don't overwrite the main 'summary' column since it's used for rolling up
        # but we return this dict to the Node backend
        db.commit()
        return summary_data


if __name__ == "__main__":
    import uvicorn

    s = settings()
    # One worker, many threads — see the module docstring.
    uvicorn.run(
        "app.api.main:app", host=s.bind_host, port=s.bind_port, workers=1
    )

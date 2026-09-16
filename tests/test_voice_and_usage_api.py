"""
The HTTP surface of the allowance, voice and staff endpoints, with the
database and AWS replaced. What this proves is the wiring: which status and
error code each situation produces, that nothing costs money when it should
be refused, and who may read a conversation. The SQL itself is exercised
against real Postgres on the server.
"""

import io
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import app.api.auth as auth
import app.api.main as main
from app.db import repository as repo
from app.db.session import Principal

ANON = Principal(user_id="u_anon", anonymous=True, role=None)
CLIENT = Principal(user_id="u_client", role="client")
ADMIN = Principal(user_id="u_admin", organization_id="org1", role="admin")


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def fake_scope(_principal):
        yield object()

    monkeypatch.setattr(main, "scoped_session", fake_scope)
    monkeypatch.setattr(main, "admin_session", lambda: fake_scope(None))
    main.app.dependency_overrides.clear()
    yield TestClient(main.app, raise_server_exceptions=False)
    main.app.dependency_overrides.clear()


def as_(principal):
    main.app.dependency_overrides[main.current_principal] = lambda: principal


def _wav(seconds=1.5, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))
    return buf.getvalue()


# ── limits ────────────────────────────────────────────────────────────────


def test_limits_are_5_anonymous_20_registered_unlimited_staff():
    assert repo.message_limit(ANON) == 5
    assert repo.message_limit(CLIENT) == 20
    assert repo.message_limit(ADMIN) is None
    # An anonymous token claiming a staff role is still anonymous.
    assert repo.message_limit(Principal(user_id="x", anonymous=True, role="admin")) == 5


def test_usage_reports_remaining(client, monkeypatch):
    as_(CLIENT)
    monkeypatch.setattr(repo, "message_usage", lambda db, p: (17, 20))
    r = client.get("/v1/usage")
    assert r.status_code == 200
    assert r.json() == {"used": 17, "limit": 20, "remaining": 3, "anonymous": False,
                        "registered_limit": 20}


def test_usage_for_staff_is_unlimited(client, monkeypatch):
    as_(ADMIN)
    monkeypatch.setattr(repo, "message_usage", lambda db, p: (400, None))
    assert client.get("/v1/usage").json()["remaining"] is None


def test_exhausted_allowance_is_a_429_in_the_standard_envelope(client, monkeypatch):
    as_(ANON)

    def exhausted(*a, **k):
        raise repo.QuotaExhausted(anonymous=True, limit=5, used=5)

    monkeypatch.setattr(main, "take_turn", exhausted)
    main.app.dependency_overrides[main.retriever] = lambda: None
    r = client.post(
        "/v1/sessions/s1/messages",
        json={"content": "سؤال", "client_message_id": "abcdef1"},
    )
    assert r.status_code == 429
    assert r.json() == {"error": {"code": "message_quota_exhausted",
                                  "message": "message allowance exhausted"}}


def test_exhausted_allowance_arrives_as_an_sse_error_when_streaming(client, monkeypatch):
    as_(CLIENT)

    def exhausted(*a, **k):
        raise repo.QuotaExhausted(anonymous=False, limit=20, used=20)
        yield  # pragma: no cover — makes this a generator, like stream_turn

    monkeypatch.setattr(main, "stream_turn", exhausted)
    main.app.dependency_overrides[main.retriever] = lambda: None
    r = client.post(
        "/v1/sessions/s1/messages/stream",
        json={"content": "سؤال", "client_message_id": "abcdef2"},
    )
    assert "event: error" in r.text
    assert "message_quota_exhausted" in r.text


def test_input_mode_is_accepted_and_validated(client, monkeypatch):
    as_(CLIENT)
    seen = {}

    def fake_take_turn(scope, p, r, **kw):
        seen.update(kw)
        return SimpleNamespace(message_id="m1", seq=2, content="جواب",
                               source_label="documents", sources=[], latency_ms=5)

    monkeypatch.setattr(main, "take_turn", fake_take_turn)
    main.app.dependency_overrides[main.retriever] = lambda: None
    ok = client.post("/v1/sessions/s1/messages",
                     json={"content": "سؤال", "client_message_id": "abcdef3",
                           "input_mode": "voice"})
    assert ok.status_code == 200 and seen["input_mode"] == "voice"
    bad = client.post("/v1/sessions/s1/messages",
                      json={"content": "سؤال", "client_message_id": "abcdef4",
                            "input_mode": "telepathy"})
    assert bad.status_code == 422


def test_quota_sql_compiles_for_postgres():
    # The statements are built inside reserve_message; build the same shapes
    # here so a typo in a column name fails in CI rather than on the server.
    t = repo._usage_table()
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert
    ins = insert(t).values(user_id="u", org_key="", messages_used=0) \
        .on_conflict_do_nothing(index_elements=["user_id", "org_key"])
    upd = update(t).where(t.c.user_id == "u", t.c.messages_used < 5) \
        .values(messages_used=t.c.messages_used + 1).returning(t.c.messages_used)
    for stmt in (ins, upd):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ai.message_usage" in sql


# ── transcription ─────────────────────────────────────────────────────────


def test_transcribe_returns_text(client, monkeypatch):
    as_(CLIENT)
    monkeypatch.setattr(main, "_remaining", lambda p: 5)

    async def fake(pcm):
        return "كم مدة الإجازة السنوية"

    monkeypatch.setattr(main, "transcribe", fake)
    r = client.post("/v1/transcribe", content=_wav(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200
    assert r.json() == {"text": "كم مدة الإجازة السنوية", "duration_seconds": 1.5}


def test_no_allowance_means_no_paid_transcription(client, monkeypatch):
    as_(ANON)
    monkeypatch.setattr(main, "_remaining", lambda p: 0)
    called = []

    async def fake(pcm):
        called.append(True)
        return "x"

    monkeypatch.setattr(main, "transcribe", fake)
    r = client.post("/v1/transcribe", content=_wav(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 429
    assert called == []


def test_transcribe_rejects_wrong_type_bad_audio_and_silence(client, monkeypatch):
    as_(CLIENT)
    monkeypatch.setattr(main, "_remaining", lambda p: 5)

    async def silence(pcm):
        return ""

    monkeypatch.setattr(main, "transcribe", silence)
    assert client.post("/v1/transcribe", content=_wav(),
                       headers={"Content-Type": "audio/webm"}).status_code == 415
    bad = client.post("/v1/transcribe", content=_wav(rate=44100),
                      headers={"Content-Type": "audio/wav"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_audio"
    quiet = client.post("/v1/transcribe", content=_wav(),
                        headers={"Content-Type": "audio/wav"})
    assert quiet.status_code == 422 and quiet.json()["error"]["code"] == "no_speech"


def test_transcribe_provider_failure_is_a_503(client, monkeypatch):
    as_(CLIENT)
    monkeypatch.setattr(main, "_remaining", lambda p: 5)

    async def down(pcm):
        raise main.SpeechUnavailable("AccessDenied")

    monkeypatch.setattr(main, "transcribe", down)
    r = client.post("/v1/transcribe", content=_wav(), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "speech_unavailable"


# ── reading aloud ─────────────────────────────────────────────────────────


def test_audio_is_mp3_and_cached(client, monkeypatch):
    as_(CLIENT)
    monkeypatch.setattr(repo, "get_answer",
                        lambda db, p, mid: SimpleNamespace(content="مدة الإجازة 21 يوماً."))
    calls = []

    def fake_synth(text):
        calls.append(text)
        return b"ID3fake"

    monkeypatch.setattr(main, "synthesize", fake_synth)
    main.audio_cache._items.clear()
    first = client.get("/v1/messages/m_audio/audio")
    second = client.get("/v1/messages/m_audio/audio")
    assert first.status_code == 200 and first.headers["content-type"] == "audio/mpeg"
    assert first.content == second.content == b"ID3fake"
    assert len(calls) == 1


def test_audio_of_someone_elses_message_is_a_404(client, monkeypatch):
    as_(CLIENT)

    def not_ours(db, p, mid):
        raise repo.NotFound(mid)

    monkeypatch.setattr(repo, "get_answer", not_ours)
    r = client.get("/v1/messages/m_other/audio")
    assert r.status_code == 404 and r.json()["error"]["code"] == "message_not_found"


# ── staff reading a conversation ──────────────────────────────────────────


@pytest.mark.parametrize("who", [ANON, CLIENT,
                                 Principal(user_id="l", role="lawyer")])
def test_only_owner_or_admin_may_read_a_conversation(monkeypatch, who):
    monkeypatch.setattr(auth, "principal", lambda authorization: who)
    with pytest.raises(HTTPException) as exc:
        auth.staff_principal("Bearer x")
    assert exc.value.status_code == 403


def test_admin_reads_transcript_with_voice_count(client, monkeypatch):
    main.app.dependency_overrides[main.staff_principal] = lambda: ADMIN
    now = datetime.now(timezone.utc)
    session = SimpleNamespace(id="s1", user_id="u_client", status="escalated",
                              lang="Arabic", title="سؤال", created_at=now)
    rows = [
        SimpleNamespace(seq=1, role="user", content="سؤال مكتوب", source_label=None,
                        input_mode="text", status="complete", created_at=now),
        SimpleNamespace(seq=2, role="assistant", content="جواب", source_label="documents",
                        input_mode="text", status="complete", created_at=now),
        SimpleNamespace(seq=3, role="user", content="سؤال بالصوت", source_label=None,
                        input_mode="voice", status="complete", created_at=now),
    ]
    monkeypatch.setattr(repo, "session_for_staff", lambda db, sid: (session, rows, 7))
    r = client.get("/v1/staff/sessions/s1")
    assert r.status_code == 200
    body = r.json()
    assert body["voice_messages"] == 1
    assert body["messages_used"] == 7
    assert body["message_limit"] == 20
    assert [m["input_mode"] for m in body["messages"]] == ["text", "text", "voice"]


def test_admin_key_alone_cannot_read_a_conversation(client):
    # No token, only the service key: the staff endpoint must not accept it.
    r = client.get("/v1/staff/sessions/s1", headers={"X-Admin-Key": "anything"})
    assert r.status_code in (401, 503)

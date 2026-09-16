"""
When a turn consumes allowance, gives it back, or charges again.

These are the rules a visitor feels as fairness: an outage must not cost them a
message, a retry of a failed answer must actually answer, and a double-click
must not cost twice.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.chat.turns as turns
from app.db.session import Principal

P = Principal(user_id="u1", role="client")


class Ledger:
    """Records what the turn did to allowance and rows."""

    def __init__(self):
        self.refunds = 0
        self.reserves = 0
        self.discarded = []
        self.persisted = []
        self.finished = []


@pytest.fixture
def ledger(monkeypatch):
    led = Ledger()

    @contextmanager
    def scope():
        yield object()

    led.scope = scope

    monkeypatch.setattr(turns.repo, "refund_message",
                        lambda db, p: setattr(led, "refunds", led.refunds + 1))
    monkeypatch.setattr(turns.repo, "reserve_message",
                        lambda db, p: setattr(led, "reserves", led.reserves + 1))
    monkeypatch.setattr(turns.repo, "discard_failed_answer",
                        lambda db, p, mid: led.discarded.append(mid))
    monkeypatch.setattr(turns.repo, "recent_turns", lambda *a, **k: [])
    monkeypatch.setattr(turns, "build_messages", lambda *a, **k: [])

    def persist(db, p, sid, **kw):
        led.persisted.append(kw)
        return SimpleNamespace(id=f"a{len(led.persisted)}", seq=10 + len(led.persisted))

    monkeypatch.setattr(turns.repo, "add_assistant_message", persist)
    monkeypatch.setattr(turns.repo, "finish_message",
                        lambda db, p, mid, **kw: led.finished.append((mid, kw)))
    return led


def retriever():
    result = SimpleNamespace(chunks=(), plan=SimpleNamespace(as_dict=lambda: {}),
                             index_version="v1")
    return SimpleNamespace(run=lambda req: result)


def new_message(monkeypatch):
    user = SimpleNamespace(id="q1", seq=1, role="user")
    monkeypatch.setattr(turns.repo, "add_user_message",
                        lambda db, p, sid, **kw: (user, True))
    monkeypatch.setattr(turns.repo, "messages", lambda *a, **k: [user])


def test_a_failed_streamed_answer_gives_the_message_back(ledger, monkeypatch):
    new_message(monkeypatch)

    def boom(*a, **k):
        raise TimeoutError("bedrock")

    monkeypatch.setattr(turns, "bedrock_chat_stream", boom)
    events = list(turns.stream_turn(ledger.scope, P, retriever(), session_id="s",
                                    content="سؤال", client_message_id="c1"))
    assert events[-1]["event"] == "error"
    assert ledger.refunds == 1
    assert ledger.finished[-1][1]["status"] == "error"


def test_a_failed_blocking_answer_is_recorded_and_refunded(ledger, monkeypatch):
    new_message(monkeypatch)

    def boom(*a, **k):
        raise TimeoutError("bedrock")

    monkeypatch.setattr(turns, "bedrock_chat", boom)
    with pytest.raises(TimeoutError):
        turns.take_turn(ledger.scope, P, retriever(), session_id="s",
                        content="سؤال", client_message_id="c1")
    assert ledger.refunds == 1
    assert ledger.persisted[-1]["status"] == "error"


def test_a_successful_answer_keeps_the_charge(ledger, monkeypatch):
    new_message(monkeypatch)
    monkeypatch.setattr(turns, "bedrock_chat",
                        lambda *a, **k: "[[SOURCE: documents]]\nجواب")
    r = turns.take_turn(ledger.scope, P, retriever(), session_id="s",
                        content="سؤال", client_message_id="c1")
    assert r.content == "جواب"
    assert ledger.refunds == 0


def test_retrying_a_failed_answer_answers_again_and_charges_once(ledger, monkeypatch):
    user = SimpleNamespace(id="q1", seq=1, role="user")
    failed = SimpleNamespace(id="a_failed", seq=2, role="assistant", status="error",
                             content="", source_label=None, sources=[], latency_ms=0)
    monkeypatch.setattr(turns.repo, "add_user_message",
                        lambda db, p, sid, **kw: (user, False))  # same client id
    monkeypatch.setattr(turns.repo, "messages", lambda *a, **k: [user, failed])
    monkeypatch.setattr(turns, "bedrock_chat",
                        lambda *a, **k: "[[SOURCE: documents]]\nجواب جديد")

    r = turns.take_turn(ledger.scope, P, retriever(), session_id="s",
                        content="سؤال", client_message_id="c1")
    assert r.content == "جواب جديد"          # not a replay of the failure
    assert ledger.discarded == ["a_failed"]
    assert ledger.reserves == 1


def test_a_double_click_on_a_good_answer_replays_it_for_free(ledger, monkeypatch):
    user = SimpleNamespace(id="q1", seq=1, role="user")
    good = SimpleNamespace(id="a_good", seq=2, role="assistant", status="complete",
                           content="جواب", source_label="documents", sources=[],
                           latency_ms=900)
    monkeypatch.setattr(turns.repo, "add_user_message",
                        lambda db, p, sid, **kw: (user, False))
    monkeypatch.setattr(turns.repo, "messages", lambda *a, **k: [user, good])

    def must_not_call(*a, **k):
        raise AssertionError("the model must not be called for a replay")

    monkeypatch.setattr(turns, "bedrock_chat", must_not_call)
    r = turns.take_turn(ledger.scope, P, retriever(), session_id="s",
                        content="سؤال", client_message_id="c1")
    assert r.reused and r.content == "جواب"
    assert ledger.reserves == 0 and ledger.refunds == 0

"""
The assistant proposing a service: the gates, the parser, the catalogue's
failure policy, and the turn wiring. No model anywhere in here; the one
test that needs a verdict fakes it.

The evaluation set (tests/suggestions.jsonl) is checked for shape here and
run against the real model by `python -m tests.eval_suggestions`, which is
deliberately not part of the suite: it costs money and needs credentials.
"""

import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.suggest import catalog as catalog_mod
from app.suggest import classify, engine
from app.suggest.catalog import Catalog, Service

CONTRACT = Service(slug="contract-review", name="مراجعة عقد", description="",
                   price_cents=30000, currency="SAR",
                   ai_hint="عندما يريد العميل فهم أو تعديل عقد قبل توقيعه", suggestable=True)
DISPUTE = Service(slug="dispute", name="إنهاء نزاع", description="",
                  price_cents=80000, currency="SAR",
                  ai_hint="عندما يوجد خلاف قائم مع صاحب العمل حول مستحقات أو فصل",
                  suggestable=True)
QUIET = Service(slug="quiet", name="خدمة صامتة", description="", price_cents=1,
                currency="SAR", ai_hint="", suggestable=True)
OFF = Service(slug="off", name="موقوفة", description="", price_cents=1,
              currency="SAR", ai_hint="شيء", suggestable=False)

OFFERABLE = (CONTRACT, DISPUTE)


# ── the gates ───────────────────────────────────────────────────────────


def gate(content, **kw):
    base = dict(staff=False, already=set(), last_answer_suggested=False, offerable=OFFERABLE)
    base.update(kw)
    return classify.should_consider(content, **base)


def test_a_real_need_passes_the_gates():
    assert gate("الشركة أعطتني عقدًا جديدًا وأريد فهم البنود قبل التوقيع")


def test_greetings_and_thanks_are_not_needs():
    assert not gate("شكرًا")
    assert not gate("مرحبا كيف الحال")


def test_staff_are_never_sold_to():
    assert not gate("الشركة أعطتني عقدًا جديدًا وأريد فهم البنود", staff=True)


def test_no_two_suggestions_in_a_row():
    assert not gate("الشركة أعطتني عقدًا جديدًا وأريد فهم البنود", last_answer_suggested=True)


def test_nothing_left_to_offer_means_no_call():
    assert not gate("الشركة أعطتني عقدًا جديدًا وأريد فهم البنود",
                    already={"contract-review", "dispute"})


def test_services_without_a_hint_or_switched_off_are_not_offerable():
    assert not QUIET.offerable
    assert not OFF.offerable
    assert CONTRACT.offerable


def test_rescue_mode_only_when_the_answer_could_not_help():
    assert classify.allowed_now(mode="rescue", source_label="refused")
    assert classify.allowed_now(mode="rescue", source_label="escalate")
    assert not classify.allowed_now(mode="rescue", source_label="documents")
    assert classify.allowed_now(mode="upsell", source_label="documents")


# ── the verdict ─────────────────────────────────────────────────────────


def test_a_listed_slug_above_threshold_is_a_suggestion():
    s = classify.parse_verdict(
        '{"service": "contract-review", "confidence": 0.9, "reason_ar": "تريد فهم بنود عقد قبل توقيعه"}',
        OFFERABLE,
    )
    assert s is not None
    assert s.service is CONTRACT
    assert s.confidence == 0.9
    assert s.reason == "تريد فهم بنود عقد قبل توقيعه"
    assert s.as_event()["price_cents"] == 30000


def test_prose_around_the_json_is_tolerated():
    s = classify.parse_verdict(
        'بالتأكيد، إليك النتيجة:\n```json\n{"service": "dispute", "confidence": 0.8, "reason_ar": "x"}\n```',
        OFFERABLE,
    )
    assert s is not None and s.service is DISPUTE


@pytest.mark.parametrize("text", [
    "",
    "لا أعرف",
    '{"service": null, "confidence": 0.9}',
    '{"service": "null", "confidence": 0.9}',
    '{"service": "made-up-service", "confidence": 0.99, "reason_ar": "x"}',  # not in the list
    '{"service": "contract-review", "confidence": 0.5, "reason_ar": "x"}',   # below threshold
    '{"service": "contract-review", "confidence": "high"}',
    '{"service": "contract-review", "confidence": 1.7}',
    '["contract-review"]',
    '{"service": 12, "confidence": 0.9}',
])
def test_anything_else_is_no_suggestion(text):
    assert classify.parse_verdict(text, OFFERABLE) is None


def test_the_threshold_is_configuration(monkeypatch):
    monkeypatch.setattr(settings(), "suggest_min_confidence", 0.4)
    s = classify.parse_verdict('{"service": "contract-review", "confidence": 0.5}', OFFERABLE)
    assert s is not None


def test_the_prompt_lists_only_offerable_services_with_their_hints():
    msgs = classify.build_prompt("سؤال", [("user", "سابق"), ("assistant", "جواب")], OFFERABLE)
    user = msgs[1]["content"]
    assert "contract-review — مراجعة عقد" in user
    assert CONTRACT.ai_hint in user
    assert "العميل: سابق" in user and "المساعد: جواب" in user
    assert msgs[0]["role"] == "system"


# ── the catalogue ───────────────────────────────────────────────────────


def test_no_backend_url_means_no_catalogue_and_no_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(catalog_mod.httpx, "get", lambda *a, **k: calls.append(a))
    c = Catalog(base_url="", ttl=1)
    assert c.services() == ()
    assert calls == []


def test_the_catalogue_is_fetched_once_per_ttl(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [{"slug": "contract-review", "name": "مراجعة عقد", "price_cents": 30000,
                           "currency": "SAR", "ai_hint": "عقد", "suggestable": True},
                          {"slug": "broken"},  # tolerated, still a service
                          {"name": "no slug at all"}],  # dropped
        )

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)
    c = Catalog(base_url="https://be.example/", ttl=3600)
    assert [s.slug for s in c.services()] == ["contract-review", "broken"]
    assert [s.slug for s in c.offerable()] == ["contract-review"]
    c.services()
    assert calls == ["https://be.example/services"]


def test_a_dead_backend_keeps_the_last_copy(monkeypatch):
    good = SimpleNamespace(raise_for_status=lambda: None,
                           json=lambda: [{"slug": "contract-review", "price_cents": 1}])

    def boom(url, timeout):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(catalog_mod.httpx, "get", lambda url, timeout: good)
    c = Catalog(base_url="https://be.example", ttl=0)
    assert c.get("contract-review") is not None
    monkeypatch.setattr(catalog_mod.httpx, "get", boom)
    assert c.get("contract-review") is not None  # stale, but still there


# ── the engine ──────────────────────────────────────────────────────────


def principal(role="client", anonymous=False):
    return SimpleNamespace(user_id="u1", role=role, anonymous=anonymous, organization_id=None)


def test_start_returns_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(settings(), "backend_url", "")
    assert engine.start(principal(), "الشركة أعطتني عقدًا جديدًا وأريد فهم البنود", [],
                        already=set(), last_answer_suggested=False) is None


def test_start_runs_the_classifier_off_thread_and_collect_returns_it(monkeypatch):
    monkeypatch.setattr(settings(), "backend_url", "https://be.example")
    cat = Catalog(base_url="https://be.example", ttl=3600)
    cat.load([{"slug": "contract-review", "name": "مراجعة عقد", "price_cents": 30000,
               "ai_hint": "عقد", "suggestable": True}])
    monkeypatch.setattr(engine, "catalog", lambda: cat)
    seen = {}

    def fake_classify(content, history, offerable):
        seen["offerable"] = [s.slug for s in offerable]
        return classify.Suggestion(service=offerable[0], reason="تريد مراجعة عقد", confidence=0.9)

    monkeypatch.setattr(engine, "classify", fake_classify)
    fut = engine.start(principal(), "الشركة أعطتني عقدًا جديدًا وأريد فهم البنود", [],
                       already=set(), last_answer_suggested=False)
    assert isinstance(fut, Future)
    s = engine.collect(fut)
    assert s is not None and s.service.slug == "contract-review"
    assert seen["offerable"] == ["contract-review"]


def test_an_already_suggested_service_is_left_out(monkeypatch):
    monkeypatch.setattr(settings(), "backend_url", "https://be.example")
    cat = Catalog(base_url="https://be.example", ttl=3600)
    cat.load([{"slug": "contract-review", "price_cents": 1, "ai_hint": "عقد"},
              {"slug": "dispute", "price_cents": 1, "ai_hint": "نزاع"}])
    monkeypatch.setattr(engine, "catalog", lambda: cat)
    seen = {}
    monkeypatch.setattr(engine, "classify",
                        lambda c, h, off: seen.setdefault("off", [s.slug for s in off]) and None)
    fut = engine.start(principal(), "الشركة أعطتني عقدًا جديدًا وأريد فهم البنود", [],
                       already={"contract-review"}, last_answer_suggested=False)
    engine.collect(fut)
    assert seen["off"] == ["dispute"]


def test_a_slow_classifier_is_dropped_not_waited_for(monkeypatch):
    monkeypatch.setattr(engine, "COLLECT_TIMEOUT_S", 0.01)
    fut: Future = Future()  # never resolved
    assert engine.collect(fut) is None


def test_staff_by_role_not_by_being_registered():
    assert engine.is_staff(principal(role="admin"))
    assert engine.is_staff(principal(role="lawyer"))
    assert not engine.is_staff(principal(role="client"))
    assert not engine.is_staff(principal(role="admin", anonymous=True))


# ── the turn ────────────────────────────────────────────────────────────


def test_a_streamed_answer_carries_the_suggestion_before_done(monkeypatch):
    from app.chat import turns

    monkeypatch.setattr(settings(), "backend_url", "https://be.example")
    recorded = {}
    user = SimpleNamespace(id="um1", seq=1, role="user")
    monkeypatch.setattr(turns.repo, "add_user_message", lambda *a, **k: (user, True))
    monkeypatch.setattr(turns.repo, "recent_turns", lambda *a, **k: [])
    monkeypatch.setattr(turns.repo, "suggested_slugs", lambda *a, **k: set())
    monkeypatch.setattr(turns.repo, "last_answer_suggested", lambda *a, **k: False)
    monkeypatch.setattr(turns.repo, "add_assistant_message",
                        lambda *a, **k: SimpleNamespace(id="am1", seq=2))
    monkeypatch.setattr(turns.repo, "finish_message", lambda *a, **k: None)
    monkeypatch.setattr(turns.repo, "set_suggestion",
                        lambda db, p, mid, **kw: recorded.update(mid=mid, **kw))
    monkeypatch.setattr(turns, "build_messages", lambda *a, **k: [])

    fut: Future = Future()
    fut.set_result(classify.Suggestion(service=CONTRACT, reason="تريد مراجعة عقد", confidence=0.9))
    monkeypatch.setattr(turns.suggest, "start", lambda *a, **k: fut)

    class R:
        def run(self, req):
            return SimpleNamespace(chunks=(), plan=SimpleNamespace(as_dict=lambda: {}),
                                   index_version="v")

    monkeypatch.setattr(turns, "bedrock_chat_stream", lambda *a, **k: iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="جواب"))]),
    ]))

    class Scope:
        def __enter__(self): return object()
        def __exit__(self, *a): return False

    events = list(turns.stream_turn(lambda: Scope(), principal(), R(), session_id="s1",
                                    content="الشركة أعطتني عقدًا جديدًا", client_message_id="abcdef"))
    names = [e["event"] for e in events]
    assert names.index("suggestion") < names.index("done")
    sugg = next(e for e in events if e["event"] == "suggestion")["data"]
    assert sugg["service"] == "contract-review" and sugg["price_cents"] == 30000
    assert recorded == {"mid": "am1", "service": "contract-review",
                        "reason": "تريد مراجعة عقد", "confidence": 0.9}


def test_rescue_mode_cancels_the_verdict_under_a_good_answer(monkeypatch):
    from app.chat import turns

    monkeypatch.setattr(settings(), "suggest_mode", "rescue")
    fut: Future = Future()
    fut.set_result(classify.Suggestion(service=CONTRACT, reason="", confidence=0.9))
    prep = turns._Prepared(user_msg_id="u", suggestion=fut)
    assert turns._settle_suggestion(prep, "documents") is None
    fut2: Future = Future()
    fut2.set_result(classify.Suggestion(service=CONTRACT, reason="", confidence=0.9))
    prep2 = turns._Prepared(user_msg_id="u", suggestion=fut2)
    assert turns._settle_suggestion(prep2, "refused") is not None


# ── the evaluation set ──────────────────────────────────────────────────


def test_the_evaluation_set_is_well_formed():
    path = Path(__file__).with_name("suggestions.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 20
    slugs = {"contract-review", "dispute", "consultation", None}
    for r in rows:
        assert set(r) == {"message", "expect"}
        assert r["expect"] in slugs
    # Hard negatives exist: questions about the law that are not needs.
    assert sum(1 for r in rows if r["expect"] is None) >= 8

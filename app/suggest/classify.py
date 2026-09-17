"""
Which service, if any, does this person need from a lawyer?

A separate small model call, not a tag hidden in the answer: the answer's
prompt is about the law, this one is about the client, and mixing them
makes both worse. It sees the message, a little context, and the firm's own
description of when each service applies (`ai_hint`, edited in the
dashboard). It returns a slug from the list or null — never a name or a
price, which come from the catalogue.

Everything that decides whether to *ask* the model, and whether to *trust*
what it said, is a pure function here so the rules can be tested without a
model in the loop.
"""

import json
import re
from dataclasses import dataclass

from app.config import settings
from app.rag.bedrock_client import bedrock_chat
from app.suggest.catalog import Service


@dataclass(frozen=True, slots=True)
class Suggestion:
    service: Service
    reason: str
    confidence: float

    def as_event(self) -> dict:
        return {
            "service": self.service.slug,
            "name": self.service.name,
            "description": self.service.description,
            "price_cents": self.service.price_cents,
            "currency": self.service.currency,
            "reason": self.reason,
        }


_WORD = re.compile(r"[\w؀-ۿ]+")


def should_consider(
    content: str,
    *,
    staff: bool,
    already: set[str],
    last_answer_suggested: bool,
    offerable: tuple[Service, ...],
) -> bool:
    """The cheap gates, before any model is called.

    Short messages are greetings and thanks. Staff are testing. Once every
    offerable service has been proposed there is nothing left to say, and
    two suggestions in a row reads as a sales pitch rather than help.
    """
    if staff or not offerable:
        return False
    if len(_WORD.findall(content)) < settings().suggest_min_words:
        return False
    if last_answer_suggested:
        return False
    if all(s.slug in already for s in offerable):
        return False
    return True


def allowed_now(*, mode: str, source_label: str | None) -> bool:
    """Whether a verdict may be used given how the answer went.

    `upsell` uses it whenever it passed; `rescue` only when the assistant
    could not help — the corpus refused, or it already asked to book.
    """
    if mode == "rescue":
        return source_label in ("refused", "escalate")
    return True


# ── the prompt ─────────────────────────────────────────────────────────

_SYSTEM = (
    "أنت مصنِّف صامت في مكتب محاماة سعودي متخصص في نظام العمل. "
    "مهمتك الوحيدة: تحديد ما إذا كان العميل يحتاج إلى إحدى الخدمات المدفوعة "
    "المذكورة أدناه من محامٍ، بناءً على رسالته الأخيرة وسياق المحادثة.\n\n"
    "قواعد صارمة:\n"
    "- اختر خدمة واحدة فقط إذا كان وصفها (متى تُقترح) ينطبق بوضوح على حالة العميل.\n"
    "- السؤال العام عن النظام أو طلب معلومة ليس حاجة إلى خدمة. أجب بـ null.\n"
    "- لا تخترع خدمات. استخدم المعرّف (slug) كما هو من القائمة فقط.\n"
    "- confidence رقم بين 0 و 1 يعبّر عن مدى وضوح الحاجة.\n"
    "- reason_ar جملة عربية قصيرة واحدة تُقال للعميل، تصف حالته لا الخدمة.\n"
    "أجب بكائن JSON فقط بهذا الشكل بلا أي نص آخر:\n"
    '{"service": "<slug أو null>", "confidence": 0.0, "reason_ar": "..."}'
)


def build_prompt(
    content: str,
    history: list[tuple[str, str]],
    offerable: tuple[Service, ...],
) -> list[dict]:
    lines = ["الخدمات المتاحة:"]
    for s in offerable:
        lines.append(f"- {s.slug} — {s.name}: متى تُقترح: {s.ai_hint.strip()}")
    ctx = ""
    if history:
        ctx = "\n\nسياق المحادثة (الأقدم أولًا):\n" + "\n".join(
            f"{'العميل' if role == 'user' else 'المساعد'}: {text[:400]}"
            for role, text in history
        )
    user = "\n".join(lines) + ctx + f"\n\nرسالة العميل الأخيرة:\n{content[:1200]}"
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


# ── the verdict ────────────────────────────────────────────────────────

_JSON = re.compile(r"\{.*\}", re.S)


def parse_verdict(text: str, offerable: tuple[Service, ...]) -> Suggestion | None:
    """Strict: anything that is not valid JSON naming a listed slug with a
    confidence at or above the threshold is `None`. The model cannot invent
    a service, and a malformed reply is treated as "nothing to suggest",
    never as an error the client sees."""
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    slug = data.get("service")
    if not isinstance(slug, str) or not slug or slug.lower() == "null":
        return None
    service = next((s for s in offerable if s.slug == slug), None)
    if service is None:
        return None
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= confidence <= 1.0) or confidence < settings().suggest_min_confidence:
        return None
    reason = data.get("reason_ar")
    reason = reason.strip() if isinstance(reason, str) else ""
    return Suggestion(service=service, reason=reason[:300], confidence=confidence)


def classify(
    content: str,
    history: list[tuple[str, str]],
    offerable: tuple[Service, ...],
) -> Suggestion | None:
    """One model call. Raises nothing: a failure is a `None`, logged by the
    caller if it cares. The answer must never wait on, or fail for, this."""
    try:
        raw = bedrock_chat(
            build_prompt(content, history, offerable),
            max_tokens=160,
            response_format={"type": "json_object"},
        )
    except Exception:
        try:
            # Some deployments reject response_format; the parser copes with
            # prose around the JSON, so ask again without it.
            raw = bedrock_chat(build_prompt(content, history, offerable), max_tokens=160)
        except Exception:
            return None
    return parse_verdict(raw, offerable)

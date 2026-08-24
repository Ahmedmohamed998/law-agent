"""
Turn a user's narrative into focused retrieval queries.

Users describe situations, not legal issues: "اشتغلت ٦ سنوات ونص في مقاولات،
المدير قال هينهي عقدي بدون سبب، وعندي إجازات ما أخذتهاش". Searching with that
text directly fails in two ways — BM25 scores the story words (مقاولات، المدير،
الأسبوع) alongside the one that matters, and a single embedding of the whole
message averages several topics into a vector close to none of them. Measured
on the corpus, a scenario about maternity leave failed to retrieve المادة 151
at all, while the bare question retrieved it at rank 1.

A scenario also tends to raise *several* issues at once — end-of-service pay,
unlawful termination, and accrued leave are three different articles — so one
rewritten query cannot cover it. This module extracts up to MAX_QUERIES.

What it deliberately does NOT do:
  - invent article numbers. `Retriever.extract_article_numbers` finds those
    with a regex from what the user actually typed, and is exact; an LLM asked
    for article references will happily supply plausible-but-unsaid ones.
  - replace the original message. The caller searches with these queries *and*
    the raw text, because extraction can drop the rare term BM25 needs.
"""

import json
import re
from dataclasses import dataclass

# A real grievance routinely raises four separate issues at once — a transfer,
# a cut allowance, a dismissal threat, and unused leave. At three, the last one
# is dropped silently: the answer then reports "لا أجد إجابة" for an issue the
# corpus covers perfectly well, which reads as a gap in the law rather than a
# gap in retrieval.
MAX_QUERIES = 4

SYSTEM_PROMPT = """أنت مساعد لتحليل استفسارات المستخدمين حول أنظمة العمل والموارد البشرية في السعودية.

مهمتك: اقرأ رسالة المستخدم واستخرج المسائل النظامية التي يسأل عنها، وصُغ لكل
مسألة عبارة بحث مستقلة بالعربية الفصحى.

القواعد:
- أعِد من عبارة واحدة إلى أربع عبارات بحث كحد أقصى.
- غطِّ كل مسألة نظامية وردت في الرسالة ولا تُهمل أياً منها، حتى لو
  ذكرها المستخدم عرضاً (مثل التهديد بالفصل، أو خصم من الأجر).
- كل عبارة تصف مسألة نظامية واحدة فقط (مثل: مكافأة نهاية الخدمة، إجازة الوضع،
  التعويض عن الفصل غير المشروع، نسبة التوطين في نشاط معين).
- اجعل كل عبارة قصيرة جداً: من كلمتين إلى ست كلمات، مصطلحاً نظامياً مجرداً
  لا جملة. اكتب "تخفيض بدل السكن" لا "مشروعية تخفيض بدل السكن أو المزايا
  التعاقدية من طرف واحد وحقوق العامل عند إنقاصها". العبارات الطويلة تُشتّت
  البحث وتُضعف مطابقة المادة المقصودة.
- لا تستخدم "و" أو "أو" للجمع بين مفهومين في عبارة واحدة؛ افصلهما في عبارتين.
- استخدم المصطلحات النظامية المتعارف عليها، لا كلمات المستخدم العامية.
- احذف تفاصيل القصة (أسماء، تواريخ، مشاعر، أسماء المدير أو الشركة).
- لا تُضف قيداً غير مذكور في النظام، وبالأخص اسم القطاع أو النشاط
  (مثل "في قطاع المطاعم")، لأن أحكام نظام العمل عامة ولا تُصنَّف حسب القطاع.
  استثناء واحد: إذا كان السؤال عن التوطين أو الترخيص أو نشاط محدد، فاذكر
  النشاط أو المهنة أو المنطقة صراحةً لأنها هي ما يميّز المستند الصحيح.
- احتفظ بنوع العامل إن كان مميزاً نظاماً (عامل منزلي، موظف حكومي، حدث).
- لا تذكر أرقام مواد نظامية إطلاقاً، حتى لو بدت مناسبة.
- إذا كانت الرسالة سؤالاً واضحاً ومباشراً أصلاً، أعِد صياغته كعبارة واحدة فقط.

أعِد النتيجة بصيغة JSON فقط:
{"queries": ["...", "..."]}"""

# Appended ONLY when the turn has prior context. A first message must reach the
# planner with byte-identical instructions to the pre-session code, or
# eval/scenarios.py stops being a valid gate for anything else that changes.
# Measured: asking for `resolved` unconditionally moved scenario recall 71% -> 64%.
FOLLOWUP_PROMPT = """

- «سياق المحادثة» أدناه يُستعمل فقط لفهم الإشارات المبهمة في الرسالة الحالية
  (الضمائر، «هذا»، «نفس الشيء»، «وهل ينطبق…»). أما إذا كانت الرسالة الحالية
  تفتح موضوعاً جديداً فتجاهل السياق تماماً.

أضف إلى ناتج JSON حقلاً باسم "resolved":
{"resolved": "...", "queries": ["...", "..."]}

"resolved" = الرسالة الحالية مُعاد صياغتها كسؤالٍ مستقل مفهوم بذاته دون الحاجة
إلى ما قبله. إذا كانت الرسالة مفهومة بذاتها أصلاً فأعِدها كما هي حرفياً."""


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """One turn's search intent. Immutable, and travels with the request.

    `resolved` is the message rewritten to stand alone. With no history it is
    the raw message unchanged, which is what makes adding history a provable
    no-op for single-turn callers.
    """

    resolved: str
    queries: tuple[str, ...] = ()
    origin: str = "planned"  # planned | cached | fallback | skipped

    @property
    def used_history(self) -> bool:
        return self.origin == "planned" and bool(self.queries)

    def as_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "queries": list(self.queries),
            "origin": self.origin,
        }


def render_history(turns: list[tuple[str, str]]) -> str:
    """Format prior turns for the planner.

    Deliberately lossy on the assistant side: a caller passes the citation
    labels of a previous answer, not its prose. That is the topic signal in a
    few dozen tokens instead of several hundred, and it cannot drag the
    previous answer's wording into a search query.
    """
    lines = []
    for role, text in turns:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        who = "المستخدم" if role == "user" else "المساعد — استند إلى"
        lines.append(f"[{who}] {text}")
    return "\n".join(lines)


def plan_queries(
    client,
    deployment: str,
    message: str,
    history: list[tuple[str, str]] | None = None,
) -> QueryPlan:
    """Resolve the message and extract up to MAX_QUERIES focused search queries.

    Fails open: any error yields a plan whose `resolved` is the raw message and
    whose query list is empty, which is exactly the pre-planner behaviour.
    """
    content = message
    system = SYSTEM_PROMPT
    if history:
        system = SYSTEM_PROMPT + FOLLOWUP_PROMPT
        content = (
            f"سياق المحادثة:\n{render_history(history)}\n\nالرسالة الحالية:\n{message}"
        )
    try:
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            # NOTE: response_format={"type": "json_object"} is intentionally
            # omitted — Gemma 4 via Bedrock Mantle may reject unknown fields.
            # The _load_json fallback handles non-JSON-only responses.
            max_tokens=400 if history else 300,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        return QueryPlan(resolved=message, queries=(), origin="fallback")

    return parse_plan(raw, message, allow_resolved=bool(history))


def parse_plan(raw: str, message: str, *, allow_resolved: bool = True) -> QueryPlan:
    """Pull the plan out of the model's reply, tolerating stray prose.

    With no history `resolved` is pinned to the raw message rather than taken
    from the model. The raw text is itself a search arm, and it is there
    precisely because extraction can drop the rare term only BM25 on the
    original catches — substituting a paraphrase for it loses that signal.
    """
    data = _load_json(raw)
    if data is None:
        return QueryPlan(resolved=message, queries=(), origin="fallback")

    resolved = data.get("resolved") if allow_resolved else None
    if not isinstance(resolved, str) or len(resolved.strip()) < 4:
        # A model that skipped the field must not silently blank the search
        # arm — fall back to what the user actually typed.
        resolved = message
    resolved = re.sub(r"\s+", " ", resolved).strip()

    return QueryPlan(
        resolved=resolved,
        queries=tuple(_clean_queries(data.get("queries"))),
        origin="planned",
    )


def parse_queries(raw: str) -> list[str]:
    """Backwards-compatible view of the query list alone."""
    data = _load_json(raw)
    return _clean_queries(data.get("queries")) if data is not None else []


def _load_json(raw: str) -> dict | None:
    if not raw:
        return None
    if not raw.lstrip().startswith("{"):
        m = re.search(r"\{.*\}", raw, re.S)
        raw = m.group(0) if m else ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _clean_queries(items) -> list[str]:
    if not isinstance(items, list):
        return []

    out = []
    for q in items:
        if not isinstance(q, str):
            continue
        # Strip any article number the model produced despite the instruction:
        # article lookup is the regex's job, and a hallucinated number here
        # would fetch a confidently wrong article.
        q = re.sub(r"الماد[ةه]\s*\(?\s*\d{1,3}\s*\)?", "", q)
        q = re.sub(r"\s+", " ", q).strip(" .،")
        if len(q) >= 8 and q not in out:
            out.append(q)
        if len(out) == MAX_QUERIES:
            break
    return out

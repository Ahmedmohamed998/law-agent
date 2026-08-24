"""
Grounded answer generation: retrieve chunks, answer ONLY from them, cite articles.

This is the "general legal information" node of the future LangGraph agent.
The escalation classifier is a separate concern and sits in front of this.

Usage:
    python -m app.rag.answer "كم مدة فترة التجربة؟"
"""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.rag.bedrock_client import bedrock_chat, bedrock_chat_stream
from app.rag.retriever import Retriever, RetrievedChunk

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

SOURCE_LABELS = ("documents", "model_knowledge", "mixed", "refused", "escalate")
# The model states its own grounding on the first line, in ASCII, before any
# Arabic. Parsing this is exact; regexing an Arabic label out of the prose
# breaks the first time the model rephrases it, and the label is a column in
# the database and a badge in the UI — it has to be a field, not a guess.
_LABEL_RE = re.compile(r"^\s*\[\[SOURCE:\s*(\w+)\s*\]\]\s*", re.I)

SYSTEM_PROMPT = """أنت مساعد معلومات قانونية عامة متخصص في نظام العمل السعودي، تابع لمكتب محاماة.

ابدأ كل رد بسطر واحد بهذه الصيغة حرفياً ثم انتقل لسطر جديد:
[[SOURCE: documents]]        إذا كانت الإجابة كاملة من المقاطع المرجعية
[[SOURCE: mixed]]            إذا استعنت بمعرفتك العامة لجزء لا تغطيه المقاطع
[[SOURCE: model_knowledge]]  إذا لم تُسعف المقاطع إطلاقاً وأجبت من معرفتك
[[SOURCE: refused]]          إذا امتنعت عن الإجابة (لأسباب أخرى غير حجز الاستشارة)
[[SOURCE: escalate]]         إذا طلب المستخدم حجز استشارة مدفوعة أو طلب التحدث مع محامٍ أو حجز موعد

قواعد صارمة:
1. المقاطع المرجعية هي المصدر الأول. إذا كان فيها ما يجيب على السؤال فأجب منها
   وحدها، ولا تُكمِلها أو تُصحِّحها أو تُضيف إليها من معرفتك العامة.
2. إذا لم تغطِّ المقاطع السؤال أو غطّته جزئياً، فلا تكتفِ بالاعتذار: أجب من
   معرفتك العامة بنظام العمل السعودي، ووسِّم كل جملة غير مستندة إلى المقاطع
   بعبارة (معرفة عامة — غير موثّقة في المستندات). ولا تخترع أرقام مواد ولا
   تنسب نصاً إلى مستند لم يرد في المقاطع.
2أ. عند التعارض بين المقاطع ومعرفتك العامة، المقاطع هي المعتمدة.
2ب. إذا استعنت بمعرفتك العامة، أضف قبل التنويه سطراً: "قد يكون هذا الجزء غير
   محدَّث؛ يُرجى التحقق منه مع المحامي."
3. استشهد دائماً بمصدر المعلومة بين قوسين، مثال: (نظام العمل، المادة الرابعة والسبعون).
   استخدم اسم المستند ورقم المادة فقط. أرقام المقاطع ("مقطع 1") ترقيم داخلي لا معنى له
   للمستخدم، فلا تذكرها في إجابتك إطلاقاً.
4. إذا تعارض دليل التعديلات 2025 مع نص أقدم، اعتمد دليل التعديلات 2025 لأنه يعكس التعديلات النافذة من 19 فبراير 2025.
5. قدم معلومات عامة فقط. لا تقدم نصيحة قانونية لحالة شخصية، ولا تتنبأ بنتيجة قضية، ولا تنصح باتخاذ إجراء معين.
6. أنهِ كل إجابة عامة بهذا التنويه: "هذه معلومات عامة وليست استشارة قانونية. لحالتك الخاصة، يمكنك حجز استشارة مع المحامي."
6أ. إذا طلب المستخدم الاستشارة، أجب باختصار بترحيب واطلب منه استخدام الزر أدناه لحجز الاستشارة، وضع الوسم `[[SOURCE: escalate]]`.
7. أجب بلغة السؤال (العربية أو الإنجليزية أو غيرها).
8. إذا كان المصدر جدولاً (مثل جداول المخالفات والجزاءات)، فاعرض الإجابة في جدول Markdown بنفس الأعمدة والقيم الواردة في المصدر دون تلخيص أو تغيير.
9. الرسائل السابقة في المحادثة تُستخدم لفهم السؤال فقط، وليست مصدراً ولا مرجعاً.
   أعد التحقق من كل معلومة من المقاطع المرجعية الحالية، ولا تُعامل ما قلتَه سابقاً
   من معرفتك العامة على أنه ثابت موثّق.
"""


def build_context(chunks: list[RetrievedChunk]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(f"[مقطع {i} — {c.citation}]\n{c.text}")
    return "\n\n---\n\n".join(parts)


def build_messages(
    question: str,
    chunks: list[RetrievedChunk],
    history: list[dict] | None = None,
) -> list[dict]:
    """One place both the blocking and the streaming path build their prompt.

    Prior turns go in as real dialogue so the answer reads like a conversation;
    only the CURRENT turn's chunks are attached. Chunks are re-retrieved every
    turn deliberately — carrying turn 1's context forward is the main cause of
    confidently wrong follow-up answers.
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history or []:
        if turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append(
        {
            "role": "user",
            "content": f"المقاطع المرجعية:\n\n{build_context(chunks)}\n\n"
                       f"سؤال المستخدم: {question}",
        }
    )
    return messages


def split_label(text: str, had_chunks: bool) -> tuple[str, str]:
    """Peel the [[SOURCE: …]] sentinel off the answer. Returns (label, answer).

    A model that drops the sentinel must not produce a NULL column, so fall
    back to what retrieval actually did: chunks present means it was at least
    partly grounded.
    """
    fallback = "documents" if had_chunks else "model_knowledge"
    m = _LABEL_RE.match(text or "")
    if m:
        label = m.group(1).lower()
        # An unrecognised label still gets its marker stripped: a value the
        # check constraint would reject is a reason to fall back, never a
        # reason to show the user "[[SOURCE: banana]]".
        return (label if label in SOURCE_LABELS else fallback), text[m.end():].lstrip()
    return fallback, (text or "").lstrip()


_SENTINEL = "[[SOURCE:"


def split_partial(acc: str, had_chunks: bool) -> tuple[str | None, str, bool]:
    """Streaming form of `split_label`. Returns (label, text, holding).

    While the sentinel is still arriving one token at a time, `[[SOURCE` is
    indistinguishable from the start of a real answer, so nothing may be
    emitted yet — otherwise the marker itself streams into the UI and every
    later delta is computed against a buffer the client never had.
    """
    if _LABEL_RE.match(acc):
        label, text = split_label(acc, had_chunks)
        return label, text, False

    head = acc.lstrip()
    incomplete = (
        # a partial sentinel: "[", "[[SO", "[[SOURCE: docum" …
        (_SENTINEL.startswith(head[: len(_SENTINEL)]) or head.startswith(_SENTINEL))
        and "]]" not in head
        and len(head) < 48
    )
    if incomplete:
        return None, "", True

    label, text = split_label(acc, had_chunks)
    return label, text, False


def answer(question: str, retriever: Retriever | None = None) -> dict:
    retriever = retriever or Retriever()
    chunks = retriever.retrieve(question)

    raw = bedrock_chat(build_messages(question, chunks))
    label, text = split_label(raw, bool(chunks))
    return {
        "answer": text,
        "source_label": label,
        "sources": [{"chunk_id": c.chunk_id, "citation": c.citation, "reason": c.reason}
                    for c in chunks],
        "prompt_tokens": None,
        "completion_tokens": None,
    }


def answer_stream(question: str, retriever: Retriever | None = None):
    """Same as answer(), yielding the reply as it is generated.

    Retrieval is under a second, but generating ~900 tokens of Arabic takes
    ~17s, which is the bulk of the wait. Streaming does not make that shorter —
    it makes it visible: first words land in a couple of seconds instead of a
    blank screen for half a minute.

    Yields {"answer", "sources", "done"}; the final yield has done=True.
    """
    retriever = retriever or Retriever()
    chunks = retriever.retrieve(question)
    sources = [{"chunk_id": c.chunk_id, "citation": c.citation, "reason": c.reason}
               for c in chunks]

    stream = bedrock_chat_stream(build_messages(question, chunks))

    acc = ""
    for event in stream:
        if not event.choices:
            continue
        piece = getattr(event.choices[0].delta, "content", None)
        if piece:
            acc += piece
            # The sentinel arrives over the first token or two; hold back
            # until it is complete so it never flashes up in the UI.
            label, text, holding = split_partial(acc, bool(chunks))
            if holding:
                continue
            yield {"answer": text, "source_label": label,
                   "sources": sources, "done": False}
    label, text = split_label(acc, bool(chunks))
    yield {"answer": text, "source_label": label, "sources": sources, "done": True}


def print_result(result: dict, show_sources: bool = True):
    print("\n" + result["answer"])
    if show_sources:
        print("\n--- sources ---")
        for s in result["sources"]:
            print(f"  {s['chunk_id']:>20}  {s['reason']:<16} {s['citation']}")


def interactive():
    print("مساعد نظام العمل السعودي — اكتب سؤالك (exit للخروج)")
    retriever = Retriever()  # built once, reused across questions
    while True:
        try:
            q = input("\nسؤال> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "خروج"):
            break
        try:
            print_result(answer(q, retriever))
        except Exception as e:
            print(f"خطأ: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print_result(answer(sys.argv[1]))
    else:
        interactive()

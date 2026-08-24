"""
Gradio test interface for the labor-law RAG assistant.

    python -m app.api.main      -> the service this talks to, on :8000
    python -m app.ui            -> http://127.0.0.1:7860
    python -m app.ui --share    -> also creates a temporary public link

This is a *client* of the AI service, not a second implementation of it. It
speaks the same HTTP + SSE contract as the frontend will, so what you see here
is what the product does — multi-turn sessions included. Two things that
follow from that:

  * The service must be running. This process no longer builds a Retriever, so
    there is no second copy of the BM25 index in memory.
  * Conversations are persisted and resolvable as follow-ups, which the old
    in-process version could not do.

There is still no escalation classifier or human handoff.
"""

import json
import os
import sys
import time
import uuid

import gradio as gr
import httpx

API = os.environ.get("LAW_AGENT_API", "http://127.0.0.1:8000")
# Dev-mode identity: with DEV_ALLOW_UNVERIFIED_TOKENS=true the service takes the
# bearer token as the user id. A real client sends a signed JWT here instead.
USER = os.environ.get("LAW_AGENT_USER", "u_gradio")
HEADERS = {"Authorization": f"Bearer {USER}", "Content-Type": "application/json"}

REASON_LABEL = {
    "article_lookup": "🎯 مطابقة رقم المادة",
    "hybrid": "⭐ دلالي + لفظي",
    "semantic": "🧠 بحث دلالي",
    "lexical": "🔤 بحث لفظي",
    "regulation_join": "🔗 ربط النظام باللائحة",
}

EXAMPLES = [
    "كم مدة فترة التجربة؟",
    "أعطني جدول المخالفات والجزاءات المتعلقة بمواعيد العمل",
    "ما هي المادة 74 من نظام العمل؟",
    "مكافأة نهاية الخدمة",
    "كم إجازة الوضع للمرأة العاملة؟",
    "How many days of marriage leave do I get?",
]

CSS = """
.rtl textarea, .rtl input { direction: rtl; text-align: right; }
footer { display: none !important; }
"""


LABEL_NOTE = {
    "documents": "",
    "mixed": "\n\n⚠️ *جزء من هذه الإجابة من معرفة النموذج وليس من المستندات.*",
    "model_knowledge": "\n\n⚠️ *هذه الإجابة من معرفة النموذج — لا تغطيها المستندات.*",
    "refused": "",
}


def _new_session() -> str:
    r = httpx.post(f"{API}/v1/sessions", headers=HEADERS,
                   json={"lang": "ar"}, timeout=15)
    r.raise_for_status()
    return r.json()["session_id"]


def _sse(response):
    """Yield (event, data) pairs from a text/event-stream response."""
    event = None
    for line in response.iter_lines():
        if not line:
            continue
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            yield event, json.loads(line[6:])


def respond(message, history, session_id):
    """Send one turn to the service and stream the answer back."""
    if not message or not message.strip():
        yield history, "", session_id
        return

    history = history + [{"role": "user", "content": message}]
    yield history + [{"role": "assistant",
                      "content": "⏳ جارٍ البحث في المستندات..."}], "", session_id

    t0 = time.time()
    try:
        # Created lazily on the first message, so an abandoned chat never
        # leaves an empty row behind.
        if not session_id:
            session_id = _new_session()

        acc, sources, label = "", [], "documents"
        last_push = 0.0
        with httpx.stream(
            "POST", f"{API}/v1/sessions/{session_id}/messages/stream",
            headers=HEADERS, timeout=httpx.Timeout(120, read=120),
            json={"content": message,
                  # One id per user action. Gradio has no retry, but the
                  # service requires it and the frontend will need it.
                  "client_message_id": uuid.uuid4().hex},
        ) as resp:
            resp.raise_for_status()
            for event, data in _sse(resp):
                if event == "token":
                    acc += data["delta"]
                    now = time.time()
                    # Repaint a few times a second: every token would flood the
                    # socket and make the browser, not the model, the bottleneck.
                    if now - last_push > 0.15:
                        last_push = now
                        yield (history + [{"role": "assistant", "content": acc}],
                               "", session_id)
                elif event == "sources":
                    sources = data["sources"]
                elif event == "done":
                    label = data["source_label"]
                elif event == "error":
                    yield (history + [{"role": "assistant",
                                       "content": f"⚠️ {data['code']}: {data['message']}"}],
                           "", session_id)
                    return
    except httpx.ConnectError:
        yield (history + [{"role": "assistant", "content":
                           f"⚠️ الخدمة غير متاحة على {API}\n\n"
                           "شغّلها أولاً: `python -m app.api.main`"}], "", session_id)
        return
    except Exception as e:  # keep the UI alive on a transient failure
        yield history + [{"role": "assistant", "content": f"⚠️ حدث خطأ: {e}"}], "", session_id
        return

    lines = [acc + LABEL_NOTE.get(label, ""), "",
             f"<details><summary>📚 المصادر ({len(sources)}) — "
             f"{time.time() - t0:.1f}s · {label}</summary>", ""]
    for s in sources:
        reason = REASON_LABEL.get(s["reason"], s["reason"])
        lines.append(f"- **{s['citation']}** · {reason}")
    lines.append("</details>")

    yield history + [{"role": "assistant", "content": "\n".join(lines)}], "", session_id


def build():
    # Gradio 6 moved theme/css from Blocks() to launch().
    with gr.Blocks(title="مساعد نظام العمل السعودي") as demo:
        gr.Markdown(
            "## ⚖️ مساعد نظام العمل السعودي\n"
            "يجيب من: **نظام العمل** · **اللائحة التنفيذية وملحقاتها** · "
            "**دليل تعديلات 2025**\n\n"
            "*نسخة تجريبية للاختبار — معلومات عامة وليست استشارة قانونية.*"
        )

        # One conversation per browser tab. Gradio keeps this per-connection,
        # so two tabs are two sessions — the same as two frontend tabs.
        session_id = gr.State(None)

        # Gradio 6: messages format is the default; no `type`/`show_copy_button`.
        chatbot = gr.Chatbot(
            height=520,
            rtl=True,
            resizable=True,
            avatar_images=(None, "⚖️"),
            placeholder="اسأل عن نظام العمل السعودي",
        )

        with gr.Row():
            box = gr.Textbox(
                placeholder="اكتب سؤالك عن نظام العمل...",
                show_label=False,
                scale=9,
                elem_classes="rtl",
                autofocus=True,
            )
            send = gr.Button("إرسال", variant="primary", scale=1)

        with gr.Row():
            clear = gr.Button("🗑️ محادثة جديدة", size="sm")

        gr.Examples(examples=EXAMPLES, inputs=box, label="أمثلة")

        for trigger in (box.submit, send.click):
            trigger(respond, [box, chatbot, session_id],
                    [chatbot, box, session_id])
        # Dropping the id starts a fresh conversation; the old one stays in the
        # database and is reachable through GET /v1/sessions.
        clear.click(lambda: ([], "", None), None, [chatbot, box, session_id])

    return demo


if __name__ == "__main__":
    build().launch(
        css=CSS,
        theme=gr.themes.Soft(primary_hue="teal"),
        server_name="127.0.0.1",
        server_port=7860,
        share="--share" in sys.argv,
        inbrowser=True,
    )

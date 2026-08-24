# Law_Agent

Arabic RAG assistant over Saudi labour law, built for a law firm. It answers
general questions from the official documents with article-level citations, and
refuses to advise on personal cases — those get routed to a paid consultation
with the lawyer.

**Status:** retrieval, grounded answering, multi-turn sessions, and the HTTP API
are working. The escalation *classifier* (deciding when to hand off), the human
handoff itself, and the payment flow are not built yet — escalation can only be
triggered explicitly today.

## Documents

**Core** — the labour law itself, parsed by bespoke rules in `chunker.py`:

| File | Pages | Content |
|---|---|---|
| `labor-law.pdf` | 68 | نظام العمل — the law itself |
| `اللائحة التنفيذية...pdf` | 109 | Executive regulations + 5 annexes |
| `dlyl-astfsarat...2025.pdf` | 10 | 2025 amendments Q&A guide |

**Wider ministry corpus** — 167 further documents (~3,900 pages) of لوائح,
أدلة إجرائية and قرارات وزارية, parsed generically by `corpus_chunker.py`.
Byte-identical duplicates are removed at source, so `data/raw/` holds 170 PDFs
for 170 distinct documents.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your Azure keys
```

## Running

```bash
python -m app.ui                    # Gradio chat at http://127.0.0.1:7860
python -m app.rag.answer            # terminal chat
python -m app.rag.answer "سؤالك"    # single question
python -m app.api.main              # HTTP API at http://127.0.0.1:8000
```

The Gradio app and the eval suite need only the Azure keys. The HTTP API also
needs Postgres — see [docs/API.md](docs/API.md) for the wire contract, the
token requirements for the product backend, and local setup.

Rebuild the index only when the documents change (order matters):

```bash
python -m app.ingestion.extract_pdfs      # PDFs -> Markdown (Azure DI layout)
python -m app.ingestion.gen_topics        # subject line per core article (cached)
python -m app.ingestion.chunker           # Markdown -> data/chunks/chunks.jsonl
python -m app.ingestion.ingest            # chunks -> Chroma (only what changed)
```

All four steps are incremental and resumable, which matters at this scale
(~3,900 pages of OCR, ~4k embeddings). `extract_pdfs` skips PDFs that already
have markdown, analyses byte-identical duplicates once, and retries throttled
requests; `--dry-run` shows the page count and bill first. `ingest` embeds only
chunks whose text changed, so adding a document does not re-bill the rest —
pass `--rebuild` to force a full re-embed.

`data/chroma/` (the embedded index, 4,011 chunks) is committed, so the app runs
straight after `pip install` without spending Azure embedding calls. Rerun the
commands above only when the source documents change.

## How it works

**Chunking** — one chunk per legal unit, never fixed-size, because cutting
mid-article makes half a rule read like a complete rule. 4,011 chunks total.
Each carries a breadcrumb header (`نظام العمل > الباب الخامس > المادة الرابعة
والسبعون`) inside the embedded text, since the embedding model never sees
metadata.

*Core* ([app/ingestion/chunker.py](app/ingestion/chunker.py)) — 448 chunks:
212 law articles, 35 regulation articles, 184 annex sections, 17 FAQ pairs.
Repealed articles (ملغاة) are excluded so the bot cannot cite a dead article.

*Subject lines* ([app/ingestion/gen_topics.py](app/ingestion/gen_topics.py)) — a law
article is identified by number and never states its own subject: المادة الحادية
والستون governs the housing allowance, but neither its breadcrumb nor its
heading contains the word السكن, while a guide with a section headed بدل السكن
does. Measured, the guide won and the governing article was not retrieved at
all. Each core article therefore carries a generated subject phrase appended to
its breadcrumb line — on that line specifically, because BM25 weights only the
first line double. Phrases are cached in `data/chunks/article_topics.json` so
builds stay deterministic. This moved scenario recall from 38% to 71%.

*Wider corpus* ([app/ingestion/corpus_chunker.py](app/ingestion/corpus_chunker.py))
— 3,563 chunks from 168 documents. Azure DI sets heading depth from font size
rather than document logic, so `#` levels are not comparable even within one
file (32 of 38 article-based documents use inconsistent depth for consecutive
articles). Structure is therefore read from the heading *text*, and each
document is routed to one of four parsers: `المادة`-based لوائح, numbered
أدلة إجرائية (`3.1 النسب المفروضة`), heading-only guides, and short قرارات
وزارية kept whole. Tables of contents are excised — 110 of them — because a TOC
repeats every heading in its document and so matches almost any query
lexically, outranking the section that actually answers it.

**Retrieval** ([app/rag/retriever.py](app/rag/retriever.py)) — five paths:

1. **Exact article lookup** — "المادة 74" or "المادة الرابعة والسبعون" is
   fetched by number, not searched.
2. **Hybrid search** — dense (Azure embeddings) + BM25, fused with RRF at
   `K=10`. Neither alone is sufficient: embeddings cannot tell the three
   near-identical penalty tables apart, and BM25 fails on paraphrase.
3. **Re-ranking** — template forms are demoted (unless the user asks for a
   نموذج), and per-source capping stops near-duplicate FAQ entries from
   crowding out the law articles. Since each corpus document is its own
   source, that cap also stops the twenty near-identical توطين guides — which
   share their definitions and penalty boilerplate — from filling every slot.
   The wider corpus is additionally weighted at `SECONDARY_TIER = 0.85`: it
   mostly restates or applies نظام العمل, and without the margin a general
   labour question surfaced a guide's paraphrase ahead of the article it
   paraphrases (in one measured case, no core document made the top 6 at all).
   The margin is deliberately small — for questions the core documents do not
   cover, the corpus still wins on relevance alone.
4. **Scenario planning** ([app/rag/query_planner.py](app/rag/query_planner.py)) —
   users describe situations, not legal questions, and one message often raises
   several issues. A message over 2 tokens is split by a small LLM call into up
   to four focused Arabic queries, each searched separately and in parallel
   alongside the raw text; results are merged round-robin so no issue starves
   another. It also normalises colloquial phrasing — "اوفر تايم" matches nothing
   in a corpus that says "العمل الإضافي". Without it, scenario recall is 15%.
5. **Law ↔ regulation join** — a deterministic metadata lookup, not a search.
   The regulation states which law article it implements
   (`في تنفيذ أحكام (المادة السادسة) من النظام`), stored at chunk time as
   `implements_law_article_number`. Retrieving one side always fetches the
   other, even when search ranked it nowhere — the law gives the rule, the
   regulation gives the operative detail.

**Arabic handling** ([app/ingestion/arabic_ordinals.py](app/ingestion/arabic_ordinals.py))
— ordinal words → integers (`الثانية والسبعون` → 72), light stemming so
`والجزاءات` matches `الجزاء`, hamza/tashkeel normalisation so common
misspellings (`الموقت` for `المؤقت`, `الثالثه` for `الثالثة`) still match, and
bidi-control stripping so text pasted out of a PDF is not shattered into
single letters.

**Answering** ([app/rag/answer.py](app/rag/answer.py)) — answers only from
retrieved chunks, cites article numbers, prefers the 2025 amendments on
conflict, reproduces source tables as tables, refuses when the corpus has no
answer, and always appends the not-legal-advice disclaimer.

## Evaluation

Measured on the full index (4,011 chunks, 171 documents):

| Check | Result |
|---|---|
| Exact article lookup (`المادة N`, all 208 law articles) | **100%** precision@1, 100% recall@6 |
| Known-item retrieval (document title → that document, n=120) | **82%** rank-1, **97.5%** top-6 |
| Scenario recall ([eval/scenarios.py](eval/scenarios.py), 8 scenarios / 14 gold chunks) | **71%**, controls 4/4 |
| Section self-retrieval (heading → its document, n=120) | 43% rank-1, 70% top-6 |
| Index integrity | 0 duplicate ids, 0 unchunked documents, 2.4% duplicate bodies |

Section self-retrieval is the weakest number and the weakest test: 55% of the
sampled headings (`التعريفات`, `المقدمة`) are shared by more than one chunk, so
no retriever could identify a single one from the heading alone.

The known-question set is not a substitute for real user questions — it only
shows the index can find what is known to be in it.

`eval/scenarios.py` is the one that catches real failures. Every scenario in it
is a question that was asked in the app and answered badly; the gold chunk is
the provision that actually governs the issue, checked by reading the corpus.
Run it before and after any retrieval change:

```bash
python -m eval.scenarios --runs 3
```

It exists because tuning without it does not work. Retrieval quality varies with
query wording, so a single measurement of a configuration is unreliable — two
changes were adopted on apparent improvements that later proved to be noise, and
one "known-good" configuration failed to reproduce. With 14 gold chunks a single
chunk is worth 7 points, so differences smaller than that are not meaningful.
[eval/scenario_questions.md](eval/scenario_questions.md) holds 91 further
scenarios covering all 163 documents, for finding cases to add.

## Not built yet

- Escalation classifier (general info vs. personal case) — currently the
  system prompt discourages personal advice but nothing structurally blocks it.
- Human handoff: consent, structured intake, conversation summary, lawyer
  dashboard.
- Paymob payment intent + webhook.
- Circuit breaker so the bot stops advising once a case is handed off.

"""
Retrieval layer over the Chroma collection.

Three retrieval behaviors combined:
  1. semantic search (Azure OpenAI embeddings, cosine)
  2. exact article lookup when the query mentions a specific article number
     ("المادة 74" or "المادة الرابعة والسبعون")
  3. law<->regulation join: retrieving a law article also pulls the regulation
     articles that implement it (and vice versa)
"""

import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import chromadb
from dotenv import load_dotenv
from openai import OpenAI

from app.ingestion.arabic_ordinals import normalize_arabic, parse_article_ordinal, strip_bidi
from app.rag.bedrock_client import INFERENCE_PROFILE, _client as _bedrock_oai_client
from app.rag.query_planner import QueryPlan, plan_queries

ROOT = Path(__file__).resolve().parents[2]
CHROMA_DIR = ROOT / "data" / "chroma"
COLLECTION = "labor_law_ar"

load_dotenv(ROOT / ".env")

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# very common words that would otherwise dominate BM25 scores in this corpus
AR_STOPWORDS = {
    "من", "في", "على", "إلى", "عن", "أو", "و", "ما", "هو", "هي", "هذا", "هذه",
    "التي", "الذي", "أن", "إن", "كان", "قد", "لا", "له", "بها", "به", "مع",
    "كل", "عند", "بين", "وفق", "وفقا", "ذلك", "هل", "كم", "المادة", "النظام",
    "نظام", "العمل", "اللائحة",
}


# Light stemming (Larkey-style "light10"): Arabic glues conjunctions and
# prepositions onto words, so a query saying "والجزاءات"/"بمواعيد" must still
# match text saying "الجزاء"/"مواعيد".
_PREFIXES = ("وال", "بال", "كال", "فال", "لل", "ال", "و", "ب", "ك", "ل", "ف")
_SUFFIXES = ("اتها", "اتهم", "ها", "هم", "ان", "ات", "ون", "ين", "ية", "ه", "ة", "ي")


def _strip_al(t: str) -> str:
    for p in _PREFIXES:
        if t.startswith(p) and len(t) - len(p) >= 3:
            t = t[len(p):]
            break
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)]
            break
    return t


# stopwords must be compared post-stripping, or entries like "العمل" never match
_STOP = {_strip_al(normalize_arabic(w)) for w in AR_STOPWORDS}


def tokenize_ar(text: str) -> list[str]:
    """Normalize + split for BM25. Strips the definite article ال so that
    'موسمي' matches 'الموسمي'."""
    text = normalize_arabic(text.translate(ARABIC_DIGITS))
    tokens = re.findall(r"[؀-ۿa-zA-Z0-9]+", text)
    out = []
    for t in tokens:
        t = _strip_al(t)
        if t and t not in _STOP and len(t) > 1:
            out.append(t)
    return out


# Relevance multiplier for the wider ministry corpus, relative to نظام العمل
# and its executive regulation. See `weight()` in _hybrid_search.
SECONDARY_TIER = 0.85

# Above this length a message gets its issues extracted before retrieval.
#
# Set to 12 initially, which left a hole: `_expand_query` handles <=4 tokens and
# the planner handled >=12, so anything in between got no query understanding at
# all. "لو بشتغل اكتر من 10 ساعات ومش باخد اوفر تايم دا قانوني؟" is 11 tokens and
# fell straight through it — colloquial "اوفر تايم" never matches "العمل
# الإضافي", so المادة 107 was never retrieved. Measured on eval/scenarios.py:
# 12 -> 27%, 8 -> 32%, 5 -> 36%. Re-measured after adding a 4-token
# colloquial case: 5 -> 29%, 4 -> 36%, 3 -> 36%, 2 -> 43%. Controls pass at
# every value, and article lookup is deterministic, so a low threshold is safe.
SCENARIO_MIN_TOKENS = 2

# Extra context slots for a multi-issue message: six chunks cannot carry four
# separate legal issues plus the surrounding material.
SCENARIO_EXTRA_SLOTS = 6

# Sentinel: `None` is a legitimate cached value for _expand_query (the rewrite
# failed), so it cannot double as "not cached".
_MISS = object()



@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    metadata: dict
    distance: float | None = None
    reason: str = "semantic"  # semantic | article_lookup | regulation_join

    @property
    def citation(self) -> str:
        m = self.metadata
        src = m.get("source")
        if src == "labor_law":
            return f"نظام العمل، المادة {m.get('article_label', '?')}"
        if src == "executive_regulation":
            if m.get("bab", "").startswith("ملحق"):
                # Annex 1 holds several penalty tables under one bab; naming the
                # annex alone does not say which table the rule came from.
                section = (m.get("fasl") or "").strip(" :")
                base = f"اللائحة التنفيذية، {m.get('bab')}"
                return f"{base}، {section}" if section else base
            return f"اللائحة التنفيذية، المادة {m.get('article_label', '?')}"
        if src == "faq_2025":
            return "دليل تعديلات نظام العمل 2025"
        # Wider ministry corpus: cite the document it actually came from.
        # Falling through to a fixed string here attributed every one of the
        # 168 corpus documents to the 2025 amendments guide — a false citation
        # in a product whose whole value is citing the right instrument.
        doc = m.get("doc_title") or src or "مستند غير محدد"
        if m.get("article_label"):
            return f"{doc}، المادة {m['article_label']}"
        if m.get("section_number"):
            return f"{doc}، البند {m['section_number']}"
        return str(doc)


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Everything one call needs. Nothing here is ever written back to the
    Retriever, which is shared by every concurrent request in the process."""

    raw: str
    history: tuple[tuple[str, str], ...] = ()
    history_key: str = ""      # fingerprint of `history`, for the plan cache
    force_expand: bool = False  # set when the message was pasted from a PDF


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: tuple[RetrievedChunk, ...]
    plan: QueryPlan
    index_version: str
    latency_ms: int

    def __iter__(self):
        # So `for c in result` and `list(result)` behave like the old return
        # value; the shim below is what callers actually use.
        return iter(self.chunks)


class _LRU:
    """Small thread-safe LRU. The caches live on a process-wide singleton, so
    an unbounded dict is a slow memory leak and an unguarded one is a race."""

    def __init__(self, maxsize: int = 512):
        self._maxsize = maxsize
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            if key not in self._data:
                return default
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key, value):
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def __contains__(self, key):
        with self._lock:
            return key in self._data


@dataclass
class Retriever:
    top_k: int = 6
    expand_queries: bool = True
    plan_scenarios: bool = True
    max_per_source: int = 3
    index_version: str = ""
    _plan_cache: _LRU = field(default_factory=_LRU, repr=False)
    _expand_cache: _LRU = field(default_factory=_LRU, repr=False)
    _client: OpenAI = field(default=None, repr=False)
    _col: chromadb.Collection = field(default=None, repr=False)

    def __post_init__(self):
        # AWS Bedrock Runtime (boto3) for embeddings — uses IAM credentials.
        self._embed_client = boto3.client(
            'bedrock-runtime',
            region_name=os.environ.get('AWS_BEDROCK_REGION', 'us-east-1'),
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
        )
        self._embed_model_id = os.environ.get('AWS_BEDROCK_EMBEDDING_MODEL', 'cohere.embed-multilingual-v3')
        # Bedrock Mantle client for all chat / query-planning calls.
        self._client = _bedrock_oai_client()
        self._col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection(COLLECTION)
        self._build_bm25()
        if not self.index_version:
            self.index_version = self._compute_index_version()

    def _compute_index_version(self) -> str:
        """Identify this build of the index.

        Recorded on every answer, because Chroma is a build artifact rather
        than a database: re-ingesting moves chunk ids, and without this there
        is no way to tell "retrieval was wrong" from "retrieval was right and
        the index has changed since" when triaging a complaint weeks later.
        """
        try:
            sqlite = CHROMA_DIR / "chroma.sqlite3"
            stamp = int(sqlite.stat().st_mtime) if sqlite.exists() else 0
        except OSError:
            stamp = 0
        return f"{len(self._bm25_ids)}c-{stamp:x}"

    # -- helpers ------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        # Uses AWS Bedrock Runtime (boto3) for embeddings.
        import json
        # Truncate text to 2048 characters to satisfy Cohere API constraints
        body = json.dumps({'texts': [text[:2048]], 'input_type': 'search_query'})
        resp = self._embed_client.invoke_model(
            body=body, modelId=self._embed_model_id,
            contentType='application/json', accept='application/json'
        )
        return json.loads(resp['body'].read())['embeddings'][0]

    @staticmethod
    def extract_article_numbers(query: str) -> list[int]:
        """Find explicit article references: 'المادة 74' or 'المادة الرابعة والسبعون'."""
        q = normalize_arabic(query.translate(ARABIC_DIGITS))
        nums = [int(n) for n in re.findall(r"المادة\s*(?:رقم\s*)?\(?(\d{1,3})\)?", q)]
        for m in re.finditer(r"المادة\s+([؀-ۿ][؀-ۿ\s]{2,40})", q):
            tokens = m.group(1).split()
            # longest token-prefix that parses wins ("الثالثة والستون بعد المائة"
            # before "الثالثة والستون", and ignore trailing "من نظام العمل")
            for k in range(min(5, len(tokens)), 0, -1):
                n = parse_article_ordinal(" ".join(tokens[:k]))
                if n is not None:
                    nums.append(n)
                    break
        return sorted(set(nums))

    def _expand_query(self, query: str) -> str | None:
        """Rewrite a keyword fragment into a standalone MSA question.
        Later this merges into the LangGraph classifier call, which already
        inspects every message — no extra round trip needed there."""
        cached = self._expand_cache.get(query, _MISS)
        if cached is not _MISS:
            return cached
        try:
            resp = self._client.chat.completions.create(
                model=INFERENCE_PROFILE,
                messages=[
                    {"role": "system", "content":
                     "أعد صياغة مدخل المستخدم كسؤال واحد كامل بالعربية الفصحى "
                     "عن نظام العمل السعودي. أعد السؤال فقط دون أي شرح."},
                    {"role": "user", "content": query},
                ],
                max_tokens=60,
            )
            out = (resp.choices[0].message.content or "").strip()
        except Exception:
            out = None
        self._expand_cache.put(query, out)
        return out

    def _build_bm25(self):
        """Lexical index over the same chunks (~4k docs — trivial in memory)."""
        from rank_bm25 import BM25Okapi

        got = self._col.get()
        self._bm25_ids = got["ids"]
        self._bm25_docs = got["documents"]
        self._bm25_metas = got["metadatas"]

        def doc_tokens(text: str) -> list[str]:
            # The first line is the breadcrumb heading ("... > أولاً: مخالفات
            # تتعلق بمواعيد العمل"). A heading match is a much stronger signal
            # than a body match, so count it twice.
            head, _, body = text.partition("\n")
            return tokenize_ar(head) * 2 + tokenize_ar(body)

        # b=0.4 (default 0.75) softens length normalisation: the penalty-table
        # chunks are legitimately long, and over-penalising them buried the
        # exact table a user asked for.
        self._bm25 = BM25Okapi([doc_tokens(d) for d in self._bm25_docs], b=0.4)

    def _hybrid_search(self, query: str, k_each: int = 60, limit: int | None = None,
                       allow_expand: bool = True, force_expand: bool = False):
        """Reciprocal-rank fusion of dense and BM25 rankings.

        k_each is the candidate pool taken from each ranking before fusion, and
        it has to scale with the corpus. At 20 — sized when the index held 448
        chunks — "جدول المخالفات والجزاءات المتعلقة بمواعيد العمل" ranked 3rd
        lexically but 30th densely across 4,011 chunks, so it fell outside the
        dense pool, earned only a lexical contribution, and lost its slot to
        generic articles about penalties. The table the user asked for by name
        was absent from the answer.
        """
        emb = self._embed(query)
        dense = self._col.query(query_embeddings=[emb], n_results=k_each)
        dense_ids = dense["ids"][0]
        dense_dist = dict(zip(dense_ids, dense["distances"][0]))

        by_id_meta = dict(zip(self._bm25_ids, self._bm25_metas))

        q_tokens = tokenize_ar(query)
        scores = self._bm25.get_scores(q_tokens)

        # "نموذج"/"صيغة"/"template"/"form" signals the user wants the blank form
        # itself rather than the rule behind it.
        wants_template = bool(re.search(
            r"نموذج|نماذج|صيغة|استمارة|template|form", normalize_arabic(query), re.I))

        # Fragments ("عقد عمل موسمي") and dialect ("وش أسوي") embed poorly.
        # Rewriting into a full MSA question recovers the articles that a bare
        # keyword probe misses.
        rare_ids: list[str] = []
        # Pasted PDF text keeps mangled word boundaries even after repair
        # ("م خالفاتت تعلقب"), so always let the rewrite step clean it up.
        if (allow_expand and self.expand_queries
                and (len(q_tokens) <= 4 or force_expand)):
            expanded = self._expand_query(query)
            if expanded and expanded != query:
                rare_ids = self._col.query(
                    query_embeddings=[self._embed(expanded)],
                    n_results=k_each,
                )["ids"][0]
        lex_order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k_each]
        lex_ids = [self._bm25_ids[i] for i in lex_order]
        lex_positive = {self._bm25_ids[i] for i in lex_order if scores[i] > 0}

        # K=60 is the web-scale default; on a corpus this size it flattens rank
        # differences so much that a strong lexical-only hit loses to two weak
        # ones. K=10 keeps top ranks meaningful.
        K = 10

        def weight(cid: str) -> float:
            """Model templates restate rules as boilerplate and otherwise
            outrank the actual articles, so demote them — but never demote the
            جدول المخالفات والجزاءات in annex 1: those penalty tables are
            unique content that exists nowhere else in the corpus, and they
            answer the practical questions workers actually ask."""
            meta = by_id_meta.get(cid) or {}
            bab = meta.get("bab") or ""
            heading = f"{meta.get('fasl') or ''} {meta.get('article_label') or ''}"

            # نظام العمل and its executive regulation state the rule itself;
            # the wider ministry corpus mostly restates or applies it. Without
            # this, a general labour question surfaces a procedural guide's
            # paraphrase ahead of the article it paraphrases. The margin is
            # deliberately small: for a question the core documents do not
            # cover, the corpus still wins on relevance alone.
            tier = SECONDARY_TIER if meta.get("priority") == "secondary" else 1.0
            # A superseded document is kept for reference but must never
            # outrank the version that replaced it.
            if meta.get("superseded_by"):
                tier *= 0.5

            if "مخالف" in heading or "جزاء" in heading:
                return tier
            # ...and don't demote a template when the user is explicitly asking
            # for one ("أعطني نموذج عقد العمل"): then the form IS the answer.
            if wants_template:
                return tier
            if bab.startswith("ملحق رقم (5)"):  # model contracts: fill-in forms
                return 0.4 * tier
            if bab.startswith("ملحق رقم (1)"):  # model work regulation
                return 0.6 * tier
            return tier

        # Short keyword queries ("عقد عمل موسمي") carry their meaning in one
        # rare term; dense embeddings blur that into generic topic similarity,
        # so trust the lexical ranking more the shorter the query is.
        lex_w = 2.0 if len(q_tokens) <= 3 else 1.0

        fused: dict[str, float] = {}
        for rank, cid in enumerate(dense_ids, 1):
            fused[cid] = fused.get(cid, 0) + 1 / (K + rank)
        for rank, cid in enumerate(lex_ids, 1):
            if cid in lex_positive:
                fused[cid] = fused.get(cid, 0) + lex_w / (K + rank)
        for rank, cid in enumerate(rare_ids, 1):
            fused[cid] = fused.get(cid, 0) + 1 / (K + rank)
        fused = {cid: s * weight(cid) for cid, s in fused.items()}

        by_id = dict(zip(self._bm25_ids, zip(self._bm25_docs, self._bm25_metas)))

        # Cap per source: near-duplicate FAQ entries would otherwise fill every
        # slot and hide the underlying article the answer should also cite.
        limit = self.top_k if limit is None else limit
        ranked, per_source, spill = [], {}, []
        for cid, _ in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
            src = (by_id_meta.get(cid) or {}).get("source", "?")
            if per_source.get(src, 0) < self.max_per_source:
                per_source[src] = per_source.get(src, 0) + 1
                ranked.append(cid)
            else:
                spill.append(cid)
            if len(ranked) >= limit:
                break
        ranked += spill[: max(0, limit - len(ranked))]

        out = []
        for cid in ranked:
            doc, meta = by_id[cid]
            in_dense, in_lex = cid in dense_dist, cid in lex_positive
            reason = "hybrid" if (in_dense and in_lex) else ("semantic" if in_dense else "lexical")
            out.append((cid, doc, meta, dense_dist.get(cid), reason))
        return out


    def _plan(self, req: "RetrievalRequest") -> QueryPlan:
        """Resolve the message against history and extract its issues.

        With no history this returns exactly what the pre-session code did:
        `resolved` is the raw message and the queries are the same, so a
        single-turn caller cannot observe the change.
        """
        if not self.plan_scenarios:
            return QueryPlan(resolved=req.raw, queries=(), origin="skipped")
        if len(tokenize_ar(req.raw)) < SCENARIO_MIN_TOKENS:
            return QueryPlan(resolved=req.raw, queries=(), origin="skipped")

        # Keyed on the history too: the same follow-up wording in two different
        # conversations has different antecedents, and keying on text alone
        # would serve the first conversation's plan to the second.
        key = (req.raw, req.history_key)
        cached = self._plan_cache.get(key)
        if cached is not None:
            return QueryPlan(cached.resolved, cached.queries, "cached")

        plan = plan_queries(
            self._client,
            INFERENCE_PROFILE,
            req.raw,
            history=list(req.history) or None,
        )
        if plan.origin == "fallback" and req.history:
            # The planner is the only thing that can resolve a follow-up, so
            # when it fails the raw message alone is close to lexically empty.
            # Concatenating the previous user turn is crude on purpose: a
            # fallback for an Azure outage must not itself call Azure.
            prev = next(
                (t for role, t in reversed(req.history) if role == "user"), ""
            )
            if prev:
                return QueryPlan(f"{req.raw} {prev}".strip(), (), "fallback")

        if plan.origin == "planned":
            self._plan_cache.put(key, plan)
        return plan

    def _multi_hybrid(self, queries: list[str]):
        """One search per issue, combined by round-robin.

        Deliberately not reciprocal-rank fusion. RRF scores a chunk higher the
        more lists it appears in, which is right within a single query and
        wrong across the issues of one message: the article answering "تخفيض
        بدل السكن" matches that issue alone, while a general rights-and-duties
        guide matches all four issues weakly and outranks every specific
        article. Measured, that answered some issues and reported "لا أجد
        إجابة" for others, varying run to run.

        Round-robin instead: each issue contributes its best remaining chunk in
        turn, so no issue can be starved by another.
        """
        pool = max(self.top_k, 8)
        with ThreadPoolExecutor(max_workers=len(queries)) as ex:
            per_query = list(ex.map(lambda q: self._hybrid_search(q, limit=pool, allow_expand=False), queries))

        best: dict[str, tuple] = {}
        for hits in per_query:
            for row in hits:
                prev = best.get(row[0])
                if prev is None or (row[3] is not None and
                                    (prev[3] is None or row[3] < prev[3])):
                    best[row[0]] = row

        # The cap grows with the number of issues: a message raising four
        # questions is legitimately answered by four or more articles of the
        # same law, and a flat cap of 3 drops the last issue's article.
        cap = self.max_per_source + max(0, len(queries) - 1)
        limit = self.top_k + SCENARIO_EXTRA_SLOTS
        cursors = [0] * len(per_query)
        out, taken, per_source = [], set(), {}
        while len(out) < limit:
            progressed = False
            for qi, hits in enumerate(per_query):
                while cursors[qi] < len(hits):
                    cid = hits[cursors[qi]][0]
                    cursors[qi] += 1
                    if cid in taken:
                        continue
                    src = (best[cid][2] or {}).get("source", "?")
                    if per_source.get(src, 0) >= cap:
                        continue
                    per_source[src] = per_source.get(src, 0) + 1
                    taken.add(cid)
                    out.append(best[cid])
                    progressed = True
                    break
                if len(out) >= limit:
                    break
            if not progressed:
                break
        return out

    def _get_where(self, where: dict, reason: str) -> list[RetrievedChunk]:
        r = self._col.get(where=where)
        return [
            RetrievedChunk(chunk_id=i, text=d, metadata=m, reason=reason)
            for i, d, m in zip(r["ids"], r["documents"], r["metadatas"])
        ]

    # -- main entry ---------------------------------------------------------

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        """Single-turn entry point. Unchanged for every existing caller —
        eval/scenarios.py, the Gradio harness, and app.rag.answer all use this."""
        return list(self.run(RetrievalRequest(raw=query)).chunks)

    def run(self, req: RetrievalRequest) -> RetrievalResult:
        started = time.perf_counter()
        # Text pasted out of an Arabic PDF carries per-letter bidi controls
        # that shatter words; repair before anything else touches the query.
        raw = re.sub(r"\s+", " ", strip_bidi(req.raw)).strip()
        force_expand = req.force_expand or strip_bidi(req.raw) != req.raw
        req = RetrievalRequest(raw, req.history, req.history_key, force_expand)

        results: dict[str, RetrievedChunk] = {}

        # 1. exact article lookup — always on the RAW message. A resolved
        # follow-up could carry "المادة ٧٤" forward from three turns ago and
        # pin every later answer to a stale article; this regex is exact, and
        # it stays that way.
        for n in self.extract_article_numbers(raw):
            for c in self._get_where(
                {"$and": [{"source": {"$eq": "labor_law"}},
                          {"article_number": {"$eq": n}}]},
                "article_lookup",
            ):
                results[c.chunk_id] = c

        # 2. hybrid search: dense (semantic) + BM25 (lexical), fused with RRF.
        # Dense alone fails on short keyword queries ("عقد عمل موسمي") because
        # contract-template forms are surface-similar while the substantive
        # articles phrase the concept differently; BM25 catches the rare term.
        plan = self._plan(req)
        # Search the extracted issues *and* the resolved message: extraction
        # sharpens the topic but can drop the rare term only BM25 on the full
        # message catches. It is the RESOLVED message rather than the raw one
        # because a raw follow-up ("وهل ينطبق على العامل المنزلي؟") is
        # lexically almost empty, yet round-robin below would still hand it an
        # equal share of the slots and starve the queries that name the issue.
        arms = [plan.resolved] + [q for q in plan.queries if q != plan.resolved]
        hits = (
            self._multi_hybrid(arms)
            if plan.queries
            else self._hybrid_search(plan.resolved, force_expand=req.force_expand)
        )
        for cid, doc, meta, dist, reason in hits:
            if cid not in results:
                results[cid] = RetrievedChunk(cid, doc, meta, distance=dist, reason=reason)


        # 3. law <-> regulation join
        law_nums = {
            c.metadata["article_number"]
            for c in results.values()
            if c.metadata.get("source") == "labor_law"
            and c.metadata.get("article_number") is not None
        }
        law_nums |= {
            c.metadata["implements_law_article_number"]
            for c in results.values()
            if c.metadata.get("implements_law_article_number") is not None
        }
        for n in law_nums:
            for c in self._get_where(
                {"$and": [{"source": {"$eq": "executive_regulation"}},
                          {"implements_law_article_number": {"$eq": n}}]},
                "regulation_join",
            ):
                results.setdefault(c.chunk_id, c)
            # and the law article itself if only the regulation was found
            for c in self._get_where(
                {"$and": [{"source": {"$eq": "labor_law"}},
                          {"article_number": {"$eq": n}}]},
                "regulation_join",
            ):
                results.setdefault(c.chunk_id, c)

        ordered = sorted(
            results.values(),
            key=lambda c: (
                {"article_lookup": 0, "hybrid": 1, "semantic": 2,
                 "lexical": 3, "regulation_join": 4}[c.reason],
                c.distance if c.distance is not None else 1.0,
            ),
        )
        limit = self.top_k + (SCENARIO_EXTRA_SLOTS if plan.queries else 4)
        return RetrievalResult(
            chunks=tuple(ordered[:limit]),
            plan=plan,
            index_version=self.index_version,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

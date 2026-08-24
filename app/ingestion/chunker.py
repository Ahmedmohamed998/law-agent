"""
Structure-aware chunker: Markdown (from Azure DI prebuilt-layout) -> chunks.jsonl

One chunk = one retrievable legal unit:
  - labor law:    one chunk per article (المادة)
  - regulations:  one chunk per regulation article (المادة (N)), linked to the
                  law article it implements; annexes chunked by their own
                  articles/headings
  - FAQ 2025:     one chunk per question/answer pair

Usage:
    python -m app.ingestion.chunker
"""

import argparse
import json
import re
from pathlib import Path

from app.ingestion.arabic_ordinals import parse_article_ordinal

ROOT = Path(__file__).resolve().parents[2]
MD_DIR = ROOT / "data" / "markdown"
OUT_PATH = ROOT / "data" / "chunks" / "chunks.jsonl"

LABOR_LAW_MD = MD_DIR / "labor-law.md"
REGULATION_MD = MD_DIR / "اللائحة التنفيذية لنظام العمل وملحقاتها-1.md"
FAQ_MD = MD_DIR / "dlyl-astfsarat-t-dylat-nzam-al-ml-als-wdy-2025.md"

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

NOISE_RE = re.compile(
    r"<!--\s*Page(Break|Number|Header|Footer)[^>]*-->|</?figure>|</?figcaption>"
)


def clean_lines(text: str) -> list[str]:
    """Strip layout noise, normalize digits, drop empty runs."""
    text = NOISE_RE.sub("", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    return lines


def squash(body_lines: list[str]) -> str:
    out, blank = [], False
    for ln in body_lines:
        if ln.strip():
            out.append(ln)
            blank = False
        else:
            if not blank and out:
                out.append("")
            blank = True
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def strip_heading(line: str) -> str:
    return re.sub(r"^#+\s*", "", line).strip()


AMEND_NOTE_RE = re.compile(r"^[-–]?\s*\d+\s*[-–]?\s*(عدلت|أضيفت|ألغيت|حذفت)")


def extract_amendment_notes(body: str):
    """Pull footnote lines like '-36عدلت هذه المادة...' out of the body."""
    kept, notes = [], []
    for ln in body.split("\n"):
        if AMEND_NOTE_RE.match(ln.strip()):
            notes.append(ln.strip())
        else:
            kept.append(ln)
    return squash(kept), notes


def make_chunk(chunk_id, source, header_path, body, **meta):
    text = " > ".join([p for p in header_path if p]) + "\n\n" + merge_split_tables(body)
    return {
        "chunk_id": chunk_id,
        "source": source,
        "text": text,
        "char_len": len(text),
        **meta,
    }


# --------------------------------------------------------------------------
# 1. labor law
# --------------------------------------------------------------------------

BAB_INLINE_RE = re.compile(r"^(?:#+\s*)?(?:\d+\s*)?(الباب\s+[؀-ۿ]+(?:\s+عشر)?)\s*$")
FASL_HEAD_RE = re.compile(r"^#+\s*(الفصل\s+.+)$")
# matches heading or plain-text article lines, tolerating leading list numbers
# ("## 32. المادة...") and trailing footnote digits / stray quotes ('...:" 8 38')
LAW_ARTICLE_RE = re.compile(
    r"^(?:#+\s*)?(?:\d+\s*[.\-]\s*)?المادة\s+([^:()#]+?)\s*[:.]?\s*[\"'«»®]?\s*([\d\s]*)\s*(\(ملغاة\))?\s*[\"'«»®]?\s*$"
)
REPEALED_BODY_RE = re.compile(r"^\(?\s*ملغاة\s*\)?\s*[.،]?$")
FOOTNOTE_NOISE_RE = re.compile(r"^\d+\s*[:.،\\\-]*\s*$")


REPEAL_NOTE_RE = re.compile(r"^(ألغيت|عدلت|أضيفت|حذفت|دمجت)")


def is_repealed_body(body: str) -> bool:
    """True if the body is just '(ملغاة)' plus footnote digits / repeal decree notes."""
    real = []
    for ln in body.split("\n"):
        ln = re.sub(r"^#+\s*", "", ln.strip())
        if not ln or FOOTNOTE_NOISE_RE.match(ln) or REPEAL_NOTE_RE.match(ln):
            continue
        real.append(ln)
    return bool(real) and all(REPEALED_BODY_RE.match(ln) for ln in real)


def parse_labor_law():
    lines = clean_lines(LABOR_LAW_MD.read_text(encoding="utf-8"))

    # -- TOC: map "الباب الخامس" -> "الباب الخامس: علاقات العمل"
    bab_titles = {}
    for ln in lines:
        m = re.search(r"(الباب\s+[؀-ۿ]+(?:\s+عشر)?)\s*:\s*([^<]+)", ln)
        if m:
            bab_titles.setdefault(m.group(1).strip(), f"{m.group(1).strip()}: {m.group(2).strip()}")

    # -- find end of TOC (second </table>)
    table_ends = [i for i, ln in enumerate(lines) if "</table>" in ln]
    start = table_ends[1] + 1 if len(table_ends) >= 2 else 0

    chunks, skipped = [], []
    current_bab, current_fasl = None, None
    cur = None  # (label, number, repealed, body_lines)

    def flush():
        nonlocal cur
        if cur is None:
            return
        label, number, repealed, body_lines = cur
        cur = None
        body = squash(body_lines)
        body, notes = extract_amendment_notes(body)
        if is_repealed_body(body):
            repealed = True
        if repealed or not body.strip():
            skipped.append(f"المادة {label}" + (" (ملغاة)" if repealed else " (فارغة)"))
            return
        bab_full = bab_titles.get(current_bab, current_bab)
        if number is not None and "مكرر" not in label:
            cid = f"labor_law_{number}"
        elif number is not None:
            cid = f"labor_law_{number}_mukarrar"
        else:
            cid = f"labor_law_{label.replace(' ', '_')}"
        chunks.append(make_chunk(
            chunk_id=cid,
            source="labor_law",
            header_path=["نظام العمل", bab_full, current_fasl, f"المادة {label}"],
            body=body,
            article_number=number,
            article_label=label,
            bab=bab_full,
            fasl=current_fasl,
            amendment_notes=notes or None,
            priority="base",
        ))

    for ln in lines[start:]:
        stripped = ln.strip().translate(ARABIC_DIGITS)
        m = LAW_ARTICLE_RE.match(stripped)
        if m:
            flush()
            label = m.group(1).strip()
            cur = (label, parse_article_ordinal(label), bool(m.group(3)), [])
            continue
        m = BAB_INLINE_RE.match(stripped)
        if m and "المادة" not in stripped:
            flush()
            current_bab, current_fasl = m.group(1).strip(), None
            continue
        m = FASL_HEAD_RE.match(stripped)
        if m:
            flush()
            current_fasl = m.group(1).strip()
            continue
        if cur is not None:
            cur[3].append(ln)
    flush()
    return chunks, skipped


# --------------------------------------------------------------------------
# 2. executive regulations (+ annexes)
# --------------------------------------------------------------------------

REG_OWN_RE = re.compile(r"^#+\s*(.*?)\s*المادة\s*\(\s*(\d+)\s*([^)]*)\)\s*(مكرر[^:]*)?\s*:?\s*(\(ملغاة\))?\s*$")
LAW_QUOTE_RE = re.compile(r"المادة\s+([؀-ۿ][^():\d]+?)\s*:")
# Annex 5 names each contract template on a bare line ("نموذج عقد العمل الدائم"),
# not as a Markdown heading, while Azure DI promotes stray table captions and
# contract boilerplate into "##" headings. Recognise the real titles explicitly
# and reject the junk, or every part of a template inherits a meaningless label.
TEMPLATE_TITLE_RE = re.compile(r"^(?:#+\s*)?((?:نموذج\s+)?عقد\s+(?:ال)?عمل[^<]{0,60})$")
JUNK_HEADING_RE = re.compile(
    r"Date to be specified|ويشار إليه|^بالطرف|^الطرف\s|^\(|Contract Information"
)

ANNEX_RE = re.compile(r"^(?:#+\s*)?ملحق\s*رقم\s*\(?\s*(\d+)\s*\)?\s*(.*)$")
IMPLEMENTS_RE = re.compile(r"في\s+تنفيذ\s+أحكام.*?\(?\s*المادة\s+([؀-ۿ][^()،.\d]+?)\s*\)?\s*من\s+(?:هذا\s+)?النظام")


def parse_regulation():
    lines = clean_lines(REGULATION_MD.read_text(encoding="utf-8"))
    text_nums = [ln.strip().translate(ARABIC_DIGITS) for ln in lines]

    # annex start line indices
    annex_starts = []
    for i, ln in enumerate(text_nums):
        m = ANNEX_RE.match(ln)
        if m and len(ln) < 60:
            annex_starts.append((i, int(m.group(1)), m.group(2).strip()))
    main_end = annex_starts[0][0] if annex_starts else len(lines)

    chunks, skipped = [], []

    # ---- main regulation ----
    law_label, law_number, law_quote_lines = None, None, []
    cur = None  # (own_num, own_suffix, repealed, body_lines, law_label, law_number, law_quote)
    collecting_quote = False

    def flush_main():
        nonlocal cur
        if cur is None:
            return
        own_num, own_suffix, repealed, body_lines, ctx_label, ctx_number, ctx_quote = cur
        cur = None
        body = squash(body_lines)
        if repealed or not body.strip():
            skipped.append(f"اللائحة المادة ({own_num}{' ' + own_suffix if own_suffix else ''}) (ملغاة/فارغة)")
            return
        # explicit "في تنفيذ أحكام (المادة X) من النظام" wins over tracked context
        m = IMPLEMENTS_RE.search(body)
        if m:
            ctx_label = m.group(1).strip()
            ctx_number = parse_article_ordinal(ctx_label)
        quote_excerpt = ctx_quote[:400].strip() if ctx_quote else None
        header_ctx = f"تنفيذاً للمادة {ctx_label} من نظام العمل" if ctx_label else None
        if quote_excerpt:
            body = f"[نص المادة {ctx_label} من النظام: {quote_excerpt}]\n\n{body}"
        suffix_id = f"_{own_suffix.replace(' ', '_')}" if own_suffix else ""
        chunks.append(make_chunk(
            chunk_id=f"regulation_{own_num}{suffix_id}",
            source="executive_regulation",
            header_path=["اللائحة التنفيذية لنظام العمل",
                         f"المادة ({own_num}{' ' + own_suffix if own_suffix else ''})",
                         header_ctx],
            body=body,
            article_number=own_num,
            article_label=f"({own_num}{' ' + own_suffix if own_suffix else ''})",
            implements_law_article_label=ctx_label,
            implements_law_article_number=ctx_number,
            bab=None, fasl=None,
            priority="base",
        ))

    for i in range(main_end):
        raw, ln = lines[i], text_nums[i]
        m = REG_OWN_RE.match(ln)
        if m and ln.startswith("#"):
            flush_main()
            collecting_quote = False
            own_num = int(m.group(2))
            own_suffix = (m.group(3).strip() + " " + (m.group(4) or "")).strip() or None
            cur = (own_num, own_suffix, bool(m.group(5)), [],
                   law_label, law_number, squash(law_quote_lines))
            continue
        # spelled-out law article heading (in headings or table cells)
        qm = LAW_QUOTE_RE.search(ln) if ("المادة" in ln and not re.search(r"المادة\s*\(", ln)) else None
        if qm and (ln.startswith("#") or "<td" in raw or len(strip_heading(ln)) < 60):
            candidate = qm.group(1).strip()
            if parse_article_ordinal(candidate) is not None or "مكرر" in candidate:
                flush_main()
                law_label, law_number = candidate, parse_article_ordinal(candidate)
                law_quote_lines, collecting_quote = [], True
                continue
        if cur is not None:
            cur[3].append(raw)
        elif collecting_quote:
            if not raw.strip().startswith(("<table", "</table", "<tr", "</tr", "<td", "</td", "<th", "</th")):
                law_quote_lines.append(re.sub(r"<[^>]+>", "", raw))
    flush_main()

    # ---- annexes ----
    annex_starts.append((len(lines), None, None))
    for (a_start, a_num, a_title), (a_end, _, _) in zip(annex_starts, annex_starts[1:]):
        # annex title may continue on following non-empty lines
        title = a_title or ""
        j = a_start + 1
        while j < min(a_start + 4, a_end) and (t := lines[j].strip()) and not t.startswith("#"):
            title = (title + " " + t).strip()
            j += 1
        annex_name = f"ملحق رقم ({a_num})" + (f": {title}" if title else "")

        current_fasl, current_section = None, None
        cur_a = None  # (kind, label, own_num, body_lines)
        counter = 0

        def flush_annex():
            nonlocal cur_a, counter
            if cur_a is None:
                return
            kind, label, own_num, body_lines = cur_a
            cur_a = None
            body = squash(body_lines)
            if not body.strip():
                return
            counter += 1
            cid = f"annex{a_num}_{own_num if own_num is not None else counter}"
            existing = {c["chunk_id"] for c in chunks}
            n = 2
            while cid in existing:
                cid = f"annex{a_num}_{own_num}_{n}"
                n += 1
            chunks.append(make_chunk(
                chunk_id=cid,
                source="executive_regulation",
                header_path=["اللائحة التنفيذية لنظام العمل", annex_name,
                             current_fasl, label],
                body=body,
                article_number=own_num,
                article_label=label,
                bab=annex_name,
                fasl=current_fasl or current_section,
                implements_law_article_label=None,
                implements_law_article_number=None,
                priority="base",
            ))

        for i in range(j, a_end):
            raw, ln = lines[i], text_nums[i]
            m = REG_OWN_RE.match(ln)
            if m and ln.startswith("#"):
                flush_annex()
                prefix = m.group(1).strip()
                if prefix:
                    current_section = prefix
                own_num = int(m.group(2))
                sfx = (m.group(3).strip() + " " + (m.group(4) or "")).strip()
                cur_a = ("article", f"المادة ({own_num}{' ' + sfx if sfx else ''})", own_num, [])
                continue
            fm = FASL_HEAD_RE.match(ln)
            if fm:
                flush_annex()
                current_fasl = fm.group(1).strip()
                continue
            tm = TEMPLATE_TITLE_RE.match(ln.strip())
            if tm and not JUNK_HEADING_RE.search(ln):
                # a named contract template starts here (heading or bare line)
                flush_annex()
                current_section = tm.group(1).strip()
                cur_a = ("section", current_section, None, [])
                continue
            if ln.startswith("#"):
                if JUNK_HEADING_RE.search(strip_heading(ln)):
                    if cur_a is None:
                        # content before any real title: open a section named
                        # after the annex so nothing is silently discarded
                        cur_a = ("section", current_section or annex_name, None, [])
                    cur_a[3].append(raw)
                    continue
                # generic heading: section marker; also a chunk boundary
                flush_annex()
                current_section = strip_heading(ln)
                cur_a = ("section", current_section, None, [])
                continue
            if cur_a is not None:
                cur_a[3].append(raw)
        flush_annex()

    return chunks, skipped


# --------------------------------------------------------------------------
# 3. FAQ 2025
# --------------------------------------------------------------------------

FAQ_TOPICS = [
    ("فترة التجربة", ["التجربة"]),
    ("إجازة الوضع والإجازات", ["الوضع", "إجازة", "الإجازات", "عطل"]),
    ("الاستقالة وإنهاء العقد", ["الاستقالة", "استقالة", "العدول", "إنهاء"]),
    ("العمل الإضافي والأجور", ["الإضافي", "أجر"]),
    ("تطوير بيئة العمل والتعديلات", ["تدريب", "تعديلات", "نفاذ", "أهداف"]),
]


def faq_topic(question: str) -> str:
    for topic, keywords in FAQ_TOPICS:
        if any(k in question for k in keywords):
            return topic
    return "عام"


def parse_faq():
    lines = clean_lines(FAQ_MD.read_text(encoding="utf-8"))
    # keep only the Q&A part: from the first "الأسئلة والأجوبة" heading
    first_qa = next((i for i, ln in enumerate(lines) if "الأسئلة والأجوبة" in ln), 0)

    # paragraphs (headings count as their own paragraph)
    paras, buf = [], []
    for ln in lines[first_qa:]:
        s = strip_heading(ln).strip()
        if not s or "الأسئلة والأجوبة" in s:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        if ln.strip().startswith("#"):
            if buf:
                paras.append(" ".join(buf))
                buf = []
            paras.append(s)
            continue
        buf.append(s)
    if buf:
        paras.append(" ".join(buf))

    chunks, skipped = [], []
    i, qnum = 0, 0
    while i < len(paras):
        if "؟" in paras[i]:
            question = paras[i]
            answer_parts = []
            i += 1
            while i < len(paras) and "؟" not in paras[i]:
                answer_parts.append(paras[i])
                i += 1
            if not answer_parts:
                skipped.append(f"سؤال بدون إجابة: {question[:60]}")
                continue
            qnum += 1
            topic = faq_topic(question)
            body = f"سؤال: {question}\nجواب: {' '.join(answer_parts)}"
            chunks.append(make_chunk(
                chunk_id=f"faq2025_{qnum}",
                source="faq_2025",
                header_path=["دليل استفسارات تعديلات نظام العمل السعودي 2025", topic],
                body=body,
                article_number=None,
                article_label=None,
                bab=None,
                fasl=topic,
                effective_date="2025-02-19",
                priority="amendment",
            ))
        else:
            skipped.append(f"فقرة يتيمة: {paras[i][:60]}")
            i += 1
    return chunks, skipped


# --------------------------------------------------------------------------

TABLE_SPLIT_RE = re.compile(r"</table>\s*<table>")
FIRST_CELL_RE = re.compile(r"<t[hd][^>]*>\s*(\d{1,3})\s*</t[hd]>")


def merge_split_tables(text: str) -> str:
    """Azure DI emits one <table> per page, so a table spanning a page break
    loses its column headers — the continuation rows' penalty values end up
    with nothing saying which is 'first offense' vs 'fourth'. Rejoin a
    continuation onto the previous table (and demote its misparsed <th> data
    cells) when its first row starts with a row number, which means it is a
    continuation rather than a genuinely new table."""
    out, pos = [], 0
    for m in TABLE_SPLIT_RE.finditer(text):
        if m.start() < pos:
            continue
        tail = text[m.end():]
        first_row = tail[: tail.find("</tr>") + 5] if "</tr>" in tail else ""
        if not FIRST_CELL_RE.search(first_row):
            continue  # genuinely separate table - keep the boundary as-is
        out.append(text[pos:m.start()])
        out.append(re.sub(r"<th(\s[^>]*)?>", r"<td\1>", first_row).replace("</th>", "</td>"))
        pos = m.end() + len(first_row)
    out.append(text[pos:])
    return "".join(out)


TOPICS_PATH = ROOT / "data" / "chunks" / "article_topics.json"


def apply_topics(chunks: list[dict]) -> int:
    """Append each article's subject to its breadcrumb line.

    Law articles are identified by number and never state their own subject:
    المادة الحادية والستون governs the housing allowance but its breadcrumb
    says only "... > الفصل الثاني الواجبات وقواعد التأديب > المادة الحادية
    والستون". A guide with a section headed بدل السكن therefore outranks it on
    every measure, and the governing article was not retrieved at all.

    The subject goes on the *first* line rather than a line of its own because
    `Retriever._build_bm25` weights only the first line double — a separate
    line would gain nothing lexically.

    Phrases come from data/chunks/article_topics.json (see gen_topics.py) so
    the build stays deterministic.
    """
    if not TOPICS_PATH.exists():
        return 0
    topics = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
    n = 0
    for c in chunks:
        topic = topics.get(c["chunk_id"])
        if not topic:
            continue
        head, sep, body = c["text"].partition("\n\n")
        if not sep or topic in head:
            continue
        c["text"] = f"{head} — {topic}{sep}{body}"
        c["char_len"] = len(c["text"])
        c["topic"] = topic
        n += 1
    return n


MAX_CHARS = 6000  # ~2.4k tokens for Arabic; safe margin under embedding limits


def split_oversized(chunks, max_chars: int = MAX_CHARS):
    """Hard-split any chunk over max_chars at paragraph boundaries,
    repeating the context header (first line) in every part.

    The default keeps the three core documents' chunking unchanged; the wider
    ministry corpus passes a tighter cap, since with 170 competing documents an
    over-long chunk dilutes its own embedding.
    """
    out = []
    for c in chunks:
        if c["char_len"] <= max_chars:
            out.append(c)
            continue
        header, _, body = c["text"].partition("\n\n")
        paras, parts, buf, size = body.split("\n\n"), [], [], 0
        for p in paras:
            if buf and size + len(p) > max_chars:
                parts.append("\n\n".join(buf))
                buf, size = [], 0
            buf.append(p)
            size += len(p) + 2
        if buf:
            parts.append("\n\n".join(buf))
        for i, part in enumerate(parts, 1):
            sub = dict(c)
            sub["chunk_id"] = f"{c['chunk_id']}_p{i}"
            sub["text"] = f"{header} (جزء {i}/{len(parts)})\n\n{part}"
            sub["char_len"] = len(sub["text"])
            out.append(sub)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-only", action="store_true",
                        help="Chunk only the three core labour-law documents")
    args = parser.parse_args()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    all_chunks, report = [], []

    for name, fn in [("labor_law", parse_labor_law),
                     ("executive_regulation", parse_regulation),
                     ("faq_2025", parse_faq)]:
        chunks, skipped = fn()
        all_chunks.extend(chunks)
        numbered = sum(1 for c in chunks if c.get("article_number") is not None)
        report.append(f"{name}: {len(chunks)} chunks ({numbered} with parsed article number), "
                      f"{len(skipped)} skipped")
        for s in skipped:
            report.append(f"    skipped: {s}")

    enriched = apply_topics(all_chunks)
    report.append(f'topic lines applied to {enriched} core chunks')

    all_chunks = split_oversized(all_chunks)
    core_count = len(all_chunks)

    if not args.core_only:
        # Imported here so the core parsers above stay independent of the
        # wider-corpus module (and its own, tighter size cap).
        from app.ingestion.corpus_chunker import build as build_corpus

        corpus_chunks = build_corpus(verbose=False)
        all_chunks.extend(corpus_chunks)
        report.append(f"ministry_corpus: {len(corpus_chunks)} chunks "
                      f"from {len({c['source'] for c in corpus_chunks})} documents")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print("\n".join(report))
    print(f"\ntotal: {len(all_chunks)} chunks -> {OUT_PATH}")

    big = [c for c in all_chunks if c["char_len"] > MAX_CHARS + 1000]
    if big:
        print(f"\nWARNING: {len(big)} chunks still over limit:")
        for c in big:
            print(f"    {c['chunk_id']}: {c['char_len']} chars")


if __name__ == "__main__":
    main()

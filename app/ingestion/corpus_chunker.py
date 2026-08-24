"""
Structure-aware chunker for the wider ministry corpus (167 documents).

The three core labour-law documents keep their bespoke parsers in
`chunker.py` — their chunks and chunk_ids must stay byte-identical so the
existing embeddings remain valid. This module handles everything else.

Why the parsing works off heading *text* rather than heading *level*:
Azure DI assigns `#` depth from font size on the page, not document logic, so
levels are not comparable even within one file. Measured over this corpus,
32 of 38 article-based documents and 68 of 90 numbered-section documents use
inconsistent depth for peer headings — e.g. consecutive articles landing at
`##`, `###`, `#####`, `######`. So `#` is treated only as "this line is a
heading"; the hierarchy comes from what the heading says.

Four document families, detected per file:

  A  أنظمة / لوائح      one chunk per "المادة ..."
  B  أدلة إجرائية        one chunk per numbered section ("3.1 النسب المفروضة")
  C  أدلة أخرى           one chunk per heading, cutting only at the levels that
                        yield substantial chunks for that particular file
  D  قرارات وزارية       whole document (1-2 pages, self-contained)

Usage:
    python -m app.ingestion.corpus_chunker        # standalone, prints a report
"""

import hashlib
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from app.ingestion.arabic_ordinals import parse_article_ordinal
from app.ingestion.chunker import (
    ARABIC_DIGITS,
    clean_lines,
    make_chunk,
    split_oversized,
    squash,
)

ROOT = Path(__file__).resolve().parents[2]
MD_DIR = ROOT / "data" / "markdown"

# Handled by chunker.py; never touched here.
CORE_DOCS = {
    "labor-law.md",
    "اللائحة التنفيذية لنظام العمل وملحقاتها-1.md",
    "dlyl-astfsarat-t-dylat-nzam-al-ml-als-wdy-2025.md",
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
ARTICLE_HEAD_RE = re.compile(r"^(?:\d+\s*[.\-]\s*)?المادة\s+([^:()#]{1,40}?)\s*[:.]?\s*$")
NUMBERED_HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)\s*[.\-]?\s+(.*)$")
TOC_TITLE_RE = re.compile(r"^(جدول\s+)?(ال)?محتويات|^قائمة\s+المحتويات|^الفهرس")
BAB_FASL_RE = re.compile(r"^(الباب|الفصل|القسم)\s")
UPDATED_RE = re.compile(r"\(?\s*(المحدث|محدث|محدثة)\s*\)?")

MAX_CORPUS_CHARS = 3000   # tighter than the core docs: 170 documents now compete
MIN_CHUNK_CHARS = 200      # below this a section is a fragment; glue it onto the previous one
TARGET_MEDIAN_CHARS = 600  # family C picks its cut levels to clear this


# --------------------------------------------------------------------------
# document-level helpers
# --------------------------------------------------------------------------


def doc_key(stem: str) -> str:
    """Stable short id for a document. Arabic filenames do not make usable
    chunk_id prefixes, and index-based ids would shift whenever a file is
    added, invalidating embeddings for unrelated documents."""
    return hashlib.md5(stem.encode("utf-8")).hexdigest()[:8]


ARABIC_CHAR_RE = re.compile(r"[؀-ۿ]")


def arabic_fraction(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(1 for c in letters if ARABIC_CHAR_RE.match(c)) / max(len(letters), 1)


def derive_title(stem: str, lines: list[str]) -> str:
    """The document's name as it should appear in every chunk's breadcrumb.

    Two thirds of this corpus arrives with filenames that carry no meaning for
    a reader or an embedding model — bare dates ("03112022") or Latin
    transliterations ("aldlyl-alajrayy-lqrar-twtyn-mhn-almshtryat"). The
    breadcrumb is the only statement of document identity inside the embedded
    text, so a filename title leaves those documents effectively anonymous:
    a question about مكافحة التسول cannot match a chunk headed "03112022".
    Prefer the document's own Arabic H1 whenever the filename is not Arabic.
    """
    if arabic_fraction(stem) >= 0.5:
        return stem
    for ln in lines:
        m = HEADING_RE.match(ln.strip())
        if not m:
            continue
        candidate = m.group(2).strip()
        if len(candidate) >= 8 and arabic_fraction(candidate) > 0.6:
            # Generic headings ("قرار وزاري") are shared by many documents and
            # would make them indistinguishable. These are cited by number in
            # practice, so keep the number the filename carries.
            if len(candidate) <= 20:
                digits = re.sub(r"\D+", "", stem)
                if digits:
                    return f"{candidate} رقم {digits}"
            return candidate[:120]
    return stem


def parse_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """-> [(line_index, level, title)] for every markdown heading."""
    out = []
    for i, ln in enumerate(lines):
        m = HEADING_RE.match(ln.strip())
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def classify(headings: list[tuple[int, int, str]], text: str) -> str:
    arts = sum(1 for _, _, t in headings if ARTICLE_HEAD_RE.match(t.translate(ARABIC_DIGITS)))
    nums = sum(1 for _, _, t in headings if NUMBERED_HEAD_RE.match(t.translate(ARABIC_DIGITS)))
    if arts >= 3:
        return "A"
    if nums >= 3:
        return "B"
    if len(headings) >= 3:
        return "C"
    return "D"


def is_toc_title(title: str) -> bool:
    return bool(TOC_TITLE_RE.match(title.strip()))


# --------------------------------------------------------------------------
# section splitting
# --------------------------------------------------------------------------


def split_sections(lines: list[str], cut: callable) -> list[tuple[str | None, int, list[str]]]:
    """Split lines wherever `cut(level, title)` is true.

    Returns [(title, level, body_lines)]. Content appearing before the first
    cut is returned with title None so nothing is silently dropped.
    """
    sections: list[tuple[str | None, int, list[str]]] = []
    title, level, body = None, 0, []
    for ln in lines:
        m = HEADING_RE.match(ln.strip())
        if m and cut(len(m.group(1)), m.group(2).strip()):
            if title is not None or any(x.strip() for x in body):
                sections.append((title, level, body))
            title, level, body = m.group(2).strip(), len(m.group(1)), []
        else:
            body.append(ln)
    if title is not None or any(x.strip() for x in body):
        sections.append((title, level, body))
    return sections


def excise_toc(lines: list[str]) -> tuple[list[str], int]:
    """Cut table-of-contents blocks out of the raw lines, before splitting.

    Dropping TOC *sections* after the split is not enough: a TOC heading is
    rarely a cut boundary itself (it is neither an article nor a numbered
    section), so its body would otherwise be absorbed into the neighbouring
    chunk and drag the whole document's heading list in with it.

    A TOC runs from its heading to the next heading, since the entries
    themselves are emitted as plain text, not headings.
    """
    out, dropped, i = [], 0, 0
    while i < len(lines):
        m = HEADING_RE.match(lines[i].strip())
        if m and is_toc_title(m.group(2).strip()):
            dropped += 1
            i += 1
            while i < len(lines) and not HEADING_RE.match(lines[i].strip()):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out, dropped


def drop_toc(sections):
    """Remove table-of-contents sections.

    A TOC repeats every heading in the document, so as a chunk it matches
    almost any query lexically and outranks the section that actually answers
    it. 90+ documents in this corpus carry one.
    """
    kept, dropped = [], 0
    for title, level, body in sections:
        if title and is_toc_title(title):
            dropped += 1
            continue
        kept.append((title, level, body))
    return kept, dropped


TARGET_SECTION_CHARS = 1800  # merge neighbouring sections up to roughly this size


def merge_small_sections(sections):
    """Merge consecutive sections until they reach a useful size.

    Merging stops at a heading that is shallower than the one the group
    started with, so a merge never spans a major topic boundary — only
    sibling subsections get combined.
    """
    merged: list[tuple[str | None, int, list[str]]] = []
    for title, level, body in sections:
        size = sum(len(x) for x in body) + len(title or "")
        if merged:
            p_title, p_level, p_body = merged[-1]
            p_size = sum(len(x) for x in p_body) + len(p_title or "")
            crosses_boundary = level < p_level
            if not crosses_boundary and p_size + size <= TARGET_SECTION_CHARS:
                new_body = p_body + ([f"{'#' * level} {title}"] if title else []) + body
                merged[-1] = (p_title, p_level, new_body)
                continue
        merged.append((title, level, body))
    return merged


def choose_cut_levels(headings) -> int:
    """Family C has no numbering to key on, so heading level is the only
    signal available. Pick the deepest level that still yields substantial
    chunks: cutting at every heading shreds these documents into fragments.
    """
    if not headings:
        return 6
    levels = sorted({lv for _, lv, _ in headings})
    line_of = [i for i, _, _ in headings]
    best = levels[0]
    for threshold in levels:
        cuts = [i for i, lv, _ in headings if lv <= threshold]
        if len(cuts) < 2:
            best = threshold
            continue
        gaps = [b - a for a, b in zip(cuts, cuts[1:])]
        # crude proxy for chunk size: lines between consecutive cuts
        if statistics.median(gaps) >= 8:
            best = threshold
        else:
            break
    return best


def merge_runts(chunks: list[dict]) -> list[dict]:
    """Glue fragment chunks onto the previous one. A bare heading or a stray
    page number is not independently retrievable and only adds noise."""
    out: list[dict] = []
    for c in chunks:
        if out and c["char_len"] < MIN_CHUNK_CHARS:
            prev = out[-1]
            prev["text"] = prev["text"] + "\n\n" + c["text"].split("\n\n", 1)[-1]
            prev["char_len"] = len(prev["text"])
        else:
            out.append(c)
    return out


# --------------------------------------------------------------------------
# per-family parsing
# --------------------------------------------------------------------------


def parse_document(path: Path, doc_meta: dict) -> tuple[list[dict], str, int]:
    raw = path.read_text(encoding="utf-8")
    lines = clean_lines(raw)
    lines, toc_excised = excise_toc(lines)
    headings = parse_headings(lines)
    family = classify(headings, raw)

    # chunk_id stays keyed to the filename so ids survive a title change
    title = derive_title(path.stem, lines)
    key = doc_key(path.stem)
    chunks: list[dict] = []
    toc_dropped = toc_excised

    if family == "A":
        sections = split_sections(
            lines, lambda lv, t: bool(ARTICLE_HEAD_RE.match(t.translate(ARABIC_DIGITS)))
        )
        sections, _d = drop_toc(sections)
        toc_dropped += _d
        ambient = None
        n = 0
        for sec_title, _lv, body in sections:
            body_text = squash(body)
            if sec_title is None:
                # preamble: keep the last باب/فصل seen so articles inherit it
                for ln in body:
                    s = re.sub(r"^#+\s*", "", ln.strip())
                    if BAB_FASL_RE.match(s) and len(s) < 80:
                        ambient = s
                if not body_text.strip():
                    continue
            if not body_text.strip() and sec_title is None:
                continue
            n += 1
            if sec_title and (m := ARTICLE_HEAD_RE.match(sec_title.translate(ARABIC_DIGITS))):
                label = m.group(1).strip()
                number = parse_article_ordinal(label)
            else:
                label, number = None, None
            chunks.append(make_chunk(
                chunk_id=f"{key}_a{n}",
                source=key,
                header_path=[title, ambient, sec_title],
                body=body_text,
                doc_title=title,
                doc_family="regulation",
                article_number=number,
                article_label=label,
                section_number=None,
                priority="secondary",
                **doc_meta.get("extra", {}),
            ))

    elif family == "B":
        sections = split_sections(
            lines, lambda lv, t: bool(NUMBERED_HEAD_RE.match(t.translate(ARABIC_DIGITS)))
        )
        sections, _d = drop_toc(sections)
        toc_dropped += _d
        parents: dict[int, str] = {}
        n = 0
        for sec_title, _lv, body in sections:
            body_text = squash(body)
            if sec_title is None and not body_text.strip():
                continue
            secnum = None
            crumb = []
            if sec_title:
                m = NUMBERED_HEAD_RE.match(sec_title.translate(ARABIC_DIGITS))
                if m:
                    secnum = m.group(1)
                    depth = secnum.count(".")
                    parents[depth] = sec_title
                    for d in sorted(parents):
                        if d < depth:
                            crumb.append(parents[d])
                    for d in [d for d in parents if d > depth]:
                        parents.pop(d)
            n += 1
            chunks.append(make_chunk(
                chunk_id=f"{key}_s{n}",
                source=key,
                header_path=[title, *crumb, sec_title],
                body=body_text,
                doc_title=title,
                doc_family="guide",
                article_number=None,
                article_label=None,
                section_number=secnum,
                priority="secondary",
                **doc_meta.get("extra", {}),
            ))

    elif family == "C":
        # These documents have no numbering to key on, and DI's heading levels
        # are not trustworthy, so neither "cut at level N" nor "cut at every
        # heading" works: the first leaves 4000+ char blocks, the second
        # shreds the document into fragments. Cut at every heading, then merge
        # neighbours back together up to a target size.
        sections = split_sections(lines, lambda lv, t: True)
        sections, _d = drop_toc(sections)
        toc_dropped += _d
        sections = merge_small_sections(sections)
        n = 0
        for sec_title, _lv, body in sections:
            body_text = squash(body)
            if not body_text.strip() and not sec_title:
                continue
            n += 1
            chunks.append(make_chunk(
                chunk_id=f"{key}_h{n}",
                source=key,
                header_path=[title, sec_title],
                body=body_text,
                doc_title=title,
                doc_family="guide",
                article_number=None,
                article_label=None,
                section_number=None,
                priority="secondary",
                **doc_meta.get("extra", {}),
            ))

    else:  # D — short decision, kept whole
        body_text = squash(lines)
        if body_text.strip():
            chunks.append(make_chunk(
                chunk_id=f"{key}_full",
                source=key,
                header_path=[title],
                body=body_text,
                doc_title=title,
                doc_family="decision",
                article_number=None,
                article_label=None,
                section_number=None,
                priority="secondary",
                **doc_meta.get("extra", {}),
            ))

    chunks = merge_runts(chunks)
    chunks = [c for c in chunks if len(c["text"].strip()) >= 40]
    return chunks, family, toc_dropped


# --------------------------------------------------------------------------


def normalize_title(stem: str) -> str:
    s = UPDATED_RE.sub("", stem)
    s = re.sub(r"[_\-()\d]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def detect_superseded(stems: list[str]) -> dict[str, str]:
    """Map stem -> stem that supersedes it.

    Only the explicit case is auto-marked: a document whose title carries
    (المحدث)/"updated" alongside a twin with the same normalized title. Other
    near-duplicates are reported for review rather than guessed at — silently
    dropping a document that turns out to be distinct is worse than indexing
    one document twice.
    """
    by_norm: dict[str, list[str]] = {}
    for s in stems:
        by_norm.setdefault(normalize_title(s), []).append(s)
    superseded = {}
    for _norm, group in by_norm.items():
        if len(group) < 2:
            continue
        updated = [g for g in group if UPDATED_RE.search(g)]
        if len(updated) == 1:
            for g in group:
                if g != updated[0]:
                    superseded[g] = updated[0]
    return superseded


def build(verbose: bool = True):
    md_files = sorted(
        p for p in MD_DIR.glob("*.md")
        if p.name not in CORE_DOCS and not p.name.startswith("_")
    )
    superseded = detect_superseded([p.stem for p in md_files])

    all_chunks: list[dict] = []
    fam_files, fam_chunks = Counter(), Counter()
    toc_total = 0

    for p in md_files:
        extra = {}
        if p.stem in superseded:
            extra = {"superseded_by": superseded[p.stem]}
        chunks, family, toc = parse_document(p, {"title": p.stem, "extra": extra})
        fam_files[family] += 1
        fam_chunks[family] += len(chunks)
        toc_total += toc
        all_chunks.extend(chunks)

    all_chunks = split_oversized(all_chunks, max_chars=MAX_CORPUS_CHARS)

    if verbose:
        print(f"{len(md_files)} documents (core labour-law docs excluded)\n")
        names = {"A": "A regulations", "B": "B guides (numbered)",
                 "C": "C guides (headings)", "D": "D decisions"}
        print(f"{'family':<24}{'files':>7}{'chunks':>9}")
        for f in sorted(fam_files):
            print(f"{names[f]:<24}{fam_files[f]:>7}{fam_chunks[f]:>9}")
        print(f"{'TOTAL':<24}{len(md_files):>7}{len(all_chunks):>9}")
        print(f"\ntables of contents dropped : {toc_total}")
        print(f"documents marked superseded: {len(superseded)}")
        for old, new in superseded.items():
            print(f"    {old[:55]}\n      -> {new[:55]}")
        sizes = sorted(c["char_len"] for c in all_chunks)
        if sizes:
            print(f"\nchunk chars: min={sizes[0]} median={statistics.median(sizes):.0f} "
                  f"p95={sizes[int(0.95 * len(sizes))]} max={sizes[-1]}")
            print(f"under {MIN_CHUNK_CHARS} chars: {sum(1 for s in sizes if s < MIN_CHUNK_CHARS)}")

    return all_chunks


if __name__ == "__main__":
    build()

"""
Extract PDFs to Markdown using Azure Document Intelligence prebuilt-layout.

Usage:
    python -m app.ingestion.extract_pdfs                 # extract everything missing
    python -m app.ingestion.extract_pdfs --force         # re-extract everything
    python -m app.ingestion.extract_pdfs --workers 6     # tune parallelism
    python -m app.ingestion.extract_pdfs --dry-run       # cost/plan only, no API calls

Reads every PDF in data/raw/ and writes a matching .md file to data/markdown/.

Notes on this corpus (177 files / ~4.5k pages), which drive the design here:
  - Several files are byte-identical duplicates (`labor-law.pdf` vs
    `labor-law (1).pdf`, ...). They are analysed once and the markdown is
    copied to the other names, so we do not pay twice.
  - ~33 files have no text layer at all, so the run is genuinely OCR-bound and
    slow; requests are issued in parallel and each file is written as soon as
    it lands, making the run resumable after any interruption.
  - A manifest records page counts and the duplicate mapping so the chunker
    can tell real documents from copies.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import DocumentContentFormat
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
MD_DIR = ROOT / "data" / "markdown"
MANIFEST_PATH = ROOT / "data" / "markdown" / "_manifest.json"

MAX_RETRIES = 4
PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


def fmt_secs(s: float) -> str:
    return f"{int(s) // 60}m{int(s) % 60:02d}s"


class Progress:
    """Live counter for a long OCR run: files, billed pages, elapsed, ETA.

    Pages (not files) drive the ETA — this corpus ranges from 1-page decisions
    to a 611-page guide, so a file counter alone would be badly misleading.
    """

    def __init__(self, total_files: int, total_pages: int):
        self.total_files = total_files
        self.total_pages = max(total_pages, 1)
        self.files = 0
        self.pages = 0
        self.failed = 0
        self.start = time.time()

    def advance(self, pages: int, ok: bool) -> str:
        self.files += 1
        self.pages += pages
        if not ok:
            self.failed += 1
        frac = self.pages / self.total_pages
        elapsed = time.time() - self.start
        eta = elapsed / frac - elapsed if frac > 0 else 0
        filled = int(frac * 24)
        bar = "#" * filled + "." * (24 - filled)
        fail = f"  {self.failed} failed" if self.failed else ""
        return (f"  [{bar}] {frac * 100:5.1f}%  {self.files}/{self.total_files} files  "
                f"{self.pages}/{self.total_pages} pages  "
                f"elapsed {fmt_secs(elapsed)}  eta {fmt_secs(eta)}{fail}")


def get_client() -> DocumentIntelligenceClient:
    load_dotenv(ROOT / ".env")
    endpoint = os.environ["AZURE_AI_ENDPOINT"]
    key = os.environ["AZURE_AI_KEY"]
    return DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))


def file_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def page_count(path: Path) -> int:
    """Page count for cost reporting. Best-effort — never fatal."""
    try:
        import fitz

        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        return 0


def analyze(client: DocumentIntelligenceClient, pdf_path: Path) -> str:
    """Run prebuilt-layout, retrying on throttling and transient failures."""
    for attempt in range(MAX_RETRIES):
        try:
            with open(pdf_path, "rb") as f:
                poller = client.begin_analyze_document(
                    "prebuilt-layout",
                    body=f,
                    content_type="application/octet-stream",
                    output_content_format=DocumentContentFormat.MARKDOWN,
                )
            return poller.result().content
        except (HttpResponseError, ServiceRequestError) as e:
            status = getattr(e, "status_code", None)
            retryable = status in (429, 500, 502, 503, 504) or isinstance(e, ServiceRequestError)
            if not retryable or attempt == MAX_RETRIES - 1:
                raise
            backoff = 2**attempt * 5 + random.uniform(0, 3)
            log(f"  .. {pdf_path.name}: {status or 'network'}, retrying in {backoff:.0f}s")
            time.sleep(backoff)
    raise RuntimeError("unreachable")


def extract_one(client: DocumentIntelligenceClient, pdf_path: Path, out_path: Path, pages: int):
    start = time.time()
    log(f"  ..  {pdf_path.name} ({pages}p) sent")
    content = analyze(client, pdf_path)
    out_path.write_text(content, encoding="utf-8")
    log(f"  OK  {pdf_path.name} ({pages}p, {len(content)} chars, {time.time() - start:.0f}s)")
    return content


def build_plan(force: bool):
    """Group PDFs by content hash; pick one representative per group."""
    pdfs = sorted(RAW_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {RAW_DIR}")
        sys.exit(1)

    groups: dict[str, list[Path]] = {}
    for p in pdfs:
        groups.setdefault(file_hash(p), []).append(p)

    todo, skipped, copies = [], [], []
    for digest, members in groups.items():
        # Prefer a member that is already extracted, so re-runs never pay twice
        # for a document we happen to hold under two filenames.
        primary = next(
            (m for m in members if (MD_DIR / (m.stem + ".md")).exists()), members[0]
        )
        members = [primary] + [m for m in members if m != primary]
        out_path = MD_DIR / (primary.stem + ".md")
        pages = page_count(primary)
        if out_path.exists() and not force:
            skipped.append(primary)
        else:
            todo.append((primary, out_path, pages, digest))
        for dup in members[1:]:
            copies.append((dup, MD_DIR / (dup.stem + ".md"), out_path, digest))

    return pdfs, todo, skipped, copies


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Re-extract even if .md already exists")
    parser.add_argument("--workers", type=int, default=5, help="Parallel Azure requests")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan and cost, call nothing")
    args = parser.parse_args()

    MD_DIR.mkdir(parents=True, exist_ok=True)
    pdfs, todo, skipped, copies = build_plan(args.force)

    todo_pages = sum(t[2] for t in todo)
    print(f"{len(pdfs)} PDFs -> {len(todo)} to analyze, {len(skipped)} already extracted, "
          f"{len(copies)} duplicates copied from a twin")
    print(f"pages to bill: {todo_pages} (~${todo_pages / 1000 * 10:.2f} at $10/1000 pages)")
    if args.dry_run:
        for dup, _, src, _ in copies:
            print(f"  dup: {dup.name} == {src.stem}.pdf")
        return

    client = get_client()
    done, failed = [], []
    progress = Progress(len(todo), todo_pages)
    print(f"prebuilt-layout, {args.workers} parallel requests\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(extract_one, client, pdf, out, pages): (pdf, out, pages, digest)
            for pdf, out, pages, digest in todo
        }
        for future in as_completed(futures):
            pdf, out, pages, digest = futures[future]
            try:
                future.result()
                done.append((pdf, out, pages, digest))
                ok = True
            except Exception as e:
                log(f"  !!  {pdf.name}: {type(e).__name__}: {e}")
                failed.append((pdf, str(e)))
                ok = False
            log(progress.advance(pages, ok))

    # Duplicates: copy the twin's markdown rather than paying for it again.
    for dup, dup_out, src_out, digest in copies:
        if src_out.exists() and (args.force or not dup_out.exists()):
            dup_out.write_text(src_out.read_text(encoding="utf-8"), encoding="utf-8")

    manifest = {}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for pdf, out, pages, digest in done:
        manifest[pdf.name] = {"markdown": out.name, "pages": pages, "sha": digest, "duplicate_of": None}
    for dup, dup_out, src_out, digest in copies:
        manifest[dup.name] = {"markdown": dup_out.name, "sha": digest, "duplicate_of": src_out.name}
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\ndone: {len(done)} extracted, {len(copies)} duplicated, {len(failed)} failed")
    for pdf, err in failed:
        print(f"  FAILED {pdf.name}: {err}")
    if failed:
        print("\nRerun the same command to retry only the failures.")


if __name__ == "__main__":
    main()

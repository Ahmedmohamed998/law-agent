"""
Generate a topic phrase for each core legal article, cached to disk.

Why this exists: a law article's own text does not say what it is about. المادة
الحادية والستون governs the housing allowance, but its breadcrumb reads
"نظام العمل > الباب الخامس > الفصل الثاني الواجبات وقواعد التأديب > المادة
الحادية والستون" — the word سكن appears nowhere in it, and in the body it
occurs once inside a long list of employer duties. Meanwhile لائحة الوظائف
الصحية has a whole section headed بدل السكن. Measured, the guide wins every
time and the governing article is not retrieved at all.

Articles are numbered, not titled; the guides that restate them are titled.
This restores the missing title so the article can compete on its own subject.

The phrases are generated once and cached in data/chunks/article_topics.json,
so chunking stays deterministic and reproducible — a fresh LLM call per build
would silently reshuffle retrieval between runs.

    python -m app.ingestion.gen_topics            # fill in what is missing
    python -m app.ingestion.gen_topics --force    # regenerate everything
"""

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from openai import AzureOpenAI

ROOT = Path(__file__).resolve().parents[2]
CHUNKS_PATH = ROOT / "data" / "chunks" / "chunks.jsonl"
TOPICS_PATH = ROOT / "data" / "chunks" / "article_topics.json"

# Only the primary instruments: these are the ones titled by number.
CORE_SOURCES = {"labor_law", "executive_regulation"}
BATCH = 10

SYSTEM = """أنت خبير في أنظمة العمل السعودية.

ستصلك عدة مواد نظامية مرقّمة. لكل مادة، اكتب عبارة قصيرة تصف موضوعها بالضبط.

القواعد:
- من ٣ إلى ٨ كلمات فقط لكل مادة.
- استخدم الكلمات التي سيبحث بها الناس فعلاً (السكن، الأجر، الإجازة، الفصل،
  النقل، ساعات العمل، مكافأة نهاية الخدمة، إصابة العمل...).
- اذكر كل المواضيع الرئيسية في المادة إن تعددت، مفصولة بفواصل.
- لا تُعِد صياغة الحكم ولا تشرحه؛ صِف الموضوع فقط.
- لا تذكر رقم المادة ولا اسم النظام.

أعِد JSON فقط بالشكل: {"topics": {"1": "...", "2": "..."}}
حيث المفتاح هو رقم المادة كما ورد في المدخل."""


def client() -> AzureOpenAI:
    load_dotenv(ROOT / ".env")
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


def body_of(chunk: dict) -> str:
    return chunk["text"].split("\n\n", 1)[-1]


def do_batch(cli, deployment: str, batch: list[dict]) -> dict[str, str]:
    parts = []
    for i, c in enumerate(batch, 1):
        text = re.sub(r"<[^>]+>", " ", body_of(c))       # tables add no topic signal
        text = re.sub(r"\s+", " ", text).strip()[:900]
        parts.append(f"[{i}]\n{text}")
    try:
        resp = cli.chat.completions.create(
            model=deployment,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": "\n\n".join(parts)}],
            response_format={"type": "json_object"},
            max_completion_tokens=900,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"    batch failed: {type(e).__name__}: {str(e)[:90]}")
        return {}

    topics = data.get("topics") or {}
    out = {}
    for i, c in enumerate(batch, 1):
        v = topics.get(str(i))
        if isinstance(v, str):
            v = re.sub(r"\s+", " ", v).strip(" .،")
            # never let a stray article number into the searchable text
            v = re.sub(r"الماد[ةه]\s*\(?\s*\d{0,3}\s*\)?", "", v).strip(" .،-")
            if 6 <= len(v) <= 160:
                out[c["chunk_id"]] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    chunks = [json.loads(l) for l in open(CHUNKS_PATH, encoding="utf-8")]
    core = [c for c in chunks if c.get("source") in CORE_SOURCES]

    cached = {}
    if TOPICS_PATH.exists() and not args.force:
        cached = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))

    todo = [c for c in core if c["chunk_id"] not in cached]
    print(f"{len(core)} core chunks, {len(cached)} cached, {len(todo)} to generate")
    if not todo:
        return

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    cli = client()
    deployment = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for result in ex.map(lambda b: do_batch(cli, deployment, b), batches):
            cached.update(result)
            done += 1
            print(f"  batch {done}/{len(batches)}  ({len(cached)} topics)")

    TOPICS_PATH.write_text(json.dumps(cached, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"\nwrote {len(cached)} topics -> {TOPICS_PATH}")
    missing = [c["chunk_id"] for c in core if c["chunk_id"] not in cached]
    if missing:
        print(f"still missing {len(missing)} (rerun to retry): {missing[:5]}")


if __name__ == "__main__":
    main()

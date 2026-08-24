"""
Embed chunks.jsonl with AWS Bedrock Mantle (OpenAI-compatible) and store them
in a persistent Chroma collection at data/chroma/.

Usage:
    python -m app.ingestion.ingest            # ingest (skips if unchanged count)
    python -m app.ingestion.ingest --rebuild  # drop and re-ingest everything
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import chromadb
from dotenv import load_dotenv
import boto3

ROOT = Path(__file__).resolve().parents[2]
CHUNKS_PATH = ROOT / "data" / "chunks" / "chunks.jsonl"
CHROMA_DIR = ROOT / "data" / "chroma"
COLLECTION = "labor_law_ar"

EMBED_BATCH = 64

load_dotenv(ROOT / ".env")


def get_embed_client():
    """Return a boto3 Bedrock Runtime client for embeddings."""
    client = boto3.client(
        'bedrock-runtime',
        region_name=os.environ.get('AWS_BEDROCK_REGION', 'us-east-1'),
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    )
    model_id = os.environ.get('AWS_BEDROCK_EMBEDDING_MODEL', 'cohere.embed-multilingual-v3')
    return client, model_id


def embed_texts(client, model_id: str, texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        # Truncate each text to 2048 characters to satisfy Cohere API constraints
        batch = [t[:2048] for t in texts[i : i + EMBED_BATCH]]
        body = json.dumps({'texts': batch, 'input_type': 'search_document'})
        resp = client.invoke_model(
            body=body, modelId=model_id,
            contentType='application/json', accept='application/json'
        )
        out.extend(json.loads(resp['body'].read())['embeddings'])
        print(f'  embedded {min(i + EMBED_BATCH, len(texts))}/{len(texts)}')
    return out


def text_sha(text: str) -> str:
    """Fingerprint of a chunk's embedded text, stored alongside it so a later
    run can tell which chunks actually changed."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def to_chroma_metadata(chunk: dict) -> dict:
    """Chroma metadata values must be str/int/float/bool — no None, no lists."""
    meta = {"text_sha": text_sha(chunk["text"])}
    for k, v in chunk.items():
        if k in ("text", "chunk_id"):
            continue
        if v is None:
            continue
        if isinstance(v, list):
            v = " | ".join(map(str, v))
        meta[k] = v
    return meta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    chunks = [json.loads(l) for l in open(CHUNKS_PATH, encoding="utf-8")]
    print(f"{len(chunks)} chunks loaded from {CHUNKS_PATH.name}")

    db = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if args.rebuild:
        try:
            db.delete_collection(COLLECTION)
            print("dropped existing collection")
        except Exception:
            pass
    col = db.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})

    # NB: no early exit on matching counts. Chunk *text* can change while the
    # count stays identical (adding a topic line to every article does exactly
    # that), and a count check would silently skip re-embedding them. The
    # text_sha comparison below is the real test, and it reports "up to date"
    # when nothing changed.

    # Embed only what is new or whose text changed. Adding documents to the
    # corpus must not re-bill (or re-run) the chunks already embedded.
    todo = chunks
    if not args.rebuild and col.count():
        existing = col.get(include=["metadatas", "documents"])
        seen = {
            cid: (meta or {}).get("text_sha")
            for cid, meta in zip(existing["ids"], existing["metadatas"])
        }
        # Backfill for chunks embedded before text_sha existed: if the stored
        # document still matches chunks.jsonl, the embedding is valid — record
        # the fingerprint rather than paying to recompute it.
        by_id = {c["chunk_id"]: c for c in chunks}
        backfill_ids, backfill_metas = [], []
        for cid, doc, meta in zip(existing["ids"], existing["documents"], existing["metadatas"]):
            if seen.get(cid) is None and cid in by_id and doc == by_id[cid]["text"]:
                sha = text_sha(doc)
                seen[cid] = sha
                backfill_ids.append(cid)
                backfill_metas.append({**(meta or {}), "text_sha": sha})
        if backfill_ids:
            col.update(ids=backfill_ids, metadatas=backfill_metas)
            print(f"backfilled text_sha for {len(backfill_ids)} already-embedded chunks")
        todo = [c for c in chunks if seen.get(c["chunk_id"]) != text_sha(c["text"])]
        stale = set(seen) - {c["chunk_id"] for c in chunks}
        if stale:
            col.delete(ids=list(stale))
            print(f"removed {len(stale)} chunks no longer in chunks.jsonl")
        print(f"{len(chunks) - len(todo)} unchanged (skipped), {len(todo)} to embed")
        if not todo:
            print(f"collection '{COLLECTION}' has {col.count()} documents — up to date")
            return

    client, model_id = get_embed_client()
    texts = [c["text"] for c in todo]
    embeddings = embed_texts(client, model_id, texts)

    chunks = todo
    ids = [c["chunk_id"] for c in chunks]
    metas = [to_chroma_metadata(c) for c in chunks]
    # upsert in batches (Chroma has a max batch size)
    B = 200
    for i in range(0, len(chunks), B):
        col.upsert(
            ids=ids[i : i + B],
            documents=texts[i : i + B],
            embeddings=embeddings[i : i + B],
            metadatas=metas[i : i + B],
        )
    print(f"collection '{COLLECTION}' now has {col.count()} documents at {CHROMA_DIR}")


if __name__ == "__main__":
    main()

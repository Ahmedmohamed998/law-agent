"""
app/rag/bedrock_client.py
=========================
Shared AWS Bedrock helpers used by the entire RAG + chat layer.

Gemma 4 on AWS Bedrock is served via the bedrock-mantle endpoint, which
exposes an OpenAI-compatible API. We reuse the already-installed `openai`
package and point it at:
    https://bedrock-mantle.{region}.api.aws/openai/v1

Auth uses the MANTLE_BEARER_TOKEN env var as the API key (Bearer token).

NOTE ON EMBEDDINGS
------------------
The Chroma vector store was built with Azure text-embedding-3-small (1536-dim).
The _embed() call in retriever.py still uses the Azure embedding client to keep
the existing index valid. All *chat/completion* calls use this module.
"""

import os
from openai import OpenAI

# ── AWS / Bedrock config ──────────────────────────────────────────────────────
AWS_REGION       = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")
AWS_BEARER_TOKEN = os.environ.get("MANTLE_BEARER_TOKEN", "")
INFERENCE_PROFILE = os.environ.get(
    "AWS_BEDROCK_INFERENCE_PROFILE_ID", "google.gemma-4-31b"
)

# Bedrock Mantle base URL (OpenAI-compatible)
_MANTLE_BASE_URL = f"https://bedrock-mantle.{AWS_REGION}.api.aws/openai/v1"


def _client() -> OpenAI:
    """Return an OpenAI client wired to the Bedrock Mantle endpoint.

    A new client object is cheap; instantiating once at module load fails
    if env vars aren't set yet (e.g. during test collection with mocked env).
    """
    return OpenAI(
        api_key=AWS_BEARER_TOKEN or os.environ.get("MANTLE_BEARER_TOKEN", ""),
        base_url=_MANTLE_BASE_URL,
    )


def bedrock_chat(
    messages: list[dict],
    max_tokens: int = 2048,
    response_format: dict | None = None,
) -> str:
    """Call Gemma 4 via Bedrock Mantle. Returns the model's text response.

    Args:
        messages: OpenAI-format list of dicts with 'role' and 'content'.
        max_tokens: Maximum output token count.
        response_format: Passed through if provided (e.g. {"type": "json_object"}).
                         Gemma may not honour it; callers must handle malformed JSON.

    Returns:
        The model's text response as a plain string.

    Raises:
        Exception: Propagates API errors to the caller.
    """
    kwargs: dict = {
        "model": INFERENCE_PROFILE,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    # Pass response_format only when explicitly requested; Gemma may reject
    # unknown fields, so don't add it unconditionally.
    if response_format is not None:
        kwargs["response_format"] = response_format

    client = _client()
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def bedrock_chat_stream(messages: list[dict], max_tokens: int = 2048):
    """Streaming version of bedrock_chat. Yields raw OpenAI stream events.

    Usage:
        stream = bedrock_chat_stream(messages)
        for event in stream:
            piece = event.choices[0].delta.content if event.choices else None
            ...
    """
    client = _client()
    return client.chat.completions.create(
        model=INFERENCE_PROFILE,
        messages=messages,
        max_tokens=max_tokens,
        stream=True,
    )


def bedrock_usage(response) -> tuple[int | None, int | None]:
    """Extract (prompt_tokens, completion_tokens) from a non-streaming response."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    return getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)

"""
bedrock_client.py
=================
Shared AWS client helpers for:
  - Bedrock Mantle OpenAI-compatible endpoint (google.gemma-4-31b) — chat completions
  - Amazon Polly — text-to-speech

Gemma 4 on AWS Bedrock is served via the bedrock-mantle endpoint, which exposes
an OpenAI-compatible API. We reuse the already-installed `openai` package and
point it at:
    https://bedrock-mantle.{region}.api.aws/openai/v1

Auth uses the AWS_BEARER_TOKEN_BEDROCK env var as the API key (Bearer token).
"""

import os
import io
import boto3
from openai import OpenAI

# ── AWS / Bedrock config ──────────────────────────────────────────────────────
AWS_REGION        = os.environ.get("AWS_BEDROCK_REGION", "us-east-1")
AWS_BEARER_TOKEN  = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY    = os.environ.get("AWS_SECRET_ACCESS_KEY")
INFERENCE_PROFILE = os.environ.get("AWS_BEDROCK_INFERENCE_PROFILE_ID", "google.gemma-4-31b")

# Bedrock Mantle base URL (OpenAI-compatible)
_MANTLE_BASE_URL = f"https://bedrock-mantle.{AWS_REGION}.api.aws/openai/v1"

# ── Bedrock Mantle OpenAI-compatible client ───────────────────────────────────
_bedrock_oai = OpenAI(
    api_key=AWS_BEARER_TOKEN,
    base_url=_MANTLE_BASE_URL,
)

# ── Polly client (boto3) ──────────────────────────────────────────────────────
_polly = boto3.client(
    "polly",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_KEY,
)


# ── Public helpers ─────────────────────────────────────────────────────────────

def bedrock_chat(
    messages: list,
    system_prompt: str = "",
    max_tokens: int = 600,
) -> str:
    """
    Call Gemma 4 via AWS Bedrock Mantle (OpenAI-compatible endpoint).

    Args:
        messages: List of dicts with keys 'role' ('user'|'assistant') and 'content' (str).
                  'system' role entries are skipped (pass system text via system_prompt).
        system_prompt: Optional system-level instruction string.
        max_tokens: Maximum output token count.

    Returns:
        The model's text response as a plain string.

    Raises:
        Exception: Propagates API errors to the caller.
    """
    oai_messages = []

    if system_prompt:
        oai_messages.append({"role": "system", "content": system_prompt})

    for m in messages:
        if m["role"] == "system":
            continue  # already handled above
        oai_messages.append({"role": m["role"], "content": m["content"]})

    response = _bedrock_oai.chat.completions.create(
        model=INFERENCE_PROFILE,
        messages=oai_messages,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


def polly_tts(text: str, language: str = "en") -> bytes:
    """
    Synthesize speech using Amazon Polly.

    Args:
        text: Text to speak (capped at 3000 chars).
        language: 'ar' for Arabic, 'en' for English.

    Returns:
        Raw MP3 bytes.

    Raises:
        Exception: Propagates Polly/boto3 errors to the caller.
    """
    # Voice mapping — use Neural engine where available.
    # Hala (arb) is Neural; Joanna (en-US) is Neural.
    VOICE_MAP = {
        "ar": ("Hala",   "neural", "arb"),
        "en": ("Joanna", "neural", "en-US"),
    }
    voice_id, engine, lang_code = VOICE_MAP.get(language, VOICE_MAP["en"])

    response = _polly.synthesize_speech(
        Text=text[:3000],
        OutputFormat="mp3",
        VoiceId=voice_id,
        Engine=engine,
        LanguageCode=lang_code,
    )
    return response["AudioStream"].read()

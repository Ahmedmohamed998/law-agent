"""
Service configuration.

Only the *service* layer reads this. The RAG modules keep reading os.environ
directly for their Azure credentials, so `app/rag/*` stays importable with no
database and no settings object — which is what lets eval/scenarios.py and the
Gradio harness run unchanged.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # -- database ----------------------------------------------------------
    # The application connects as a role with no grant on `public`; migrations
    # run as the owner. Two URLs, deliberately, so the app cannot DDL its own
    # tables and RLS is not bypassed by ownership.
    database_url: str = Field(
        default="postgresql+psycopg://ai_service@localhost:5432/law_agent"
    )
    database_migrate_url: str = Field(
        default="postgresql+psycopg://law_agent_owner@localhost:5432/law_agent"
    )
    # One uvicorn worker serves requests on a threadpool; a pool smaller than
    # that threadpool makes threads queue on connections instead of on Azure.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    # -- identity ----------------------------------------------------------
    # Verification only. This service never issues a token and never holds a
    # signing key: the product backend signs with RS256/EdDSA and publishes
    # public keys at jwks_url.
    jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = "law-agent-ai"
    jwt_algorithms: tuple[str, ...] = ("RS256", "EdDSA")
    jwks_cache_seconds: int = 3600
    # Escape hatch for local development only; refuses to apply when
    # jwks_url is set, so it cannot silently disable verification in prod.
    dev_allow_unverified_tokens: bool = False

    # -- conversation ------------------------------------------------------
    # Past four turns, because beyond that an earlier scenario's story starts
    # polluting the query plan for a new topic.
    history_turns: int = 4
    summarize_after_turns: int = 6

    # -- retrieval ---------------------------------------------------------
    # Recorded on every message so a complaint about an old answer can be told
    # apart from a re-ingest that moved the chunk ids underneath it.
    index_version: str = "unknown"

    # -- service-to-service ------------------------------------------------
    # Shared key for /v1/admin/* and the erasure endpoint. These are called by
    # another *service*, not by a person, so they authenticate with a key
    # rather than a user token — modelling them as a very privileged user
    # would mean a credential that can also hold conversations.
    #
    # Unset means those endpoints refuse to run at all. An unauthenticated
    # erasure endpoint is worse than a missing one.
    admin_api_key: str = ""

    # -- message allowance -------------------------------------------------
    # Totals per user, across every conversation, never reset. An anonymous
    # visitor who signs up keeps the same user id, so what they sent before
    # counts towards the registered total — 5 then 15 more is a total of 20.
    #
    # Staff roles are exempt: a lawyer reviewing a case must not be locked out
    # of the tool by their own testing.
    anon_message_limit: int = 5
    registered_message_limit: int = 20
    unlimited_roles: tuple[str, ...] = ("owner", "admin", "lawyer")

    # -- voice -------------------------------------------------------------
    # Speech in (Amazon Transcribe streaming) and speech out (Amazon Polly),
    # using the same IAM credentials as embeddings. Region is separate from
    # Bedrock's so either can move without the other.
    speech_region: str = "us-east-1"
    transcribe_language: str = "ar-SA"
    # Longest recording accepted. The widget stops at the same length; this is
    # the backstop against a client that does not.
    voice_max_seconds: int = 60
    # Zeina (standard engine) is the Arabic voice available everywhere Polly
    # is. Try Hala with the neural engine for a Gulf accent once it is
    # confirmed in this region. The model answers in the question's language,
    # so English answers get an English voice.
    polly_voice_id: str = "Zeina"
    polly_engine: str = "standard"
    polly_voice_id_en: str = "Joanna"
    polly_engine_en: str = "standard"

    # -- cors --------------------------------------------------------------
    # The frontend calls this service directly for chat, because proxying SSE
    # buffers it — so CORS is ours to get right. Configurable because the real
    # origin is not known at development time and a hardcoded pattern is a
    # code change at the worst possible moment.
    cors_origin_regex: str = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

    # -- binding ------------------------------------------------------------
    # Loopback by default, because on a bare host this service must not be
    # reachable except through the reverse proxy that adds TLS and turns off
    # response buffering.
    #
    # In a container 127.0.0.1 is the container's own loopback, so nothing —
    # not even the host — can reach it; there the value is 0.0.0.0 and the
    # privacy comes from publishing the port as 127.0.0.1:8000 instead.
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    # -- service suggestions ----------------------------------------------
    # The one place this service knows the product backend exists: it reads
    # the public catalogue (GET /services) so the assistant can propose a
    # service under an answer. Read-only, cached, and optional — with no
    # backend URL there are simply no suggestions.
    backend_url: str = ""
    services_refresh_seconds: int = 300
    # Below this the classifier's pick is discarded. Start strict; lower it
    # only with the evaluation set (tests/suggestions.jsonl) in hand.
    suggest_min_confidence: float = 0.75
    # "upsell": suggest whenever a service clearly fits, even under a good
    # answer. "rescue": only when the answer was refused or asked to book.
    suggest_mode: str = "upsell"
    # Messages shorter than this are greetings and thanks, not needs.
    suggest_min_words: int = 4
    # How many turns of context the classifier sees besides the message.
    suggest_history_turns: int = 3

    @property
    def auth_enforced(self) -> bool:
        return bool(self.jwks_url)

    @property
    def suggestions_enabled(self) -> bool:
        return bool(self.backend_url)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()

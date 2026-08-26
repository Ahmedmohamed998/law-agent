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

    @property
    def auth_enforced(self) -> bool:
        return bool(self.jwks_url)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()

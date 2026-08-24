"""
Product backend configuration.

Every setting is read with the `PRODUCT_` prefix from `product/.env`, so this
service and the AI service can sit in one checkout without their environments
colliding. `DATABASE_URL` here is a different role on the same cluster than
`DATABASE_URL` there, and silently sharing one variable would mean the identity
service connecting as `ai_service` — a role with no grant on `public`.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_DIR = ROOT / "product"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PRODUCT_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="PRODUCT_",
        extra="ignore",
    )

    # -- database ----------------------------------------------------------
    # Same split as the AI service and for the same reason: the application
    # role owns nothing, so it cannot DDL its own tables.
    database_url: str = Field(
        default="postgresql+psycopg://product_service@localhost:5432/law_agent"
    )
    database_migrate_url: str = Field(
        default="postgresql+psycopg://product_owner@localhost:5432/law_agent"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # -- token signing -----------------------------------------------------
    # This service is the ONLY holder of a private key in the system. The AI
    # service fetches the public half from `jwks_path` and verifies; it can
    # never mint. Losing that asymmetry is the one change that would collapse
    # the whole trust boundary, so the private key never leaves this process.
    keys_dir: Path = PRODUCT_DIR / "keys"
    # Which `kid` signs new tokens. Empty means "the newest key in keys_dir",
    # which is what you want in development and never what you want in
    # production, where a rotation should be an explicit config change.
    active_kid: str = ""
    jwt_algorithm: str = "RS256"

    jwt_issuer: str = "http://127.0.0.1:8001"
    # A list, not a string. One token is valid at both APIs; PyJWT's
    # `audience=` check passes when the claim is a list containing the
    # expected value, so the AI service needs no change to accept this.
    jwt_audiences: tuple[str, ...] = ("law-agent-ai", "law-agent-product")

    # Short, because expiry is the only revocation this design has. A stolen
    # access token is valid until it expires and there is no denylist; 15
    # minutes bounds that, and the refresh token carries the long session.
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30

    # -- passwords ---------------------------------------------------------
    password_min_length: int = 10

    # -- erasure -----------------------------------------------------------
    # There is no foreign key from ai.sessions to public.users, so deleting a
    # user here cascades nothing there. This is the address we tell.
    ai_service_url: str = "http://127.0.0.1:8000"
    ai_erasure_timeout_seconds: float = 10.0

    # -- maintenance -------------------------------------------------------
    # Shared key for the service-to-service endpoints under /admin. Unset
    # means those endpoints refuse to run at all — an unauthenticated erasure
    # endpoint is worse than a missing one.
    admin_api_key: str = ""

    # -- cors --------------------------------------------------------------
    cors_origin_regex: str = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

    @property
    def jwks_url(self) -> str:
        """What to put in the AI service's JWKS_URL."""
        return f"{self.jwt_issuer.rstrip('/')}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()

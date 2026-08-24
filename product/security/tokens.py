"""
Token minting: short-lived signed access tokens, long-lived opaque refresh
tokens.

The two are deliberately different kinds of object. The access token is a JWT
because the AI service must be able to validate it with no call back here — it
holds the public key and nothing else. The refresh token is NOT a JWT: it is
opaque random bytes, meaningful only against a row in `refresh_tokens`, because
it must be revocable, and a self-contained signed token cannot be revoked
without inventing the very lookup this avoids.

That asymmetry is the whole reason access TTL is minutes. There is no denylist
for access tokens; expiry is the only revocation they have.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from product.config import settings
from product.security import keys

# 32 bytes of urandom, base64url. Long enough that guessing is not a threat
# model, short enough to sit in a JSON body and a cookie without complaint.
REFRESH_TOKEN_BYTES = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_at: datetime
    expires_in: int
    kid: str


def issue_access_token(
    *,
    user_id: str,
    organization_id: str | None = None,
    role: str | None = None,
    anonymous: bool = False,
) -> AccessToken:
    """Sign an access token for this user.

    The claim names are fixed by what the AI service reads in
    `app/api/auth.py` — `sub`, `org_id`, `role`, `anon` — and changing one here
    silently breaks the other service, which is why they are not configurable.
    """
    s = settings()
    key = keys.active()

    issued = _now()
    expires = issued + timedelta(seconds=s.access_token_ttl_seconds)

    claims: dict = {
        "iss": s.jwt_issuer,
        # A list. PyJWT's `audience=` check passes when the claim is a list
        # containing the expected value, so one token is valid at both APIs
        # and the AI service needs no change to accept it.
        "aud": list(s.jwt_audiences),
        "sub": user_id,
        "iat": int(issued.timestamp()),
        "nbf": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        # Unique per token. Nothing consumes it today; it is here so that
        # adding a denylist later does not require re-issuing every token in
        # circulation to give them identities.
        "jti": secrets.token_urlsafe(12),
        # Distinguishes this from any other signed artefact this service might
        # later produce (email links, invites). A verifier that assumes every
        # token it holds is an access token is one feature away from being
        # wrong.
        "typ": "access",
    }
    if organization_id:
        claims["org_id"] = organization_id
    if role:
        claims["role"] = role
    if anonymous:
        claims["anon"] = True

    token = jwt.encode(
        claims,
        key.private_key,
        algorithm=s.jwt_algorithm,
        # The `kid` is what lets the AI service pick the right public key out
        # of a JWKS holding several. Without it, rotation means trying every
        # key and hoping.
        headers={"kid": key.kid},
    )
    return AccessToken(
        token=token,
        expires_at=expires,
        expires_in=s.access_token_ttl_seconds,
        kid=key.kid,
    )


def new_refresh_token() -> tuple[str, str]:
    """Returns (plaintext, sha256_hex).

    The plaintext is returned to the caller exactly once, in the response that
    created it. Only the hash is stored, so a database dump yields nothing that
    can be replayed.
    """
    plaintext = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return plaintext, hash_refresh_token(plaintext)


def hash_refresh_token(plaintext: str) -> str:
    """Plain SHA-256, deliberately not a password hash.

    Argon2 is correct for passwords because they are low-entropy and guessable.
    This value is 256 bits of urandom: there is nothing to brute-force, and a
    deliberately slow hash here would only add latency to every refresh.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return _now() + timedelta(seconds=settings().refresh_token_ttl_seconds)


def decode_own_token(token: str) -> dict:
    """Verify a token this service issued.

    Selects the key by the token's own `kid`, not by whichever key is currently
    active — during a rotation the two differ, and validating only against the
    active key would reject every still-valid token signed by the outgoing one.
    That is precisely the bug this service exists to avoid causing in others.
    """
    s = settings()
    kid = jwt.get_unverified_header(token).get("kid")
    available = keys.load()
    key = available.get(kid) if kid else None
    if key is None:
        raise jwt.InvalidKeyError(f"unknown kid: {kid!r}")
    return jwt.decode(
        token,
        key.public_key,
        algorithms=[s.jwt_algorithm],
        audience=list(s.jwt_audiences),
        issuer=s.jwt_issuer,
        options={"require": ["exp", "sub"]},
    )

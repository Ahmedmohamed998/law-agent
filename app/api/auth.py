"""
Token verification. This service never issues a token and holds no signing key.

The product backend signs with RS256/EdDSA and publishes its public keys at a
JWKS URL; we cache those and verify. A shared symmetric secret was rejected
deliberately: it would let this service mint tokens for the product domain, and
it would make key rotation a coordinated deploy of two services instead of one.
"""

import hmac
import threading
import time

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

from app.config import settings
from app.db.session import Principal

_jwk_client: PyJWKClient | None = None
_jwk_lock = threading.Lock()
_jwk_built_at = 0.0


def _jwks() -> PyJWKClient:
    global _jwk_client, _jwk_built_at
    s = settings()
    with _jwk_lock:
        stale = time.time() - _jwk_built_at > s.jwks_cache_seconds
        if _jwk_client is None or stale:
            _jwk_client = PyJWKClient(s.jwks_url, cache_keys=True)
            _jwk_built_at = time.time()
        return _jwk_client


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthenticated", "message": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


def principal(authorization: str = Header(default="")) -> Principal:
    """The verified caller.

    Identity comes from here and nowhere else. No endpoint accepts a user id in
    a path, query, or body — otherwise reading someone else's conversations is
    a matter of editing one JSON field.
    """
    s = settings()

    if not s.auth_enforced:
        # Local development only, and it cannot apply once JWKS_URL is set —
        # so it is impossible to accidentally ship with verification disabled.
        if not s.dev_allow_unverified_tokens:
            raise HTTPException(
                status_code=503,
                detail={"code": "auth_unconfigured",
                        "message": "JWKS_URL is not set"},
            )
        token = authorization.removeprefix("Bearer ").strip()
        return Principal(user_id=token or "dev-user", organization_id=None)

    if not authorization.startswith("Bearer "):
        raise _unauthorized("missing bearer token")
    token = authorization[7:].strip()

    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=list(s.jwt_algorithms),
            # `aud` must name THIS service: a token minted for the product API
            # is not a token for the AI API, and accepting one silently widens
            # the blast radius of any leaked product token.
            audience=s.jwt_audience,
            issuer=s.jwt_issuer or None,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized(str(exc)[:120]) from exc

    return Principal(
        user_id=str(claims["sub"]),
        organization_id=claims.get("org_id"),
        role=claims.get("role", "client"),
        anonymous=bool(claims.get("anon", False)),
    )


CurrentPrincipal = Depends(principal)

# Roles that may read a client's conversation from the dashboard. Matches the
# product backend's StaffGuard.
STAFF_ROLES = ("owner", "admin")


def staff_principal(authorization: str = Header(default="")) -> Principal:
    """A verified member of staff, reading as themselves.

    This is how the dashboard reads a transcript, and it is deliberately NOT
    the admin key. The key is shared by services, cannot be attributed to a
    person or revoked for one, and the service-to-service endpoints are
    promised never to read a conversation. A staff token names who looked, and
    dies in fifteen minutes.
    """
    p = principal(authorization)
    if p.anonymous or p.role not in STAFF_ROLES:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden",
                    "message": "reading conversations requires an admin or owner role"},
        )
    return p


def admin_key(x_admin_key: str = Header(default="")) -> None:
    """Authorization for the service-to-service endpoints.

    A shared header rather than a user token, because the callers are other
    *services*: the product backend telling us a user exercised erasure, and
    the payments service telling us a consultation was paid for. Neither has a
    user token to present — a webhook from a payment gateway carries no
    identity at all.

    This is deliberately NOT a way to act as a user. It grants exactly the two
    operations the endpoints expose, both of which are already outside the
    per-user model: erasure is cross-tenant by definition, and escalation is a
    state change the user is not the one making.
    """
    configured = settings().admin_api_key
    if not configured:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "admin_unconfigured",
                "message": "ADMIN_API_KEY is not set",
            },
        )
    # Constant-time: a plain `!=` leaks the key one byte at a time to anyone
    # willing to measure enough requests.
    if not hmac.compare_digest(x_admin_key, configured):
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "bad admin key"},
        )


AdminKey = Depends(admin_key)

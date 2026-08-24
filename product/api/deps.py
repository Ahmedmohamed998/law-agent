"""
Request-scoped dependencies: the caller's identity, and the admin key.

This service verifies its OWN tokens locally, against the private keys it
already holds — there is no HTTP hop and no JWKS fetch, because the issuer and
the verifier are the same process. The AI service does the opposite and that
asymmetry is correct: it must never be able to reach a private key.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession, selectinload

from product.config import settings
from product.db.models import User
from product.security import tokens


@dataclass(frozen=True, slots=True)
class Caller:
    user_id: str
    organization_id: str | None
    role: str | None
    anonymous: bool
    claims: dict


def unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthenticated", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden(message: str) -> HTTPException:
    return HTTPException(
        status_code=403, detail={"code": "forbidden", "message": message}
    )


def not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def caller(authorization: str = Header(default="")) -> Caller:
    """The verified caller, from the bearer token and nowhere else."""
    if not authorization.startswith("Bearer "):
        raise unauthorized("missing bearer token")
    token = authorization[7:].strip()

    try:
        claims = tokens.decode_own_token(token)
    except jwt.PyJWTError as exc:
        raise unauthorized(str(exc)[:120]) from exc

    if claims.get("typ") != "access":
        # A refresh token is opaque and could never decode here, but a future
        # signed artefact (an invite link, an email confirmation) would. Reject
        # anything that is not explicitly an access token rather than
        # discovering later that a password-reset link was also a login.
        raise unauthorized("not an access token")

    return Caller(
        user_id=str(claims["sub"]),
        organization_id=claims.get("org_id"),
        role=claims.get("role"),
        anonymous=bool(claims.get("anon", False)),
        claims=claims,
    )


def require_registered(c: Caller) -> Caller:
    """Reject anonymous callers.

    Anonymous users may hold a conversation; they may not own organizations,
    add members, or delete accounts.
    """
    if c.anonymous:
        raise forbidden("this action requires a registered account")
    return c


def admin_key(x_admin_key: str = Header(default="")) -> None:
    """Service-to-service authorization for the maintenance endpoints.

    A shared header rather than a user token on purpose: erasure is called by
    another *service*, not by a person, and modelling it as a very privileged
    user account would mean a credential that can also log in.
    """
    configured = settings().admin_api_key
    if not configured:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "admin_unconfigured",
                "message": "PRODUCT_ADMIN_API_KEY is not set",
            },
        )
    # Constant-time comparison: a plain `!=` leaks the key one byte at a time
    # to anyone willing to measure enough requests.
    import hmac

    if not hmac.compare_digest(x_admin_key, configured):
        raise forbidden("bad admin key")


def load_user(db: SASession, user_id: str) -> User:
    row = db.scalar(
        select(User)
        .where(User.id == user_id, User.deleted_at.is_(None))
        .options(selectinload(User.memberships))
    )
    if row is None:
        raise not_found("user_not_found", "no such user")
    if row.status != "active":
        raise forbidden("account is disabled")
    return row


def now() -> datetime:
    return datetime.now(timezone.utc)

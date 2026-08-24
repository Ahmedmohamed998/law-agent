"""
Signup, login, refresh, logout, profile — and the anonymous path that makes the
funnel work.

The one design decision here worth reading before changing anything: an
anonymous user is a **real user row** with a real id, and signup UPGRADES that
row in place rather than creating a second one. The `sub` in their token never
changes, so the conversations the AI service already stored under that id are
still theirs after they register.

The alternative — minting a throwaway `anon_7f3c` subject and reassigning
sessions at signup — needs a cross-service call, an endpoint on the AI side
that does not exist, and a window where a crash strands someone's history in a
subject nobody owns. Keeping the id stable deletes that whole problem instead
of solving it.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession, selectinload

from product.api import deps
from product.api.deps import Caller, forbidden, unauthorized
from product.api.schemas import (
    IntrospectOut,
    LoginIn,
    LogoutIn,
    MembershipOut,
    RefreshIn,
    SignupIn,
    TokenOut,
    UserOut,
)
from product.config import settings
from product.db.base import tx
from product.db.ids import new_id
from product.db.models import RefreshToken, User
from product.security import passwords, tokens

log = logging.getLogger("product.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def normalize_email(raw: str) -> str:
    """Lowercased and stripped. The unique index enforces exactly this form, so
    every write path must agree on it or the constraint protects nothing."""
    return raw.strip().lower()


# ── claim context ─────────────────────────────────────────────────────────


def _claim_context(
    user: User, requested_org: str | None
) -> tuple[str | None, str | None]:
    """Which (org_id, role) go into the token.

    A user in no organization gets neither claim — the AI service reads that as
    an individual and falls back to `user_id` filtering. A user in exactly one
    gets it implicitly. A user in several must be explicit, and the oldest
    membership is the default rather than an error, because failing a login
    over an ambiguity the client did not know existed is a bad first
    experience.
    """
    memberships = sorted(user.memberships, key=lambda m: m.created_at)
    if not memberships:
        return None, None

    if requested_org:
        for m in memberships:
            if m.organization_id == requested_org:
                return m.organization_id, m.role
        raise forbidden("not a member of that organization")

    first = memberships[0]
    return first.organization_id, first.role


def _user_out(user: User) -> UserOut:
    return UserOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_anonymous=user.is_anonymous,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        memberships=[
            MembershipOut(
                organization_id=m.organization_id,
                organization_name=m.organization.name,
                role=m.role,
            )
            for m in user.memberships
        ],
    )


def _issue(
    db: SASession,
    user: User,
    *,
    requested_org: str | None,
    request: Request,
    family_id: str | None = None,
) -> TokenOut:
    """Mint an access token and a fresh refresh token for this user."""
    org_id, role = _claim_context(user, requested_org)

    access = tokens.issue_access_token(
        user_id=user.id,
        organization_id=org_id,
        role=role,
        anonymous=user.is_anonymous,
    )

    plaintext, token_hash = tokens.new_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        # A rotation stays in its family; a fresh login starts a new one. That
        # is what lets a leak revoke one device's chain without logging the
        # user out everywhere.
        family_id=family_id or new_id(),
        token_hash=token_hash,
        expires_at=tokens.refresh_expiry(),
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        ip=(request.client.host if request.client else None),
    )
    db.add(row)
    db.flush()

    return TokenOut(
        access_token=access.token,
        expires_in=access.expires_in,
        refresh_token=plaintext,
        user=_user_out(user),
    )


# ── anonymous ─────────────────────────────────────────────────────────────


@router.post("/anonymous", response_model=TokenOut, status_code=201)
def anonymous(request: Request):
    """A signed token for someone who has not signed up yet.

    Deliberately a real token and a real user row, not a bypass. The AI service
    has one identity mechanism and this goes through it — which means anonymous
    conversations are tenant-scoped, rate-limitable, and erasable like any
    other, instead of being a special case in every downstream query.
    """
    with tx() as db:
        user = User(is_anonymous=True, email=None, password_hash=None)
        db.add(user)
        db.flush()
        db.refresh(user, ["memberships"])
        return _issue(db, user, requested_org=None, request=request)


# ── signup ────────────────────────────────────────────────────────────────


@router.post("/signup", response_model=TokenOut, status_code=201)
def signup(
    body: SignupIn,
    request: Request,
    authorization: str = Header(default=""),
):
    """Register. If called with an anonymous token, upgrades that user in place.

    Upgrading keeps the user id, so every conversation already stored against
    it on the AI side simply belongs to the registered account afterwards. No
    cross-service call, no reassignment endpoint, no window where history is
    orphaned.
    """
    email = normalize_email(body.email)
    password_hash = passwords.hash_password(body.password)

    # An anonymous token in the header means "this is the same person" — but it
    # is only trusted after verification, like any other identity claim.
    upgrading: str | None = None
    if authorization.startswith("Bearer "):
        try:
            c = deps.caller(authorization)
            if c.anonymous:
                upgrading = c.user_id
        except HTTPException:
            # An expired or invalid anonymous token is not a reason to refuse a
            # signup — it just means we cannot carry the history over.
            upgrading = None

    with tx() as db:
        user: User | None = None
        if upgrading:
            user = db.scalar(
                select(User)
                .where(User.id == upgrading, User.deleted_at.is_(None))
                .options(selectinload(User.memberships))
                .with_for_update()
            )
            if user is not None and not user.is_anonymous:
                # The token said anonymous but the row is not: it was already
                # upgraded by a concurrent request. Treat as a plain signup.
                user = None

        if user is None:
            user = User(email=email, password_hash=password_hash, is_anonymous=False)
            db.add(user)
        else:
            user.email = email
            user.password_hash = password_hash
            user.is_anonymous = False
            user.updated_at = deps.now()

        if body.display_name:
            user.display_name = body.display_name
        user.last_login_at = deps.now()

        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            # The partial unique index on email. Same message either way — a
            # distinct "already registered" reply turns signup into an oracle
            # for which addresses hold accounts.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "email_unavailable",
                    "message": "that email cannot be registered",
                },
            ) from exc

        db.refresh(user, ["memberships"])
        return _issue(db, user, requested_org=None, request=request)


# ── login ─────────────────────────────────────────────────────────────────


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request):
    email = normalize_email(body.email)

    with tx() as db:
        user = db.scalar(
            select(User)
            .where(
                User.email == email,
                User.deleted_at.is_(None),
                User.is_anonymous.is_(False),
            )
            .options(selectinload(User.memberships))
        )

        if user is None:
            # Spend the same time as a real verification so the response does
            # not answer "does this address have an account?".
            passwords.dummy_verify()
            raise unauthorized("invalid email or password")

        if not passwords.verify_password(user.password_hash, body.password):
            raise unauthorized("invalid email or password")

        if user.status != "active":
            raise forbidden("account is disabled")

        # The only moment the plaintext is in hand — upgrade a hash made with
        # older parameters rather than asking the user to change their password.
        if user.password_hash and passwords.needs_rehash(user.password_hash):
            user.password_hash = passwords.hash_password(body.password)

        user.last_login_at = deps.now()
        return _issue(
            db, user, requested_org=body.organization_id, request=request
        )


# ── refresh ───────────────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request):
    """Rotate the refresh token and mint a new access token.

    Reuse detection: presenting a token that was already spent means it leaked,
    because the legitimate client always holds the newest one. At that point
    the attacker and the user are indistinguishable, so the entire family is
    revoked and both must log in again.
    """
    token_hash = tokens.hash_refresh_token(body.refresh_token)

    # The revocation on the reuse path CANNOT happen inside this transaction:
    # `tx()` rolls back on any exception, so revoking and then raising a 401
    # would undo the revocation and leave the leaked family alive — a reuse
    # detector that detects and does nothing. Carry the decision out of the
    # transaction instead, then commit the revocation on its own.
    reuse_family: str | None = None

    with tx() as db:
        row = db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        if row is None:
            raise unauthorized("invalid refresh token")

        if row.revoked_at is not None:
            raise unauthorized("refresh token revoked")

        if row.used_at is not None:
            reuse_family = row.family_id
        elif row.expires_at <= deps.now():
            raise unauthorized("refresh token expired")
        else:
            row.used_at = deps.now()

            user = db.scalar(
                select(User)
                .where(User.id == row.user_id, User.deleted_at.is_(None))
                .options(selectinload(User.memberships))
            )
            if user is None or user.status != "active":
                # Rolling back `used_at` here is correct: no new token was
                # issued, so the old one was never actually spent.
                raise unauthorized("account is no longer active")

            return _issue(
                db,
                user,
                requested_org=body.organization_id,
                request=request,
                family_id=row.family_id,
            )

    with tx() as db:
        db.execute(
            RefreshToken.__table__.update()
            .where(
                RefreshToken.family_id == reuse_family,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=deps.now())
        )
    log.warning("refresh token reuse detected, family=%s", reuse_family)
    raise unauthorized("refresh token reuse detected; sign in again")


# ── logout ────────────────────────────────────────────────────────────────


@router.post("/logout", status_code=204)
def logout(body: LogoutIn):
    """Revoke the whole family this token belongs to.

    Note what this cannot do: the access token stays valid until it expires.
    There is no denylist, which is exactly why its TTL is minutes rather than
    days.
    """
    token_hash = tokens.hash_refresh_token(body.refresh_token)
    with tx() as db:
        row = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if row is None:
            return  # already gone; 204 either way, nothing to disclose
        db.execute(
            RefreshToken.__table__.update()
            .where(
                RefreshToken.family_id == row.family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=deps.now())
        )


# ── profile ───────────────────────────────────────────────────────────────


@router.get("/me", response_model=UserOut)
def me(c: Caller = Depends(deps.caller)):
    with tx() as db:
        user = deps.load_user(db, c.user_id)
        return _user_out(user)


@router.delete("/me", status_code=202)
def erase_me(c: Caller = Depends(deps.caller)):
    """The user erases themselves.

    202, not 204: the AI service still has to be told, and that delivery is
    recorded rather than assumed. See api/admin.py::_purge.
    """
    from product.api.admin import purge_user

    deps.require_registered(c)
    return purge_user(c.user_id)


# ── debugging ─────────────────────────────────────────────────────────────


@router.post("/introspect", response_model=IntrospectOut)
def introspect(authorization: str = Header(default="")):
    """Decode a token this service issued, and say why if it will not verify.

    Here for whoever is wiring up the AI service or the frontend: a 401 with no
    detail is a bad afternoon, and this turns it into one request.
    """
    if not authorization.startswith("Bearer "):
        return IntrospectOut(valid=False, error="missing bearer token")
    try:
        claims = tokens.decode_own_token(authorization[7:].strip())
        return IntrospectOut(valid=True, claims=claims)
    except Exception as exc:
        return IntrospectOut(valid=False, error=f"{type(exc).__name__}: {exc}"[:300])

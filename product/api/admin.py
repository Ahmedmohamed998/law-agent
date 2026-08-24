"""
Maintenance endpoints: erasure, and the retry that makes it reliable.

This is the concrete implementation of the contract that replaces the missing
foreign key. Because `ai.sessions` has no FK to `public.users`, deleting a user
here cascades *nothing* on the AI side — those conversations contain
dismissals, unpaid-wage disputes and salary details, and they would simply
remain.

The ordering below is the part that matters:

  1. Delete the user and write an `erasure_requests` row IN THE SAME
     TRANSACTION. After this commits, the instruction is durable — a crash
     costs a retry, never the record itself.
  2. THEN attempt the HTTP call, outside that transaction. A slow or dead AI
     service must not hold a database transaction open, and its failure must
     not roll back a deletion the user is entitled to.

Doing it the other way — call first, delete second — loses the instruction
whenever the process dies between the two, and that is a silent compliance
failure rather than a loud one.
"""

import logging

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select

from product.api import deps
from product.api.schemas import ErasureOut
from product.config import settings
from product.db.base import tx
from product.db.models import ErasureRequest, RefreshToken, User

log = logging.getLogger("product.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(deps.admin_key)])

# How many times a pending request is retried before it needs a human. Erasure
# has a legal clock on it, so failing quietly forever is not an option — this
# is what makes a stuck one visible.
MAX_ATTEMPTS = 10


def _notify_ai(user_id: str) -> tuple[bool, str | None]:
    """Tell the AI service to purge this user. Returns (delivered, error).

    The endpoint is idempotent on that side — purging an already-purged user
    deletes nothing and still returns 200 — so a retry after an ambiguous
    failure is always safe.
    """
    s = settings()
    url = f"{s.ai_service_url.rstrip('/')}/v1/users/{user_id}/data"
    try:
        resp = httpx.delete(
            url,
            timeout=s.ai_erasure_timeout_seconds,
            headers={"X-Admin-Key": s.admin_api_key},
        )
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"[:400]

    if resp.status_code in (200, 202, 204):
        return True, None

    if resp.status_code == 404:
        # The erasure endpoint answers 200 even for an unknown user, so a 404
        # means the ROUTE is missing — a misconfigured ai_service_url, or a
        # version of that service predating the endpoint. Never treat it as
        # "already gone": that would mark an undelivered instruction complete.
        return False, "no erasure endpoint at that URL — check ai_service_url"

    return False, f"HTTP {resp.status_code}: {resp.text[:300]}"


def purge_user(user_id: str) -> ErasureOut:
    """Delete the user here, record the instruction, then try to deliver it."""
    with tx() as db:
        user = db.scalar(select(User).where(User.id == user_id))
        request_row = ErasureRequest(user_id=user_id, status="pending", attempts=0)
        db.add(request_row)

        if user is not None:
            # Hard delete. Memberships and refresh tokens cascade; the
            # erasure_requests row deliberately does not, because it has to
            # outlive the user it describes.
            db.delete(user)
        db.flush()
        request_id = request_row.id

    delivered, error = _notify_ai(user_id)

    with tx() as db:
        row = db.scalar(
            select(ErasureRequest).where(ErasureRequest.id == request_id)
        )
        row.attempts += 1
        if delivered:
            row.status = "delivered"
            row.delivered_at = deps.now()
            row.last_error = None
        else:
            row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
            row.last_error = error
            log.warning("erasure not delivered for %s: %s", user_id, error)

        return ErasureOut(
            user_id=row.user_id,
            status=row.status,
            attempts=row.attempts,
            last_error=row.last_error,
        )


@router.delete("/users/{user_id}", response_model=ErasureOut, status_code=202)
def erase_user(user_id: str):
    """Erase a user and everything the AI service holds for them.

    202 rather than 204: the local delete is done, the remote one is recorded
    and attempted. Poll `GET /admin/erasure` to see whether it landed.
    """
    return purge_user(user_id)


@router.get("/erasure", response_model=list[ErasureOut])
def pending_erasures(limit: int = 100):
    """Everything not yet confirmed delivered. Point a monitor at this."""
    with tx() as db:
        rows = list(
            db.scalars(
                select(ErasureRequest)
                .where(ErasureRequest.status != "delivered")
                .order_by(ErasureRequest.requested_at)
                .limit(min(limit, 500))
            )
        )
        return [
            ErasureOut(
                user_id=r.user_id,
                status=r.status,
                attempts=r.attempts,
                last_error=r.last_error,
            )
            for r in rows
        ]


@router.post("/erasure/retry", response_model=list[ErasureOut])
def retry_erasures(limit: int = 50):
    """Re-attempt delivery for pending requests.

    Idempotent on the AI side by construction — purging a user who is already
    purged deletes nothing and returns success — so retrying is always safe.
    """
    with tx() as db:
        pending = [
            r.user_id
            for r in db.scalars(
                select(ErasureRequest)
                .where(ErasureRequest.status == "pending")
                .order_by(ErasureRequest.requested_at)
                .limit(min(limit, 200))
            )
        ]

    out: list[ErasureOut] = []
    for user_id in pending:
        delivered, error = _notify_ai(user_id)
        with tx() as db:
            row = db.scalar(
                select(ErasureRequest)
                .where(ErasureRequest.user_id == user_id)
                .order_by(ErasureRequest.requested_at.desc())
                .limit(1)
            )
            if row is None:
                continue
            row.attempts += 1
            if delivered:
                row.status = "delivered"
                row.delivered_at = deps.now()
                row.last_error = None
            else:
                row.status = "failed" if row.attempts >= MAX_ATTEMPTS else "pending"
                row.last_error = error
            out.append(
                ErasureOut(
                    user_id=row.user_id,
                    status=row.status,
                    attempts=row.attempts,
                    last_error=row.last_error,
                )
            )
    return out

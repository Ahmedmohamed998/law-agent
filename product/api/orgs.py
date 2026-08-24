"""
Organizations and membership.

This is what fills the `org_id` claim, which the AI service turns into a
row-level-security tenant key. So a bug here is not a product bug — it is a
data-isolation bug on the other side of the boundary, where one firm's
employment disputes become visible to another. Every write path is
authorization-checked explicitly.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession, selectinload

from product.api import deps
from product.api.deps import Caller, forbidden, not_found
from product.api.schemas import AddMember, CreateOrg, MemberOut, OrgOut
from product.db.base import tx
from product.db.models import Membership, Organization, User
from product.api.auth import normalize_email
from fastapi import HTTPException

router = APIRouter(prefix="/orgs", tags=["organizations"])

# Who may change membership. `lawyer` is deliberately absent: practising in an
# organization is not the same authority as deciding who belongs to it.
MANAGING_ROLES = ("owner", "admin")


def _membership(db: SASession, user_id: str, org_id: str) -> Membership:
    row = db.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.organization_id == org_id
        )
    )
    if row is None:
        # Same 404 whether the organization does not exist or the caller is not
        # in it — otherwise this endpoint enumerates other firms' org ids.
        raise not_found("organization_not_found", "no such organization")
    return row


def _require_manager(db: SASession, user_id: str, org_id: str) -> Membership:
    m = _membership(db, user_id, org_id)
    if m.role not in MANAGING_ROLES:
        raise forbidden("requires owner or admin")
    return m


@router.post("", response_model=OrgOut, status_code=201)
def create_org(body: CreateOrg, c: Caller = Depends(deps.caller)):
    """Create an organization. The caller becomes its owner."""
    deps.require_registered(c)

    with tx() as db:
        user = deps.load_user(db, c.user_id)
        org = Organization(name=body.name, slug=body.slug)
        db.add(org)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": "slug_taken", "message": "that slug is in use"},
            ) from exc

        db.add(Membership(user_id=user.id, organization_id=org.id, role="owner"))
        db.flush()

        return OrgOut(
            organization_id=org.id,
            name=org.name,
            slug=org.slug,
            created_at=org.created_at,
            my_role="owner",
        )


@router.get("", response_model=list[OrgOut])
def my_orgs(c: Caller = Depends(deps.caller)):
    with tx() as db:
        rows = list(
            db.scalars(
                select(Membership)
                .where(Membership.user_id == c.user_id)
                .options(selectinload(Membership.organization))
            )
        )
        return [
            OrgOut(
                organization_id=m.organization_id,
                name=m.organization.name,
                slug=m.organization.slug,
                created_at=m.organization.created_at,
                my_role=m.role,
            )
            for m in rows
            if m.organization.deleted_at is None
        ]


@router.get("/{org_id}/members", response_model=list[MemberOut])
def members(org_id: str, c: Caller = Depends(deps.caller)):
    with tx() as db:
        _membership(db, c.user_id, org_id)  # authorises, 404s if not ours
        rows = list(
            db.scalars(
                select(Membership)
                .where(Membership.organization_id == org_id)
                .options(selectinload(Membership.user))
                .order_by(Membership.created_at)
            )
        )
        return [
            MemberOut(
                user_id=m.user_id,
                email=m.user.email,
                display_name=m.user.display_name,
                role=m.role,
                joined_at=m.created_at,
            )
            for m in rows
        ]


@router.post("/{org_id}/members", response_model=MemberOut, status_code=201)
def add_member(org_id: str, body: AddMember, c: Caller = Depends(deps.caller)):
    """Add an existing user to the organization by email.

    Invitation email flows are not built; this assumes the person already has
    an account. Creating a user here would mean minting an account someone
    never asked for.
    """
    deps.require_registered(c)
    email = normalize_email(body.email)

    with tx() as db:
        _require_manager(db, c.user_id, org_id)

        user = db.scalar(
            select(User).where(
                User.email == email,
                User.deleted_at.is_(None),
                User.is_anonymous.is_(False),
            )
        )
        if user is None:
            raise not_found("user_not_found", "no registered user with that email")

        existing = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == org_id,
            )
        )
        if existing is not None:
            existing.role = body.role
            db.flush()
            row = existing
        else:
            row = Membership(
                user_id=user.id, organization_id=org_id, role=body.role
            )
            db.add(row)
            db.flush()

        return MemberOut(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=row.role,
            joined_at=row.created_at,
        )


@router.delete("/{org_id}/members/{user_id}", status_code=204)
def remove_member(org_id: str, user_id: str, c: Caller = Depends(deps.caller)):
    deps.require_registered(c)

    with tx() as db:
        _require_manager(db, c.user_id, org_id)
        target = _membership(db, user_id, org_id)

        if target.role == "owner":
            remaining = db.scalar(
                select(Membership)
                .where(
                    Membership.organization_id == org_id,
                    Membership.role == "owner",
                    Membership.user_id != user_id,
                )
                .limit(1)
            )
            if remaining is None:
                # An organization with no owner cannot be administered again
                # without a database edit.
                raise forbidden("cannot remove the last owner")

        db.delete(target)

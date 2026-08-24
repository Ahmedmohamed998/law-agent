"""
The product domain: users, organizations, memberships, refresh tokens.

Everything lives in `public`. Two rules mirror the AI service's and exist for
the same reason — so the two halves can be split onto separate clusters later
without it becoming a project:

  1. No foreign key crosses into `ai.*`. The AI service holds `user_id` as an
     opaque string and nothing here points at it.
  2. Deleting a user here cascades NOTHING there. `erasure_requests` is the
     contract that replaces the missing foreign key: a durable record that the
     other side still has to be told.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from product.db.ids import new_id

SCHEMA = "public"

USER_STATUSES = ("active", "disabled")
# `client` is the default for anyone who signs up. `lawyer` and the two admin
# roles are granted, never self-assigned — the signup endpoint cannot produce
# them, which is why the check constraint is not the only guard.
MEMBER_ROLES = ("owner", "admin", "lawyer", "client")
ERASURE_STATUSES = ("pending", "delivered", "failed")


def _in(column: str, allowed: tuple[str, ...]) -> str:
    values = ", ".join(f"'{v}'" for v in allowed)
    return f"{column} IN ({values})"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Case-insensitive uniqueness without the citext extension: email is
        # lowercased on the way in (see api/auth.py::normalize_email) and the
        # constraint enforces what that produces. Partial, so a deleted user's
        # address is released for re-registration.
        Index(
            "uq_users_email",
            "email",
            unique=True,
            postgresql_where="deleted_at IS NULL AND email IS NOT NULL",
        ),
        CheckConstraint(_in("status", USER_STATUSES), name="ck_users_status"),
        # An anonymous user has no email and no password; a real one must have
        # both. Enforced here because the alternative is trusting every future
        # code path that creates a user.
        CheckConstraint(
            "(is_anonymous AND email IS NULL AND password_hash IS NULL) "
            "OR (NOT is_anonymous AND email IS NOT NULL AND password_hash IS NOT NULL)",
            name="ck_users_anonymous_shape",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # The funnel: someone asks a labour-law question before they have an
    # account. They get a real signed token and a real user row, so their
    # conversations are stored under a stable `sub` from the first message.
    # Signup UPGRADES this row in place and keeps the id — which is what makes
    # their pre-signup history survive with no session-reassignment call to the
    # AI service at all.
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class Membership(Base):
    """Which organization a user belongs to, and as what.

    This is what fills the `org_id` and `role` claims. A user with no
    membership gets neither, which the AI service reads as an
    organization-less individual: its row-level security then buckets them
    under the empty tenant key and `user_id` filtering does the real work.
    """

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(_in("role", MEMBER_ROLES), name="ck_memberships_role"),
        Index("ix_memberships_org", "organization_id", "role"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(f"{SCHEMA}.organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="client")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship(back_populates="memberships")


class RefreshToken(Base):
    """A long-lived credential, stored only as a hash.

    Rotating with reuse detection: every refresh mints a new token and marks
    the old one used. Presenting an already-used token means it leaked, and the
    whole family is revoked rather than just that one — at that point the
    attacker and the real user are indistinguishable, so the safe move is to
    end the session for both and make the user log in again.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
        Index("ix_refresh_user", "user_id", "expires_at"),
        Index("ix_refresh_family", "family_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # All tokens descended from one login. Revoking the family ends that login
    # everywhere without touching the user's other devices.
    family_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # SHA-256 of the opaque token. The plaintext exists only in the response
    # body that created it; a database dump yields nothing usable.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip: Mapped[str | None] = mapped_column(String(64))


class ErasureRequest(Base):
    """A durable record that the AI service still has to be told about a delete.

    Without a foreign key there is no cascade, and a best-effort HTTP call that
    fails while the user row is already gone loses the instruction entirely.
    This row is written in the same transaction as the delete, so the worst
    case is a retry — never a silent orphan.
    """

    __tablename__ = "erasure_requests"
    __table_args__ = (
        CheckConstraint(_in("status", ERASURE_STATUSES), name="ck_erasure_status"),
        Index("ix_erasure_pending", "status", "requested_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Deliberately NOT a foreign key: the user row is deleted, and the whole
    # point of this table is to outlive it.
    user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

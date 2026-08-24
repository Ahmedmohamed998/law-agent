"""The wire contract for the product backend.

Kept flat and boring. The frontend consumes this for login and profile; the AI
service consumes none of it — the only thing that crosses to the other side is
the signed token itself.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

Role = Literal["owner", "admin", "lawyer", "client"]


# ── auth ──────────────────────────────────────────────────────────────────


class SignupIn(BaseModel):
    email: EmailStr
    # The floor is length, not composition. Character-class rules push people
    # toward "Password1!" and measurably do not help; length does.
    password: str = Field(min_length=10, max_length=200)
    display_name: str | None = Field(default=None, max_length=160)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    # Which organization's context to mint the token for. Only meaningful for
    # a user who belongs to more than one.
    organization_id: str | None = Field(default=None, max_length=32)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)
    organization_id: str | None = Field(default=None, max_length=32)


class LogoutIn(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class MembershipOut(BaseModel):
    organization_id: str
    organization_name: str
    role: Role


class UserOut(BaseModel):
    user_id: str
    email: str | None
    display_name: str | None
    is_anonymous: bool
    email_verified: bool
    created_at: datetime
    memberships: list[MembershipOut] = []


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    # Seconds. The client refreshes before this elapses; it should never wait
    # for a 401 to discover the token died mid-conversation.
    expires_in: int
    refresh_token: str
    user: UserOut


class IntrospectOut(BaseModel):
    """Debugging aid for whoever is wiring up the other side."""

    valid: bool
    claims: dict | None = None
    error: str | None = None


# ── organizations ─────────────────────────────────────────────────────────


class CreateOrg(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class OrgOut(BaseModel):
    organization_id: str
    name: str
    slug: str
    created_at: datetime
    my_role: Role | None = None


class AddMember(BaseModel):
    email: EmailStr
    role: Role = "client"


class MemberOut(BaseModel):
    user_id: str
    email: str | None
    display_name: str | None
    role: Role
    joined_at: datetime


# ── erasure ───────────────────────────────────────────────────────────────


class ErasureOut(BaseModel):
    user_id: str
    status: Literal["pending", "delivered", "failed"]
    attempts: int
    last_error: str | None = None


# ── errors ────────────────────────────────────────────────────────────────


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody

"""Identity domain: users, organizations, memberships, refresh tokens, erasure

Revision ID: 0001_identity
Revises:
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_identity"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "public"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("email", sa.String(320)),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("password_hash", sa.Text),
        sa.Column("display_name", sa.String(160)),
        sa.Column("phone", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "is_anonymous",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        # An anonymous user has no credentials; a registered one must have
        # both. The database refuses the half-built states rather than trusting
        # every future code path that writes a user.
        sa.CheckConstraint(
            "(is_anonymous AND email IS NULL AND password_hash IS NULL) "
            "OR (NOT is_anonymous AND email IS NOT NULL AND password_hash IS NOT NULL)",
            name="ck_users_anonymous_shape",
        ),
        schema=SCHEMA,
    )
    # Partial unique: case-insensitivity comes from lowercasing on write, and
    # excluding soft-deleted rows releases the address for re-registration.
    op.create_index(
        "uq_users_email",
        "users",
        ["email"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("deleted_at IS NULL AND email IS NOT NULL"),
    )

    op.create_table(
        "organizations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        schema=SCHEMA,
    )

    op.create_table(
        "memberships",
        sa.Column("user_id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(32), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="client"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], [f"{SCHEMA}.organizations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'lawyer', 'client')",
            name="ck_memberships_role",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_memberships_org",
        "memberships",
        ["organization_id", "role"],
        schema=SCHEMA,
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("ip", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_refresh_user", "refresh_tokens", ["user_id", "expires_at"], schema=SCHEMA
    )
    op.create_index(
        "ix_refresh_family", "refresh_tokens", ["family_id"], schema=SCHEMA
    )

    op.create_table(
        "erasure_requests",
        sa.Column("id", sa.String(32), primary_key=True),
        # No foreign key on purpose: this row exists to outlive the user it
        # names, and a cascade would delete the instruction along with them.
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')", name="ck_erasure_status"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_erasure_pending",
        "erasure_requests",
        ["status", "requested_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("erasure_requests", schema=SCHEMA)
    op.drop_table("refresh_tokens", schema=SCHEMA)
    op.drop_table("memberships", schema=SCHEMA)
    op.drop_table("organizations", schema=SCHEMA)
    op.drop_table("users", schema=SCHEMA)

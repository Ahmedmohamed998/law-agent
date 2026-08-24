"""AI domain: sessions, messages, retrieval provenance, feedback

Revision ID: 0001_ai_domain
Revises:
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_ai_domain"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"

# Tenant isolation, enforced by the database rather than by remembering to
# write a WHERE clause. `ai.org_id` is set per transaction in
# app/db/session.py::scoped_session. When it is unset the comparison is NULL,
# so the policy denies everything — a connection that forgot to bind a tenant
# reads nothing rather than reading everyone.
_POLICY = (
    "COALESCE(organization_id, '') = current_setting('ai.org_id', true)"
)

_TENANT_TABLES = ("sessions", "messages", "message_sources", "feedback")


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("organization_id", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("lang", sa.String(8)),
        sa.Column("title", sa.Text),
        sa.Column("summary", sa.Text),
        sa.Column("summary_upto_seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('active', 'escalated', 'closed')", name="ck_sessions_status"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sessions_user",
        "sessions",
        ["organization_id", "user_id", "last_active_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sessions_active",
        "sessions",
        ["organization_id", "last_active_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("client_message_id", sa.String(64)),
        sa.Column("organization_id", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("source_label", sa.String(24)),
        sa.Column("planned_queries", JSONB),
        sa.Column("index_version", sa.String(64)),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_tokens", sa.Integer),
        sa.Column("completion_tokens", sa.Integer),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("error_code", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["session_id"], [f"{SCHEMA}.sessions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("session_id", "seq", name="uq_messages_seq"),
        sa.UniqueConstraint(
            "session_id", "client_message_id", name="uq_messages_client_id"
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.CheckConstraint(
            "status IN ('streaming', 'complete', 'error')", name="ck_messages_status"
        ),
        sa.CheckConstraint(
            "source_label IS NULL OR source_label IN "
            "('documents', 'model_knowledge', 'mixed', 'refused')",
            name="ck_messages_source_label",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_messages_session", "messages", ["session_id", "seq"], schema=SCHEMA
    )

    op.create_table(
        "message_sources",
        sa.Column("message_id", sa.String(32), primary_key=True),
        sa.Column("rank", sa.Integer, primary_key=True),
        sa.Column("chunk_id", sa.String(128), nullable=False),
        sa.Column("citation", sa.Text, nullable=False),
        sa.Column("doc_title", sa.Text),
        sa.Column("article_label", sa.Text),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("organization_id", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["message_id"], [f"{SCHEMA}.messages.id"], ondelete="CASCADE"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_sources_chunk", "message_sources", ["chunk_id"], schema=SCHEMA)

    op.create_table(
        "feedback",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("message_id", sa.String(32), nullable=False),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("organization_id", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["message_id"], [f"{SCHEMA}.messages.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("message_id", name="uq_feedback_message"),
        sa.CheckConstraint("rating IN ('up', 'down')", name="ck_feedback_rating"),
        schema=SCHEMA,
    )

    # Row-level security. Not forced, deliberately: the owner role runs
    # migrations and cross-tenant maintenance (erasure) and needs to bypass it,
    # while the application connects as `ai_service`, owns nothing, and is
    # therefore always subject to the policy.
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {SCHEMA}.{table} "
            f"USING ({_POLICY}) WITH CHECK ({_POLICY})"
        )


def downgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {SCHEMA}.{table}")
    op.drop_table("feedback", schema=SCHEMA)
    op.drop_table("message_sources", schema=SCHEMA)
    op.drop_table("messages", schema=SCHEMA)
    op.drop_table("sessions", schema=SCHEMA)

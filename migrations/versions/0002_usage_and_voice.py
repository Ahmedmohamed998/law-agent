"""Per-user message allowance, and whether a message was spoken

Revision ID: 0002_usage_and_voice
Revises: e1627fc6e802
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_usage_and_voice"
down_revision: str | None = "e1627fc6e802"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"


def upgrade() -> None:
    # ── message allowance ────────────────────────────────────────────────
    #
    # A counter, not a COUNT over ai.messages, for two reasons:
    #
    #   * Deleting a conversation is a soft delete. A limit computed from
    #     visible messages is a limit anyone can reset by deleting a chat.
    #   * The check has to be atomic. Two tabs sending at once would both read
    #     "4 of 5 used" and both proceed; a single conditional UPDATE cannot.
    #
    # Keyed on the user id, which does not change when an anonymous visitor
    # signs up (the row is upgraded in place), so messages sent before
    # sign-in count towards the total afterwards.
    #
    # `org_key` rather than a nullable organization_id: it is part of the
    # primary key, and it is exactly the value row-level security compares.
    op.create_table(
        "message_usage",
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("org_key", sa.String(128), nullable=False, server_default=""),
        sa.Column("messages_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "org_key", name="pk_message_usage"),
        sa.CheckConstraint("messages_used >= 0", name="ck_message_usage_nonneg"),
        schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.message_usage ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {SCHEMA}.message_usage "
        "USING (org_key = current_setting('ai.org_id', true)) "
        "WITH CHECK (org_key = current_setting('ai.org_id', true))"
    )
    # Default privileges already cover this when migrations run as the owner,
    # which they should. Stated anyway, so a migration run as the wrong role
    # fails loudly here rather than as a 42501 on the first message.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {SCHEMA}.message_usage TO ai_service"
    )

    # ── spoken messages ──────────────────────────────────────────────────
    #
    # Whether the user typed or spoke. Informational only — it changes nothing
    # about how the turn is answered — so it is safe to take from the client.
    op.add_column(
        "messages",
        sa.Column("input_mode", sa.String(8), nullable=False, server_default="text"),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_messages_input_mode",
        "messages",
        "input_mode IN ('text', 'voice')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint("ck_messages_input_mode", "messages", schema=SCHEMA, type_="check")
    op.drop_column("messages", "input_mode", schema=SCHEMA)
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {SCHEMA}.message_usage")
    op.drop_table("message_usage", schema=SCHEMA)

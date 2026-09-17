"""What the assistant suggested buying, on the answer it suggested it under

Revision ID: 0003_suggestions
Revises: 0002_usage_and_voice
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_suggestions"
down_revision: str | None = "0002_usage_and_voice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ai"


def upgrade() -> None:
    # Three columns on the assistant message rather than a table of their
    # own: a suggestion is a property of one answer, is read back whenever
    # that answer is (reopening a conversation, the dashboard transcript),
    # and there is at most one per answer. The slug, not an id: the
    # catalogue lives in another service's schema, so there is nothing here
    # to reference, and a slug still reads correctly after the service is
    # renamed or retired.
    op.add_column(
        "messages",
        sa.Column("suggested_service", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "messages",
        sa.Column("suggestion_reason", sa.Text, nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "messages",
        sa.Column("suggestion_confidence", sa.Float, nullable=True),
        schema=SCHEMA,
    )
    # The client pressed the card. With the order's link to the session this
    # gives suggested -> clicked -> bought per service, which is how a bad
    # hint gets told apart from a bad price.
    op.add_column(
        "messages",
        sa.Column("suggestion_clicked_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for col in (
        "suggestion_clicked_at",
        "suggestion_confidence",
        "suggestion_reason",
        "suggested_service",
    ):
        op.drop_column("messages", col, schema=SCHEMA)

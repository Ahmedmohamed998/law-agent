"""Add escalate to source_labels

Revision ID: e1627fc6e802
Revises: 0001_ai_domain
Create Date: 2026-08-24 13:52:55.300546
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e1627fc6e802'
down_revision: str | None = '0001_ai_domain'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop old constraint
    op.drop_constraint('ck_messages_source_label', 'messages', schema='ai', type_='check')
    
    # Add new constraint
    op.create_check_constraint(
        'ck_messages_source_label', 
        'messages', 
        "source_label IS NULL OR source_label IN ('documents', 'model_knowledge', 'mixed', 'refused', 'escalate')",
        schema='ai'
    )


def downgrade() -> None:
    # Drop new constraint
    op.drop_constraint('ck_messages_source_label', 'messages', schema='ai', type_='check')
    
    # Add old constraint
    op.create_check_constraint(
        'ck_messages_source_label', 
        'messages', 
        "source_label IS NULL OR source_label IN ('documents', 'model_knowledge', 'mixed', 'refused')",
        schema='ai'
    )

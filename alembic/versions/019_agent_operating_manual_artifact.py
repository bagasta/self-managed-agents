"""add artifact jsonb to agent_operating_manuals + backfill"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "019_agent_operating_manual_artifact"
down_revision: Union[str, Sequence[str], None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_operating_manuals",
        sa.Column(
            "artifact",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.execute("""
        UPDATE agent_operating_manuals m
        SET artifact = COALESCE(a.tools_config->'operating_manual',
                                jsonb_build_object(
                                  'source', m.source,
                                  'domain', m.domain,
                                  'domain_confidence', m.domain_confidence,
                                  'maturity', m.maturity,
                                  'owner_review_required', m.owner_review_required,
                                  'missing_context', to_jsonb(m.missing_context),
                                  'assumptions', to_jsonb(m.assumptions),
                                  'workflows', to_jsonb(m.workflows)))
        FROM agents a
        WHERE m.agent_id = a.id
    """)


def downgrade() -> None:
    op.drop_column("agent_operating_manuals", "artifact")

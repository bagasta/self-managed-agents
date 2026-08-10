"""users.wa_lid — WhatsApp LID alias terpisah dari phone_number

Revision ID: 022
Revises: 021
Create Date: 2026-07-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("wa_lid", sa.String(64), nullable=True))
    op.create_index("ix_users_wa_lid", "users", ["wa_lid"])


def downgrade() -> None:
    op.drop_index("ix_users_wa_lid", table_name="users")
    op.drop_column("users", "wa_lid")

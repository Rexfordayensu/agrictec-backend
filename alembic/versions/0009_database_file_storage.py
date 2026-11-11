"""Store private files in the existing PostgreSQL database.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stored_files")

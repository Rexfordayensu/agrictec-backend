"""weather and market price alerts already sent to farmers

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('farm_alerts',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ux_farm_alerts_user_key', 'farm_alerts', ['user_id', 'key'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_farm_alerts_user_key', table_name='farm_alerts')
    op.drop_table('farm_alerts')

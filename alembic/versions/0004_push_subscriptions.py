"""push subscriptions for error alerts

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('push_subscriptions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('admin_id', sa.String(length=64), nullable=False),
    sa.Column('endpoint', sa.Text(), nullable=False),
    sa.Column('p256dh', sa.String(length=200), nullable=False),
    sa.Column('auth', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['admin_id'], ['admins.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('endpoint')
    )
    op.create_index(op.f('ix_push_subscriptions_admin_id'), 'push_subscriptions', ['admin_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_push_subscriptions_admin_id'), table_name='push_subscriptions')
    op.drop_table('push_subscriptions')

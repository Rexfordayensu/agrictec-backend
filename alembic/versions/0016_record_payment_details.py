"""farm records: how the money was paid

Additive. A money record (income, expense, financing) now says how it was paid:
payment_method is 'cash', 'bank' or 'mobile_money' for new records (older records keep whatever
free text they had). A bank payment adds the bank's name (bank_name) and its reference
(external_reference); a mobile money payment adds the network and must come with its receipt.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('farm_records', sa.Column('network', sa.String(length=16), nullable=True))
    op.add_column('farm_records', sa.Column('bank_name', sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column('farm_records', 'bank_name')
    op.drop_column('farm_records', 'network')

"""loan eligibility assessments: preliminary, explained checks against a loan product

Additive. One row per check a farmer runs: the product version, the amount and term asked for,
the outcome and the full explained result as shown, so a later application can carry the
assessment it was based on. Never a loan decision.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'loan_eligibility_assessments',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('product_id', sa.String(length=120), nullable=False),
        sa.Column('product_version', sa.Integer(), nullable=False),
        sa.Column('lender_id', sa.String(length=64), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('outcome', sa.String(length=32), nullable=False),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('meets_preliminary', 'additional_information', 'does_not_meet', 'unable_to_assess')",
            name='ck_loan_assessment_outcome',
        ),
        sa.CheckConstraint('amount > 0 AND months > 0', name='ck_loan_assessment_request'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['loan_products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_loan_assessments_user', 'loan_eligibility_assessments', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('loan_eligibility_assessments')

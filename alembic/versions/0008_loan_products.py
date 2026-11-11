"""loan products: versioned terms and requirements per lender

A product's draft is edited by administrators; each publish freezes it into an immutable version
with its requirements (a trigger refuses updates), so an application can always show the exact
terms it was prepared against. Additive: no existing table changes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('loan_products',
    sa.Column('id', sa.String(length=120), nullable=False),
    sa.Column('lender_id', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('draft', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('published_version', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=80), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_by', sa.String(length=80), nullable=True),
    sa.ForeignKeyConstraint(['lender_id'], ['lenders.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint("status IN ('draft', 'published', 'unpublished')", name='ck_loan_products_status'),
    sa.CheckConstraint(
        "status = 'draft' OR published_version IS NOT NULL", name='ck_loan_products_published_version'
    ),
    )
    op.create_index('ix_loan_products_lender_id', 'loan_products', ['lender_id'])

    op.create_table('loan_product_versions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('product_id', sa.String(length=120), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('min_amount', sa.Numeric(14, 2), nullable=True),
    sa.Column('max_amount', sa.Numeric(14, 2), nullable=True),
    sa.Column('interest_rate', sa.Numeric(7, 4), nullable=True),
    sa.Column('min_months', sa.Integer(), nullable=True),
    sa.Column('max_months', sa.Integer(), nullable=True),
    sa.Column('terms', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('source_checked_on', sa.Date(), nullable=False),
    sa.Column('note', sa.String(length=300), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('published_by', sa.String(length=80), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['loan_products.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint('min_amount IS NULL OR max_amount IS NULL OR min_amount <= max_amount',
                       name='ck_loan_product_versions_amounts'),
    sa.CheckConstraint('min_months IS NULL OR max_months IS NULL OR min_months <= max_months',
                       name='ck_loan_product_versions_months'),
    )
    op.create_index('ux_loan_product_versions', 'loan_product_versions', ['product_id', 'version'], unique=True)

    op.create_table('loan_requirements',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('version_id', sa.String(length=64), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('required', sa.Boolean(), nullable=False),
    sa.Column('field_type', sa.String(length=12), nullable=False),
    sa.Column('verification', sa.String(length=20), nullable=False),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['version_id'], ['loan_product_versions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ux_loan_requirements_key', 'loan_requirements', ['version_id', 'key'], unique=True)

    # Published versions and their requirements are read-only (deleting a whole product, with
    # its versions, is still possible while nothing refers to it).
    op.execute(
        """
        CREATE FUNCTION loan_versions_read_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is read-only once published', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in ('loan_product_versions', 'loan_requirements'):
        op.execute(
            f"CREATE TRIGGER {table}_read_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION loan_versions_read_only()"
        )


def downgrade() -> None:
    for table in ('loan_requirements', 'loan_product_versions'):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_read_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS loan_versions_read_only()")
    op.drop_index('ux_loan_requirements_key', table_name='loan_requirements')
    op.drop_table('loan_requirements')
    op.drop_index('ux_loan_product_versions', table_name='loan_product_versions')
    op.drop_table('loan_product_versions')
    op.drop_index('ix_loan_products_lender_id', table_name='loan_products')
    op.drop_table('loan_products')

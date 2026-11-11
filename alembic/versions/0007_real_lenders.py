"""real lenders: institution, licence verification, listing authorisation and publication

The lender table becomes a register of real institutions that administrators add and check.
Loan terms (rate, amounts, periods, minimum score) leave the institution: they belong to loan
products, which come next. Loan applications keep their own copy of the rate and payment.

Data:
- The four sample lenders from development (fixed ids and names, made-up terms) are removed. One
  that a loan application refers to is kept for that history, suspended and hidden.
- Any other lender becomes unpublished and unverified until an administrator checks it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAMPLE_LENDERS = {
    'sinapi': 'Sinapi Aba Trust',
    'opportunity': 'Opportunity Fund',
    'agridev': 'AgriDev Microfinance',
    'rural': 'Rural Bank Consortium',
}
SAMPLE_REASON = 'Sample lender from development: its terms were never real. Kept only for old applications.'


def upgrade() -> None:
    add = lambda *cols: [op.add_column('lenders', c) for c in cols]  # noqa: E731
    add(
        sa.Column('institution_type', sa.String(length=32), nullable=False, server_default='other'),
        sa.Column('registered_name', sa.String(length=200)),
        sa.Column('logo_key', sa.String(length=200)),
        sa.Column('logo_updated_at', sa.DateTime(timezone=True)),
        sa.Column('website', sa.String(length=300)),
        sa.Column('email', sa.String(length=160)),
        sa.Column('phone', sa.String(length=40)),
        sa.Column('address', sa.Text()),
        sa.Column('region', sa.String(length=40)),
        sa.Column('offers_agri_loans', sa.Boolean()),
        sa.Column('supported_regions', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('licence_ref', sa.String(length=120)),
        sa.Column('regulator', sa.String(length=120)),
        sa.Column('licence_status', sa.String(length=16), nullable=False, server_default='unverified'),
        sa.Column('verification_source', sa.Text()),
        sa.Column('verification_note', sa.String(length=500)),
        sa.Column('verified_at', sa.DateTime(timezone=True)),
        sa.Column('verified_by', sa.String(length=80)),
        sa.Column('listing_authorised', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('listing_note', sa.String(length=500)),
        sa.Column('partnership_status', sa.String(length=16), nullable=False, server_default='none'),
        sa.Column('publication_status', sa.String(length=12), nullable=False, server_default='draft'),
        sa.Column('suspension_reason', sa.String(length=300)),
        sa.Column('published_at', sa.DateTime(timezone=True)),
        sa.Column('created_by', sa.String(length=80)),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
        sa.Column('updated_by', sa.String(length=80)),
    )
    op.alter_column('lenders', 'name', type_=sa.String(length=160))
    op.alter_column('lenders', 'description', type_=sa.Text())

    conn = op.get_bind()
    for lender_id, name in SAMPLE_LENDERS.items():
        params = {'id': lender_id, 'name': name}
        used = conn.execute(
            sa.text('SELECT count(*) FROM loan_applications WHERE partner_id = :id'), params
        ).scalar()
        if used:
            conn.execute(
                sa.text(
                    "UPDATE lenders SET publication_status = 'suspended', suspension_reason = :reason, "
                    "updated_at = now(), updated_by = 'Migration 0007' WHERE id = :id AND name = :name"
                ),
                {**params, 'reason': SAMPLE_REASON},
            )
        else:
            conn.execute(sa.text('DELETE FROM lenders WHERE id = :id AND name = :name'), params)
    # Every other lender waits for an administrator to check it.
    conn.execute(
        sa.text(
            "UPDATE lenders SET publication_status = 'unpublished', updated_at = now(), "
            "updated_by = 'Migration 0007' WHERE publication_status = 'draft'"
        )
    )

    for column in ('rate', 'max_amount', 'min_months', 'max_months', 'min_score', 'active'):
        op.drop_column('lenders', column)
    op.create_index('ix_lenders_publication_status', 'lenders', ['publication_status'])
    op.create_check_constraint(
        'ck_lenders_publication_status', 'lenders',
        "publication_status IN ('draft', 'published', 'unpublished', 'suspended')",
    )
    op.create_check_constraint(
        'ck_lenders_licence_status', 'lenders',
        "licence_status IN ('unverified', 'verified', 'expired', 'revoked')",
    )
    op.create_check_constraint(
        'ck_lenders_partnership_status', 'lenders',
        "partnership_status IN ('none', 'in_discussion', 'partner')",
    )
    # A published lender always has a recorded verification and listing authorisation.
    op.create_check_constraint(
        'ck_lenders_published_checked', 'lenders',
        "publication_status <> 'published' OR (licence_status = 'verified' AND verified_at IS NOT NULL "
        "AND verification_source IS NOT NULL AND listing_authorised)",
    )


def downgrade() -> None:
    """Puts the old columns back (lenders come back switched off, with placeholder terms that
    must be edited before use). Removed sample lenders are not restored."""
    for name in (
        'ck_lenders_published_checked', 'ck_lenders_partnership_status', 'ck_lenders_licence_status',
        'ck_lenders_publication_status',
    ):
        op.drop_constraint(name, 'lenders', type_='check')
    op.drop_index('ix_lenders_publication_status', table_name='lenders')
    op.add_column('lenders', sa.Column('rate', sa.Float(), nullable=False, server_default='0'))
    op.add_column('lenders', sa.Column('max_amount', sa.Numeric(14, 2), nullable=False, server_default='0'))
    op.add_column('lenders', sa.Column('min_months', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('lenders', sa.Column('max_months', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('lenders', sa.Column('min_score', sa.Integer(), nullable=False, server_default='850'))
    op.add_column('lenders', sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.false()))
    for column in (
        'updated_by', 'updated_at', 'created_by', 'published_at', 'suspension_reason', 'publication_status',
        'partnership_status', 'listing_note', 'listing_authorised', 'verified_by', 'verified_at',
        'verification_note', 'verification_source', 'licence_status', 'regulator', 'licence_ref',
        'supported_regions', 'offers_agri_loans', 'region', 'address', 'phone', 'email', 'website',
        'logo_updated_at', 'logo_key', 'registered_name', 'institution_type',
    ):
        op.drop_column('lenders', column)
    op.alter_column('lenders', 'description', type_=sa.String(length=200))
    op.alter_column('lenders', 'name', type_=sa.String(length=80))

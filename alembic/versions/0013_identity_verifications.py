"""identity verifications: Ghana Card verification attempts with an identity provider

Additive. One row per attempt with NIA's approved verification platform (or, in development
only, a simulated stand-in): the status, the consent accepted, and the provider's reference and
outcome. No card number, photo or biometric data is ever stored. At most one open session per
farmer. Existing admin reviews of typed-in card numbers (profiles.ghana_card_status) are a
separate, lower level and are not touched.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'identity_verifications',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('verification_reference', sa.String(length=160), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('verification_method', sa.String(length=60), nullable=True),
        sa.Column('consent_version', sa.String(length=40), nullable=False),
        sa.Column('consent_given_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('initiated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_code', sa.String(length=40), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('last_event_id', sa.String(length=160), nullable=True),
        sa.Column('reviewed_by', sa.String(length=80), nullable=True),
        sa.Column('review_note', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'verified', 'failed', 'expired', 'requires_review')",
            name='ck_identity_verification_status',
        ),
        sa.CheckConstraint('attempt_count >= 1', name='ck_identity_verification_attempts'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_identity_verifications_user', 'identity_verifications', ['user_id', 'created_at'])
    op.create_index('ix_identity_verifications_status', 'identity_verifications', ['status'])
    op.create_index(
        'uq_identity_verification_active', 'identity_verifications', ['user_id'], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    op.create_index(
        'uq_identity_verification_reference', 'identity_verifications', ['provider', 'verification_reference'],
        unique=True, postgresql_where=sa.text('verification_reference IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_table('identity_verifications')

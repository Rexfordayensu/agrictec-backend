"""farmer loan applications: prepared by the farmer, reviewed by AgriCtec, sent to the lender

Additive. The earlier loan_applications table is left as it is, read-only history.

- farmer_loan_applications: one application to one published loan product version, with the
  farmer's answers, letter, the eligibility assessment and statement attached at submission,
  and where it stands (draft -> submitted -> in AgriCtec review -> sent to the lender -> the
  lender's progress, recorded only with the lender's own confirmation).
- loan_application_events: its timeline. A lender status carries the lender's proof (a file).
- loan_application_documents: files the farmer uploads for the lender's requirements.
- loan_data_sharing_consents: what the farmer agreed to share, with which lender, why, when.
- lenders: the submission contact the lender agreed to (and when), for sending applications.

Stored files are removed with the row that owns them (the existing cleanup_evidence_file()).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "'draft', 'submitted', 'in_review', 'changes_requested', 'approved_for_sending', 'sent_to_lender', "
    "'received_by_lender', 'under_lender_review', 'documents_requested', 'approved_by_lender', "
    "'rejected_by_lender', 'withdrawn'"
)


def upgrade() -> None:
    op.add_column('lenders', sa.Column('submission_email', sa.String(length=160), nullable=True))
    op.add_column('lenders', sa.Column('submission_agreed_on', sa.Date(), nullable=True))
    op.add_column('lenders', sa.Column('submission_note', sa.String(length=500), nullable=True))

    op.create_table(
        'farmer_loan_applications',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('reference', sa.String(length=24), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('lender_id', sa.String(length=64), nullable=False),
        sa.Column('product_id', sa.String(length=120), nullable=True),
        sa.Column('product_version', sa.Integer(), nullable=False),
        sa.Column('product_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=300), nullable=False),
        sa.Column('letter', sa.Text(), nullable=True),
        sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('assessment_id', sa.String(length=64), nullable=True),
        sa.Column('assessment_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('statement_report_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('review_note', sa.Text(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_via', sa.String(length=16), nullable=True),
        sa.Column('sent_to', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name='ck_farmer_application_status'),
        sa.CheckConstraint('amount > 0 AND months > 0', name='ck_farmer_application_request'),
        sa.CheckConstraint("sent_via IS NULL OR sent_via IN ('email', 'recorded')", name='ck_farmer_application_sent_via'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['lender_id'], ['lenders.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['product_id'], ['loan_products.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['assessment_id'], ['loan_eligibility_assessments.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['statement_report_id'], ['financial_report_exports.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('reference', name='uq_farmer_application_reference'),
    )
    op.create_index('ix_farmer_applications_user', 'farmer_loan_applications', ['user_id', 'created_at'])
    op.create_index('ix_farmer_applications_status', 'farmer_loan_applications', ['status', 'updated_at'])

    op.create_table(
        'loan_application_events',
        sa.Column('id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('application_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('actor_type', sa.String(length=16), nullable=False),
        sa.Column('actor_name', sa.String(length=120), nullable=False),
        sa.Column('note', sa.String(length=1000), nullable=True),
        sa.Column('file_key', sa.String(length=512), nullable=True),
        sa.Column('file_name', sa.String(length=255), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("actor_type IN ('farmer', 'admin', 'system')", name='ck_application_event_actor'),
        sa.ForeignKeyConstraint(['application_id'], ['farmer_loan_applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_key'], ['stored_files.key'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_application_events_app', 'loan_application_events', ['application_id', 'at'])

    op.create_table(
        'loan_application_documents',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('application_id', sa.String(length=64), nullable=False),
        sa.Column('requirement_key', sa.String(length=40), nullable=False),
        sa.Column('file_key', sa.String(length=512), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=80), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['farmer_loan_applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_key'], ['stored_files.key']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_application_documents_app', 'loan_application_documents', ['application_id'])

    op.create_table(
        'loan_data_sharing_consents',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('application_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('lender_id', sa.String(length=64), nullable=False),
        sa.Column('purpose', sa.String(length=300), nullable=False),
        sa.Column('data_items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('notice_version', sa.String(length=40), nullable=False),
        sa.Column('given_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('withdrawn_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['farmer_loan_applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sharing_consents_app', 'loan_data_sharing_consents', ['application_id'])

    # A document's or a lender proof's stored file goes with its row.
    op.execute(
        "CREATE TRIGGER application_document_file_cleanup AFTER DELETE ON loan_application_documents "
        "FOR EACH ROW EXECUTE FUNCTION cleanup_evidence_file()"
    )
    op.execute(
        "CREATE TRIGGER application_event_file_cleanup AFTER DELETE ON loan_application_events "
        "FOR EACH ROW WHEN (OLD.file_key IS NOT NULL) EXECUTE FUNCTION cleanup_evidence_file()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER application_event_file_cleanup ON loan_application_events")
    op.execute("DROP TRIGGER application_document_file_cleanup ON loan_application_documents")
    op.drop_table('loan_data_sharing_consents')
    op.drop_table('loan_application_documents')
    op.drop_table('loan_application_events')
    op.drop_table('farmer_loan_applications')
    op.execute("DELETE FROM stored_files WHERE key LIKE 'applications/%'")
    op.drop_column('lenders', 'submission_note')
    op.drop_column('lenders', 'submission_agreed_on')
    op.drop_column('lenders', 'submission_email')

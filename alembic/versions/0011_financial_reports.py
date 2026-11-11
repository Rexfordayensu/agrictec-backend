"""financial reports: server-generated PDFs a farmer can download again

Additive. Each report keeps its PDF in stored_files and a snapshot of the figures it was made
from; deleting the report (or the farmer's account) removes its file too.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('financial_report_exports',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('period_months', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('file_key', sa.String(length=512), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['file_key'], ['stored_files.key']),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint('period_months IN (3, 6, 12)', name='ck_financial_reports_months'),
    )
    op.create_index('ix_financial_report_exports_user_id', 'financial_report_exports', ['user_id'])
    # The report's PDF goes with it (cleanup_evidence_file deletes OLD.file_key; migration 0010).
    op.execute(
        "CREATE TRIGGER financial_report_file_cleanup AFTER DELETE ON financial_report_exports "
        "FOR EACH ROW EXECUTE FUNCTION cleanup_evidence_file()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS financial_report_file_cleanup ON financial_report_exports")
    op.drop_index('ix_financial_report_exports_user_id', table_name='financial_report_exports')
    op.drop_table('financial_report_exports')
    op.execute("DELETE FROM stored_files WHERE key LIKE 'reports/%'")

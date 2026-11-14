"""Guest data is never stored: delete every guest account and everything linked to it.

The guest demo now runs in the web app only (no account, no session, no data on the server).
Deleting a guest cascades to its profile, records, MoMo, receipts, chats, diagnoses, settings,
sessions and the rest; triggers remove the stored files (receipts, report PDFs, documents) with
their rows. Two tables keep a user ID without a link and are cleared first: the transaction
history (append-only, so its guard is paused for these rows only) and the old loan applications.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUESTS = "SELECT id FROM users WHERE is_guest"


def upgrade() -> None:
    op.execute("ALTER TABLE transaction_verifications DISABLE TRIGGER transaction_history_no_change")
    op.execute(f"DELETE FROM transaction_verifications WHERE user_id IN ({GUESTS})")
    op.execute("ALTER TABLE transaction_verifications ENABLE TRIGGER transaction_history_no_change")
    op.execute(f"DELETE FROM loan_applications WHERE user_id IN ({GUESTS})")
    op.execute("DELETE FROM users WHERE is_guest")


def downgrade() -> None:
    # Deleted guest data can't come back; nothing to undo in the schema.
    pass

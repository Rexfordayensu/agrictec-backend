"""no demo data in production: remove the demo world if it ever reached the live platform

The demo world (invented farmers, their records and applications, sample announcements and the
demo team) is for local development only. A backup restored from development could have carried
it into production. On the live platform (ENVIRONMENT=production) this removes it, the same way
the "Reset demo world" action clears it (app/services/seed.py clear_seed_data):
- farmers whose id starts "seed-farmer-" and everything of theirs (their applications first);
- announcements whose id starts "seed-ann-", and the "seeded" marker;
- the four demo team members (whose password is published in the code) are disabled, not
  deleted, so the audit trail keeps their names.
Real accounts are never touched: their ids are random. Outside production nothing changes.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-11
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEMO_TEAM_IDS = ('admin-efua', 'admin-kofi', 'admin-ama', 'admin-yaw')


def upgrade() -> None:
    if os.environ.get('ENVIRONMENT', '').strip().lower() != 'production':
        return
    run = op.get_bind().execute
    run(sa.text(
        "DELETE FROM loan_applications WHERE user_id IN (SELECT id FROM users WHERE id LIKE 'seed-farmer-%')"
    ))
    run(sa.text("DELETE FROM users WHERE id LIKE 'seed-farmer-%'"))
    run(sa.text("DELETE FROM announcements WHERE id LIKE 'seed-ann-%'"))
    run(sa.text("DELETE FROM platform_settings WHERE key = 'seeded'"))
    run(
        sa.text("UPDATE admins SET status = 'disabled' WHERE id IN :ids AND status <> 'disabled'")
        .bindparams(sa.bindparam('ids', expanding=True)),
        {'ids': list(DEMO_TEAM_IDS)},
    )


def downgrade() -> None:
    # Removed demo data is not recreated on the live platform.
    pass

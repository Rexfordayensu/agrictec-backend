"""user settings: when the farmer finished or skipped the app tour

Additive. user_settings.tour_seen_at is null until the farmer finishes or skips the 6-step
tour of the app (POST /me/tour), so it opens once for new farmers. Farmers who already use
AgriCtec are marked as having seen it, so this deploy doesn't interrupt them; anyone can still
replay it from Settings.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("tour_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE user_settings SET tour_seen_at = now()")
    op.execute(
        "INSERT INTO user_settings (user_id, language, notifications, tour_seen_at) "
        "SELECT id, 'en', '{}'::jsonb, now() FROM users "
        "WHERE id NOT IN (SELECT user_id FROM user_settings)"
    )


def downgrade() -> None:
    op.drop_column("user_settings", "tour_seen_at")

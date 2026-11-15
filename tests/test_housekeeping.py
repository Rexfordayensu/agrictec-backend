"""Sign-in data that has served its purpose is cleared: sessions ended or expired over 30 days
ago, used view-as-farmer tokens, old reset and phone codes, run-out lockouts. Anything still in
use stays."""

from datetime import timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.domain.clock import now_utc
from app.models import AuthSession, LoginLockout, PasswordReset, User
from app.services import housekeeping
from tests.conftest import API, signup


def test_old_sign_in_data_is_cleared_and_current_data_kept():
    ama = signup("housekeeping@example.com")
    now = now_utc()
    long_ago = now - timedelta(days=40)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "housekeeping@example.com"))
        db.add_all([
            AuthSession(id="old-ended", kind="farmer", user_id=user.id, created_at=long_ago,
                        last_activity_at=long_ago, expires_at=now + timedelta(days=1), ended_at=long_ago),
            AuthSession(id="old-expired", kind="farmer", user_id=user.id, created_at=long_ago,
                        last_activity_at=long_ago, expires_at=long_ago),
            PasswordReset(user_id=user.id, code_hash="x" * 64, created_at=long_ago, expires_at=long_ago),
            PasswordReset(user_id=user.id, code_hash="y" * 64, created_at=now, expires_at=now + timedelta(minutes=15)),
            LoginLockout(key="run-out", failures=5, locked_until=long_ago),
            LoginLockout(key="still-locked", failures=5, locked_until=now + timedelta(minutes=5)),
        ])
        db.commit()
        counts = housekeeping.purge_expired(db)
        assert counts["sessions"] == 2 and counts["password_resets"] == 1 and counts["lockouts"] == 1
        assert db.get(AuthSession, "old-ended") is None and db.get(LoginLockout, "run-out") is None
        assert db.get(LoginLockout, "still-locked") is not None
        assert db.scalar(select(PasswordReset).where(PasswordReset.user_id == user.id)) is not None
    # The farmer's own live session is untouched.
    assert ama.get(f"{API}/auth/session").status_code == 200


def test_it_runs_at_most_every_few_hours_per_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(housekeeping, "purge_expired", lambda db: calls.append(1) or {})
    monkeypatch.setattr(housekeeping, "_last_run", [0.0])
    housekeeping.run_if_due(None)
    housekeeping.run_if_due(None)
    assert calls == [1]

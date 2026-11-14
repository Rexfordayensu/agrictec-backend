"""Guest data is never stored: the guest demo runs in the web app only. The old guest endpoint
answers 410 and creates nothing, migration 0017 deletes the guest accounts that were stored
before (with everything linked to them, files included), and a platform restore of an older
backup drops guest accounts instead of bringing them back."""

import importlib.util
import pathlib
from datetime import date

from sqlalchemy import func, select, text, update

from app.models import (
    FarmRecord,
    MomoTransaction,
    StoredFile,
    TransactionEvidence,
    TransactionVerification,
    User,
)
from tests.conftest import ADMIN_PASSWORD, API, add_momo, add_record, new_client, signup, super_admin

MOMO = {"direction": "in", "counterparty": "Maize buyer", "amount": "250.00", "date": date.today().isoformat(),
        "network": "MTN", "reference": "MP261114.0800.G00001"}
SALE = {"type": "income", "description": "Maize sale", "crop": "Maize", "amount": "300", "date": date.today().isoformat()}


def _user_id(client) -> str:
    return client.get(f"{API}/auth/session").json()["userId"]


def _as_old_guest(db, user_id: str) -> None:
    """Turns an account into what a stored guest used to be (only possible in a test now)."""
    db.execute(update(User).where(User.id == user_id).values(is_guest=True, email="guest@agrictec.demo"))
    db.commit()


def _count(db, model, user_id: str) -> int:
    return db.scalar(select(func.count()).select_from(model).where(model.user_id == user_id)) or 0


def test_the_old_guest_endpoint_creates_nothing(db):
    users = db.scalar(select(func.count()).select_from(User))
    response = new_client().post(f"{API}/auth/guest")
    assert response.status_code == 410 and response.json()["code"] == "guest_demo_moved"
    assert "agrictec_session" not in response.cookies and "agrictec_guest" not in response.cookies
    assert db.scalar(select(func.count()).select_from(User)) == users


def test_migration_0017_deletes_stored_guests_and_everything_linked(db):
    guest, farmer = signup("old.guest@example.com"), signup("real.farmer@example.com")
    for client in (guest, farmer):
        assert add_momo(client, MOMO).status_code == 201
        assert add_record(client, SALE).status_code == 201
    guest_id, farmer_id = _user_id(guest), _user_id(farmer)
    files = {e.file_key for e in db.scalars(select(TransactionEvidence).where(TransactionEvidence.user_id == guest_id))}
    assert files and _count(db, TransactionVerification, guest_id) > 0
    _as_old_guest(db, guest_id)

    _run_migration(db)

    assert db.get(User, guest_id) is None
    for model in (FarmRecord, MomoTransaction, TransactionEvidence, TransactionVerification):
        assert _count(db, model, guest_id) == 0, model.__tablename__
    assert db.scalar(select(func.count()).select_from(StoredFile).where(StoredFile.key.in_(files))) == 0
    # The real farmer is untouched, and the history guard is back on.
    assert _count(db, FarmRecord, farmer_id) == 1 and _count(db, MomoTransaction, farmer_id) == 1
    assert _count(db, TransactionVerification, farmer_id) > 0
    try:
        db.execute(text("DELETE FROM transaction_verifications WHERE user_id = :u"), {"u": farmer_id})
        db.flush()
    except Exception:  # noqa: BLE001 - the append-only trigger refuses it
        db.rollback()
    else:
        raise AssertionError("the transaction history must stay append-only")


def test_restoring_an_older_backup_drops_guest_accounts(db):
    boss = super_admin()
    guest, farmer = signup("backup.guest@example.com"), signup("backup.farmer@example.com")
    assert add_momo(guest, MOMO).status_code == 201
    assert add_record(farmer, SALE).status_code == 201
    guest_id, farmer_id = _user_id(guest), _user_id(farmer)
    _as_old_guest(db, guest_id)

    # The real order: a backup made while guests were stored, migration 0017, then the old backup.
    secret = "correct horse battery staple"
    backup = boss.post(f"{API}/admin/system/backup", json={"passphrase": secret})
    assert backup.status_code == 200
    _run_migration(db)
    restored = boss.post(f"{API}/admin/system/restore", json={
        "backup": backup.json(), "passphrase": secret, "confirmation": "RESTORE", "password": ADMIN_PASSWORD,
    })
    assert restored.status_code == 200, restored.text
    db.expire_all()
    assert db.get(User, guest_id) is None
    assert db.scalar(select(func.count()).select_from(User).where(User.is_guest.is_(True))) == 0
    for model in (MomoTransaction, TransactionEvidence, TransactionVerification):
        assert _count(db, model, guest_id) == 0, model.__tablename__
    assert _count(db, FarmRecord, farmer_id) == 1


def _run_migration(db) -> None:
    """Runs migration 0017's upgrade on the test database."""
    migration = _load_migration()

    class Op:
        @staticmethod
        def execute(sql: str) -> None:
            db.execute(text(sql))

    migration.op = Op
    migration.upgrade()
    db.commit()


def _load_migration():
    path = pathlib.Path(__file__).parents[1] / "alembic" / "versions" / "0017_delete_guest_accounts.py"
    spec = importlib.util.spec_from_file_location("migration_0017", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

"""Database-backed logos: durability, replacement, backup and legacy import."""

import io

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.integrations.storage import store
from app.models import Lender, StoredFile
from app.scripts.migrate_r2_logos import copy_logos
from tests.conftest import API, ADMIN_PASSWORD, published_lender, super_admin
from tests.test_lenders import png


def test_logo_replacement_and_lender_deletion():
    boss = super_admin()
    lender = published_lender(boss)
    endpoint = f"{API}/admin/lenders/{lender['id']}/logo"
    first = png()
    for image in (first, first, png((50, 50))):
        assert boss.put(endpoint, files={"file": ("logo.png", image, "image/png")}).status_code == 200
        with SessionLocal() as db:
            assert len(db.scalars(select(StoredFile)).all()) == 1
            assert db.get(StoredFile, db.get(Lender, lender["id"]).logo_key) is not None
    assert boss.delete(f"{API}/admin/lenders/{lender['id']}").status_code == 200
    with SessionLocal() as db:
        assert db.scalars(select(StoredFile)).all() == []


def test_logo_survives_encrypted_backup_restore():
    boss = super_admin()
    lender = published_lender(boss)
    endpoint = f"{API}/admin/lenders/{lender['id']}/logo"
    assert boss.put(endpoint, files={"file": ("logo.png", png(), "image/png")}).status_code == 200
    original = boss.get(endpoint).content
    secret = "correct horse battery staple"
    response = boss.post(f"{API}/admin/system/backup", json={"passphrase": secret})
    assert response.status_code == 200
    backup = response.json()
    assert boss.delete(endpoint).status_code == 200
    assert boss.get(endpoint).status_code == 404
    restored = boss.post(f"{API}/admin/system/restore", json={
        "backup": backup, "passphrase": secret, "confirmation": "RESTORE", "password": ADMIN_PASSWORD,
    })
    assert restored.status_code == 200, restored.text
    assert boss.get(endpoint).content == original


def test_file_writes_rollback_with_the_transaction(db):
    files = store(db)
    files.put("uncommitted.webp", b"binary", "image/webp")
    db.rollback()
    assert files.get("uncommitted.webp") is None


def test_legacy_r2_import_preserves_keys_and_can_be_rerun():
    boss = super_admin()
    lender = published_lender(boss)
    key = f"lenders/{lender['id']}/old-logo.webp"
    with SessionLocal() as db:
        db.get(Lender, lender["id"]).logo_key = key
        db.commit()

    class OldBucket:
        calls = 0

        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "old-bucket", "Key": key}
            self.calls += 1
            return {"Body": io.BytesIO(b"existing logo"), "ContentType": "image/webp"}

    bucket = OldBucket()
    with SessionLocal() as db:
        assert copy_logos(db, bucket, "old-bucket") == 1
    with SessionLocal() as db:
        assert copy_logos(db, bucket, "old-bucket") == 0
        assert store(db).get(key) == (b"existing logo", "image/webp")
        assert db.get(Lender, lender["id"]).logo_key == key
    assert bucket.calls == 1


def test_legacy_import_failure_leaves_no_partial_files():
    boss = super_admin()
    for index in range(2):
        lender = published_lender(boss, name=f"Bank {index}")
        with SessionLocal() as db:
            db.get(Lender, lender["id"]).logo_key = f"old-{index}.webp"
            db.commit()

    class MissingObject:
        calls = 0

        def get_object(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Missing R2 object")
            return {"Body": io.BytesIO(b"first logo")}

    with pytest.raises(RuntimeError, match="Missing R2 object"):
        with SessionLocal() as db:
            copy_logos(db, MissingObject(), "old-bucket")
    with SessionLocal() as db:
        assert db.scalars(select(StoredFile)).all() == []

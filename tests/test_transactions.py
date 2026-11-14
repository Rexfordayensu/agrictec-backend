"""Milestone 3 contracts, against migrated PostgreSQL and the real API."""

import io
import os
from datetime import date
from decimal import Decimal

import pytest
from PIL import Image
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import AppError
from app.db import SessionLocal, engine
from app.domain.transactions import transition
from app.models import AuditEntry, StoredFile, TransactionEvidence, TransactionEvidenceLink, TransactionVerification
from app.services.transactions import MAX_BYTES, clean_file
from tests.conftest import API, HERE, ADMIN_PASSWORD, admin_with_role, signup, super_admin, add_momo, add_record


def add(farmer, **values):
    response = add_record(farmer, {
            "type": "income",
            "description": "Produce sold",
            "crop": "Maize",
            "amount": "12.10",
            "date": date.today().isoformat(),
            **values,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload(farmer, row, data=b"%PDF-1.7\nreceipt", kind="farm"):
    return farmer.post(
        f"{API}/me/transactions/{kind}/{row['id']}/evidence", files={"file": ("receipt.pdf", data, "application/pdf")}
    )


@pytest.mark.parametrize(
    "status,action,expected",
    [
        ("self_reported", "attach", "evidence_submitted"),
        ("rejected", "attach", "evidence_submitted"),
        ("evidence_submitted", "request_review", "verification_pending"),
        ("self_reported", "request_review", "verification_pending"),
        ("verification_pending", "verify", "verified"),
        ("verification_pending", "reject", "rejected"),
        ("rejected", "dispute", "disputed"),
        ("verified", "dispute", "disputed"),
        ("disputed", "verify", "verified"),
        ("disputed", "reject", "rejected"),
        ("verification_pending", "edit", "self_reported"),
        ("rejected", "edit", "self_reported"),
    ],
)
def test_allowed_status_transitions(status, action, expected):
    assert transition(status, action, evidence_count=1, reason="Reviewed supporting receipt") == expected


@pytest.mark.parametrize(
    "status,action",
    [
        ("self_reported", "verify"),
        ("evidence_submitted", "verify"),
        ("verified", "edit"),
        ("verified", "request_review"),
        ("self_reported", "dispute"),
        ("disputed", "request_review"),
        ("verified", "remove_evidence"),
        ("verification_pending", "remove_evidence"),
    ],
)
def test_refused_status_transitions(status, action):
    with pytest.raises(AppError):
        transition(status, action, evidence_count=1, reason="Explanation")


@pytest.mark.parametrize(
    "status,action",
    [
        ("rejected", "dispute"),
        ("verified", "dispute"),
        ("verification_pending", "reject"),
        ("verification_pending", "verify"),
    ],
)
def test_reasons_required(status, action):
    with pytest.raises(AppError):
        transition(status, action, evidence_count=1, reason="  ")


def test_review_requires_evidence():
    with pytest.raises(AppError):
        transition("self_reported", "request_review")


def test_evidence_sniffing_and_metadata_removal():
    for invalid in (b"<svg></svg>", b"<html>receipt</html>", b"random", b"", b"x" * (MAX_BYTES + 1)):
        with pytest.raises(AppError):
            clean_file(invalid)
    original = b"%PDF-1.7\nreal bytes"
    assert clean_file(original) == (original, "application/pdf")
    for format in ("PNG", "JPEG", "WEBP"):
        out = io.BytesIO()
        Image.new("RGB", (10, 10), (15, 120, 65)).save(out, format=format)
        data, content_type = clean_file(out.getvalue())
        assert content_type == "image/webp"
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "WEBP" and "exif" not in image.info


def test_exact_summary_and_duplicates():
    farmer = signup()
    row = add(farmer, amount="0.10")
    assert (
        row["category"] is None and row["sourceType"] == "farmer_entry" and row["verificationStatus"] == "self_reported"
    )
    add(farmer, amount="0.20")
    add(farmer, type="expense", amount="0.10", category="seed")
    add(farmer, type="financing", direction="in", category="loan_disbursement", amount="1000.00")
    summary = farmer.get(f"{API}/me/transactions/summary?months=3").json()
    assert summary["income"] == "0.30" and summary["expenses"] == "0.10" and summary["net"] == "0.20"
    assert summary["financingIn"] == "1000.00" and summary["momoIn"] == "0.00"
    duplicate = add_record(farmer, {
            "type": "income",
            "description": "Another sale",
            "crop": "Rice",
            "amount": "0.10",
            "date": date.today().isoformat(),
        },
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "possible_duplicate"
    add(farmer, amount="0.10", confirmDuplicate=True)
    body = {"direction": "in", "counterparty": "Buyer", "amount": "20.00", "date": date.today().isoformat()}
    first = add_momo(farmer, {**body, "reference": "REF-REAL"})
    assert first.status_code == 201
    duplicate = add_momo(farmer, {**body, "reference": "ref-real", "confirmDuplicate": True})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "duplicate_reference"
    unknown = add_momo(farmer, {**body, "amount": "21.00"})
    assert unknown.status_code == 201 and unknown.json()["reference"] is None
    assert (
        add_record(farmer, {
                "type": "income",
                "description": "Provider claim",
                "crop": "Maize",
                "amount": 1,
                "date": date.today().isoformat(),
                "sourceType": "provider_integration",
            },
        ).status_code
        == 422
    )


def test_isolation_permissions_review_history_and_verified_lock():
    farmer = signup()
    other = signup("other@example.com")
    boss = super_admin()
    row = add(farmer)
    root = f"{API}/me/transactions/farm/{row['id']}"
    assert farmer.post(root + "/request-review").status_code == 400
    evidence = upload(farmer, row).json()["evidence"][0]
    file = f"{API}/me/evidence/{evidence['id']}/file"
    own = farmer.get(file)
    assert own.status_code == 200
    assert own.headers["cache-control"] == "private, no-store" and own.headers["x-content-type-options"] == "nosniff"
    assert own.headers["content-disposition"].startswith("attachment;")
    assert other.get(file).status_code == 404 and other.get(root).status_code == 404
    assert other.post(root + "/request-review").status_code == 404 and upload(other, row).status_code == 404
    assert other.delete(f"{API}/me/evidence/{evidence['id']}").status_code == 404
    assert farmer.post(root + "/request-review").json()["verificationStatus"] == "verification_pending"
    assert farmer.delete(f"{API}/me/evidence/{evidence['id']}").status_code == 403
    admin_file = f"{API}/admin/transactions/evidence/{evidence['id']}/file"
    for role in ("support", "analyst"):
        staff = admin_with_role(boss, role)
        assert staff.get(f"{API}/admin/transactions/review").status_code == 403
        assert staff.get(admin_file).status_code == 403
        assert (
            staff.post(f"{API}/admin/transactions/farm/{row['id']}/verify", json={"reason": "Reviewed"}).status_code
            == 403
        )
    queue = boss.get(f"{API}/admin/transactions/review")
    assert queue.status_code == 200 and queue.json()["total"] == 1
    assert boss.get(admin_file).status_code == 200
    assert (
        boss.post(
            f"{API}/admin/transactions/farm/{row['id']}/verify", json={"reason": "Receipt matches entry"}
        ).status_code
        == 200
    )
    assert farmer.patch(f"{API}/me/records/{row['id']}", json={"amount": 20}).status_code == 403
    assert upload(farmer, row, b"%PDF-1.7 new evidence").status_code == 403
    assert farmer.delete(f"{API}/me/records/{row['id']}").status_code == 403
    assert farmer.delete(f"{API}/me/records").status_code == 403
    assert farmer.patch(f"{API}/me/records/{row['id']}", json={"notes": "Added context"}).status_code == 200
    history = farmer.get(root).json()["history"]
    assert [h["toStatus"] for h in history] == [
        "self_reported",
        "evidence_submitted",
        "verification_pending",
        "verified",
    ]
    assert farmer.get(f"{API}/me/notifications").status_code == 200
    assert boss.get(admin_file).status_code == 404  # no browsing files outside the queue
    assert farmer.post(root + "/dispute", json={"reason": "Amount needs another review"}).status_code == 200
    assert (
        boss.post(
            f"{API}/admin/transactions/farm/{row['id']}/reject", json={"reason": "Evidence does not support amount"}
        ).status_code
        == 200
    )
    assert farmer.post(root + "/dispute", json={"reason": "Please check the statement too"}).status_code == 200
    with SessionLocal() as db:
        actions = set(db.scalars(select(AuditEntry.action)).all())
        assert {
            "transaction.review_view",
            "transaction.evidence_view",
            "transaction.verify",
            "transaction.reject",
        } <= actions
    with engine.begin() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(text("UPDATE transaction_verifications SET reason='rewritten'"))


def test_evidence_dedup_count_edit_and_orphan_cleanup():
    farmer = signup()
    first = add(farmer)
    second = add(farmer, amount="13.00")
    # One statement may cover several entries (a receipt may not: see test_receipt_reuse).
    def statement(row):
        return farmer.post(f"{API}/me/transactions/farm/{row['id']}/evidence",
                           files={"file": ("statement.pdf", b"%PDF-1.7 statement", "application/pdf")},
                           data={"evidence_kind": "statement"})
    evidence = statement(first).json()["evidence"][0]
    assert statement(first).status_code == 200
    assert statement(second).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(TransactionEvidence)) == 1
        assert db.scalar(select(func.count()).select_from(TransactionEvidenceLink)) == 2
    assert farmer.delete(f"{API}/me/records/{first['id']}").status_code == 204
    assert farmer.get(f"{API}/me/evidence/{evidence['id']}/file").status_code == 200
    for n in range(4):
        assert upload(farmer, second, f"%PDF-1.7 receipt {n}".encode()).status_code == 200
    assert upload(farmer, second, b"%PDF-1.7 extra").status_code == 400
    assert farmer.post(f"{API}/me/transactions/farm/{second['id']}/request-review").status_code == 200
    assert (
        farmer.patch(f"{API}/me/records/{second['id']}", json={"notes": "Still pending"}).json()["verificationStatus"]
        == "verification_pending"
    )
    edited = farmer.patch(f"{API}/me/records/{second['id']}", json={"amount": "14.00"})
    assert edited.json()["verificationStatus"] == "self_reported"
    assert farmer.delete(f"{API}/me/records/{second['id']}").status_code == 204
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(TransactionEvidence)) == 0
        assert db.scalar(select(func.count()).select_from(StoredFile)) == 0
        assert db.scalar(select(func.count()).select_from(TransactionVerification)) > 0


def test_backup_round_trip_with_evidence_and_history():
    farmer = signup()
    boss = super_admin()
    row = add(farmer)
    evidence = upload(farmer, row).json()["evidence"][0]
    secret = "correct horse battery staple"
    backup = boss.post(f"{API}/admin/system/backup", json={"passphrase": secret})
    assert backup.status_code == 200, backup.text
    assert farmer.delete(f"{API}/me/records/{row['id']}").status_code == 204
    restored = boss.post(
        f"{API}/admin/system/restore",
        json={"backup": backup.json(), "passphrase": secret, "confirmation": "RESTORE", "password": ADMIN_PASSWORD},
    )
    assert restored.status_code == 200, restored.text
    assert (
        farmer.post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "password123"}).status_code
        == 200
    )
    assert farmer.get(f"{API}/me/evidence/{evidence['id']}/file").status_code == 200
    result = farmer.get(f"{API}/me/transactions/farm/{row['id']}").json()
    assert result["amount"] == "12.10" and result["evidenceCount"] == 1 and len(result["history"]) == 2


def test_migration_preserves_production_rows_and_downgrades():
    farmer = signup()
    uid = farmer.get(f"{API}/auth/session").json()["userId"]
    config = Config(os.path.join(HERE, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(HERE, "alembic"))
    command.downgrade(config, "0009")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO farm_records (id,user_id,type,description,crop,amount,date,created_at) VALUES ('old-farm',:uid,'income','Original description','Maize',12.34,current_date,now())"
                ),
                {"uid": uid},
            )
            for id in ("old-momo-1", "old-momo-2"):
                conn.execute(
                    text(
                        "INSERT INTO momo_transactions (id,user_id,direction,counterparty,amount,reference,date,created_at) VALUES (:id,:uid,'in','Original buyer',0.10,'SAME-REF',current_date,now())"
                    ),
                    {"uid": uid, "id": id},
                )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT amount,category,source_type,verification_status,currency FROM farm_records WHERE id='old-farm'"
                )
            ).one()
            assert tuple(row) == (Decimal("12.34"), None, "farmer_entry", "self_reported", "GHS")
            assert conn.execute(text("SELECT count(*) FROM momo_transactions")).scalar_one() == 2
            assert (
                conn.execute(text("SELECT count(*) FROM momo_transactions WHERE reference_duplicate")).scalar_one() == 1
            )
            assert conn.execute(text("SELECT count(*) FROM transaction_verifications")).scalar_one() == 3
        command.downgrade(config, "0009")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT amount FROM farm_records WHERE id='old-farm'")).scalar_one() == Decimal(
                "12.34"
            )
            assert (
                conn.execute(text("SELECT count(*) FROM momo_transactions WHERE reference='SAME-REF'")).scalar_one()
                == 2
            )
    finally:
        command.upgrade(config, "head")


def test_decimal_finance_and_existing_credit_score_results():
    from datetime import UTC, datetime
    from app.domain.credit import calculate_credit_score
    from app.domain.finance import total_income, total_expenses
    from app.schemas.farm import FarmRecord, Profile

    now = date(2026, 10, 15)
    entered = datetime(2026, 10, 15, 9, tzinfo=UTC)
    records = [
        FarmRecord(
            id=str(i),
            user_id="u",
            type=kind,
            description="Financial record",
            crop="Maize",
            amount=amount,
            date=now,
            created_at=entered,
        )
        for i, (kind, amount) in enumerate((("income", 0.10), ("income", 0.20), ("expense", 0.10)))
    ]
    assert total_income(records) == Decimal("0.30")
    assert total_expenses(records) == Decimal("0.10")
    before = calculate_credit_score(records, [], Profile(user_id="u"), now=now)
    # Where an entry came from doesn't change the score; AgriCtec verifying it does (in full,
    # against the unchecked 50%), and the 45-day rule still applies the same way.
    from_statement = [r.model_copy(update={"source_type": "statement_upload"}) for r in records]
    assert calculate_credit_score(from_statement, [], Profile(user_id="u"), now=now) == before
    verified = [r.model_copy(update={"verification_status": "verified"}) for r in records]
    after = calculate_credit_score(verified, [], Profile(user_id="u"), now=now)
    assert after.record_completeness > before.record_completeness
    assert after.payment_history > before.payment_history
    financing = records[0].model_copy(update={"type": "financing", "direction": "in", "amount": 9999})
    assert total_income([*records, financing]) == Decimal("0.30")
    assert total_expenses([*records, financing]) == Decimal("0.10")


def test_backup_restore_appends_status_reconciliation_without_erasing_decisions():
    farmer = signup()
    boss = super_admin()
    row = add(farmer)
    assert upload(farmer, row).status_code == 200
    secret = "correct horse battery staple"
    backup = boss.post(f"{API}/admin/system/backup", json={"passphrase": secret}).json()
    assert farmer.post(f"{API}/me/transactions/farm/{row['id']}/request-review").status_code == 200
    assert boss.post(f"{API}/admin/transactions/farm/{row['id']}/verify", json={"reason": "Receipt checked"}).status_code == 200
    restored = boss.post(f"{API}/admin/system/restore", json={"backup":backup,"passphrase":secret,"confirmation":"RESTORE","password":ADMIN_PASSWORD})
    assert restored.status_code == 200, restored.text
    assert farmer.post(f"{API}/auth/login",json={"email":"ama@example.com","password":"password123"}).status_code == 200
    entry = farmer.get(f"{API}/me/transactions/farm/{row['id']}").json()
    assert entry["verificationStatus"] == "evidence_submitted"
    assert entry["history"][-1]["fromStatus"] == "verified"
    assert entry["history"][-1]["toStatus"] == "evidence_submitted"
    assert entry["history"][-1]["actorType"] == "system"
    assert any(h["toStatus"] == "verified" and h["actorType"] == "admin" for h in entry["history"])


def test_database_refuses_cross_farmer_evidence_links():
    first, second = signup(), signup("second@example.com")
    first_entry, second_entry = add(first), add(second)
    evidence = upload(first, first_entry).json()["evidence"][0]
    with engine.begin() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(text("INSERT INTO transaction_evidence_links(evidence_id,record_kind,record_id) VALUES (:evidence,'farm',:record)"),
                         {"evidence":evidence["id"], "record":second_entry["id"]})

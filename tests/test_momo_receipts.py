"""MoMo transactions need their receipt: saved together, sent straight to AgriCtec's review, and
verified or rejected by an admin (POST /me/momo is multipart: `data` JSON + `receipt` file)."""

import json
from datetime import date

from app.db import SessionLocal
from app.domain.clock import now_utc
from app.models import MomoTransaction
from tests.conftest import API, add_momo, receipt_png, signup, super_admin

BODY = {"direction": "in", "counterparty": "Maize buyer", "amount": "250.00", "date": date.today().isoformat(),
        "network": "MTN", "reference": "MP260101.1200.A00001"}


def test_a_momo_transaction_is_refused_without_a_receipt_and_nothing_is_saved():
    farmer = signup()
    as_json = farmer.post(f"{API}/me/momo", json=BODY)
    assert as_json.status_code == 400 and as_json.json()["code"] == "receipt_required"
    no_file = farmer.post(f"{API}/me/momo", data={"data": json.dumps(BODY)})
    assert no_file.status_code == 400 and "receipt" in no_file.json()["detail"].lower()
    not_an_image = farmer.post(
        f"{API}/me/momo", data={"data": json.dumps(BODY)}, files={"receipt": ("r.txt", b"hello", "text/plain")}
    )
    assert not_an_image.status_code == 400
    bad_details = add_momo(farmer, {**BODY, "amount": "-5"})
    assert bad_details.status_code == 422
    assert farmer.get(f"{API}/me/momo").json() == []


def test_with_its_receipt_it_goes_to_review_and_the_admin_verifies_it():
    farmer = signup()
    boss = super_admin()
    created = add_momo(farmer, BODY)
    assert created.status_code == 201
    row = created.json()
    assert row["verificationStatus"] == "verification_pending"
    detail = farmer.get(f"{API}/me/transactions/momo/{row['id']}").json()
    assert len(detail["evidence"]) == 1 and detail["evidence"][0]["kind"] == "receipt"
    assert farmer.get(f"{API}/me/evidence/{detail['evidence'][0]['id']}/file").status_code == 200

    queue = boss.get(f"{API}/admin/transactions/review").json()
    assert queue["total"] == 1 and queue["items"][0]["entry"]["id"] == row["id"]
    assert boss.get(f"{API}/admin/transactions/review/count").json()
    verified = boss.post(f"{API}/admin/transactions/momo/{row['id']}/verify", json={"reason": "Receipt matches"})
    assert verified.status_code == 200 and verified.json()["status"] == "verified"
    assert farmer.get(f"{API}/me/momo").json()[0]["verificationStatus"] == "verified"
    inbox = farmer.get(f"{API}/me/notifications").json()["items"]
    assert inbox[0]["title"] == "Transaction review completed" and "verified" in inbox[0]["body"]
    # A verified entry is locked.
    assert farmer.patch(f"{API}/me/momo/{row['id']}", json={"amount": "999.00"}).status_code == 403


def test_after_a_rejection_a_new_receipt_or_corrected_details_send_it_back_to_review():
    farmer = signup()
    boss = super_admin()
    row = add_momo(farmer, BODY).json()
    rejected = boss.post(f"{API}/admin/transactions/momo/{row['id']}/reject", json={"reason": "Receipt unreadable"})
    assert rejected.json()["status"] == "rejected"
    clearer = farmer.post(
        f"{API}/me/transactions/momo/{row['id']}/evidence",
        files={"file": ("clear.png", receipt_png(77), "image/png")},
    )
    assert clearer.status_code == 200 and clearer.json()["verificationStatus"] == "verification_pending"

    boss.post(f"{API}/admin/transactions/momo/{row['id']}/reject", json={"reason": "Amount differs"})
    fixed = farmer.patch(f"{API}/me/momo/{row['id']}", json={"amount": "240.00"})
    assert fixed.status_code == 200
    assert farmer.get(f"{API}/me/momo").json()[0]["verificationStatus"] == "verification_pending"


def test_an_older_entry_without_a_receipt_is_sent_for_review_once_its_receipt_is_added():
    farmer = signup()
    me = farmer.get(f"{API}/me/profile").json()["userId"]
    with SessionLocal() as db:  # saved before receipts were required
        db.add(MomoTransaction(id="legacy-1", user_id=me, direction="out", counterparty="Input shop", amount=80,
                               date=date.today(), verification_status="self_reported",
                               created_at=now_utc()))
        db.commit()
    assert farmer.get(f"{API}/me/momo").json()[0]["verificationStatus"] == "self_reported"
    added = farmer.post(
        f"{API}/me/transactions/momo/legacy-1/evidence", files={"file": ("r.png", receipt_png(5), "image/png")}
    )
    assert added.json()["verificationStatus"] == "verification_pending"


def test_receipts_and_entries_stay_private():
    farmer, other = signup(), signup("other.momo@example.com")
    row = add_momo(farmer, BODY).json()
    evidence = farmer.get(f"{API}/me/transactions/momo/{row['id']}").json()["evidence"][0]
    assert other.get(f"{API}/me/transactions/momo/{row['id']}").status_code == 404
    assert other.get(f"{API}/me/evidence/{evidence['id']}/file").status_code == 404
    assert other.get(f"{API}/me/momo").json() == []

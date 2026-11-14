"""How a farm record was paid: cash; bank (name and reference); or mobile money (network and its
receipt, then AgriCtec's review). Activities carry no payment."""

import json
from datetime import date

from app.db import SessionLocal
from app.domain.clock import now_utc
from app.models import FarmRecord
from tests.conftest import API, receipt_png, signup, super_admin

TODAY = date.today().isoformat()
SALE = {"type": "income", "description": "Sold maize", "crop": "Maize", "amount": "300.00", "date": TODAY}


def with_receipt(client, body, seed=1):
    return client.post(
        f"{API}/me/records/with-receipt",
        data={"data": json.dumps(body)},
        files={"receipt": ("receipt.png", receipt_png(seed), "image/png")},
    )


def test_a_money_record_must_say_how_it_was_paid_but_an_activity_need_not():
    farmer = signup()
    missing = farmer.post(f"{API}/me/records", json=SALE)
    assert missing.status_code == 400 and missing.json()["code"] == "payment_method_required"
    activity = farmer.post(
        f"{API}/me/records",
        json={"type": "activity", "description": "Weeding", "crop": "Maize", "date": TODAY, "paymentMethod": "cash"},
    )
    assert activity.status_code == 201 and activity.json()["paymentMethod"] is None
    cash = farmer.post(f"{API}/me/records", json={**SALE, "paymentMethod": "cash"})
    assert cash.status_code == 201 and cash.json()["verificationStatus"] == "self_reported"


def test_a_bank_payment_needs_the_bank_name_and_reference():
    farmer = signup()
    no_bank = farmer.post(f"{API}/me/records", json={**SALE, "paymentMethod": "bank", "externalReference": "FT1"})
    assert no_bank.status_code == 400 and no_bank.json()["code"] == "bank_name_required"
    no_ref = farmer.post(f"{API}/me/records", json={**SALE, "paymentMethod": "bank", "bankName": "GCB Bank"})
    assert no_ref.status_code == 400 and no_ref.json()["code"] == "bank_reference_required"
    saved = farmer.post(
        f"{API}/me/records",
        json={**SALE, "paymentMethod": "bank", "bankName": "GCB Bank", "externalReference": "FT2611130042",
              "network": "MTN"},
    ).json()
    assert (saved["paymentMethod"], saved["bankName"], saved["externalReference"], saved["network"]) == (
        "bank", "GCB Bank", "FT2611130042", None)


def test_mobile_money_needs_the_network_and_receipt_and_goes_to_review():
    farmer = signup()
    boss = super_admin()
    as_json = farmer.post(f"{API}/me/records", json={**SALE, "paymentMethod": "mobile_money", "network": "MTN"})
    assert as_json.status_code == 400 and as_json.json()["code"] == "receipt_required"
    no_network = with_receipt(farmer, {**SALE, "paymentMethod": "mobile_money"})
    assert no_network.status_code == 400 and no_network.json()["code"] == "network_required"
    assert farmer.get(f"{API}/me/records").json() == []

    row = with_receipt(farmer, {**SALE, "paymentMethod": "mobile_money", "network": "Telecel"}).json()
    assert row["verificationStatus"] == "verification_pending" and row["network"] == "Telecel"
    queue = boss.get(f"{API}/admin/transactions/review").json()
    assert queue["total"] == 1 and queue["items"][0]["entry"]["paymentMethod"] == "mobile_money"
    done = boss.post(f"{API}/admin/transactions/farm/{row['id']}/verify", json={"reason": "Receipt matches"})
    assert done.json()["status"] == "verified"


def test_a_cash_record_with_a_receipt_keeps_it_without_joining_the_review_queue():
    farmer = signup()
    boss = super_admin()
    row = with_receipt(farmer, {**SALE, "paymentMethod": "cash"}, seed=9).json()
    assert row["verificationStatus"] == "evidence_submitted" and row["evidenceCount"] == 1
    assert boss.get(f"{API}/admin/transactions/review").json()["total"] == 0


def test_switching_a_record_to_mobile_money_needs_its_receipt_then_it_is_reviewed():
    farmer = signup()
    row = farmer.post(f"{API}/me/records", json={**SALE, "paymentMethod": "cash"}).json()
    switch = {"paymentMethod": "mobile_money", "network": "MTN"}
    refused = farmer.patch(f"{API}/me/records/{row['id']}", json=switch)
    assert refused.status_code == 400 and refused.json()["code"] == "receipt_required"
    farmer.post(f"{API}/me/transactions/farm/{row['id']}/evidence",
                files={"file": ("r.png", receipt_png(21), "image/png")})
    switched = farmer.patch(f"{API}/me/records/{row['id']}", json=switch).json()
    assert switched["paymentMethod"] == "mobile_money" and switched["verificationStatus"] == "verification_pending"


def test_an_older_record_without_a_payment_method_can_still_be_edited():
    farmer = signup()
    me = farmer.get(f"{API}/me/profile").json()["userId"]
    with SessionLocal() as db:
        db.add(FarmRecord(id="old-1", user_id=me, type="expense", description="Seed", crop="Maize", amount=50,
                          date=date.today(), payment_method="paid at the shop", created_at=now_utc()))
        db.commit()
    edited = farmer.patch(f"{API}/me/records/old-1", json={"description": "Maize seed"})
    assert edited.status_code == 200 and edited.json()["paymentMethod"] == "paid at the shop"

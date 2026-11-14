"""Regression checks for farmer backup compatibility and cross-worker AI credit spending."""

import threading
import time

import pytest
from sqlalchemy import func, select, text

from app.core.errors import Forbidden
from app.db import SessionLocal
from app.domain.clock import now_utc
from app.domain.plans import AI_COSTS, month_key
from app.models import CreditReservation
from app.services import subscriptions
from tests.conftest import API, ONBOARDING, signup, add_momo, add_record


def test_farmer_backup_restores_financing_and_optional_momo_reference():
    client = signup()
    assert client.post(f"{API}/me/onboarding/complete", json=ONBOARDING).status_code == 200
    record = {
        "type": "financing", "direction": "in", "category": "loan_disbursement",
        "description": "Recorded financing", "crop": "Maize", "amount": 123.45,
        "date": "2026-10-01", "notes": "Financing note", "sourceType": "statement_upload",
        "paymentMethod": "bank", "bankName": "GCB Bank", "externalReference": "FIN-1",
    }
    momo = {
        "direction": "out", "counterparty": "Supplier", "amount": 12.30, "date": "2026-10-02",
        "category": "seed", "notes": "Paid for seed", "paymentMethod": "mobile money",
        "externalReference": "SEED-1", "sourceType": "farmer_entry",
    }
    assert add_record(client, record).status_code == 201
    assert add_momo(client, momo).status_code == 201
    exported = client.get(f"{API}/me/export").json()
    # A copied file cannot assert that an entry was verified by a reviewer.
    exported["records"][0]["verificationStatus"] = "verified"
    exported["records"][0]["evidenceCount"] = 5
    assert client.post(f"{API}/me/import", json=exported).status_code == 200
    restored = client.get(f"{API}/me/records").json()[0]
    restored_momo = client.get(f"{API}/me/momo").json()[0]
    for key, value in record.items():
        assert restored[key] == value
    for key, value in momo.items():
        assert restored_momo[key] == value
    assert restored_momo["reference"] is None
    assert restored["verificationStatus"] == restored_momo["verificationStatus"] == "self_reported"
    assert restored["evidenceCount"] == 0

    # Invalid financing in an import is refused before any existing entries are cleared.
    exported["records"][0]["direction"] = None
    invalid = client.post(f"{API}/me/import", json=exported)
    assert invalid.status_code == 400
    assert client.get(f"{API}/me/records").json()[0]["id"] == restored["id"]


@pytest.mark.parametrize("feature", ["aiAssistant", "cropDoctor"])
def test_concurrent_requests_cannot_both_spend_the_last_credits(db, monkeypatch, feature):
    client = signup()
    assert client.post(f"{API}/me/subscription", json={"planId": "premium", "method": "card"}).status_code == 200
    user_id = client.get(f"{API}/auth/session").json()["userId"]
    now = now_utc()
    db.add(CreditReservation(
        user_id=user_id, month=month_key(now), feature=feature,
        cost=100 - AI_COSTS[feature], status="settled", created_at=now, settled_at=now,
    ))
    db.commit()

    checked = threading.Event()
    release = threading.Event()
    second_connected = threading.Event()
    pids, outcomes, errors = [], [], []
    original = subscriptions.assert_feature

    def pause_after_check(session, *args, **kwargs):
        snap = original(session, *args, **kwargs)
        if session.info.get("pause_credit_check"):
            checked.set()
            assert release.wait(10), "first credit check was not released"
        return snap

    monkeypatch.setattr(subscriptions, "assert_feature", pause_after_check)

    def spend(first=False):
        try:
            with SessionLocal() as session:
                session.info["pause_credit_check"] = first
                if not first:
                    pids.append(session.scalar(select(func.pg_backend_pid())))
                    second_connected.set()
                try:
                    subscriptions.reserve(session, user_id, feature)
                    outcomes.append("reserved")
                except Forbidden as error:
                    outcomes.append(error.code)
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=spend, kwargs={"first": True}, daemon=True)
    second = threading.Thread(target=spend, daemon=True)
    first.start()
    blocked = False
    try:
        assert checked.wait(10)
        second.start()
        assert second_connected.wait(10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blocked = bool(db.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pids[0]}))
            if blocked or not second.is_alive():
                break
            time.sleep(0.01)
    finally:
        release.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert blocked, "the second worker must wait until the first balance check and hold commit"
    assert sorted(outcomes) == ["credits_exhausted", "reserved"]
    assert subscriptions.snapshot(db, user_id).credits_used == 100

    # A failed provider call releases its hold and makes those credits available again.
    hold = db.scalar(select(CreditReservation).where(CreditReservation.user_id == user_id,
                                                    CreditReservation.status == "pending"))
    subscriptions.settle(db, hold, success=False)
    subscriptions.reserve(db, user_id, feature)
    assert subscriptions.snapshot(db, user_id).credits_used == 100

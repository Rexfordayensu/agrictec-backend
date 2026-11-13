"""Billing history and payment receipts (GET /me/subscription/payments, .../receipt)."""

from datetime import timedelta

from app.db import SessionLocal
from app.domain.clock import now_utc
from app.models import Payment
from tests.conftest import API, ONBOARDING, signup


def _farmer(email):
    c = signup(email)
    c.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    return c


def test_no_payments_means_an_empty_history():
    c = _farmer("bill.none@example.com")
    assert c.get(f"{API}/me/subscription/payments").json() == []


def test_a_simulated_payment_is_listed_as_simulated_with_a_receipt():
    c = _farmer("bill.demo@example.com")
    assert c.post(f"{API}/me/subscription", json={"planId": "premium", "method": "mobile-money"}).status_code == 200
    [item] = c.get(f"{API}/me/subscription/payments").json()
    assert item["status"] == "simulated" and item["planName"] == "Premium" and item["amountGHS"] == "50.00"
    assert item["reference"].startswith("DEMO-")
    pdf = c.get(f"{API}/me/subscription/payments/{item['reference']}/receipt")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")


def test_paystack_payments_are_listed_newest_first_and_kept_private():
    c = _farmer("bill.paid@example.com")
    me = c.get(f"{API}/me/profile").json()["userId"]
    now = now_utc()
    with SessionLocal() as db:
        db.add_all([
            Payment(reference="PSK-OLD", user_id=me, plan_id="basic", amount=25, method="card", status="paid",
                    created_at=now - timedelta(days=40), paid_at=now - timedelta(days=40),
                    provider_data={"channel": "card"}),
            Payment(reference="PSK-NEW", user_id=me, plan_id="premium", amount=50, method="mobile-money",
                    status="failed", created_at=now - timedelta(days=1)),
        ])
        db.commit()
    items = c.get(f"{API}/me/subscription/payments").json()
    assert [(i["reference"], i["status"], i["amountGHS"]) for i in items] == [
        ("PSK-NEW", "failed", "50.00"), ("PSK-OLD", "paid", "25.00"),
    ]
    assert c.get(f"{API}/me/subscription/payments/PSK-OLD/receipt").content.startswith(b"%PDF")
    # Another farmer can neither list nor download them.
    other = _farmer("bill.other@example.com")
    assert other.get(f"{API}/me/subscription/payments").json() == []
    assert other.get(f"{API}/me/subscription/payments/PSK-OLD/receipt").status_code == 404

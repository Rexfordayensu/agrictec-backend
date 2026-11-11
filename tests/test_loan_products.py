"""Loan products: drafts, frozen versions, and what farmers see."""

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal, engine
from tests.conftest import API, ONBOARDING, TEST_LENDER, admin_with_role, published_lender, signup, super_admin

TERMS = {
    "purpose": "Inputs for the major season",
    "minAmount": "1000.00",
    "maxAmount": "50000.00",
    "interestRate": "28.5",
    "ratePeriod": "year",
    "rateMethod": "reducing_balance",
    "fees": [{"name": "Processing fee", "kind": "percent", "value": "2"}],
    "minMonths": 6,
    "maxMonths": 12,
    "repaymentFrequency": "monthly",
    "supportedRegions": ["Ashanti", "Bono"],
}
REQUIREMENTS = [
    {"key": "ghana_card", "name": "Ghana Card", "category": "identity", "required": True,
     "fieldType": "none", "check": {"metric": "ghana_card_verified", "op": "is_true"},
     "verification": "agrictec_verified"},
    {"key": "has_guarantor", "name": "Do you have a guarantor?", "category": "other", "required": True,
     "fieldType": "yes_no"},
    {"key": "guarantor_letter", "name": "Guarantor letter", "category": "other", "required": False,
     "fieldType": "document", "allowedFormats": ["pdf", "jpg"], "maxSizeMb": 5,
     "visibleIf": {"key": "has_guarantor", "equals": "yes"}, "verification": "lender_verifies"},
    {"key": "records", "name": "Six months of records", "category": "financial", "required": True,
     "fieldType": "none", "check": {"metric": "months_of_records", "op": "gte", "value": 6}},
]


def content(**over) -> dict:
    return {
        "name": "Season Input Loan",
        "terms": TERMS,
        "requirements": REQUIREMENTS,
        "source": "Lender's agricultural loan brochure (test)",
        "sourceCheckedOn": date.today().isoformat(),
        **over,
    }


def new_product(boss, lender_id: str) -> dict:
    r = boss.post(f"{API}/admin/lenders/{lender_id}/products", json={"name": "Season Input Loan"})
    assert r.status_code == 201, r.text
    return r.json()


def test_a_new_product_is_an_empty_draft_nobody_sees(client):
    boss = super_admin()
    lender = published_lender(boss)
    product = new_product(boss, lender["id"])
    assert product["status"] == "draft" and product["published"] is None
    # Nothing is filled in for the admin.
    assert product["draft"]["terms"]["interestRate"] is None and product["draft"]["requirements"] == []
    assert product["problems"] and not product["listed"]
    assert client.get(f"{API}/platform/loan-products").json() == []
    assert client.get(f"{API}/platform").json()["loanProducts"] == []


def test_publishing_needs_a_source_and_consistent_terms():
    boss = super_admin()
    product = new_product(boss, published_lender(boss)["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    bad = content(source=None, terms={**TERMS, "minAmount": "60000"})
    saved = boss.put(f"{url}/draft", json=bad).json()
    assert any("come from" in p for p in saved["problems"])
    assert any("smallest loan" in p for p in saved["problems"])
    r = boss.post(f"{url}/publish", json={"note": "First version"})
    assert r.status_code == 400 and r.json()["code"] == "not_publishable"
    # A requirement can only depend on an earlier yes/no or choice requirement.
    swapped = content(requirements=[REQUIREMENTS[2], REQUIREMENTS[1]])
    problems = boss.put(f"{url}/draft", json=swapped).json()["problems"]
    assert any("listed before it" in p for p in problems)
    future = content(sourceCheckedOn=(date.today() + timedelta(days=1)).isoformat())
    assert any("future" in p for p in boss.put(f"{url}/draft", json=future).json()["problems"])
    # Unknown checks and keys are refused outright.
    wrong = content(requirements=[{**REQUIREMENTS[0], "check": {"metric": "credit_bureau", "op": "is_true"}}])
    assert boss.put(f"{url}/draft", json=wrong).status_code == 422


def test_publish_freezes_a_version_farmers_see(client):
    boss = super_admin()
    lender = published_lender(boss)
    product = new_product(boss, lender["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    assert boss.put(f"{url}/draft", json=content()).json()["problems"] == []
    published = boss.post(f"{url}/publish", json={"note": "From the 2026 brochure"}).json()
    assert published["status"] == "published" and published["published"]["version"] == 1
    assert published["listed"] and not published["draftChanged"]

    listed = client.get(f"{API}/platform/loan-products").json()
    assert [p["id"] for p in listed] == [product["id"]]
    shown = listed[0]
    # Exact decimals, as text; nothing rounded or invented.
    assert shown["terms"]["interestRate"] == "28.5" and shown["terms"]["maxAmount"] == "50000.00"
    assert shown["terms"]["description"] is None and shown["terms"]["collateral"] is None
    assert [r["key"] for r in shown["requirements"]] == ["ghana_card", "has_guarantor", "guarantor_letter", "records"]
    assert shown["requirements"][2]["visibleIf"] == {"key": "has_guarantor", "equals": "yes"}
    assert shown["lender"]["name"] == lender["name"] and shown["version"] == 1
    assert client.get(f"{API}/platform/loan-products/{product['id']}").status_code == 200

    # Editing the draft doesn't change what farmers see until the next publish.
    boss.put(f"{url}/draft", json=content(terms={**TERMS, "interestRate": "26"}))
    assert client.get(f"{API}/platform/loan-products").json()[0]["terms"]["interestRate"] == "28.5"
    v2 = boss.post(f"{url}/publish", json={"note": "Rate lowered to 26%"}).json()
    assert v2["published"]["version"] == 2
    assert client.get(f"{API}/platform/loan-products").json()[0]["terms"]["interestRate"] == "26"
    history = boss.get(f"{url}/versions").json()
    assert [v["version"] for v in history] == [2, 1] and history[1]["terms"]["interestRate"] == "28.5"


def test_published_versions_can_never_be_changed():
    boss = super_admin()
    product = new_product(boss, published_lender(boss)["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    boss.put(f"{url}/draft", json=content())
    boss.post(f"{url}/publish", json={"note": "First version"})
    for table in ("loan_product_versions", "loan_requirements"):
        with pytest.raises(DBAPIError, match="read-only"), engine.begin() as conn:
            conn.execute(text(f"update {table} set id = id"))


def test_products_follow_their_lender_and_their_own_status(client):
    boss = super_admin()
    lender = published_lender(boss)
    product = new_product(boss, lender["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    boss.put(f"{url}/draft", json=content())
    boss.post(f"{url}/publish", json={"note": "First version"})
    assert len(client.get(f"{API}/platform/loan-products").json()) == 1
    boss.post(f"{API}/admin/lenders/{lender['id']}/suspend", json={"reason": "Licence under question"})
    assert client.get(f"{API}/platform/loan-products").json() == []
    assert client.get(f"{API}/platform/loan-products/{product['id']}").status_code == 404
    boss.post(f"{API}/admin/lenders/{lender['id']}/reinstate")
    boss.post(f"{API}/admin/lenders/{lender['id']}/publish")
    assert len(client.get(f"{API}/platform/loan-products").json()) == 1
    boss.post(f"{url}/unpublish")
    assert client.get(f"{API}/platform/loan-products").json() == []
    # Republishing an unchanged draft brings back the same version, not a new one.
    again = boss.post(f"{url}/publish", json={"note": "Back on"}).json()
    assert again["published"]["version"] == 1 and again["status"] == "published"
    # A published product keeps its history: it can't be deleted.
    assert boss.delete(url).status_code == 400


def test_drafts_can_be_discarded_and_unpublished_drafts_deleted():
    boss = super_admin()
    lender = published_lender(boss)
    product = new_product(boss, lender["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    assert boss.post(f"{url}/draft/reset").status_code == 400
    boss.put(f"{url}/draft", json=content())
    boss.post(f"{url}/publish", json={"note": "First version"})
    boss.put(f"{url}/draft", json=content(name="Renamed"))
    reset = boss.post(f"{url}/draft/reset").json()
    assert reset["draft"]["name"] == "Season Input Loan" and not reset["draftChanged"]
    other = new_product(boss, lender["id"])
    assert boss.delete(f"{API}/admin/loan-products/{other['id']}").status_code == 200


def test_only_lender_managers_manage_products_and_every_step_is_audited():
    boss = super_admin()
    lender = published_lender(boss)
    support = admin_with_role(boss, "support")
    assert support.post(f"{API}/admin/lenders/{lender['id']}/products", json={"name": "X"}).status_code == 403
    product = new_product(boss, lender["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    assert support.get(url).status_code == 403
    boss.put(f"{url}/draft", json=content())
    boss.post(f"{url}/publish", json={"note": "First version"})
    with SessionLocal() as db:
        actions = db.execute(
            text("select action from audit_log where target_id = :id order by seq"), {"id": product["id"]}
        ).scalars().all()
    assert actions == ["product.create", "product.draft", "product.publish"]


def test_farmers_see_products_but_cannot_apply_through_agrictec_yet():
    boss = super_admin()
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json={**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-123456789-0"}})
    request = boss.get(f"{API}/admin/verifications", params={"status": "pending", "kind": "ghanaCard"}).json()[0]
    boss.post(f"{API}/admin/verifications/{request['id']}/approve")
    lender = published_lender(boss)
    loan = {"partnerId": lender["id"], "amount": 1000, "purpose": "Farm Equipment", "months": 12}
    assert farmer.post(f"{API}/me/loans/applications", json=loan).json()["code"] == "no_loan_products"
    product = new_product(boss, lender["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    boss.put(f"{url}/draft", json=content())
    boss.post(f"{url}/publish", json={"note": "First version"})
    r = farmer.post(f"{API}/me/loans/applications", json=loan)
    assert r.status_code == 400 and r.json()["code"] == "applications_not_open"
    assert farmer.get(f"{API}/me/loans/applications").json() == []


def test_deleting_a_lender_removes_its_unused_products():
    boss = super_admin()
    lender = boss.post(f"{API}/admin/lenders", json={**TEST_LENDER, "name": "Short-lived Test Lender"}).json()
    new_product(boss, lender["id"])
    assert boss.delete(f"{API}/admin/lenders/{lender['id']}").status_code == 200
    with SessionLocal() as db:
        assert db.execute(text("select count(*) from loan_products")).scalar() == 0


def test_a_lender_with_published_products_cannot_be_deleted():
    boss = super_admin()
    lender = published_lender(boss)
    product = new_product(boss, lender["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    boss.put(f"{url}/draft", json=content())
    boss.post(f"{url}/publish", json={"note": "First version"})
    r = boss.delete(f"{API}/admin/lenders/{lender['id']}")
    assert r.status_code == 400 and "version history" in r.json()["detail"]

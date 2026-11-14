"""Loan applications: the farmer prepares and consents, AgriCtec reviews and sends, and the
lender's progress is recorded only with the lender's own proof. AgriCtec never decides a loan."""

from datetime import date

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.integrations import messaging
from app.models import AuditEntry, LoanDataSharingConsent, Notification
from tests.conftest import API, ONBOARDING, admin_with_role, new_client, published_lender, signup, super_admin, add_record

PDF = b"%PDF-1.4\n% test document\n"
DATES = {"effectiveFrom": "2026-01-01", "reviewedOn": date.today().isoformat()}
REQUIREMENTS = [
    {"key": "ghana_card", "name": "Ghana Card", "category": "identity", "required": True, "fieldType": "none",
     "check": {"metric": "ghana_card_verified", "op": "is_true"}, "verification": "agrictec_verified"},
    {"key": "records", "name": "A month of records", "category": "financial", "required": True,
     "fieldType": "none", "check": {"metric": "months_of_records", "op": "gte", "value": 1}, **DATES},
    {"key": "has_guarantor", "name": "Do you have a guarantor?", "category": "other", "required": True,
     "fieldType": "yes_no"},
    {"key": "guarantor_name", "name": "Guarantor's name", "category": "other", "required": True,
     "fieldType": "text", "visibleIf": {"key": "has_guarantor", "equals": "yes"}},
    {"key": "statement", "name": "MoMo statement", "category": "financial", "required": True,
     "fieldType": "document", "allowedFormats": ["pdf"], "maxSizeMb": 5, "verification": "lender_verifies"},
]
LETTER = "I grow maize on five acres in Ejisu and need inputs for the major season."


def product_content(requirements=REQUIREMENTS) -> dict:
    return {
        "name": "Season Input Loan",
        "terms": {"minAmount": "1000.00", "maxAmount": "20000.00", "interestRate": "24", "ratePeriod": "year",
                  "rateMethod": "reducing_balance", "repaymentFrequency": "monthly", "minMonths": 6, "maxMonths": 12},
        "requirements": requirements,
        "source": "Lender's agricultural loan brochure (test)",
        "sourceCheckedOn": date.today().isoformat(),
    }


def setup(boss, requirements=REQUIREMENTS, lender_fields=None) -> tuple[dict, dict]:
    lender = published_lender(boss, **(lender_fields or {}))
    product = boss.post(f"{API}/admin/lenders/{lender['id']}/products", json={"name": "Season Input Loan"}).json()
    url = f"{API}/admin/loan-products/{product['id']}"
    saved = boss.put(f"{url}/draft", json=product_content(requirements)).json()
    assert saved["problems"] == [], saved["problems"]
    assert boss.post(f"{url}/publish", json={"note": "First version"}).status_code == 200
    return lender, product


def ready_farmer(boss, email="ama@example.com", card=True):
    farmer = signup(email)
    onboarding = {**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-123456789-0"}} if card else ONBOARDING
    farmer.post(f"{API}/me/onboarding/complete", json=onboarding)
    if card:
        request = boss.get(f"{API}/admin/verifications", params={"status": "pending", "kind": "ghanaCard"}).json()[0]
        assert boss.post(f"{API}/admin/verifications/{request['id']}/approve").status_code == 200
    add_record(farmer, {"type": "income", "category": "crop_sale", "description": "Maize sale (test)",
                                          "crop": "Maize", "amount": 2400, "date": date.today().isoformat()})
    return farmer


def prepared(farmer, product_id, **over) -> dict:
    app = farmer.post(f"{API}/me/applications", json={"productId": product_id, "amount": "5000.00", "months": 12,
                                                      "purpose": "Fertilizer and seed", **over})
    assert app.status_code == 201, app.text
    app = app.json()
    url = f"{API}/me/applications/{app['id']}"
    farmer.patch(url, json={"answers": {"has_guarantor": "no"}, "letter": LETTER})
    r = farmer.post(f"{url}/documents", data={"requirementKey": "statement"}, files={"file": ("momo.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def notice():
    return new_client().get(f"{API}/me/applications/consent-notice").json()


def submit(farmer, app_id):
    return farmer.post(f"{API}/me/applications/{app_id}/submit", json={"consent": True, "noticeVersion": notice()["version"]})


# ---------- The farmer prepares ----------


def test_a_draft_is_validated_and_lists_what_is_missing():
    boss = super_admin()
    _, product = setup(boss)
    farmer = ready_farmer(boss)
    app = farmer.post(f"{API}/me/applications", json={"productId": product["id"], "amount": "5000", "months": 12,
                                                      "purpose": "Inputs"}).json()
    assert app["status"] == "draft" and app["canSubmit"] is False
    assert set(app["missing"]) == {"Answer: Do you have a guarantor?", "Upload: MoMo statement",
                                   "Write your application letter (at least 30 characters)."}
    again = farmer.post(f"{API}/me/applications", json={"productId": product["id"], "amount": "5000", "months": 12,
                                                        "purpose": "Inputs"})
    assert again.status_code == 409 and again.json()["applicationId"] == app["id"]
    url = f"{API}/me/applications/{app['id']}"
    assert farmer.patch(url, json={"answers": {"has_guarantor": "maybe"}}).status_code == 400
    assert farmer.patch(url, json={"answers": {"not_a_question": "x"}}).status_code == 400
    # A conditional question appears only when its condition is met.
    shown = farmer.patch(url, json={"answers": {"has_guarantor": "yes"}}).json()
    assert "Answer: Guarantor's name" in shown["missing"]
    assert farmer.post(f"{url}/documents", data={"requirementKey": "statement"},
                       files={"file": ("x.png", b"\x89PNG\r\n\x1a\nnotreally", "image/png")}).status_code == 400
    assert farmer.post(f"{url}/documents", data={"requirementKey": "has_guarantor"},
                       files={"file": ("m.pdf", PDF, "application/pdf")}).status_code == 400


def test_submission_needs_consent_and_attaches_the_check_and_statement():
    boss = super_admin()
    _, product = setup(boss)
    farmer = ready_farmer(boss)
    app = prepared(farmer, product["id"])
    assert app["canSubmit"] is True
    url = f"{API}/me/applications/{app['id']}"
    refused = farmer.post(f"{url}/submit", json={"consent": False, "noticeVersion": notice()["version"]})
    assert refused.status_code == 400 and refused.json()["code"] == "consent_required"
    done = submit(farmer, app["id"])
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "submitted" and body["statementReportId"] and body["assessment"]["outcome"]
    assert body["consent"]["dataItems"] == notice()["dataItems"] and not body["canEdit"]
    assert farmer.patch(url, json={"letter": LETTER + " Edited."}).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LoanDataSharingConsent)) == 1
        assert db.scalar(select(func.count()).select_from(Notification).where(Notification.title == "Application submitted")) == 1
    pack = farmer.get(f"{url}/pack")
    assert pack.status_code == 200 and pack.content.startswith(b"%PDF")


def test_a_confirmed_lender_condition_that_fails_stops_submission_but_short_history_does_not():
    boss = super_admin()
    strict = [*REQUIREMENTS, {"key": "income", "name": "Income", "category": "financial", "required": True,
                              "fieldType": "none", "check": {"metric": "period_income", "op": "gte", "value": 999999}, **DATES}]
    _, product = setup(boss, strict)
    farmer = ready_farmer(boss)
    app = prepared(farmer, product["id"])
    r = submit(farmer, app["id"])
    assert r.status_code == 400 and r.json()["code"] == "requirement_not_met"
    boss2_lender_requirements = [*REQUIREMENTS[:1], {**REQUIREMENTS[1], "check": {"metric": "months_of_records", "op": "gte", "value": 12}},
                                 *REQUIREMENTS[2:]]
    _, lenient = setup(boss, boss2_lender_requirements, {"name": "Second Test Lender"})
    app2 = prepared(farmer, lenient["id"])
    ok = submit(farmer, app2["id"])
    assert ok.status_code == 200 and ok.json()["assessment"]["outcome"] == "additional_information"


def test_identity_is_required_before_submitting():
    boss = super_admin()
    _, product = setup(boss)
    farmer = ready_farmer(boss, card=False)
    app = prepared(farmer, product["id"])
    r = submit(farmer, app["id"])
    assert r.status_code == 403 and r.json()["code"] == "identity_required"


def test_a_changed_product_must_be_reviewed_before_submitting():
    boss = super_admin()
    _, product = setup(boss)
    farmer = ready_farmer(boss)
    app = prepared(farmer, product["id"])
    url = f"{API}/admin/loan-products/{product['id']}"
    boss.put(f"{url}/draft", json={**product_content(), "name": "Season Input Loan 2026"})
    boss.post(f"{url}/publish", json={"note": "Renamed"})
    r = submit(farmer, app["id"])
    assert r.status_code == 409 and r.json()["code"] == "product_updated"
    refreshed = farmer.get(f"{API}/me/applications/{app['id']}").json()
    assert refreshed["product"]["version"] == 2 and refreshed["product"]["name"] == "Season Input Loan 2026"
    assert submit(farmer, app["id"]).status_code == 200


def test_farmers_only_ever_see_their_own_applications():
    boss = super_admin()
    _, product = setup(boss)
    ama = ready_farmer(boss)
    app = prepared(ama, product["id"])
    kofi = signup("kofi@example.com")
    url = f"{API}/me/applications/{app['id']}"
    assert kofi.get(url).status_code == 404 and kofi.patch(url, json={"letter": LETTER}).status_code == 404
    assert kofi.get(f"{url}/pack").status_code == 404
    assert new_client().get(f"{API}/me/applications").status_code == 401
    assert kofi.get(f"{API}/me/applications").json() == []
    # Drafts stay private: AgriCtec sees an application only once it is submitted.
    assert boss.get(f"{API}/admin/applications").json() == []
    assert boss.get(f"{API}/admin/applications/{app['id']}").status_code == 404


# ---------- AgriCtec reviews and sends ----------


def test_review_tally_changes_send_and_lender_progress_with_proof():
    boss = super_admin()
    _, product = setup(boss)
    farmer = ready_farmer(boss)
    app = prepared(farmer, product["id"])
    submit(farmer, app["id"])
    admin = f"{API}/admin/applications/{app['id']}"

    listed = boss.get(f"{API}/admin/applications", params={"group": "to_review"}).json()
    assert [a["id"] for a in listed] == [app["id"]]
    detail = boss.get(admin).json()
    assert detail["tally"]["allMatch"] is True and detail["farmer"]["identityLevel"] == "reviewed"
    assert boss.get(f"{admin}/statement").status_code == 200
    doc_id = detail["requirements"][4]["documents"][0]["id"]
    assert boss.get(f"{admin}/documents/{doc_id}/file").content == PDF
    # The farmer's records changed after submitting: the tally says so.
    add_record(farmer, {"type": "expense", "category": "seed", "description": "Seed (test)",
                                          "crop": "Maize", "amount": 300, "date": date.today().isoformat()})
    assert boss.get(admin).json()["tally"]["allMatch"] is False

    # Read-only roles can look but not act.
    analyst = admin_with_role(boss, "analyst")
    assert analyst.get(admin).status_code == 200
    assert analyst.post(f"{admin}/review").status_code == 403

    assert boss.post(f"{admin}/review").json()["status"] == "in_review"
    assert boss.post(f"{admin}/request-changes", json={"note": ""}).status_code == 400
    returned = boss.post(f"{admin}/request-changes", json={"note": "Add your guarantor's details."}).json()
    assert returned["status"] == "changes_requested"
    mine = farmer.get(f"{API}/me/applications/{app['id']}").json()
    assert mine["canEdit"] and mine["reviewNote"] == "Add your guarantor's details."
    farmer.patch(f"{API}/me/applications/{app['id']}", json={"letter": LETTER + " I now have a guarantor."})
    assert submit(farmer, app["id"]).json()["status"] == "submitted"

    # Approval is AgriCtec's review passing, never a loan approval. Without an agreed contact
    # (or email), it waits for an administrator to deliver it and record how.
    approved = boss.post(f"{admin}/approve", json={"note": "Tallies with the records."}).json()
    assert approved["status"] == "approved_for_sending" and approved["sendResult"] == "manual"
    assert boss.post(f"{admin}/record-sent", json={"how": "x"}).status_code == 422
    sent = boss.post(f"{admin}/record-sent", json={"how": "Delivered by hand to the Ejisu branch manager"}).json()
    assert sent["status"] == "sent_to_lender" and sent["sentVia"] == "recorded"
    with SessionLocal() as db:
        note = db.scalars(select(Notification).where(Notification.title == "Sent to the lender")).one()
        assert "for further review" in note.body

    # The lender's progress needs the lender's own proof.
    missing = boss.post(f"{admin}/lender-status", data={"status": "received_by_lender", "note": "Branch confirmed receipt"})
    assert missing.status_code == 422
    received = boss.post(f"{admin}/lender-status", data={"status": "received_by_lender", "note": "Branch confirmed receipt"},
                         files={"proof": ("lender-email.pdf", PDF, "application/pdf")}).json()
    event = received["timeline"][-1]
    assert received["status"] == "received_by_lender" and event["hasProof"] and event["lenderConfirmed"]
    assert boss.get(f"{admin}/events/{event['eventId']}/proof").content == PDF
    final = boss.post(f"{admin}/lender-status", data={"status": "approved_by_lender", "note": "Approval letter received"},
                      files={"proof": ("letter.pdf", PDF, "application/pdf")}).json()
    assert final["status"] == "approved_by_lender"
    # The farmer sees AgriCtec (and the lender's confirmation), never a staff member's name.
    seen = farmer.get(f"{API}/me/applications/{app['id']}").json()["timeline"]
    assert {e["by"] for e in seen} <= {"You", "AgriCtec", "AgriCtec, confirmed by the lender"}
    assert seen[-1]["by"] == "AgriCtec, confirmed by the lender"
    assert "AgriCtec (" in final["timeline"][-1]["by"]
    assert farmer.post(f"{API}/me/applications/{app['id']}/withdraw", json={}).status_code == 409
    with SessionLocal() as db:
        actions = set(db.scalars(select(AuditEntry.action).where(AuditEntry.action.like("application.%"))))
    assert {"application.view", "application.document", "application.sent", "application.lender_status"} <= actions


def test_sending_by_email_to_the_contact_the_lender_agreed_to(monkeypatch):
    boss = super_admin()
    lender, product = setup(boss, lender_fields={"submissionEmail": "agri-loans@lender.example",
                                                  "submissionAgreedOn": date.today().isoformat()})
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@agrictec.test")
    sent = []
    monkeypatch.setattr(messaging, "send_email", lambda to, subject, text, attachments=None: sent.append((to, subject, attachments)))
    farmer = ready_farmer(boss)
    app = prepared(farmer, product["id"])
    submit(farmer, app["id"])
    done = boss.post(f"{API}/admin/applications/{app['id']}/approve", json={}).json()
    assert done["status"] == "sent_to_lender" and done["sentVia"] == "email" and done["sendResult"] == "sent"
    # (The farmer's own notification emails go out too; this is the one to the lender.)
    to, subject, files = next(mail for mail in sent if mail[0] == "agri-loans@lender.example")
    assert app["reference"] in subject
    names = [f[0] for f in files]
    assert names[0].endswith("-application.pdf") and names[1].endswith("-transaction-statement.pdf") and "momo.pdf" in names


def test_a_lender_contact_needs_the_date_it_was_agreed():
    boss = super_admin()
    lender = published_lender(boss)
    r = boss.patch(f"{API}/admin/lenders/{lender['id']}", json={"submissionEmail": "loans@lender.example"})
    assert r.status_code == 422


def test_withdrawing_records_the_consent_withdrawal():
    boss = super_admin()
    _, product = setup(boss)
    farmer = ready_farmer(boss)
    app = prepared(farmer, product["id"])
    submit(farmer, app["id"])
    out = farmer.post(f"{API}/me/applications/{app['id']}/withdraw", json={"note": "Found other funding"}).json()
    assert out["status"] == "withdrawn" and out["consent"]["withdrawnAt"]
    # A new application to the same product is allowed after withdrawing.
    again = farmer.post(f"{API}/me/applications", json={"productId": product["id"], "amount": "3000", "months": 6,
                                                        "purpose": "Inputs"})
    assert again.status_code == 201

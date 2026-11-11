"""Real lender management: only published, verified, authorised lenders reach farmers."""

import io
import os
from datetime import date, timedelta

from PIL import Image
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.domain.clock import now_utc
from app.models import Lender, StoredFile
from app.services.backup import _upgrade_lenders
from tests.conftest import (
    API,
    HERE,
    ONBOARDING,
    TEST_LENDER,
    admin_with_role,
    published_lender,
    recorded_application,
    signup,
    super_admin,
)


def listed_ids(client) -> list[str]:
    return [lender["id"] for lender in client.get(f"{API}/platform/lenders").json()]


def test_no_lender_exists_until_an_admin_adds_one(client):
    assert client.get(f"{API}/platform/lenders").json() == []
    assert client.get(f"{API}/platform").json()["lenders"] == []
    assert "allLenders" not in client.get(f"{API}/platform").json()


def test_a_lender_is_listed_only_after_every_check(client):
    boss = super_admin()
    draft = boss.post(f"{API}/admin/lenders", json=TEST_LENDER).json()
    assert draft["publicationStatus"] == "draft" and draft["licenceStatus"] == "unverified"
    assert draft["listed"] is False and len(draft["blockers"]) == 4
    assert listed_ids(client) == []
    assert client.get(f"{API}/platform/lenders/{draft['id']}").status_code == 404

    r = boss.post(f"{API}/admin/lenders/{draft['id']}/publish")
    assert r.status_code == 400 and r.json()["code"] == "not_publishable"

    # A verified licence needs its official source.
    r = boss.post(f"{API}/admin/lenders/{draft['id']}/verification",
                  json={"licenceStatus": "verified", "checkedOn": date.today().isoformat()})
    assert r.status_code == 422
    future = (date.today() + timedelta(days=2)).isoformat()
    r = boss.post(f"{API}/admin/lenders/{draft['id']}/verification",
                  json={"licenceStatus": "verified", "source": "Register", "checkedOn": future})
    assert r.status_code == 400
    boss.post(f"{API}/admin/lenders/{draft['id']}/verification",
              json={"licenceStatus": "verified", "source": "Register", "checkedOn": date.today().isoformat()})
    r = boss.post(f"{API}/admin/lenders/{draft['id']}/publish")
    assert r.status_code == 400 and "listing" in r.json()["detail"]
    # Authorising the listing needs a record of how.
    assert boss.post(f"{API}/admin/lenders/{draft['id']}/listing", json={"authorised": True}).status_code == 422
    boss.post(f"{API}/admin/lenders/{draft['id']}/listing", json={"authorised": True, "note": "Letter, 3 Nov"})
    published = boss.post(f"{API}/admin/lenders/{draft['id']}/publish").json()
    assert published["listed"] is True and published["blockers"] == []

    assert listed_ids(client) == [draft["id"]]
    shown = client.get(f"{API}/platform/lenders/{draft['id']}").json()
    assert shown["verificationSource"] == "Register" and "listingNote" not in shown
    assert client.get(f"{API}/platform").json()["lenders"][0]["id"] == draft["id"]


def test_changing_who_the_institution_is_clears_the_check(client):
    boss = super_admin()
    lender = published_lender(boss)
    # Contact details can change without a new check.
    r = boss.patch(f"{API}/admin/lenders/{lender['id']}", json={"website": "https://example.org"})
    assert r.json()["listed"] is True and listed_ids(client) == [lender["id"]]
    # A different licence number is a different check.
    r = boss.patch(f"{API}/admin/lenders/{lender['id']}", json={"licenceRef": "TEST-0002"}).json()
    assert r["licenceStatus"] == "unverified" and r["publicationStatus"] == "unpublished"
    assert listed_ids(client) == []


def test_suspension_hides_a_lender_at_once_even_with_applications():
    boss = super_admin()
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    user_id = farmer.get(f"{API}/auth/session").json()["userId"]
    lender = published_lender(boss)
    recorded_application(user_id, lender["id"], status="under_review")

    assert boss.post(f"{API}/admin/lenders/{lender['id']}/suspend", json={"reason": "x"}).status_code == 422
    r = boss.post(f"{API}/admin/lenders/{lender['id']}/suspend", json={"reason": "Licence under question"})
    assert r.status_code == 200 and r.json()["publicationStatus"] == "suspended"
    assert listed_ids(farmer) == []
    assert boss.post(f"{API}/admin/lenders/{lender['id']}/publish").status_code == 400
    # The farmer's application still names its lender.
    assert farmer.get(f"{API}/me/loans/applications").json()[0]["partnerName"] == TEST_LENDER["name"]
    # A lender with applications on record can't be deleted.
    r = boss.delete(f"{API}/admin/lenders/{lender['id']}")
    assert r.status_code == 400 and "history" in r.json()["detail"]
    reinstated = boss.post(f"{API}/admin/lenders/{lender['id']}/reinstate").json()
    assert reinstated["publicationStatus"] == "unpublished" and reinstated["applications"] == 1


def test_an_old_licence_check_takes_the_lender_off_the_marketplace(client):
    boss = super_admin()
    lender = published_lender(boss)
    with SessionLocal() as db:
        row = db.get(Lender, lender["id"])
        row.verified_at = now_utc() - timedelta(days=366)
        db.commit()
    assert listed_ids(client) == []
    view = boss.get(f"{API}/admin/lenders/{lender['id']}").json()
    assert view["publicationStatus"] == "published" and view["listed"] is False
    assert view["reviewDue"] is True and "more than 365 days" in view["blockers"][0]


def test_only_lender_managers_change_lenders():
    boss = super_admin()
    support = admin_with_role(boss, "support")
    assert support.post(f"{API}/admin/lenders", json=TEST_LENDER).status_code == 403
    assert support.get(f"{API}/admin/lenders").status_code == 403
    admin = admin_with_role(boss, "admin")
    assert admin.post(f"{API}/admin/lenders", json=TEST_LENDER).status_code == 201


def test_every_lender_step_is_audited():
    boss = super_admin()
    lender = published_lender(boss)
    boss.post(f"{API}/admin/lenders/{lender['id']}/suspend", json={"reason": "Checking the licence"})
    with SessionLocal() as db:
        actions = db.execute(
            text("select action from audit_log where target_id = :id order by seq"), {"id": lender["id"]}
        ).scalars().all()
    assert actions == ["lender.create", "lender.verify", "lender.listing", "lender.publish", "lender.suspend"]


def test_names_and_contact_details_are_checked():
    boss = super_admin()
    boss.post(f"{API}/admin/lenders", json=TEST_LENDER)
    assert boss.post(f"{API}/admin/lenders", json={**TEST_LENDER, "name": " test lender  (AUTOMATED tests) "}).status_code == 409
    for bad in ({"website": "javascript:alert(1)"}, {"email": "no-at-sign"}, {"region": "Atlantis"},
                {"institutionType": "bank"}):
        assert boss.post(f"{API}/admin/lenders", json={**TEST_LENDER, "name": "Other", **bad}).status_code == 422


def png(size=(800, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (16, 120, 80)).save(out, format="PNG")
    return out.getvalue()


def test_logos_use_database_without_bucket_credentials(client):
    boss = super_admin()
    lender = published_lender(boss)
    endpoint = f"{API}/admin/lenders/{lender['id']}/logo"
    assert boss.put(endpoint, files={"file": ("logo.png", b"not an image", "image/png")}).status_code == 400
    assert boss.put(endpoint, files={"file": ("logo.png", png(), "image/png")}).status_code == 200
    # A fresh database session sees the actual committed file, without a mock store.
    with SessionLocal() as db:
        key = db.get(Lender, lender["id"]).logo_key
        stored = db.get(StoredFile, key)
        assert stored.content_type == "image/webp" and stored.data


def test_logo_upload_and_who_can_see_it(client):
    boss = super_admin()
    lender = published_lender(boss)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = boss.put(f"{API}/admin/lenders/{lender['id']}/logo", files={"file": ("logo.svg", svg, "image/svg+xml")})
    assert r.status_code == 400
    r = boss.put(f"{API}/admin/lenders/{lender['id']}/logo", files={"file": ("logo.png", png(), "image/png")})
    assert r.status_code == 200
    path = client.get(f"{API}/platform/lenders").json()[0]["logoPath"]
    assert path.startswith(f"/platform/lenders/{lender['id']}/logo?v=")
    logo = client.get(f"{API}{path}")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/webp"
    assert max(Image.open(io.BytesIO(logo.content)).size) == 512
    # Unlisted lenders' logos are for the console only.
    boss.post(f"{API}/admin/lenders/{lender['id']}/unpublish")
    assert client.get(f"{API}{path}").status_code == 404
    assert boss.get(f"{API}/admin/lenders/{lender['id']}/logo").status_code == 200
    boss.delete(f"{API}/admin/lenders/{lender['id']}/logo")
    with SessionLocal() as db:
        assert db.query(StoredFile).count() == 0


def test_farmers_cannot_apply_until_a_lender_publishes_a_product():
    boss = super_admin()
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json={**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-123456789-0"}})
    request = boss.get(f"{API}/admin/verifications", params={"status": "pending", "kind": "ghanaCard"}).json()[0]
    boss.post(f"{API}/admin/verifications/{request['id']}/approve")
    draft = boss.post(f"{API}/admin/lenders", json=TEST_LENDER).json()
    loan = {"partnerId": draft["id"], "amount": 1000, "purpose": "Farm Equipment", "months": 12}
    r = farmer.post(f"{API}/me/loans/applications", json=loan)
    assert r.status_code == 400 and r.json()["code"] == "lender_unavailable"
    assert farmer.post(f"{API}/me/loans/eligibility", json={"amount": 1000, "partnerId": draft["id"]}).status_code == 404
    listed = published_lender(boss, name="Second Test Lender")
    r = farmer.post(f"{API}/me/loans/applications", json={**loan, "partnerId": listed["id"]})
    assert r.status_code == 400 and r.json()["code"] == "no_loan_products"
    assert farmer.get(f"{API}/me/loans/applications").json() == []


def test_restoring_an_old_backup_never_brings_made_up_terms_back():
    old = [
        {"id": "sinapi", "name": "Sinapi Aba Trust", "rate": 18, "max_amount": 100000, "min_months": 6,
         "max_months": 24, "min_score": 550, "color": "#10b981", "active": True, "description": None,
         "created_at": "2026-01-01T00:00:00.000Z"},
        {"id": "rural", "name": "Rural Bank Consortium", "rate": 20, "max_amount": 150000, "min_months": 6,
         "max_months": 24, "min_score": 580, "color": "#f5c451", "active": True, "description": None,
         "created_at": "2026-01-01T00:00:00.000Z"},
        {"id": "real-bank", "name": "Real Bank", "rate": 21, "max_amount": 9000, "min_months": 6,
         "max_months": 12, "min_score": 500, "color": "#000000", "active": True, "description": None,
         "created_at": "2026-03-01T00:00:00.000Z"},
    ]
    upgraded = _upgrade_lenders(old, [{"partner_id": "rural"}])
    assert [row["id"] for row in upgraded] == ["rural", "real-bank"]
    assert all("rate" not in row and row["licence_status"] == "unverified" for row in upgraded)
    assert upgraded[0]["publication_status"] == "suspended"
    assert upgraded[1]["publication_status"] == "unpublished"


def test_migration_removes_the_development_sample_lenders():
    """0006 -> 0007 on data shaped like production: unused samples go, a used one stays hidden."""
    from alembic import command
    from alembic.config import Config

    config = Config(os.path.join(HERE, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(HERE, "alembic"))
    command.downgrade(config, "0006")
    try:
        with engine.begin() as conn:
            for lender_id, name in (("sinapi", "Sinapi Aba Trust"), ("rural", "Rural Bank Consortium"),
                                    ("my-bank", "My Bank")):
                conn.execute(text(
                    "insert into lenders (id, name, rate, max_amount, min_months, max_months, min_score, color, "
                    "active, created_at) values (:id, :name, 18, 1000, 6, 12, 500, '#000000', true, now())"
                ), {"id": lender_id, "name": name})
            conn.execute(text(
                "insert into loan_applications (id, reference, user_id, partner_id, amount, months, purpose, "
                "rate, monthly, score_at_application, status, created_at) values ('app1', 'APP-2026-55555', "
                "'u1', 'rural', 1000, 12, 'Seeds', 18, 91.68, 600, 'submitted', now())"
            ))
    finally:
        command.upgrade(config, "head")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "select id, publication_status, licence_status from lenders order by id"
        )).all()
    assert [tuple(r) for r in rows] == [
        ("my-bank", "unpublished", "unverified"),
        ("rural", "suspended", "unverified"),
    ]

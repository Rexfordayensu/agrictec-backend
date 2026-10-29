"""The farmer's own data, onboarding, verifications, loans and AI tools."""

from tests.conftest import API, ONBOARDING, new_client, signup, super_admin

RECORD = {"type": "income", "description": "Maize sales", "crop": "Maize", "amount": 1200, "date": "2026-10-01"}


def test_records_are_private_to_their_farmer():
    ama = signup("ama@example.com")
    kofi = signup("kofi@example.com")
    created = ama.post(f"{API}/me/records", json=RECORD).json()
    assert [r["id"] for r in ama.get(f"{API}/me/records").json()] == [created["id"]]
    assert kofi.get(f"{API}/me/records").json() == []
    assert kofi.patch(f"{API}/me/records/{created['id']}", json={"amount": 1}).status_code == 404
    assert kofi.delete(f"{API}/me/records/{created['id']}").status_code == 404
    assert ama.patch(f"{API}/me/records/{created['id']}", json={"amount": 1500}).json()["amount"] == 1500
    # An activity carries no amount.
    activity = ama.patch(f"{API}/me/records/{created['id']}", json={"type": "activity"}).json()
    assert activity["amount"] == 0
    assert ama.delete(f"{API}/me/records/{created['id']}").status_code == 204


def test_momo_reference_is_generated_and_diagnoses_keep_ten():
    c = signup()
    t = c.post(f"{API}/me/momo", json={"direction": "in", "counterparty": "Buyer", "amount": 50, "date": "2026-10-02"}).json()
    assert t["reference"].startswith("MOMO") and len(t["reference"]) == 10
    for i in range(12):
        c.post(
            f"{API}/me/diagnoses",
            json={"disease": "Healthy", "severity": "None", "confidence": 90, "treatment": "None needed",
                  "date": f"2026-10-{i + 1:02d}T10:00:00.000Z"},
        )
    history = c.get(f"{API}/me/diagnoses").json()
    assert len(history) == 10 and history[0]["date"].startswith("2026-10-12")


def test_onboarding_with_a_ghana_card_queues_it_for_review():
    c = signup()
    body = {**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-123456789-0"}}
    profile = c.post(f"{API}/me/onboarding/complete", json=body).json()
    assert profile["onboardingCompletedAt"]
    assert profile["verification"]["location"] is True
    assert profile["ghanaCard"]["status"] == "pending"
    assert profile["ghanaCard"]["masked"] == "GHA-******789-0"
    assert c.cookies.get("agrictec_onboarded") == "1"
    pending = c.get(f"{API}/me/verifications").json()
    assert pending[0]["status"] == "pending" and "123456" not in str(pending)
    again = c.post(f"{API}/me/verifications", json={"kind": "ghanaCard", "value": "GHA-123456789-0"})
    assert again.status_code == 409


def test_onboarding_checks_the_whole_profile():
    c = signup()
    bad = {"profile": {**ONBOARDING["profile"], "constituency": "Tamale North"}}
    r = c.post(f"{API}/me/onboarding/complete", json=bad)
    assert r.status_code == 422 and "constituency" in r.json()["detail"]


def test_the_farmer_cannot_verify_themselves():
    c = signup()
    c.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    sneaky = {**ONBOARDING["profile"], "ghanaCard": {"status": "verified"}, "verification": {"ghanaCard": True}}
    profile = c.put(f"{API}/me/profile", json=sneaky).json()
    assert profile["ghanaCard"]["status"] == "not_provided"
    assert profile["verification"]["ghanaCard"] is False


def test_phone_check_passes_at_once_and_a_new_number_loses_it():
    c = signup()
    c.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    check = c.post(f"{API}/me/verifications", json={"kind": "phone", "value": "0241234567"}).json()
    assert check["status"] == "approved" and check["reviewedBy"] == "Automatic check"
    assert c.get(f"{API}/me/profile").json()["verification"]["phone"] is True
    profile = c.put(f"{API}/me/profile", json={**ONBOARDING["profile"], "phone": "0559876543"}).json()
    assert profile["verification"]["phone"] is False


def test_loans_need_a_verified_card_and_every_rule():
    c = signup()
    c.post(f"{API}/me/onboarding/complete", json={**ONBOARDING, "ghanaCard": {"cardNumber": "GHA-123456789-0"}})
    loan = {"partnerId": "sinapi", "amount": 1000, "purpose": "Farm Equipment", "months": 12}
    r = c.post(f"{API}/me/loans/applications", json=loan)
    assert r.status_code == 403 and "being reviewed" in r.json()["detail"]

    boss = super_admin()
    request = boss.get(f"{API}/admin/verifications", params={"status": "pending"}).json()[0]
    assert boss.post(f"{API}/admin/verifications/{request['id']}/approve").status_code == 200
    assert c.get(f"{API}/me/profile").json()["ghanaCard"]["status"] == "verified"

    r = c.post(f"{API}/me/loans/applications", json=loan)
    assert r.status_code == 400 and "farm records" in r.json()["detail"]
    eligibility = c.post(f"{API}/me/loans/eligibility", json={"amount": 1000, "partnerId": "sinapi"}).json()
    assert not eligibility["eligible"] and {i["code"] for i in eligibility["issues"]} >= {"records", "score"}


def test_ai_tools_follow_the_plan_and_spend_credits():
    c = signup()
    r = c.post(f"{API}/me/assistant", json={"message": "hello"})
    assert r.status_code == 403 and r.json()["code"] == "plan_required"
    assert c.post(f"{API}/me/subscription", json={"planId": "premium", "method": "card"}).status_code == 200
    answer = c.post(f"{API}/me/assistant", json={"message": "How do I improve my credit score?"}).json()
    assert answer["reply"]["title"] == "Improve Your Credit Score"
    assert answer["creditsRemaining"] == 99
    scan = c.post(f"{API}/me/crop-doctor", files={"image": ("leaf.jpg", b"\xff\xd8" + b"0" * 2048, "image/jpeg")}, data={"crop": "Maize"})
    assert scan.status_code == 200 and scan.json()["creditsRemaining"] == 94
    assert scan.json()["result"]["crop"] == "Maize"
    wrong_type = c.post(f"{API}/me/crop-doctor", files={"image": ("notes.txt", b"hello", "text/plain")})
    assert wrong_type.status_code == 400
    assert c.get(f"{API}/me/subscription").json()["creditsUsed"] == 6


def test_export_import_and_delete_my_data():
    c = signup()
    c.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    c.post(f"{API}/me/records", json=RECORD)
    exported = c.get(f"{API}/me/export").json()
    assert len(exported["records"]) == 1

    assert c.post(f"{API}/me/delete-data", json={"confirmation": "nope"}).status_code == 400
    assert c.post(f"{API}/me/delete-data", json={"confirmation": "DELETE"}).status_code == 200
    assert c.get(f"{API}/me/records").json() == []
    assert c.get(f"{API}/me/profile").json()["onboardingCompletedAt"] is None

    body = {
        "version": 1,
        "profile": exported["profile"],
        "records": [{k: r[k] for k in ("type", "description", "crop", "amount", "date")} for r in exported["records"]],
        "momo": [],
        "settings": exported["settings"],
    }
    assert c.post(f"{API}/me/import", json=body).status_code == 200
    assert len(c.get(f"{API}/me/records").json()) == 1
    assert c.get(f"{API}/me/profile").json()["farmName"] == "Mensah Maize Farm"


def test_announcements_reach_the_right_farmers():
    boss = super_admin()
    created = boss.post(
        f"{API}/admin/announcements",
        json={"title": "Dry spell", "message": "Mulch now.", "type": "warning",
              "audience": {"kind": "region", "regions": ["Ashanti"]},
              "startsAt": "2026-01-01T00:00:00.000Z", "active": True, "dismissible": True},
    ).json()
    ashanti = signup("ashanti@example.com")
    ashanti.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    north = signup("north@example.com")
    north.post(f"{API}/me/onboarding/complete", json={"profile": {**ONBOARDING["profile"], "region": "Northern", "constituency": "Tamale North", "gps": None}})
    assert [a["id"] for a in ashanti.get(f"{API}/me/announcements").json()] == [created["id"]]
    assert north.get(f"{API}/me/announcements").json() == []
    ashanti.post(f"{API}/me/announcements/{created['id']}/dismiss")
    assert ashanti.get(f"{API}/me/announcements").json() == []


def test_maintenance_mode_closes_the_farmer_app():
    c = signup()
    boss = super_admin()
    flags = new_client().get(f"{API}/platform/flags").json()
    boss.put(f"{API}/admin/settings/flags", json={**flags, "maintenanceMode": True, "maintenanceMessage": "Back at noon."})
    r = c.get(f"{API}/me/records")
    assert r.status_code == 503 and r.json()["detail"] == "Back at noon."
    assert new_client().get(f"{API}/platform").status_code == 200

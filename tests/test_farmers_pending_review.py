"""The farmers list's "Pending verification" figure counts farmers whose checks wait for an
admin's review (their verification requests), not an account status, and filters to them."""

from tests.conftest import API, signup, super_admin

PAGE = f"{API}/admin/farmers/page"


def test_pending_review_counts_and_filters_waiting_verification_requests():
    boss = super_admin()
    waiting, other = signup("waiting.review@example.com"), signup("nothing.waiting@example.com")
    before = boss.get(PAGE).json()
    assert before["summary"]["pending_review"] == 0 and before["pendingChecks"] == {}

    card = waiting.post(f"{API}/me/verifications", json={"kind": "ghanaCard", "value": "GHA-222222222-2"})
    assert card.status_code == 201, card.text
    waiting_id = waiting.get(f"{API}/auth/session").json()["userId"]

    page = boss.get(PAGE).json()
    assert page["summary"]["pending_review"] == 1
    assert page["summary"]["all"] == 2
    assert page["pendingChecks"] == {waiting_id: ["ghanaCard"]}
    filtered = boss.get(PAGE, params={"verification": "pending-review"}).json()
    assert [f["userId"] for f in filtered["items"]] == [waiting_id] and filtered["total"] == 1

    # Reviewed: nothing waits any more, and the farmer leaves the filter.
    request = next(r for r in boss.get(f"{API}/admin/farmers/{waiting_id}/verifications").json() if r["status"] == "pending")
    assert boss.post(f"{API}/admin/verifications/{request['id']}/approve").status_code == 200
    after = boss.get(PAGE).json()
    assert after["summary"]["pending_review"] == 0 and after["pendingChecks"] == {}
    assert boss.get(PAGE, params={"verification": "pending-review"}).json()["total"] == 0
    assert other.get(f"{API}/me/verifications").status_code == 200


def test_unknown_verification_filters_are_refused():
    boss = super_admin()
    assert boss.get(PAGE, params={"verification": "nia-verified"}).status_code == 422

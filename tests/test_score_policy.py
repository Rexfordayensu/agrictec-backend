"""The score is feedback; it cannot grant or block loan access."""

from app.models import Profile
from app.services import account, lending
from tests.conftest import API, ONBOARDING, signup, add_record


def test_low_and_high_scores_leave_the_same_history_and_loan_checks_unchanged(db, monkeypatch):
    client = signup()
    assert client.post(f"{API}/me/onboarding/complete", json=ONBOARDING).status_code == 200
    user_id = client.get(f"{API}/auth/session").json()["userId"]
    assert add_record(client, {
        "type": "income", "description": "Farm sale", "crop": "Maize", "amount": "30.10", "date": "2026-10-01",
    }).status_code == 201
    profile = db.get(Profile, user_id)
    profile.ghana_card_status = "verified"
    db.commit()
    original = account.calculate_credit_score
    gates, histories = [], []

    def score_must_not_be_checked(*args, **kwargs):
        raise AssertionError("The loan gate must not calculate or inspect a score")

    monkeypatch.setattr(lending, "calculate_credit_score", score_must_not_be_checked)
    for score in (300, 850):
        def informational_score(*args, **kwargs):
            return original(*args, **kwargs).model_copy(update={"score": score})

        monkeypatch.setattr(account, "calculate_credit_score", informational_score)
        shown = client.get(f"{API}/me/credit").json()
        assert shown["credit"]["score"] == score
        gates.append(client.post(f"{API}/me/loans/eligibility", json={"amount": 1000}).json())
        histories.append(client.get(f"{API}/me/transactions").json())
    assert gates[0] == gates[1]
    assert gates[0]["eligible"] is True and gates[0]["issues"] == []
    assert gates[0]["maxAffordable"] is None  # an identity check is not a loan offer
    assert histories[0] == histories[1]
    assert histories[0]["items"][0]["amount"] == "30.10"

    # Even the highest score cannot bypass identity requirements.
    profile.ghana_card_status = "not_provided"
    db.commit()
    blocked = client.post(f"{API}/me/loans/eligibility", json={"amount": 1000}).json()
    assert blocked["eligible"] is False
    assert [issue["code"] for issue in blocked["issues"]] == ["ghana-card"]

"""Entries count towards the score by what AgriCtec checked: in full once verified from the
receipt, at the rules' `unverifiedWeight` (50% by default) while nobody has checked them, and not
at all once rejected or disputed. Typed-in sales that never happened can't buy a good score."""

from datetime import UTC, date, datetime

from app.domain.credit import calculate_credit_score, default_config, entry_weight, score_gaps
from app.schemas.farm import FarmRecord, MomoTransaction, Profile
from app.schemas.platform import CreditConfigInput
from tests.conftest import API, add_momo, add_record, new_client, signup, super_admin

NOW = date(2026, 10, 15)


def rec(kind="income", amount=1000.0, status="self_reported", on=NOW, rid="r") -> FarmRecord:
    return FarmRecord(
        id=rid, user_id="u", type=kind, description="x", crop="Maize", amount=amount, date=on,
        created_at=datetime(on.year, on.month, on.day, 12, tzinfo=UTC), verification_status=status,
    )


def momo(status="self_reported", rid="m") -> MomoTransaction:
    return MomoTransaction(
        id=rid, user_id="u", direction="in", counterparty="Buyer", amount=100, date=NOW,
        created_at=datetime(2026, 10, 15, 12, tzinfo=UTC), verification_status=status,
    )


def config(unverified: int) -> CreditConfigInput:
    values = default_config().values()
    values["targets"] = {**values["targets"], "unverifiedWeight": unverified}
    return CreditConfigInput.model_validate(values)


def score(records, momo_entries=(), cfg=None):
    return calculate_credit_score(list(records), list(momo_entries), Profile(user_id="u"), cfg, now=NOW)


def test_each_status_has_its_weight():
    assert entry_weight(rec(status="verified"), 50) == 1
    for status in ("self_reported", "evidence_submitted", "verification_pending"):
        assert entry_weight(rec(status=status), 50) == 0.5
    for status in ("rejected", "disputed"):
        assert entry_weight(rec(status=status), 50) == 0


def test_verified_entries_count_twice_as_much_as_unchecked_ones_and_rejected_ones_nothing():
    ten = lambda status: [rec(status=status, rid=f"r{i}") for i in range(10)]  # noqa: E731
    verified, unchecked, rejected = score(ten("verified")), score(ten("self_reported")), score(ten("rejected"))
    # 10 entries against the target of 30.
    assert verified.record_completeness == 33
    assert unchecked.record_completeness == 17
    assert rejected.record_completeness == 0
    assert verified.score > unchecked.score > rejected.score
    # This month has an entry: full when verified, half when unchecked (6-month window).
    assert verified.payment_history == 17 and unchecked.payment_history == 8 and rejected.payment_history == 0
    # MoMo activity works the same way (target 20).
    assert score([], [momo("verified", f"m{i}") for i in range(4)]).momo_activity == 20
    assert score([], [momo("verification_pending", f"m{i}") for i in range(4)]).momo_activity == 10


def test_made_up_income_cant_beat_real_costs():
    costs = rec("expense", 6000, status="verified", rid="e")
    # Real sales of 10,000: a 40% margin.
    assert score([rec("income", 10000, status="verified"), costs]).income_stability == 90
    # The same sales, typed in with nothing checked: they count for 5,000, below the costs.
    assert score([rec("income", 10000), costs]).income_stability == 30
    # A rejected sale counts for nothing at all.
    assert score([rec("income", 10000, status="rejected"), costs]).income_stability == 0
    # Costs count in full even unchecked (nobody inflates their own costs), but not once rejected.
    assert score([rec("income", 10000, status="verified"), rec("expense", 6000, rid="e")]).income_stability == 90
    assert score([rec("income", 10000, status="verified"),
                  rec("expense", 6000, status="rejected", rid="e")]).income_stability == 100


def test_the_weight_is_an_admin_setting():
    entries = [rec(rid=f"r{i}") for i in range(10)]
    assert score(entries, cfg=config(100)).record_completeness == 33  # unchecked counts in full
    assert score(entries, cfg=config(0)).record_completeness == 0     # only verified counts
    # Rules saved before the setting existed read as 50%.
    old = default_config().values()
    old["targets"] = {k: v for k, v in old["targets"].items() if k != "unverifiedWeight"}
    assert CreditConfigInput.model_validate(old).targets.unverified_weight == 50
    bad = default_config().values()
    bad["targets"] = {**bad["targets"], "unverifiedWeight": 101}
    try:
        CreditConfigInput.model_validate(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("a weight above 100% must be refused")


def test_the_farmer_is_told_why():
    records = [rec(status="verified", rid="v"), rec(rid="u1"), rec(rid="u2"), rec(status="rejected", rid="x")]
    gaps = score_gaps(records, [], Profile(user_id="u"), default_config(),
                      verified_entries=1, entries_with_evidence=1, now=NOW)
    assert "2 entries are not verified yet, so they count for 50% of the score until AgriCtec checks the receipt." in gaps
    assert "1 entry was rejected or is disputed, so it doesn't count towards the score." in gaps


def test_verifying_a_receipt_raises_the_live_score_and_the_admin_setting_applies_everywhere():
    farmer = signup()
    boss = super_admin()
    today = date.today().isoformat()
    for i in range(3):
        assert add_record(farmer, {"type": "income", "description": f"Maize sale {i}", "crop": "Maize",
                                   "amount": str(500 + i * 75), "date": today}).status_code == 201
    row = add_momo(farmer, {"direction": "in", "counterparty": "Buyer", "amount": "200.00", "date": today,
                            "network": "MTN", "reference": "MP261014.0900.Z00001"}).json()
    before = farmer.get(f"{API}/me/credit").json()
    assert any("not verified yet" in gap for gap in before["missing"])
    assert "unchecked ones 50%" in next(f for f in before["factors"] if f["key"] == "record_completeness")["measures"]

    assert boss.post(f"{API}/admin/transactions/momo/{row['id']}/verify",
                     json={"reason": "Receipt matches"}).status_code == 200
    after = farmer.get(f"{API}/me/credit").json()
    assert after["credit"]["momoActivity"] > before["credit"]["momoActivity"]
    assert after["credit"]["score"] > before["credit"]["score"]

    # The admin's setting: the draft preview shows its effect, and once published unchecked
    # entries count in full for the farmer.
    live = new_client().get(f"{API}/platform").json()["creditConfig"]
    assert live["targets"]["unverifiedWeight"] == 50
    draft = {k: live[k] for k in ("weights", "targets", "bands", "eligibility")}
    draft["targets"] = {**draft["targets"], "unverifiedWeight": 100}
    impact = boss.post(f"{API}/admin/credit-config/impact", json={"draft": draft}).json()
    assert impact["changedScores"] == 1 and impact["farmers"][0]["delta"] > 0
    published = boss.post(f"{API}/admin/credit-config/publish",
                          json={"config": draft, "note": "Count unchecked entries in full"})
    assert published.status_code == 200 and published.json()["targets"]["unverifiedWeight"] == 100
    full = farmer.get(f"{API}/me/credit").json()
    assert full["credit"]["score"] > after["credit"]["score"]
    assert not any("not verified yet" in gap for gap in full["missing"])

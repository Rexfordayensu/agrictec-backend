"""The pure business rules, ported from the frontend's lib/ folder."""

from datetime import date, timedelta

import pytest

from app.domain.announcements import state_of
from app.domain.clock import now_utc, today
from app.domain.credit import calculate_credit_score, default_config, score_label
from app.domain.csv import to_csv
from app.domain.loans import check_eligibility, monthly_repayment, status_change_error
from app.domain.permissions import ROLE_PERMISSIONS, can, can_manage_role
from app.domain.risk_rules import RiskSubject, detect_risks
from app.domain.verification import hash_ghana_card, mask_full_ghana_card, rejection_message
from app.schemas.farm import FarmRecord, LoanApplication, LoanHistoryEntry, Profile
from app.schemas.platform import AnnouncementInput, CreditConfigInput
from app.services.seed import Random, build_world


def record(
    kind: str, amount: float, days_ago: int = 0, rid: str = "r", on: date | None = None
) -> FarmRecord:
    return FarmRecord(
        id=rid, user_id="u", type=kind, description="x", crop="Maize", amount=amount,
        date=on or today() - timedelta(days=days_ago), created_at=now_utc(),
    )


def test_empty_farmer_scores_the_floor():
    score = calculate_credit_score([], [], Profile(user_id="u"))
    assert score.score == 300
    assert score_label(score.score) == "Building"


def test_score_rewards_records_profit_and_verification():
    # One income per calendar month for the six-month window ending on a fixed day.
    now = date(2026, 10, 15)
    records = [record("income", 5000, rid=f"i{m}", on=date(2026, m, 10)) for m in range(5, 11)]
    records.append(record("expense", 1000, rid="e", on=date(2026, 10, 12)))
    profile = Profile(
        user_id="u", name="Ama", phone="0241234567", farm_name="Farm", acres=3,
        verification={"phone": True}, ghana_card={"status": "verified"}, gps={"lat": 6.7, "lng": -1.6},
    )
    score = calculate_credit_score(records, [], profile, now=now)
    assert score.payment_history == 100
    assert score.verification == 100
    assert score.income_stability == 100
    assert 600 < score.score <= 850


def test_weights_must_add_up_to_100():
    values = {**default_config().values()}
    values["weights"] = {**values["weights"], "verification": 50}
    with pytest.raises(ValueError, match="add up to 100"):
        CreditConfigInput.model_validate(values)


def test_amortized_repayment():
    result = monthly_repayment(10_000, 18, 12)
    assert result.monthly == 916.80
    assert result.total > 10_000
    assert monthly_repayment(1200, 0, 12).monthly == 100


def test_eligibility_lists_every_failing_rule():
    rules = default_config().eligibility
    result = check_eligibility(amount=50_000, records=[record("income", 100)], score=400, rules=rules, ghana_card="pending")
    codes = [i.code for i in result.issues]
    assert codes == ["ghana-card", "records", "affordability", "score"]
    assert not result.eligible
    assert "being reviewed" in result.reasons[0]


def test_loan_transitions_and_required_details():
    now = now_utc()
    app = LoanApplication(
        id="a", reference="APP-2026-10000", user_id="u", partner_id="sinapi", amount=1000, months=12,
        purpose="Seeds", rate=18, monthly=91.68, score_at_application=600, status="submitted",
        history=[LoanHistoryEntry(status="submitted", by="Ama", at=now)], created_at=now,
    )
    common = {"note": None, "disbursed_on": None, "disbursement_ref": None, "now": now, "farmer_deleted": False}
    assert "can’t move" in status_change_error(app, "disbursed", **common)
    assert status_change_error(app, "under_review", **common) is None
    assert "Add a note" in status_change_error(app, "rejected", **common)
    approved = app.model_copy(update={"status": "approved"})
    assert "disbursement date" in status_change_error(approved, "disbursed", **common)
    future = (now + timedelta(days=2)).date().isoformat()
    assert "future" in status_change_error(approved, "disbursed", **{**common, "disbursed_on": future, "disbursement_ref": "TX123"})
    assert "can only be closed" in status_change_error(app, "under_review", **{**common, "farmer_deleted": True})


def test_permission_matrix_matches_the_spec():
    assert set(ROLE_PERMISSIONS["analyst"]) == {"overview.view", "farmers.view", "loans.view", "reports.export"}
    assert not can("admin", "credit.configure")
    assert not can("admin", "farmers.delete")
    assert can("support", "verifications.review") and not can("support", "reports.export")
    assert not can("farmer", "overview.view") and not can(None, "overview.view") and not can("toString", "x")
    assert len(ROLE_PERMISSIONS["super_admin"]) == 19


def test_last_super_admin_is_protected():
    assert not can_manage_role("admin", "analyst")
    assert not can_manage_role("super_admin", "super_admin", next_role="admin", active_super_admins=1)
    assert can_manage_role("super_admin", "super_admin", next_role="admin", active_super_admins=2)


def test_ghana_card_is_never_kept_in_full():
    assert mask_full_ghana_card("GHA-123456789-0") == "GHA-******789-0"
    # Same fingerprint as the browser (lowercase input is normalized).
    assert hash_ghana_card("gha-123456789-0") == hash_ghana_card("GHA-123456789-0")
    assert len(hash_ghana_card("GHA-123456789-0")) == 64
    assert rejection_message("Unclear.", "Retake the photo.") == "Rejected: Unclear. Please resubmit. Retake the photo."


def test_csv_neutralizes_formulas():
    csv = to_csv([{"d": "=HYPERLINK(1)", "n": -500}], [("Desc", lambda r: r["d"]), ("Amount", lambda r: r["n"])])
    assert csv.startswith("﻿")
    assert "'=HYPERLINK(1)" in csv and ",-500" in csv


def test_announcement_life_cycle():
    now = now_utc()
    base = {"title": "T", "message": "M", "type": "info", "audience": {"kind": "all"}, "active": True, "dismissible": True}
    assert state_of(AnnouncementInput(**base, starts_at=now + timedelta(hours=1)), now) == "scheduled"
    assert state_of(AnnouncementInput(**base, starts_at=now - timedelta(hours=1)), now) == "live"
    ended = AnnouncementInput(**base, starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1))
    assert state_of(ended, now) == "ended"


def test_random_is_the_browsers_mulberry32():
    assert Random(1).next() == 0.6270739405881613


def test_seed_world_matches_the_spec_and_plants_every_risk():
    world = build_world()
    assert len(world.farmers) == 60
    assert {f.profile.region for f in world.farmers} >= {"Ashanti", "Upper West", "Oti"}
    assert sum(1 for v in world.verifications if v["status"] == "pending") == 12
    assert {a["status"] for a in world.applications} == {
        "submitted", "under_review", "approved", "rejected", "disbursed", "repaid", "defaulted"
    }
    rules = {r.rule for r in detect_risks([RiskSubject(f.user["id"], f.profile, f.records, f.momo) for f in world.farmers])}
    assert len(rules) == 7
    # Repeatable: the same day builds the same world.
    assert [f.profile.name for f in build_world().farmers] == [f.profile.name for f in world.farmers]

"""Milestone 4: exact financial summaries, the AgriCtec score explained, server-side reports."""

import ast
import hashlib
import pathlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.domain import financial_summary
from app.domain.clock import today
from app.services.backup import open_backup
from tests.conftest import API, ONBOARDING, admin_with_role, signup, super_admin

TODAY = date(2026, 11, 12)


def row(kind="income", amount="0", on=TODAY, status="self_reported", direction=None, category=None, desc="x"):
    return SimpleNamespace(
        type=kind, amount=Decimal(amount), date=on, verification_status=status, direction=direction,
        category=category, description=desc, created_at=datetime(on.year, on.month, on.day, 9, tzinfo=UTC),
    )


def momo(direction="in", amount="0", on=TODAY, status="self_reported"):
    return SimpleNamespace(direction=direction, amount=Decimal(amount), date=on, verification_status=status,
                           created_at=datetime(on.year, on.month, on.day, 9, tzinfo=UTC))


def test_totals_are_exact_decimals():
    s = financial_summary.build([row(amount="0.10"), row(amount="0.20")], [], 3, TODAY)
    assert s["farm"]["income"]["total"] == "0.30" and s["farm"]["net"] == "0.30"


def test_momo_and_financing_never_count_as_farm_income_or_expenses():
    farm = [
        row("income", "1000"),
        row("expense", "400"),
        row("financing", "5000", direction="in", category="loan_disbursement"),
        row("financing", "800", direction="out", category="loan_repayment"),
    ]
    s = financial_summary.build(farm, [momo("in", "300"), momo("out", "100")], 3, TODAY)
    assert s["farm"]["income"]["total"] == "1000.00" and s["farm"]["expenses"]["total"] == "400.00"
    assert s["farm"]["net"] == "600.00" and s["momo"]["net"] == "200.00"
    assert s["financing"]["in"]["total"] == "5000.00" and s["financing"]["out"]["total"] == "800.00"
    assert s["obligations"]["outstanding"] == "4200.00"
    assert [r["amount"] for r in s["repayments"]] == ["800.00"]


def test_future_entries_and_older_months_are_left_out_and_empty_months_shown():
    farm = [
        row("income", "100", on=TODAY + timedelta(days=1)),
        row("income", "200", on=date(2026, 8, 31)),  # before a 3-month period (Sep-Nov)
        row("income", "300", on=date(2026, 9, 1)),
    ]
    s = financial_summary.build(farm, [], 3, TODAY)
    assert s["period"] == {"from": "2026-09-01", "to": "2026-11-12", "months": 3}
    assert s["farm"]["income"]["total"] == "300.00"
    assert [m["month"] for m in s["monthly"]] == ["2026-09", "2026-10", "2026-11"]
    assert s["monthly"][1] == {"month": "2026-10", "income": "0.00", "expenses": "0.00", "net": "0.00"}
    # Records in only one of three months: reported as they are, flagged, never projected.
    assert s["monthsWithRecords"] == 1 and s["insufficient"] is True
    assert financial_summary.build(farm, [], 12, TODAY)["farm"]["income"]["total"] == "500.00"
    with pytest.raises(ValueError):
        financial_summary.period_bounds(4, TODAY)


def test_the_chart_starts_when_the_farmer_started_recording():
    # First entry in October: a 6-month period (Jun-Nov) charts Oct and Nov only.
    farm = [row("activity", "0", on=date(2026, 10, 15)), row("income", "50", on=date(2026, 11, 2))]
    s = financial_summary.build(farm, [], 6, TODAY)
    assert [m["month"] for m in s["monthly"]] == ["2026-10", "2026-11"]
    assert s["firstRecordedOn"] == "2026-10-15"
    assert s["period"]["from"] == "2026-06-01"  # the totals still cover the whole period
    # A MoMo entry counts as recording too.
    started = financial_summary.build([], [momo("in", "20", on=date(2026, 9, 3))], 6, TODAY)
    assert [m["month"] for m in started["monthly"]] == ["2026-09", "2026-10", "2026-11"]
    # Recording before the period: the whole period is charted.
    assert len(financial_summary.build([row("income", "1", on=date(2025, 1, 5))], [], 6, TODAY)["monthly"]) == 6
    # Nothing recorded yet: only this month, and no start date.
    empty = financial_summary.build([], [], 12, TODAY)
    assert [m["month"] for m in empty["monthly"]] == ["2026-11"] and empty["firstRecordedOn"] is None
    # A future-dated entry doesn't count as having started.
    future = financial_summary.build([row("income", "9", on=TODAY + timedelta(days=3))], [], 3, TODAY)
    assert future["firstRecordedOn"] is None


def test_only_reviewed_entries_count_as_verified():
    farm = [row("income", "300", status="verified"), row("income", "100", status="evidence_submitted"),
            row("expense", "100", status="verification_pending")]
    s = financial_summary.build(farm, [], 3, TODAY)
    assert s["farm"]["income"] == {"total": "400.00", "verified": "300.00", "selfReported": "100.00",
                                   "verifiedShare": "75.00"}
    assert s["counts"]["verified"] == 1 and s["verifiedShare"]["byCount"] == "33.33"
    assert s["counts"]["farmRecords"] == 3 and s["counts"]["financialRecords"] == 3 and s["counts"]["momo"] == 0
    assert s["verifiedShare"]["byAmount"] == "60.00"


def onboarded(email="ama@example.com"):
    c = signup(email)
    c.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    return c


def add_sale(c, amount="1200.50", on=None):
    r = c.post(f"{API}/me/records", json={
        "type": "income", "description": "Maize sale", "crop": "Maize", "amount": amount,
        "date": (on or today()).isoformat(), "category": "crop_sale",
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_the_summary_endpoint_is_private_and_exact():
    ama, kofi = onboarded(), onboarded("kofi@example.com")
    add_sale(ama)
    mine = ama.get(f"{API}/me/financial-summary", params={"months": 3}).json()
    assert mine["farm"]["income"]["total"] == "1200.50" and mine["period"]["months"] == 3
    assert mine["dataAsOf"].endswith("Z")
    assert kofi.get(f"{API}/me/financial-summary").json()["farm"]["income"]["total"] == "0.00"
    assert ama.get(f"{API}/me/financial-summary", params={"months": 5}).status_code == 422


def test_admins_see_a_farmers_summary_only_with_permission_and_audited():
    ama = onboarded()
    add_sale(ama)
    user_id = ama.get(f"{API}/auth/session").json()["userId"]
    boss = super_admin()
    r = boss.get(f"{API}/admin/farmers/{user_id}/financial-summary", params={"months": 12})
    assert r.status_code == 200 and r.json()["farm"]["income"]["total"] == "1200.50"
    with SessionLocal() as db:
        n = db.execute(text("select count(*) from audit_log where action = 'farmer.financial_summary'")).scalar()
    assert n == 1


def test_the_score_is_explained_and_no_longer_a_loan_gate():
    ama = onboarded()
    add_sale(ama)
    credit = ama.get(f"{API}/me/credit").json()
    assert credit["scoreName"] == "AgriCtec score" and credit["methodologyVersion"] == 1
    assert credit["computedAt"].endswith("Z") and credit["dataAsOf"].endswith("Z")
    assert [f["key"] for f in credit["factors"]] == [
        "payment_history", "record_completeness", "income_stability", "momo_activity", "verification",
    ]
    assert sum(f["maxPoints"] for f in credit["factors"]) in (549, 550, 551)
    assert abs(300 + sum(f["points"] for f in credit["factors"]) - credit["credit"]["score"]) <= 3
    assert credit["evidence"]["total"] == 1 and credit["evidence"]["verified"] == 0
    assert any("Ghana Card" in m for m in credit["missing"])
    assert any("receipt or statement" in m for m in credit["missing"])
    assert credit["maxAffordable"] is None and credit["annualizedProfit"] is None


def test_reports_are_generated_from_the_database_and_kept_private():
    ama, kofi = onboarded(), onboarded("kofi@example.com")
    add_sale(ama)
    created = ama.post(f"{API}/me/reports/financial", json={"months": 6})
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["periodMonths"] == 6
    pdf = ama.get(f"{API}/me/reports/financial/{report['id']}/file")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-disposition"].startswith("attachment")
    assert hashlib.sha256(pdf.content).hexdigest() == report["sha256"]
    assert [r["id"] for r in ama.get(f"{API}/me/reports/financial").json()] == [report["id"]]
    assert kofi.get(f"{API}/me/reports/financial/{report['id']}/file").status_code == 404
    assert kofi.delete(f"{API}/me/reports/financial/{report['id']}").status_code == 404
    with SessionLocal() as db:
        snapshot = db.execute(text("select summary from financial_report_exports")).scalar()
    assert snapshot["farm"]["income"]["total"] == "1200.50"


def test_admin_downloads_are_audited_and_deleting_removes_the_file():
    ama = onboarded()
    add_sale(ama)
    user_id = ama.get(f"{API}/auth/session").json()["userId"]
    report = ama.post(f"{API}/me/reports/financial", json={"months": 3}).json()
    boss = super_admin()
    analyst = admin_with_role(boss, "analyst")
    assert analyst.get(f"{API}/admin/farmers/{user_id}/reports").status_code == 200
    assert boss.get(f"{API}/admin/farmers/{user_id}/reports/{report['id']}/file").content.startswith(b"%PDF")
    with SessionLocal() as db:
        assert db.execute(text("select count(*) from audit_log where action = 'farmer.report_download'")).scalar() == 1
        assert db.execute(text("select count(*) from stored_files where key like 'reports/%'")).scalar() == 1
    assert ama.delete(f"{API}/me/reports/financial/{report['id']}").status_code == 204
    with SessionLocal() as db:
        assert db.execute(text("select count(*) from stored_files where key like 'reports/%'")).scalar() == 0


def test_reports_are_in_platform_backups():
    ama = onboarded()
    add_sale(ama)
    ama.post(f"{API}/me/reports/financial", json={"months": 3})
    boss = super_admin()
    secret = "a long backup passphrase 2026"
    backup = boss.post(f"{API}/admin/system/backup", json={"passphrase": secret}).json()
    inside = open_backup(backup, secret)
    assert len(inside["data"]["financial_report_exports"]) == 1


def _user_strings(path: pathlib.Path) -> list[str]:
    """String constants that aren't docstrings or comments."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def test_no_user_facing_text_calls_it_a_credit_score_or_promises_a_loan():
    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app.rglob("*.py"):
        for value in _user_strings(path):
            low = value.lower()
            if "credit score" in low or "you may qualify" in low or "borrow up to" in low or "you are eligible" in low:
                offenders.append(f"{path.relative_to(app)}: {value[:60]!r}")
    assert offenders == []


def test_the_overview_counts_farmers_with_verified_entries():
    onboarded()
    boss = super_admin()
    coverage = boss.get(f"{API}/admin/overview").json()["evidenceCoverage"]
    assert coverage == {"farmers": 0, "total": 1, "share": "0.00"}


def test_change_on_the_previous_period_of_the_same_length():
    # 6 months to 12 Nov 2026 is Jun-Nov; the previous 6 months are Dec 2025 - May 2026.
    farm = [
        row("income", "1220", on=date(2026, 7, 1)), row("expense", "540", on=date(2026, 8, 1)),
        row("income", "1000", on=date(2026, 5, 31)), row("expense", "500", on=date(2025, 12, 1)),
        row("income", "9999", on=date(2025, 11, 30)),  # before the previous period
    ]
    s = financial_summary.build(farm, [], 6, TODAY)
    assert s["previous"] == {"from": "2025-12-01", "to": "2026-05-31", "income": "1000.00",
                             "expenses": "500.00", "net": "500.00"}
    assert s["changeOnPrevious"] == {"income": "22.00", "expenses": "8.00", "net": "36.00"}


def test_no_previous_figures_means_no_made_up_change():
    s = financial_summary.build([row("income", "100")], [], 6, TODAY)
    assert s["changeOnPrevious"] == {"income": None, "expenses": None, "net": None}


def test_a_loss_compared_with_a_loss_keeps_its_direction():
    farm = [row("expense", "300", on=date(2026, 7, 1)), row("expense", "100", on=date(2026, 3, 1))]
    s = financial_summary.build(farm, [], 6, TODAY)
    # Net went from -100 to -300: 200% worse, shown as a fall.
    assert s["changeOnPrevious"]["net"] == "-200.00"


def test_income_and_expenses_by_category_group():
    farm = [
        row("income", "480", category="crop_sale"), row("income", "120", category="livestock_sale"),
        row("expense", "240", category="fertilizer"), row("expense", "80", category="labour"),
        row("expense", "80"),  # uncategorised: Other
        row("financing", "5000", direction="in", category="loan_disbursement"),  # never counted here
    ]
    s = financial_summary.build(farm, [momo("in", "999")], 6, TODAY)
    groups = {g["key"]: (g["label"], g["amount"], g["share"]) for g in s["byCategory"]}
    assert s["categoryTotal"] == "1000.00"
    assert groups == {
        "crop_sales": ("Crop sales", "480.00", "48.00"),
        "livestock_sales": ("Livestock sales", "120.00", "12.00"),
        "farm_inputs": ("Farm inputs", "240.00", "24.00"),
        "labour_services": ("Labour & services", "80.00", "8.00"),
        "other": ("Other", "80.00", "8.00"),
    }

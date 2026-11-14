"""Transaction-based loan eligibility: real recorded entries only, lender rules only as recorded
(with their source and dates), exact decimals, and never a loan decision."""

import re
import uuid
import zlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.domain.loan_assessment import Facts, evaluate
from app.domain.repayment import schedule
from tests.conftest import API, ONBOARDING, published_lender, signup, super_admin, add_record

TODAY = date(2026, 11, 12)
FACTS = Facts(identity_level="reviewed", phone_verified=True, location_verified=True, profile_complete=True,
              cooperative_member=False, acres=Decimal(5), region="Ashanti", crops=["Maize"], farm_type="Smallholder")
TERMS = {
    "min_amount": Decimal("1000"), "max_amount": Decimal("50000"), "interest_rate": Decimal("24"),
    "rate_period": "year", "rate_method": "reducing_balance", "repayment_frequency": "monthly", "fees": [],
    "min_months": 6, "max_months": 24,
}


def farm(kind, amount, on, category=None, status="self_reported", direction=None):
    return SimpleNamespace(id=str(uuid.uuid4()), type=kind, category=category, direction=direction,
                           amount=Decimal(str(amount)), date=on, verification_status=status,
                           created_at=datetime(on.year, on.month, on.day, 9, tzinfo=UTC))


def momo(direction, amount, on, category=None, status="self_reported", duplicate=False):
    return SimpleNamespace(id=str(uuid.uuid4()), direction=direction, category=category,
                           amount=Decimal(str(amount)), date=on, verification_status=status,
                           reference_duplicate=duplicate, created_at=datetime(on.year, on.month, on.day, 9, tzinfo=UTC))


def rule(metric, op, value=None, *, key=None, required=True, **extra):
    return {"key": key or metric, "name": metric.replace("_", " "), "category": "financial", "required": required,
            "field_type": "none", "check": {"metric": metric, "op": op, "value": value},
            "effective_from": date(2026, 1, 1), "reviewed_on": date(2026, 10, 1), "review_by": None,
            "source": "Lender's 2026 agricultural lending policy (test)", **extra}


def assess(*requirements, terms=None, farm_rows=(), momo_rows=(), amount="10000", months=12, facts=FACTS):
    return evaluate(product={"terms": {**TERMS, **(terms or {})}, "requirements": list(requirements)},
                    lender_name="Test Rural Bank", amount=Decimal(amount), months=months, facts=facts,
                    farm=list(farm_rows), momo=list(momo_rows), today=TODAY)


def every_month(amount, kind="income", category="crop_sale", months=12):
    return [farm(kind, amount, date(2026, 11, 5) - timedelta(days=30 * i), category) for i in range(months)]


def by_key(result, key):
    return next(r for r in result["rules"] if r["key"] == key)


# ---------- Repayments from the lender's own terms ----------


def test_repayments_follow_the_lenders_method_in_exact_decimals():
    reducing = schedule(amount=Decimal("10000"), months=12, rate=Decimal("24"), rate_period="year",
                        method="reducing_balance", frequency="monthly")
    assert (reducing.instalment, reducing.total_repayment, reducing.total_interest) == (
        Decimal("945.60"), Decimal("11347.20"), Decimal("1347.20"))
    flat = schedule(amount=Decimal("10000"), months=12, rate=Decimal("24"), rate_period="year",
                    method="flat", frequency="monthly")
    assert (flat.total_interest, flat.instalment) == (Decimal("2400.00"), Decimal("1033.33"))
    # One payment at the end of the season, spread over the term for comparison with cash flow.
    season = schedule(amount=Decimal("10000"), months=6, rate=Decimal("2"), rate_period="month", method="flat",
                      frequency="end_of_season", fees=[{"kind": "percent", "value": "2"}, {"kind": "fixed", "value": "50"}])
    assert (season.payments, season.instalment, season.average_monthly, season.total_fees) == (
        1, Decimal("11200.00"), Decimal("1866.67"), Decimal("250.00"))
    quarterly = schedule(amount=Decimal("10000"), months=12, rate=Decimal("24"), rate_period="year",
                         method="reducing_balance", frequency="quarterly")
    assert quarterly.payments == 4 and quarterly.total_repayment > Decimal("10000")
    # Nothing is invented when the lender didn't say.
    for missing in ({"rate": None}, {"rate_period": None}, {"method": "other"}, {"frequency": "other"}, {"frequency": None}):
        args = {"amount": Decimal("10000"), "months": 12, "rate": Decimal("24"), "rate_period": "year",
                "method": "flat", "frequency": "monthly", **missing}
        assert isinstance(schedule(**args), str)
    assert "whole quarters" in schedule(amount=Decimal("1000"), months=13, rate=Decimal("24"), rate_period="year",
                                        method="flat", frequency="quarterly")


# ---------- History and records ----------


def test_missing_history_asks_for_records_and_never_rejects():
    result = assess(rule("period_income", "gte", Decimal("1000")))
    assert result["outcome"] == "additional_information"
    assert "no recorded income or expense entries" in by_key(result, "period_income")["explanation"]
    assert result["figures"]["entries"] == 0


def test_a_short_history_needs_more_information_with_the_numbers():
    rows = [farm("income", 500, date(2026, 11, 1), "crop_sale"), farm("expense", 100, date(2026, 10, 3), "seed")]
    result = assess(rule("months_of_records", "gte", 6), farm_rows=rows)
    assert result["outcome"] == "additional_information"
    assert "2, but the lender asks for at least 6" in by_key(result, "months_of_records")["explanation"]


def test_a_confirmed_income_rule_that_is_not_met_shows_the_figures():
    rows = [farm("income", 500, date(2026, 11, 1), "crop_sale")]
    result = assess(rule("period_income", "gte", Decimal("1000")), farm_rows=rows)
    assert result["outcome"] == "does_not_meet"
    assert "GHS 500.00, but the lender asks for at least GHS 1,000.00" in result["summary"]


def test_borrowed_money_and_uncategorised_momo_are_never_income():
    rows = [farm("income", 1000, date(2026, 11, 1), "crop_sale"),
            farm("financing", 5000, date(2026, 10, 1), "loan_disbursement", direction="in")]
    momo_rows = [momo("in", 3000, date(2026, 10, 20))]  # no category: not assumed to be income
    figures = assess(farm_rows=rows, momo_rows=momo_rows)["figures"]
    assert figures["income"] == "1000.00" and figures["borrowed"] == "5000.00"
    assert figures["otherMoneyIn"] == "3000.00" and figures["outstandingRecordedLoans"] == "5000.00"


def test_duplicate_transfers_and_matching_momo_are_counted_once():
    sale = farm("income", 1200, date(2026, 11, 2), "crop_sale")
    same_sale = momo("in", 1200, date(2026, 11, 2), "crop_sale")  # the same sale, paid by MoMo
    other_sale = momo("in", 800, date(2026, 11, 3), "crop_sale")
    repeated_ref = momo("in", 800, date(2026, 11, 3), "crop_sale", duplicate=True)
    result = assess(farm_rows=[sale], momo_rows=[same_sale, other_sale, repeated_ref])
    figures = result["figures"]
    assert figures["income"] == "2000.00"  # 1200 once + 800; the duplicate reference is left out
    assert figures["matchedMomo"] == 1 and figures["momoEntries"] == 2
    assert any("counted once" in note for note in result["notes"])


def test_rejected_disputed_and_future_entries_are_left_out():
    rows = [farm("income", 1000, date(2026, 11, 1), "crop_sale"),
            farm("income", 9000, date(2026, 11, 2), "crop_sale", status="rejected"),
            farm("income", 9000, date(2026, 11, 3), "crop_sale", status="disputed"),
            farm("income", 9000, TODAY + timedelta(days=2), "crop_sale")]
    assert assess(farm_rows=rows)["figures"]["income"] == "1000.00"


def test_unverified_entries_need_evidence_when_the_lender_asks_for_coverage():
    rows = [farm("income", 1000, date(2026, 11, 1), "crop_sale", status="verified"),
            farm("income", 3000, date(2026, 10, 1), "crop_sale")]
    result = assess(rule("verified_share", "gte", 50), farm_rows=rows)
    assert by_key(result, "verified_share")["value"] == "25.00%"
    assert result["outcome"] == "additional_information"
    assert "Attach receipts" in by_key(result, "verified_share")["explanation"]


# ---------- Seasonal and irregular income ----------


def test_seasonal_income_is_averaged_not_rejected():
    harvest = [farm("income", 6000, date(2026, 8, 10), "crop_sale"), farm("income", 6000, date(2026, 9, 10), "crop_sale")]
    result = assess(rule("period_income", "gte", Decimal("10000")), farm_rows=harvest)
    assert result["outcome"] == "meets_preliminary"
    assert result["figures"]["monthsWithIncome"] == 2 and result["figures"]["averageMonthlyNet"] == "1000.00"
    assert any("seasonal" in note for note in result["notes"])
    # Only a lender's own consistency rule can hold low-volume months against the farmer.
    strict = assess(rule("income_months_share", "gte", 50), farm_rows=harvest)
    assert strict["outcome"] == "does_not_meet" and by_key(strict, "income_months_share")["value"] == "16.67%"


def test_one_large_irregular_deposit_is_pointed_out():
    rows = [farm("income", 9000, date(2026, 10, 1), "crop_sale"), farm("income", 500, date(2026, 11, 1), "crop_sale")]
    assert any("one entry" in note for note in assess(farm_rows=rows)["notes"])


# ---------- Repayment capacity ----------


def _steady_farmer():
    rows = every_month(1000) + every_month(200, "expense", "seed")
    rows.append(farm("financing", 1200, date(2026, 6, 1), "loan_repayment", direction="out"))
    return rows


def test_existing_debt_and_dscr_use_the_documented_calculation():
    result = assess(rule("dscr", "gte", Decimal("1.2")), farm_rows=_steady_farmer())
    dscr = by_key(result, "dscr")
    # 800 average monthly net ÷ (100 existing + 945.60 proposed) = 0.77
    assert dscr["status"] == "not_met" and dscr["value"] == "0.77"
    assert "945.60" in dscr["explanation"] and "100.00" in dscr["explanation"]
    assert result["outcome"] == "does_not_meet"
    assert assess(rule("dscr", "gte", Decimal("0.5")), farm_rows=_steady_farmer())["outcome"] == "meets_preliminary"
    burden = assess(rule("debt_burden", "lte", 5), farm_rows=_steady_farmer())
    assert by_key(burden, "debt_burden")["value"] == "10.00%" and burden["outcome"] == "does_not_meet"


def test_without_an_affordability_rule_the_comparison_is_information_only():
    result = assess(rule("period_income", "gte", Decimal("1000")), farm_rows=_steady_farmer())
    aff = result["affordability"]
    assert aff["available"] and not aff["ruled"] and "Information only" in aff["note"]
    assert aff["leftAfterRepayments"] == "-245.60"  # 800 - 100 - 945.60
    assert result["outcome"] == "meets_preliminary"  # the comparison never fails anyone


def test_missing_interest_terms_mean_unable_to_assess_not_a_guess():
    result = assess(rule("dscr", "gte", 1), terms={"interest_rate": None}, farm_rows=_steady_farmer())
    assert result["repayment"]["available"] is False
    assert by_key(result, "dscr")["status"] == "unknown"
    assert result["outcome"] == "unable_to_assess"


# ---------- Lender rules ----------


def test_expired_rules_are_not_applied():
    result = assess(rule("period_income", "gte", Decimal("1"), review_by=date(2026, 10, 31)), farm_rows=_steady_farmer())
    assert by_key(result, "period_income")["status"] == "expired" and result["outcome"] == "unable_to_assess"
    later = assess(rule("period_income", "gte", Decimal("999999"), effective_from=date(2027, 1, 1)), farm_rows=_steady_farmer())
    assert by_key(later, "period_income")["status"] == "not_yet_in_effect" and later["outcome"] == "meets_preliminary"


def test_different_lenders_different_results_for_the_same_farmer():
    rows = _steady_farmer()
    assert assess(rule("period_income", "gte", Decimal("10000")), farm_rows=rows)["outcome"] == "meets_preliminary"
    assert assess(rule("period_income", "gte", Decimal("50000")), farm_rows=rows)["outcome"] == "does_not_meet"


def test_amount_outside_the_published_range_does_not_meet():
    result = assess(amount="60000", farm_rows=_steady_farmer())
    assert result["outcome"] == "does_not_meet" and "outside the lender's published range" in result["summary"]


def test_a_product_with_nothing_to_check_is_unable_to_assess():
    result = assess(terms={"min_amount": None, "max_amount": None, "min_months": None, "max_months": None})
    assert result["outcome"] == "unable_to_assess" and "has not published conditions" in result["summary"]


def test_identity_reviewed_by_agrictec_is_labelled_as_such():
    card = {**rule("ghana_card_verified", "is_true"), "category": "identity"}
    assert "not an NIA identity check" in by_key(assess(card), "ghana_card_verified")["explanation"]
    none = Facts(**{**FACTS.__dict__, "identity_level": "none"})
    assert assess(card, facts=none)["outcome"] == "additional_information"


def test_documents_to_provide_are_listed_and_the_result_is_never_a_decision():
    doc = {"key": "statement", "name": "Bank statement", "category": "financial", "required": True,
           "field_type": "document", "check": None}
    result = assess(rule("period_income", "gte", Decimal("1000")), doc, farm_rows=_steady_farmer())
    assert result["toProvide"][0]["name"] == "Bank statement"
    assert "not a loan decision" in result["disclaimer"] and "Test Rural Bank makes the final decision" in result["disclaimer"]
    text = str(result).lower()
    assert "approved" not in text and "you qualify" not in text


# ---------- Through the API ----------

PRODUCT = {
    "name": "Season Input Loan",
    "terms": {"minAmount": "1000.00", "maxAmount": "50000.00", "interestRate": "24", "ratePeriod": "year",
              "rateMethod": "reducing_balance", "repaymentFrequency": "monthly", "minMonths": 6, "maxMonths": 24,
              "assessmentMonths": 6},
    "requirements": [{"key": "income", "name": "Six months' income", "category": "financial", "required": True,
                      "fieldType": "none", "check": {"metric": "period_income", "op": "gte", "value": 1000},
                      "effectiveFrom": "2026-01-01", "reviewedOn": date.today().isoformat()}],
    "source": "Lender's agricultural loan brochure (test)",
    "sourceCheckedOn": date.today().isoformat(),
}


def published_product(boss, **over) -> dict:
    lender = published_lender(boss)
    product = boss.post(f"{API}/admin/lenders/{lender['id']}/products", json={"name": "Season Input Loan"}).json()
    url = f"{API}/admin/loan-products/{product['id']}"
    saved = boss.put(f"{url}/draft", json={**PRODUCT, **over}).json()
    assert saved["problems"] == [], saved["problems"]
    assert boss.post(f"{url}/publish", json={"note": "First version"}).status_code == 200
    return product


def test_financial_rules_need_their_dates_before_publishing():
    boss = super_admin()
    lender = published_lender(boss)
    product = boss.post(f"{API}/admin/lenders/{lender['id']}/products", json={"name": "Undated"}).json()
    undated = {**PRODUCT, "requirements": [{**PRODUCT["requirements"][0], "effectiveFrom": None, "reviewedOn": None}]}
    problems = boss.put(f"{API}/admin/loan-products/{product['id']}/draft", json=undated).json()["problems"]
    assert any("took effect" in p for p in problems) and any("last confirmed" in p for p in problems)


def test_a_farmer_checks_a_product_and_the_snapshot_is_kept_privately():
    boss = super_admin()
    product = published_product(boss)
    url = f"{API}/me/loan-products/{product['id']}/eligibility-check"
    assert signup("nobody@example.com").post(url.replace(product["id"], "missing"), json={"amount": 1000, "months": 6}).status_code == 404
    from tests.conftest import new_client
    assert new_client().post(url, json={"amount": 1000, "months": 6}).status_code == 401
    ama = signup("ama@example.com")
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    assert ama.post(url, json={"amount": 0, "months": 6}).status_code == 422
    assert ama.post(url, json={"amount": 1000, "months": 0}).status_code == 422
    first = ama.post(url, json={"amount": 5000, "months": 12})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["outcome"] == "additional_information"  # no records yet
    assert body["period"] == {"months": 6, "from": body["period"]["from"], "to": body["period"]["to"], "source": "lender"}
    assert body["product"]["version"] == 1 and body["repayment"]["available"]
    add_record(ama, {"type": "income", "category": "crop_sale", "description": "Maize sale (test)",
                                       "crop": "Maize", "amount": 2400, "date": date.today().isoformat()})
    second = ama.post(url, json={"amount": 6000, "months": 12}).json()
    assert second["outcome"] == "meets_preliminary" and second["figures"]["income"] == "2400.00"
    latest = ama.get(f"{API}/me/loan-assessments").json()
    assert len(latest) == 1 and latest[0]["id"] == second["id"] and latest[0]["amount"] == "6000.00"
    kofi = signup("kofi@example.com")
    assert kofi.get(f"{API}/me/loan-assessments").json() == []


def test_checks_stop_when_loans_are_switched_off():
    boss = super_admin()
    product = published_product(boss)
    from tests.conftest import new_client
    flags = new_client().get(f"{API}/platform/flags").json()
    assert boss.put(f"{API}/admin/settings/flags", json={**flags, "loans": False}).status_code == 200
    ama = signup()
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    r = ama.post(f"{API}/me/loan-products/{product['id']}/eligibility-check", json={"amount": 1000, "months": 6})
    assert r.status_code == 403 and r.json()["code"] == "feature_off"


def test_the_statement_pdf_lists_each_entry_with_when_it_was_recorded():
    ama = signup()
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    add_record(ama, {"type": "income", "category": "crop_sale", "description": "Maize sale (test)",
                                       "crop": "Maize", "amount": 2400, "date": date.today().isoformat()})
    report = ama.post(f"{API}/me/reports/financial", json={"months": 3}).json()
    pdf = ama.get(f"{API}/me/reports/financial/{report['id']}/file").content
    text = b""
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            text += zlib.decompress(stream)
        except zlib.error:
            pass
    # The column header wraps ("Recorded" / "(GMT)"), so its words are checked separately.
    assert b"(Recorded)" in text and b"GMT" in text and b"Maize sale" in text

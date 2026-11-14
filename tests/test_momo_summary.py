"""The MoMo page's figures: exact totals from the farmer's own MoMo entries (GET /me/momo/summary)."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.domain import momo_summary
from app.domain.clock import today
from tests.conftest import API, ONBOARDING, signup, add_momo

TODAY = date(2026, 11, 12)


def momo(direction="in", amount="0", on=TODAY, network="MTN", status="self_reported"):
    return SimpleNamespace(direction=direction, amount=Decimal(amount), date=on, network=network,
                           verification_status=status)


def test_all_time_totals_are_exact_and_future_entries_are_left_out():
    s = momo_summary.build([
        momo("in", "0.10"), momo("in", "0.20"), momo("out", "100"),
        momo("in", "999", on=TODAY + timedelta(days=1)),
    ], TODAY)
    a = s["allTime"]
    assert (a["in"], a["out"], a["net"], a["moved"]) == ("0.30", "100.00", "-99.70", "100.30")
    assert (a["count"], a["countIn"], a["countOut"]) == (3, 2, 1)


def test_this_month_compared_with_last_month():
    s = momo_summary.build([
        momo("in", "300", on=date(2026, 11, 2)), momo("out", "50", on=date(2026, 11, 3)),
        momo("in", "200", on=date(2026, 10, 20)),
        momo("in", "999", on=date(2026, 9, 1)),
    ], TODAY)
    assert s["thisMonth"]["month"] == "2026-11" and s["thisMonth"]["in"] == "300.00"
    assert s["thisMonth"]["net"] == "250.00" and s["lastMonth"]["in"] == "200.00"
    assert s["changeOnLastMonth"]["in"] == "50.00"
    # Nothing sent last month: no percentage is made up.
    assert s["changeOnLastMonth"]["out"] is None


def test_six_month_trend_starts_five_months_back_and_shows_empty_months():
    s = momo_summary.build([momo("out", "40", on=date(2026, 6, 1)), momo("out", "10", on=date(2026, 5, 31))], TODAY)
    assert [m["month"] for m in s["monthly"]] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    assert s["monthly"][0]["out"] == "40.00" and s["monthly"][-1]["count"] == 0


def test_networks_verification_and_main_network():
    s = momo_summary.build([
        momo("in", "300", network="MTN", status="verified"), momo("out", "100", network="MTN"),
        momo("out", "100", network="Telecel"), momo("in", "50", network=None),
    ], TODAY)
    mtn = next(n for n in s["networks"] if n["network"] == "MTN")
    assert (mtn["amount"], mtn["count"], mtn["amountShare"], mtn["countShare"]) == ("400.00", 2, "72.73", "50.00")
    assert s["untagged"] == {"amount": "50.00", "count": 1}
    assert s["mainNetwork"] == {"network": "MTN", "countShare": "50.00"}
    assert s["allTime"]["verified"] == 1 and s["allTime"]["verifiedShare"] == "25.00"
    assert s["byStatus"]["verified"] == 1 and s["byStatus"]["self_reported"] == 3


def test_no_entries_means_zeros_and_no_main_network():
    s = momo_summary.build([], TODAY)
    assert s["allTime"]["net"] == "0.00" and s["allTime"]["verifiedShare"] == "0.00"
    assert s["mainNetwork"] is None and all(n["amountShare"] == "0.00" for n in s["networks"])


def test_the_endpoint_reads_only_the_farmers_own_entries():
    ama = signup("ama.momo@example.com")
    ama.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    day = today().isoformat()
    for body in ({"direction": "in", "counterparty": "Buyer", "amount": 250.5, "network": "MTN", "date": day},
                 {"direction": "out", "counterparty": "Agro shop", "amount": 100, "network": "Telecel", "date": day}):
        assert add_momo(ama, body).status_code == 201
    kofi = signup("kofi.momo@example.com")
    kofi.post(f"{API}/me/onboarding/complete", json=ONBOARDING)

    mine = ama.get(f"{API}/me/momo/summary").json()
    assert (mine["allTime"]["in"], mine["allTime"]["out"], mine["allTime"]["net"]) == ("250.50", "100.00", "150.50")
    assert mine["thisMonth"]["count"] == 2 and mine["mainNetwork"]["network"] in ("MTN", "Telecel")
    other = kofi.get(f"{API}/me/momo/summary").json()
    assert other["allTime"]["count"] == 0

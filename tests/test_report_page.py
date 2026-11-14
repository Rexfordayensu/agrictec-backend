"""Database totals and recent transactions on the Financial Reports page."""
from datetime import timedelta

from app.domain.clock import today
from tests.conftest import API, signup, add_record


def test_report_page_totals_recent_rows_and_real_evidence_refresh(monkeypatch):
    from app.services import farm
    events = []
    monkeypatch.setattr(farm.bus, "publish", lambda *args: events.append(args))
    owner, other = signup(), signup("other-report-page@example.com")

    def add(client, amount, description, **values):
        r = add_record(client, {"type": "income", "amount": amount, "crop": "Maize",
                       "description": description, "date": today().isoformat(), **values})
        assert r.status_code == 201, r.text
        return r.json()

    first = add(owner, "0.10", "Own sale", category="crop_sale")
    add(owner, "0.20", "Own sale two", category="livestock_sale")
    add(owner, "0.05", "Own expense", type="expense", category="seed")
    add(owner, "10", "Old sale", date=(today() - timedelta(days=500)).isoformat())
    add(owner, "20", "Tomorrow", date=(today() + timedelta(days=1)).isoformat())
    add(other, "999", "Other farmer")
    url = f"{API}/me/financial-summary?months=6"
    s = owner.get(url).json()
    assert s["farm"]["income"]["total"] == "0.30"
    assert s["farm"]["expenses"]["total"] == "0.05" and s["farm"]["net"] == "0.25"
    assert s["categoryTotal"] == "0.35"
    assert {r["description"] for r in s["recentTransactions"]} == {"Own sale", "Own sale two", "Own expense"}
    assert {r["amount"] for r in s["recentTransactions"]} == {"0.10", "0.20", "0.05"}
    assert s["recordReadiness"] == {"entriesWithEvidence": 0, "identityVerified": False}
    evidence = owner.post(f"{API}/me/transactions/farm/{first['id']}/evidence",
                          files={"file": ("receipt.pdf", b"%PDF-1.7 real attachment", "application/pdf")})
    assert evidence.status_code == 200, evidence.text
    assert owner.get(url).json()["recordReadiness"]["entriesWithEvidence"] == 1
    assert owner.patch(f"{API}/me/records/{first['id']}", json={"amount": "0.40"}).status_code == 200
    assert owner.get(url).json()["farm"]["income"]["total"] == "0.60"
    assert owner.delete(f"{API}/me/records/{first['id']}").status_code == 204
    s = owner.get(url).json()
    assert s["farm"]["net"] == "0.15" and len(s["recentTransactions"]) == 2
    assert s["recordReadiness"]["entriesWithEvidence"] == 0
    assert other.get(url).json()["farm"]["income"]["total"] == "999.00"
    assert all(topic == "account" for topic, _ in events)

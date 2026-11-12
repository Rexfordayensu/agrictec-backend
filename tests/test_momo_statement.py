"""Private filtered MoMo PDF exports and live account updates after writes."""

import re
import zlib

from app.domain.clock import today
from app.services import farm
from tests.conftest import API, signup


def pdf_text(data):
    chunks = []
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, flags=re.S):
        try:
            chunks.append(zlib.decompress(stream).decode("latin-1"))
        except zlib.error:
            pass
    return "\n".join(chunks)


def add(client, **overrides):
    response = client.post(f"{API}/me/momo", json={
        "direction": "in", "counterparty": "Local buyer", "amount": "10.10", "network": "MTN",
        "date": today().isoformat(), **overrides,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_statement_is_momo_only_private_exact_and_filtered():
    owner, other = signup(), signup("other-statement@example.com")
    add(owner, counterparty="OwnBuyer", amount="0.10", reference="OWN-1")
    add(owner, counterparty="OwnSupplier", direction="out", amount="0.20", network="Telecel", reference="OWN-2")
    add(other, counterparty="PrivateOtherFarmer", amount="999.99")
    owner.post(f"{API}/me/records", json={"type": "income", "description": "FarmOnlyNotMoMo", "amount": 5000,
                                         "crop": "Maize", "date": today().isoformat()})
    response = owner.get(f"{API}/me/momo/statement")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF")
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment" in response.headers["content-disposition"]
    text = pdf_text(response.content)
    assert "OwnBuyer" in text and "OwnSupplier" in text
    assert "PrivateOtherFarmer" not in text and "FarmOnlyNotMoMo" not in text
    assert "Received GHS 0.10" in text and "Sent GHS 0.20" in text and "Net -GHS 0.10" in text
    assert "Self-reported" in text and "Not a bank statement" in text
    filtered = owner.get(f"{API}/me/momo/statement", params={"network": "Telecel", "direction": "out", "query": "GHS 0.20"})
    text = pdf_text(filtered.content)
    assert "OwnSupplier" in text and "OwnBuyer" not in text
    assert "1 transactions" in text
    empty = owner.get(f"{API}/me/momo/statement", params={"status": "verified"})
    assert "No recorded MoMo transactions match" in pdf_text(empty.content)
    assert owner.get(f"{API}/me/momo/statement", params={"network": "Invented"}).status_code == 422
    assert owner.get(f"{API}/me/momo/statement", params={"query": "sNaN"}).status_code == 200


def test_momo_writes_notify_only_the_owning_account(monkeypatch):
    updates = []
    monkeypatch.setattr(farm.bus, "publish", lambda topic, user_id=None: updates.append((topic, user_id)))
    owner, other = signup(), signup("other-live@example.com")
    user_id = owner.get(f"{API}/auth/session").json()["userId"]
    row = add(owner)
    assert other.patch(f"{API}/me/momo/{row['id']}", json={"amount": "5.00"}).status_code == 404
    assert owner.patch(f"{API}/me/momo/{row['id']}", json={"amount": "15.15"}).status_code == 200
    assert owner.get(f"{API}/me/momo/summary").json()["allTime"]["in"] == "15.15"
    assert owner.delete(f"{API}/me/momo/{row['id']}").status_code == 204
    assert updates == [("account", user_id)] * 3
    assert owner.get(f"{API}/me/momo/summary").json()["allTime"]["count"] == 0

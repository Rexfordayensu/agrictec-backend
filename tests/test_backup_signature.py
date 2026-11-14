"""A backup can only restore exactly what AgriCtec exported for the same account: an edited or
hand-made file, or another farmer's file, is refused, so a backup can't add entries (such as MoMo
payments without their receipt)."""

from datetime import date

from tests.conftest import API, add_record, signup

SALE = {"type": "income", "description": "Sold maize", "crop": "Maize", "amount": "250.00",
        "date": date.today().isoformat()}


def test_an_unchanged_export_restores():
    farmer = signup()
    add_record(farmer, SALE)
    exported = farmer.get(f"{API}/me/export").json()
    assert len(exported["signature"]) == 64
    assert farmer.post(f"{API}/me/import", json=exported).status_code == 200
    assert [r["description"] for r in farmer.get(f"{API}/me/records").json()] == ["Sold maize"]


def test_an_edited_file_is_refused_and_nothing_changes():
    farmer = signup()
    add_record(farmer, SALE)
    exported = farmer.get(f"{API}/me/export").json()
    for change in (
        lambda e: e["records"][0].update(amount=25000),
        lambda e: e["momo"].append({"direction": "in", "counterparty": "Buyer", "amount": 900,
                                    "date": date.today().isoformat(), "network": "MTN"}),
        lambda e: e.pop("signature"),
    ):
        tampered = {**exported, "records": [dict(r) for r in exported["records"]], "momo": list(exported["momo"])}
        change(tampered)
        refused = farmer.post(f"{API}/me/import", json=tampered)
        assert refused.status_code == 400 and refused.json()["code"] == "backup_signature"
    assert [r["amount"] for r in farmer.get(f"{API}/me/records").json()] == [250.0]
    assert farmer.get(f"{API}/me/momo").json() == []


def test_another_farmers_file_is_refused():
    ama, kofi = signup(), signup("kofi.backup@example.com")
    add_record(ama, SALE)
    amas_file = ama.get(f"{API}/me/export").json()
    refused = kofi.post(f"{API}/me/import", json=amas_file)
    assert refused.status_code == 400 and refused.json()["code"] == "backup_signature"
    assert kofi.get(f"{API}/me/records").json() == []

"""A receipt proves one payment; the review warns when a receipt or MoMo ID shows up on another
farmer's account."""

from datetime import date

from tests.conftest import API, add_momo, add_record, receipt_png, signup, super_admin

TODAY = date.today().isoformat()


def momo(ref, amount="150.00"):
    return {"direction": "in", "counterparty": "Buyer", "amount": amount, "date": TODAY, "network": "MTN",
            "reference": ref}


def test_the_same_receipt_cannot_back_a_second_transaction():
    farmer = signup()
    receipt = receipt_png(201)
    assert add_momo(farmer, momo("A1"), receipt).status_code == 201
    again = add_momo(farmer, momo("A2", "151.00"), receipt)
    assert again.status_code == 400 and again.json()["code"] == "receipt_reused"
    assert len(farmer.get(f"{API}/me/momo").json()) == 1

    record = add_record(farmer, {"type": "income", "description": "Sale", "crop": "Maize", "amount": "90.00",
                                 "date": TODAY}).json()
    reused = farmer.post(f"{API}/me/transactions/farm/{record['id']}/evidence",
                         files={"file": ("r.png", receipt, "image/png")})
    assert reused.status_code == 400 and reused.json()["code"] == "receipt_reused"


def test_a_statement_may_cover_several_entries():
    farmer = signup()
    statement = b"%PDF-1.7\nbank statement for October"
    ids = [add_record(farmer, {"type": "income", "description": f"Sale {i}", "crop": "Maize",
                               "amount": f"{10 + i}.00", "date": TODAY}).json()["id"] for i in range(2)]
    for record_id in ids:
        ok = farmer.post(f"{API}/me/transactions/farm/{record_id}/evidence",
                         files={"file": ("statement.pdf", statement, "application/pdf")},
                         data={"evidence_kind": "statement"})
        assert ok.status_code == 200, ok.text


def test_the_review_warns_about_a_receipt_or_momo_id_on_another_account():
    ama, kofi = signup(), signup("kofi.reuse@example.com")
    boss = super_admin()
    shared = receipt_png(202)
    add_momo(ama, momo("MP261114.0001.X1"), shared)
    add_momo(kofi, momo("MP261114.0001.X1"), shared)
    add_momo(kofi, momo("MP261114.0002.Y2", "75.00"))
    queue = boss.get(f"{API}/admin/transactions/review").json()["items"]
    by_ref = {item["entry"]["reference"]: item["warnings"] for item in queue}
    flagged = [w for item in queue if item["entry"]["reference"] == "MP261114.0001.X1" for w in item["warnings"]]
    assert any("same receipt file" in w for w in flagged)
    assert any("MP261114.0001.X1 is also recorded by 1 other farmer" in w for w in flagged)
    assert by_ref["MP261114.0002.Y2"] == []

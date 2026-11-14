"""Bounded transaction-list queries and transactional removal of saved financial reports."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.db import engine
from app.domain.clock import now_utc
from app.models import FarmRecord, FinancialReportExport, MomoTransaction, StoredFile
from app.services import farm, transactions
from tests.conftest import API, ONBOARDING, signup
from tests.test_transactions import upload


@pytest.mark.parametrize("kind", ["farm", "momo"])
def test_transaction_lists_use_constant_queries_and_keep_metadata(db, kind):
    client = signup()
    user_id = client.get(f"{API}/auth/session").json()["userId"]
    now = now_utc()
    fields = {"type": "income", "description": "Sale", "crop": "Maize"} if kind == "farm" else {
        "direction": "in", "counterparty": "Buyer",
    }
    model = FarmRecord if kind == "farm" else MomoTransaction
    rows = [model(user_id=user_id, amount=Decimal("0.10"), date=date.today(), created_at=now, **fields)
            for _ in range(30)]
    db.add_all(rows)
    db.flush()
    for row in rows:
        transactions.change_status(db, row, kind, "self_reported", initial=True)
    db.commit()
    first_id = rows[0].id
    assert upload(client, {"id": first_id}, kind=kind).status_code == 200
    db.expire_all()
    queries = []

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        listed = farm.list_records(db, user_id) if kind == "farm" else farm.list_momo(db, user_id)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    assert len(listed) == 30
    assert len(queries) == 3, queries  # entries, histories, grouped evidence counts
    attached = next(row for row in listed if row.id == first_id)
    assert attached.evidence_count == 1
    # A MoMo receipt sends the entry straight to review; a farm record waits for "Request review".
    assert attached.verification_status == ("evidence_submitted" if kind == "farm" else "verification_pending")
    expected = ["self_reported", "evidence_submitted"] + ([] if kind == "farm" else ["verification_pending"])
    assert [h.to_status for h in attached.history] == expected
    assert all(row.evidence_count == 0 and len(row.history) == 1 for row in listed if row.id != first_id)


def test_delete_my_data_removes_own_report_pdfs_and_preserves_another_farmer(db):
    clients = [signup("reports-a@example.com"), signup("reports-b@example.com")]
    reports = []
    for client in clients:
        assert client.post(f"{API}/me/onboarding/complete", json=ONBOARDING).status_code == 200
        response = client.post(f"{API}/me/reports/financial", json={"months": 6})
        assert response.status_code == 201, response.text
        reports.append(response.json())
    rows = list(db.scalars(select(FinancialReportExport)))
    keys = {row.id: row.file_key for row in rows}
    assert clients[0].post(f"{API}/me/delete-data", json={"confirmation": "DELETE"}).status_code == 200
    db.expire_all()
    assert db.get(FinancialReportExport, reports[0]["id"]) is None
    assert db.get(StoredFile, keys[reports[0]["id"]]) is None
    assert db.get(StoredFile, keys[reports[1]["id"]]) is not None
    assert clients[0].get(f"{API}/me/reports/financial").json() == []
    assert clients[0].get(f"{API}/me/reports/financial/{reports[0]['id']}/file").status_code == 404
    assert clients[1].get(f"{API}/me/reports/financial/{reports[1]['id']}/file").status_code == 200
    assert clients[0].get(f"{API}/auth/session").status_code == 200

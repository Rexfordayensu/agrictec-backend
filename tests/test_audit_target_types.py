"""Every kind of record the code writes to the audit log is one the log can show. One unknown
entry used to stop the audit log, and the admin overview, from loading at all."""

import pathlib
import re
from typing import get_args

from app.schemas.admin import TargetType
from tests.conftest import API, add_momo, signup, super_admin

APP = pathlib.Path(__file__).parents[1] / "app"


def test_the_code_only_writes_audit_targets_the_log_can_show():
    written = set()
    for path in APP.rglob("*.py"):
        written |= set(re.findall(r'target_type="([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert written, "the code writes audit entries"
    assert written <= set(get_args(TargetType)), written - set(get_args(TargetType))


def test_the_overview_and_the_log_load_after_a_transaction_review():
    boss = super_admin()
    farmer = signup("audit.target@example.com")
    entry = add_momo(farmer, {"direction": "in", "counterparty": "Buyer", "amount": "40.00",
                              "date": "2026-11-14", "network": "MTN", "reference": "MP261114.1200.T00001"}).json()
    assert boss.post(f"{API}/admin/transactions/momo/{entry['id']}/verify",
                     json={"reason": "Receipt matches"}).status_code == 200
    log = boss.get(f"{API}/admin/audit")
    assert log.status_code == 200, log.text
    assert any(e["targetType"] == "transaction" for e in log.json()["items"])
    assert boss.get(f"{API}/admin/overview", params={"range": "12m"}).status_code == 200

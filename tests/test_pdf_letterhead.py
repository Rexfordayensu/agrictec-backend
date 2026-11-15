"""Every downloadable PDF carries the AgriCtec letterhead (logo, "DIGITAL IDENTITY", the document's
title, reference and date) and footer ("AgriCtec Digital Identity", the reference, "Page X of Y"),
each with its own arrangement below. The financial report lists every transaction, numbered
1, 2, 3... from the earliest, the numbering running on across pages, and nothing is invented."""

import re
import zlib
from datetime import datetime, timezone

import pytest

from app.domain.application_pack import render as render_pack
from app.domain.financial_report import reference_for, render
from app.domain.pdf_brand import DISCLAIMER
from tests.conftest import API, add_momo, add_record, signup

AT = datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc)


def text_of(data: bytes) -> str:
    chunks = []
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, flags=re.S):
        try:
            chunks.append(zlib.decompress(stream).decode("latin-1"))
        except zlib.error:
            pass
    return "\n".join(chunks)


def body_of(data: bytes) -> str:
    """Each page's text without its footer (drawn last), so page totals never read as rows."""
    page_starts = "\n2 J\n"
    return "\n".join(page.split("(AgriCtec Digital Identity) Tj")[0] for page in text_of(data).split(page_starts))


def pages(data: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b", data))


def summary(n: int) -> dict:
    zero = {"total": "0.00", "verified": "0.00", "selfReported": "0.00", "verifiedShare": "0"}
    return {
        "period": {"from": "2026-06-01", "to": "2026-11-30", "months": 6},
        "farm": {"income": zero, "expenses": zero, "net": "0.00"},
        "momo": {"in": zero, "out": zero, "net": "0.00"},
        "financing": {"in": zero, "out": zero},
        "monthly": [],
        "monthsWithRecords": 0,
        "counts": {"total": n, "verified": 0, "byStatus": {"self_reported": n}},
        "verifiedShare": {"byCount": "0", "byAmount": "0"},
        "obligations": {"disbursed": "0.00", "repaid": "0.00", "interestPaid": "0.00", "outstanding": "0.00"},
    }


def entries(n: int, description: str = "Maize sale") -> list[dict]:
    return [
        {"date": f"2026-{6 + i % 6:02d}-{1 + i % 27:02d}", "kind": "Income", "description": f"{description} {i + 1}",
         "amount": f"{i + 1}.25", "status": "self_reported", "source": "farmer_entry"}
        for i in range(n)
    ]


def report(n: int, **kw) -> bytes:
    return render(summary=summary(n), profile={"name": "Kofi Test"}, entries=kw.get("rows", entries(n)),
                  report_id=reference_for("abc12345", AT), generated_at=AT)


@pytest.mark.parametrize("n", [0, 1, 10, 50, 150])
def test_every_transaction_is_listed_and_numbered_without_gaps(n):
    data = report(n)
    text = body_of(data)
    # Row numbers are their own cells in the transactions table, in order, across every page.
    table = text[text.find("(Transaction details) Tj"):]
    positions = []
    for i in range(1, n + 1):
        at = table.find(f"({i}) Tj", positions[-1] + 1 if positions else 0)
        assert at >= 0, f"row {i} missing"
        positions.append(at)
    assert table.find(f"({n + 1}) Tj", positions[-1] if positions else 0) < 0
    text = text_of(data)
    if n:
        assert f"numbered 1 to {n}, earliest first" in text
    else:
        assert "No transactions were recorded in this period." in text
    if n >= 150:
        assert pages(data) > 3


def test_the_letterhead_and_footer_are_on_every_page():
    data = report(150)
    n = pages(data)
    text = text_of(data)
    assert text.count("FINANCIAL TRANSACTION STATEMENT") == n
    assert text.count("DIGITAL IDENTITY") == n
    assert text.count("AgriCtec Digital Identity") == n
    # "Page X of " then the total, filled in when the document is finished.
    assert [int(x) for x in re.findall(r"\(Page (\d+) of \) Tj", text)] == list(range(1, n + 1))
    assert text.count("Ref FR-20261115-ABC12345") == n
    assert b"/Subtype /Image" in data  # the AgriCtec mark
    # The disclaimer closes the report, once.
    assert text.count(DISCLAIMER.split(".")[0][:40]) == 1


def test_long_descriptions_wrap_instead_of_being_cut():
    long = "Sold forty bags of maize to the Ejisu aggregator after the second harvest, paid partly in cash"
    text = text_of(report(1, rows=entries(1, long)))
    for word in ("aggregator", "harvest,", "cash"):
        assert word in text


def test_months_without_records_say_so_and_nothing_is_invented():
    text = text_of(report(0))
    assert text.count("No records") >= 6 * 3  # six empty months, three columns each
    assert "GHS" in text and "Ghana Cedis" in text


def test_the_application_pack_has_the_same_letterhead_with_its_own_layout():
    data = render_pack(
        application={"reference": "LA-1", "lenderName": "Test Bank", "productSnapshot": {"name": "Input loan"},
                     "productVersion": 1, "amount": "100.00", "months": 3, "purpose": "Seed"},
        profile={"name": "Kofi Test"}, documents=[], consent=None, generated_at=AT,
    )
    text = text_of(data)
    assert "LOAN APPLICATION PACK" in text and "DIGITAL IDENTITY" in text and "(Page 1 of ) Tj" in text
    assert "LOAN APPLICATION" in text and "Documents attached" in text


def test_downloads_through_the_api_carry_the_letterhead_and_stay_private():
    ama, other = signup("pdf.owner@example.com"), signup("pdf.other@example.com")
    add_record(ama, {"type": "income", "description": "Maize sale", "amount": 120, "crop": "Maize",
                     "date": "2026-11-02", "paymentMethod": "cash"})
    add_momo(ama, {"direction": "in", "counterparty": "Buyer", "amount": "40.00", "date": "2026-11-03",
                   "network": "MTN", "reference": "MP261103.1200.T00001"})
    made = ama.post(f"{API}/me/reports/financial", json={"months": 3}).json()
    pdf = ama.get(f"{API}/me/reports/financial/{made['id']}/file").content
    text = text_of(pdf)
    assert "FARMER FINANCIAL SUMMARY" in text and "(1) Tj" in text and "(2) Tj" in text
    assert re.search(r"FR-\d{8}-[0-9A-F]{8}", text)
    assert other.get(f"{API}/me/reports/financial/{made['id']}/file").status_code == 404
    statement = text_of(ama.get(f"{API}/me/momo/statement").content)
    assert "MOBILE MONEY STATEMENT" in statement and "DIGITAL IDENTITY" in statement and "(1) Tj" in statement

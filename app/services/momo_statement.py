"""A private MoMo-only statement, using the same filters as the farmer's transaction table."""

import re
import secrets
from decimal import Decimal

from sqlalchemy import select

from app.domain.clock import add_months, month_start, now_utc, today
from app.domain.financial_report import STATUS_LABELS
from app.domain.pdf_brand import BrandedPDF, clean, ghs
from app.models import MomoTransaction, Profile


def matching_rows(db, user_id, *, period="all", network=None, direction=None, status=None, query="", newest_first=True):
    end = today()
    start = {"month": month_start(end), "3m": add_months(month_start(end), -2),
             "year": end.replace(month=1, day=1)}.get(period)
    statement = select(MomoTransaction).where(MomoTransaction.user_id == user_id)
    if start:
        statement = statement.where(MomoTransaction.date >= start)
    if network:
        statement = statement.where(MomoTransaction.network == network)
    if direction:
        statement = statement.where(MomoTransaction.direction == direction)
    if status:
        statement = statement.where(MomoTransaction.verification_status == status)
    rows = list(db.scalars(statement))
    needle = query.strip().lower()
    if needle:
        amount_text = needle.removeprefix("ghs").strip().replace(",", "")
        amount = Decimal(amount_text) if re.fullmatch(r"\d+(\.\d{1,2})?", amount_text) else None
        rows = [r for r in rows if r.amount == amount or any(needle in v.lower() for v in (
            r.counterparty, r.reference or "", r.network or "",
        ))]
    return sorted(rows, key=lambda r: (r.date, r.created_at), reverse=newest_first)


STATEMENT_NOTE = (
    "Prepared by AgriCtec from the Mobile Money entries recorded in the farmer's account. Not a bank "
    "statement and not a statement from the mobile money provider: entries are self-reported unless "
    "verified by AgriCtec review."
)
PERIODS = {"all": "All time", "month": "This month", "3m": "Last 3 months", "year": "This year"}


def render(db, user_id, **filters):
    """The statement on the AgriCtec letterhead: the account and the filters used, the totals,
    then every matching entry numbered 1, 2, 3... in the order the farmer chose."""
    rows = matching_rows(db, user_id, **filters)
    profile = db.get(Profile, user_id)
    generated = now_utc()
    reference = f"MS-{generated:%Y%m%d}-{secrets.token_hex(3).upper()}"
    pdf = BrandedPDF(title="MOBILE MONEY STATEMENT", reference=reference, generated_at=generated)
    pdf.add_page()
    pdf.document_title("MOBILE MONEY TRANSACTION STATEMENT")

    newest_first = filters.get("newest_first", True)
    pdf.details([
        ("Account holder", (profile.name if profile else "") or "Farmer account"),
        ("Farm name", profile.farm_name if profile else ""),
        ("Region", ", ".join(x for x in ((profile.constituency, profile.region) if profile else ()) if x)),
        ("Statement reference", reference),
        ("Period", PERIODS.get(filters.get("period", "all"), "All time")),
        ("Network", filters.get("network") or "All networks"),
        ("Direction", {"in": "Money in", "out": "Money out"}.get(filters.get("direction"), "Money in and out")),
        ("Verification", STATUS_LABELS.get(filters.get("status"), "All statuses")),
        ("Order", "Newest first" if newest_first else "Oldest first"),
    ] + ([("Search", filters["query"])] if filters.get("query") else []), columns=3)

    incoming = sum((r.amount for r in rows if r.direction == "in"), Decimal(0))
    outgoing = sum((r.amount for r in rows if r.direction == "out"), Decimal(0))
    pdf.section("Summary", keep=24)
    style = pdf.table_style(col_widths=(1, 1, 1, 1), text_align=("CENTER", "CENTER", "CENTER", "CENTER"))
    with pdf.table(**style) as table:
        table.row(["Transactions", "Received", "Sent", "Net"])
        table.row([str(len(rows)), ghs(incoming), ghs(outgoing), ghs(incoming - outgoing)])
    pdf.ln(1)
    count = f"{len(rows)} transaction{'' if len(rows) == 1 else 's'}"
    pdf.note(clean(f"{count} | Received {ghs(incoming)} | Sent {ghs(outgoing)} | Net {ghs(incoming - outgoing)}"),
             italic=False)

    pdf.section("Transactions", keep=20)
    if not rows:
        pdf.note("No recorded MoMo transactions match these filters.", italic=False)
    else:
        style = pdf.table_style(
            col_widths=(9, 21, 44, 18, 24, 30, 30),
            text_align=("CENTER", "LEFT", "LEFT", "LEFT", "RIGHT", "LEFT", "LEFT"),
            line_height=4.3, font_size=8,
        )
        with pdf.table(**style) as table:
            table.row(["No.", "Date", "Counterparty", "Type", "Amount (GHS)", "Status", "Reference"])
            for number, item in enumerate(rows, start=1):
                table.row([
                    str(number),
                    item.date.strftime("%d %b %Y"),
                    clean(item.counterparty) + (f"\n{clean(item.network)}" if item.network else ""),
                    "Received" if item.direction == "in" else "Sent",
                    ghs(item.amount, prefix=False),
                    STATUS_LABELS.get(item.verification_status, "Self-reported"),
                    clean(item.reference or "Not provided"),
                ])
    pdf.disclaimer(STATEMENT_NOTE)
    return bytes(pdf.output())

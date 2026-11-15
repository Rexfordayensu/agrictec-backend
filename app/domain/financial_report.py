"""The farmer's Financial Summary PDF (fpdf2), on the AgriCtec letterhead (app/domain/pdf_brand.py).

Only recorded figures. Every transaction in the period is listed and numbered 1, 2, 3... from
the earliest, the numbering running on across pages; each is marked Verified (by an AgriCtec
reviewer, from evidence) or Self-reported, with its source and reference. Months without any
records say so instead of showing zero. It is not a bank statement, not certified and not a
loan decision, and says so on its last page."""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from fpdf.enums import XPos, YPos

from app.domain.pdf_brand import MUTED, TOTAL_ROW, BrandedPDF, clean, ghs

STATUS_LABELS = {
    "verified": "Verified by AgriCtec",
    "self_reported": "Self-reported",
    "evidence_submitted": "Self-reported (evidence attached)",
    "verification_pending": "Self-reported (under review)",
    "rejected": "Self-reported (review rejected)",
    "disputed": "Self-reported (disputed)",
}
SOURCE_LABELS = {"farmer_entry": "Entered by farmer", "statement_upload": "From an uploaded statement"}
# Kept for the documents that still print a one-line note (e.g. the MoMo statement).
FOOTER = (
    "Prepared by AgriCtec from records in the farmer's account. Not a bank statement, not a "
    "certified document and not a loan decision."
)

__all__ = ["FOOTER", "SOURCE_LABELS", "STATUS_LABELS", "clean", "ghs", "render", "reference_for"]


def reference_for(report_id: str, generated_at: datetime) -> str:
    """The report's printed reference, from its stored ID and generation date."""
    return f"FR-{generated_at:%Y%m%d}-{report_id.replace('-', '')[:8].upper()}"


def _months(start: date, end: date) -> list[str]:
    keys, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def _month_label(key: str) -> str:
    return datetime.strptime(key, "%Y-%m").strftime("%b %Y")


def _date(value: str) -> str:
    return date.fromisoformat(value).strftime("%d %b %Y")


def _details(entry: dict) -> str:
    """The description, then where it came from (reference, network or bank, source) and when
    it was recorded in AgriCtec (an entry typed long after the event is easy to spot)."""
    extra = [
        f"Ref {entry['reference']}" if entry.get("reference") else None,
        entry.get("network") or entry.get("bank") or None,
        SOURCE_LABELS.get(entry.get("source"), entry.get("source")),
    ]
    lines = [clean(entry["description"]), clean(" - ".join(x for x in extra if x))]
    if entry.get("recorded"):
        lines.append(clean(f"Recorded {entry['recorded']} GMT"))
    return "\n".join(line for line in lines if line)


def render(
    *,
    summary: dict,
    profile: dict,
    entries: list[dict],
    report_id: str,
    generated_at: datetime,
) -> bytes:
    """`report_id` is the printed reference (reference_for); `entries` are listed in the order
    given (the service sends them oldest first)."""
    pdf = BrandedPDF(title="FINANCIAL TRANSACTION STATEMENT", reference=report_id, generated_at=generated_at)
    pdf.add_page()
    period = summary["period"]
    start, end = date.fromisoformat(period["from"]), date.fromisoformat(period["to"])

    # ---------- Title and report details ----------
    pdf.document_title("FARMER FINANCIAL SUMMARY")
    pdf.details([
        ("Report reference", report_id),
        ("Reporting period", f"{start:%d %b %Y} to {end:%d %b %Y} ({period['months']} months)"),
        ("Date generated", f"{generated_at:%d %B %Y}"),
        ("Time generated", f"{generated_at:%H:%M} GMT"),
        ("Currency", "Ghana Cedis (GHS)"),
        ("Transactions listed", f"{len(entries)}"),
    ], columns=3)

    # ---------- Farmer ----------
    pdf.section("Farmer information")
    place = ", ".join(x for x in (profile.get("community"), profile.get("constituency"), profile.get("region")) if x)
    acres = profile.get("acres")
    pdf.details([
        ("Farmer's full name", profile.get("name")),
        ("Farm name", profile.get("farm_name")),
        ("Region and location", place),
        ("Farm size", f"{acres:g} acres" if acres else ""),
        ("Main crops", ", ".join(profile.get("primary_crops") or [])),
        ("Farm type", profile.get("farm_type")),
    ])

    # ---------- Financial summary ----------
    pdf.section("Financial summary", keep=60)
    farm, momo, financing = summary["farm"], summary["momo"], summary["financing"]
    rows = [
        ("Total farm income", farm["income"]),
        ("Total farm expenses", farm["expenses"]),
        ("Mobile Money inflows", momo["in"]),
        ("Mobile Money outflows", momo["out"]),
        ("Financing received", financing["in"]),
        ("Financing repaid", financing["out"]),
    ]
    style = pdf.table_style(col_widths=(46, 30, 30, 30), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"))
    with pdf.table(**style) as table:
        table.row(["Item", "Total (GHS)", "Verified (GHS)", "Self-reported (GHS)"])
        for label, item in rows:
            table.row([label, ghs(item["total"], prefix=False), ghs(item["verified"], prefix=False),
                       ghs(item["selfReported"], prefix=False)])
        net = Decimal(farm["net"])
        table.row(["Net farm cash flow", ghs(net, prefix=False), "", ""], style=TOTAL_ROW)
    pdf.ln(1)
    covered = summary.get("monthsWithRecords", 0)
    pdf.note(
        f"Records exist in {covered} of the {period['months']} months of this period. Figures are the "
        "amounts recorded; nothing is projected, estimated or annualised. \"Verified\" means an "
        "AgriCtec reviewer checked the entry against its evidence; it is not a bank confirmation."
    )

    # ---------- Monthly breakdown ----------
    pdf.section("Monthly financial breakdown", keep=40)
    by_month = {m["month"]: m for m in summary.get("monthly", [])}
    farm_kinds = {"Income", "Expense", "Farm activity", "Financing in", "Financing out"}
    active = Counter(e["date"][:7] for e in entries if e.get("kind") in farm_kinds)
    style = pdf.table_style(col_widths=(34, 34, 34, 34), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"))
    with pdf.table(**style) as table:
        table.row(["Month", "Total income (GHS)", "Total expenses (GHS)", "Net cash flow (GHS)"])
        for key in _months(start, end):
            m = by_month.get(key)
            if not active.get(key) and not m:
                table.row([_month_label(key), "No records", "No records", "No records"])
                continue
            m = m or {"income": "0", "expenses": "0", "net": "0"}
            table.row([_month_label(key), ghs(m["income"], prefix=False), ghs(m["expenses"], prefix=False),
                       ghs(m["net"], prefix=False)])
    pdf.ln(1)
    pdf.note("\"No records\" means nothing was recorded that month (not that the farm earned nothing). "
             "Monthly figures cover farm records; Mobile Money entries are listed with the transactions.")

    # ---------- Financing ----------
    pdf.section("Loan and financing information", keep=36)
    o = summary["obligations"]
    style = pdf.table_style(col_widths=(70, 40), text_align=("LEFT", "RIGHT"))
    with pdf.table(**style) as table:
        table.row(["As recorded in AgriCtec", "Amount (GHS)"])
        table.row(["Recorded loan disbursements", ghs(o["disbursed"], prefix=False)])
        table.row(["Recorded repayments", ghs(o["repaid"], prefix=False)])
        table.row(["Recorded interest payments", ghs(o["interestPaid"], prefix=False)])
        table.row(["Recorded outstanding obligations", ghs(o["outstanding"], prefix=False)])
    pdf.ln(1)
    pdf.note("These figures come from the records in the farmer's AgriCtec account. They are not balances "
             "confirmed by any lender.")

    # ---------- Verification ----------
    pdf.section("Transaction verification summary", keep=40)
    counts = summary["counts"]
    by_status = counts.get("byStatus", {})
    share = summary.get("verifiedShare", {})
    style = pdf.table_style(col_widths=(80, 30), text_align=("LEFT", "RIGHT"))
    with pdf.table(**style) as table:
        table.row(["Entries in the period", "Count"])
        table.row(["Total transaction entries", str(counts["total"])])
        table.row([f"Verified by AgriCtec review ({share.get('byCount', '0')}% of entries, "
                   f"{share.get('byAmount', '0')}% of amounts)", str(counts["verified"])])
        table.row(["Self-reported, not checked", str(by_status.get("self_reported", 0))])
        table.row(["Evidence submitted, not yet sent for review", str(by_status.get("evidence_submitted", 0))])
        table.row(["Under review", str(by_status.get("verification_pending", 0))])
        table.row(["Rejected or disputed", str(by_status.get("rejected", 0) + by_status.get("disputed", 0))])
    pdf.ln(1)
    pdf.note("AgriCtec's review checks an entry against the evidence the farmer provided. It is not a "
             "verification by a bank or a mobile money provider. Unchecked entries stay self-reported.")

    # ---------- Every transaction, numbered ----------
    pdf.section("Transaction details", keep=25)
    if not entries:
        pdf.note("No transactions were recorded in this period.", italic=False)
    else:
        style = pdf.table_style(
            col_widths=(9, 21, 66, 22, 24, 30),
            text_align=("CENTER", "LEFT", "LEFT", "LEFT", "RIGHT", "LEFT"),
            line_height=4.3, font_size=8,
        )
        with pdf.table(**style) as table:
            table.row(["No.", "Date", "Transaction details", "Type", "Amount (GHS)", "Status"])
            for number, entry in enumerate(entries, start=1):
                table.row([
                    str(number),
                    _date(entry["date"]),
                    _details(entry),
                    clean(entry["kind"]),
                    ghs(entry["amount"], prefix=False),
                    STATUS_LABELS.get(entry["status"], clean(entry["status"])),
                ])
        pdf.ln(1)
        pdf.set_font("Helvetica", size=8)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 4, clean(f"{len(entries)} transactions listed, numbered 1 to {len(entries)}, earliest first."),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.disclaimer()
    return bytes(pdf.output())

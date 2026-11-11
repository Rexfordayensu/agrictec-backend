"""The farmer's financial report as a PDF (fpdf2), from the financial summary and the entries.

Only recorded figures: every entry is marked Verified (by an AgriCtec reviewer, from evidence)
or Self-reported, with its source. The report states its period, when it was generated and that
AgriCtec prepared it. It is not a bank statement, not certified, and makes no claim about loans.

The built-in PDF fonts only cover Latin-1, so other characters are mapped to close equivalents
(or "?") rather than failing."""

from datetime import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

STATUS_LABELS = {
    "verified": "Verified",
    "self_reported": "Self-reported",
    "evidence_submitted": "Self-reported (evidence attached)",
    "verification_pending": "Self-reported (under review)",
    "rejected": "Self-reported (review rejected)",
    "disputed": "Self-reported (disputed)",
}
SOURCE_LABELS = {"farmer_entry": "Entered by farmer", "statement_upload": "From an uploaded statement"}
FOOTER = (
    "Prepared by AgriCtec from records in the farmer's account. Not a bank statement, not a "
    "certified document and not a loan decision."
)
MAX_ROWS = 400
_SWAPS = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
          "·": "-", "•": "-", "₵": "GHS ", "…": "..."}


def clean(text: object) -> str:
    """Text the core PDF fonts can print."""
    value = "" if text is None else str(text)
    for a, b in _SWAPS.items():
        value = value.replace(a, b)
    return value.encode("latin-1", "replace").decode("latin-1")


def ghs(amount: str) -> str:
    """"1234.50" -> "GHS 1,234.50" (the exact figure, never rounded away)."""
    whole, _, cents = amount.partition(".")
    sign = "-" if whole.startswith("-") else ""
    return f"{sign}GHS {int(whole.lstrip('-')):,}.{(cents + '00')[:2]}"


class _Report(FPDF):
    report_id = ""

    def footer(self) -> None:
        self.set_y(-18)
        self.set_font("Helvetica", size=7)
        self.set_text_color(90, 90, 90)
        self.multi_cell(0, 3.5, clean(FOOTER), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.cell(0, 4, clean(f"Report {self.report_id} - page {self.page_no()}"), align="R")


def render(
    *,
    summary: dict,
    profile: dict,
    entries: list[dict],
    report_id: str,
    generated_at: datetime,
) -> bytes:
    pdf = _Report(format="A4")
    pdf.report_id = report_id
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.set_title("AgriCtec financial summary")
    pdf.set_author("AgriCtec")
    pdf.add_page()

    def heading(text: str) -> None:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(15, 80, 60)
        pdf.cell(0, 7, clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(20, 20, 20)
        pdf.set_font("Helvetica", size=9)

    def pairs(rows: list[tuple[str, str]]) -> None:
        for label, value in rows:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(55, 5.5, clean(label))
            pdf.set_font("Helvetica", size=9)
            pdf.multi_cell(0, 5.5, clean(value or "Not provided"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "AgriCtec financial summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    period = summary["period"]
    pdf.set_font("Helvetica", size=9)
    pdf.cell(
        0, 5,
        clean(f"Period {period['from']} to {period['to']} ({period['months']} months) - generated "
              f"{generated_at.strftime('%d %b %Y %H:%M')} GMT - report {report_id}"),
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
    )

    heading("Farmer")
    pairs([
        ("Name", profile.get("name", "")),
        ("Farm", profile.get("farm_name", "")),
        ("Region", ", ".join(x for x in (profile.get("constituency"), profile.get("region")) if x)),
        ("Main crops", ", ".join(profile.get("primary_crops") or [])),
        ("Farm size", f"{profile['acres']} acres" if profile.get("acres") else ""),
    ])

    heading("Recorded totals")
    farm, momo, financing = summary["farm"], summary["momo"], summary["financing"]

    def line(name: str, item: dict) -> tuple[str, str]:
        return name, f"{ghs(item['total'])}  (verified {ghs(item['verified'])}, {item['verifiedShare']}%)"

    pairs([
        line("Farm income", farm["income"]),
        line("Farm expenses", farm["expenses"]),
        ("Net farm cash flow", ghs(farm["net"])),
        line("Mobile Money in", momo["in"]),
        line("Mobile Money out", momo["out"]),
        line("Loans and financing in", financing["in"]),
        line("Loans and financing out", financing["out"]),
    ])
    if summary.get("insufficient"):
        pdf.set_font("Helvetica", "I", 8)
        pdf.multi_cell(
            0, 4.5,
            clean(f"Records cover {summary['monthsWithRecords']} of the {period['months']} months; "
                  "figures are not projected or annualised."),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    heading("Month by month (farm records)")
    with pdf.table(col_widths=(30, 50, 50, 50), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
                   first_row_as_headings=True, line_height=5.5) as table:
        table.row(["Month", "Income", "Expenses", "Net"])
        for m in summary["monthly"]:
            table.row([m["month"], ghs(m["income"]), ghs(m["expenses"]), ghs(m["net"])])

    heading("Loans as recorded by the farmer")
    o = summary["obligations"]
    pairs([
        ("Disbursements recorded", ghs(o["disbursed"])),
        ("Repayments recorded", ghs(o["repaid"])),
        ("Interest paid recorded", ghs(o["interestPaid"])),
        ("Outstanding, as recorded", ghs(o["outstanding"])),
    ])

    counts = summary["counts"]
    heading("How these figures are supported")
    by_status = counts["byStatus"]
    pairs([
        ("Entries in the period", str(counts["total"])),
        ("Verified by AgriCtec", f"{counts['verified']} ({summary['verifiedShare']['byCount']}% of entries, "
                                 f"{summary['verifiedShare']['byAmount']}% of amounts)"),
        ("Self-reported", str(counts["total"] - counts["verified"])),
        ("  with evidence, not yet reviewed", str(by_status.get("evidence_submitted", 0))),
        ("  under review", str(by_status.get("verification_pending", 0))),
        ("  review rejected or disputed", str(by_status.get("rejected", 0) + by_status.get("disputed", 0))),
    ])

    heading("Entries")
    shown = entries[:MAX_ROWS]
    with pdf.table(col_widths=(22, 22, 58, 30, 38, 30), first_row_as_headings=True,
                   text_align=("LEFT", "LEFT", "LEFT", "RIGHT", "LEFT", "LEFT"), line_height=4.5) as table:
        table.row(["Date", "Kind", "Description", "Amount", "Status", "Source"])
        for e in shown:
            table.row([
                e["date"], clean(e["kind"]), clean(e["description"])[:80], ghs(e["amount"]),
                STATUS_LABELS.get(e["status"], e["status"]), SOURCE_LABELS.get(e["source"], e["source"]),
            ])
    if len(entries) > MAX_ROWS:
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 5, clean(f"{len(entries) - MAX_ROWS} more entries are not listed here."),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())

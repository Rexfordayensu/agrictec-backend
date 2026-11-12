"""A private MoMo-only statement, using the same filters as the farmer's transaction table."""

from decimal import Decimal
import re

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from sqlalchemy import select

from app.domain.clock import add_months, month_start, today
from app.domain.financial_report import FOOTER, STATUS_LABELS, clean, ghs
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


class Statement(FPDF):
    def footer(self):
        self.set_y(-16)
        self.set_font("Helvetica", size=7)
        self.set_text_color(85, 95, 95)
        self.multi_cell(0, 3.5, clean(FOOTER), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.cell(0, 4, f"Page {self.page_no()}", align="R")


def render(db, user_id, **filters):
    rows = matching_rows(db, user_id, **filters)
    profile = db.get(Profile, user_id)
    pdf = Statement(orientation="L")
    pdf.set_auto_page_break(True, margin=22)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_text_color(0, 110, 75)
    pdf.cell(0, 10, "AgriCtec - MoMo Transaction Statement", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(20, 30, 30)
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 6, clean(f"{profile.farm_name or profile.name if profile else 'Farmer account'} | Generated {today().isoformat()} (Ghana time)"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    labels = {"all": "All time", "month": "This month", "3m": "Last 3 months", "year": "This year"}
    description = " | ".join(str(v) for v in (
        labels.get(filters.get("period", "all")), filters.get("network"),
        {"in": "Money in", "out": "Money out"}.get(filters.get("direction")),
        STATUS_LABELS.get(filters.get("status")), f"Search: {filters['query']}" if filters.get("query") else None,
    ) if v)
    pdf.multi_cell(0, 6, clean(description), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    incoming = sum((r.amount for r in rows if r.direction == "in"), Decimal(0))
    outgoing = sum((r.amount for r in rows if r.direction == "out"), Decimal(0))
    pdf.set_font("Helvetica", "B", 10)
    pdf.multi_cell(0, 7, clean(f"{len(rows)} transactions | Received {ghs(f'{incoming:.2f}')} | Sent {ghs(f'{outgoing:.2f}')} | Net {ghs(f'{incoming - outgoing:.2f}')}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)
    pdf.set_font("Helvetica", size=8)
    with pdf.table(col_widths=(22, 55, 23, 20, 25, 48, 40), line_height=5, padding=2) as table:
        header = table.row()
        for label in ("Date", "Description", "Network", "Type", "Amount (GHS)", "Verification", "Reference"):
            header.cell(label)
        for item in rows:
            row = table.row()
            for value in (item.date.isoformat(), item.counterparty, item.network or "Not provided",
                          "Received" if item.direction == "in" else "Sent", f"{item.amount:,.2f}",
                          STATUS_LABELS.get(item.verification_status, "Self-reported"), item.reference or "Not provided"):
                row.cell(clean(value))
    if not rows:
        pdf.multi_cell(0, 7, "No recorded MoMo transactions match these filters.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())

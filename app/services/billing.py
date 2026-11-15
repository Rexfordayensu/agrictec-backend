"""A farmer's billing history and payment receipts.

Real payments are the `payments` table (Paystack: pending, paid or failed). A demo payment
(simulated, development only) is kept only on the subscription row, so it is listed too, clearly
marked as simulated. Nothing else is invented: no payment, amount or status that is not stored."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.errors import NotFound
from app.domain.clock import iso, now_utc
from app.domain.pdf_brand import TOTAL_ROW, BrandedPDF, clean, ghs
from app.models import Payment, Profile, Subscription
from app.services import platform_store

_PLAN_FALLBACK = {"free": "Free", "basic": "Basic", "premium": "Premium", "advanced": "Advanced"}


def _plan_names(db: DB) -> dict[str, str]:
    names = dict(_PLAN_FALLBACK)
    for plan in platform_store.plans(db):
        names[plan.id] = plan.name
    return names


def _money(value) -> str:
    return f"{float(value):.2f}"


def history(db: DB, user_id: str) -> list[dict]:
    """Newest first: every Paystack payment, plus the current simulated one (if any)."""
    names = _plan_names(db)
    rows = db.scalars(select(Payment).where(Payment.user_id == user_id).order_by(Payment.created_at.desc())).all()
    items = [
        {
            "reference": p.reference,
            "planId": p.plan_id,
            "planName": names.get(p.plan_id, p.plan_id),
            "amountGHS": _money(p.amount),
            "currency": p.currency,
            "method": p.method,
            "status": p.status,
            "createdAt": iso(p.created_at),
            "paidAt": iso(p.paid_at) if p.paid_at else None,
            "channel": (p.provider_data or {}).get("channel"),
        }
        for p in rows
    ]
    sub = db.get(Subscription, user_id)
    demo = (sub.payment or {}) if sub else {}
    if demo.get("reference") and demo.get("status", "simulated") == "simulated":
        items.append({
            "reference": demo["reference"],
            "planId": demo.get("planId"),
            "planName": names.get(demo.get("planId"), demo.get("planId")),
            "amountGHS": _money(demo.get("amountGHS", 0)),
            "currency": "GHS",
            "method": demo.get("method"),
            "status": "simulated",
            "createdAt": demo.get("createdAt"),
            "paidAt": None,
            "channel": None,
        })
    items.sort(key=lambda item: item["createdAt"] or "", reverse=True)
    return items


_STATUS = {
    "paid": "Paid",
    "simulated": "Simulated (demo) - no money was charged",
    "pending": "Pending - not paid yet",
    "failed": "Failed - no money was taken",
}


def receipt(db: DB, user_id: str, reference: str) -> tuple[str, bytes, str]:
    """A PDF receipt for one of the farmer's own payments. 404 for anyone else's."""
    item = next((p for p in history(db, user_id) if p["reference"] == reference), None)
    if item is None:
        raise NotFound("This payment was not found.")
    profile = db.get(Profile, user_id)
    paid = item["status"] == "paid"
    when = item["paidAt"] or item["createdAt"] or ""
    pdf = BrandedPDF(title="PAYMENT RECEIPT" if paid else "PAYMENT RECORD",
                     reference=item["reference"], generated_at=now_utc())
    pdf.add_page()
    pdf.document_title("Payment receipt" if paid else "Payment record",
                       "For an AgriCtec plan subscription, from AgriCtec's billing records.")
    method = {"mobile-money": "Mobile money", "card": "Card"}.get(item["method"] or "", item["method"] or "Not recorded")
    if item["channel"]:
        method += f" ({str(item['channel']).replace('_', ' ')})"
    amount = Decimal(str(item["amountGHS"] or "0"))

    pdf.section("Billed to", keep=20)
    pdf.details([
        ("Name", profile.name if profile else ""),
        ("Farm", profile.farm_name if profile else ""),
    ])

    pdf.section("Items", keep=26)
    style = pdf.table_style(col_widths=(90, 40, 40), text_align=("LEFT", "LEFT", "RIGHT"))
    with pdf.table(**style) as table:
        table.row(["Description", "Period", "Amount (GHS)"])
        table.row([clean(f"AgriCtec {item['planName']} plan"), "One month", ghs(amount, prefix=False)])
        table.row(["Total", "", ghs(amount, prefix=False)], style=TOTAL_ROW)

    pdf.section("Payment details", keep=24)
    pdf.details([
        ("Receipt reference", item["reference"]),
        ("Status", _STATUS.get(item["status"], item["status"])),
        ("Payment method", method),
        ("Date", _date(when)),
        ("Currency", "Ghana Cedis (GHS)" if item["currency"] == "GHS" else item["currency"]),
        ("Amount", ghs(amount)),
    ], columns=3)
    note = (
        "This simulated payment was made in AgriCtec's demo mode. It is not a real payment and no money was charged."
        if item["status"] == "simulated"
        else "Payments are processed by Paystack. AgriCtec never sees your card or mobile money wallet details."
    )
    pdf.disclaimer(note)
    name = f"AgriCtec_Receipt_{item['reference']}.pdf"
    return name, bytes(pdf.output()), "application/pdf"


def _date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, AttributeError):
        return value or "-"

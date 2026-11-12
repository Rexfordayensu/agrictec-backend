"""The application pack: one PDF summarising a farmer's loan application for the lender, sent
with the transaction statement and the documents the farmer uploaded. Pure (fpdf2).

It states what the farmer asked for and provided, AgriCtec's preliminary check and the consent
behind sharing it. It is not a loan decision, and it never says the farmer qualifies."""

from datetime import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.domain.financial_report import clean, ghs

FOOTER = (
    "Prepared by AgriCtec at the farmer's request and with the farmer's consent. AgriCtec's check "
    "is preliminary and not a loan decision: the lender makes every decision."
)


class _Pack(FPDF):
    reference = ""

    def footer(self) -> None:
        self.set_y(-16)
        self.set_font("Helvetica", size=7)
        self.set_text_color(90, 90, 90)
        self.multi_cell(0, 3.5, clean(FOOTER), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.cell(0, 4, clean(f"Application {self.reference} - page {self.page_no()}"), align="R")


def render(*, application: dict, profile: dict, documents: list[dict], consent: dict | None,
           generated_at: datetime) -> bytes:
    pdf = _Pack(format="A4")
    pdf.reference = application["reference"]
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title(f"AgriCtec loan application {application['reference']}")
    pdf.set_author("AgriCtec")
    pdf.add_page()

    def heading(text: str) -> None:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(5, 120, 85)
        pdf.cell(0, 7, clean(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(20, 20, 20)

    def rows(pairs: list[tuple[str, str]]) -> None:
        pdf.set_font("Helvetica", size=9)
        for label, value in pairs:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(55, 5.5, clean(label))
            pdf.set_font("Helvetica", size=9)
            pdf.multi_cell(0, 5.5, clean(value or "Not provided"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "AgriCtec loan application", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, clean(f"Reference {application['reference']} - prepared {generated_at:%d %b %Y %H:%M} GMT"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(20, 20, 20)

    product = application["productSnapshot"]
    heading("Application")
    rows([
        ("Lender", application["lenderName"]),
        ("Loan product", f"{product.get('name')} (version {application['productVersion']})"),
        ("Amount requested", ghs(application["amount"])),
        ("Repayment period", f"{application['months']} months"),
        ("Purpose", application["purpose"]),
    ])

    heading("Farmer")
    rows([
        ("Name", profile.get("name")),
        ("Phone", profile.get("phone")),
        ("Farm", profile.get("farm_name")),
        ("Location", ", ".join(x for x in (profile.get("constituency"), profile.get("region")) if x)),
        ("Main crops", ", ".join(profile.get("primary_crops") or [])),
        ("Farm size", f"{profile.get('acres')} acres" if profile.get("acres") else None),
        ("Ghana Card", application.get("identityLabel")),
    ])

    if application.get("letter"):
        heading("Application letter")
        pdf.set_font("Helvetica", size=9.5)
        pdf.multi_cell(0, 5, clean(application["letter"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    answers = application.get("answerRows") or []
    if answers:
        heading("Answers to the lender's requirements")
        rows([(a["name"], a["value"]) for a in answers])

    heading("Documents attached")
    if documents:
        rows([(d["requirementName"], d["filename"]) for d in documents])
    else:
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 5.5, "None.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    assessment = application.get("assessment")
    if assessment:
        heading("AgriCtec's preliminary check (not a loan decision)")
        figures = assessment.get("figures") or {}
        period = assessment.get("period") or {}
        rows([
            ("Result", assessment.get("outcomeLabel")),
            ("Summary", assessment.get("summary")),
            ("Period assessed", f"{period.get('months')} months, {period.get('from')} to {period.get('to')}"),
            ("Documented income", ghs(figures.get("income", "0.00"))),
            ("Documented expenses", ghs(figures.get("expenses", "0.00"))),
            ("Net cash flow", ghs(figures.get("net", "0.00"))),
            ("Verified share", f"{figures.get('verifiedShare')}% of income and costs"
             if figures.get("verifiedShare") is not None else "No income or costs recorded"),
        ])
    if application.get("statementReference"):
        pdf.set_font("Helvetica", "I", 8.5)
        pdf.multi_cell(0, 4.5, clean(
            f"The transaction statement (report {application['statementReference']}) is attached: it marks every "
            "entry as verified by AgriCtec or self-reported."), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if consent:
        heading("Consent to share")
        rows([
            ("Given on", consent["givenAt"]),
            ("Shared with", application["lenderName"]),
            ("Purpose", consent["purpose"]),
            ("Information shared", "; ".join(consent["dataItems"])),
        ])
    return bytes(pdf.output())

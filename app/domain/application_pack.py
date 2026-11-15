"""The application pack: one PDF summarising a farmer's loan application for the lender, sent
with the transaction statement and the documents the farmer uploaded. Pure (fpdf2), on the
AgriCtec letterhead (app/domain/pdf_brand.py).

It states what the farmer asked for and provided, AgriCtec's preliminary check and the consent
behind sharing it. It is not a loan decision, and it never says the farmer qualifies."""

from datetime import datetime

from app.domain.pdf_brand import TOTAL_ROW, BrandedPDF, clean, ghs

FOOTER = (
    "Prepared by AgriCtec at the farmer's request and with the farmer's consent. AgriCtec's check "
    "is preliminary and not a loan decision: the lender makes every decision."
)


def render(*, application: dict, profile: dict, documents: list[dict], consent: dict | None,
           generated_at: datetime) -> bytes:
    pdf = BrandedPDF(title="LOAN APPLICATION PACK", reference=application["reference"], generated_at=generated_at)
    pdf.add_page()
    product = application["productSnapshot"]
    pdf.document_title(
        "LOAN APPLICATION",
        f"Prepared for {application['lenderName']} at the farmer's request.",
    )

    pdf.section("Application")
    pdf.details([
        ("Application reference", application["reference"]),
        ("Lender", application["lenderName"]),
        ("Loan product", f"{product.get('name')} (version {application['productVersion']})"),
        ("Amount requested", ghs(application["amount"])),
        ("Repayment period", f"{application['months']} months"),
        ("Purpose", application["purpose"]),
    ])

    pdf.section("Applicant")
    acres = profile.get("acres")
    pdf.details([
        ("Full name", profile.get("name")),
        ("Phone", profile.get("phone")),
        ("Farm name", profile.get("farm_name")),
        ("Location", ", ".join(x for x in (profile.get("constituency"), profile.get("region")) if x)),
        ("Main crops", ", ".join(profile.get("primary_crops") or [])),
        ("Farm size", f"{acres:g} acres" if acres else None),
        ("Ghana Card", application.get("identityLabel")),
    ])

    if application.get("letter"):
        pdf.section("Application letter", keep=20)
        pdf.set_font("Helvetica", size=9.5)
        pdf.multi_cell(0, 5, clean(application["letter"]), align="L")
        pdf.ln(1)

    answers = application.get("answerRows") or []
    if answers:
        pdf.section("Answers to the lender's requirements", keep=20)
        style = pdf.table_style(col_widths=(60, 110), text_align=("LEFT", "LEFT"))
        with pdf.table(**style) as table:
            table.row(["Requirement", "Answer"])
            for a in answers:
                table.row([clean(a["name"]), clean(a["value"] or "Not provided")])

    pdf.section("Documents attached", keep=18)
    if documents:
        style = pdf.table_style(col_widths=(10, 80, 80), text_align=("CENTER", "LEFT", "LEFT"))
        with pdf.table(**style) as table:
            table.row(["No.", "Requirement", "File"])
            for number, d in enumerate(documents, start=1):
                table.row([str(number), clean(d["requirementName"]), clean(d["filename"])])
    else:
        pdf.note("No documents were attached.", italic=False)

    assessment = application.get("assessment")
    if assessment:
        pdf.section("AgriCtec's preliminary check (not a loan decision)", keep=40)
        figures = assessment.get("figures") or {}
        period = assessment.get("period") or {}
        pdf.details([
            ("Result", assessment.get("outcomeLabel")),
            ("Period assessed", f"{period.get('months')} months, {period.get('from')} to {period.get('to')}"),
        ])
        if assessment.get("summary"):
            pdf.note(assessment["summary"], italic=False)
        style = pdf.table_style(col_widths=(90, 50), text_align=("LEFT", "RIGHT"))
        with pdf.table(**style) as table:
            table.row(["Documented in AgriCtec", "Amount (GHS)"])
            table.row(["Income", ghs(figures.get("income", "0.00"), prefix=False)])
            table.row(["Expenses", ghs(figures.get("expenses", "0.00"), prefix=False)])
            table.row(["Net cash flow", ghs(figures.get("net", "0.00"), prefix=False)], style=TOTAL_ROW)
        pdf.ln(1)
        share = figures.get("verifiedShare")
        pdf.note(f"{share}% of the income and costs are verified by AgriCtec review." if share is not None
                 else "No income or costs were recorded in the period assessed.")
    if application.get("statementReference"):
        pdf.note(f"The transaction statement (report {application['statementReference']}) is attached: it marks "
                 "every entry as verified by AgriCtec or self-reported.")

    if consent:
        pdf.section("Consent to share", keep=26)
        pdf.details([
            ("Given on", consent["givenAt"]),
            ("Shared with", application["lenderName"]),
            ("Purpose", consent["purpose"]),
            ("Information shared", "; ".join(consent["dataItems"])),
        ])

    pdf.disclaimer(FOOTER)
    return bytes(pdf.output())

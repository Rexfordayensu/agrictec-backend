"""The signed-in farmer's own data (/me). Reads work in the admin's read-only view; every write
needs the farmer's own session."""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import Field
from sqlalchemy.orm import Session as DB

from app.core.deps import FarmerContext, farmer_context, writable_farmer
from app.core.ratelimit import rate_limit
from app.db import get_db
from app.domain.diagnosis import MAX_IMAGE_BYTES
from app.routers.common import set_onboarded_hint
from app.schemas import farm as f
from app.schemas.base import CamelModel, Confirmation, Message
from app.schemas.platform import Announcement
from app.routers.transactions import attachment
from app.services import account, farm, financial_reports, financial_summary, lending, loan_eligibility, notifications, phone_verification, subscriptions, tools, verifications

router = APIRouter(prefix="/me", tags=["Farmer"])

Reader = Annotated[FarmerContext, Depends(farmer_context)]
Writer = Annotated[FarmerContext, Depends(writable_farmer)]
Database = Annotated[DB, Depends(get_db)]

# ---------- Profile and onboarding ----------


@router.get("/profile", response_model=f.Profile)
def get_profile(ctx: Reader, db: Database):
    return farm.get_profile(db, ctx.user.id)


@router.put("/profile", response_model=f.Profile)
def save_profile(body: f.ProfileFields, ctx: Writer, db: Database):
    """The editable fields. A changed phone number loses its verification; the Ghana Card and
    the verification flags change only through /me/verifications and the admin's review."""
    return farm.save_profile(db, ctx.user.id, body)


@router.get("/onboarding/draft", response_model=f.OnboardingDraft | None)
def get_draft(ctx: Reader, db: Database):
    return farm.get_draft(db, ctx.user.id)


@router.put("/onboarding/draft", response_model=f.OnboardingDraft)
def save_draft(body: f.OnboardingDraftInput, ctx: Writer, db: Database):
    return farm.save_draft(db, ctx.user.id, body)


@router.delete("/onboarding/draft", status_code=204)
def clear_draft(ctx: Writer, db: Database):
    farm.clear_draft(db, ctx.user.id)


@router.post("/onboarding/complete", response_model=f.Profile)
def complete_onboarding(body: f.OnboardingComplete, response: Response, ctx: Writer, db: Database):
    profile = account.complete_onboarding(db, ctx.user, body)
    set_onboarded_hint(response, True)
    return profile


# ---------- Records ----------


@router.get("/records", response_model=list[f.FarmRecord])
def list_records(ctx: Reader, db: Database):
    return farm.list_records(db, ctx.user.id)


@router.post("/records", response_model=f.FarmRecord, status_code=201)
def create_record(body: f.RecordInput, ctx: Writer, db: Database):
    return farm.create_record(db, ctx.user.id, body)


@router.delete("/records", status_code=204)
def clear_records(ctx: Writer, db: Database):
    """Deletes every record ("clear all"). Records are only ever added one by one."""
    farm.replace_records(db, ctx.user.id, [])


@router.patch("/records/{record_id}", response_model=f.FarmRecord)
def update_record(record_id: str, body: f.RecordPatch, ctx: Writer, db: Database):
    return farm.update_record(db, ctx.user.id, record_id, body)


@router.delete("/records/{record_id}", status_code=204)
def delete_record(record_id: str, ctx: Writer, db: Database):
    farm.delete_record(db, ctx.user.id, record_id)


# ---------- MoMo ----------


@router.get("/momo/statement", dependencies=[Depends(rate_limit("momo-statement", 10, 3600))])
def momo_statement(
    ctx: Reader, db: Database,
    period: Literal["all", "month", "3m", "year"] = "all",
    network: Literal["MTN", "Telecel", "AirtelTigo"] | None = None,
    direction: Literal["in", "out"] | None = None,
    status: Literal["self_reported", "evidence_submitted", "verification_pending", "verified", "rejected", "disputed"] | None = None,
    query: str = Query("", max_length=120), newest_first: bool = True,
):
    from app.services.momo_statement import render
    from app.domain.clock import today
    data = render(db, ctx.user.id, period=period, network=network, direction=direction,
                  status=status, query=query, newest_first=newest_first)
    return attachment((f"AgriCtec_MoMo_Statement_{today().isoformat()}.pdf", data, "application/pdf"))


@router.get("/momo/summary")
def momo_summary(ctx: Reader, db: Database):
    """Exact MoMo totals for the MoMo page: all time, this and last month, the last six months,
    by network and by verification status. Money is decimal text."""
    return farm.momo_summary(db, ctx.user.id)


@router.get("/momo", response_model=list[f.MomoTransaction])
def list_momo(ctx: Reader, db: Database):
    return farm.list_momo(db, ctx.user.id)


@router.post("/momo", response_model=f.MomoTransaction, status_code=201)
def create_momo(body: f.MomoInput, ctx: Writer, db: Database):
    return farm.create_momo(db, ctx.user.id, body)


@router.delete("/momo", status_code=204)
def clear_momo(ctx: Writer, db: Database):
    """Deletes every MoMo transaction ("clear all")."""
    farm.replace_momo(db, ctx.user.id, [])


@router.patch("/momo/{momo_id}", response_model=f.MomoTransaction)
def update_momo(momo_id: str, body: f.MomoPatch, ctx: Writer, db: Database):
    return farm.update_momo(db, ctx.user.id, momo_id, body)


@router.delete("/momo/{momo_id}", status_code=204)
def delete_momo(momo_id: str, ctx: Writer, db: Database):
    farm.delete_momo(db, ctx.user.id, momo_id)


# ---------- Diagnoses, chat, settings ----------


@router.get("/diagnoses", response_model=list[f.Diagnosis])
def list_diagnoses(ctx: Reader, db: Database):
    return farm.list_diagnoses(db, ctx.user.id)


@router.delete("/diagnoses/{diagnosis_id}", status_code=204)
def delete_diagnosis(diagnosis_id: str, ctx: Writer, db: Database):
    farm.delete_diagnosis(db, ctx.user.id, diagnosis_id)


@router.delete("/diagnoses", status_code=204)
def clear_diagnoses(ctx: Writer, db: Database):
    farm.clear_diagnoses(db, ctx.user.id)


@router.get("/chat", response_model=list[f.ChatMessage])
def list_chat(ctx: Reader, db: Database):
    return farm.list_chat(db, ctx.user.id)


@router.delete("/chat", status_code=204)
def clear_chat(ctx: Writer, db: Database):
    farm.clear_chat(db, ctx.user.id)


@router.get("/settings", response_model=f.AppSettings)
def get_settings(ctx: Reader, db: Database):
    return farm.get_settings_for(db, ctx.user.id)


@router.put("/settings", response_model=f.AppSettings)
def save_settings(body: f.AppSettings, ctx: Writer, db: Database):
    return farm.save_settings(db, ctx.user.id, body)


# ---------- Credit, loans, verifications ----------


@router.get("/credit", response_model=f.CreditSummary)
def credit_summary(ctx: Reader, db: Database):
    """The score with the live rules, its band, the readiness checklist and the borrowing limit."""
    return account.credit_summary(db, ctx.user.id)


@router.get("/financial-summary")
def my_financial_summary(ctx: Reader, db: Database, months: Literal["3", "6", "12"] = "6"):
    """Exact totals (decimal strings) for the last 3, 6 or 12 months, verified vs self-reported."""
    return financial_summary.for_user(db, ctx.user.id, int(months))


class ReportRequest(CamelModel):
    months: Literal[3, 6, 12] = 6


@router.post(
    "/reports/financial", status_code=201, dependencies=[Depends(rate_limit("financial-report", 10, 3600))]
)
def create_financial_report(body: ReportRequest, ctx: Writer, db: Database):
    """A PDF of the farmer's recorded figures for the period, kept so it can be downloaded again."""
    return financial_reports.create(db, ctx.user.id, body.months)


@router.get("/reports/financial")
def list_financial_reports(ctx: Reader, db: Database):
    return financial_reports.list_for(db, ctx.user.id)


@router.get("/reports/financial/{report_id}/file")
def financial_report_file(report_id: str, ctx: Reader, db: Database):
    data, name = financial_reports.file(db, ctx.user.id, report_id)
    return attachment((name, data, "application/pdf"))


@router.delete("/reports/financial/{report_id}", status_code=204)
def delete_financial_report(report_id: str, ctx: Writer, db: Database):
    financial_reports.delete(db, ctx.user.id, report_id)


class EligibilityCheck(CamelModel):
    amount: Annotated[Decimal, Field(gt=0, le=100_000_000, max_digits=14, decimal_places=2)]
    months: Annotated[int, Field(ge=1, le=360)]


@router.post(
    "/loan-products/{product_id}/eligibility-check",
    dependencies=[Depends(rate_limit("eligibility-check", 60, 3600))],
)
def loan_product_eligibility(product_id: str, body: EligibilityCheck, ctx: Writer, db: Database):
    """A preliminary, explained check of the signed-in farmer's recorded transactions against
    one published loan product's terms and rules. Saved with the product version it used.
    Never a loan decision."""
    return loan_eligibility.check(db, ctx.user.id, product_id, body.amount, body.months)


@router.get("/loan-assessments")
def loan_assessments(ctx: Reader, db: Database):
    """The farmer's latest check of each loan product (newest first)."""
    return loan_eligibility.latest(db, ctx.user.id)


@router.post("/loans/eligibility", response_model=f.EligibilityResult)
def check_eligibility(body: f.EligibilityQuery, ctx: Reader, db: Database):
    return lending.eligibility(db, ctx.user.id, body)


@router.get("/loans/applications", response_model=list[f.LoanApplication])
def list_applications(ctx: Reader, db: Database):
    return lending.list_for_farmer(db, ctx.user.id)


@router.post("/loans/applications", response_model=f.LoanApplication, status_code=201)
def submit_application(body: f.LoanApplicationInput, ctx: Writer, db: Database):
    """Needs a verified Ghana Card and every eligibility rule; the rate, monthly payment and score
    are set here, not taken from the request."""
    return lending.submit(db, ctx.user.id, body)


@router.get("/verifications", response_model=list[f.VerificationRequest])
def list_verifications(ctx: Reader, db: Database):
    return verifications.list_for_farmer(db, ctx.user.id)


@router.post(
    "/phone/code",
    response_model=f.PhoneCodeSent,
    dependencies=[Depends(rate_limit("phone-code", 5, 600))],
)
def send_phone_code(ctx: Writer, db: Database):
    """Texts a six-digit code to the phone number saved on the profile (10 minutes)."""
    return phone_verification.send_code(db, ctx.user.id)


@router.post("/verifications", response_model=f.VerificationRequest, status_code=201)
def submit_verification(body: f.VerificationSubmit, ctx: Writer, db: Database):
    """A phone passes with the code texted to it (POST /me/phone/code); a farm location and a
    Ghana Card (full number) wait for an admin's review."""
    return verifications.submit(db, ctx.user.id, body.kind, body.value, body.code).model_copy(
        update={"identity_check": None}
    )


# ---------- Announcements ----------


@router.get("/announcements", response_model=list[Announcement])
def my_announcements(ctx: Reader, db: Database):
    """Live, for this farmer's region and crops, not dismissed; most severe first."""
    return account.my_announcements(db, ctx.user.id)


@router.get("/announcements/dismissed", response_model=list[str])
def dismissed_announcements(ctx: Reader, db: Database):
    """IDs of the announcements this farmer closed."""
    return farm.dismissed(db, ctx.user.id)


@router.post("/announcements/{announcement_id}/dismiss", response_model=list[str])
def dismiss_announcement(announcement_id: str, ctx: Writer, db: Database):
    return farm.dismiss(db, ctx.user.id, announcement_id)


# ---------- Notifications ----------


class ReadNotifications(CamelModel):
    """IDs to mark read; none marks every notice read."""

    ids: list[str] | None = None


@router.get("/notifications")
def list_notifications(ctx: Reader, db: Database):
    """The latest 50 notices, newest first, and how many are unread."""
    return notifications.inbox(db, ctx.user.id)


@router.post("/notifications/read", status_code=204)
def read_notifications(ctx: Writer, db: Database, body: ReadNotifications | None = None):
    notifications.mark_read(db, ctx.user.id, body.ids if body else None)


# ---------- Plans and AI tools ----------


@router.get("/subscription", response_model=f.SubscriptionSnapshot)
def get_subscription(ctx: Reader, db: Database):
    return subscriptions.snapshot(db, ctx.user.id)


@router.post("/subscription", response_model=f.SubscriptionSnapshot)
def activate_plan(body: f.ActivatePlan, ctx: Writer, db: Database):
    """DEMO payments: the plan is activated with a simulated receipt."""
    return subscriptions.activate(db, ctx.user.id, body.plan_id, body.method)


@router.post("/subscription/checkout", response_model=f.Checkout)
def checkout(body: f.ActivatePlan, ctx: Writer, db: Database):
    """Starts a real payment (Paystack): send the farmer to `authorizationUrl`. Paystack brings
    them back to /settings/plans?reference=..., which then calls /subscription/confirm."""
    return subscriptions.checkout(db, ctx.user, body.plan_id, body.method)


@router.post("/subscription/confirm", response_model=f.SubscriptionSnapshot)
def confirm_payment(body: f.ConfirmPayment, ctx: Writer, db: Database):
    """Checks the payment with Paystack; the plan is active once it is paid in full."""
    return subscriptions.confirm(db, body.reference, ctx.user.id)


@router.post("/assistant", response_model=f.AssistantAnswer, dependencies=[Depends(rate_limit("assistant", 30, 60))])
def ask_assistant(body: f.AssistantQuestion, ctx: Writer, db: Database):
    """Spends 1 AI credit, only when a reply is produced."""
    return tools.ask_assistant(db, ctx.user.id, body.message)


@router.post("/crop-doctor", response_model=f.DiagnosisAnswer, dependencies=[Depends(rate_limit("crop-doctor", 10, 60))])
def scan_crop(
    ctx: Writer,
    db: Database,
    image: Annotated[UploadFile, File(description="A photo of the crop, up to 10 MB")],
    crop: Annotated[str | None, Form()] = None,
):
    """Spends 5 AI credits, only when a diagnosis is produced. The diagnosis is saved to the
    farmer's history (the 10 most recent are kept) and returned as `diagnosis`."""
    content = image.file.read(MAX_IMAGE_BYTES + 1)
    return tools.scan_crop(db, ctx.user.id, image.filename or "photo", image.content_type, content, crop)


@router.post("/yield", response_model=f.YieldResult)
def predict_yield(body: f.YieldInput, ctx: Reader, db: Database):
    return tools.predict(db, ctx.user.id, body)


# ---------- The farmer's data ----------


@router.get("/export", response_model=f.DataExport)
def export_data(ctx: Reader, db: Database):
    return account.export_data(db, ctx.user.id)


@router.post("/import", response_model=Message)
def import_data(body: f.DataImport, ctx: Writer, db: Database):
    account.import_data(db, ctx.user.id, body)
    return Message(detail="Your data was restored.")


@router.post("/delete-data", response_model=Message)
def delete_data(body: Confirmation, ctx: Writer, db: Database):
    """Type DELETE to confirm."""
    account.delete_data(db, ctx.user.id, body.confirmation)
    return Message(detail="Your data was deleted.")

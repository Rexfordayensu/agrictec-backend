"""The console's people: overview, farmers, verifications, risk and the team. Every route
checks its permission; the services audit every write."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session as DB

from app.core.deps import AdminContext, admin_context, require
from app.db import get_db
from app.domain.permissions import PERMISSION_LABELS, ROLE_LABELS, ROLE_PERMISSIONS
from app.domain.risk_rules import RISK_RULES, THRESHOLDS
from app.routers.common import file_response
from app.schemas import admin as a
from app.schemas.base import Message
from app.schemas.farm import VerificationRequest
from app.services import admin_farmers, insights, risk, team, verifications

router = APIRouter(prefix="/admin")
Database = Annotated[DB, Depends(get_db)]


def allowed(permission: str):
    return Annotated[AdminContext, Depends(require(permission))]


# ---------- Overview ----------


@router.get("/overview", tags=["Admin overview"])
def overview(
    ctx: allowed("overview.view"), db: Database, date_range: Annotated[Literal["30d", "90d", "12m", "all"], Query(alias="range")] = "30d"
):
    """KPIs, sign-ups, the loan funnel, charts and the "needs attention" list for this role."""
    return insights.overview(db, ctx.admin, date_range)


@router.get("/roles", tags=["Admin team"])
def roles(ctx: Annotated[AdminContext, Depends(admin_context)]):
    """The role -> permission matrix (for showing and hiding controls)."""
    return {
        "roles": [{"id": r, "label": ROLE_LABELS[r], "permissions": p} for r, p in ROLE_PERMISSIONS.items()],
        "permissionLabels": PERMISSION_LABELS,
    }


# ---------- Farmers ----------


@router.get("/farmers", response_model=list[a.FarmerAccount], tags=["Admin farmers"])
def list_farmers(
    ctx: allowed("farmers.view"),
    db: Database,
    search: str | None = None,
    region: str | None = None,
    status: Literal["active", "suspended", "pending_verification"] | None = None,
    tag: str | None = None,
    include_guests: Annotated[bool, Query(alias="includeGuests")] = True,
):
    return admin_farmers.list_farmers(db, search=search, region=region, status=status, tag=tag, include_guests=include_guests)


@router.post("/farmers/export", tags=["Admin farmers"])
def export_farmers(body: a.IdList, ctx: allowed("reports.export"), db: Database):
    """CSV of the given farmers (or all). Never a password hash or Ghana Card digits."""
    return file_response(admin_farmers.export_csv(db, ctx.admin, body.ids), "AgriCtec_Farmers.csv")


@router.get("/farmers/{user_id}", response_model=a.FarmerAccount, tags=["Admin farmers"])
def get_farmer(user_id: str, ctx: allowed("farmers.view"), db: Database):
    return admin_farmers.get_farmer(db, user_id)


@router.get("/farmers/{user_id}/detail", response_model=a.FarmerDetail, tags=["Admin farmers"])
def farmer_detail(user_id: str, ctx: allowed("farmers.view"), db: Database):
    """Records, MoMo, diagnoses and the score breakdown with the live rules."""
    return admin_farmers.get_detail(db, user_id)


@router.get("/farmers/{user_id}/verifications", response_model=list[VerificationRequest], tags=["Admin farmers"])
def farmer_verifications(user_id: str, ctx: allowed("farmers.view"), db: Database):
    return verifications.list_for_farmer(db, user_id)


@router.post("/farmers/{user_id}/suspend", response_model=a.FarmerAccount, tags=["Admin farmers"])
def suspend(user_id: str, body: a.Suspend, ctx: allowed("farmers.suspend"), db: Database):
    """The farmer is signed out everywhere at once."""
    return admin_farmers.suspend(db, ctx.admin, user_id, body.reason)


@router.post("/farmers/{user_id}/reactivate", response_model=a.FarmerAccount, tags=["Admin farmers"])
def reactivate(user_id: str, ctx: allowed("farmers.suspend"), db: Database):
    return admin_farmers.reactivate(db, ctx.admin, user_id)


@router.post("/farmers/{user_id}/delete", response_model=Message, tags=["Admin farmers"])
def delete_farmer(user_id: str, body: a.DeleteFarmer, ctx: allowed("farmers.delete"), db: Database):
    """Permanent. `confirmation` is the farmer's email or name, typed exactly."""
    admin_farmers.delete_farmer(db, ctx.admin, user_id, body.confirmation)
    return Message(detail="Farmer deleted.")


@router.post("/farmers/{user_id}/notes", response_model=a.AdminNote, status_code=201, tags=["Admin farmers"])
def add_note(user_id: str, body: a.NoteInput, ctx: allowed("farmers.edit"), db: Database):
    return admin_farmers.add_note(db, ctx.admin, user_id, body.text)


@router.put("/farmers/{user_id}/tags", response_model=list[str], tags=["Admin farmers"])
def set_tags(user_id: str, body: a.TagsInput, ctx: allowed("farmers.edit"), db: Database):
    return admin_farmers.set_tags(db, ctx.admin, user_id, body.tags)


@router.post("/farmers/{user_id}/reset-password", response_model=a.TemporaryPassword, tags=["Admin farmers"])
def reset_password(user_id: str, ctx: allowed("farmers.edit"), db: Database):
    """A one-time password, shown once and never logged."""
    return a.TemporaryPassword(temporary_password=admin_farmers.reset_password(db, ctx.admin, user_id))


@router.get("/farmers/{user_id}/records.csv", tags=["Admin farmers"])
def export_records(user_id: str, ctx: allowed("reports.export"), db: Database):
    return file_response(admin_farmers.export_records_csv(db, ctx.admin, user_id), f"AgriCtec_Records_{user_id}.csv")


@router.post("/farmers/{user_id}/impersonate", response_model=a.ImpersonationStart, tags=["View as farmer"])
def impersonate(user_id: str, ctx: allowed("farmers.impersonate"), db: Database):
    """Opens the farmer app read-only: pass the token to POST /auth/impersonation/claim within a minute."""
    return admin_farmers.start_impersonation(db, ctx.admin, user_id)


# ---------- Verifications ----------


@router.get("/verifications", response_model=list[VerificationRequest], tags=["Admin verifications"])
def list_verifications(
    ctx: allowed("verifications.review"),
    db: Database,
    status: Literal["pending", "approved", "rejected"] | None = None,
    kind: Literal["phone", "location", "ghanaCard"] | None = None,
    user_id: Annotated[str | None, Query(alias="userId")] = None,
):
    return verifications.list_all(db, status=status, kind=kind, user_id=user_id)


@router.post("/verifications/approve-many", response_model=list[VerificationRequest], tags=["Admin verifications"])
def approve_many(body: a.IdList, ctx: allowed("verifications.review"), db: Database):
    """Phone and location checks only: Ghana Cards are reviewed one by one."""
    return verifications.approve_many(db, ctx.admin, body.ids or [])


@router.post("/verifications/{request_id}/approve", response_model=VerificationRequest, tags=["Admin verifications"])
def approve(request_id: str, ctx: allowed("verifications.review"), db: Database):
    return verifications.approve(db, ctx.admin, request_id)


@router.post("/verifications/{request_id}/reject", response_model=VerificationRequest, tags=["Admin verifications"])
def reject(request_id: str, body: a.Reject, ctx: allowed("verifications.review"), db: Database):
    """`reason` is one of: Number format invalid, Doesn't match name, Duplicate, Unclear, Other
    (Other needs a note)."""
    return verifications.reject(db, ctx.admin, request_id, body.reason, body.note)


# ---------- Risk ----------


@router.get("/risk/rules", response_model=list[a.RiskRuleInfo], tags=["Admin risk"])
def risk_rules(ctx: allowed("risk.review")):
    return [
        a.RiskRuleInfo(id=r.id, label=r.label, explanation=r.explanation, guidance=r.guidance, severity=r.severity, related=r.related)
        for r in RISK_RULES
    ]


@router.get("/risk/thresholds", tags=["Admin risk"])
def risk_thresholds(ctx: allowed("risk.review")):
    return THRESHOLDS


@router.post("/risk/scan", response_model=list[a.RiskFlag], tags=["Admin risk"])
def scan(ctx: allowed("risk.review"), db: Database):
    """Runs every rule over every account; returns only the new flags."""
    return risk.run_scan(db, ctx.admin)


@router.get("/risk/flags", response_model=list[a.RiskFlag], tags=["Admin risk"])
def list_flags(
    ctx: allowed("risk.review"),
    db: Database,
    status: Literal["open", "dismissed", "confirmed"] | None = None,
    severity: Literal["low", "medium", "high"] | None = None,
    rule: str | None = None,
    user_id: Annotated[str | None, Query(alias="userId")] = None,
):
    return risk.list_flags(db, status=status, severity=severity, rule=rule, user_id=user_id)


@router.get("/risk/flags/{flag_id}/related", response_model=a.RiskRelated, tags=["Admin risk"])
def related(flag_id: str, ctx: allowed("risk.review"), db: Database):
    return risk.related(db, flag_id)


@router.post("/risk/flags/{flag_id}/dismiss", response_model=a.RiskFlag, tags=["Admin risk"])
def dismiss(flag_id: str, body: a.RiskReview, ctx: allowed("risk.review"), db: Database):
    return risk.dismiss(db, ctx.admin, flag_id, body.note)


@router.post("/risk/flags/{flag_id}/confirm", response_model=a.RiskFlag, tags=["Admin risk"])
def confirm(flag_id: str, body: a.RiskReview, ctx: allowed("risk.review"), db: Database):
    """With `suspend`, also suspends the farmer (needs farmers.suspend)."""
    return risk.confirm(db, ctx.admin, flag_id, body.note, body.suspend)


# ---------- Team ----------


@router.get("/team", response_model=list[a.AdminUser], tags=["Admin team"])
def list_team(ctx: allowed("admins.manage"), db: Database):
    return team.list_team(db)


@router.post("/team", response_model=a.InviteResult, status_code=201, tags=["Admin team"])
def invite(body: a.Invite, ctx: allowed("admins.manage"), db: Database):
    """The temporary password is returned once; the new admin must change it at first sign-in."""
    return team.invite(db, ctx.admin, body.name, body.email, body.role)


@router.post("/team/{admin_id}/role", response_model=a.AdminUser, tags=["Admin team"])
def change_role(admin_id: str, body: a.RoleChange, ctx: allowed("admins.manage"), db: Database):
    return team.change_role(db, ctx.admin, admin_id, body.role)


@router.post("/team/{admin_id}/disable", response_model=a.AdminUser, tags=["Admin team"])
def disable(admin_id: str, ctx: allowed("admins.manage"), db: Database):
    return team.disable(db, ctx.admin, admin_id)


@router.post("/team/{admin_id}/enable", response_model=a.AdminUser, tags=["Admin team"])
def enable(admin_id: str, ctx: allowed("admins.manage"), db: Database):
    return team.enable(db, ctx.admin, admin_id)


@router.post("/team/{admin_id}/reset-password", response_model=a.TemporaryPassword, tags=["Admin team"])
def reset_admin_password(admin_id: str, ctx: allowed("admins.manage"), db: Database):
    return a.TemporaryPassword(temporary_password=team.reset_password(db, ctx.admin, admin_id))

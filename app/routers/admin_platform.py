"""The console's platform controls: settings, content, announcements, the audit log, reports
and the danger zone."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session as DB

from app.core.deps import AdminContext, require
from app.db import get_db
from app.domain.announcements import in_audience
from app.domain.clock import today
from app.domain.reports import datasets_meta
from app.integrations import market_prices
from app.routers.common import file_response
from app.schemas import admin as a
from app.schemas.base import CamelModel, Message, Paged
from app.schemas.platform import (
    Announcement,
    AnnouncementCreate,
    AnnouncementPatch,
    AssistantKnowledgeItem,
    Audience,
    CropContent,
    DiagnosisContent,
    FeatureFlags,
    MarketPrice,
    Plan,
    Weather,
)
from app.services import admin_config, announcements, audit_log, backup, insights, platform_store
from app.services.farmer_data import load_farmers

router = APIRouter(prefix="/admin")
Database = Annotated[DB, Depends(get_db)]


def allowed(permission: str):
    return Annotated[AdminContext, Depends(require(permission))]


# ---------- Settings ----------


@router.put("/settings/flags", response_model=FeatureFlags, tags=["Admin settings"])
def save_flags(body: FeatureFlags, ctx: allowed("settings.manage"), db: Database):
    """Feature flags, sign-ups, guest mode and maintenance mode (with an optional end time)."""
    return admin_config.save_flags(db, ctx.admin, body)


@router.put("/settings/plans", response_model=list[Plan], tags=["Admin settings"])
def save_plans(body: list[Plan], ctx: allowed("settings.manage"), db: Database):
    return admin_config.save_plans(db, ctx.admin, body)


# ---------- Content ----------


@router.get("/content/meta", tags=["Admin content"])
def content_meta(ctx: allowed("content.manage"), db: Database):
    """Who last saved each content type, and when."""
    return platform_store.content_meta(db)


@router.get("/content/market", response_model=list[MarketPrice], tags=["Admin content"])
def get_market_prices(ctx: allowed("content.manage"), db: Database):
    """Every observation, drafts included (farmers see only published ones)."""
    return platform_store.market_prices(db)


@router.put("/content/market", response_model=list[MarketPrice], tags=["Admin content"])
def save_market_prices(body: list[MarketPrice], ctx: allowed("content.manage"), db: Database):
    return admin_config.save_market_prices(db, ctx.admin, body)


class ResearchQuery(CamelModel):
    crop: Annotated[str, Field(min_length=1, max_length=80)]
    region: str | None = None
    market: Annotated[str | None, Field(max_length=160)] = None
    unit: Literal["kg", "bag", "crate", "tonne"]
    quantity: Annotated[float, Field(gt=0, le=100_000)]
    package_weight_kg: Annotated[float | None, Field(gt=0, le=100_000)] = None


@router.post("/content/market/research", tags=["Admin content"])
def research_market_prices(body: ResearchQuery, ctx: allowed("content.manage")):
    """Source-backed Ghanaian observations (WFP market monitoring) for one crop, unit and
    package size: the latest per market, never an average. Nothing is published: the admin
    reviews each one first."""
    query = body.model_dump(by_alias=True)
    try:
        found = market_prices.research(query, today(), market_prices.observations())
    except market_prices.PricesUnavailable:
        return {
            "status": "unavailable",
            "message": "Market price data unavailable",
            "reason": "The WFP price dataset couldn't be reached just now. Try again in a moment.",
        }
    if not found:
        return {
            "status": "unavailable",
            "message": "Market price data unavailable",
            "reason": (
                "No WFP observation matches this crop, unit and package size"
                + (" in this region" if body.region else "")
                + ". Try another unit (kg, or a bag with its weight) or leave the region empty."
            ),
        }
    return {"status": "ready", "data": found}


@router.put("/content/weather-override", response_model=Weather | None, tags=["Admin content"])
def save_weather_override(body: Weather | None, ctx: allowed("content.manage"), db: Database):
    return admin_config.save_weather_override(db, ctx.admin, body)


@router.get("/content/crops", response_model=list[CropContent], tags=["Admin content"])
def get_crops(ctx: allowed("content.manage"), db: Database):
    return platform_store.crops(db)


@router.put("/content/crops", response_model=list[CropContent], tags=["Admin content"])
def save_crops(body: list[CropContent], ctx: allowed("content.manage"), db: Database):
    return admin_config.save_crops(db, ctx.admin, body)


@router.get("/content/assistant", response_model=list[AssistantKnowledgeItem], tags=["Admin content"])
def get_assistant_kb(ctx: allowed("content.manage"), db: Database):
    return platform_store.assistant_kb(db)


@router.put("/content/assistant", response_model=list[AssistantKnowledgeItem], tags=["Admin content"])
def save_assistant_kb(body: list[AssistantKnowledgeItem], ctx: allowed("content.manage"), db: Database):
    """The fallback reply ("kb-fallback") can be edited but never removed."""
    return admin_config.save_assistant_kb(db, ctx.admin, body)


@router.post("/content/assistant/test", response_model=a.AssistantTestResult, tags=["Admin content"])
def test_assistant(body: a.AssistantTest, ctx: allowed("content.manage"), db: Database):
    """Which reply a message gets, for one farmer or a sample farmer. No credits are spent."""
    return admin_config.test_assistant(db, body.message, body.farmer_id)


@router.get("/content/diagnoses", response_model=list[DiagnosisContent], tags=["Admin content"])
def get_diagnoses(ctx: allowed("content.manage"), db: Database):
    return platform_store.diagnoses(db)


@router.put("/content/diagnoses", response_model=list[DiagnosisContent], tags=["Admin content"])
def save_diagnoses(body: list[DiagnosisContent], ctx: allowed("content.manage"), db: Database):
    return admin_config.save_diagnoses(db, ctx.admin, body)


# ---------- Announcements ----------


class ReachQuery(CamelModel):
    audience: Audience


@router.get("/announcements", response_model=list[Announcement], tags=["Admin announcements"])
def list_announcements(ctx: allowed("announcements.send"), db: Database):
    return announcements.list_all(db)


@router.post("/announcements/reach", tags=["Admin announcements"])
def estimate_reach(body: ReachQuery, ctx: allowed("announcements.send"), db: Database):
    """How many registered farmers an audience reaches today ("~23 farmers")."""
    farmers = [d for d in load_farmers(db).values() if not d.user.is_guest]
    return {"farmers": sum(1 for d in farmers if in_audience(body.audience, d.profile.region, d.profile.primary_crops))}


@router.post("/announcements", response_model=Announcement, status_code=201, tags=["Admin announcements"])
def create_announcement(body: AnnouncementCreate, ctx: allowed("announcements.send"), db: Database):
    """A critical announcement to everyone needs confirmedCritical: true."""
    return announcements.create(db, ctx.admin, body)


@router.patch("/announcements/{announcement_id}", response_model=Announcement, tags=["Admin announcements"])
def update_announcement(announcement_id: str, body: AnnouncementPatch, ctx: allowed("announcements.send"), db: Database):
    """Only before it starts."""
    return announcements.update(db, ctx.admin, announcement_id, body)


@router.post("/announcements/{announcement_id}/end", response_model=Announcement, tags=["Admin announcements"])
def end_announcement(announcement_id: str, ctx: allowed("announcements.send"), db: Database):
    return announcements.end(db, ctx.admin, announcement_id)


@router.delete("/announcements/{announcement_id}", response_model=Message, tags=["Admin announcements"])
def delete_announcement(announcement_id: str, ctx: allowed("announcements.send"), db: Database):
    """Not while farmers are seeing it: end it first."""
    announcements.remove(db, ctx.admin, announcement_id)
    return Message(detail="Announcement deleted.")


# ---------- Audit log ----------


@router.get("/audit", response_model=Paged[a.AuditEntry], tags=["Admin audit log"])
def list_audit(
    ctx: allowed("audit.view"),
    db: Database,
    actor_id: Annotated[str | None, Query(alias="actorId")] = None,
    action: str | None = None,
    group: Literal["farmer", "loan", "config", "content", "team", "system", "auth"] | None = None,
    target_type: Annotated[str | None, Query(alias="targetType")] = None,
    target_id: Annotated[str | None, Query(alias="targetId")] = None,
    date_from: Annotated[str | None, Query(alias="from")] = None,
    date_to: Annotated[str | None, Query(alias="to")] = None,
    search: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=500)] = audit_log.PAGE_SIZE,
):
    """Newest first. Append-only: there is no edit or delete, for anyone."""
    flt = a.AuditFilter.model_validate(
        {"actorId": actor_id, "action": action, "group": group, "targetType": target_type, "targetId": target_id,
         "from": date_from, "to": date_to, "search": search}
    )
    return audit_log.list_entries(db, flt, page, page_size)


@router.get("/audit/actors", response_model=list[a.AuditActor], tags=["Admin audit log"])
def audit_actors(ctx: allowed("audit.view"), db: Database):
    return audit_log.actors(db)


@router.post("/audit/export", response_model=a.FileExport, tags=["Admin audit log"])
def export_audit(body: a.AuditExport, ctx: allowed("audit.view"), db: Database):
    """Every matching entry, oldest first, as JSON or CSV. The export is itself logged."""
    return audit_log.export(db, ctx.admin, body.filter, body.format)


# ---------- Reports ----------


@router.get("/reports", tags=["Admin reports"])
def report_catalog(ctx: allowed("reports.export")):
    return {"reports": insights.catalog(), "datasets": datasets_meta()}


@router.get("/reports/{report_id}", response_model=a.AdminReport, tags=["Admin reports"])
def build_report(report_id: str, ctx: allowed("reports.export"), db: Database):
    """A ready-made report from the latest data (the preview; not logged)."""
    return insights.build(db, report_id)


@router.post("/reports/{report_id}/export", tags=["Admin reports"])
def export_report(
    report_id: str, ctx: allowed("reports.export"), db: Database, fmt: Annotated[Literal["csv", "pdf"], Query(alias="format")] = "csv"
):
    """CSV downloads directly. For PDF the response carries the report for the console to render
    (lib/pdf/generate-admin-report.ts); both are logged."""
    result = insights.export_report(db, ctx.admin, report_id, fmt)
    if fmt == "csv":
        return file_response(result["content"], result["filename"])
    return {"filename": result["filename"], "report": result["report"]}


@router.post("/reports/builder/preview", response_model=a.BuilderResult, tags=["Admin reports"])
def builder_preview(body: a.BuilderSpec, ctx: allowed("reports.export"), db: Database):
    return insights.preview(db, body)


@router.post("/reports/builder/export", tags=["Admin reports"])
def builder_export(body: a.BuilderSpec, ctx: allowed("reports.export"), db: Database):
    result = insights.export_custom(db, ctx.admin, body)
    response = file_response(result["csv"], result["filename"])
    response.headers["X-Row-Count"] = str(result["rows"])
    return response


# ---------- System: backups and the danger zone ----------


@router.get("/system/status", tags=["Admin system"])
def system_status(ctx: allowed("overview.view"), db: Database):
    """When the demo world was seeded (None if never)."""
    return {"seededAt": platform_store.get_raw(db, platform_store.SEEDED)}


@router.post("/system/backup", tags=["Admin system"])
def export_backup(ctx: allowed("data.manage"), db: Database):
    """Every table except sign-ins, the team and the audit trail. No password hashes."""
    return backup.export_all(db, ctx.admin)


@router.post("/system/backup/summary", tags=["Admin system"])
def summarize_backup(body: dict, ctx: allowed("data.manage")):
    """What a backup file holds, checked, before restoring it."""
    return backup.summarize(body)


@router.post("/system/restore", tags=["Admin system"])
def restore_backup(body: a.BackupImport, ctx: allowed("data.manage"), db: Database):
    """Replaces all platform and farmer data, all or nothing. Type RESTORE to confirm."""
    return backup.import_all(db, ctx.admin, body.backup, body.confirmation)


@router.post("/system/reset-demo", tags=["Admin system"])
def reset_demo(body: a.ResetDemo, ctx: allowed("data.manage"), db: Database):
    """Type RESET to confirm."""
    return backup.reset_demo(db, ctx.admin, body.confirmation)


@router.post("/system/delete-farmers", tags=["Admin system"])
def delete_all_farmers(body: a.DeleteAllFarmers, ctx: allowed("data.manage"), db: Database):
    """Type DELETE ALL FARMERS and your own password."""
    return backup.delete_all_farmers(db, ctx.admin, body.confirmation, body.password)

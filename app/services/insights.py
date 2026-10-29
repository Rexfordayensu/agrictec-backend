"""The admin overview and reports (lib/admin/overview.ts, services/admin/reports.service.ts).
Every export is checked against reports.export and logged as "report.export"."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import NotFound
from app.domain import analytics
from app.domain.clock import iso, now_utc
from app.domain.permissions import can
from app.domain.reports import (
    DATASETS,
    REPORT_CATALOG,
    ReportData,
    build_report,
    builder_to_csv,
    report_file_name,
    report_to_csv,
    run_builder,
)
from app.models import Admin, AuditEntry, LoanApplication, RiskFlag, VerificationRequest
from app.schemas.admin import BuilderSpec
from app.services import platform_store
from app.services.farmer_data import accounts, load_farmers
from app.services.mappers import audit_out, loan_out, risk_out, verification_out


def _platform(db: DB):
    config = platform_store.credit_config(db)
    farmers = accounts(db, config)
    apps = [loan_out(r) for r in db.scalars(select(LoanApplication))]
    flags = [risk_out(r) for r in db.scalars(select(RiskFlag))]
    verifications = [verification_out(r) for r in db.scalars(select(VerificationRequest))]
    return config, farmers, apps, flags, verifications


def overview(db: DB, admin: Admin, date_range: str) -> dict:
    config, farmers, apps, flags, verifications = _platform(db)
    now = now_utc()
    recent = (
        [audit_out(r) for r in db.scalars(select(AuditEntry).order_by(AuditEntry.seq.desc()).limit(10))]
        if can(admin.role, "audit.view")
        else []
    )
    return {
        "range": date_range,
        "kpis": analytics.overview_kpis(farmers, apps, flags, verifications, date_range, now),
        "signups": analytics.signup_series(farmers, date_range, now),
        "loanStages": analytics.loan_stage_reach(apps, date_range, now),
        "loanFunnel": analytics.loan_funnel(apps),
        "farmersByRegion": analytics.farmers_by_region(farmers),
        "cropPopularity": analytics.crop_popularity(farmers),
        "scoreDistribution": analytics.score_distribution(farmers, config.bands),
        "needsAttention": analytics.needs_attention(
            farmers=farmers, apps=apps, flags=flags, verifications=verifications,
            feature_flags=platform_store.flags(db), role=admin.role, now=now,
        ),
        "recentActivity": [e.model_dump(mode="json", by_alias=True) for e in recent],
        "badges": {
            "pendingVerifications": sum(1 for v in verifications if v.status == "pending"),
            "openRiskFlags": sum(1 for f in flags if f.status == "open"),
            "newApplications": sum(1 for a in apps if a.status == "submitted"),
        },
    }


def report_data(db: DB) -> ReportData:
    config, farmers, apps, flags, verifications = _platform(db)
    data = load_farmers(db)
    return ReportData(
        farmers=farmers,
        records=[r for d in data.values() for r in d.records],
        momo=[t for d in data.values() for t in d.momo],
        applications=apps,
        lenders=platform_store.lenders(db, include_inactive=True),
        verifications=verifications,
        risk_flags=flags,
        bands=config.bands,
    )


def catalog() -> list[dict]:
    return REPORT_CATALOG


def build(db: DB, report_id: str) -> dict:
    if report_id not in {r["id"] for r in REPORT_CATALOG}:
        raise NotFound("Report not found.")
    return build_report(report_id, report_data(db), now_utc())


def export_report(db: DB, actor: Admin, report_id: str, fmt: str) -> dict:
    """CSV is built here. For PDF the console renders the returned report with jsPDF
    (lib/pdf/generate-admin-report.ts); the export is logged either way."""
    report = build(db, report_id)
    append_audit(
        db, actor, action="report.export", target_type="system", target_id=f"report:{report_id}",
        summary=f"Exported the {report['title']} report ({fmt.upper()})", after={"report": report_id, "format": fmt},
    )
    db.commit()
    return {
        "filename": report_file_name(report, fmt),
        "content": report_to_csv(report) if fmt == "csv" else None,
        "report": {**report, "generatedAt": iso(report["generatedAt"])},
    }


def preview(db: DB, spec: BuilderSpec) -> dict:
    """The builder's preview (no export, so not logged)."""
    return run_builder(spec, report_data(db))


def export_custom(db: DB, actor: Admin, spec: BuilderSpec) -> dict:
    result = run_builder(spec, report_data(db))
    label = DATASETS[spec.dataset]["label"]
    append_audit(
        db, actor, action="report.export", target_type="system", target_id=f"custom:{spec.dataset}",
        summary=f"Exported a custom {label.lower()} report ({result['total']} rows)",
        after={**spec.model_dump(mode="json", by_alias=True, exclude_none=True), "rows": result["total"]},
    )
    db.commit()
    slug = re.sub(r"\W+", "_", label)
    return {
        "filename": f"AgriCtec_{slug}_{iso(now_utc())[:10]}.csv",
        "csv": builder_to_csv(result),
        "rows": result["total"],
    }

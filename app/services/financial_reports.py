"""Financial reports a farmer generates (app/domain/financial_report.py), kept in PostgreSQL.

The PDF and a snapshot of its figures are stored together and never changed; the farmer can
download a report again or delete it. Admins see them only through farmers.view, audited."""

import hashlib
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import NotFound
from app.domain.clock import iso, now_utc, today
from app.domain.financial_report import render
from app.domain.financial_summary import period_bounds
from app.integrations import storage
from app.models import Admin, FarmRecord, FinancialReportExport, MomoTransaction, User, new_id
from app.services import farm, financial_summary

FARM_KINDS = {"income": "Income", "expense": "Expense", "activity": "Farm activity"}


def _out(row: FinancialReportExport) -> dict:
    return {
        "id": row.id,
        "periodMonths": row.period_months,
        "periodStart": row.period_start.isoformat(),
        "periodEnd": row.period_end.isoformat(),
        "generatedAt": iso(row.generated_at),
        "sizeBytes": row.size_bytes,
        "sha256": row.sha256,
    }


def _entries(db: DB, user_id: str, months: int) -> list[dict]:
    start, end = period_bounds(months, today())
    rows: list[dict] = []
    for r in db.scalars(select(FarmRecord).where(
        FarmRecord.user_id == user_id, FarmRecord.date >= start, FarmRecord.date <= end
    )):
        kind = FARM_KINDS.get(r.type) or f"Financing {'in' if r.direction == 'in' else 'out'}"
        rows.append({"date": r.date.isoformat(), "kind": kind, "description": r.description,
                     "amount": f"{r.amount:.2f}", "status": r.verification_status, "source": r.source_type})
    for t in db.scalars(select(MomoTransaction).where(
        MomoTransaction.user_id == user_id, MomoTransaction.date >= start, MomoTransaction.date <= end
    )):
        rows.append({"date": t.date.isoformat(), "kind": f"MoMo {'in' if t.direction == 'in' else 'out'}",
                     "description": t.counterparty, "amount": f"{t.amount:.2f}",
                     "status": t.verification_status, "source": t.source_type})
    return sorted(rows, key=lambda e: e["date"], reverse=True)


def create(db: DB, user_id: str, months: int) -> dict:
    summary = financial_summary.for_user(db, user_id, months)
    report_id = new_id()
    generated_at = now_utc()
    pdf = render(
        summary=summary,
        profile=farm.get_profile(db, user_id).model_dump(),
        entries=_entries(db, user_id, months),
        report_id=report_id[:8].upper(),
        generated_at=generated_at,
    )
    key = f"reports/{user_id}/{report_id}.pdf"
    storage.store(db).put(key, pdf, "application/pdf")
    row = FinancialReportExport(
        id=report_id,
        user_id=user_id,
        period_months=months,
        period_start=date.fromisoformat(summary["period"]["from"]),
        period_end=date.fromisoformat(summary["period"]["to"]),
        generated_at=generated_at,
        file_key=key,
        sha256=hashlib.sha256(pdf).hexdigest(),
        size_bytes=len(pdf),
        summary=summary,
    )
    db.add(row)
    db.commit()
    return _out(row)


def list_for(db: DB, user_id: str) -> list[dict]:
    rows = db.scalars(
        select(FinancialReportExport)
        .where(FinancialReportExport.user_id == user_id)
        .order_by(FinancialReportExport.generated_at.desc())
    )
    return [_out(row) for row in rows]


def _own(db: DB, user_id: str, report_id: str) -> FinancialReportExport:
    row = db.get(FinancialReportExport, report_id)
    if row is None or row.user_id != user_id:
        raise NotFound("Report not found.")
    return row


def file(db: DB, user_id: str, report_id: str) -> tuple[bytes, str]:
    row = _own(db, user_id, report_id)
    found = storage.store(db).get(row.file_key)
    if found is None:
        raise NotFound("Report file not found.")
    data, _ = found
    return data, f"AgriCtec_Financial_Summary_{row.period_end.isoformat()}_{row.period_months}m.pdf"


def delete(db: DB, user_id: str, report_id: str) -> None:
    """The report and (by a database trigger) its PDF."""
    db.delete(_own(db, user_id, report_id))
    db.commit()


def admin_list(db: DB, user_id: str) -> list[dict]:
    if db.get(User, user_id) is None:
        raise NotFound("Farmer not found.")
    return list_for(db, user_id)


def admin_file(db: DB, actor: Admin, user_id: str, report_id: str) -> tuple[bytes, str]:
    data, name = file(db, user_id, report_id)
    append_audit(db, actor, action="farmer.report_download", target_type="farmer", target_id=user_id,
                 summary=f"Downloaded a farmer's financial report ({report_id[:8].upper()})")
    db.commit()
    return data, name

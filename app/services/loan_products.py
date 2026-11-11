"""Loan products (services/admin/loan-products.service.ts, and the farmer marketplace).

An administrator edits a product's draft from the lender's official terms. Publishing checks
it (app/domain/loan_products.publish_problems) and freezes it into a new read-only version.
Farmers see a product's latest published version, only while its lender is listed."""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, NotFound
from app.core.events import bus
from app.domain.clock import now_utc
from app.domain.loan_products import publish_problems
from app.models import Admin, Lender, LoanProduct, LoanProductVersion, LoanRequirement
from app.schemas import products as s
from app.services import lending
from app.services.mappers import lender_logo_path

# A requirement row's own columns (as the API names them); everything else goes in `rules`.
REQUIREMENT_RULE_SKIP = {"key", "name", "category", "required", "fieldType", "verification"}


def _dump(model) -> dict:
    return model.model_dump(mode="json", by_alias=True)


def _product(db: DB, product_id: str) -> LoanProduct:
    row = db.get(LoanProduct, product_id)
    if row is None:
        raise NotFound("Loan product not found.")
    return row


def _version(db: DB, product_id: str, version: int | None) -> LoanProductVersion | None:
    if version is None:
        return None
    return db.scalar(
        select(LoanProductVersion).where(
            LoanProductVersion.product_id == product_id, LoanProductVersion.version == version
        )
    )


def version_out(row: LoanProductVersion) -> s.ProductVersion:
    requirements = []
    for r in row.requirements:
        requirements.append(
            s.Requirement.model_validate(
                {
                    **r.rules,
                    "key": r.key,
                    "name": r.name,
                    "category": r.category,
                    "required": r.required,
                    "fieldType": r.field_type,
                    "verification": r.verification,
                }
            )
        )
    return s.ProductVersion(
        name=row.name,
        terms=s.ProductTerms.model_validate(row.terms),
        requirements=requirements,
        source=row.source,
        source_checked_on=row.source_checked_on,
        version=row.version,
        published_at=row.published_at,
        published_by=row.published_by,
        note=row.note,
    )


def _content(version: s.ProductVersion) -> s.ProductContent:
    return s.ProductContent.model_validate(version.model_dump(include=set(s.ProductContent.model_fields)))


def _listed(row: LoanProduct, lender: Lender | None) -> bool:
    return row.status == "published" and lender is not None and lending.is_listed(lender)


def admin_out(db: DB, row: LoanProduct) -> s.AdminProduct:
    draft = s.ProductContent.model_validate(row.draft)
    published_row = _version(db, row.id, row.published_version)
    published = version_out(published_row) if published_row else None
    changed = published is None or _dump(draft) != _dump(_content(published))
    return s.AdminProduct(
        id=row.id,
        lender_id=row.lender_id,
        status=row.status,
        published=published,
        draft=draft,
        draft_changed=changed,
        problems=publish_problems(draft, now_utc().date()),
        listed=_listed(row, db.get(Lender, row.lender_id)),
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def _saved(db: DB, actor: Admin, row: LoanProduct, *, action: str, summary: str, after=None) -> s.AdminProduct:
    row.updated_at, row.updated_by = now_utc(), actor.name
    append_audit(db, actor, action=action, target_type="loan_product", target_id=row.id, summary=summary,
                 after=after)
    db.commit()
    bus.publish("platform")
    return admin_out(db, row)


# ---------- Console ----------


def list_for_lender(db: DB, lender_id: str) -> list[s.AdminProduct]:
    if db.get(Lender, lender_id) is None:
        raise NotFound("Lender not found.")
    rows = db.scalars(select(LoanProduct).where(LoanProduct.lender_id == lender_id).order_by(LoanProduct.created_at))
    return [admin_out(db, row) for row in rows]


def get_admin(db: DB, product_id: str) -> s.AdminProduct:
    return admin_out(db, _product(db, product_id))


def _make_id(db: DB, lender_id: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "product"
    base = f"{lender_id[:80]}-{slug}"
    candidate, n = base, 2
    while db.get(LoanProduct, candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def create(db: DB, actor: Admin, lender_id: str, data: s.NewProduct) -> s.AdminProduct:
    """A new product is an empty draft: nothing is filled in for the admin."""
    lender = db.get(Lender, lender_id)
    if lender is None:
        raise NotFound("Lender not found.")
    now = now_utc()
    row = LoanProduct(
        id=_make_id(db, lender_id, data.name),
        lender_id=lender_id,
        status="draft",
        draft=_dump(s.ProductContent(name=data.name)),
        created_at=now,
        created_by=actor.name,
    )
    db.add(row)
    db.flush()
    return _saved(db, actor, row, action="product.create",
                  summary=f"Added loan product {data.name} for {lender.name} (draft)")


def save_draft(db: DB, actor: Admin, product_id: str, content: s.ProductContent) -> s.AdminProduct:
    """Saves the draft as typed; problems only block publishing."""
    row = _product(db, product_id)
    row.draft = _dump(content)
    return _saved(db, actor, row, action="product.draft", summary=f"Edited the draft of {content.name}")


def reset_draft(db: DB, actor: Admin, product_id: str) -> s.AdminProduct:
    """Throws the draft's changes away: back to the published version."""
    row = _product(db, product_id)
    published = _version(db, row.id, row.published_version)
    if published is None:
        raise AppError("This product has never been published, so there is nothing to go back to.")
    row.draft = _dump(_content(version_out(published)))
    return _saved(db, actor, row, action="product.draft_reset",
                  summary=f"Discarded draft changes to {published.name}")


def publish(db: DB, actor: Admin, product_id: str, body: s.PublishProduct) -> s.AdminProduct:
    """Freezes the draft into a new version (or republishes the last one if nothing changed)."""
    row = _product(db, product_id)
    content = s.ProductContent.model_validate(row.draft)
    problems = publish_problems(content, now_utc().date())
    if problems:
        raise AppError(" ".join(problems), code="not_publishable")
    current = admin_out(db, row)
    if current.published is not None and not current.draft_changed:
        if row.status == "published":
            raise AppError("This version is already published.")
        row.status = "published"
        return _saved(db, actor, row, action="product.publish",
                      summary=f"Published {content.name} again (version {row.published_version})")

    latest = db.scalar(
        select(func.max(LoanProductVersion.version)).where(LoanProductVersion.product_id == row.id)
    )
    number = (latest or 0) + 1
    terms = content.terms
    version = LoanProductVersion(
        product_id=row.id,
        version=number,
        name=content.name,
        min_amount=terms.min_amount,
        max_amount=terms.max_amount,
        interest_rate=terms.interest_rate,
        min_months=terms.min_months,
        max_months=terms.max_months,
        terms=_dump(terms),
        source=(content.source or "").strip(),
        source_checked_on=content.source_checked_on,
        note=body.note,
        published_at=now_utc(),
        published_by=actor.name,
    )
    db.add(version)
    db.flush()
    for position, requirement in enumerate(content.requirements):
        db.add(
            LoanRequirement(
                version_id=version.id,
                position=position,
                key=requirement.key,
                name=requirement.name,
                category=requirement.category,
                required=requirement.required,
                field_type=requirement.field_type,
                verification=requirement.verification,
                rules={k: v for k, v in _dump(requirement).items() if k not in REQUIREMENT_RULE_SKIP},
            )
        )
    row.status, row.published_version = "published", number
    return _saved(db, actor, row, action="product.publish",
                  summary=f"Published {content.name}, version {number}: {body.note}",
                  after={"version": number})


def unpublish(db: DB, actor: Admin, product_id: str) -> s.AdminProduct:
    row = _product(db, product_id)
    if row.status != "published":
        raise AppError("This product is not published.")
    row.status = "unpublished"
    return _saved(db, actor, row, action="product.unpublish",
                  summary=f"Unpublished {row.draft.get('name', row.id)}")


def delete(db: DB, actor: Admin, product_id: str) -> None:
    """Only a product that was never published: unpublish a published one instead."""
    row = _product(db, product_id)
    if row.published_version is not None:
        raise AppError("This product has been published, so its history is kept. Unpublish it instead.")
    name = row.draft.get("name", row.id)
    db.delete(row)
    append_audit(db, actor, action="product.delete", target_type="loan_product", target_id=product_id,
                 summary=f"Removed the draft product {name}")
    db.commit()
    bus.publish("platform")


def versions(db: DB, product_id: str) -> list[s.ProductVersion]:
    _product(db, product_id)
    rows = db.scalars(
        select(LoanProductVersion)
        .where(LoanProductVersion.product_id == product_id)
        .order_by(LoanProductVersion.version.desc())
    )
    return [version_out(row) for row in rows]


# ---------- Farmers ----------


def _public(row: LoanProduct, lender: Lender, version: LoanProductVersion) -> s.LoanProduct:
    return s.LoanProduct(
        **version_out(version).model_dump(),
        id=row.id,
        lender=s.LenderSummary(
            id=lender.id,
            name=lender.name,
            color=lender.color,
            logo_path=lender_logo_path(lender),
            partnership_status=lender.partnership_status,
        ),
    )


def listed_products(db: DB) -> list[s.LoanProduct]:
    """Every farmer sees the same products: published, of a listed lender, latest version."""
    rows = db.execute(
        select(LoanProduct, Lender)
        .join(Lender, Lender.id == LoanProduct.lender_id)
        .where(LoanProduct.status == "published")
        .order_by(Lender.name, LoanProduct.created_at)
    ).all()
    out = []
    for product, lender in rows:
        if not lending.is_listed(lender):
            continue
        version = _version(db, product.id, product.published_version)
        if version is not None:
            out.append(_public(product, lender, version))
    return out


def listed_product(db: DB, product_id: str) -> s.LoanProduct:
    row = db.get(LoanProduct, product_id)
    lender = db.get(Lender, row.lender_id) if row else None
    version = _version(db, row.id, row.published_version) if row else None
    if row is None or version is None or not _listed(row, lender):
        raise NotFound("This loan product is not available.")
    return _public(row, lender, version)  # type: ignore[arg-type]


def has_listed_products(db: DB, lender_id: str) -> bool:
    return any(p.lender.id == lender_id for p in listed_products(db))

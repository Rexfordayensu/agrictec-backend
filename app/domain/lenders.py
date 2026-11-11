"""Which lenders farmers may see. Pure rules, no database.

A lender reaches the farmer marketplace only when every one of these holds:
- an administrator added it and published it (not a draft, not unpublished or suspended);
- its licence was checked against an official source and found valid, recently enough;
- its public listing on AgriCtec was authorised.

Regulatory verification and partnership are separate: a licensed institution has not
necessarily agreed to receive applications through AgriCtec. Nothing here invents a lender."""

from datetime import datetime, timedelta

INSTITUTION_TYPES = [
    "universal_bank",
    "rural_community_bank",
    "savings_and_loans",
    "microfinance",
    "finance_house",
    "credit_union",
    "development_finance",
    "other",
]

# "verified": checked against the regulator's own register or notice.
LICENCE_STATUSES = ["unverified", "verified", "expired", "revoked"]
PUBLICATION_STATUSES = ["draft", "published", "unpublished", "suspended"]
# "partner": the institution agreed with AgriCtec to receive applications through the platform.
PARTNERSHIP_STATUSES = ["none", "in_discussion", "partner"]

# A verification older than this must be reviewed again before the lender stays listed.
VERIFICATION_VALID_DAYS = 365
# Shown to admins as "review due" this long before the verification lapses.
REVIEW_WARNING_DAYS = 30

# Changing any of these means the institution may no longer be the one that was checked.
IDENTITY_FIELDS = {"name", "registered_name", "licence_ref", "regulator", "institution_type"}


def verification_expires(last_checked: datetime | None) -> datetime | None:
    return last_checked + timedelta(days=VERIFICATION_VALID_DAYS) if last_checked else None


def verification_current(licence_status: str, last_checked: datetime | None, now: datetime) -> bool:
    expires = verification_expires(last_checked)
    return licence_status == "verified" and expires is not None and now < expires


def review_due(licence_status: str, last_checked: datetime | None, now: datetime) -> bool:
    expires = verification_expires(last_checked)
    return (
        licence_status == "verified"
        and expires is not None
        and now >= expires - timedelta(days=REVIEW_WARNING_DAYS)
    )


def publish_blockers(
    *,
    licence_status: str,
    last_checked: datetime | None,
    verification_source: str | None,
    listing_authorised: bool,
    now: datetime,
) -> list[str]:
    """Why this lender may not be published (empty: it may)."""
    blockers: list[str] = []
    if licence_status != "verified":
        blockers.append("Its licence has not been verified against an official source.")
    elif not verification_current(licence_status, last_checked, now):
        blockers.append(
            f"Its licence check is more than {VERIFICATION_VALID_DAYS} days old. Review it again."
        )
    if not (verification_source or "").strip():
        blockers.append("The official source of the licence check is missing.")
    if not listing_authorised:
        blockers.append("Its public listing on AgriCtec has not been authorised.")
    return blockers


def listing_blockers(
    *,
    publication_status: str,
    licence_status: str,
    last_checked: datetime | None,
    verification_source: str | None,
    listing_authorised: bool,
    now: datetime,
) -> list[str]:
    """Why farmers can't see this lender right now (empty: they can)."""
    reasons: list[str] = []
    if publication_status == "suspended":
        reasons.append("It is suspended.")
    elif publication_status != "published":
        reasons.append("It is not published.")
    return reasons + publish_blockers(
        licence_status=licence_status,
        last_checked=last_checked,
        verification_source=verification_source,
        listing_authorised=listing_authorised,
        now=now,
    )

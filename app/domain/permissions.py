"""Role-based access control (lib/admin/permissions.ts, admin spec Section 2).

Every admin endpoint checks a permission here; hiding a button in the console is never the
only guard."""

from typing import Literal

AdminRole = Literal["analyst", "support", "admin", "super_admin"]
Permission = Literal[
    "overview.view",
    "farmers.view",
    "farmers.edit",
    "farmers.suspend",
    "farmers.delete",
    "farmers.impersonate",
    "verifications.review",
    "loans.view",
    "loans.manage",
    "lenders.manage",
    "credit.configure",
    "content.manage",
    "announcements.send",
    "risk.review",
    "reports.export",
    "admins.manage",
    "audit.view",
    "settings.manage",
    "data.manage",
]

ADMIN_ROLES: list[str] = ["analyst", "support", "admin", "super_admin"]

ALL_PERMISSIONS: list[str] = [
    "overview.view",
    "farmers.view",
    "farmers.edit",
    "farmers.suspend",
    "farmers.delete",
    "farmers.impersonate",
    "verifications.review",
    "loans.view",
    "loans.manage",
    "lenders.manage",
    "credit.configure",
    "content.manage",
    "announcements.send",
    "risk.review",
    "reports.export",
    "admins.manage",
    "audit.view",
    "settings.manage",
    "data.manage",
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "analyst": ["overview.view", "farmers.view", "loans.view", "reports.export"],
    "support": [
        "overview.view",
        "farmers.view",
        "farmers.edit",
        "farmers.impersonate",
        "verifications.review",
        "loans.view",
        "risk.review",
    ],
    "admin": [
        "overview.view",
        "farmers.view",
        "farmers.edit",
        "farmers.suspend",
        "farmers.impersonate",
        "verifications.review",
        "loans.view",
        "loans.manage",
        "lenders.manage",
        "content.manage",
        "announcements.send",
        "risk.review",
        "reports.export",
        "audit.view",
    ],
    "super_admin": list(ALL_PERMISSIONS),
}

ROLE_LABELS = {"analyst": "Analyst", "support": "Support", "admin": "Admin", "super_admin": "Super Admin"}
ROLE_RANK = {"analyst": 1, "support": 2, "admin": 3, "super_admin": 4}

# Completes "You do not have permission to ...".
PERMISSION_LABELS: dict[str, str] = {
    "overview.view": "view the overview",
    "farmers.view": "view farmers",
    "farmers.edit": "add farmer notes and tags or reset their passwords",
    "farmers.suspend": "suspend farmers",
    "farmers.delete": "permanently delete farmers",
    "farmers.impersonate": "view the app as a farmer",
    "verifications.review": "review verifications",
    "loans.view": "view loan applications",
    "loans.manage": "manage loan applications",
    "lenders.manage": "manage lenders",
    "credit.configure": "change the credit rules",
    "content.manage": "manage content",
    "announcements.send": "send announcements",
    "risk.review": "review risk flags",
    "reports.export": "export reports",
    "admins.manage": "manage the admin team",
    "audit.view": "view the audit log",
    "settings.manage": "change system settings",
    "data.manage": "manage platform data",
}


def can(role: str | None, permission: str) -> bool:
    """Deny by default: a farmer, a missing role or an unknown role has no admin permission."""
    return permission in ROLE_PERMISSIONS.get(role or "", [])


def permissions_of(role: str) -> list[str]:
    return list(ROLE_PERMISSIONS.get(role, []))


def can_manage_role(
    actor: str, target: str, *, next_role: str | None = None, active_super_admins: int | None = None
) -> bool:
    """Only a super admin manages roles, and the last active super admin can never be demoted
    or disabled (next_role 'disabled'), so the console can never be locked out."""
    if actor != "super_admin":
        return False
    if (
        next_role is not None
        and target == "super_admin"
        and next_role != "super_admin"
        and (active_super_admins or 0) <= 1
    ):
        return False
    return True


def permission_message(permission: str) -> str:
    return f"You do not have permission to {PERMISSION_LABELS[permission]}."


AUTH_ACTIONS = ["admin.login", "admin.logout", "admin.password_change"]


def audit_group_of(action: str) -> str:
    """Which audit-log group an action belongs to (lib/admin/audit-groups.ts)."""
    if action in AUTH_ACTIONS:
        return "auth"
    prefix = action.split(".")[0]
    return {
        "farmer": "farmer",
        "verification": "farmer",
        "risk": "farmer",
        "loan": "loan",
        "lender": "loan",
        "credit": "config",
        "content": "content",
        "announcement": "content",
        "admin": "team",
    }.get(prefix, "system")

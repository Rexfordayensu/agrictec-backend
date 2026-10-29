"""The admin team (services/admin/team.service.ts). Nobody changes their own access, and the
last active super admin can never be demoted or disabled."""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.security import hash_password
from app.core.sessions import end_sessions_for
from app.domain.clock import now_utc
from app.domain.passwords import make_temp_password
from app.domain.permissions import ROLE_LABELS, ROLE_RANK, can_manage_role
from app.models import Admin, User
from app.schemas.admin import AdminUser, InviteResult
from app.services.mappers import admin_out

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LAST_SUPER_ADMIN = "There must always be at least one active Super Admin."


def _find(db: DB, admin_id: str) -> Admin:
    row = db.get(Admin, admin_id)
    if row is None:
        raise NotFound("Team member not found.")
    return row


def _active_super_admins(db: DB) -> int:
    return db.scalar(select(func.count()).where(Admin.role == "super_admin", Admin.status == "active")) or 0


def _not_self(actor: Admin, admin_id: str) -> None:
    if actor.id == admin_id:
        raise Forbidden("You can’t change your own access. Ask another Super Admin.")


def list_team(db: DB) -> list[AdminUser]:
    """Highest role first, then by name."""
    members = [admin_out(r) for r in db.scalars(select(Admin))]
    return sorted(members, key=lambda m: (-ROLE_RANK[m.role], m.name))


def invite(db: DB, actor: Admin, name: str, email: str, role: str) -> InviteResult:
    """The account gets a temporary password the new admin must change at first sign-in."""
    if not can_manage_role(actor.role, role):
        raise Forbidden("Only a Super Admin can add team members.")
    clean_name = name.strip()
    normalized = email.strip().lower()
    if len(clean_name) < 2:
        raise AppError("Enter the team member’s full name.")
    if not EMAIL.match(normalized):
        raise AppError("Enter a valid email address.")
    if db.scalar(select(Admin.id).where(Admin.email == normalized)):
        raise Conflict("A team member with this email already exists.")
    # Staff and farmer sign-ins are separate: one email is never both.
    if db.scalar(select(User.id).where(User.email == normalized, User.is_guest.is_(False))):
        raise Conflict("This email belongs to a farmer account. Use a different email for staff.")
    temporary = make_temp_password()
    row = Admin(
        name=clean_name, email=normalized, role=role, status="active", must_change_password=True,
        created_at=now_utc(), created_by=actor.id, password_hash=hash_password(temporary),
    )
    db.add(row)
    db.flush()
    member = admin_out(row)
    append_audit(db, actor, action="admin.invite", target_type="admin", target_id=row.id,
                 summary=f"Invited {clean_name} as {ROLE_LABELS[role]}", after=member)
    db.commit()
    return InviteResult(admin=member, temporary_password=temporary)


def change_role(db: DB, actor: Admin, admin_id: str, role: str) -> AdminUser:
    _not_self(actor, admin_id)
    target = _find(db, admin_id)
    if target.role == role:
        raise AppError(f"{target.name} is already {ROLE_LABELS[role]}.")
    if not can_manage_role(actor.role, target.role, next_role=role, active_super_admins=_active_super_admins(db)):
        raise Forbidden(LAST_SUPER_ADMIN)
    before = target.role
    target.role = role
    append_audit(db, actor, action="admin.role", target_type="admin", target_id=admin_id,
                 summary=f"Changed {target.name} from {ROLE_LABELS[before]} to {ROLE_LABELS[role]}",
                 before={"role": before}, after={"role": role})
    db.commit()
    return admin_out(target)


def disable(db: DB, actor: Admin, admin_id: str) -> AdminUser:
    _not_self(actor, admin_id)
    target = _find(db, admin_id)
    if target.status == "disabled":
        raise AppError(f"{target.name} is already disabled.")
    if not can_manage_role(actor.role, target.role, next_role="disabled", active_super_admins=_active_super_admins(db)):
        raise Forbidden(LAST_SUPER_ADMIN)
    target.status = "disabled"
    end_sessions_for(db, admin_id=admin_id)
    append_audit(db, actor, action="admin.disable", target_type="admin", target_id=admin_id,
                 summary=f"Disabled {target.name}", before={"status": "active"}, after={"status": "disabled"})
    db.commit()
    return admin_out(target)


def enable(db: DB, actor: Admin, admin_id: str) -> AdminUser:
    target = _find(db, admin_id)
    if target.status == "active":
        raise AppError(f"{target.name} is already active.")
    target.status = "active"
    append_audit(db, actor, action="admin.enable", target_type="admin", target_id=admin_id,
                 summary=f"Enabled {target.name}", before={"status": "disabled"}, after={"status": "active"})
    db.commit()
    return admin_out(target)


def reset_password(db: DB, actor: Admin, admin_id: str) -> str:
    """A new temporary password (returned once, never logged); the admin must change it."""
    _not_self(actor, admin_id)
    target = _find(db, admin_id)
    temporary = make_temp_password()
    target.password_hash = hash_password(temporary)
    target.must_change_password = True
    end_sessions_for(db, admin_id=admin_id)
    append_audit(db, actor, action="admin.reset_password", target_type="admin", target_id=admin_id,
                 summary=f"Reset the password for {target.name}", after={"reset": True})
    db.commit()
    return temporary

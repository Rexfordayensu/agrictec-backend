"""Admin sign-in (/admin/auth). Separate from the farmer's: neither signs the other out."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session as DB

from app.core.deps import AdminContext, admin_context
from app.core.ratelimit import rate_limit
from app.db import get_db
from app.routers.common import clear_admin_cookies, set_admin_cookies
from app.schemas import admin as a
from app.schemas.base import Message
from app.schemas.farm import Credentials
from app.services import admin_auth

router = APIRouter(prefix="/admin/auth", tags=["Admin auth"])
Database = Annotated[DB, Depends(get_db)]
Signed = Annotated[AdminContext, Depends(admin_context)]


@router.post("/login", response_model=a.AdminSignedIn, dependencies=[Depends(rate_limit("admin-login", 30, 900))])
def sign_in(body: Credentials, response: Response, db: Database):
    """Five failed attempts lock the email for five minutes (423). An admin on a temporary
    password signs in with mustChangePassword true and can only change it."""
    token, session = admin_auth.sign_in(db, body.email, body.password)
    set_admin_cookies(response, token, session.admin.role)
    return a.AdminSignedIn(session=session, token=token)


@router.get("/lockout", response_model=a.LockStatus)
def lockout(email: Annotated[str, Query(min_length=3, max_length=254)], db: Database):
    return admin_auth.lock_status(db, email)


@router.get("/session", response_model=a.AdminSession)
def get_session(ctx: Signed):
    """The admin re-read from the team, so role changes apply at once. 401 "session_expired"
    after 30 minutes without activity."""
    return admin_auth.session_out(ctx.admin, ctx.session)


@router.post("/touch", response_model=a.AdminSession)
def touch(ctx: Signed, db: Database):
    """Records keyboard, mouse or touch activity (keeps the session from going idle)."""
    admin_auth.touch(db, ctx.session)
    return admin_auth.session_out(ctx.admin, ctx.session)


@router.post("/logout", response_model=Message)
def sign_out(
    response: Response, ctx: Signed, db: Database, reason: Literal["manual", "timeout"] = "manual"
):
    """reason=timeout when the console's idle countdown ran out (logged as such)."""
    admin_auth.end_admin_session(db, ctx.session, ctx.admin, reason)
    clear_admin_cookies(response)
    return Message(detail="Signed out.")


@router.post("/change-password", response_model=a.AdminSession)
def change_password(body: a.ChangePassword, ctx: Signed, db: Database):
    """Also clears a forced change (mustChangePassword)."""
    admin_auth.change_password(db, ctx.admin, body.current_password, body.new_password)
    return admin_auth.session_out(ctx.admin, ctx.session)


@router.patch("/me", response_model=a.AdminSession)
def rename(body: a.Rename, ctx: Signed, db: Database):
    admin_auth.rename(db, ctx.admin, body.name)
    return admin_auth.session_out(ctx.admin, ctx.session)

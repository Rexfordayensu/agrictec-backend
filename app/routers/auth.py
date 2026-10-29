"""Farmer sign-in (/auth) and the read-only "view as farmer" handshake."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session as DB

from app.core.deps import AdminContext, FarmerContext, admin_context, farmer_context
from app.core.errors import AppError
from app.core.ratelimit import rate_limit
from app.core.sessions import GUEST_COOKIE, IMPERSONATION_HEADER, end_session, find_session, set_cookie
from app.db import get_db
from app.domain.clock import now_utc
from app.models import Admin, Profile, User
from app.routers.common import YEAR, clear_farmer_cookies, set_farmer_cookies
from app.schemas.base import CamelModel, Message
from app.schemas.farm import Credentials, ReplaceTemporaryPassword, Session, SignedIn, SignUp
from app.services import admin_farmers, auth

router = APIRouter(prefix="/auth", tags=["Farmer auth"])


def _onboarded(db: DB, user_id: str) -> bool:
    row = db.get(Profile, user_id)
    return bool(row and row.onboarding_completed_at)


def _signed_in(db: DB, response: Response, token: str, session: Session) -> SignedIn:
    set_farmer_cookies(response, token, _onboarded(db, session.user_id))
    return SignedIn(session=session, token=token)


@router.post("/signup", response_model=SignedIn, status_code=201, dependencies=[Depends(rate_limit("signup", 10, 3600))])
def sign_up(body: SignUp, response: Response, db: DB = Depends(get_db)):
    token, session = auth.sign_up(db, body.email, body.password)
    return _signed_in(db, response, token, session)


@router.post("/login", response_model=SignedIn, dependencies=[Depends(rate_limit("login", 20, 900))])
def sign_in(body: Credentials, response: Response, db: DB = Depends(get_db)):
    """409 with code "password_change_required" after an admin reset: call
    /auth/replace-temporary-password with the temporary and the new password."""
    token, session = auth.sign_in(db, body.email, body.password)
    return _signed_in(db, response, token, session)


@router.post("/replace-temporary-password", response_model=SignedIn, dependencies=[Depends(rate_limit("login", 20, 900))])
def replace_temporary_password(body: ReplaceTemporaryPassword, response: Response, db: DB = Depends(get_db)):
    token, session = auth.replace_temporary_password(db, body.email, body.temporary_password, body.new_password)
    return _signed_in(db, response, token, session)


@router.post("/guest", response_model=SignedIn, dependencies=[Depends(rate_limit("guest", 10, 3600))])
def continue_as_guest(request: Request, response: Response, db: DB = Depends(get_db)):
    """One guest per device, remembered by a signed cookie, so demo data never vanishes."""
    token, session, device = auth.continue_as_guest(db, request.cookies.get(GUEST_COOKIE))
    set_cookie(response, GUEST_COOKIE, device, YEAR)
    return _signed_in(db, response, token, session)


@router.get("/session", response_model=Session)
def get_session(ctx: FarmerContext = Depends(farmer_context)):
    """The signed-in farmer (401 when signed out, 403 "account_suspended" when suspended)."""
    return auth.session_out(ctx.user, ctx.session, ctx.read_only)


@router.post("/logout", response_model=Message)
def sign_out(response: Response, ctx: FarmerContext = Depends(farmer_context), db: DB = Depends(get_db)):
    if not ctx.read_only:
        auth.sign_out(db, ctx.session)
        clear_farmer_cookies(response)
    return Message(detail="Signed out.")


# ---------- View as farmer ----------


class ClaimBody(CamelModel):
    token: str


class Claimed(CamelModel):
    # Send as the X-Impersonation-Token header from the viewing tab only.
    token: str
    session: Session


@router.post("/impersonation/claim", response_model=Claimed, tags=["View as farmer"])
def claim_impersonation(body: ClaimBody, admin: AdminContext = Depends(admin_context), db: DB = Depends(get_db)):
    """Exchanges the console's one-minute token for a read-only session. Kept per tab (sent as a
    header), so the admin's other tabs, and any farmer session in the browser, are unaffected."""
    token = admin_farmers.claim_impersonation(db, admin.admin, body.token)
    record = find_session(db, token, ("impersonation",), now_utc())
    user = db.get(User, record.user_id) if record and record.user_id else None
    if record is None or user is None:
        raise AppError("The view could not start. Try again.")
    return Claimed(token=token, session=auth.session_out(user, record, read_only=True))


@router.post("/impersonation/end", response_model=Message, tags=["View as farmer"])
def end_impersonation(request: Request, db: DB = Depends(get_db)):
    """Ends this tab's view and logs it."""
    record = find_session(db, request.headers.get(IMPERSONATION_HEADER), ("impersonation",), now_utc())
    if record:
        end_session(record)
        admin = db.get(Admin, record.admin_id) if record.admin_id else None
        if admin and record.user_id:
            admin_farmers.end_impersonation(db, admin, record.user_id)
        else:
            db.commit()
    return Message(detail="Stopped viewing as farmer.")

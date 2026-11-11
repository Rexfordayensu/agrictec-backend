"""Ghana Card identity verification for the signed-in farmer (app/services/ghana_card_verification.py).

Only the server decides whether a farmer is verified: these routes never accept a "verified"
value from the browser, and a result comes only from the configured provider (NIA's approved
platform; in development, a clearly simulated stand-in)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session as DB

from app.core.deps import FarmerContext, farmer_context, writable_farmer
from app.core.ratelimit import rate_limit
from app.db import get_db
from app.integrations.identity_providers import IdentityProvider, get_identity_provider
from app.schemas.base import CamelModel
from app.services.ghana_card_verification import GhanaCardVerificationService

router = APIRouter(prefix="/identity-verification", tags=["Identity verification"])

Reader = Annotated[FarmerContext, Depends(farmer_context)]
Writer = Annotated[FarmerContext, Depends(writable_farmer)]
Database = Annotated[DB, Depends(get_db)]
Provider = Annotated[IdentityProvider | None, Depends(get_identity_provider)]


class StartVerification(CamelModel):
    # The farmer read the privacy notice and agreed; the version they saw.
    consent: bool
    consent_version: str
    # Development only (the simulated provider): the outcome to simulate. Ignored by NIA.
    simulate: Literal["verified", "failed", "requires_review", "processing"] | None = None


def _service(db: DB, provider: IdentityProvider | None) -> GhanaCardVerificationService:
    return GhanaCardVerificationService(db, provider)


def _options(body: StartVerification, provider: IdentityProvider | None) -> dict[str, str]:
    return {"simulate": body.simulate} if body.simulate and provider is not None and provider.simulated else {}


@router.get("/status")
def status(ctx: Reader, db: Database, provider: Provider):
    """The farmer's identity level, latest attempt, whether they can start or retry, and which
    features need which level."""
    return _service(db, provider).status(ctx.user.id)


@router.get("/history")
def history(ctx: Reader, db: Database, provider: Provider):
    """Past attempts, without provider references or reviewer notes."""
    return _service(db, provider).history(ctx.user.id)


@router.post("/start", status_code=201, dependencies=[Depends(rate_limit("identity-start", 10, 3600))])
def start(body: StartVerification, ctx: Writer, db: Database, provider: Provider):
    """Starts a session with the provider after consent. 503 "provider_unavailable" while NIA
    verification isn't live; 409 when already verified or a session is open."""
    return _service(db, provider).start(
        ctx.user.id, consent=body.consent, consent_version=body.consent_version, options=_options(body, provider)
    )


@router.post("/retry", status_code=201, dependencies=[Depends(rate_limit("identity-start", 10, 3600))])
def retry(body: StartVerification, ctx: Writer, db: Database, provider: Provider):
    """A new attempt after a failed or expired one (consent is asked again)."""
    return _service(db, provider).start(
        ctx.user.id, consent=body.consent, consent_version=body.consent_version, options=_options(body, provider),
        retry=True,
    )


@router.post("/provider-callback", dependencies=[Depends(rate_limit("identity-callback", 120, 60))])
async def provider_callback(request: Request, db: Database, provider: Provider):
    """For a provider that sends results. Off (404) until the provider's documentation defines
    how callbacks are authenticated; the provider adapter checks every callback."""
    body = await request.body()
    return {"result": _service(db, provider).handle_callback(dict(request.headers), body)}

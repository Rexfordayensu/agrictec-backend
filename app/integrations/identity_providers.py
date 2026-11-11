"""Ghana Card identity verification providers (used by app/services/ghana_card_verification.py).

`IdentityProvider` is what the verification service needs from a provider. Two exist:

- `NIAProvider`: the National Identification Authority's approved Identity Verification System
  Platform (IVSP). Nothing about NIA's API is written here yet: no endpoint, authentication
  scheme, request field, response format or biometric step. Those come only from NIA's official
  technical documentation after onboarding (see the README, "Ghana Card identity verification").
  Until then the provider reports itself unavailable, and every call raises
  `ProviderUnavailable`, so no farmer is ever shown as verified by NIA.
- `SimulatedProvider`: a development-only stand-in for building and testing the flow. It is
  refused in production (settings validation and here), labels every answer as simulated, and
  its results never count as verified in production (the service filters them out).

Smile ID (app/integrations/identity.py) is separate: advice for an admin reviewing a typed-in card
number, never a verification of identity.

`get_identity_provider` is a FastAPI dependency, so tests and future providers replace it without
touching the service.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from app.config import Settings, get_settings

ResultStatus = Literal["processing", "verified", "failed", "requires_review", "expired"]


@dataclass(frozen=True)
class ProviderSession:
    """A started verification session, as the provider describes it."""

    reference: str
    # What the farmer does next, as the provider states it (shown on the verification page).
    instructions: str
    # Where the farmer completes the provider's own Ghana Card and biometric process, if any.
    redirect_url: str | None = None
    expires_in_seconds: int | None = None


@dataclass(frozen=True)
class ProviderResult:
    """The provider's authenticated answer for one session."""

    reference: str
    status: ResultStatus
    # As the provider reports it (e.g. "biometric"); never inferred here.
    method: str | None = None
    # The provider's reason, mapped by the service onto its own short list of safe codes.
    failure_code: str | None = None
    # The provider's event ID for a callback, so a replay is ignored.
    event_id: str | None = None


class ProviderUnavailable(Exception):
    """The provider can't be used right now: not set up, switched off, or not responding."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class TransientProviderError(Exception):
    """A timeout or a temporary failure: safe to try again (never a biometric step)."""


class CallbackRejected(Exception):
    """A callback that isn't authentic, is stale, or is malformed."""


class IdentityProvider(Protocol):
    name: str
    #: True for the development stand-in: its results never count in production.
    simulated: bool
    #: Whether the provider sends results to /identity-verification/provider-callback.
    supports_callback: bool

    def unavailable_reason(self) -> str | None:
        """None when verification can start, else a short reason code."""

    def start(self, *, attempt_id: str, user_id: str, options: Mapping[str, str]) -> ProviderSession: ...

    def result(self, reference: str) -> ProviderResult: ...

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> ProviderResult: ...


# ---------- NIA ----------

NIA_PENDING_DOCUMENTATION = (
    "Ghana Card verification with NIA is not available yet. AgriCtec is completing onboarding "
    "with the National Identification Authority."
)


class NIAProvider:
    """NIA's approved verification platform. Intentionally without an API contract until NIA's
    official documentation and credentials are supplied: implement `start`, `result` and (if
    NIA documents callbacks) `parse_callback` from that documentation, then set
    `CONTRACT_IMPLEMENTED = True`."""

    name = "nia"
    simulated = False
    # Off until NIA's documentation confirms callbacks and how they are authenticated.
    supports_callback = False
    CONTRACT_IMPLEMENTED = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def unavailable_reason(self) -> str | None:
        s = self.settings
        if not s.nia_verification_enabled:
            return "disabled"
        if not (s.nia_api_base_url and s.nia_api_credential and s.nia_api_secret):
            return "not_configured"
        if not self.CONTRACT_IMPLEMENTED:
            return "awaiting_documentation"
        return None

    def _refuse(self) -> None:
        raise ProviderUnavailable(self.unavailable_reason() or "awaiting_documentation", NIA_PENDING_DOCUMENTATION)

    def start(self, *, attempt_id: str, user_id: str, options: Mapping[str, str]) -> ProviderSession:
        self._refuse()
        raise AssertionError("unreachable")

    def result(self, reference: str) -> ProviderResult:
        self._refuse()
        raise AssertionError("unreachable")

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> ProviderResult:
        raise CallbackRejected("NIA callbacks are not enabled.")


# ---------- Development only ----------

SIMULATED_OUTCOMES = ("verified", "failed", "requires_review", "processing")
SIMULATED_NOTICE = (
    "Simulated verification (development only). No Ghana Card, NIA record or biometric check is "
    "involved, and the result never counts on the live platform."
)


class SimulatedProvider:
    """Answers with the outcome the developer picks, so every screen and rule can be tested
    without a real Ghana Card. Never available in production."""

    name = "simulated"
    simulated = True
    supports_callback = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def unavailable_reason(self) -> str | None:
        return "production" if self.settings.environment == "production" else None

    def start(self, *, attempt_id: str, user_id: str, options: Mapping[str, str]) -> ProviderSession:
        if self.unavailable_reason():
            raise ProviderUnavailable("production", "Simulated verification is never available on the live platform.")
        outcome = options.get("simulate") or "verified"
        if outcome not in SIMULATED_OUTCOMES:
            outcome = "verified"
        return ProviderSession(
            reference=f"SIM-{outcome}-{attempt_id}",
            instructions=SIMULATED_NOTICE,
            expires_in_seconds=30 * 60,
        )

    def result(self, reference: str) -> ProviderResult:
        if self.unavailable_reason():
            raise ProviderUnavailable("production", "Simulated verification is never available on the live platform.")
        outcome = reference.split("-")[1] if reference.startswith("SIM-") else "failed"
        status: ResultStatus = outcome if outcome in SIMULATED_OUTCOMES else "failed"  # type: ignore[assignment]
        return ProviderResult(
            reference=reference,
            status=status,
            method="simulated",
            failure_code="biometric_mismatch" if status == "failed" else None,
        )

    def parse_callback(self, headers: Mapping[str, str], body: bytes) -> ProviderResult:
        raise CallbackRejected("The simulated provider sends no callbacks.")


def get_identity_provider() -> IdentityProvider | None:
    """The configured provider (IDENTITY_PROVIDER), or None when verification isn't offered."""
    settings = get_settings()
    if settings.identity_provider == "nia":
        return NIAProvider(settings)
    if settings.identity_provider == "simulated" and settings.environment != "production":
        return SimulatedProvider(settings)
    return None

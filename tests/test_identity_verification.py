"""Ghana Card identity verification: the provider decides, never the browser or an admin; the
development stand-in never counts on the live platform; NIA stays unavailable until onboarded.
No test uses real Ghana Card information."""

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.domain.clock import now_utc
from app.integrations.identity_providers import (
    CallbackRejected,
    NIAProvider,
    ProviderResult,
    ProviderSession,
    ProviderUnavailable,
    TransientProviderError,
    get_identity_provider,
)
from app.main import app
from app.models import AuditEntry, IdentityVerification, Notification, Profile
from app.services.ghana_card_verification import GhanaCardVerificationService, identity_level
from tests.conftest import ADMIN_PASSWORD, API, ONBOARDING, new_client, signup, super_admin

IV = f"{API}/identity-verification"


def consent(**extra):
    return {"consent": True, "consentVersion": get_settings().identity_consent_version, **extra}


def farmer(email="ama@example.com"):
    c = signup(email)
    assert c.post(f"{API}/me/onboarding/complete", json=ONBOARDING).status_code == 200
    return c


def user_id(client) -> str:
    return client.get(f"{API}/me/profile").json()["userId"]


@pytest.fixture
def simulated(monkeypatch):
    monkeypatch.setattr(get_settings(), "identity_provider", "simulated")


class SignedProvider:
    """A test provider that sends signed callbacks, standing in for a documented real one."""

    name = "test-provider"
    simulated = False
    supports_callback = True
    secret = b"test-callback-secret"

    def unavailable_reason(self):
        return None

    def start(self, *, attempt_id, user_id, options):
        return ProviderSession(reference=f"REF-{attempt_id}", instructions="Follow the provider's steps.",
                               redirect_url="https://provider.example/session", expires_in_seconds=600)

    def result(self, reference):
        raise TransientProviderError()

    def parse_callback(self, headers, body):
        stamp = headers.get("x-test-timestamp", "0")
        expected = hmac.new(self.secret, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("x-test-signature", ""), expected):
            raise CallbackRejected("bad signature")
        if abs(time.time() - int(stamp)) > 300:
            raise CallbackRejected("stale")
        data = json.loads(body)
        return ProviderResult(reference=data["reference"], status=data["status"], method="biometric",
                              event_id=data["eventId"], failure_code=data.get("failureCode"))


def signed(provider, payload, *, stamp=None, secret=None):
    body = json.dumps(payload).encode()
    stamp = str(stamp if stamp is not None else int(time.time()))
    sig = hmac.new(secret or provider.secret, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {"x-test-timestamp": stamp, "x-test-signature": sig, "content-type": "application/json"}


@pytest.fixture
def provider():
    p = SignedProvider()
    app.dependency_overrides[get_identity_provider] = lambda: p
    yield p
    app.dependency_overrides.pop(get_identity_provider, None)


# ---------- Not available until NIA onboarding ----------


def test_without_a_provider_nothing_starts_and_nothing_is_recorded():
    c = farmer()
    s = c.get(f"{IV}/status").json()
    assert s["status"] == "not_started" and s["level"] == "none"
    assert s["provider"] == {"name": None, "available": False, "reason": "not_offered", "simulated": False}
    r = c.post(f"{IV}/start", json=consent())
    assert r.status_code == 503 and r.json()["code"] == "provider_unavailable"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(IdentityVerification)) == 0


def test_nia_stays_unavailable_until_documented_never_inventing_a_connection(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "identity_provider", "nia")
    c = farmer()
    assert c.get(f"{IV}/status").json()["provider"]["reason"] == "disabled"
    monkeypatch.setattr(settings, "nia_verification_enabled", True)
    assert c.get(f"{IV}/status").json()["provider"]["reason"] == "not_configured"
    for key, value in (("nia_api_base_url", "https://example.invalid"), ("nia_api_credential", "x"), ("nia_api_secret", "y")):
        monkeypatch.setattr(settings, key, value)
    # Fully configured, but the API contract is written only from NIA's official documentation.
    assert c.get(f"{IV}/status").json()["provider"]["reason"] == "awaiting_documentation"
    r = c.post(f"{IV}/start", json=consent())
    assert r.status_code == 503 and "not available yet" in r.json()["detail"]
    with pytest.raises(ProviderUnavailable):
        NIAProvider(settings).start(attempt_id="a", user_id="u", options={})
    assert c.post(f"{IV}/provider-callback", content=b"{}").status_code == 404


# ---------- The flow (development stand-in) ----------


def test_consent_is_required_and_must_match_the_current_notice(simulated):
    c = farmer()
    assert c.post(f"{IV}/start", json={**consent(), "consent": False}).json()["code"] == "consent_required"
    r = c.post(f"{IV}/start", json={**consent(), "consentVersion": "2020-01-01"})
    assert r.status_code == 400 and r.json()["consentVersion"] == get_settings().identity_consent_version


def test_a_successful_check_verifies_and_is_labelled_simulated(simulated):
    c = farmer()
    started = c.post(f"{IV}/start", json=consent(simulate="verified"))
    assert started.status_code == 201, started.text
    assert started.json()["attempt"]["simulated"] is True and "Simulated" in started.json()["instructions"]
    s = c.get(f"{IV}/status").json()
    assert s["status"] == "verified" and s["level"] == "nia_verified" and s["attempt"]["verifiedAt"]
    assert "verificationReference" not in s["attempt"]  # never shown
    assert c.post(f"{IV}/start", json=consent()).json()["code"] == "already_verified"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Notification).where(Notification.kind == "verification")) == 1


def test_a_failed_check_can_be_retried_and_attempts_are_capped(simulated):
    c = farmer()
    c.post(f"{IV}/start", json=consent(simulate="failed"))
    s = c.get(f"{IV}/status").json()
    assert s["status"] == "failed" and s["attempt"]["failureCode"] == "biometric_mismatch"
    assert s["level"] == "none" and s["canRetry"] is True
    # A failed check never deactivates the account.
    assert c.get(f"{API}/me/profile").status_code == 200
    assert c.post(f"{IV}/start", json=consent()).json()["code"] == "retry_required"
    again = c.post(f"{IV}/retry", json=consent(simulate="failed"))
    assert again.status_code == 201 and again.json()["attempt"]["attemptCount"] == 2
    c.get(f"{IV}/status")
    c.post(f"{IV}/retry", json=consent(simulate="failed"))
    c.get(f"{IV}/status")
    capped = c.post(f"{IV}/retry", json=consent(simulate="verified"))
    assert capped.status_code == 429 and capped.json()["code"] == "max_attempts"
    assert [a["attemptCount"] for a in c.get(f"{IV}/history").json()] == [3, 2, 1]


def test_duplicate_starts_are_refused_while_a_session_is_open(simulated):
    c = farmer()
    assert c.post(f"{IV}/start", json=consent(simulate="processing")).status_code == 201
    dup = c.post(f"{IV}/start", json=consent(simulate="verified"))
    assert dup.status_code == 409 and dup.json()["code"] == "in_progress"
    assert c.post(f"{IV}/retry", json=consent()).json()["code"] == "in_progress"


def test_an_open_session_expires_and_can_then_be_retried(simulated):
    c = farmer()
    c.post(f"{IV}/start", json=consent(simulate="processing"))
    with SessionLocal() as db:
        db.execute(update(IdentityVerification).values(expires_at=now_utc() - timedelta(minutes=1)))
        db.commit()
    s = c.get(f"{IV}/status").json()
    assert s["status"] == "expired" and s["attempt"]["failureCode"] == "session_expired" and s["canRetry"]
    assert c.post(f"{IV}/retry", json=consent(simulate="verified")).status_code == 201


def test_only_the_server_decides_a_browser_cannot_send_verified(simulated):
    c = farmer()
    # Extra fields claiming a result are ignored; only the provider's answer counts.
    r = c.post(f"{IV}/start", json={**consent(simulate="failed"), "status": "verified", "isVerified": True})
    assert r.status_code == 201
    assert c.get(f"{IV}/status").json()["level"] == "none"


def test_access_needs_a_session_and_farmers_never_see_each_others_attempts(simulated):
    assert new_client().get(f"{IV}/status").status_code == 401
    assert new_client().post(f"{IV}/start", json=consent()).status_code == 401
    ama, kofi = farmer("ama@example.com"), farmer("kofi@example.com")
    ama.post(f"{IV}/start", json=consent(simulate="verified"))
    assert kofi.get(f"{IV}/history").json() == []
    assert kofi.get(f"{IV}/status").json()["level"] == "none"


def test_start_and_retry_are_rate_limited(simulated, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limits_enabled", True)
    c = farmer()
    codes = [c.post(f"{IV}/start", json={**consent(), "consent": False}).status_code for _ in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429


# ---------- Provider failures ----------


class FlakyProvider(SignedProvider):
    def __init__(self, failures, error=TransientProviderError):
        self.failures, self.error, self.calls = failures, error, 0

    def start(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error("down") if self.error is ProviderUnavailable else self.error()
        return super().start(**kwargs)


def test_timeouts_are_retried_a_bounded_number_of_times_with_backoff():
    c = farmer()
    waits = []
    with SessionLocal() as db:
        uid = db.scalars(select(Profile.user_id)).first()
        recovering = FlakyProvider(failures=2)
        GhanaCardVerificationService(db, recovering, sleep=waits.append).start(
            uid, consent=True, consent_version=get_settings().identity_consent_version
        )
        assert recovering.calls == 3 and waits == [0.5, 1.0]
    assert c.get(f"{API}/me/profile").status_code == 200


def test_a_provider_that_is_down_leaves_nothing_behind(provider):
    c = farmer()
    down = FlakyProvider(failures=99)
    app.dependency_overrides[get_identity_provider] = lambda: down
    r = c.post(f"{IV}/start", json=consent())
    assert r.status_code == 503 and r.json()["code"] == "provider_unavailable"
    assert down.calls == 3  # bounded
    with SessionLocal() as db:  # rolled back: no half-started attempt
        assert db.scalar(select(func.count()).select_from(IdentityVerification)) == 0


# ---------- Callbacks ----------


def test_callbacks_must_be_authentic_and_are_applied_once(provider):
    c = farmer()
    started = c.post(f"{IV}/start", json=consent()).json()
    assert started["redirectUrl"] == "https://provider.example/session"
    with SessionLocal() as db:
        reference = db.scalars(select(IdentityVerification.verification_reference)).one()
    payload = {"reference": reference, "status": "verified", "eventId": "evt-1"}

    body, headers = signed(provider, payload, secret=b"wrong-secret")
    assert new_client().post(f"{IV}/provider-callback", content=body, headers=headers).status_code == 401
    body, headers = signed(provider, payload, stamp=int(time.time()) - 3600)
    assert new_client().post(f"{IV}/provider-callback", content=body, headers=headers).status_code == 401
    assert c.get(f"{IV}/status").json()["status"] == "processing"

    body, headers = signed(provider, payload)
    first = new_client().post(f"{IV}/provider-callback", content=body, headers=headers)
    assert first.json() == {"result": "applied"}
    # The same event replayed, or a later contradicting one, changes nothing.
    assert new_client().post(f"{IV}/provider-callback", content=body, headers=headers).json() == {"result": "ignored"}
    body, headers = signed(provider, {**payload, "status": "failed", "eventId": "evt-2"})
    assert new_client().post(f"{IV}/provider-callback", content=body, headers=headers).json() == {"result": "ignored"}
    assert c.get(f"{IV}/status").json()["level"] == "nia_verified"

    body, headers = signed(provider, {**payload, "reference": "REF-unknown", "eventId": "evt-3"})
    assert new_client().post(f"{IV}/provider-callback", content=body, headers=headers).status_code == 404


def test_unknown_failure_reasons_are_never_passed_through(provider):
    c = farmer()
    c.post(f"{IV}/start", json=consent())
    with SessionLocal() as db:
        reference = db.scalars(select(IdentityVerification.verification_reference)).one()
    body, headers = signed(provider, {"reference": reference, "status": "failed", "eventId": "e",
                                      "failureCode": "DB timeout at nia-core-7 for 192.0.2.1"})
    new_client().post(f"{IV}/provider-callback", content=body, headers=headers)
    assert c.get(f"{IV}/status").json()["attempt"]["failureCode"] == "provider_error"


# ---------- Admin review: never a way to verify ----------


def test_admins_can_close_a_review_but_never_verify(simulated):
    c = farmer()
    c.post(f"{IV}/start", json=consent(simulate="requires_review"))
    assert c.get(f"{IV}/status").json()["status"] == "requires_review"
    boss = super_admin()
    queue = boss.get(f"{API}/admin/identity-verifications").json()
    assert len(queue) == 1 and "verificationReference" not in queue[0]
    attempt = queue[0]["id"]
    # No route makes an identity verified.
    for path in (f"{API}/admin/identity-verifications/{attempt}/verify", f"{API}/admin/identity-verifications/{attempt}/approve"):
        assert boss.post(path, json={}).status_code in (404, 405)
    assert boss.post(f"{API}/admin/identity-verifications/{attempt}/close", json={"note": ""}).status_code == 400
    closed = boss.post(f"{API}/admin/identity-verifications/{attempt}/close", json={"note": "Photo unclear"})
    assert closed.status_code == 200 and closed.json()["failureCode"] == "closed_by_review"
    assert c.get(f"{IV}/status").json()["level"] == "none"
    assert boss.post(f"{API}/admin/identity-verifications/{attempt}/close", json={"note": "again"}).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuditEntry).where(AuditEntry.action == "identity.review_closed")) == 1


# ---------- Features that need identity ----------


def test_loans_need_the_configured_identity_level():
    c = farmer()
    application = {"partnerId": "no-such-lender", "amount": 1000, "purpose": "Seeds", "months": 6}
    r = c.post(f"{API}/me/loans/applications", json=application)
    assert r.status_code == 403 and r.json()["code"] == "identity_required" and r.json()["required"] == "reviewed"
    assert r.json()["detail"] == (
        "Add your Ghana Card and wait for AgriCtec to review it before you apply for a loan through AgriCtec."
    )
    # An admin-reviewed card meets the default ("reviewed") requirement: the lender check comes next.
    with SessionLocal() as db:
        db.execute(update(Profile).values(ghana_card_status="verified"))
        db.commit()
        assert identity_level(db, user_id(c)) == "reviewed"
    assert c.post(f"{API}/me/loans/applications", json=application).json()["code"] == "lender_unavailable"
    # Once loans need NIA verification, a reviewed card is no longer enough.
    boss = super_admin()
    assert boss.put(f"{API}/admin/identity-requirements", json={"requirements": {"loan_application": "magic"}}).status_code == 422
    saved = boss.put(f"{API}/admin/identity-requirements", json={"requirements": {"loan_application": "nia_verified"}})
    assert saved.json()["requirements"] == {"loan_application": "nia_verified"}
    r = c.post(f"{API}/me/loans/applications", json=application)
    assert r.status_code == 403 and r.json()["required"] == "nia_verified"
    assert c.get(f"{IV}/status").json()["requirements"][0] == {
        "feature": "loan_application", "label": "Apply for a loan through AgriCtec", "required": "nia_verified", "met": False,
    }
    assert c.post(f"{API}/me/loans/eligibility", json={"amount": 1000}).json()["eligible"] is False


# ---------- The development stand-in never reaches production ----------


def test_the_simulated_provider_is_refused_in_production(monkeypatch):
    with pytest.raises(ValueError, match="development only"):
        Settings(environment="production", identity_provider="simulated", secret_key="s" * 40, cookie_secure=True,
                 admin_email="owner@agrictec.app", admin_password=ADMIN_PASSWORD)
    settings = get_settings()
    monkeypatch.setattr(settings, "identity_provider", "simulated")
    c = farmer()
    c.post(f"{IV}/start", json=consent(simulate="verified"))
    assert c.get(f"{IV}/status").json()["level"] == "nia_verified"  # development only
    monkeypatch.setattr(settings, "environment", "production")
    assert get_identity_provider() is None
    s = c.get(f"{IV}/status").json()
    # The simulated result is invisible and worthless on the live platform.
    assert s["level"] == "none" and s["status"] == "not_started" and c.get(f"{IV}/history").json() == []

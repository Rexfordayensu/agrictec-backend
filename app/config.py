"""Settings, read from the environment (and backend/.env)."""

import ipaddress
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.passwords import password_problem


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg2://agrictec:agrictec@localhost:5432/agrictec"
    api_prefix: str = "/api/v1"
    # Signs the guest device cookie. Required (32+ characters) in production.
    secret_key: str = "dev-only-insecure-secret-change-me"

    # The Next.js app's origin(s). Cookies need credentials, so "*" is never allowed.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    # True behind HTTPS (production): session cookies are then sent over HTTPS only.
    cookie_secure: bool = False
    cookie_domain: str | None = None

    # Proxies whose X-Forwarded-For is believed: addresses or networks, e.g. 172.16.0.0/12 for
    # containers. The Next.js app forwards from the same host by default.
    trusted_proxies: Annotated[list[str], NoDecode] = ["127.0.0.1", "::1"]
    rate_limits_enabled: bool = True

    farmer_session_days: int = 30
    # Admin spec: 30 minutes of inactivity signs an admin out; 12 hours at most per sign-in.
    admin_idle_minutes: int = 30
    admin_session_hours: int = 12

    # The owner's super admin sign-in. Without both, the demo super admin is used
    # (superadmin@agrictec.app / Admin@2026, which must be changed at first sign-in).
    admin_email: str | None = None
    admin_password: str | None = None

    # Seeds the demo world (~60 farmers, loans, verifications, risk flags) at startup if the
    # database has never been seeded. Never turn this on in production.
    seed_demo_data: bool = False

    # The app's public address, for links back to it (payment returns, emails).
    public_app_url: str = "http://localhost:3000"

    # ---- Optional outside services: each is off until its keys are set ----
    # Paystack (https://paystack.com): MTN MoMo, Telecel Cash, AirtelTigo Money and cards in GHS.
    # sk_test_... for test mode, sk_live_... for real money. Without it, plans use demo payments
    # (refused in production).
    paystack_secret_key: str | None = None
    # Arkesel (https://arkesel.com) SMS: the API key and an approved sender ID (max 11 chars).
    arkesel_api_key: str | None = None
    sms_sender_id: str = "AgriCtec"
    # Email over SMTP (any provider).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    # Smile ID (https://usesmileid.com) Ghana Card checks against the NIA register: advice for
    # the admin reviewing a card, never an automatic approval.
    smile_id_partner_id: str | None = None
    smile_id_api_key: str | None = None
    smile_id_sandbox: bool = True

    @property
    def payments_live(self) -> bool:
        return bool(self.paystack_secret_key)

    @property
    def sms_enabled(self) -> bool:
        return bool(self.arkesel_api_key)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def identity_checks_enabled(self) -> bool:
        return bool(self.smile_id_partner_id and self.smile_id_api_key)

    @field_validator("cors_origins", "trusted_proxies", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "admin_email",
        "admin_password",
        "paystack_secret_key",
        "arkesel_api_key",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        "smile_id_partner_id",
        "smile_id_api_key",
        mode="before",
    )
    @classmethod
    def blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def check(self) -> "Settings":
        if "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS cannot be '*': list the frontend origins.")
        for entry in self.trusted_proxies:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise ValueError(
                    f"TRUSTED_PROXIES: {entry!r} is not an IP address or network (e.g. 172.16.0.0/12)."
                ) from None
        if bool(self.admin_email) != bool(self.admin_password):
            raise ValueError("Set both ADMIN_EMAIL and ADMIN_PASSWORD (or neither).")
        if self.admin_password:
            problem = password_problem(self.admin_password)
            if problem:
                raise ValueError(f"ADMIN_PASSWORD: {problem}")
        if self.environment == "production":
            if self.seed_demo_data:
                raise ValueError("SEED_DEMO_DATA must be off in production.")
            if len(self.secret_key) < 32 or self.secret_key.startswith("dev-only"):
                raise ValueError("Set SECRET_KEY to a random value of 32+ characters in production.")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production (HTTPS only).")
            if not self.admin_email:
                # Otherwise the demo super admin, whose password is published, would be created.
                raise ValueError("Set ADMIN_EMAIL and ADMIN_PASSWORD in production.")
        return self

    @property
    def configured_admin_email(self) -> str | None:
        return self.admin_email.strip().lower() if self.admin_email else None


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Test setup: a dedicated PostgreSQL database, migrated once, emptied before every test.

Set TEST_DATABASE_URL to a database the tests may wipe (default: agrictec_test on localhost)."""

import os
from datetime import date

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://postgres@localhost:5432/agrictec_test"
)
os.environ["ENVIRONMENT"] = "test"
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["ADMIN_EMAIL"] = ""
os.environ["ADMIN_PASSWORD"] = ""

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.backup import bootstrap  # noqa: E402

API = "/api/v1"
ADMIN_PASSWORD = "Sup3r$ecret!"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def migrated():
    """A clean schema built by the real migration (so the migration itself is tested)."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    config = Config(os.path.join(HERE, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(HERE, "alembic"))
    command.upgrade(config, "head")
    yield


@pytest.fixture(autouse=True)
def clean_db():
    """Every table emptied (the audit trigger is lifted for this test-only wipe) and the
    startup defaults put back."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate"))
        conn.execute(text("ALTER TABLE transaction_verifications DISABLE TRIGGER transaction_history_no_truncate"))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE transaction_verifications ENABLE TRIGGER transaction_history_no_truncate"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))
    with SessionLocal() as db:
        # A live platform starts with no lenders: tests that need one add it through the
        # console's own steps (published_lender below).
        bootstrap(db, seed_demo_data=False)
        db.commit()
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def new_client() -> TestClient:
    """A browser of its own (its own cookies)."""
    return TestClient(app, base_url="http://testserver")


@pytest.fixture
def client():
    return new_client()


def receipt_png(seed: int = 0) -> bytes:
    """A tiny, real PNG (different per seed, so each is a separate file)."""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), (seed % 256, 120, 60)).save(out, format="PNG")
    return out.getvalue()


_receipts = iter(range(1, 10_000))


def add_momo(client: TestClient, body: dict, receipt: bytes | None = None):
    """POST /me/momo the way the app does: the details as JSON plus the MoMo receipt."""
    import json

    return client.post(
        f"{API}/me/momo",
        data={"data": json.dumps(body, default=str)},
        files={"receipt": ("receipt.png", receipt or receipt_png(next(_receipts)), "image/png")},
    )


def add_record(client: TestClient, body: dict):
    """POST /me/records; a money record without a payment method is recorded as paid in cash."""
    if body.get("type") != "activity" and "paymentMethod" not in body:
        body = {**body, "paymentMethod": "cash"}
    return client.post(f"{API}/me/records", json=body)


def signup(email: str = "ama@example.com", password: str = "password123") -> TestClient:
    c = new_client()
    r = c.post(f"{API}/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return c


def super_admin() -> TestClient:
    """The bootstrap super admin, signed in, its demo password changed."""
    c = new_client()
    r = c.post(f"{API}/admin/auth/login", json={"email": "superadmin@agrictec.app", "password": "Admin@2026"})
    assert r.status_code == 200, r.text
    r = c.post(f"{API}/admin/auth/change-password", json={"currentPassword": "Admin@2026", "newPassword": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return c


def admin_with_role(boss: TestClient, role: str, email: str | None = None) -> TestClient:
    """Invites a team member with `role` and signs them in (temporary password changed)."""
    email = email or f"{role}@agrictec.app"
    r = boss.post(f"{API}/admin/team", json={"name": f"Test {role}", "email": email, "role": role})
    assert r.status_code == 201, r.text
    temporary = r.json()["temporaryPassword"]
    c = new_client()
    assert c.post(f"{API}/admin/auth/login", json={"email": email, "password": temporary}).status_code == 200
    r = c.post(f"{API}/admin/auth/change-password", json={"currentPassword": temporary, "newPassword": "R0le$ecret!"})
    assert r.status_code == 200, r.text
    return c


TEST_LENDER = {
    "name": "Test Lender (automated tests)",
    "institutionType": "rural_community_bank",
    "regulator": "Test regulator",
    "licenceRef": "TEST-0001",
}


def published_lender(boss: TestClient, **fields) -> dict:
    """A test lender taken through every console step: add, licence check, listing, publish."""
    r = boss.post(f"{API}/admin/lenders", json={**TEST_LENDER, **fields})
    assert r.status_code == 201, r.text
    lender_id = r.json()["id"]
    r = boss.post(f"{API}/admin/lenders/{lender_id}/verification", json={
        "licenceStatus": "verified", "source": "Test register entry", "checkedOn": date.today().isoformat(),
    })
    assert r.status_code == 200, r.text
    r = boss.post(f"{API}/admin/lenders/{lender_id}/listing", json={"authorised": True, "note": "Test authorisation"})
    assert r.status_code == 200, r.text
    r = boss.post(f"{API}/admin/lenders/{lender_id}/publish")
    assert r.status_code == 200, r.text
    return r.json()


def recorded_application(user_id: str, partner_id: str, status: str = "submitted") -> str:
    """An application already on record (sent before lenders moved to loan products)."""
    from app.domain.clock import now_utc
    from app.models import LoanApplication, LoanStatusEvent

    with SessionLocal() as db:
        now = now_utc()
        row = LoanApplication(
            reference=f"APP-2026-{abs(hash((user_id, partner_id, status))) % 90000 + 10000}", user_id=user_id,
            partner_id=partner_id, amount=1000, months=12, purpose="Seeds & Fertilizer", rate=18, monthly=91.68,
            score_at_application=600, status=status, created_at=now,
        )
        row.history.append(LoanStatusEvent(position=0, status="submitted", by="Ama", at=now))
        if status != "submitted":
            row.history.append(LoanStatusEvent(position=1, status=status, by="Admin", at=now))
        db.add(row)
        db.commit()
        return row.id


def audit_count(db) -> int:
    return db.execute(text("select count(*) from audit_log")).scalar_one()


ONBOARDING = {
    "profile": {
        "name": "Ama Mensah",
        "phone": "0241234567",
        "farmName": "Mensah Maize Farm",
        "region": "Ashanti",
        "constituency": "Ejisu",
        "gps": {"lat": 6.7, "lng": -1.6},
        "acres": 5,
        "primaryCrops": ["Maize"],
        "farmType": "Smallholder",
    }
}

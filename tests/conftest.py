"""Test setup: a dedicated PostgreSQL database, migrated once, emptied before every test.

Set TEST_DATABASE_URL to a database the tests may wipe (default: agrictec_test on localhost)."""

import os

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
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))
    with SessionLocal() as db:
        bootstrap(db, seed_demo_data=False)
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

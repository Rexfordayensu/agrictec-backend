"""Demo data stays out of the live platform: no simulated payments, no demo world, and no
invented prices. The guest login is the only demo a farmer meets."""

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.domain.defaults import DEFAULT_CROP_CONTENT
from app.domain.farming import predict_yield
from app.domain.market import crop_price_per_tonne
from app.models import Admin, User
from app.schemas.farm import YieldInput
from app.schemas.platform import CropContent, MarketPrice
from tests.conftest import ADMIN_PASSWORD, API, ONBOARDING, new_client, signup, super_admin

CROPS = [CropContent.model_validate(c) for c in DEFAULT_CROP_CONTENT]
PLAN = YieldInput(crop="Maize", acres=2, season="major", fertilizer="both", irrigation="drip")


def observation(pid="maize-1", price=400.0, crop="Maize (white)", **extra) -> MarketPrice:
    return MarketPrice.model_validate({
        "id": pid, "country": "GH", "currency": "GHS", "crop": crop, "unit": "bag", "quantity": 1,
        "packageWeightKg": 100, "price": price, "change": 0, "status": "published", "scope": "region",
        "region": "Ashanti", "observedAt": "2026-10-01",
        "source": {"title": "Weekly prices", "publisher": "MoFA SRID", "url": "https://mofa.gov.gh/prices",
                   "publishedAt": "2026-10-01", "kind": "official"},
        "reviewedAt": "2026-10-02T10:00:00.000Z", "reviewedBy": "Super Admin", **extra,
    })


def test_crops_carry_no_built_in_price():
    assert all(c.price_per_tonne is None for c in CROPS)


def test_yield_revenue_comes_only_from_a_single_published_price():
    nothing = predict_yield(PLAN, CROPS, [])
    assert nothing["expected"] > 0
    assert nothing["revenue"] is None and nothing["price_per_tonne"] is None and nothing["price_market"] is None

    priced = predict_yield(PLAN, CROPS, [observation()])  # GHS 400 per 100 kg bag
    assert priced["price_per_tonne"] == 4000
    assert priced["revenue"] == 41_580  # 10.395 t (shown rounded as 10.4) x GHS 4,000
    assert priced["price_market"] == "Ashanti" and str(priced["price_observed_at"]) == "2026-10-01"

    # Several observations are not reduced to one price; a range, a draft or an unweighed unit gives none.
    assert crop_price_per_tonne("Maize", [observation(), observation("maize-2", 420)])[0] is None
    assert crop_price_per_tonne("Maize", [observation(priceMax=450)])[0] is None
    assert crop_price_per_tonne("Maize", [observation(status="draft")]) == (None, None)
    assert crop_price_per_tonne("Maize", [observation(packageWeightKg=None)])[0] is None
    assert crop_price_per_tonne("Maize", [observation(unit="tonne", packageWeightKg=None, price=3900)])[0] == 3900
    assert crop_price_per_tonne("Rice", [observation()]) == (None, None)


@pytest.fixture
def live(monkeypatch):
    """The live platform without a Paystack key."""
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "paystack_secret_key", None)
    return settings


def test_demo_payments_and_the_demo_world_exist_only_outside_production():
    boss = super_admin()
    assert new_client().get(f"{API}/platform").json()["payments"]["mode"] == "demo"
    assert boss.get(f"{API}/admin/system/status").json()["demoData"] is True


def test_the_live_platform_offers_no_simulated_payment(live):
    assert new_client().get(f"{API}/platform").json()["payments"]["mode"] == "off"
    farmer = signup()
    farmer.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    bought = farmer.post(f"{API}/me/subscription", json={"planId": "premium", "method": "card"})
    assert bought.status_code == 403
    assert farmer.get(f"{API}/me/subscription").json()["subscription"]["planId"] == "free"
    assert farmer.post(f"{API}/me/subscription", json={"planId": "free", "method": "card"}).status_code == 200


def test_the_live_console_hides_and_refuses_the_demo_world(live):
    boss = super_admin()
    assert boss.get(f"{API}/admin/system/status").json() == {"seededAt": None, "demoData": False}
    reset = boss.post(f"{API}/admin/system/reset-demo", json={"confirmation": "RESET", "password": ADMIN_PASSWORD})
    assert reset.status_code == 403


def test_a_restore_never_brings_the_demo_world_to_the_live_platform(monkeypatch):
    boss = super_admin()
    assert boss.post(f"{API}/admin/system/reset-demo", json={"confirmation": "RESET", "password": ADMIN_PASSWORD}).status_code == 200
    real = signup("real.farmer@example.com")
    real.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    secret = "correct horse battery staple"
    backup = boss.post(f"{API}/admin/system/backup", json={"passphrase": secret}).json()

    monkeypatch.setattr(get_settings(), "environment", "production")
    restored = boss.post(f"{API}/admin/system/restore", json={
        "backup": backup, "passphrase": secret, "confirmation": "RESTORE", "password": ADMIN_PASSWORD,
    })
    assert restored.status_code == 200, restored.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.id.startswith("seed-farmer-"))) == 0
        assert db.scalar(select(func.count()).select_from(User).where(User.email == "real.farmer@example.com")) == 1
        team = dict(db.execute(select(Admin.id, Admin.status).where(Admin.id.startswith("admin-"))).all())
    assert all(team[i] == "disabled" for i in ("admin-efua", "admin-kofi", "admin-ama", "admin-yaw"))
    assert team["admin-super"] == "active"
    assert boss.get(f"{API}/admin/system/status").json()["seededAt"] is None


def _migrate_0011_to_head(monkeypatch, environment: str) -> None:
    import os

    from alembic import command
    from alembic.config import Config

    from tests.conftest import HERE

    config = Config(os.path.join(HERE, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(HERE, "alembic"))
    command.downgrade(config, "0011")
    monkeypatch.setenv("ENVIRONMENT", environment)
    command.upgrade(config, "head")


def _demo_left() -> tuple[int, int, str]:
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as conn:
        farmers = conn.execute(text("select count(*) from users where id like 'seed-farmer-%'")).scalar()
        notices = conn.execute(text("select count(*) from announcements where id like 'seed-ann-%'")).scalar()
        efua = conn.execute(text("select status from admins where id = 'admin-efua'")).scalar()
    return farmers, notices, efua


def test_migration_removes_demo_data_only_on_the_live_platform(monkeypatch):
    boss = super_admin()
    assert boss.post(f"{API}/admin/system/reset-demo", json={"confirmation": "RESET", "password": ADMIN_PASSWORD}).status_code == 200
    real = signup("real.farmer@example.com")
    real.post(f"{API}/me/onboarding/complete", json=ONBOARDING)
    _migrate_0011_to_head(monkeypatch, "development")
    farmers, notices, efua = _demo_left()
    assert farmers == 60 and notices > 0 and efua == "active"  # development keeps its demo world

    _migrate_0011_to_head(monkeypatch, "production")
    assert _demo_left() == (0, 0, "disabled")
    assert real.get(f"{API}/me/profile").status_code == 200  # real accounts untouched
    assert boss.get(f"{API}/admin/farmers").json()[0]["email"] == "real.farmer@example.com"

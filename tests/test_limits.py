"""Limits: the request size ceiling, each farmer's evidence storage, and new entries a month."""

import asyncio
from datetime import date

import pytest

from app.config import get_settings
from app.core.body_limit import BodyLimit
from tests.conftest import API, add_momo, add_record, receipt_png, signup

SALE = {"type": "income", "description": "Sale", "crop": "Maize", "date": date.today().isoformat()}


@pytest.fixture
def settings():
    return get_settings()


def test_a_request_over_the_ceiling_is_refused_before_it_is_read():
    farmer = signup()
    big = farmer.post(f"{API}/me/records", content=b"x" * (16 * 1024 * 1024),
                      headers={"content-type": "application/json"})
    assert big.status_code == 413 and big.json()["code"] == "request_too_large"


def test_a_streamed_body_without_a_length_is_cut_off_too():
    reached = []

    async def app(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        reached.append(True)

    chunks = [{"type": "http.request", "body": b"x" * 600, "more_body": True} for _ in range(3)]
    sent = []

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    limited = BodyLimit(app, default=1000)
    asyncio.run(limited({"type": "http", "path": "/api/v1/me/records", "headers": []}, receive, send))
    assert not reached and sent[0]["status"] == 413


def test_the_admin_backup_restore_has_a_larger_ceiling():
    limited = BodyLimit(None, default=10, overrides={"/system/restore": 1000})
    assert limited._limit("/api/v1/admin/system/restore") == 1000
    assert limited._limit("/api/v1/me/records") == 10


def test_evidence_storage_is_capped_per_farmer(settings, monkeypatch):
    farmer = signup()
    first = add_momo(farmer, {"direction": "in", "counterparty": "Buyer", "amount": "10.00",
                              "date": date.today().isoformat(), "network": "MTN"}, receipt_png(301))
    assert first.status_code == 201
    monkeypatch.setattr(settings, "evidence_quota_bytes", 1)  # already over: the next file is refused
    full = add_momo(farmer, {"direction": "in", "counterparty": "Buyer", "amount": "11.00",
                             "date": date.today().isoformat(), "network": "MTN"}, receipt_png(302))
    assert full.status_code == 400 and full.json()["code"] == "storage_full"
    assert len(farmer.get(f"{API}/me/momo").json()) == 1


def test_new_entries_are_capped_each_month(settings, monkeypatch):
    farmer = signup()
    monkeypatch.setattr(settings, "monthly_entry_limit", 2)
    assert add_record(farmer, {**SALE, "amount": "1.00"}).status_code == 201
    assert add_momo(farmer, {"direction": "in", "counterparty": "Buyer", "amount": "2.00",
                             "date": date.today().isoformat(), "network": "MTN"}).status_code == 201
    third = add_record(farmer, {**SALE, "amount": "3.00"})
    assert third.status_code == 400 and third.json()["code"] == "entry_limit"

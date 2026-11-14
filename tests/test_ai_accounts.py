"""Claude in the assistant and crop doctor, farmers resetting their own password, and reports
emailed on a schedule. Claude, email and SMS are replaced with fakes: nothing leaves the machine."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import get_settings
from app.integrations import claude, messaging
from app.models import PasswordReset, ReportSchedule
from app.services import password_reset, report_schedules
from tests.conftest import API, ONBOARDING, admin_with_role, audit_count, new_client, signup, super_admin, add_record


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def ai_on(settings, monkeypatch):
    """Claude "configured", with every request answered by `answers` (a dict or an exception)
    and recorded in `calls`."""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    calls, answers = [], []

    def fake(**kwargs):
        calls.append(kwargs)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(claude, "_structured", fake)
    return calls, answers


def farmer_on_premium():
    c = signup()
    assert c.post(f"{API}/me/onboarding/complete", json=ONBOARDING).status_code == 200
    assert c.post(f"{API}/me/subscription", json={"planId": "premium", "method": "card"}).status_code == 200
    return c


def photo(width=3000, height=2000, fmt="JPEG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 120, 40)).save(out, format=fmt)
    return out.getvalue()


# ---------- The assistant ----------


def test_claude_answers_with_the_farmers_own_figures(ai_on):
    calls, answers = ai_on
    c = farmer_on_premium()
    add_record(c, {"type": "income", "description": "Maize sale", "crop": "Maize", "amount": 1200, "date": "2026-10-01"})
    # An earlier exchange, saved by the API itself (the browser can't write the conversation).
    answers.append({"title": "Planting maize", "intro": "At the start of the rains.", "bullets": [], "tip": ""})
    first = c.post(f"{API}/me/assistant", json={"message": "When should I plant maize?"})
    assert first.status_code == 200, first.text
    assert [m["role"] for m in first.json()["messages"]] == ["user", "ai"]
    answers.append({"title": "Raising your score", "intro": "Keep recording sales.", "bullets": ["Record every sale", " "], "tip": ""})
    r = c.post(f"{API}/me/assistant", json={"message": "How do I improve my credit score?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "ai" and body["creditsRemaining"] == 98
    reply = {k: v for k, v in body["reply"].items() if v is not None}
    assert reply == {"title": "Raising your score", "intro": "Keep recording sales.", "bullets": ["Record every sale"]}
    sent = calls[1]
    assert "AgriCtec score:" in sent["system"] and "GHS 1,200.00" in sent["system"]
    assert "prepared answer" in sent["system"]  # the knowledge base's reply guides Claude
    # The history: the earlier exchange, then the new question.
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert sent["messages"][1]["content"] == "Planting maize\nAt the start of the rains."
    assert sent["messages"][-1]["content"] == "How do I improve my credit score?"


def test_loan_answers_still_say_how_to_unlock_loans(ai_on):
    _, answers = ai_on
    c = farmer_on_premium()
    answers.append({"title": "Loans", "intro": "You can apply for a loan from our lenders.", "bullets": [], "tip": "Apply today."})
    reply = c.post(f"{API}/me/assistant", json={"message": "Can I get a loan?"}).json()["reply"]
    assert reply["tip"] != "Apply today." and "Ghana Card" in reply["tip"]


def test_without_claude_the_knowledge_base_answers(ai_on, settings, monkeypatch):
    calls, answers = ai_on
    c = farmer_on_premium()
    answers.append(claude.AIUnavailable("down"))
    r = c.post(f"{API}/me/assistant", json={"message": "How do I improve my credit score?"}).json()
    assert r["source"] == "rules" and r["reply"]["title"] == "Improve Your AgriCtec Score"
    assert r["creditsRemaining"] == 99  # a real answer still costs its credit
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    calls.clear()
    assert c.post(f"{API}/me/assistant", json={"message": "hello"}).json()["source"] == "rules"
    assert calls == []


def test_the_request_asks_for_json_and_opts_into_fallbacks(settings, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    sent = {}

    class Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            block = type("Block", (), {"type": "text", "text": '{"title": "Hi", "intro": "", "bullets": [], "tip": ""}'})
            return type("Message", (), {"stop_reason": "end_turn", "content": [block]})

    fake = type("Client", (), {"beta": type("Beta", (), {"messages": Messages()})(), "api_key": "sk-ant-test"})()
    monkeypatch.setattr(claude, "_client", fake)
    assert claude.assistant_reply(farmer_facts="- x", prepared=None, turns=[], message="hi") == {"title": "Hi"}
    assert sent["model"] == "claude-opus-5-5"
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["output_config"]["format"]["type"] == "json_schema" and sent["output_config"]["effort"] == "low"


# ---------- The crop doctor ----------


def test_claude_reads_the_photo(ai_on, db):
    calls, answers = ai_on
    c = farmer_on_premium()
    answers.append({
        "isPlant": True, "crop": "maize", "disease": "Maize Streak Virus", "severity": "High",
        "confidence": 87.6, "treatment": "Pull out infected plants.", "prevention": ["Plant resistant seed", ""],
    })
    r = c.post(f"{API}/me/crop-doctor", files={"image": ("leaf.jpg", photo(), "image/jpeg")})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result == {
        "disease": "Maize Streak Virus", "severity": "High", "confidence": 88, "treatment": "Pull out infected plants.",
        "prevention": ["Plant resistant seed"], "crop": "Maize", "source": "ai",
    }
    assert r.json()["creditsRemaining"] == 95
    image = calls[0]["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    # Saved by the API with where it came from and a thumbnail it made itself.
    saved = r.json()["diagnosis"]
    assert saved["source"] == "ai" and saved["imageThumb"].startswith("data:image/jpeg;base64,")
    history = c.get(f"{API}/me/diagnoses").json()
    assert [d["id"] for d in history] == [saved["id"]] and history[0]["disease"] == "Maize Streak Virus"


def test_photos_are_scaled_before_sending():
    sent = claude.prepare_photo(photo(4000, 3000, "PNG"), "image/png")
    import base64

    image = Image.open(io.BytesIO(base64.b64decode(sent.data)))
    assert sent.media_type == "image/jpeg" and max(image.size) == claude.MAX_EDGE
    with pytest.raises(ValueError):
        claude.prepare_photo(b"\xff\xd8not really", "image/jpeg")


def test_a_failed_reading_costs_nothing(ai_on):
    _, answers = ai_on
    c = farmer_on_premium()
    answers.append({"isPlant": False, "crop": "", "disease": "", "severity": "None", "confidence": 0, "treatment": "", "prevention": []})
    r = c.post(f"{API}/me/crop-doctor", files={"image": ("cat.jpg", photo(), "image/jpeg")})
    assert r.status_code == 400 and "doesn't show a plant" in r.json()["detail"]
    answers.append(claude.AIUnavailable("down"))
    r = c.post(f"{API}/me/crop-doctor", files={"image": ("leaf.jpg", photo(), "image/jpeg")})
    assert r.status_code == 503 and r.json()["code"] == "ai_unavailable"
    heic = c.post(f"{API}/me/crop-doctor", files={"image": ("leaf.heic", b"heic", "image/heic")})
    assert heic.status_code == 400 and "JPEG" in heic.json()["detail"]
    assert c.get(f"{API}/me/subscription").json()["creditsUsed"] == 0


def test_the_snapshot_says_whether_ai_is_on(settings, monkeypatch, client):
    assert client.get(f"{API}/platform").json()["ai"] == {"mode": "off"}
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    assert client.get(f"{API}/platform").json()["ai"] == {"mode": "claude"}


# ---------- Password reset ----------


@pytest.fixture
def codes(settings, monkeypatch):
    """Email "configured"; the codes that would have been sent, by address."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "AgriCtec <no-reply@example.com>")
    sent: dict[str, list[str]] = {}

    def fake_send(routes, email, phone, code):
        sent.setdefault(email, []).append(code)

    class Now:  # run the "background" send straight away
        def __init__(self, target, args, **_):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(password_reset, "_send", fake_send)
    monkeypatch.setattr(password_reset.threading, "Thread", Now)
    return sent


def test_a_farmer_resets_their_own_password(codes, client):
    old = signup("ama@example.com", "password123")
    unknown = client.post(f"{API}/auth/password-reset/request", json={"email": "nobody@example.com"})
    known = client.post(f"{API}/auth/password-reset/request", json={"email": "AMA@example.com "})
    assert unknown.status_code == known.status_code == 200 and unknown.json() == known.json()
    assert list(codes) == ["ama@example.com"]
    code = codes["ama@example.com"][0]
    assert len(code) == 6 and code.isdigit()

    wrong = "000000" if code != "000000" else "111111"
    r = client.post(f"{API}/auth/password-reset/confirm", json={"email": "ama@example.com", "code": wrong, "newPassword": "newpass123"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_code"
    r = client.post(f"{API}/auth/password-reset/confirm", json={"email": "ama@example.com", "code": code, "newPassword": "short"})
    assert r.status_code == 400 and "at least" in r.json()["detail"]
    r = client.post(f"{API}/auth/password-reset/confirm", json={"email": "ama@example.com", "code": code, "newPassword": "newpass123"})
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/auth/session").status_code == 200  # signed in here
    assert old.get(f"{API}/auth/session").status_code == 401  # every other session ended
    inbox = client.get(f"{API}/me/notifications").json()
    assert inbox["items"][0]["title"] == "Your password was changed"
    # The code is single-use, and the new password works.
    again = new_client().post(f"{API}/auth/password-reset/confirm", json={"email": "ama@example.com", "code": code, "newPassword": "another123"})
    assert again.status_code == 400
    assert new_client().post(f"{API}/auth/login", json={"email": "ama@example.com", "password": "newpass123"}).status_code == 200


def test_codes_resist_guessing(codes, client, db):
    signup("kofi@example.com")
    client.post(f"{API}/auth/password-reset/request", json={"email": "kofi@example.com"})
    code = codes["kofi@example.com"][0]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(password_reset.MAX_ATTEMPTS):
        client.post(f"{API}/auth/password-reset/confirm", json={"email": "kofi@example.com", "code": wrong, "newPassword": "newpass123"})
    r = client.post(f"{API}/auth/password-reset/confirm", json={"email": "kofi@example.com", "code": code, "newPassword": "newpass123"})
    assert r.status_code == 400  # five wrong guesses use the code up
    # Only a hash is stored, a quick second request sends nothing new, and codes expire.
    row = db.scalars(select(PasswordReset)).one()
    assert code not in row.code_hash
    client.post(f"{API}/auth/password-reset/request", json={"email": "kofi@example.com"})
    assert len(codes["kofi@example.com"]) == 1
    row.created_at = row.created_at - timedelta(minutes=20)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    row.attempts = 0
    db.commit()
    r = client.post(f"{API}/auth/password-reset/confirm", json={"email": "kofi@example.com", "code": code, "newPassword": "newpass123"})
    assert r.status_code == 400


def test_at_most_five_codes_a_day(codes, db):
    signup("efua@example.com")
    for _ in range(7):
        password_reset.request_reset(db, "efua@example.com")
        for row in db.scalars(select(PasswordReset)):  # step past the one-minute wait
            row.created_at = row.created_at - timedelta(minutes=2)
        db.commit()
    assert len(codes["efua@example.com"]) == password_reset.DAILY_CODES


# ---------- Scheduled reports ----------


def test_send_times():
    monday_9am = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert report_schedules.next_run("weekly", monday_9am) == datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    assert report_schedules.next_run("weekly", datetime(2026, 11, 2, 6, 0, tzinfo=UTC)) == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
    assert report_schedules.next_run("monthly", datetime(2026, 11, 1, 7, 0, tzinfo=UTC)) == datetime(2026, 12, 1, 7, 0, tzinfo=UTC)
    assert report_schedules.next_run("monthly", datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 1, 7, 0, tzinfo=UTC)


def test_admins_schedule_reports_and_they_go_out_once(settings, monkeypatch, db):
    boss = super_admin()
    before = audit_count(db)
    r = boss.post(f"{API}/admin/reports/schedules", json={"reportId": "loans", "frequency": "weekly", "recipients": ["Ops@Example.com", "ops@example.com", "cfo@example.com"]})
    assert r.status_code == 201, r.text
    schedule = r.json()
    assert schedule["recipients"] == ["ops@example.com", "cfo@example.com"] and schedule["reportTitle"] == "Loan Portfolio"
    assert audit_count(db) == before + 1
    bad = boss.post(f"{API}/admin/reports/schedules", json={"reportId": "loans", "frequency": "weekly", "recipients": ["not-an-email"]})
    assert bad.status_code == 400
    listing = boss.get(f"{API}/admin/reports/schedules").json()
    assert [s["id"] for s in listing["schedules"]] == [schedule["id"]] and listing["emailEnabled"] is False

    # Due now, with email set up: one email per recipient, with the CSV attached.
    sent = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "AgriCtec <no-reply@example.com>")
    monkeypatch.setattr(messaging, "send_email", lambda to, subject, text, attachments=None: sent.append((to, subject, attachments)))
    row = db.get(ReportSchedule, schedule["id"])
    row.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert report_schedules.send_due(db) == 1
    assert [s[0] for s in sent] == ["ops@example.com", "cfo@example.com"]
    filename, mime, content = sent[0][2][0]
    assert filename.endswith(".csv") and mime == "text/csv" and b"Loan Portfolio" in content
    db.refresh(row)
    assert row.last_status == "sent to 2" and row.next_run_at > datetime.now(UTC)
    assert report_schedules.send_due(db) == 0  # not due again until next Monday

    # Paused, then removed; an analyst may manage them too (reports.export).
    r = boss.patch(f"{API}/admin/reports/schedules/{schedule['id']}", json={"active": False, "frequency": "monthly"})
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["frequency"] == "monthly"
    analyst = admin_with_role(boss, "analyst")
    assert analyst.delete(f"{API}/admin/reports/schedules/{schedule['id']}").status_code == 204
    assert boss.get(f"{API}/admin/reports/schedules").json()["schedules"] == []


def test_without_email_a_due_report_is_recorded_as_failed(db):
    boss = super_admin()
    schedule = boss.post(f"{API}/admin/reports/schedules", json={"reportId": "platform", "frequency": "monthly", "recipients": ["ops@example.com"]}).json()
    row = db.get(ReportSchedule, schedule["id"])
    row.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert report_schedules.send_due(db) == 0
    db.refresh(row)
    assert row.last_status == "failed: email is not configured" and row.next_run_at > datetime.now(UTC)

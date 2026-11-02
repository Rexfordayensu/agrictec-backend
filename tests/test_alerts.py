"""Error alerts on admins' phones: web push encryption and signing, devices, and the alerts."""

import base64
import json

import http_ece
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app.config import get_settings
from app.integrations import push
from app.main import app
from app.services import alerts
from tests.conftest import API, admin_with_role, super_admin

FCM = "https://fcm.googleapis.com/fcm/send/device-1"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def browser():
    """A browser's push keys, as PushSubscription.toJSON() gives them."""
    key = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return key, auth, {"p256dh": b64(public), "auth": b64(auth)}


@pytest.fixture(autouse=True)
def fresh_alerts():
    alerts._sent.clear()
    alerts._known.clear()
    yield


@pytest.fixture
def pushed(monkeypatch):
    """Every push the server would send: (endpoint, message); answers with `pushed.status`."""

    class Pushed(list):
        status = 201

    sent = Pushed()

    def fake_send(endpoint, p256dh, auth, message, client=None):
        sent.append((endpoint, message))
        return sent.status

    monkeypatch.setattr(push, "send", fake_send)
    return sent


def test_only_the_device_can_read_the_message():
    key, auth, keys = browser()
    body = push.encrypt(b'{"title": "hello"}', keys["p256dh"], keys["auth"])
    assert http_ece.decrypt(body, private_key=key, auth_secret=auth, version="aes128gcm") == b'{"title": "hello"}'
    other, _, _ = browser()
    with pytest.raises(Exception):
        http_ece.decrypt(body, private_key=other, auth_secret=auth, version="aes128gcm")


def test_each_message_is_signed_with_the_servers_own_key(monkeypatch):
    sent = {}

    class Recorder:
        def post(self, url, content, headers):
            sent.update(url=url, headers=headers)
            return type("Response", (), {"status_code": 201})()

        def close(self):
            pass

    _, _, keys = browser()
    assert push.send(FCM, keys["p256dh"], keys["auth"], {"title": "t"}, client=Recorder()) == 201
    scheme, _, params = sent["headers"]["Authorization"].partition(" ")
    fields = dict(part.split("=", 1) for part in params.split(","))
    assert scheme == "vapid" and fields["k"] == push.public_key()
    assert sent["headers"]["Content-Encoding"] == "aes128gcm"

    header, claims, signature = fields["t"].split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(claims))["aud"] == "https://fcm.googleapis.com"
    raw = pad(signature)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pad(push.public_key()))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if forged


def test_the_key_comes_from_the_secret_key(monkeypatch):
    settings = get_settings()
    first = push.public_key()
    assert push.public_key() == first and len(push.b64url_decode(first)) == 65
    monkeypatch.setattr(settings, "secret_key", "another-secret-key-of-at-least-32-characters")
    assert push.public_key() != first


def test_super_admins_turn_alerts_on_per_device(pushed):
    boss = super_admin()
    _, _, keys = browser()
    r = boss.get(f"{API}/admin/alerts/push").json()
    assert r == {"publicKey": push.public_key(), "devices": 0}
    r = boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": FCM, "keys": keys})
    assert r.status_code == 200 and r.json()["devices"] == 1
    # The same device again is still one device.
    assert boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": FCM, "keys": keys}).json()["devices"] == 1

    assert boss.post(f"{API}/admin/alerts/push/test").json() == {"sent": 1, "failed": 0}
    assert pushed[-1][0] == FCM and pushed[-1][1]["title"] == "AgriCtec: test alert"

    r = boss.post(f"{API}/admin/alerts/push/unsubscribe", json={"endpoint": FCM})
    assert r.json()["devices"] == 0
    assert boss.post(f"{API}/admin/alerts/push/test").status_code == 400


def test_alerts_go_only_to_browsers_push_services_and_need_settings_rights():
    boss = super_admin()
    _, _, keys = browser()
    for endpoint in ("https://attacker.example/push", "http://fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.io/x"):
        assert boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": endpoint, "keys": keys}).status_code == 400
    bad = {"p256dh": keys["p256dh"][:20], "auth": keys["auth"]}
    assert boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": FCM, "keys": bad}).status_code == 400
    assert boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": "https://web.push.apple.com/abc", "keys": keys}).status_code == 200
    support = admin_with_role(boss, "support")
    assert support.get(f"{API}/admin/alerts/push").status_code == 403


def test_a_crash_alerts_admins_once_per_quarter_hour_without_farmer_data(pushed, monkeypatch):
    boss = super_admin()
    _, _, keys = browser()
    boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": FCM, "keys": keys})

    from app.routers import me

    def broken(*args, **kwargs):
        raise KeyError("ama@example.com")

    monkeypatch.setattr(me.farm, "list_records", broken)
    farmer = TestClient(app, base_url="http://testserver", raise_server_exceptions=False)
    assert farmer.post(f"{API}/auth/signup", json={"email": "ama@example.com", "password": "password123"}).status_code == 201

    r = farmer.get(f"{API}/me/records")
    assert r.status_code == 500 and r.json()["code"] == "server_error"
    assert len(pushed) == 1
    message = pushed[0][1]
    assert "GET /me/records failed: KeyError" in message["body"]
    assert "ama@example.com" not in json.dumps(message)

    farmer.get(f"{API}/me/records")
    farmer.get(f"{API}/me/records")
    assert len(pushed) == 1  # held back, counted into the next alert
    kind = "GET /me/records failed: KeyError"
    last, held = alerts._sent[kind]
    alerts._sent[kind] = (last - alerts.REPEAT_EVERY, held)
    farmer.get(f"{API}/me/records")
    assert len(pushed) == 2 and "2 more times" in pushed[1][1]["body"]


def test_ended_subscriptions_are_forgotten_and_the_last_devices_are_remembered(pushed, db):
    boss = super_admin()
    _, _, keys = browser()
    boss.post(f"{API}/admin/alerts/push/subscribe", json={"endpoint": FCM, "keys": keys})
    assert [d.endpoint for d in alerts._known] == [FCM]

    pushed.status = 410
    alerts.report("Something failed: ValueError")
    assert boss.get(f"{API}/admin/alerts/push").json()["devices"] == 0
    assert alerts._known == []

"""On the live site the API's documentation pages and description are off: they would list
every console endpoint to anyone."""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def test_docs_are_off_in_production(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "production")
    app = create_app()
    client = TestClient(app)
    prefix = get_settings().api_prefix
    for path in ("/docs", "/redoc", f"{prefix}/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_docs_stay_on_while_developing(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "development")
    client = TestClient(create_app())
    assert client.get(f"{get_settings().api_prefix}/openapi.json").status_code == 200

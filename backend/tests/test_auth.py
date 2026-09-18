# backend/tests/test_auth.py
import os
import pytest
from fastapi.testclient import TestClient

os.environ["SITE_PASSWORD"] = "testpass123"
os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))

from app.main import app


@pytest.fixture(autouse=True)
def _ensure_lifespan(tmp_path, monkeypatch):
    """Point DATA_DIR / DB_PATH at a temp dir so each test gets a fresh DB."""
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main_mod, "DB_PATH", str(tmp_path / "linkage.db"))
    (tmp_path / "uploads").mkdir(exist_ok=True)
    (tmp_path / "runs").mkdir(exist_ok=True)
    from app.db import init_db
    init_db(str(tmp_path / "linkage.db"))


def test_login_wrong_password():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401


def test_login_correct_password():
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": "testpass123"})
        assert r.status_code == 200
        assert "session" in r.cookies


def test_me_without_session():
    with TestClient(app) as c:
        r = c.get("/api/auth/me")
        assert r.status_code == 401


def test_set_user_and_me():
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"password": "testpass123"})
        c.post("/api/auth/set-user", json={"name": "Tom Wright", "initials": "TW"})
        r = c.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["name"] == "Tom Wright"


def test_set_user_without_session_returns_401():
    with TestClient(app) as c:
        r = c.post("/api/auth/set-user", json={"name": "Tom Wright", "initials": "TW"})
        assert r.status_code == 401


def test_users_list():
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"password": "testpass123"})
        c.post("/api/auth/set-user", json={"name": "Alice Test", "initials": "AT"})
        r = c.get("/api/auth/users")
        assert r.status_code == 200
        names = [u["name"] for u in r.json()]
        assert "Alice Test" in names


def test_health_does_not_require_session():
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.status_code == 200


def test_protected_api_without_session():
    """Any /api/* route besides /api/auth/login and /api/health should 401."""
    with TestClient(app) as c:
        r = c.get("/api/auth/users")
        assert r.status_code == 401


def test_set_user_upserts():
    """Setting the same user twice should update, not duplicate."""
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"password": "testpass123"})
        c.post("/api/auth/set-user", json={"name": "Bob Builder", "initials": "BB"})
        c.post("/api/auth/set-user", json={"name": "Bob Builder", "initials": "B2"})
        r = c.get("/api/auth/users")
        bobs = [u for u in r.json() if u["name"] == "Bob Builder"]
        assert len(bobs) == 1
        assert bobs[0]["initials"] == "B2"

"""Base path support: the prefix strip, and what goes into index.html.

In production Caddy serves this app under /donations or /psc. The app must work
whether or not the proxy strips that prefix before forwarding.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.main as _main_mod
from app.base_path import base_path, render_index


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    """A stand-in for a production frontend build."""
    build = tmp_path / "static"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text(
        "<!doctype html><html><head><title>placeholder</title>"
        '<script type="module" src="./assets/index.js"></script>'
        "</head><body><div id=root></div></body></html>",
        encoding="utf-8",
    )
    (build / "assets" / "index.js").write_text("console.log('hi')", encoding="utf-8")
    monkeypatch.setattr(_main_mod, "STATIC_DIR", build)
    return build


@pytest.fixture
def client(static_dir, db_path, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    (data_dir / "runs").mkdir(parents=True)
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)

    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app)


# ---------------------------------------------------------------------------
# Normalising BASE_PATH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("donations", "/donations"),
    ("/donations", "/donations"),
    ("/donations/", "/donations"),
    ("donations/", "/donations"),
    ("/", ""),
    ("", ""),
    ("  ", ""),
])
def test_base_path_is_normalised(monkeypatch, raw, expected):
    monkeypatch.setenv("BASE_PATH", raw)
    assert base_path() == expected


def test_base_path_is_empty_when_unset(monkeypatch):
    monkeypatch.delenv("BASE_PATH", raising=False)
    assert base_path() == ""


# ---------------------------------------------------------------------------
# The middleware
# ---------------------------------------------------------------------------


def test_prefixed_and_bare_paths_both_reach_the_app(client, monkeypatch):
    monkeypatch.setenv("BASE_PATH", "/donations")
    assert client.get("/donations/api/health").json() == {"status": "ok"}
    assert client.get("/api/health").json() == {"status": "ok"}


def test_the_strip_happens_before_the_auth_check(client, monkeypatch):
    """A prefixed public path must still read as public, not as a protected one."""
    monkeypatch.setenv("BASE_PATH", "/donations")
    assert client.get("/donations/api/profile").status_code == 200
    # ...and a prefixed protected path is still protected.
    assert client.get("/donations/api/runs").status_code == 401


def test_a_similar_prefix_is_not_stripped(client, monkeypatch):
    """/donationsXYZ is a different path, not /donations plus 'XYZ'."""
    monkeypatch.setenv("BASE_PATH", "/donations")
    r = client.get("/donationsXYZ/api/health")
    assert "status" not in r.text  # falls through to the SPA, not to the API route


def test_no_prefix_configured_leaves_paths_alone(client, monkeypatch):
    monkeypatch.delenv("BASE_PATH", raising=False)
    assert client.get("/api/health").json() == {"status": "ok"}
    assert "status" not in client.get("/donations/api/health").text


def test_assets_are_served_under_the_prefix(client, monkeypatch):
    monkeypatch.setenv("BASE_PATH", "/donations")
    r = client.get("/donations/assets/index.js")
    assert r.status_code == 200
    assert "console.log" in r.text


# ---------------------------------------------------------------------------
# index.html injection
# ---------------------------------------------------------------------------


def test_index_injects_the_prefix_and_the_title(client, monkeypatch):
    monkeypatch.setenv("BASE_PATH", "/donations")
    body = client.get("/donations/runs").text
    assert '<base href="/donations/">' in body
    assert '<script>window.__BASE__="/donations";</script>' in body
    assert "<title>Donations reconciliation</title>" in body
    assert "placeholder" not in body


def test_index_with_no_prefix_injects_a_root_base(client, monkeypatch):
    monkeypatch.delenv("BASE_PATH", raising=False)
    body = client.get("/runs").text
    assert '<base href="/">' in body
    assert '<script>window.__BASE__="";</script>' in body


def test_index_is_not_cached_across_env_changes(client, monkeypatch):
    monkeypatch.setenv("BASE_PATH", "/donations")
    assert '<base href="/donations/">' in client.get("/").text
    monkeypatch.setenv("BASE_PATH", "/psc")
    assert '<base href="/psc/">' in client.get("/").text


def test_unknown_api_route_stays_a_404(client, monkeypatch):
    """An unknown /api/ path must not be answered with the SPA shell."""
    import app.auth as _auth_mod

    monkeypatch.setenv("BASE_PATH", "/donations")
    monkeypatch.setattr(_auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True})
    client.cookies.set("session", "fake")
    r = client.get("/donations/api/nope")
    assert r.status_code == 404
    assert r.json()["detail"] == "API route not found"


def test_render_index_without_a_head_tag(monkeypatch):
    """A hand-written index with no <head> still gets the base and the title."""
    monkeypatch.setenv("BASE_PATH", "/donations")
    out = render_index("<html><body>hi</body></html>", "Donations reconciliation")
    assert '<base href="/donations/">' in out
    assert "<title>Donations reconciliation</title>" in out


def test_render_index_escapes_the_title(monkeypatch):
    monkeypatch.delenv("BASE_PATH", raising=False)
    out = render_index("<html><head><title>x</title></head></html>", "A & B")
    assert "<title>A &amp; B</title>" in out

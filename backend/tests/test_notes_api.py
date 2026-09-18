# backend/tests/test_notes_api.py
"""Shared methodology-notes endpoint + HTML sanitizer."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.services.html_sanitizer import sanitize_html


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True})
    from fastapi.testclient import TestClient
    return TestClient(_main_mod.app, cookies={"session": "fake"})


# --- Sanitizer -------------------------------------------------------------

class TestSanitizer:
    def test_keeps_formatting_and_tables(self):
        html = "<h2>Title</h2><p><strong>bold</strong> and <em>italic</em></p>" \
               "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td colspan=\"2\">x</td></tr></tbody></table>"
        out = sanitize_html(html)
        assert "<h2>Title</h2>" in out
        assert "<strong>bold</strong>" in out
        assert "<table>" in out and "<th>A</th>" in out
        assert 'colspan="2"' in out

    def test_strips_script_and_handlers(self):
        html = '<p onclick="steal()">hi</p><script>alert(1)</script>'
        out = sanitize_html(html)
        assert "script" not in out.lower()
        assert "onclick" not in out.lower()
        assert "alert(1)" not in out
        assert "<p>hi</p>" in out

    def test_blocks_javascript_urls_but_keeps_http(self):
        assert "href" not in sanitize_html('<a href="javascript:alert(1)">x</a>')
        assert 'href="https://example.com"' in sanitize_html('<a href="https://example.com">x</a>')

    def test_drops_inline_styles_and_unknown_tags(self):
        out = sanitize_html('<div style="position:fixed">a</div><marquee>b</marquee>')
        assert "style" not in out
        assert "marquee" not in out.lower()
        assert "a" in out and "b" in out


# --- Endpoints -------------------------------------------------------------

class TestMethodologyNotes:
    def test_get_empty_by_default(self, client):
        r = client.get("/api/notes/methodology")
        assert r.status_code == 200
        assert r.json() == {"content": "", "updated_at": None, "updated_by": None}

    def test_put_sanitizes_and_persists(self, client):
        r = client.put("/api/notes/methodology",
                       json={"content": "<p>Note</p><script>alert(1)</script>"})
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == "<p>Note</p>"
        assert "script" not in body["content"].lower()
        assert body["updated_by"]  # attribution set (falls back to "user")
        assert body["updated_at"]

        # Persisted for the next reader.
        again = client.get("/api/notes/methodology").json()
        assert again["content"] == "<p>Note</p>"

    def test_put_overwrites(self, client):
        client.put("/api/notes/methodology", json={"content": "<p>v1</p>"})
        client.put("/api/notes/methodology", json={"content": "<p>v2</p>"})
        assert client.get("/api/notes/methodology").json()["content"] == "<p>v2</p>"


class TestLogout:
    def test_logout_clears_cookies(self, client):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # A delete-cookie header is emitted for both session and user.
        set_cookie = r.headers.get("set-cookie", "")
        assert "session=" in set_cookie

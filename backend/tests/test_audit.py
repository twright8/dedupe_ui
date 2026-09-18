# backend/tests/test_audit.py
"""Tests for audit_logger service and /api/audit endpoint."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must be set before app.main / app.auth are imported
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.services.audit_logger import log_event
from app.db import query_db


# ---------------------------------------------------------------------------
# audit_logger service tests
# ---------------------------------------------------------------------------

def test_log_event(db_path):
    log_event(db_path, user="Tom Wright", kind="run", description="Run started")
    rows = query_db(db_path, "SELECT * FROM audit_log")
    assert len(rows) == 1
    assert rows[0]["kind"] == "run"
    assert rows[0]["user_name"] == "Tom Wright"
    assert rows[0]["timestamp"] is not None


def test_log_event_with_metadata(db_path):
    log_event(
        db_path,
        user="Tom Wright",
        kind="label",
        description="Marked TRUE",
        metadata={"match_id": "m_001"},
    )
    rows = query_db(db_path, "SELECT * FROM audit_log")
    assert '"match_id"' in rows[0]["metadata_json"]


def test_log_event_no_metadata(db_path):
    log_event(db_path, user="Tom", kind="config", description="Saved v2")
    rows = query_db(db_path, "SELECT * FROM audit_log")
    assert rows[0]["metadata_json"] is None


def test_log_event_description_stored(db_path):
    log_event(db_path, user="Alice", kind="export", description="Exported CSV")
    rows = query_db(db_path, "SELECT * FROM audit_log")
    assert rows[0]["description"] == "Exported CSV"


def test_log_event_multiple_rows(db_path):
    log_event(db_path, user="A", kind="run", description="first")
    log_event(db_path, user="B", kind="upload", description="second")
    rows = query_db(db_path, "SELECT * FROM audit_log ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["user_name"] == "A"
    assert rows[1]["user_name"] == "B"


# ---------------------------------------------------------------------------
# /api/audit endpoint tests
# ---------------------------------------------------------------------------

import app.main as _main_mod  # imported after SITE_PASSWORD is set
import app.auth as _auth_mod


@pytest.fixture
def client(db_path, monkeypatch):
    """TestClient with DB wired to the tmp db_path, session auth bypassed."""
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)

    # Bypass session middleware so tests don't need cookies
    monkeypatch.setattr(_auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True})

    from fastapi.testclient import TestClient
    return TestClient(_main_mod.app, cookies={"session": "fake"})


def test_api_audit_empty(client):
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1
    assert body["per_page"] == 50


def test_api_audit_returns_events(client, db_path):
    log_event(db_path, user="Tom", kind="run", description="Run A")
    log_event(db_path, user="Tom", kind="label", description="Label B")
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_api_audit_ordered_desc(client, db_path):
    log_event(db_path, user="Tom", kind="run", description="first")
    log_event(db_path, user="Tom", kind="run", description="second")
    r = client.get("/api/audit")
    items = r.json()["items"]
    # Most recent (higher id) should come first
    assert items[0]["description"] == "second"
    assert items[1]["description"] == "first"


def test_api_audit_filter_kind(client, db_path):
    log_event(db_path, user="Tom", kind="run", description="Run A")
    log_event(db_path, user="Tom", kind="label", description="Label B")
    r = client.get("/api/audit?kind=run")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["kind"] == "run"


def test_api_audit_filter_user(client, db_path):
    log_event(db_path, user="Tom", kind="run", description="Run A")
    log_event(db_path, user="Alice", kind="run", description="Run B")
    r = client.get("/api/audit?user=Alice")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["user_name"] == "Alice"


def test_api_audit_pagination(client, db_path):
    for i in range(5):
        log_event(db_path, user="Tom", kind="run", description=f"Run {i}")
    r = client.get("/api/audit?page=1&per_page=2")
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 1
    assert body["per_page"] == 2

    r2 = client.get("/api/audit?page=2&per_page=2")
    body2 = r2.json()
    assert len(body2["items"]) == 2
    assert body2["page"] == 2

    r3 = client.get("/api/audit?page=3&per_page=2")
    body3 = r3.json()
    assert len(body3["items"]) == 1

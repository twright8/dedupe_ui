"""GET /api/profile — what this instance is, read before the login screen."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.main as _main_mod


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    (data_dir / "runs").mkdir(parents=True)
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)

    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app)


def test_profile_needs_no_login(client):
    """Like /api/health — the page needs the title and base path before login."""
    assert client.get("/api/profile").status_code == 200


def test_profile_payload(client, monkeypatch):
    monkeypatch.delenv("BASE_PATH", raising=False)
    body = client.get("/api/profile").json()

    assert body["key"] == "donations"
    assert body["title"] == "Donations reconciliation"
    assert body["subtitle"]
    assert body["base_path"] == ""

    assert body["input"]["label"] == "Donations spreadsheet"
    assert body["input"]["extensions"] == [".xlsx", ".csv"]
    assert body["input"]["help"]

    assert [t["key"] for t in body["tracks"]] == ["person", "organisation"]
    assert all(t["label"] for t in body["tracks"])


def test_profile_reports_the_base_path(client, monkeypatch):
    monkeypatch.setenv("BASE_PATH", "donations/")
    assert client.get("/api/profile").json()["base_path"] == "/donations"


def test_display_columns_are_in_order_and_typed(client):
    columns = client.get("/api/profile").json()["display_columns"]

    assert [c["key"] for c in columns] == [
        "name", "donor_status", "postcode", "company_number", "parties",
        "first_year", "last_year", "n_donations", "total_value",
        "existing_entity_id", "all_names",
    ]
    by_key = {c["key"]: c for c in columns}
    assert by_key["parties"]["type"] == "list"
    assert by_key["all_names"]["type"] == "list"
    assert by_key["n_donations"]["type"] == "number"
    assert by_key["total_value"]["type"] == "money"
    assert by_key["first_year"]["type"] == "year"
    assert by_key["name"]["type"] == "text"
    assert all(c["label"] for c in columns)


def test_priority_columns(client):
    assert client.get("/api/profile").json()["priority_columns"] == ["total_value"]


def test_the_app_title_comes_from_the_profile():
    assert _main_mod.app.title == "Donations reconciliation"

# backend/tests/test_config_manager.py
"""Tests for config_manager service and /api/config endpoints."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must be set before app.main / app.auth are imported
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.services.config_manager import (
    save_version,
    get_version,
    get_current,
    list_versions,
    diff_versions,
    test_rules,
)
from app.db import query_db


# ---------------------------------------------------------------------------
# config_manager service tests
# ---------------------------------------------------------------------------


def test_save_and_get_version(db_path):
    v = save_version(
        db_path,
        created_by="Tom",
        note="initial",
        name_rules=[{"pattern": "LTD", "replace": "LTD"}],
        jurisdiction_map=[{"canonical": "JERSEY", "aliases": ["JE"]}],
        legal_tokens=["LTD", "LLC"],
        linkage_settings={"threshold_high": 0.70},
    )
    assert v == 1
    got = get_version(db_path, 1)
    assert json.loads(got["name_rules"])[0]["pattern"] == "LTD"


def test_get_current_returns_latest(db_path):
    save_version(
        db_path,
        created_by="Tom",
        note="v1",
        name_rules=[],
        jurisdiction_map=[],
        legal_tokens=[],
        linkage_settings={},
    )
    save_version(
        db_path,
        created_by="Tom",
        note="v2",
        name_rules=[{"pattern": "X"}],
        jurisdiction_map=[],
        legal_tokens=[],
        linkage_settings={},
    )
    cur = get_current(db_path)
    assert cur["version"] == 2


def test_get_current_empty(db_path):
    cur = get_current(db_path)
    assert cur is None


def test_list_versions(db_path):
    save_version(
        db_path,
        created_by="Tom",
        note="v1",
        name_rules=[],
        jurisdiction_map=[],
        legal_tokens=[],
        linkage_settings={},
    )
    save_version(
        db_path,
        created_by="Tom",
        note="v2",
        name_rules=[],
        jurisdiction_map=[],
        legal_tokens=[],
        linkage_settings={},
    )
    versions = list_versions(db_path)
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # DESC order


def test_diff_versions(db_path):
    save_version(
        db_path,
        created_by="Tom",
        note="v1",
        name_rules=[{"pattern": "A"}],
        jurisdiction_map=[],
        legal_tokens=["LTD"],
        linkage_settings={"t": 0.7},
    )
    save_version(
        db_path,
        created_by="Tom",
        note="v2",
        name_rules=[{"pattern": "A"}, {"pattern": "B"}],
        jurisdiction_map=[],
        legal_tokens=["LTD"],
        linkage_settings={"t": 0.8},
    )
    d = diff_versions(db_path, 1, 2)
    assert d["name_rules"]["changed"] is True
    assert d["linkage_settings"]["changed"] is True
    assert d["legal_tokens"]["changed"] is False


def test_test_rules_applies_rules():
    rules = [
        {"pattern": "-", "replace": " "},
        {"pattern": "LIMITED", "replace": "LTD"},
        {"pattern": "\\s+", "replace": " "},
    ]
    result = test_rules("SORA-OREWA LIMITED", rules=rules)
    assert result["final"] == "SORA OREWA LTD"
    assert len(result["steps"]) == 3
    assert result["steps"][0]["after"] == "SORA OREWA LIMITED"


def test_test_rules_uppercases_input():
    rules = [{"pattern": "LTD", "replace": "LTD"}]
    result = test_rules("acme ltd", rules=rules)
    assert result["steps"][0]["before"] == "ACME LTD"


def test_save_logs_audit_event(db_path):
    """Saving a config version should create an audit_log entry with kind='config'."""
    save_version(
        db_path,
        created_by="Tom",
        note="audit test",
        name_rules=[],
        jurisdiction_map=[],
        legal_tokens=[],
        linkage_settings={},
    )
    rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'config'")
    assert len(rows) == 1
    assert rows[0]["user_name"] == "Tom"


def test_test_rules_from_db(db_path):
    """test_rules with rules=None should load from current config version."""
    save_version(
        db_path,
        created_by="Tom",
        note="rules test",
        name_rules=[
            {"pattern": "-", "replace": " "},
            {"pattern": "LIMITED", "replace": "LTD"},
        ],
        jurisdiction_map=[],
        legal_tokens=[],
        linkage_settings={},
    )
    result = test_rules("ACME-LIMITED", db_path=db_path)
    assert result["final"] == "ACME LTD"
    assert len(result["steps"]) == 2


# ---------------------------------------------------------------------------
# /api/config endpoint tests
# ---------------------------------------------------------------------------

import app.main as _main_mod
import app.auth as _auth_mod


@pytest.fixture
def client(db_path, monkeypatch):
    """TestClient with DB wired to the tmp db_path, session auth bypassed."""
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )

    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake"})


def test_api_config_current_empty(client):
    r = client.get("/api/config/current")
    assert r.status_code == 200
    assert r.json() is None


def test_api_config_save_and_get_current(client, db_path):
    r = client.post(
        "/api/config",
        json={
            "name_rules": [{"pattern": "X", "replace": "Y"}],
            "jurisdiction_map": [],
            "legal_tokens": ["LTD"],
            "linkage_settings": {"threshold_high": 0.7},
            "note": "first save",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1

    r2 = client.get("/api/config/current")
    assert r2.status_code == 200
    cur = r2.json()
    # JSON fields should be parsed objects, not strings
    assert isinstance(cur["name_rules"], list)
    assert cur["name_rules"][0]["pattern"] == "X"
    assert isinstance(cur["linkage_settings"], dict)


def test_api_config_versions_list(client, db_path):
    client.post(
        "/api/config",
        json={
            "name_rules": [],
            "jurisdiction_map": [],
            "legal_tokens": [],
            "linkage_settings": {},
            "note": "v1",
        },
    )
    client.post(
        "/api/config",
        json={
            "name_rules": [],
            "jurisdiction_map": [],
            "legal_tokens": [],
            "linkage_settings": {},
            "note": "v2",
        },
    )
    r = client.get("/api/config/versions")
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) == 2
    assert versions[0]["version"] == 2


def test_api_config_get_specific_version(client, db_path):
    client.post(
        "/api/config",
        json={
            "name_rules": [{"pattern": "A"}],
            "jurisdiction_map": [],
            "legal_tokens": [],
            "linkage_settings": {},
            "note": "v1",
        },
    )
    r = client.get("/api/config/versions/1")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["name_rules"], list)
    assert body["name_rules"][0]["pattern"] == "A"


def test_api_config_diff(client, db_path):
    client.post(
        "/api/config",
        json={
            "name_rules": [{"pattern": "A"}],
            "jurisdiction_map": [],
            "legal_tokens": ["LTD"],
            "linkage_settings": {"t": 0.7},
            "note": "v1",
        },
    )
    client.post(
        "/api/config",
        json={
            "name_rules": [{"pattern": "A"}, {"pattern": "B"}],
            "jurisdiction_map": [],
            "legal_tokens": ["LTD"],
            "linkage_settings": {"t": 0.8},
            "note": "v2",
        },
    )
    r = client.get("/api/config/diff/1/2")
    assert r.status_code == 200
    d = r.json()
    assert d["name_rules"]["changed"] is True
    assert d["legal_tokens"]["changed"] is False


def test_api_config_test_rules_with_body_rules(client):
    r = client.post(
        "/api/config/test-rules",
        json={
            "input_name": "acme-limited",
            "rules": [
                {"pattern": "-", "replace": " "},
                {"pattern": "LIMITED", "replace": "LTD"},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["final"] == "ACME LTD"
    assert len(body["steps"]) == 2


def test_api_config_test_rules_from_current(client, db_path):
    client.post(
        "/api/config",
        json={
            "name_rules": [{"pattern": "-", "replace": " "}],
            "jurisdiction_map": [],
            "legal_tokens": [],
            "linkage_settings": {},
            "note": "rules",
        },
    )
    r = client.post(
        "/api/config/test-rules",
        json={"input_name": "ACME-LTD"},
    )
    assert r.status_code == 200
    assert r.json()["final"] == "ACME LTD"


# ---------------------------------------------------------------------------
# /api/config/jurisdictions — quick-add mappings (self-serve fix for failed runs)
# ---------------------------------------------------------------------------

def _seed_one(client):
    client.post(
        "/api/config",
        json={
            "name_rules": [],
            "jurisdiction_map": [
                {"source_dataset": "roe", "raw_value": "JERSEY", "standardised_value": "JERSEY"},
            ],
            "legal_tokens": [],
            "linkage_settings": {"match_probability_threshold_high": 0.9},
            "note": "seed",
        },
    )


def test_api_add_jurisdictions_appends_new_version(client, db_path):
    _seed_one(client)
    r = client.post(
        "/api/config/jurisdictions",
        json={
            "entries": [
                {"source_dataset": "ocod", "raw_value": "PUERTO RICO", "standardised_value": "UNITED STATES"},
                {"source_dataset": "roe", "raw_value": "TAJIKISTAN", "standardised_value": "TAJIKISTAN"},
            ],
            "note": "add june jurisdictions",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["added"] == 2

    cur = client.get("/api/config/current").json()
    jm = cur["jurisdiction_map"]
    # Original entry preserved + 2 new, and linkage_settings carried over.
    assert len(jm) == 3
    assert cur["linkage_settings"]["match_probability_threshold_high"] == 0.9
    keys = {(e["source_dataset"], e["raw_value"]) for e in jm}
    assert ("ocod", "PUERTO RICO") in keys
    assert ("roe", "TAJIKISTAN") in keys


def test_api_add_jurisdictions_dedupes_case_insensitive(client, db_path):
    _seed_one(client)
    r = client.post(
        "/api/config/jurisdictions",
        json={"entries": [{"source_dataset": "roe", "raw_value": "jersey", "standardised_value": "JERSEY"}]},
    )
    assert r.status_code == 200
    assert r.json()["added"] == 0  # already present (case-insensitive)
    assert r.json()["version"] == 1  # no new version created when nothing added
    cur = client.get("/api/config/current").json()
    assert cur["version"] == 1
    assert len(cur["jurisdiction_map"]) == 1


def test_api_add_jurisdictions_requires_existing_config(client, db_path):
    r = client.post(
        "/api/config/jurisdictions",
        json={"entries": [{"source_dataset": "roe", "raw_value": "X", "standardised_value": "Y"}]},
    )
    assert r.status_code == 400

# backend/tests/test_config_manager.py
"""config_manager and the /api/config endpoints, on the ruleset shape."""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must be set before app.main / app.auth are imported
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset, small_ruleset

from app.db import query_db
from app.services.config_manager import (
    diff_versions,
    get_current,
    get_version,
    list_versions,
    save_version,
)


# ---------------------------------------------------------------------------
# config_manager service
# ---------------------------------------------------------------------------


def test_save_and_get_version(db_path, ruleset):
    version = save_version(
        db_path, created_by="Tom", note="initial",
        ruleset=ruleset, linkage_settings={"threshold_high": 0.70},
    )
    assert version == 1
    got = get_version(db_path, 1)
    assert got["ruleset"]["default_track"] == "organisation"
    assert got["ruleset"]["track_rules"][0]["id"] == "r1"


def test_ruleset_comes_back_parsed_but_settings_stay_json(db_path, ruleset):
    """The pipeline writes linkage_settings straight to the run folder, so it
    stays the string it was stored as."""
    save_version(db_path, created_by="Tom", note="", ruleset=ruleset,
                 linkage_settings={"a": 1})
    current = get_current(db_path)
    assert isinstance(current["ruleset"], dict)
    assert isinstance(current["linkage_settings"], str)
    assert json.loads(current["linkage_settings"]) == {"a": 1}


def test_the_legacy_columns_stay_null(db_path, ruleset):
    save_version(db_path, created_by="Tom", note="", ruleset=ruleset, linkage_settings={})
    row = query_db(db_path, "SELECT * FROM config_versions WHERE version = 1")[0]
    assert row["name_rules"] is None
    assert row["jurisdiction_map"] is None
    assert row["legal_tokens"] is None


def test_get_current_returns_latest(db_path, ruleset):
    save_version(db_path, created_by="Tom", note="v1", ruleset=ruleset, linkage_settings={})
    save_version(db_path, created_by="Tom", note="v2", ruleset=ruleset, linkage_settings={})
    assert get_current(db_path)["version"] == 2


def test_get_current_empty(db_path):
    assert get_current(db_path) is None


def test_list_versions(db_path, ruleset):
    save_version(db_path, created_by="Tom", note="v1", ruleset=ruleset, linkage_settings={})
    save_version(db_path, created_by="Tom", note="v2", ruleset=ruleset, linkage_settings={})
    versions = list_versions(db_path)
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # DESC order


def test_save_logs_audit_event(db_path, ruleset):
    save_version(db_path, created_by="Tom", note="audit test",
                 ruleset=ruleset, linkage_settings={})
    rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'config'")
    assert len(rows) == 1
    assert rows[0]["user_name"] == "Tom"


# ---------------------------------------------------------------------------
# Diff, section by section
# ---------------------------------------------------------------------------


def _two_versions(db_path, first, second, settings1=None, settings2=None):
    save_version(db_path, created_by="Tom", note="v1", ruleset=first,
                 linkage_settings=settings1 or {})
    save_version(db_path, created_by="Tom", note="v2", ruleset=second,
                 linkage_settings=settings2 or {})
    return diff_versions(db_path, 1, 2)


def test_diff_reports_an_added_track_rule(db_path, ruleset):
    after = copy.deepcopy(ruleset)
    after["track_rules"].append({
        "id": "r2", "description": "", "track": "person",
        "when": [{"column": "donor_status", "op": "equals", "value": "Other"}],
    })
    diff = _two_versions(db_path, ruleset, after)
    assert diff["track_rules"] == {
        "changed": True, "added": ["r2"], "removed": [], "modified": [],
    }


def test_diff_reports_a_changed_step_by_id(db_path, ruleset):
    after = copy.deepcopy(ruleset)
    after["cleaning"]["person"][0]["op"] = "lower"
    diff = _two_versions(db_path, ruleset, after)
    assert diff["cleaning.person"]["modified"] == ["p1"]
    assert diff["cleaning.organisation"]["changed"] is False


def test_diff_reports_token_lists_and_lookups_by_name(db_path, ruleset):
    after = copy.deepcopy(ruleset)
    after["token_lists"]["extra"] = {"description": "", "tokens": ["X"]}
    del after["token_lists"]["titles"]
    after["lookups"]["nicknames"] = {"fallback": "passthrough", "rows": []}
    diff = _two_versions(db_path, ruleset, after)
    assert diff["token_lists"]["added"] == ["extra"]
    assert diff["token_lists"]["removed"] == ["titles"]
    assert diff["lookups"]["added"] == ["nicknames"]


def test_diff_compares_default_track_and_settings_whole(db_path, ruleset):
    after = copy.deepcopy(ruleset)
    after["default_track"] = "person"
    diff = _two_versions(db_path, ruleset, after, {"t": 0.7}, {"t": 0.8})
    assert diff["default_track"] == {"changed": True, "v1": "organisation", "v2": "person"}
    assert diff["linkage_settings"]["changed"] is True
    assert diff["linkage_settings"]["v2"] == {"t": 0.8}


def test_diff_covers_every_section(db_path, ruleset):
    diff = _two_versions(db_path, ruleset, ruleset)
    assert set(diff) == {
        "token_lists", "lookups", "track_rules", "default_track",
        "cleaning.person", "cleaning.organisation", "derived_columns",
        "match_keys", "vetoes",
        "linkage_settings",
    }
    assert all(section["changed"] is False for section in diff.values())


def test_diff_of_a_missing_version_raises(db_path, ruleset):
    save_version(db_path, created_by="Tom", note="", ruleset=ruleset, linkage_settings={})
    with pytest.raises(ValueError, match="version 9 not found"):
        diff_versions(db_path, 1, 9)


# ---------------------------------------------------------------------------
# /api/config endpoints
# ---------------------------------------------------------------------------

import app.main as _main_mod
import app.auth as _auth_mod


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    """TestClient with DB wired to the tmp db_path, session auth bypassed."""
    data_dir = tmp_path / "data"
    (data_dir / "runs").mkdir(parents=True)
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )

    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake"})


def _save(client, ruleset, note="v", settings=None):
    return client.post("/api/config", json={
        "ruleset": ruleset, "linkage_settings": settings or {}, "note": note,
    })


def test_api_config_current_empty(client):
    r = client.get("/api/config/current")
    assert r.status_code == 200
    assert r.json() is None


def test_api_config_save_and_get_current(client, ruleset):
    assert _save(client, ruleset, settings={"threshold_high": 0.7}).json()["version"] == 1

    body = client.get("/api/config/current").json()
    assert set(body) == {
        "version", "created_at", "created_by", "note", "ruleset", "linkage_settings",
    }
    assert body["ruleset"]["default_track"] == "organisation"
    assert body["linkage_settings"] == {"threshold_high": 0.7}


def test_api_config_rejects_an_invalid_ruleset_with_paths(client, ruleset):
    broken = copy.deepcopy(ruleset)
    broken["cleaning"]["person"][0]["op"] = "explode"
    r = _save(client, broken)
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert errors == [{"path": "cleaning.person[0].op", "message": "Unknown op 'explode'"}]
    # Nothing was stored.
    assert client.get("/api/config/current").json() is None


def test_saving_without_settings_keeps_the_ones_already_there(client, ruleset):
    """The rules screen saves a ruleset. It must not wipe the thresholds."""
    _save(client, ruleset, settings={"match_probability_threshold_high": 0.92})
    client.post("/api/config", json={"ruleset": ruleset, "note": "rules only"})
    assert client.get("/api/config/current").json()["linkage_settings"] == {
        "match_probability_threshold_high": 0.92,
    }


def test_api_config_versions_list(client, ruleset):
    _save(client, ruleset, note="v1")
    _save(client, ruleset, note="v2")
    versions = client.get("/api/config/versions").json()
    assert [v["version"] for v in versions] == [2, 1]


def test_api_config_get_specific_version(client, ruleset):
    _save(client, ruleset)
    body = client.get("/api/config/versions/1").json()
    assert body["version"] == 1
    assert body["ruleset"]["track_rules"][0]["id"] == "r1"


def test_api_config_diff(client, ruleset):
    after = copy.deepcopy(ruleset)
    after["default_track"] = "person"
    _save(client, ruleset, note="v1")
    _save(client, after, note="v2")
    diff = client.get("/api/config/diff/1/2").json()
    assert diff["default_track"]["v2"] == "person"
    assert diff["cleaning.person"]["changed"] is False


def test_api_config_diff_missing_version_is_404(client, ruleset):
    _save(client, ruleset)
    assert client.get("/api/config/diff/1/7").status_code == 404


def test_api_validate_does_not_save(client, ruleset):
    broken = copy.deepcopy(ruleset)
    broken["default_track"] = "banana"
    r = client.post("/api/config/validate", json={"ruleset": broken})
    assert r.status_code == 200
    assert r.json()["errors"][0]["path"] == "default_track"
    assert client.get("/api/config/current").json() is None


def test_api_validate_accepts_the_shipped_default(client):
    r = client.post("/api/config/validate", json={"ruleset": default_ruleset()})
    assert r.json() == {"errors": []}


# ---- functions ----


def test_api_functions_lists_the_library(client):
    library = client.get("/api/config/functions").json()
    by_name = {f["name"]: f for f in library}
    assert "parse_person_name" in by_name
    assert by_name["parse_person_name"]["outputs"] == [
        "forename", "middle_names", "surname", "forename_initial",
    ]
    assert by_name["normalise_postcode"]["outputs"] == ["target"]
    for spec in library:
        assert set(spec) == {"name", "description", "outputs", "args", "example"}
        assert spec["description"]
        assert "input" in spec["example"] and "output" in spec["example"]


# ---- columns ----


def test_api_columns_from_the_current_ruleset(client):
    _save(client, default_ruleset())
    body = client.get("/api/config/columns", params={"track": "person"}).json()

    raw_keys = [c["key"] for c in body["raw"]]
    assert "name" in raw_keys and "donor_status" in raw_keys
    assert {"key": "name", "label": "Donor"} in body["raw"]
    # The parse step contributes four columns at once.
    parse = next(s for s in body["steps"] if s["step_id"] == "p8")
    assert parse["targets"] == ["forename", "middle_names", "surname", "forename_initial"]
    assert body["all"][:len(raw_keys)] == raw_keys
    assert "surname_metaphone" in body["all"]


def test_api_columns_for_a_draft(client):
    draft = small_ruleset()
    draft["cleaning"]["organisation"].append({
        "id": "o2", "description": "", "op": "function", "name": "sorted_tokens",
        "source": "name_clean", "target": "name_tokens_sorted",
    })
    body = client.post("/api/config/columns",
                       json={"ruleset": draft, "track": "organisation"}).json()
    assert [s["targets"] for s in body["steps"]] == [["name_clean"], ["name_tokens_sorted"]]


def test_api_columns_rejects_an_unknown_track(client, ruleset):
    _save(client, ruleset)
    assert client.get("/api/config/columns", params={"track": "alien"}).status_code == 400


def test_api_columns_needs_a_config(client):
    assert client.get("/api/config/columns", params={"track": "person"}).status_code == 400

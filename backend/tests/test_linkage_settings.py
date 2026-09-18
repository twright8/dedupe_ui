# backend/tests/test_untrained_comparisons.py
"""A comparison no training rule lets vary is silently worth nothing."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.rules import linkage

DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "app", "profiles", "defaults")


def _settings(path):
    with open(os.path.join(DEFAULTS, path, "linkage_settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_columns_held_equal_reads_a_rule_the_way_splink_does():
    assert linkage.columns_held_equal("l.a = r.a") == {"a"}
    assert linkage.columns_held_equal("l.a = r.a AND l.b = r.b") == {"a", "b"}
    # A cross-column equality holds neither column to its own value.
    assert linkage.columns_held_equal("l.a = r.b") == set()
    assert linkage.columns_held_equal("") == set()


def test_a_comparison_every_rule_pins_is_flagged():
    settings = {"tracks": {"person": {
        "comparisons": [{"id": "c1", "column": "dob_year"},
                        {"id": "c2", "column": "surname"}],
        "em_blocking_rules": ["l.surname = r.surname AND l.dob_year = r.dob_year"],
    }}}
    warnings = linkage.linkage_warnings(settings)
    paths = {w["path"] for w in warnings}
    assert paths == {
        "linkage_settings.tracks.person.comparisons[0]",
        "linkage_settings.tracks.person.comparisons[1]",
    }
    assert "count for nothing" in warnings[0]["message"]
    assert "dob_year" in warnings[0]["message"]


def test_a_comparison_one_rule_leaves_free_is_not_flagged():
    settings = {"tracks": {"person": {
        "comparisons": [{"id": "c1", "column": "dob_year"},
                        {"id": "c2", "column": "surname"}],
        "em_blocking_rules": ["l.surname = r.surname",
                              "l.dob_year = r.dob_year"],
    }}}
    assert linkage.linkage_warnings(settings) == []


def test_no_training_rules_means_nothing_to_warn_about():
    settings = {"tracks": {"person": {
        "comparisons": [{"id": "c1", "column": "dob_year"}],
        "em_blocking_rules": [],
    }}}
    assert linkage.linkage_warnings(settings) == []


def test_both_shipped_profiles_are_clean():
    """The PSC person track shipped with this bug; the guard is what keeps it
    from coming back."""
    assert linkage.linkage_warnings(_settings("psc")) == []
    assert linkage.linkage_warnings(_settings("donations")) == []


def test_the_shape_psc_shipped_with_would_be_caught():
    settings = _settings("psc")
    settings["tracks"]["person"]["em_blocking_rules"] = [
        "l.surname_metaphone = r.surname_metaphone "
        "AND l.dob_year_clean = r.dob_year_clean "
        "AND l.dob_month_clean = r.dob_month_clean"
    ]
    flagged = {w["path"] for w in linkage.linkage_warnings(settings)}
    columns = settings["tracks"]["person"]["comparisons"]
    dob = [f"linkage_settings.tracks.person.comparisons[{i}]"
           for i, c in enumerate(columns) if c["column"].startswith("dob_")]
    assert set(dob) <= flagged


def test_the_validate_endpoint_carries_warnings_beside_errors(tmp_path, monkeypatch):
    import app.auth as _auth_mod
    import app.main as _main_mod
    from fastapi.testclient import TestClient

    from tests.rulesets import default_ruleset

    monkeypatch.setattr(_main_mod, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(_auth_mod, "_unsign", lambda t, max_age=None: {"authenticated": True})
    from app.db import init_db

    init_db(str(tmp_path / "t.db"))
    client = TestClient(_main_mod.app, cookies={"session": "fake"})

    settings = _settings("donations")
    settings["tracks"]["person"]["em_blocking_rules"] = ["l.surname = r.surname"]
    body = client.post("/api/config/validate", json={
        "ruleset": default_ruleset(), "linkage_settings": settings,
    }).json()

    assert body["errors"] == []
    assert any("count for nothing" in w["message"] for w in body["warnings"])
    assert body["warnings"][0]["path"].startswith("linkage_settings.tracks.person.comparisons[")

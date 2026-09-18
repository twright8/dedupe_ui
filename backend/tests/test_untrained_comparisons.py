# backend/tests/test_untrained_comparisons.py
"""A comparison no training rule lets vary is silently worth nothing.

Splink cannot estimate a comparison whose column every ``em_blocking_rules``
entry holds equal: there is no disagreement inside the training block to learn
from. The level comes back with no m probability and contributes zero, the run
finishes, and nothing says so. The PSC person track shipped that way and
accepted pairs 37 birth-years apart.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.rules import linkage

DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "app", "profiles", "defaults")


def _settings(profile: str) -> dict:
    path = os.path.join(DEFAULTS, profile, "linkage_settings.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_columns_held_equal_reads_a_rule_the_way_splink_does():
    assert linkage.columns_held_equal("l.a = r.a") == {"a"}
    assert linkage.columns_held_equal("l.a = r.a AND l.b = r.b") == {"a", "b"}
    # A cross-column equality pins neither column to its own value.
    assert linkage.columns_held_equal("l.a = r.b") == set()
    assert linkage.columns_held_equal("") == set()


def test_a_comparison_every_rule_pins_is_flagged():
    settings = {"tracks": {"person": {
        "comparisons": [{"id": "c1", "column": "dob_year"},
                        {"id": "c2", "column": "surname"}],
        "em_blocking_rules": ["l.surname = r.surname AND l.dob_year = r.dob_year"],
    }}}
    warnings = linkage.linkage_warnings(settings)
    assert {w["path"] for w in warnings} == {
        "linkage_settings.tracks.person.comparisons[0]",
        "linkage_settings.tracks.person.comparisons[1]",
    }
    assert "count for nothing" in warnings[0]["message"]
    assert "dob_year" in warnings[0]["message"]


def test_a_comparison_one_rule_leaves_free_is_not_flagged():
    settings = {"tracks": {"person": {
        "comparisons": [{"id": "c1", "column": "dob_year"},
                        {"id": "c2", "column": "surname"}],
        "em_blocking_rules": ["l.surname = r.surname", "l.dob_year = r.dob_year"],
    }}}
    assert linkage.linkage_warnings(settings) == []


def test_no_training_rules_means_nothing_to_warn_about():
    settings = {"tracks": {"person": {
        "comparisons": [{"id": "c1", "column": "dob_year"}],
        "em_blocking_rules": [],
    }}}
    assert linkage.linkage_warnings(settings) == []


def test_both_shipped_profiles_are_clean():
    """The guard is what keeps this from coming back."""
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
    expected = {
        f"linkage_settings.tracks.person.comparisons[{index}]"
        for index, comparison in enumerate(settings["tracks"]["person"]["comparisons"])
        if comparison["column"].startswith("dob_")
    }
    assert expected and expected <= flagged


def test_the_validate_endpoint_carries_warnings_beside_errors(tmp_path, monkeypatch):
    import app.auth as _auth_mod
    import app.main as _main_mod
    from fastapi.testclient import TestClient

    from app.db import init_db
    from tests.rulesets import default_ruleset

    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(_main_mod, "DB_PATH", db)
    monkeypatch.setattr(_auth_mod, "_unsign", lambda t, max_age=None: {"authenticated": True})
    client = TestClient(_main_mod.app, cookies={"session": "fake"})

    settings = _settings("donations")
    settings["tracks"]["person"]["em_blocking_rules"] = ["l.surname = r.surname"]
    body = client.post("/api/config/validate", json={
        "ruleset": default_ruleset(), "linkage_settings": settings,
    }).json()

    assert body["errors"] == []
    assert any("count for nothing" in w["message"] for w in body["warnings"])
    assert any(w["path"].startswith("linkage_settings.tracks.person.comparisons[")
               for w in body["warnings"])


def test_a_clean_draft_reports_no_warnings(tmp_path, monkeypatch):
    import app.auth as _auth_mod
    import app.main as _main_mod
    from fastapi.testclient import TestClient

    from app.db import init_db
    from tests.rulesets import default_ruleset

    db = str(tmp_path / "t.db")
    init_db(db)
    monkeypatch.setattr(_main_mod, "DB_PATH", db)
    monkeypatch.setattr(_auth_mod, "_unsign", lambda t, max_age=None: {"authenticated": True})
    client = TestClient(_main_mod.app, cookies={"session": "fake"})

    body = client.post("/api/config/validate", json={
        "ruleset": default_ruleset(), "linkage_settings": _settings("donations"),
    }).json()
    assert body == {"errors": [], "warnings": []}


# ---------------------------------------------------------------------------
# After EM: the model itself is inspected, because a settings check cannot see
# everything that stops a comparison being learned.
# ---------------------------------------------------------------------------


def _model(levels):
    return {"comparisons": [{"output_column_name": "dob_year", "comparison_levels": levels}]}


def test_a_level_with_no_m_is_reported():
    from app.pipeline.dedupe.stage_3_score import untrained_levels

    found = untrained_levels(_model([
        {"label_for_charts": "dob_year is NULL", "is_null_level": True},
        {"label_for_charts": "Exact match", "m_probability": None, "u_probability": 0.02},
        {"label_for_charts": "All other", "m_probability": None, "u_probability": 0.98},
    ]))
    assert [f["level"] for f in found] == ["Exact match", "All other"]
    assert found[0]["comparison"] == "dob_year"
    assert found[0]["missing"] == ["m"]


def test_a_trained_comparison_is_not_reported():
    from app.pipeline.dedupe.stage_3_score import untrained_levels

    assert untrained_levels(_model([
        {"label_for_charts": "dob_year is NULL", "is_null_level": True},
        {"label_for_charts": "Exact match", "m_probability": 0.9, "u_probability": 0.02},
        {"label_for_charts": "All other", "m_probability": 0.1, "u_probability": 0.98},
    ])) == []


def test_a_null_level_is_meant_to_have_neither():
    from app.pipeline.dedupe.stage_3_score import untrained_levels

    assert untrained_levels(_model([
        {"label_for_charts": "is NULL", "is_null_level": True},
        {"label_for_charts": "Exact", "m_probability": 0.9, "u_probability": 0.1},
    ])) == []


def test_the_inspector_warns_through_the_progress_callback(tmp_path):
    from app.pipeline.dedupe.stage_3_score import inspect_trained_model

    path = tmp_path / "splink_model_person.json"
    path.write_text(json.dumps(_model([
        {"label_for_charts": "Exact", "m_probability": None, "u_probability": 0.02},
    ])), encoding="utf-8")

    events = []
    found = inspect_trained_model(path, "person", lambda kind, detail: events.append((kind, detail)))
    assert len(found) == 1 and found[0]["track"] == "person"
    warnings = [d for kind, d in events if kind == "warning"]
    assert warnings and warnings[0]["kind"] == "untrained_comparisons"
    assert warnings[0]["comparisons"] == ["dob_year"]
    assert "count for nothing" in warnings[0]["message"] or "nothing" in warnings[0]["message"]


def test_a_missing_or_broken_model_file_is_not_an_error(tmp_path):
    from app.pipeline.dedupe.stage_3_score import inspect_trained_model

    assert inspect_trained_model(tmp_path / "nope.json", "person") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert inspect_trained_model(broken, "person") == []


def test_the_run_counts_carry_the_number():
    from app.routers.runs import _normalize_counts

    assert _normalize_counts({"untrained_comparisons": 3})["untrainedComparisons"] == 3
    # A run with no counts at all stays None — "not scored yet" is not "zero".
    assert _normalize_counts({}) is None
    assert _normalize_counts({"pairs_scored": 1})["untrainedComparisons"] == 0

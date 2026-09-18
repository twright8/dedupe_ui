"""Validating ``linkage_settings`` — on its own and through the config API."""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.profiles.donations import RAW_COLUMNS
from app.rules import linkage
from tests.rulesets import default_linkage_settings, default_ruleset


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )
    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake"})


def _check(settings) -> list[dict]:
    return linkage.validate_linkage_settings(settings, default_ruleset(), RAW_COLUMNS)


def _paths(errors) -> list[str]:
    return [e["path"] for e in errors]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_the_shipped_settings_pass():
    assert _check(default_linkage_settings()) == []


def test_a_splink_function_outside_the_allow_list_is_refused():
    settings = default_linkage_settings()
    settings["tracks"]["person"]["comparisons"][1]["splink_function"] = "cl.EvalMyCode"
    errors = _check(settings)
    assert _paths(errors) == \
        ["linkage_settings.tracks.person.comparisons[1].splink_function"]
    assert "cl.ExactMatch" in errors[0]["message"]


def test_a_comparison_on_a_column_the_track_does_not_produce_is_refused():
    settings = default_linkage_settings()
    settings["tracks"]["person"]["comparisons"][1]["column"] = "name_core"
    errors = _check(settings)
    assert _paths(errors) == ["linkage_settings.tracks.person.comparisons[1].column"]
    assert "name_core" in errors[0]["message"]
    assert "person track" in errors[0]["message"]


def test_a_blocking_rule_may_only_name_columns_that_exist():
    settings = default_linkage_settings()
    settings["tracks"]["organisation"]["blocking_rules"][0]["sql"] = \
        "l.postcode_clean = r.postcode_clean AND l.surname = r.made_up"
    errors = _check(settings)
    assert _paths(errors) == [
        "linkage_settings.tracks.organisation.blocking_rules[0].sql",
        "linkage_settings.tracks.organisation.blocking_rules[0].sql",
    ]
    assert {"made_up", "surname"} == {
        word for e in errors for word in ("made_up", "surname") if word in e["message"]
    }


def test_an_em_rule_is_checked_the_same_way():
    settings = default_linkage_settings()
    settings["tracks"]["person"]["em_blocking_rules"] = ["l.nope = r.nope"]
    assert _paths(_check(settings)) == \
        ["linkage_settings.tracks.person.em_blocking_rules[0]"]


def test_the_three_thresholds_must_be_in_order():
    settings = default_linkage_settings()
    settings["match_probability_threshold_review"] = 0.01
    assert _paths(_check(settings)) == \
        ["linkage_settings.match_probability_threshold_review"]

    settings = default_linkage_settings()
    settings["match_probability_threshold_high"] = 0.2
    assert _paths(_check(settings)) == \
        ["linkage_settings.match_probability_threshold_high"]

    settings = default_linkage_settings()
    settings["match_probability_threshold_candidate"] = 2.0
    assert _paths(_check(settings)) == \
        ["linkage_settings.match_probability_threshold_candidate"]


@pytest.mark.parametrize("budget", [0, -1, 1.5, "lots", True])
def test_max_pairs_must_be_a_whole_number_above_zero(budget):
    settings = default_linkage_settings()
    settings["tracks"]["person"]["max_pairs"] = budget
    assert _paths(_check(settings)) == ["linkage_settings.tracks.person.max_pairs"]


def test_an_unknown_track_a_bad_prior_and_bad_iterations_are_all_reported():
    settings = default_linkage_settings()
    settings["tracks"]["vehicle"] = {"blocking_rules": [], "comparisons": []}
    settings["probability_two_random_records_match"] = 7
    settings["em_iterations"] = 0
    assert set(_paths(_check(settings))) == {
        "linkage_settings.tracks.vehicle",
        "linkage_settings.probability_two_random_records_match",
        "linkage_settings.em_iterations",
    }


def test_a_null_prior_is_fine_because_it_means_estimate_it():
    settings = default_linkage_settings()
    settings["probability_two_random_records_match"] = None
    assert _check(settings) == []


def test_nothing_to_validate_is_not_an_error():
    assert _check(None) == []
    assert _check({}) == []


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------


def _seed(db_path):
    from app.services.config_manager import save_version

    return save_version(db_path, created_by="test", note="base",
                        ruleset=default_ruleset(),
                        linkage_settings=default_linkage_settings())


def test_saving_a_config_with_broken_settings_is_a_422_that_names_the_path(client, db_path):
    _seed(db_path)
    settings = default_linkage_settings()
    settings["tracks"]["person"]["comparisons"][1]["column"] = "not_a_column"

    response = client.post("/api/config", json={
        "ruleset": default_ruleset(), "linkage_settings": settings, "note": "bad",
    })
    assert response.status_code == 422
    paths = [e["path"] for e in response.json()["detail"]["errors"]]
    assert paths == ["linkage_settings.tracks.person.comparisons[1].column"]

    # And nothing was stored.
    assert client.get("/api/config/current").json()["version"] == 1


def test_validate_reports_settings_errors_without_saving(client, db_path):
    _seed(db_path)
    settings = default_linkage_settings()
    settings["tracks"]["organisation"]["max_pairs"] = 0

    body = client.post("/api/config/validate", json={
        "ruleset": default_ruleset(), "linkage_settings": settings,
    }).json()
    assert [e["path"] for e in body["errors"]] == \
        ["linkage_settings.tracks.organisation.max_pairs"]

    # Sent without settings, only the ruleset is judged.
    assert client.post("/api/config/validate",
                       json={"ruleset": default_ruleset()}).json()["errors"] == []


def test_a_save_that_leaves_the_settings_out_keeps_the_stored_ones(client, db_path):
    _seed(db_path)
    ruleset = copy.deepcopy(default_ruleset())
    ruleset["default_track"] = "person"

    version = client.post("/api/config", json={
        "ruleset": ruleset, "note": "rules only",
    }).json()["version"]
    stored = client.get(f"/api/config/versions/{version}").json()
    assert stored["linkage_settings"]["tracks"]["person"]["max_pairs"] == 5000000


def test_a_saved_settings_document_round_trips_through_the_api(client, db_path):
    _seed(db_path)
    current = client.get("/api/config/current").json()
    assert json.dumps(current["linkage_settings"], sort_keys=True) == \
        json.dumps(default_linkage_settings(), sort_keys=True)

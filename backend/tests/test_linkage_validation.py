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


# ---------------------------------------------------------------------------
# The two raw validation messages now come from the vocabulary
# ---------------------------------------------------------------------------


def test_a_bad_on_oversize_says_what_the_setting_is_for():
    from app import vocabulary
    from app.rules import linkage

    errors: list[dict] = []
    linkage._check_block_control(
        {"sql": "l.a = r.a", "max_block_size": 10, "on_oversize": "explode"},
        "l.a = r.a", "rule", "person", {"a"}, errors)
    messages = [e["message"] for e in errors
                if e["path"] == "rule.on_oversize"]
    assert messages == [vocabulary.choice_error("on_oversize", "explode",
                                                linkage.ON_OVERSIZE)]
    assert "what happens when a blocking rule makes too many pairs" in messages[0]


# ---------------------------------------------------------------------------
# The accept line, per track
# ---------------------------------------------------------------------------


def test_a_track_may_set_an_accept_line_of_its_own():
    """The two tracks want different lines, so a track may name its own.

    Measured on the donations sheet: the person track needs 0.96 to buy back
    the precision the surname fix cost, and the organisation track pays for
    that line in recall without wanting the precision.
    """
    settings = default_linkage_settings()
    settings["match_probability_threshold_high"] = 0.92
    settings[linkage.HIGH_BY_TRACK_KEY] = {"person": 0.96}
    assert _paths(_check(settings)) == []
    assert linkage.high_by_track(settings) == {"person": 0.96}
    assert linkage.accept_line(settings, "person") == 0.96
    assert linkage.accept_line(settings, "organisation") == 0.92
    # The single line still answers a caller with no pair in front of it.
    assert linkage.thresholds(settings)[2] == 0.92


def test_a_track_with_no_line_of_its_own_reads_the_shared_one():
    settings = default_linkage_settings()
    settings.pop(linkage.HIGH_BY_TRACK_KEY, None)
    settings["match_probability_threshold_high"] = 0.92
    assert linkage.high_by_track(settings) == {}
    assert linkage.accept_line(settings, "person") == 0.92
    assert linkage.accept_line(settings) == 0.92
    assert linkage.accept_lines(settings) == {"person": 0.92, "organisation": 0.92}


def test_a_per_track_accept_line_must_be_a_score_above_the_review_line():
    settings = default_linkage_settings()
    settings["match_probability_threshold_review"] = 0.5
    settings[linkage.HIGH_BY_TRACK_KEY] = {"person": 0.4}
    assert _paths(_check(settings)) == \
        [f"linkage_settings.{linkage.HIGH_BY_TRACK_KEY}.person"]

    settings[linkage.HIGH_BY_TRACK_KEY] = {"person": 1.4}
    assert _paths(_check(settings)) == \
        [f"linkage_settings.{linkage.HIGH_BY_TRACK_KEY}.person"]

    settings[linkage.HIGH_BY_TRACK_KEY] = {"person": "high"}
    assert _paths(_check(settings)) == \
        [f"linkage_settings.{linkage.HIGH_BY_TRACK_KEY}.person"]


def test_a_per_track_accept_line_must_name_a_track_that_exists():
    settings = default_linkage_settings()
    settings[linkage.HIGH_BY_TRACK_KEY] = {"persons": 0.96}
    assert _paths(_check(settings)) == \
        [f"linkage_settings.{linkage.HIGH_BY_TRACK_KEY}.persons"]
    # Dropped rather than guessed at, so nothing downstream reads a bad track.
    assert linkage.high_by_track(settings) == {}


def test_a_per_track_accept_line_must_be_an_object():
    settings = default_linkage_settings()
    settings[linkage.HIGH_BY_TRACK_KEY] = 0.96
    assert _paths(_check(settings)) == \
        [f"linkage_settings.{linkage.HIGH_BY_TRACK_KEY}"]


# ---------------------------------------------------------------------------
# The stage 4 gate: conjunctions, and columns counted together
# ---------------------------------------------------------------------------


def test_a_gate_clause_may_ask_for_two_counts_at_once():
    """A limit on postcode districts alone is wrong 14 times in 20 on the full
    PSC run, because one person's companies have many registered offices. Paired
    with 'more than one full birth date' it stops being a guess."""
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": [
        {"column": "surname", "count": 3},
        {"all": [{"column": "postcode_district", "count": 5},
                 {"columns": ["dob_year_clean", "dob_month_clean"], "count": 1}]},
    ]}
    assert _paths(_check(settings)) == []
    assert linkage.max_distinct_values(settings) == {"person": [
        [{"columns": ["surname"], "count": 3, "key": "surname"}],
        [{"columns": ["postcode_district"], "count": 5, "key": "postcode_district"},
         {"columns": ["dob_year_clean", "dob_month_clean"], "count": 1,
          "key": "dob_year_clean+dob_month_clean"}],
    ]}


def test_a_single_column_gate_entry_is_unchanged_by_the_conjunction():
    """The shape that shipped keeps working and keeps its own key name."""
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": {"column": "surname", "count": 3}}
    assert _paths(_check(settings)) == []
    assert linkage.max_distinct_values(settings) == {
        "person": [[{"columns": ["surname"], "count": 3, "key": "surname"}]]}


def test_a_conjunction_needs_a_column_and_a_count_on_every_member():
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": [
        {"all": [{"column": "surname", "count": 3}, {"count": 1}]}]}
    assert _paths(_check(settings)) == \
        ["linkage_settings.max_distinct_values.person[0].all[1].column"]
    # One bad member drops the whole clause: a half-read conjunction would hold
    # MORE clusters than its author meant, which is the dangerous direction.
    assert linkage.max_distinct_values(settings) == {}


def test_an_empty_conjunction_is_refused():
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": [{"all": []}]}
    assert _paths(_check(settings)) == \
        ["linkage_settings.max_distinct_values.person[0].all"]


def test_a_conjunction_cannot_hold_another_conjunction():
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": [
        {"all": [{"all": [{"column": "surname", "count": 1}]}]}]}
    assert _paths(_check(settings)) == \
        ["linkage_settings.max_distinct_values.person[0].all[0]"]


def test_columns_counted_together_must_be_a_list_of_names():
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": [{"columns": [], "count": 1}]}
    assert _paths(_check(settings)) == \
        ["linkage_settings.max_distinct_values.person[0].columns"]
    assert linkage.max_distinct_values(settings) == {}


def test_a_gate_count_must_be_a_whole_number_of_one_or_more():
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"person": [{"column": "surname", "count": 0}]}
    assert _paths(_check(settings)) == \
        ["linkage_settings.max_distinct_values.person[0].count"]


def test_the_gate_must_name_a_track_that_exists():
    settings = default_linkage_settings()
    settings["max_distinct_values"] = {"people": [{"column": "surname", "count": 3}]}
    assert _paths(_check(settings)) == \
        ["linkage_settings.max_distinct_values.people"]

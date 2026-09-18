# backend/tests/test_run_counts.py
"""A partial rerun must never wipe another stage's counts.

A run's `counts_json` is built by six stages. Anything that reruns part of the
pipeline recomputes only its own keys, and the run screen decides which tabs
exist from the `has*` flags derived from all of them — so writing a partial dict
straight back makes most of a run disappear. Every path that writes counts is
checked here against the same rule.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.db import query_db, write_db
from app.routers.runs import _normalize_counts
from app.services import run_counts

# What stage 0, stage 2 and stage 5 leave behind. No later stage recomputes any
# of these, so all of them must survive a partial rerun.
EARLIER_STAGES = {
    "input_rows": 51839,
    "records_total": 51839,
    "records_labelled": 29464,
    "exact_merged_groups": 1837,
    "exact_entities_after": 22375,
    "clusters_total": 17547,
    "entities_proposed": 17547,
    "review_queue": 1268,
    "published_at": "2026-09-18T16:50:00+00:00",
}


@pytest.fixture
def run(db_path):
    write_db(db_path, "INSERT INTO runs (id, status, counts_json) VALUES (?, ?, ?)",
             ("run_test", "complete", json.dumps(EARLIER_STAGES)))
    return db_path


def _stored(db_path):
    return json.loads(query_db(
        db_path, "SELECT counts_json FROM runs WHERE id = ?", ("run_test",)
    )[0]["counts_json"])


def test_merge_lays_new_keys_over_the_old_ones(run):
    merged = run_counts.merge(run, "run_test", {"pairs_scored": 28843,
                                                "records_total": 51840})
    assert merged["pairs_scored"] == 28843
    assert merged["records_total"] == 51840          # the caller's value wins
    assert merged["exact_merged_groups"] == 1837     # everything else survives
    assert _stored(run) == merged


def test_merge_of_nothing_changes_nothing(run):
    assert run_counts.merge(run, "run_test", None) == EARLIER_STAGES
    assert run_counts.merge(run, "run_test", {}) == EARLIER_STAGES


def test_merge_can_set_other_columns_in_the_same_statement(run):
    run_counts.merge(run, "run_test", {"pairs_scored": 5},
                     extra_sql="threshold_high = ?, threshold_review = ?, ",
                     extra_params=(0.8, 0.3))
    row = query_db(run, "SELECT threshold_high, threshold_review FROM runs "
                        "WHERE id = ?", ("run_test",))[0]
    assert (row["threshold_high"], row["threshold_review"]) == (0.8, 0.3)
    assert _stored(run)["records_total"] == 51839


def test_unreadable_counts_are_treated_as_empty(db_path):
    write_db(db_path, "INSERT INTO runs (id, status, counts_json) VALUES (?, ?, ?)",
             ("broken", "complete", "not json"))
    assert run_counts.stored(db_path, "broken") == {}
    assert run_counts.merge(db_path, "broken", {"pairs_scored": 1}) == {
        "pairs_scored": 1}


def test_a_run_with_no_counts_yet_is_empty(db_path):
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)",
             ("fresh", "complete"))
    assert run_counts.stored(db_path, "fresh") == {}


def test_the_screen_flags_survive_a_scoring_only_rerun(run):
    """The bug this file exists for: after a partial rerun the run screen hid the
    Records, Exact groups, Entities and Publish tabs, because their `has*` flags
    are derived from counts a scoring rerun does not produce."""
    before = _normalize_counts(_stored(run))
    merged = run_counts.merge(run, "run_test", {
        "pairs_scored": 28843, "pairs_accept": 26406, "units_total": 22375,
    })
    after = _normalize_counts(merged)
    # The flags the earlier stages own survive; the one this rerun owns turns on.
    for flag in ("hasRecords", "hasExact", "hasEntities"):
        assert before[flag] is True
        assert after[flag] is True, flag
    assert before["hasUnits"] is False and after["hasUnits"] is True
    assert after["recordsTotal"] == 51839
    assert after["exactEntitiesAfter"] == 22375
    assert after["clustersTotal"] == 17547
    assert after["entitiesProposed"] == 17547
    assert after["reviewQueue"] == 1268
    assert after["pairsScored"] == 28843

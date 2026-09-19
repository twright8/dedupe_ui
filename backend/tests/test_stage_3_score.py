"""Stage 3: units, the blocking budget, the overlays, the evaluation and the stage."""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.pipeline.dedupe import score_eval, stage_3_score as stage_3
from app.pipeline.dedupe import units as units_module
from tests.conftest import nulls_as_none
from app.pipeline.dedupe.stage_3_score import (
    BlockingBudgetError,
    _db_api,
    apply_overlays,
    blocking_budget_report,
    bucket_of,
    counts_from,
    finalise_pairs,
    rebucket,
    run_stage_3_score,
)
from app.rules import linkage
from tests.rulesets import default_linkage_settings, default_ruleset


# ---------------------------------------------------------------------------
# Frames the tests build on
# ---------------------------------------------------------------------------


def records_frame(rows: list[dict]) -> pd.DataFrame:
    """A cleaned records frame with the columns every stage-3 test needs."""
    frame = pd.DataFrame(rows)
    for column, default in (
        ("track", "person"), ("name", None), ("existing_entity_id", None),
        ("total_value", 0.0), ("surname", None), ("forename_initial", None),
        ("postcode", None),
    ):
        if column not in frame.columns:
            frame[column] = default
    frame["record_id"] = frame["record_id"].astype(str)
    return frame


def groups_frame(rows: list[dict]) -> pd.DataFrame:
    columns = ["record_id", "group_id", "track", "status", "key_ids", "guard"]
    frame = pd.DataFrame(rows, columns=columns)
    if len(frame):
        frame["record_id"] = frame["record_id"].astype(str)
    return frame


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_a_merged_group_is_one_unit_and_everything_else_is_its_own():
    records = records_frame([
        {"record_id": "1"}, {"record_id": "2"}, {"record_id": "3"}, {"record_id": "4"},
    ])
    groups = groups_frame([
        {"record_id": "1", "group_id": "X-1", "track": "person",
         "status": "merged", "key_ids": "k1", "guard": None},
        {"record_id": "2", "group_id": "X-1", "track": "person",
         "status": "merged", "key_ids": "k1", "guard": None},
        {"record_id": "3", "group_id": "H-k1-3", "track": "person",
         "status": "held", "key_ids": "k1", "guard": "max_group_size:9>8"},
        {"record_id": "4", "group_id": "H-k1-3", "track": "person",
         "status": "held", "key_ids": "k1", "guard": "max_group_size:9>8"},
    ])
    units, members = units_module.build_units(records, groups)

    assert list(units["unit_id"]) == ["1", "3", "4"]
    assert dict(zip(units["unit_id"], units["unit_size"])) == {"1": 2, "3": 1, "4": 1}
    # A held group's members stay apart but keep the id that groups them on screen.
    assert dict(zip(units["unit_id"], units["held_group_id"])) == {
        "1": None, "3": "H-k1-3", "4": "H-k1-3",
    }
    assert sorted(members["record_id"]) == ["1", "2", "3", "4"]
    assert dict(zip(members["record_id"], members["unit_id"])) == {
        "1": "1", "2": "1", "3": "3", "4": "4",
    }


def test_the_representative_takes_the_modal_value_and_breaks_ties_on_the_smallest_id():
    records = records_frame([
        {"record_id": "1", "name": "Alpha", "postcode": "AA1 1AA", "total_value": 1.0},
        {"record_id": "2", "name": "Beta", "postcode": "BB2 2BB", "total_value": 2.0},
        {"record_id": "3", "name": "Beta", "postcode": None, "total_value": 3.0},
    ])
    groups = groups_frame([
        {"record_id": r, "group_id": "X-1", "track": "person",
         "status": "merged", "key_ids": "k1", "guard": None} for r in ("1", "2", "3")
    ])
    units, _ = units_module.build_units(records, groups)
    row = units.iloc[0]

    assert row["name"] == "Beta"          # 2 votes against 1
    assert row["postcode"] == "AA1 1AA"   # one vote each, smallest record id wins
    assert row["total_value"] == 6.0      # priority columns are summed
    assert row["unit_size"] == 3


def test_a_unit_carries_every_distinct_existing_id_and_only_a_single_one_counts():
    records = records_frame([
        {"record_id": "1", "existing_entity_id": "E2"},
        {"record_id": "2", "existing_entity_id": "E1"},
        {"record_id": "3", "existing_entity_id": "  "},
        {"record_id": "4", "existing_entity_id": "E9"},
        {"record_id": "5", "existing_entity_id": "E9"},
        {"record_id": "6", "existing_entity_id": None},
    ])
    groups = groups_frame([
        {"record_id": r, "group_id": "X-1", "track": "person",
         "status": "merged", "key_ids": "k1", "guard": None} for r in ("1", "2", "3")
    ] + [
        {"record_id": r, "group_id": "X-4", "track": "person",
         "status": "merged", "key_ids": "k1", "guard": None} for r in ("4", "5")
    ])
    units, _ = units_module.build_units(records, groups)
    by_id = units.set_index("unit_id")

    assert by_id.loc["1", "existing_entity_ids"] == "E1 | E2"
    assert by_id.loc["1", "n_existing_ids"] == 2
    # Two old ids means no single id, so the import overlay must not read one.
    assert by_id.loc["1", "existing_entity_id"] is None
    assert by_id.loc["4", "existing_entity_ids"] == "E9"
    assert by_id.loc["4", "existing_entity_id"] == "E9"
    assert by_id.loc["6", "n_existing_ids"] == 0


# ---------------------------------------------------------------------------
# The blocking budget
# ---------------------------------------------------------------------------


def _budget_settings(max_pairs: int) -> dict:
    return {
        "tracks": {
            "person": {
                "blocking_rules": [
                    {"id": "b1", "description": "same surname",
                     "sql": "l.surname = r.surname"}
                ],
                "comparisons": [],
                "max_pairs": max_pairs,
            },
            "organisation": {"blocking_rules": [], "comparisons": [], "max_pairs": 10},
        }
    }


def _budget_units(n: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "unit_id": [str(i) for i in range(n)],
        "track": ["person"] * n,
        "surname": ["SMITH"] * n,
    })


def test_the_budget_report_counts_the_pairs_each_rule_would_make():
    report, failure = blocking_budget_report(_budget_units(), _budget_settings(100),
                                             _db_api())
    assert failure is None
    person = report["tracks"]["person"]
    assert person["units"] == 6
    assert person["rules"][0]["pairs"] == 15   # 6 * 5 / 2
    assert person["total"] == 15
    assert person["over_budget"] is False


def test_a_rule_over_budget_fails_with_a_structured_error_naming_it():
    report, failure = blocking_budget_report(_budget_units(), _budget_settings(5),
                                             _db_api())
    assert isinstance(failure, BlockingBudgetError)
    detail = failure.detail()
    assert detail["kind"] == "blocking_budget"
    assert detail["track"] == "person"
    assert detail["budget"] == 5
    assert detail["total"] == 15
    assert detail["rules"][0]["id"] == "b1"
    assert detail["rules"][0]["pairs"] == 15
    assert report["tracks"]["person"]["over_budget"] is True
    assert "b1" in str(failure)


def _em_budget_settings(max_pairs: int, em_rules: list[str]) -> dict:
    """Settings whose prediction blocking is free, so only EM can blow the budget.

    This is the shape the defect had: the prediction rules were comfortably
    inside the budget and the training rule was the expensive one.
    """
    settings = _budget_settings(max_pairs)
    settings["tracks"]["person"]["blocking_rules"] = [
        {"id": "b1", "description": "nothing matches itself",
         "sql": "l.unit_id = r.unit_id"}
    ]
    settings["tracks"]["person"]["em_blocking_rules"] = em_rules
    return settings


def test_the_budget_report_prices_the_em_training_rules_too():
    # An em_blocking_rule blocks like any other rule, and nothing downstream
    # trims what it produces, so it has to be counted before Splink runs.
    report, failure = blocking_budget_report(
        _budget_units(), _em_budget_settings(100, ["l.surname = r.surname"]), _db_api()
    )
    assert failure is None
    person = report["tracks"]["person"]
    assert person["em_rules"][0]["id"] == "em1"
    assert person["em_rules"][0]["pairs"] == 15
    assert person["em_over_budget"] is False
    # EM runs after prediction, one rule at a time, so its pairs are a separate
    # workload rather than an addition to the prediction total.
    assert person["total"] == 0


def test_an_em_rule_over_budget_fails_the_run_before_splink_starts():
    # The regression this exists for: PSC person trained on "same birth year and
    # month", which made 170M pairs from 449k units while the prediction rules
    # made 2.4M. The old check only looked at the prediction rules, reported
    # "under budget", and the run hung until it was killed.
    report, failure = blocking_budget_report(
        _budget_units(), _em_budget_settings(5, ["l.surname = r.surname"]), _db_api()
    )
    assert isinstance(failure, BlockingBudgetError)
    detail = failure.detail()
    assert detail["phase"] == "training"
    assert detail["total"] == 15
    assert detail["rules"][0]["id"] == "em1"
    assert report["tracks"]["person"]["em_over_budget"] is True
    assert "Training the model on" in str(failure)


def test_the_shipped_psc_person_em_rules_are_affordable_and_leave_the_names_free():
    from app.rules import linkage

    settings = json.loads(
        (Path("app/profiles/defaults/psc/linkage_settings.json")).read_text(
            encoding="utf-8"
        )
    )
    rules = linkage.em_rules(linkage.track_settings(settings, "person"))
    # The date-of-birth training rule must carry a third, selective column.
    # Year plus month alone is 1,058 blocks over the whole of PSC.
    dob_rule = [r for r in rules if "dob_year_clean" in r][0]
    assert "postcode_district" in dob_rule
    # ...and it must still leave both name comparisons free to be estimated,
    # which is the only reason the rule exists.
    assert "surname" not in dob_rule
    assert "forename" not in dob_rule
    assert linkage.linkage_warnings(settings) == []


def test_stale_duckdb_spill_is_cleared_and_the_run_outputs_are_not(tmp_path):
    from app.pipeline.dedupe.stage_3_score import clear_duckdb_tmp

    tmp_dir = tmp_path / "duckdb_tmp"
    tmp_dir.mkdir()
    (tmp_dir / "duckdb_temp_storage_DEFAULT-0.tmp").write_bytes(b"x" * 2048)
    (tmp_dir / "duckdb_temp_storage_S32K-1.tmp").write_bytes(b"x" * 1024)
    keep = tmp_dir / "notes.json"
    keep.write_text("{}", encoding="utf-8")

    assert clear_duckdb_tmp(tmp_dir) == 3072
    assert not list(tmp_dir.glob("duckdb_temp_storage_*.tmp"))
    assert keep.is_file()
    # A missing directory is the normal first-run case, not an error.
    assert clear_duckdb_tmp(tmp_path / "nothing_here") == 0


def test_the_runner_turns_a_budget_failure_into_run_error_detail():
    from app.services.pipeline_runner import _build_error_detail

    error = BlockingBudgetError("person", 5, 15, [{"id": "b1", "description": "",
                                                   "pairs": 15}])
    assert json.loads(_build_error_detail(error))["kind"] == "blocking_budget"


# ---------------------------------------------------------------------------
# Bucketing and the overlays
# ---------------------------------------------------------------------------


def _overlay_units() -> pd.DataFrame:
    return pd.DataFrame({
        "unit_id": ["1", "2", "3", "4", "5"],
        "unit_size": [1, 1, 1, 1, 1],
        "track": ["person"] * 5,
        "existing_entity_id": ["E1", "E1", "E2", None, None],
        "held_group_id": ["H-k1-1", "H-k1-1", None, "H-k1-4", None],
        "total_value": [10.0, 20.0, 30.0, 40.0, 50.0],
    })


def _overlay_pairs() -> pd.DataFrame:
    return pd.DataFrame({
        "unit_id_l": ["1", "1", "4", "2"],
        "unit_id_r": ["2", "3", "5", "3"],
        "match_probability": [0.20, 0.99, 0.60, 0.95],
        "match_weight": [-2.0, 7.0, 0.6, 4.2],
    })


def test_the_score_buckets_on_the_runs_two_lines():
    assert list(bucket_of(pd.Series([0.95, 0.60, 0.10]), 0.5, 0.92)) == \
        ["accept", "review", "reject"]
    # The lines are inclusive at the bottom of each band.
    assert list(bucket_of(pd.Series([0.92, 0.50]), 0.5, 0.92)) == ["accept", "review"]


def test_the_import_overlay_accepts_agreeing_ids_and_only_flags_disagreeing_ones():
    pairs = apply_overlays(_overlay_pairs(), _overlay_units(), 0.5, 0.92)
    by_pair = pairs.set_index(["unit_id_l", "unit_id_r"])

    # Same single old id: accepted however low the score.
    agree = by_pair.loc[("1", "2")]
    assert (agree["score_bucket"], agree["bucket"], agree["decided_by"]) == \
        ("reject", "accept", "import")
    assert agree["import_disagrees"] is False or agree["import_disagrees"] == False

    # Different old ids: the bucket is left alone and the pair is flagged.
    disagree = by_pair.loc[("1", "3")]
    assert (disagree["score_bucket"], disagree["bucket"], disagree["decided_by"]) == \
        ("accept", "accept", "score")
    assert bool(disagree["import_disagrees"]) is True

    # Neither side has an id: nothing to say.
    quiet = by_pair.loc[("4", "5")]
    assert (quiet["bucket"], quiet["decided_by"]) == ("review", "score")
    assert bool(quiet["import_disagrees"]) is False


def test_a_pair_is_flagged_only_when_both_units_sit_in_the_same_held_group():
    pairs = apply_overlays(_overlay_pairs(), _overlay_units(), 0.5, 0.92)
    by_pair = pairs.set_index(["unit_id_l", "unit_id_r"])
    assert by_pair.loc[("1", "2"), "held_group_id"] == "H-k1-1"
    assert by_pair.loc[("1", "3"), "held_group_id"] is None
    assert by_pair.loc[("4", "5"), "held_group_id"] is None


def test_the_pair_ids_are_ordered_as_strings_and_the_priorities_are_summed():
    pairs = pd.DataFrame({
        "unit_id_l": ["3"], "unit_id_r": ["1"],
        "match_probability": [0.99], "match_weight": [7.0],
    })
    result = finalise_pairs(apply_overlays(pairs, _overlay_units(), 0.5, 0.92),
                            _overlay_units())
    assert list(result["unit_id_l"]) == ["1"]
    assert list(result["unit_id_r"]) == ["3"]
    assert list(result["priority_total_value"]) == [40.0]


def test_the_pairs_file_holds_the_score_and_the_import_and_nothing_human():
    """The human overlay is joined on where the pairs are read, never baked in.

    A label write must not rewrite a file that will one day hold millions of
    rows, so ``apply_overlays`` knows nothing about labels at all — see
    ``test_pair_labels.py`` for the layer that does.
    """
    import inspect

    assert "labels" not in inspect.signature(apply_overlays).parameters
    pairs = apply_overlays(_overlay_pairs(), _overlay_units(), 0.5, 0.92)
    assert set(pairs["decided_by"]) <= {"score", "import"}


# ---------------------------------------------------------------------------
# The evaluation, against numbers worked out by hand
# ---------------------------------------------------------------------------


def _eval_fixtures():
    """Six records: A over r1-r3, B over r4-r5, and r6 never reviewed.

    The exact keys merged r1 with r2. The scorer then accepts (u1, u3) and
    (u5, u6) and rejects (u3, u6). Nothing ever puts u4 beside u5, so the one B
    pair the reviewers made is the recall this run misses.
    """
    records = records_frame([
        {"record_id": "1", "existing_entity_id": "A"},
        {"record_id": "2", "existing_entity_id": "A"},
        {"record_id": "3", "existing_entity_id": "A"},
        {"record_id": "4", "existing_entity_id": "B"},
        {"record_id": "5", "existing_entity_id": "B"},
        {"record_id": "6", "existing_entity_id": None},
    ])
    groups = groups_frame([
        {"record_id": r, "group_id": "X-1", "track": "person",
         "status": "merged", "key_ids": "k1", "guard": None} for r in ("1", "2")
    ])
    units, members = units_module.build_units(records, groups)
    pairs = pd.DataFrame({
        "unit_id_l": ["1", "5", "3"],
        "unit_id_r": ["3", "6", "6"],
        "track": ["person"] * 3,
        "match_probability": [0.99, 0.97, 0.30],
        "match_weight": [7.0, 5.0, -1.0],
    })
    pairs = finalise_pairs(apply_overlays(pairs, units, 0.5, 0.92), units)
    return records, groups, units, members, pairs


def test_the_evaluation_matches_the_numbers_worked_out_by_hand():
    records, groups, units, members, pairs = _eval_fixtures()
    result = score_eval.evaluate(records, groups, units, members, pairs)

    # Units: {r1,r2}, r3, r4, r5, r6.
    assert result["units_total"] == 5
    # Components: {u1,u3} = r1,r2,r3; {u4} = r4; {u5,u6} = r5,r6.
    assert result["entities_after"] == 3
    # Labelled pairs inside a component: 3 in the first, all agreeing on A.
    assert result["labelled_pairs"] == 3
    assert result["labelled_pairs_agreeing"] == 3
    assert result["pair_precision"] == 1.0
    # Reviewers put A over 3 records (3 pairs) and B over 2 (1 pair). The run
    # finds the three A pairs and misses the B one.
    assert result["manual_pairs"] == 4
    assert result["manual_pairs_found"] == 3
    assert result["pair_recall"] == 0.75
    # The exact keys alone only found r1-r2.
    assert result["exact_only"]["entities_after"] == 5
    assert result["exact_only"]["pair_recall"] == 0.25


def test_the_evaluation_reports_a_conflict_when_two_old_ids_meet():
    records, groups, units, members, pairs = _eval_fixtures()
    joined = pd.concat([pairs, finalise_pairs(apply_overlays(pd.DataFrame({
        "unit_id_l": ["3"], "unit_id_r": ["4"], "track": ["person"],
        "match_probability": [0.99], "match_weight": [7.0],
    }), units, 0.5, 0.92), units)], ignore_index=True)
    result = score_eval.evaluate(records, groups, units, members, joined)

    # A and B now meet in one component: r1,r2,r3 (A) with r4 (B).
    assert result["entities_after"] == 2
    assert result["conflicts"] == 1
    assert result["labelled_pairs"] == 6     # four labelled records in one component
    assert result["labelled_pairs_agreeing"] == 3
    assert result["pair_precision"] == 0.5


def test_the_review_split_counts_the_score_bucket_not_the_overlaid_one():
    units = _overlay_units()
    records = records_frame([
        {"record_id": str(i), "existing_entity_id": e}
        for i, e in zip(range(1, 6), ["E1", "E1", "E2", None, None])
    ])
    groups = groups_frame([])
    _u, members = units_module.build_units(records, groups)
    pairs = pd.DataFrame({
        "unit_id_l": ["1", "1", "4"],
        "unit_id_r": ["2", "3", "5"],
        "track": ["person"] * 3,
        "match_probability": [0.60, 0.70, 0.55],
        "match_weight": [0.5, 1.0, 0.2],
    })
    pairs = finalise_pairs(apply_overlays(pairs, units, 0.5, 0.92), units)
    result = score_eval.evaluate(records, groups, units, members, pairs)

    # All three score into the review band; the overlay lifts the agreeing one.
    assert result["review"]["score_bucket_pairs"] == 3
    assert result["review"]["pairs"] == 2
    assert result["review"]["import_agrees"] == 1
    assert result["review"]["import_disagrees"] == 1
    assert result["review"]["import_unknown"] == 1


def test_the_histogram_has_fifty_bins_split_by_what_the_labels_say():
    records, groups, units, members, pairs = _eval_fixtures()
    result = score_eval.evaluate(records, groups, units, members, pairs)
    histogram = result["histogram"]

    assert histogram["bins"] == 50
    assert len(histogram["edges"]) == 51
    for name in ("agrees", "disagrees", "unknown"):
        assert len(histogram[name]) == 50
    assert sum(histogram["agrees"]) + sum(histogram["disagrees"]) \
        + sum(histogram["unknown"]) == len(pairs)


# ---------------------------------------------------------------------------
# The stage, end to end on a small frame
# ---------------------------------------------------------------------------


SMALL_SETTINGS = {
    "tracks": {
        "person": {
            "blocking_rules": [{"id": "b1", "description": "same surname",
                                "sql": "l.surname = r.surname"}],
            "comparisons": [
                {"id": "c1", "column": "forename", "term_frequency": False,
                 "splink_function": "cl.JaroWinklerAtThresholds",
                 "splink_args": {"score_threshold_or_thresholds": [0.9]}},
                {"id": "c2", "column": "postcode", "term_frequency": False,
                 "splink_function": "cl.ExactMatch", "splink_args": {}},
            ],
            "em_blocking_rules": ["l.surname = r.surname"],
            "max_pairs": 1000000,
        },
        "organisation": {"blocking_rules": [], "comparisons": [], "max_pairs": 1000},
    },
    "em_iterations": 3,
    "random_seed": 42,
    "probability_two_random_records_match": 0.01,
    "match_probability_threshold_candidate": 0.01,
    "match_probability_threshold_review": 0.5,
    "match_probability_threshold_high": 0.92,
}


def _small_run(tmp_path, settings=None):
    """A run folder with 30 person records, half of them near-duplicates."""
    run_dir = tmp_path / "run"
    config_dir = run_dir / "config"
    config_dir.mkdir(parents=True)

    rows = []
    for i in range(15):
        surname = f"SURNAME{i:02d}"
        rows.append({"record_id": f"{i * 2 + 1}", "name": f"Ann {surname}",
                     "surname": surname, "forename": "ANN",
                     "postcode": f"AA{i} 1AA", "total_value": 10.0,
                     "existing_entity_id": f"E{i}" if i < 8 else None})
        rows.append({"record_id": f"{i * 2 + 2}", "name": f"Anne {surname}",
                     "surname": surname, "forename": "ANNE",
                     "postcode": f"AA{i} 1AA", "total_value": 20.0,
                     "existing_entity_id": f"E{i}" if i < 8 else None})
    records = records_frame(rows)
    records.to_parquet(run_dir / "records.parquet", index=False)
    groups_frame([]).to_parquet(run_dir / "exact_groups.parquet", index=False)
    (config_dir / "ruleset.json").write_text(json.dumps(default_ruleset()),
                                             encoding="utf-8")
    (config_dir / "linkage_settings.json").write_text(
        json.dumps(settings or SMALL_SETTINGS), encoding="utf-8"
    )
    return run_dir, config_dir


@pytest.mark.slow
def test_the_stage_scores_a_small_frame_end_to_end(tmp_path):
    run_dir, config_dir = _small_run(tmp_path)
    counts = run_stage_3_score(str(run_dir), str(config_dir),
                               render_diagnostics=False)

    assert counts["units_total"] == 30
    assert counts["pairs_scored"] >= 15
    assert (run_dir / "pairs.parquet").is_file()
    assert (run_dir / "units.parquet").is_file()
    assert (run_dir / "unit_members.parquet").is_file()
    assert (run_dir / "splink_model_person.json").is_file()

    report = json.loads((run_dir / "blocking_report.json").read_text())
    assert report["tracks"]["person"]["total"] == 15
    assert report["tracks"]["person"]["over_budget"] is False

    pairs = pd.read_parquet(run_dir / "pairs.parquet")
    assert set(pairs["track"]) == {"person"}
    assert (pairs["unit_id_l"] < pairs["unit_id_r"]).all()
    assert any(c.startswith("gamma_") for c in pairs.columns)
    assert "priority_total_value" in pairs.columns

    evaluation = json.loads((run_dir / "score_eval.json").read_text())
    assert evaluation["units_total"] == 30
    assert evaluation["entities_after"] <= 30


@pytest.mark.slow
def test_the_stage_fails_the_run_when_a_track_is_over_budget(tmp_path):
    settings = json.loads(json.dumps(SMALL_SETTINGS))
    settings["tracks"]["person"]["max_pairs"] = 3
    run_dir, config_dir = _small_run(tmp_path, settings)

    with pytest.raises(BlockingBudgetError) as caught:
        run_stage_3_score(str(run_dir), str(config_dir), render_diagnostics=False)

    assert caught.value.detail()["track"] == "person"
    # The report is written before the failure, so the screen can show the rule.
    assert (run_dir / "blocking_report.json").is_file()
    # Nothing is scored when a rule explodes.
    assert not (run_dir / "pairs.parquet").exists()


@pytest.mark.slow
def test_a_track_with_nothing_to_compare_is_skipped_not_failed(tmp_path):
    settings = json.loads(json.dumps(SMALL_SETTINGS))
    settings["tracks"]["person"]["blocking_rules"] = []
    run_dir, config_dir = _small_run(tmp_path, settings)

    counts = run_stage_3_score(str(run_dir), str(config_dir),
                               render_diagnostics=False)
    assert counts["pairs_scored"] == 0
    assert counts["units_total"] == 30


@pytest.mark.slow
def test_rebucketing_moves_the_lines_without_rerunning_splink(tmp_path):
    run_dir, config_dir = _small_run(tmp_path)
    before = run_stage_3_score(str(run_dir), str(config_dir),
                               render_diagnostics=False)
    written = pd.read_parquet(run_dir / "pairs.parquet")

    after = rebucket(str(run_dir), threshold_high=0.999, threshold_review=0.99)
    assert after["pairs_scored"] == before["pairs_scored"]
    assert after["pairs_accept"] <= before["pairs_accept"]
    # The scores themselves are untouched — only the lines drawn over them move.
    again = pd.read_parquet(run_dir / "pairs.parquet")
    assert list(again["match_probability"]) == list(written["match_probability"])


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_the_stage_counts_use_the_dedupe_keys_and_nothing_legacy():
    records, groups, units, members, pairs = _eval_fixtures()
    evaluation = score_eval.evaluate(records, groups, units, members, pairs)
    counts = counts_from(units, pairs, evaluation)

    assert counts["units_total"] == 5
    assert counts["pairs_scored"] == 3
    assert counts["pairs_accept"] + counts["pairs_review"] + counts["pairs_reject"] == 3
    assert counts["entities_after_score"] == 3
    assert counts["score_pair_precision"] == 1.0
    assert counts["score_pair_recall"] == 0.75


def test_the_run_counts_expose_the_new_keys_and_a_honest_has_pairs_flag():
    from app.routers.runs import _normalize_counts

    empty = _normalize_counts({"records_total": 3})
    assert empty["hasPairs"] is False
    assert empty["pairsScored"] == 0

    scored = _normalize_counts({
        "units_total": 5, "pairs_scored": 3, "pairs_accept": 2, "pairs_review": 1,
        "pairs_reject": 0, "pairs_decided_by_import": 1, "pairs_import_disagrees": 1,
        "entities_after_score": 3, "score_pair_precision": 1.0,
        "score_pair_recall": 0.75,
    })
    assert scored["hasPairs"] is True
    assert scored["unitsTotal"] == 5
    assert scored["pairsScored"] == 3
    assert scored["pairsDecidedByImport"] == 1
    assert scored["pairsImportDisagrees"] == 1
    assert scored["entitiesAfterScore"] == 3
    assert scored["scorePairPrecision"] == 1.0
    assert scored["scorePairRecall"] == 0.75


# ---------------------------------------------------------------------------
# The shipped defaults
# ---------------------------------------------------------------------------


def test_the_shipped_linkage_settings_are_valid_against_the_shipped_ruleset():
    from app.profiles.donations import RAW_COLUMNS

    errors = linkage.validate_linkage_settings(
        default_linkage_settings(), default_ruleset(), RAW_COLUMNS
    )
    assert errors == []


def test_the_shipped_settings_name_both_tracks_and_a_positive_budget():
    settings = default_linkage_settings()
    for track in ("person", "organisation"):
        config = settings["tracks"][track]
        assert len(linkage.blocking_rules(config)) == 3
        assert linkage.comparisons(config)
        assert linkage.max_pairs(config) > 0
    assert linkage.thresholds(settings) == (0.05, 0.5, 0.92)


# ---------------------------------------------------------------------------
# The representative vote, now done in DuckDB
# ---------------------------------------------------------------------------


def _reference_representative(joined, column):
    """The rule LINKAGE.md states, written the slow obvious way.

    Most frequent non-null value per unit, ties to the smallest record_id. The
    DuckDB version has to agree with this on every frame.
    """
    answer = {}
    for unit_id, group in joined.groupby("unit_id", sort=True):
        seen = group[group[column].notna()]
        if not len(seen):
            continue
        best = None
        for value in seen[column].unique():
            rows = seen[seen[column] == value]
            key = (-len(rows), min(rows["record_id"]))
            if best is None or key < best[0]:
                best = (key, value)
        answer[unit_id] = best[1]
    return answer


def test_the_duckdb_vote_matches_the_rule_on_ties_nulls_and_mixed_types():
    joined = pd.DataFrame({
        "unit_id": ["u1"] * 6 + ["u2"] * 3 + ["u3"],
        "record_id": ["9", "2", "7", "3", "5", "1", "b", "a", "c", "z"],
        # u1: BETA twice, ALPHA twice -> tie, and record 2 is smaller than 3.
        "name":   ["BETA", "ALPHA", "BETA", "ALPHA", "GAMMA", None,
                   "X", "X", None, None],
        # A null-only column for one unit, and a column that is all null.
        "town":   [None, "LEEDS", None, None, None, "LEEDS",
                   None, None, None, "YORK"],
        "empty":  [None] * 10,
        # Mixed types: a float with a null, and a boolean.
        "amount": [1.0, 2.0, 2.0, None, 1.0, 1.0, 5.0, 5.0, None, 9.0],
        "flag":   [True, False, True, True, None, False, True, True, None, False],
    })
    columns = ["name", "town", "empty", "amount", "flag"]
    found = units_module.representatives(joined, columns)

    for column in columns:
        expected = _reference_representative(joined, column)
        got = {
            unit: value for unit, value in found[column].dropna().items()
        } if column in found.columns else {}
        assert got == expected, column

    # Spelled out, so the rule is readable and not just asserted:
    assert found.loc["u1", "name"] == "ALPHA"   # tie on count, record 2 < 3
    assert found.loc["u1", "town"] == "LEEDS"   # the only non-null value
    assert found.loc["u1", "amount"] == 1.0     # three of them
    assert "empty" not in found.columns or pd.isna(found.loc["u1", "empty"])


def test_the_unit_build_is_unchanged_by_where_the_vote_happens():
    """A frame with ties, nulls and mixed types, end to end."""
    records = records_frame([
        {"record_id": "1", "name": "Ann", "postcode": None, "total_value": 1.0,
         "existing_entity_id": "E1"},
        {"record_id": "2", "name": "Anne", "postcode": "LE1 1AA", "total_value": 2.0,
         "existing_entity_id": None},
        {"record_id": "3", "name": "Anne", "postcode": "LE1 1AA", "total_value": 3.0,
         "existing_entity_id": "E2"},
        {"record_id": "4", "name": None, "postcode": None, "total_value": None,
         "existing_entity_id": None},
    ])
    groups = groups_frame([
        {"record_id": r, "group_id": "X-1", "track": "person", "status": "merged",
         "key_ids": "k1", "guard": None} for r in ("1", "2", "3")
    ])
    units, members = units_module.build_units(records, groups)
    row = units.set_index("unit_id").loc["1"]

    assert row["name"] == "Anne"                 # two of them beat one
    assert row["postcode"] == "LE1 1AA"          # nulls do not vote
    assert row["unit_size"] == 3
    assert row["total_value"] == 6.0             # priority columns are summed
    assert row["existing_entity_ids"] == "E1 | E2"
    assert row["n_existing_ids"] == 2
    assert row["existing_entity_id"] is None     # two ids means no single id
    assert len(members) == 4


# ---------------------------------------------------------------------------
# Out of core: the pairs never exist whole
# ---------------------------------------------------------------------------


def test_only_the_ids_the_scores_and_the_gammas_survive_a_prediction():
    """Splink hands back both sides' values and a Bayes factor per comparison.

    `finalise_pairs` threw all of that away again, but only after 3.3 GB of it
    had become Python objects. The projection drops them in SQL instead.
    """
    names = ["unit_id_l", "unit_id_r", "match_probability", "match_weight",
             "surname_l", "surname_r", "gamma_surname", "tf_surname_l",
             "bf_surname", "bf_tf_adj_surname", "gamma_postcode"]
    assert stage_3.prediction_columns(names) == [
        "unit_id_l", "unit_id_r", "match_probability", "match_weight",
        "gamma_postcode", "gamma_surname",
    ]


def test_the_batch_sizes_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(stage_3.PAIR_BATCH_ENV, "1234")
    monkeypatch.setenv(stage_3.MODEL_BATCH_ENV, "99")
    assert stage_3.pair_batch_rows() == 1234
    assert stage_3.model_batch_pairs() == 99
    monkeypatch.setenv(stage_3.PAIR_BATCH_ENV, "0")
    monkeypatch.setenv(stage_3.MODEL_BATCH_ENV, "nonsense")
    assert stage_3.pair_batch_rows() == stage_3.DEFAULT_PAIR_BATCH
    assert stage_3.model_batch_pairs() == stage_3.DEFAULT_MODEL_BATCH


def test_the_overlay_projection_asks_for_the_columns_it_reads_and_no_others():
    ruleset = {"vetoes": [{"id": "v1", "track": "person", "action": "reject",
                           "when": [{"column": "dob_year_clean", "op": "abs_diff_gt",
                                     "value": 1}]}]}

    class Profile:
        priority_columns = ["total_value"]

    columns = stage_3.overlay_columns(ruleset, profile=Profile())
    assert columns == ["unit_id", "track", "existing_entity_id", "held_group_id",
                       "total_value", "dob_year_clean"]


def test_a_batched_overlay_writes_what_one_pass_writes(tmp_path):
    """The identity that makes the streaming safe: a pairs file written a batch
    at a time has to equal the file written in one go, row for row."""
    units = _overlay_units()
    pairs = _overlay_pairs()
    pairs["gamma_surname"] = [0, 2, 1, 2]
    source = tmp_path / "predictions_person.parquet"
    pairs.to_parquet(source, index=False)

    whole = tmp_path / "whole.parquet"
    stage_3.overlay_predictions({"person": source}, units, 0.5, 0.92, whole,
                                batch_rows=1000)
    batched = tmp_path / "batched.parquet"
    stage_3.overlay_predictions({"person": source}, units, 0.5, 0.92, batched,
                                batch_rows=1)
    left = pd.read_parquet(whole)
    right = pd.read_parquet(batched)
    assert left.equals(right)
    # And it is what the in-memory path has always produced. Stage 3 always
    # stamped the track onto a track's pairs before the overlays ran, so the
    # streamed file carries it too.
    in_memory = pairs.copy()
    in_memory["track"] = "person"
    expected = finalise_pairs(apply_overlays(in_memory, units, 0.5, 0.92), units)
    # File against frame, so nulls are compared by meaning: a frame that never
    # went through parquet spells a missing value differently. The file against
    # file check above is the one that is exact, and it is.
    assert list(left.columns) == list(expected.columns)
    assert nulls_as_none(left).equals(nulls_as_none(expected))


def test_two_tracks_are_laid_out_on_one_set_of_columns(tmp_path):
    """Each track has its own comparisons, so its own gammas. The file holds the
    union with a null where a track has no such comparison — which is exactly
    what concatenating the two frames used to leave behind."""
    units = _overlay_units().copy()
    units["track"] = ["person", "person", "person", "organisation", "organisation"]
    person = pd.DataFrame({
        "unit_id_l": ["1"], "unit_id_r": ["2"], "match_probability": [0.99],
        "match_weight": [7.0], "gamma_surname": [2],
    })
    organisation = pd.DataFrame({
        "unit_id_l": ["4"], "unit_id_r": ["5"], "match_probability": [0.95],
        "match_weight": [4.0], "gamma_regnum": [1],
    })
    person.to_parquet(tmp_path / "p.parquet", index=False)
    organisation.to_parquet(tmp_path / "o.parquet", index=False)

    out = tmp_path / "pairs.parquet"
    stage_3.overlay_predictions(
        {"person": tmp_path / "p.parquet", "organisation": tmp_path / "o.parquet"},
        units, 0.5, 0.92, out, batch_rows=1,
    )
    written = pd.read_parquet(out)
    assert list(written["track"]) == ["person", "organisation"]
    assert {"gamma_surname", "gamma_regnum"} <= set(written.columns)
    assert pd.isna(written.loc[1, "gamma_surname"])
    assert pd.isna(written.loc[0, "gamma_regnum"])


def test_a_writer_keeps_one_schema_when_the_first_batch_is_all_null(tmp_path):
    """Stage 1 learnt this on the records file and it holds here.

    A batch where every veto reason is null must not write a different type from
    the batch after it, or the file cannot be read back at all.
    """
    path = tmp_path / "pairs.parquet"
    writer = stage_3.PairWriter(path)
    writer.write(pd.DataFrame({"unit_id_l": ["1"], "vetoed_by": [None]}))
    writer.write(pd.DataFrame({"unit_id_l": ["2"], "vetoed_by": ["v1"]}))
    writer.close()
    back = pd.read_parquet(path)
    assert list(back["unit_id_l"]) == ["1", "2"]
    # One column, one type, both rows readable. pandas 3 hands a text column's
    # missing value back as NaN, so the null is compared by meaning.
    assert nulls_as_none(back)["vetoed_by"].tolist() == [None, "v1"]


def test_the_pair_counts_agree_with_the_frame_they_replace(tmp_path):
    from app.rules import vetoes

    units = _overlay_units()
    pairs = finalise_pairs(apply_overlays(_overlay_pairs(), units, 0.5, 0.92), units)
    pairs["vetoed_by"] = [None, "v1", None, None]
    path = tmp_path / "pairs.parquet"
    pairs.to_parquet(path, index=False)

    counted = stage_3.pair_counts(path, tmp_path / "duckdb_tmp")
    assert counted["pairs_scored"] == len(pairs)
    assert counted["pairs_decided_by_import"] == int(
        (pairs["decided_by"] == "import").sum())
    assert counted["pairs_import_disagrees"] == int(
        pairs["import_disagrees"].fillna(False).sum())
    for key, value in vetoes.counts_from(pairs).items():
        assert counted[key] == value, key


def test_the_unit_counts_agree_with_the_frame_they_replace(tmp_path):
    units = _overlay_units()
    path = tmp_path / "units.parquet"
    units.to_parquet(path, index=False)
    assert stage_3.unit_counts_of(path, tmp_path / "duckdb_tmp") == \
        units_module.counts_from(units)


def test_rewriting_the_pairs_in_batches_is_the_in_memory_answer(tmp_path):
    units = _overlay_units()
    pairs = finalise_pairs(apply_overlays(_overlay_pairs(), units, 0.5, 0.92), units)
    path = tmp_path / "pairs.parquet"
    pairs.to_parquet(path, index=False)

    stage_3.rewrite_pairs(path, units, 0.5, 0.98, batch_rows=1)
    rewritten = pd.read_parquet(path)
    expected = finalise_pairs(apply_overlays(pairs.copy(), units, 0.5, 0.98), units)
    assert list(rewritten.columns) == list(expected.columns)
    # Again file against frame, so the nulls are compared by meaning.
    assert nulls_as_none(rewritten).equals(nulls_as_none(expected))

    # And a batch of one writes the same file as one pass, exactly.
    one_pass = tmp_path / "one_pass.parquet"
    expected.to_parquet(one_pass, index=False)
    stage_3.rewrite_pairs(one_pass, units, 0.5, 0.98, batch_rows=1000)
    assert rewritten.equals(pd.read_parquet(one_pass))


def test_the_evaluation_is_the_same_whether_it_reads_a_frame_or_a_file(tmp_path):
    """The evaluation of a hundred million pairs has to stream, and streaming it
    must not change a single number."""
    records, groups, units, members, pairs = _eval_fixtures()
    path = tmp_path / "pairs.parquet"
    pairs.to_parquet(path, index=False)

    in_memory = score_eval.evaluate(records, groups, units, members, pairs)
    from_file = score_eval.evaluate(records, groups, units, members, path)
    assert json.dumps(from_file, sort_keys=True, default=str) == \
        json.dumps(in_memory, sort_keys=True, default=str)


def test_the_components_step_is_given_integer_arrays():
    """Strings in SciPy, or a dict of fifteen million of them, is what had to go."""
    import numpy as np

    rows = np.array([0, 1], dtype="int32")
    cols = np.array([1, 2], dtype="int32")
    labels = score_eval.components_of(rows, cols, 4)
    assert len(labels) == 4
    assert labels[0] == labels[1] == labels[2]
    assert labels[3] != labels[0]


def test_the_histogram_is_counted_in_duckdb_not_embedded_row_by_row(tmp_path):
    """The person chart was a 68 MB HTML file because Altair embeds its data.

    Fifty bins is fifty rows, whatever the run's size.
    """
    import numpy as np

    units = _overlay_units()
    raw = _overlay_pairs()
    raw["track"] = "person"
    pairs = finalise_pairs(apply_overlays(raw, units, 0.5, 0.92), units)
    path = tmp_path / "pairs.parquet"
    pairs.to_parquet(path, index=False)

    counts = stage_3.histogram_counts(path, "person", tmp_path / "duckdb_tmp")
    assert int(counts["pairs"].sum()) == len(pairs)
    assert counts["bin"].max() < stage_3.HISTOGRAM_BINS
    edges = np.linspace(0.0, 1.0, stage_3.HISTOGRAM_BINS + 1)
    expected, _ = np.histogram(pairs["match_probability"].to_numpy(), bins=edges)
    got = np.zeros(stage_3.HISTOGRAM_BINS, dtype="int64")
    for _, row in counts.iterrows():
        got[int(row["bin"])] += int(row["pairs"])
    assert list(got) == list(expected)


# ---------------------------------------------------------------------------
# The hot-key control, where the budget and the scorer meet it
# ---------------------------------------------------------------------------


def _hot_units(n_hot: int = 40, n_rest: int = 6) -> pd.DataFrame:
    """One very common surname and a few rare ones, with two forename initials."""
    rows = [{"unit_id": f"h{i}", "track": "person", "surname": "SMITH",
             "forename_initial": "A" if i % 2 else "B"} for i in range(n_hot)]
    rows += [{"unit_id": f"r{i}", "track": "person", "surname": f"RARE{i}",
              "forename_initial": "C"} for i in range(n_rest)]
    return pd.DataFrame(rows)


def _hot_settings(control: dict | None) -> dict:
    rule = {"id": "b1", "description": "same surname",
            "sql": "l.surname = r.surname"}
    if control:
        rule.update(control)
    return {
        "tracks": {
            "person": {"blocking_rules": [rule], "comparisons": [],
                       "em_blocking_rules": [], "max_pairs": 10_000_000},
            "organisation": {"blocking_rules": [], "comparisons": [],
                             "em_blocking_rules": [], "max_pairs": 10_000_000},
        }
    }


def test_the_budget_prices_a_rule_after_its_hot_key_control():
    """A budget that prices the uncontrolled rule is not a budget.

    Forty units on one surname make 780 pairs. Refining the oversized block on
    the forename initial leaves two blocks of twenty, which make 380.
    """
    units = _hot_units()
    api = _db_api()

    plain, _ = blocking_budget_report(units, _hot_settings(None), api)
    assert plain["tracks"]["person"]["rules"][0]["pairs"] == 780

    controlled, _ = blocking_budget_report(units, _hot_settings({
        "max_block_size": 30, "on_oversize": "refine",
        "refine_with": ["forename_initial"],
    }), api)
    entry = controlled["tracks"]["person"]["rules"][0]
    assert entry["pairs"] == 380
    assert entry["pairs_before_control"] == 780
    assert entry["control"]["max_block_size"] == 30
    assert "forename initial" in entry["control_description"].lower()
    # The rule's own SQL is reported unchanged beside what was really run, so a
    # reader can see both.
    assert entry["sql"] == "l.surname = r.surname"
    assert entry["sql_after_control"] != entry["sql"]


def test_a_rule_with_no_control_is_handed_back_untouched():
    """The donations no-op. Nothing in the generated SQL path may run for a rule
    that does not ask for it."""
    rules = [{"id": "b1", "description": "", "sql": "l.surname = r.surname"}]
    assert stage_3.controlled_rules(_hot_units(), rules, "person") is rules


def test_dropping_an_oversized_block_leaves_only_the_rest():
    """Over the drop line the block is not compared at all, and its units are
    reported as too common to compare on this rule."""
    units = _hot_units()
    report, _ = blocking_budget_report(units, _hot_settings({
        "max_block_size": 30, "on_oversize": "drop",
    }), _db_api())
    entry = report["tracks"]["person"]["rules"][0]
    assert entry["pairs"] == 0
    assert entry["pairs_before_control"] == 780

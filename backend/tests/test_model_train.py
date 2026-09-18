# backend/tests/test_model_train.py
"""Training: calibration, grading, thresholds, determinism, and the version store.

The synthetic run below is small but shaped like a real one: pairs of units with
a Splink weight, an import overlay that agrees on most of them, and a handful of
human labels that can be turned into a frozen test set.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.db import init_db
from app.model import store, train
from app.model.train import derive_thresholds, wilson_bounds, wilson_lower
from app.profiles.donations import DonationsProfile
from app.services import pair_labels


# ---------------------------------------------------------------------------
# A run on disk
# ---------------------------------------------------------------------------


def _make_run(run_dir, n=120, seed=0):
    """A run folder with a person track whose imported ids mostly agree."""
    rng = np.random.default_rng(seed)
    rows = []
    units = []
    for i in range(n):
        same = i % 5 != 0
        left, right = f"l{i}", f"r{i}"
        surname = f"SUR{i:04d}"
        forename = "JOHN" if same else "JOHN"
        other = surname if same else f"OTH{i:04d}"
        for unit_id, sn in ((left, surname), (right, other)):
            units.append({
                "unit_id": unit_id, "unit_size": 1, "track": "person",
                "name_clean": f"{forename} {sn}", "forename": forename,
                "forename_canon": forename, "middle_names": None, "surname": sn,
                "title": None, "post_nominals": None,
                "parties": "Labour" if same else "Greens",
                "units": None, "first_year": 2001, "last_year": 2005,
                "n_donations": 3, "median_value": 1000.0, "modal_value": 1000.0,
                "share_round_1000": 1.0, "top_values": "1,000",
                "name_core": None, "postcode_clean": None, "postcode_district": None,
                "company_number_clean": None, "legal_form": None,
                "donor_status_std": None,
            })
        rows.append({
            "unit_id_l": left, "unit_id_r": right, "track": "person",
            "match_probability": 0.9 if same else 0.2,
            "match_weight": float(rng.normal(4 if same else -2, 0.5)),
            "gamma_surname": 3.0 if same else 0.0,
            "score_bucket": "accept" if same else "reject",
            "bucket": "accept" if same else "reject",
            "decided_by": "import" if same else "score",
            "import_disagrees": (not same),
            "held_group_id": None,
        })
    pairs = pd.DataFrame(rows)
    unit_frame = pd.DataFrame(units)
    members = pd.DataFrame({"unit_id": unit_frame["unit_id"],
                            "record_id": unit_frame["unit_id"]})
    run_dir.mkdir(parents=True, exist_ok=True)
    pairs.to_parquet(run_dir / "pairs.parquet", index=False)
    unit_frame.to_parquet(run_dir / "units.parquet", index=False)
    members.to_parquet(run_dir / "unit_members.parquet", index=False)
    (run_dir / "config").mkdir(exist_ok=True)
    (run_dir / "config" / "linkage_settings.json").write_text(json.dumps({}))
    return pairs, unit_frame


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    run_dir = tmp_path / "runs" / "run_test"
    _make_run(run_dir)
    db = str(tmp_path / "test.db")
    init_db(db)
    return {"run_dir": run_dir, "db": db, "data_dir": tmp_path}


def _label_many(db, pairs, n, held_out=0, start=0):
    """Label *n* pairs with the verdict the import overlay already implies."""
    for i in range(start, start + n):
        row = pairs.iloc[i]
        pair_labels.save_label(
            db, row["unit_id_l"], row["unit_id_r"],
            "TRUE" if row["decided_by"] == "import" else "FALSE",
            reviewer="test", track="person", held_out=held_out,
        )


# ---------------------------------------------------------------------------
# Wilson bounds, against numbers worked out by hand
# ---------------------------------------------------------------------------


def test_wilson_bounds_against_hand_computed_values():
    # 3 of 3: the textbook 95% Wilson interval is 0.4385 to 1.0.
    lower, upper = wilson_bounds(3, 3)
    assert lower == pytest.approx(0.4385, abs=0.0005)
    assert upper == pytest.approx(1.0, abs=0.0005)
    # 50 of 100: 0.4038 to 0.5962, symmetric about a half.
    lower, upper = wilson_bounds(50, 100)
    assert lower == pytest.approx(0.4038, abs=0.0005)
    assert upper == pytest.approx(0.5962, abs=0.0005)
    # 99 of 100: 0.9455 to 0.9982. Worked through:
    #   centre = 0.99 + 1.96^2 / 200                       = 1.009208
    #   spread = 1.96 * sqrt(0.99*0.01/100 + 1.96^2/40000) = 0.027369
    #   denominator = 1 + 1.96^2 / 100                     = 1.038416
    lower, upper = wilson_bounds(99, 100)
    assert lower == pytest.approx((1.009208 - 0.027369) / 1.038416, abs=0.0005)
    assert lower == pytest.approx(0.9455, abs=0.0005)
    assert upper == pytest.approx(0.9982, abs=0.0005)


def test_wilson_is_honest_about_small_samples():
    # A perfect score on four rows promises far less than a perfect score on 400.
    assert wilson_lower(4, 4) < wilson_lower(400, 400)
    assert wilson_lower(0, 0) == 0.0


def test_a_perfect_small_sample_cannot_reach_a_high_target():
    # 3 out of 3 matches has a lower bound of 0.44, so it must not set a 0.99 line.
    scores = np.array([0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    y = np.array([1, 1, 1, 0, 0, 0])
    lines = derive_thresholds(scores, y, 0.99)
    assert lines["accept"] is None


def test_a_big_clean_sample_sets_both_lines():
    scores = np.concatenate([np.full(400, 0.95), np.full(400, 0.05)])
    y = np.concatenate([np.ones(400, dtype=int), np.zeros(400, dtype=int)])
    lines = derive_thresholds(scores, y, 0.99)
    assert lines["accept"] == pytest.approx(0.95)
    assert lines["reject"] == pytest.approx(0.95)  # everything below 0.95 is a non-match
    assert lines["accept_metrics"]["precision"] == 1.0
    assert lines["accept_metrics"]["precision_wilson_lower"] > 0.99


def test_the_threshold_grid_walks_in_steps_of_a_twentieth():
    scores = np.array([0.1, 0.4, 0.6, 0.9])
    y = np.array([0, 0, 1, 1])
    grid = train.threshold_grid(scores, y)
    assert len(grid) == 21
    assert grid[0]["threshold"] == 0.0
    assert grid[-1]["threshold"] == 1.0
    row = [r for r in grid if r["threshold"] == 0.5][0]
    assert row["n_predicted_positive"] == 2
    assert row["tp"] == 2
    assert row["precision"] == 1.0
    assert row["recall"] == 1.0
    assert row["precision_wilson_lower"] == pytest.approx(wilson_lower(2, 2))


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


def test_a_run_with_no_human_labels_is_a_cold_start(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    assert summary["cold_start"] is True
    assert summary["graded"] is False
    assert summary["accept"] is None and summary["reject"] is None
    report = store.load_report("person", summary["version"])
    assert report["calibration"]["fitted_on"] == "imported"
    codes = {w["code"] for w in report["warnings"]}
    assert "cold_start" in codes and "no_test_set" in codes


def test_a_cold_start_model_may_not_auto_accept(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    store.set_active("person", summary["version"])
    assert store.can_auto_accept("person") is False


def test_the_known_limit_is_in_every_report(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    report = store.load_report("person", summary["version"])
    assert "imported labels were made mostly on the name" in report["known_limit"]


def test_calibration_is_fitted_on_human_rows_only_once_there_are_enough(run_env):
    pairs = pd.read_parquet(run_env["run_dir"] / "pairs.parquet")
    _label_many(run_env["db"], pairs, 60)
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    report = store.load_report("person", summary["version"])
    assert report["cold_start"] is False
    assert report["calibration"]["fitted_on"] == "human"
    assert report["calibration"]["n"] <= 60
    assert report["labels"]["human_total"] == 60


def test_the_frozen_test_set_grades_the_model_and_sets_the_lines(run_env):
    pairs = pd.read_parquet(run_env["run_dir"] / "pairs.parquet")
    _label_many(run_env["db"], pairs, 55, held_out=0)
    _label_many(run_env["db"], pairs, 60, held_out=1, start=60)
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    report = store.load_report("person", summary["version"])
    assert report["graded"] is True
    assert report["auc"]["source"] == "held_out"
    assert report["thresholds"]["available"] is True
    assert report["thresholds"]["n_test"] == 60
    assert len(report["thresholds"]["grid"]) == 21


def test_a_report_carries_every_section_the_panel_reads(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    report = store.load_report("person", summary["version"])
    for key in ("labels", "folds", "auc", "average_precision", "brier",
                "calibration", "thresholds", "importance", "references",
                "warnings", "timings", "features", "known_limit", "graded",
                "cold_start"):
        assert key in report, key
    assert {row["source"] for row in report["labels"]["by_source"]} == {
        "human", "decision", "import_agree", "import_disagree"}
    assert report["importance"]["gain"] and report["importance"]["shap"]
    assert report["importance"]["ablation"]["rows"]
    assert report["calibration"]["points"]


def test_the_ablation_covers_every_group_once(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    report = store.load_report("person", summary["version"])
    groups = [row["group"] for row in report["importance"]["ablation"]["rows"]]
    assert len(groups) == len(set(groups))
    assert {f["group"] for f in report["features"]} == set(groups)


def test_the_report_says_when_a_reference_table_is_missing(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    report = store.load_report("person", summary["version"])
    names = {r["name"]: r for r in report["references"]}
    assert names["uk_name_frequencies"]["present"] is False
    assert names["uk_name_frequencies"]["rows"] is None
    assert names["uk_name_frequencies"]["affects"] == [
        "surname_log_frequency", "full_name_log_frequency"]
    assert "missing_reference" in {w["code"] for w in report["warnings"]}
    # `references` is the only place that says a table is missing — there is no
    # second list beside it to keep in step.
    assert "reference_tables_missing" not in summary
    rarity = [f for f in report["features"] if f["group"] == "rarity"]
    assert len(rarity) == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_gives_the_same_model(run_env):
    first = train.train(run_env["run_dir"], run_env["db"], "person", seed=7,
                        profile=DonationsProfile())
    second = train.train(run_env["run_dir"], run_env["db"], "person", seed=7,
                         profile=DonationsProfile())
    assert first["version"] == 1 and second["version"] == 2
    assert first["auc"] == second["auc"]
    left = (store.version_dir("person", 1) / "model.txt").read_text()
    right = (store.version_dir("person", 2) / "model.txt").read_text()
    assert left == right
    assert store.load_calibration("person", 1) == store.load_calibration("person", 2)


def test_training_refuses_a_run_with_no_scored_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    empty = tmp_path / "runs" / "empty"
    empty.mkdir(parents=True)
    with pytest.raises(train.TrainingError, match="no scored pairs"):
        train.train(empty, None, "person", profile=DonationsProfile())


def test_training_refuses_when_nothing_is_labelled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    run_dir = tmp_path / "runs" / "plain"
    run_dir.mkdir(parents=True)
    pairs = pd.DataFrame({
        "unit_id_l": ["a"], "unit_id_r": ["b"], "track": ["person"],
        "match_weight": [1.0], "decided_by": ["score"], "import_disagrees": [False],
    })
    pairs.to_parquet(run_dir / "pairs.parquet", index=False)
    pd.DataFrame({"unit_id": ["a", "b"], "unit_size": [1, 1]}).to_parquet(
        run_dir / "units.parquet", index=False)
    with pytest.raises(train.TrainingError, match="Nothing to train on"):
        train.train(run_dir, None, "person", profile=DonationsProfile())


# ---------------------------------------------------------------------------
# The version store
# ---------------------------------------------------------------------------


def test_versions_are_numbered_from_one_and_never_overwritten(run_env):
    first = train.train(run_env["run_dir"], run_env["db"], "person",
                        profile=DonationsProfile())
    booster_before = (store.version_dir("person", 1) / "model.txt").read_bytes()
    second = train.train(run_env["run_dir"], run_env["db"], "person", note="again",
                         profile=DonationsProfile())
    assert (first["version"], second["version"]) == (1, 2)
    assert (store.version_dir("person", 1) / "model.txt").read_bytes() == booster_before
    assert store.list_versions("person") == [1, 2]
    assert store.summary("person", 2)["note"] == "again"


def test_a_version_folder_holds_all_five_files(run_env):
    summary = train.train(run_env["run_dir"], run_env["db"], "person",
                          profile=DonationsProfile())
    folder = store.version_dir("person", summary["version"])
    for name in ("model.txt", "calibration.json", "features.json", "report.json",
                 "meta.json"):
        assert (folder / name).exists(), name


def test_saving_into_an_existing_version_is_refused(run_env, monkeypatch):
    train.train(run_env["run_dir"], run_env["db"], "person", profile=DonationsProfile())
    monkeypatch.setattr(store, "next_version", lambda track: 1)
    booster = store.load_booster("person", 1)
    with pytest.raises(store.ModelStoreError, match="already exists"):
        store.save("person", booster, {}, [], {}, {})


def test_activate_and_deactivate(run_env):
    train.train(run_env["run_dir"], run_env["db"], "person", profile=DonationsProfile())
    train.train(run_env["run_dir"], run_env["db"], "person", profile=DonationsProfile())
    assert store.get_active("person") is None
    store.set_active("person", 1)
    assert store.get_active("person") == 1
    assert store.summary("person", 1)["active"] is True
    assert store.summary("person", 2)["active"] is False
    store.set_active("person", 2)
    assert store.get_active("person") == 2
    assert store.clear_active("person") == 2
    assert store.get_active("person") is None
    assert store.clear_active("person") is None


def test_activating_a_version_that_does_not_exist_is_refused(run_env):
    with pytest.raises(store.ModelStoreError):
        store.set_active("person", 9)


def test_the_two_tracks_keep_separate_versions_and_separate_active_models(run_env):
    train.train(run_env["run_dir"], run_env["db"], "person", profile=DonationsProfile())
    store.set_active("person", 1)
    assert store.list_versions("organisation") == []
    assert store.get_active("organisation") is None


def test_calibration_is_applied_by_lookup(run_env):
    calibration = {"x_grid": [0.0, 1.0], "y_grid": [0.2, 0.8]}
    out = store.apply_calibration([0.0, 0.5, 1.0], calibration)
    assert out.tolist() == pytest.approx([0.2, 0.5, 0.8])
    # No calibration, or an unusable one, is the identity.
    assert store.apply_calibration([0.4], None).tolist() == [0.4]
    assert store.apply_calibration([0.4], {"x_grid": [1.0], "y_grid": [1.0]}) \
        .tolist() == [0.4]

# backend/tests/test_model_apply.py
"""Stage 3b: applying a model to a run, reverting it, and the pairs it changes.

The run under test is the synthetic one from `test_model_train`, extended with
the files `score_eval` needs, so a real apply can be run end to end without
Splink.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import init_db
from app.model import store, train
from app.pipeline.dedupe import stage_3b_model
from app.pipeline.dedupe.stage_3_score import apply_overlays, finalise_pairs
from app.profiles.donations import DonationsProfile
from app.services import model_apply, pairs_reader
from tests.test_model_train import _make_run


def _complete_run(run_dir):
    """Add the records, groups and overlay columns a real stage 3 would leave."""
    pairs, units = _make_run(run_dir)
    records = pd.DataFrame({
        "record_id": units["unit_id"].to_numpy(),
        "name": units["name_clean"].to_numpy(),
        "track": "person",
        "review_state": "unreviewed",
        "existing_entity_id": None,
    })
    groups = pd.DataFrame({"record_id": [], "group_id": [], "track": [],
                           "status": []}, dtype="object")
    members = pd.DataFrame({"unit_id": units["unit_id"], "record_id": units["unit_id"]})
    # The imported entity ids the overlay reads. `_make_run` pairs l<i> with
    # r<i>, and every fifth pair is a non-match, so the ids agree on four in five
    # and disagree on the rest — which is what gives the model something to learn.
    number = units["unit_id"].str.slice(1).astype(int)
    same = (number % 5 != 0)
    side = units["unit_id"].str.slice(0, 1)
    existing = np.where(same | (side == "l"), "E" + number.astype(str),
                        "X" + number.astype(str))
    units = units.assign(existing_entity_id=existing, existing_entity_ids=existing,
                         n_existing_ids=1, held_group_id=None,
                         name=units["name_clean"], total_value=1000.0)
    overlaid = finalise_pairs(apply_overlays(pairs, units, 0.5, 0.92), units)

    records.to_parquet(run_dir / "records.parquet", index=False)
    groups.to_parquet(run_dir / "exact_groups.parquet", index=False)
    members.to_parquet(run_dir / "unit_members.parquet", index=False)
    units.to_parquet(run_dir / "units.parquet", index=False)
    overlaid.to_parquet(run_dir / "pairs.parquet", index=False)
    (run_dir / "config" / "linkage_settings.json").write_text(json.dumps({
        "match_probability_threshold_candidate": 0.05,
        "match_probability_threshold_review": 0.5,
        "match_probability_threshold_high": 0.92,
    }))
    return overlaid, units


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_main_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_main_mod, "DB_PATH", str(tmp_path / "test.db"))
    run_dir = tmp_path / "runs" / "run_test"
    _complete_run(run_dir)
    db = str(tmp_path / "test.db")
    init_db(db)
    # The run endpoints look the run up before they touch its folder.
    from app.db import write_db

    write_db(db, "INSERT INTO runs (id, status) VALUES (?, ?)",
             ("run_test", "complete"))
    return {"dir": run_dir, "data": tmp_path, "db": db}


def _train_and_activate(run, graded=False):
    summary = train.train(run["dir"], run["db"], "person", profile=DonationsProfile())
    if graded:
        # Grading needs a frozen test set the synthetic run has no labels for, so
        # the lines are written straight into the version's own meta. The rest of
        # the file is untouched, which is what a real graded version looks like.
        meta_path = store.version_dir("person", summary["version"]) / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta.update({"graded": True, "cold_start": False, "accept": 0.6,
                     "reject": 0.2})
        meta_path.write_text(json.dumps(meta))
    store.set_active("person", summary["version"])
    return summary["version"]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_applying_a_cold_start_model_scores_but_decides_nothing(run):
    version = _train_and_activate(run, graded=False)
    before = pd.read_parquet(run["dir"] / "pairs.parquet")
    result = model_apply.apply_model(run["dir"])

    after = pd.read_parquet(run["dir"] / "pairs.parquet")
    assert "gbt_score" in after.columns
    assert after["gbt_score"].notna().all()
    # The buckets and who decided them have not moved.
    assert after["bucket"].tolist() == before["bucket"].tolist()
    assert set(after["decided_by"]) <= {"score", "import"}
    assert result["tracks"][0] == {"track": "person", "version": version,
                                   "graded": False, "applied": True,
                                   "decided_by_model": 0, "warning": None}
    assert result["counts"]["model_active"] is True
    assert result["counts"]["model_graded"] is False
    assert result["counts"]["model_version"] == {"person": version}


def test_a_graded_model_takes_over_the_buckets(run):
    version = _train_and_activate(run, graded=True)
    result = model_apply.apply_model(run["dir"], force=True)
    after = pd.read_parquet(run["dir"] / "pairs.parquet")

    assert (after["decided_by"] == "model").any()
    assert result["counts"]["model_graded"] is True
    # Every pair the model decided is bucketed on its own score, not Splink's.
    decided = after[after["decided_by"] == "model"]
    assert (decided.loc[decided["bucket"] == "accept", "gbt_score"] >= 0.6).all()
    assert (decided.loc[decided["bucket"] == "reject", "gbt_score"] < 0.2).all()
    # Splink's own number is still on every row, untouched.
    assert after["match_probability"].notna().all()


def test_the_import_overlay_still_wins_over_the_model(run):
    _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    after = pd.read_parquet(run["dir"] / "pairs.parquet")
    imported = after[after["decided_by"] == "import"]
    assert len(imported) == 0 or (imported["bucket"] == "accept").all()


def test_reverting_puts_the_run_back_exactly(run):
    before = pd.read_parquet(run["dir"] / "pairs.parquet")
    _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    result = model_apply.revert_model(run["dir"])

    after = pd.read_parquet(run["dir"] / "pairs.parquet")
    assert "gbt_score" not in after.columns
    assert after["bucket"].tolist() == before["bucket"].tolist()
    assert after["decided_by"].tolist() == before["decided_by"].tolist()
    assert result["counts"]["model_active"] is False
    assert not (run["dir"] / stage_3b_model.MODEL_STATE_FILENAME).exists()


def test_reverting_a_run_that_was_never_applied_is_fine(run):
    result = model_apply.revert_model(run["dir"])
    assert result["counts"]["model_active"] is False


def test_applying_with_no_active_model_is_refused(run):
    with pytest.raises(model_apply.ModelApplyError, match="No active model"):
        model_apply.apply_model(run["dir"])


# ---------------------------------------------------------------------------
# The collapse guard
# ---------------------------------------------------------------------------


def test_the_guard_catches_a_near_two_valued_score():
    assert stage_3b_model.collapse_reason(100, 100, 2) is not None
    assert "two-valued" in stage_3b_model.collapse_reason(0, 0, 2)
    assert stage_3b_model.collapse_reason(100, 100, 400) is None
    # Nothing scored yet is not a collapse.
    assert stage_3b_model.collapse_reason(100, 100, 0) is None


def test_the_guard_catches_a_collapsed_review_band():
    assert stage_3b_model.collapse_reason(200, 0, 500) is not None
    assert "review band" in stage_3b_model.collapse_reason(200, 1, 500)
    # A band that merely shrinks is not a collapse.
    assert stage_3b_model.collapse_reason(200, 150, 500) is None


def test_a_refused_apply_leaves_the_run_exactly_as_it_was(run, monkeypatch):
    before = pd.read_parquet(run["dir"] / "pairs.parquet")
    _train_and_activate(run, graded=True)
    monkeypatch.setattr(stage_3b_model, "collapse_reason",
                        lambda *a, **k: "made-up collapse")
    with pytest.raises(model_apply.ModelApplyError, match="made-up collapse"):
        model_apply.apply_model(run["dir"])

    after = pd.read_parquet(run["dir"] / "pairs.parquet")
    assert "gbt_score" not in after.columns
    assert after["bucket"].tolist() == before["bucket"].tolist()
    assert stage_3b_model.read_state(run["dir"])["applied"] is False


def test_force_overrides_the_guard(run, monkeypatch):
    _train_and_activate(run, graded=True)
    monkeypatch.setattr(stage_3b_model, "collapse_reason",
                        lambda *a, **k: "made-up collapse")
    result = model_apply.apply_model(run["dir"], force=True)
    assert result["counts"]["model_warning"] == "made-up collapse"
    assert "gbt_score" in pd.read_parquet(run["dir"] / "pairs.parquet").columns


# ---------------------------------------------------------------------------
# score_eval
# ---------------------------------------------------------------------------


def test_score_eval_carries_both_decisions_so_they_can_be_compared(run):
    _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    evaluation = json.loads((run["dir"] / "score_eval.json").read_text())

    assert evaluation["splink_only"] is not None
    assert evaluation["model_only"] is not None
    for key in ("entities_after", "pair_precision", "pair_recall", "accepted_pairs"):
        assert key in evaluation["splink_only"], key
        assert key in evaluation["model_only"], key


def test_a_run_with_no_model_has_no_model_figures(run):
    model_apply.revert_model(run["dir"])
    evaluation = json.loads((run["dir"] / "score_eval.json").read_text())
    assert evaluation["model_only"] is None
    assert evaluation["splink_only"] is not None


# ---------------------------------------------------------------------------
# The pairs API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(run, monkeypatch):
    monkeypatch.setattr(
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom",
                                     "initials": "T"},
    )
    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "x", "user": "x"})


def test_pairs_carry_gbt_score_null_before_a_model_and_a_number_after(run, client):
    body = client.get("/api/runs/run_test/pairs?limit=3").json()
    assert all(item["gbt_score"] is None for item in body["items"])

    _train_and_activate(run)
    model_apply.apply_model(run["dir"])
    body = client.get("/api/runs/run_test/pairs?limit=3").json()
    assert all(0.0 <= item["gbt_score"] <= 1.0 for item in body["items"])
    assert all(item["match_probability"] is not None for item in body["items"])


def test_sort_useful_works_before_any_model_exists(run, client):
    body = client.get("/api/runs/run_test/pairs?sort=useful&limit=5").json()
    scores = [item["usefulness"]["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)
    # With no model the disagreement term is zero and uncertainty is Splink's.
    assert all(item["usefulness"]["disagreement"] == 0 for item in body["items"])


def test_sort_useful_puts_the_uncertain_and_the_disagreeing_first(run, client):
    _train_and_activate(run)
    model_apply.apply_model(run["dir"])
    body = client.get("/api/runs/run_test/pairs?sort=useful&limit=200").json()
    items = body["items"]
    scores = [item["usefulness"]["score"] for item in items]
    assert scores == sorted(scores, reverse=True)
    top, bottom = items[0], items[-1]
    assert (top["usefulness"]["uncertainty"] + top["usefulness"]["disagreement"]) \
        >= (bottom["usefulness"]["uncertainty"] + bottom["usefulness"]["disagreement"])
    for item in items:
        parts = item["usefulness"]
        expected = ((0.5 * parts["uncertainty"] + 0.5 * parts["disagreement"])
                    * (0.5 + 0.5 * parts["weight"]))
        assert parts["score"] == pytest.approx(expected)
        assert 0.0 <= parts["weight"] <= 1.0


def test_desc_is_most_useful_first_and_is_the_default(run, client):
    default = client.get("/api/runs/run_test/pairs?sort=useful&limit=5").json()
    descending = client.get(
        "/api/runs/run_test/pairs?sort=useful&order=desc&limit=5").json()
    ascending = client.get(
        "/api/runs/run_test/pairs?sort=useful&order=asc&limit=5").json()
    assert [i["pair_id"] for i in default["items"]] == \
        [i["pair_id"] for i in descending["items"]]
    assert descending["items"][0]["usefulness"]["score"] \
        >= ascending["items"][0]["usefulness"]["score"]


def test_usefulness_is_absent_on_the_other_sorts(run, client):
    body = client.get("/api/runs/run_test/pairs?sort=score&limit=2").json()
    assert all("usefulness" not in item for item in body["items"])


def test_a_bad_sort_is_four_hundred(run, client):
    response = client.get("/api/runs/run_test/pairs?sort=nonsense")
    assert response.status_code == 400
    assert "useful" in response.json()["detail"]


def test_the_gbt_filters_narrow_the_list(run, client):
    _train_and_activate(run)
    model_apply.apply_model(run["dir"])
    everything = client.get("/api/runs/run_test/pairs?limit=1").json()["total"]
    narrowed = client.get("/api/runs/run_test/pairs?min_gbt=0.9&limit=1").json()
    assert 0 < narrowed["total"] < everything


def test_the_histogram_gains_the_model_dial(run, client):
    body = client.get("/api/runs/run_test/pairs/histogram").json()
    assert body["score_column"] == "match_probability"
    assert body["by_score_column"] is None
    assert body["modelActive"] is False

    version = _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    body = client.get("/api/runs/run_test/pairs/histogram").json()
    assert body["score_column"] == "gbt_score"
    # Both series have the same shape: one number per bin, edges shared.
    assert len(body["edges"]) == body["bins"] + 1
    for key in ("total", "accept", "review", "reject", "agrees", "disagrees",
                "unknown"):
        assert len(body[key]) == body["bins"]
        assert len(body["by_score_column"][key]) == body["bins"]
    assert sum(body["by_score_column"]["total"]) > 0
    # The Splink series is still there, so both dials can be drawn.
    assert sum(body["total"]) > 0
    # And the lines the pairs were bucketed on travel with it.
    assert body["modelActive"] is True
    assert body["modelGraded"] is True
    assert body["modelVersion"] == {"person": version}
    assert body["modelAcceptLine"] == {"person": 0.6}
    assert body["modelRejectLine"] == {"person": 0.2}


def test_the_histogram_lines_do_not_move_when_a_newer_version_is_activated(run, client):
    _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    before = client.get("/api/runs/run_test/pairs/histogram").json()

    # Train and activate a second version with different lines. The finished run
    # must keep saying what it was actually decided by.
    second = _train_and_activate(run, graded=True)
    meta_path = store.version_dir("person", second) / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update({"accept": 0.99, "reject": 0.01})
    meta_path.write_text(json.dumps(meta))

    after = client.get("/api/runs/run_test/pairs/histogram").json()
    assert after["modelVersion"] == before["modelVersion"]
    assert after["modelAcceptLine"] == {"person": 0.6}


def test_the_histogram_narrows_the_model_keys_to_the_track_asked_for(run, client):
    version = _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    body = client.get("/api/runs/run_test/pairs/histogram?track=person").json()
    assert body["modelVersion"] == {"person": version}
    body = client.get("/api/runs/run_test/pairs/histogram?track=organisation").json()
    assert body["modelVersion"] == {}


def test_pair_detail_explains_the_model_score(run, client):
    _train_and_activate(run)
    model_apply.apply_model(run["dir"])
    listing = client.get("/api/runs/run_test/pairs?limit=1").json()
    pair_id = listing["items"][0]["pair_id"].replace("|", "%7C")
    body = client.get(f"/api/runs/run_test/pairs/{pair_id}").json()

    explanation = body["model_explanation"]
    assert explanation["track"] == "person"
    assert explanation["graded"] is False
    assert "imported labels were made mostly on the name" in explanation["known_limit"]
    assert explanation["contributions"]
    first = explanation["contributions"][0]
    assert set(first) == {"name", "label", "group", "value", "value_label",
                          "contribution"}
    # EVERY row carries a plain rendering of its value: a bare number beside a
    # bar tells a reviewer nothing.
    from app.profiles.donations_features import STATUS_KINDS

    for row in explanation["contributions"]:
        assert isinstance(row["value_label"], str) and row["value_label"]
        if row["name"] == "status_kind":
            assert row["label"] == "Kind of donor"
            assert row["value_label"] in STATUS_KINDS
        if row["name"] == "name_jaro_winkler":
            assert row["value_label"] == f"{row['value']:.2f}"
        if row["name"] == "forename_exact":
            assert row["value_label"] in ("yes", "no")
        if row["name"] == "match_weight":
            assert row["value_label"].endswith(" bits")
    # The contributions and the base sum to the raw margin in log-odds.
    total = explanation["base"] + sum(c["contribution"]
                                      for c in explanation["contributions"])
    assert 1 / (1 + np.exp(-total)) == pytest.approx(explanation["raw"], abs=1e-6)
    # Biggest push first, whichever way it pushes.
    sizes = [abs(c["contribution"]) for c in explanation["contributions"]]
    assert sizes == sorted(sizes, reverse=True)
    assert body["gbt_score"] == pytest.approx(explanation["score"], abs=1e-6)


def test_a_version_trained_before_render_existed_still_reads_in_words(run, client):
    """`render` is presentation, not model semantics, so an older version borrows
    the profile's current answer rather than printing bare numbers."""
    version = _train_and_activate(run)
    model_apply.apply_model(run["dir"])
    listing = client.get("/api/runs/run_test/pairs?limit=1").json()
    pair_id = listing["items"][0]["pair_id"].replace("|", "%7C")

    def _labels():
        rows = client.get(f"/api/runs/run_test/pairs/{pair_id}").json()[
            "model_explanation"]["contributions"]
        return {r["name"]: r["value_label"] for r in rows}

    intact = _labels()
    assert all(intact.values())

    path = store.version_dir("person", version) / "features.json"
    path.write_text(json.dumps(
        [{k: v for k, v in f.items() if k != "render"}
         for f in json.loads(path.read_text())]
    ))
    assert _labels() == intact


def test_pair_detail_has_no_model_explanation_without_a_model(run, client):
    listing = client.get("/api/runs/run_test/pairs?limit=1").json()
    pair_id = listing["items"][0]["pair_id"].replace("|", "%7C")
    body = client.get(f"/api/runs/run_test/pairs/{pair_id}").json()
    assert body["model_explanation"] is None


# ---------------------------------------------------------------------------
# The run endpoints
# ---------------------------------------------------------------------------


EARLIER_STAGES = {
    "input_rows": 51839, "records_total": 51839, "exact_merged_groups": 1837,
    "exact_entities_after": 22375, "clusters_total": 17547,
    "entities_proposed": 17547, "review_queue": 1268,
}


def test_apply_model_keeps_every_other_stages_counts(run, client):
    """The defect this guards: applying a model wrote stage 3's counts over the
    whole dict, so the run screen lost its Records, Exact groups and Entities
    tabs — it decides they exist from the `has*` flags those counts carry."""
    from app.db import write_db

    write_db(run["db"], "UPDATE runs SET counts_json = ? WHERE id = ?",
             (json.dumps(EARLIER_STAGES), "run_test"))
    before = client.get("/api/runs/run_test").json()["counts"]
    _train_and_activate(run)

    after = client.post("/api/runs/run_test/apply-model", json={}).json()["counts"]
    for key in ("recordsTotal", "exactEntitiesAfter", "clustersTotal",
                "entitiesProposed", "reviewQueue", "inputRows"):
        assert after[key] == before[key], key
    for flag in ("hasRecords", "hasExact", "hasEntities"):
        assert after[flag] is True, flag
    assert after["modelActive"] is True
    # And the stored row says the same as the response.
    assert client.get("/api/runs/run_test").json()["counts"]["recordsTotal"] == 51839

    reverted = client.post("/api/runs/run_test/revert-model").json()["counts"]
    for key in ("recordsTotal", "exactEntitiesAfter", "clustersTotal",
                "entitiesProposed"):
        assert reverted[key] == before[key], key
    assert reverted["modelActive"] is False


def test_a_label_write_keeps_every_other_stages_counts(run, client):
    from app.db import write_db

    write_db(run["db"], "UPDATE runs SET counts_json = ? WHERE id = ?",
             (json.dumps(EARLIER_STAGES), "run_test"))
    pairs = pd.read_parquet(run["dir"] / "pairs.parquet")
    pair_id = f"{pairs.iloc[0]['unit_id_l']}|{pairs.iloc[0]['unit_id_r']}"
    body = client.post("/api/runs/run_test/labels", json={
        "labels": [{"pair_id": pair_id, "is_match": "TRUE"}]}).json()
    counts = body.get("counts") or client.get("/api/runs/run_test").json()["counts"]
    assert counts["recordsTotal"] == 51839
    assert counts["clustersTotal"] == 17547
    assert counts["hasEntities"] is True


def test_a_cold_start_apply_does_not_recluster(run, client):
    """Nothing moved, so stages 4 and 5 are not worth minutes of anyone's time."""
    _train_and_activate(run)
    body = client.post("/api/runs/run_test/apply-model", json={}).json()
    assert body["reclustered"] is False
    assert all(t["decided_by_model"] == 0 for t in body["tracks"])


def test_a_graded_apply_reclusters_so_the_entities_match_the_buckets(run, client):
    _train_and_activate(run, graded=True)
    body = client.post("/api/runs/run_test/apply-model",
                       json={"force": True}).json()
    assert body["reclustered"] is True
    assert body["counts"]["clustersTotal"] > 0
    assert body["counts"]["hasEntities"] is True
    # Reverting moves them back, so it reclusters too.
    reverted = client.post("/api/runs/run_test/revert-model").json()
    assert reverted["reclustered"] is True


def test_apply_and_revert_over_http(run, client):
    version = _train_and_activate(run, graded=True)

    body = client.post("/api/runs/run_test/apply-model",
                       json={"force": True}).json()
    assert body["ok"] is True
    assert body["tracks"][0]["version"] == version
    assert body["counts"]["modelActive"] is True
    assert body["counts"]["modelVersion"] == {"person": version}
    assert body["counts"]["modelAcceptLine"] == {"person": 0.6}
    assert body["counts"]["modelRejectLine"] == {"person": 0.2}
    assert body["counts"]["modelGraded"] is True

    body = client.post("/api/runs/run_test/revert-model").json()
    assert body["counts"]["modelActive"] is False
    assert body["counts"]["modelVersion"] == {}
    assert body["counts"]["modelAcceptLine"] == {}
    assert body["counts"]["modelRejectLine"] == {}


def test_a_cold_start_run_records_no_lines(run, client):
    version = _train_and_activate(run)
    body = client.post("/api/runs/run_test/apply-model", json={}).json()
    assert body["counts"]["modelVersion"] == {"person": version}
    assert body["counts"]["modelAcceptLine"] == {"person": None}
    assert body["counts"]["modelGraded"] is False
    assert body["counts"]["modelWarning"] is None


def test_apply_with_no_active_model_is_four_hundred(run, client):
    response = client.post("/api/runs/run_test/apply-model", json={})
    assert response.status_code == 400
    assert "No active model" in response.json()["detail"]


def test_a_collapse_is_four_oh_nine(run, client, monkeypatch):
    _train_and_activate(run, graded=True)
    monkeypatch.setattr(stage_3b_model, "collapse_reason",
                        lambda *a, **k: "made-up collapse")
    response = client.post("/api/runs/run_test/apply-model", json={})
    assert response.status_code == 409
    assert "made-up collapse" in response.json()["detail"]
    assert "gbt_score" not in pd.read_parquet(run["dir"] / "pairs.parquet").columns

    forced = client.post("/api/runs/run_test/apply-model", json={"force": True})
    assert forced.status_code == 200


def test_applying_to_an_unknown_run_is_four_oh_four(run, client):
    assert client.post("/api/runs/nope/apply-model", json={}).status_code == 404


# ---------------------------------------------------------------------------
# decided_by
# ---------------------------------------------------------------------------


def test_decided_by_model_is_a_filter(run, client):
    _train_and_activate(run, graded=True)
    model_apply.apply_model(run["dir"], force=True)
    body = client.get("/api/runs/run_test/pairs?decided_by=model&limit=5").json()
    assert body["total"] > 0
    assert all(item["decided_by"] == "model" for item in body["items"])
    assert "model" in pairs_reader.DECIDED_BY

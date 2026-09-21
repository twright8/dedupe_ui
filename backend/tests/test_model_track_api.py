# backend/tests/test_model_track_api.py
"""The per-track model API, against `docs/MODEL_API.md`.

Every response shape the model panel reads is checked here, so a rename in the
backend breaks a test rather than a screen.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.auth as _auth_mod
import app.main as _main_mod
from app.model import jobs, store, train
from app.profiles.donations import DonationsProfile
from tests.test_model_train import _make_run


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(_main_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(_main_mod, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom", "initials": "T"},
    )
    from app.db import init_db

    init_db(str(tmp_path / "test.db"))
    _make_run(tmp_path / "runs" / "run_test")
    jobs.reset()
    from fastapi.testclient import TestClient

    client = TestClient(_main_mod.app, cookies={"session": "x", "user": "x"})
    return {"client": client, "dir": tmp_path}


def _train(env, note=None):
    return train.train(env["dir"] / "runs" / "run_test",
                       str(env["dir"] / "test.db"), "person", note=note,
                       profile=DonationsProfile())


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_a_track_with_no_model_answers_two_hundred(env):
    body = env["client"].get("/api/model/person").json()
    assert body["track"] == "person"
    assert body["label"] == "People"
    assert body["active_version"] is None
    assert body["latest_version"] is None
    assert body["active"] is None
    assert body["versions"] == []
    assert body["can_auto_accept"] is False
    assert body["training"] is None


def test_an_unknown_track_is_four_hundred(env):
    response = env["client"].get("/api/model/badgers")
    assert response.status_code == 400
    assert "person or organisation" in response.json()["detail"]


def test_status_lists_versions_newest_first(env):
    _train(env, note="one")
    _train(env, note="two")
    body = env["client"].get("/api/model/person").json()
    assert [v["version"] for v in body["versions"]] == [2, 1]
    assert body["latest_version"] == 2
    assert body["active_version"] is None
    assert "report" not in body["versions"][0]
    for key in ("trained_at", "graded", "auc", "n_train_rows", "n_human_labels",
                "accept", "reject", "note", "active"):
        assert key in body["versions"][0], key


def test_one_version_carries_the_whole_report(env):
    _train(env)
    body = env["client"].get("/api/model/person/versions/1").json()
    assert body["version"] == 1
    assert body["report"]["known_limit"]
    assert body["features"]
    assert body["report"]["importance"]["ablation"]["rows"]


def test_a_version_that_does_not_exist_is_four_oh_four(env):
    assert env["client"].get("/api/model/person/versions/7").status_code == 404


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def test_features_answer_before_anything_is_trained(env):
    body = env["client"].get("/api/model/person/features").json()
    assert body["source"] == "next_train"
    assert body["n_features"] == len(body["features"])
    names = {f["name"] for f in body["features"]}
    assert "name_jaro_winkler" in names
    assert "surname_log_frequency" in names
    assert "match_weight" in names
    first = body["features"][0]
    assert set(first) == {"name", "label", "group", "monotone", "source",
                          "null_when", "categories", "render"}
    # An ordinary number carries no category list; the kind of donor does.
    assert first["categories"] is None
    kind = [f for f in body["features"] if f["name"] == "status_kind"][0]
    assert kind["categories"][0] == "kinds differ"
    assert kind["monotone"] == 0
    assert kind["render"] == "category"
    # Every feature says how to put its value into words.
    from app.model.features import RENDERINGS

    assert all(f["render"] in RENDERINGS for f in body["features"])
    assert {g["key"] for g in body["groups"]} <= {
        "splink", "name", "rarity", "recipients", "timing", "amounts", "kind",
        "size"}
    reference = body["references"][0]
    assert reference["name"] == "uk_name_frequencies"
    assert set(reference) >= {"name", "present", "rows", "built_at", "affects"}


def test_features_follow_the_active_model_once_there_is_one(env):
    _train(env)
    store.set_active("person", 1)
    body = env["client"].get("/api/model/person/features").json()
    assert body["source"] == "version"
    assert body["version"] == 1
    assert [f["name"] for f in body["features"]] == \
        [f["name"] for f in store.load_features("person", 1)]


def test_the_organisation_track_has_its_own_features(env):
    body = env["client"].get("/api/model/organisation/features").json()
    names = {f["name"] for f in body["features"]}
    assert "company_number_equal" in names
    assert "name_tfidf_cosine" in names
    assert "surname_log_frequency" not in names


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def test_training_starts_a_job_and_finishes(env):
    response = env["client"].post("/api/model/person/train",
                                  json={"run_id": "run_test", "seed": 3})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert response.json()["state"] in ("queued", "running")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = env["client"].get(f"/api/model/person/train/{job_id}").json()
        if job["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["state"] == "done", job.get("error")
    assert job["version"] == 1
    assert job["percent"] == 100
    assert store.list_versions("person") == [1]
    # The panel can restore its progress strip after a reload.
    assert env["client"].get("/api/model/person").json()["training"]["job_id"] == job_id


def test_training_an_unknown_run_is_four_oh_four(env):
    response = env["client"].post("/api/model/person/train", json={"run_id": "nope"})
    assert response.status_code == 404


def test_training_a_run_with_no_pairs_is_four_hundred(env):
    (env["dir"] / "runs" / "bare").mkdir(parents=True)
    response = env["client"].post("/api/model/person/train", json={"run_id": "bare"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Run has no scored pairs yet"


def test_one_job_per_track(env, monkeypatch):
    monkeypatch.setattr(jobs, "running", lambda track: True)
    response = env["client"].post("/api/model/person/train", json={"run_id": "run_test"})
    assert response.status_code == 409


def test_an_unknown_job_is_four_oh_four(env):
    assert env["client"].get("/api/model/person/train/mj_nope").status_code == 404


def test_a_failed_job_carries_its_message(env):
    jobs.start("person", env["dir"] / "runs" / "missing", None, background=False,
               run_id="missing", profile=DonationsProfile())
    job = jobs.latest("person")
    assert job["state"] == "failed"
    assert "no scored pairs" in job["error"]
    body = env["client"].get(f"/api/model/person/train/{job['job_id']}").json()
    assert body["state"] == "failed"
    assert body["version"] is None


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def test_activate_and_deactivate_over_http(env):
    _train(env)
    _train(env)
    body = env["client"].post("/api/model/person/activate", json={"version": 1}).json()
    assert body == {"ok": True, "track": "person", "active_version": 1,
                    "graded": False, "can_auto_accept": False,
                    "warnings": body["warnings"]}
    assert {w["code"] for w in body["warnings"]} >= {"cold_start", "no_test_set"}
    assert env["client"].get("/api/model/person").json()["active"]["version"] == 1

    # No version named activates the newest.
    assert env["client"].post("/api/model/person/activate", json={}).json()[
        "active_version"] == 2

    body = env["client"].post("/api/model/person/deactivate").json()
    assert body == {"ok": True, "track": "person", "active_version": None}
    assert env["client"].get("/api/model/person").json()["active"] is None


def test_deactivating_nothing_is_fine(env):
    body = env["client"].post("/api/model/person/deactivate").json()
    assert body["active_version"] is None


def test_activating_a_version_that_does_not_exist_is_four_hundred(env):
    response = env["client"].post("/api/model/person/activate", json={"version": 9})
    assert response.status_code == 400


def test_a_cold_start_model_is_active_but_decides_nothing(env):
    _train(env)
    env["client"].post("/api/model/person/activate", json={"version": 1})
    body = env["client"].get("/api/model/person").json()
    assert body["active_version"] == 1
    assert body["active"]["graded"] is False
    assert body["can_auto_accept"] is False


# ---------------------------------------------------------------------------
# The frozen test set
# ---------------------------------------------------------------------------


def _label(env, a, b, verdict, provenance="manual"):
    from app.services import pair_labels

    return pair_labels.save_label(
        str(env["dir"] / "test.db"), a, b, verdict, reviewer="Tom",
        provenance=provenance, track="person",
    )


def test_an_empty_test_set_reads_as_empty(env):
    body = env["client"].get("/api/model/person/test-set").json()
    assert body.pop("by_answer") == {"Match": 0, "Not a match": 0}
    assert body == {"track": "person", "total": 0,
                    "by_verdict": {"TRUE": 0, "FALSE": 0},
                    "training": 0, "designatable": 0}


def test_designating_never_takes_more_than_half_of_either_verdict(env):
    for i in range(10):
        _label(env, f"a{i}", f"b{i}", "TRUE")
    for i in range(4):
        _label(env, f"c{i}", f"d{i}", "FALSE")

    body = env["client"].post("/api/model/person/test-set/designate",
                              json={"n": 200}).json()
    # Half of ten trues and half of four falses, never more.
    assert body["designated"] == 5 + 2
    assert body["total"] == 7
    assert body["by_verdict"] == {"TRUE": 5, "FALSE": 2}
    assert body["training"] == 7


def test_designating_is_balanced_and_capped_by_n(env):
    for i in range(20):
        _label(env, f"a{i}", f"b{i}", "TRUE")
        _label(env, f"c{i}", f"d{i}", "FALSE")
    body = env["client"].post("/api/model/person/test-set/designate",
                              json={"n": 6}).json()
    assert body["by_verdict"] == {"TRUE": 3, "FALSE": 3}


def test_designating_is_additive(env):
    for i in range(20):
        _label(env, f"a{i}", f"b{i}", "TRUE")
        _label(env, f"c{i}", f"d{i}", "FALSE")
    first = env["client"].post("/api/model/person/test-set/designate",
                               json={"n": 4}).json()
    second = env["client"].post("/api/model/person/test-set/designate",
                                json={"n": 4}).json()
    assert second["total"] > first["total"]


def test_an_imported_label_is_never_frozen(env):
    _label(env, "a", "b", "TRUE", provenance="import")
    _label(env, "c", "d", "TRUE", provenance="cluster_merge")
    body = env["client"].get("/api/model/person/test-set").json()
    assert body["designatable"] == 0
    assert env["client"].post("/api/model/person/test-set/designate",
                              json={"n": 10}).json()["designated"] == 0


def test_the_test_set_is_per_track(env):
    from app.services import pair_labels

    db = str(env["dir"] / "test.db")
    for i in range(10):
        pair_labels.save_label(db, f"a{i}", f"b{i}", "TRUE", reviewer="Tom",
                               track="organisation")
    env["client"].post("/api/model/person/test-set/designate", json={"n": 10})
    assert env["client"].get("/api/model/person/test-set").json()["total"] == 0
    assert env["client"].get("/api/model/organisation/test-set").json()["designatable"] \
        == 10


def test_the_legacy_model_endpoints_are_gone(env):
    """`routers/model.py` served the two-dataset tool. It is deleted (D21), and
    this router now owns everything under /api/model."""
    assert env["client"].get("/api/model").status_code == 404
    # /api/model/eval-set now reads "eval-set" as a track name, and there is
    # no such track.
    assert env["client"].get("/api/model/eval-set").status_code == 400

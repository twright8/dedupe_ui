# backend/tests/test_model_api.py
"""Model status, eval-set designation, and batch label endpoints."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.db import query_db
import app.main as _main_mod
import app.auth as _auth_mod


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom", "initials": "T"},
    )
    from fastapi.testclient import TestClient
    return TestClient(_main_mod.app, cookies={"session": "x", "user": "x"})


def _label(client, name, roe, verdict):
    return client.post("/api/labels", json={
        "ocod_name_clean": name, "jurisdiction_clean": "ENGLAND",
        "roe_company_number": roe, "is_true_match": verdict,
    })


def test_model_status_shape(client):
    r = client.get("/api/model")
    assert r.status_code == 200
    assert "exists" in r.json()


def test_eval_set_designate_balances_and_freezes(client, db_path):
    for i in range(4):
        _label(client, f"TRUE CO {i}", f"OET{i}", "true")
    for i in range(4):
        _label(client, f"FALSE CO {i}", f"OEF{i}", "false")

    r = client.post("/api/model/eval-set/designate", json={"n": 4})
    assert r.status_code == 200
    designated = r.json()["designated"]
    assert designated >= 2  # at least some TRUE and some FALSE

    status = client.get("/api/model/eval-set").json()
    assert status["total"] == designated

    held = query_db(db_path, "SELECT COUNT(*) AS c FROM labels WHERE held_out = 1 AND active = 1")
    assert held[0]["c"] == designated


# --- Change 3: collapse-guard blind spots ------------------------------------------------

def test_collapse_reason_bimodal_regardless_of_band():
    """Near-bimodal scores are refused regardless of band counts — including a first-ever
    apply where the Splink baseline review band was already 0 (0 -> 0)."""
    from app.routers.model import _collapse_reason

    assert _collapse_reason(0, 0, 2) is not None          # first-ever apply, empty baseline
    assert _collapse_reason(100, 100, 2) is not None       # bimodal even with a full band
    assert _collapse_reason(100, 100, 3) is not None       # <=3 distinct values
    assert _collapse_reason(100, 100, 4) is None           # 4 distinct = not bimodal
    assert _collapse_reason(0, 0, 50) is None              # healthy spread, empty baseline ok


def test_collapse_reason_tiny_remnant_band():
    """A band that shrinks to a tiny remnant (197 -> 1) is refused, not just 197 -> 0."""
    from app.routers.model import _collapse_reason

    assert _collapse_reason(197, 0, 50) is not None        # emptied
    assert _collapse_reason(197, 1, 50) is not None         # tiny remnant, below 5% floor
    assert _collapse_reason(197, 9, 50) is not None         # still below floor (~10)
    assert _collapse_reason(197, 20, 50) is None            # healthy remnant
    assert _collapse_reason(3, 2, 50) is None               # small band, 2 >= floor(1)


def test_distinct_gbt_scores_counts_calibrated(tmp_path):
    import pandas as pd
    from app.routers.model import _distinct_gbt_scores

    rd = tmp_path / "run"
    rd.mkdir()
    # No parquet yet -> 0.
    assert _distinct_gbt_scores(str(rd)) == 0
    # A bimodal score column -> 2 distinct.
    pd.DataFrame({
        "unique_id_l": ["a", "b", "c", "d"],
        "unique_id_r": ["w", "x", "y", "z"],
        "gbt_score": [0.001, 0.999, 0.001, 0.999],
    }).to_parquet(rd / "linkage_scored.parquet")
    assert _distinct_gbt_scores(str(rd)) == 2


def test_model_status_surfaces_threshold_metrics(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.pipeline import gbt_model

    payload = {"available": False, "reason": "held-out eval set is empty"}
    gbt_model.save_metrics({"threshold_metrics": payload})
    r = client.get("/api/model")
    assert r.status_code == 200
    assert r.json()["threshold_metrics"] == payload


# --- Model versioning + active-model state -----------------------------------------------

def test_model_versioning_roundtrip(tmp_path, monkeypatch):
    """Every save stamps a monotonic version + trained_at and archives an immutable
    snapshot; the latest and versioned load paths both round-trip."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.pipeline import gbt_model

    gbt_model.model_file().write_text("booster-bytes", encoding="utf-8")
    gbt_model.feature_cols_file().write_text('["a", "b"]', encoding="utf-8")
    gbt_model.save_calibration([0.0, 1.0], [0.0, 1.0])

    metrics = {"n_labels_train": 5, "auc": 0.9}
    gbt_model.save_metrics(metrics)
    # save_metrics stamps the caller's dict in place, so train()'s return value carries it.
    assert metrics["version"] == 1 and "trained_at" in metrics

    assert gbt_model.latest_version() == 1
    assert gbt_model.model_exists(1)
    assert gbt_model.load_feature_cols(1) == ["a", "b"]
    assert gbt_model.load_metrics(1)["n_labels_train"] == 5

    # A second save bumps the version and leaves the first snapshot intact.
    gbt_model.save_metrics({"n_labels_train": 6})
    assert gbt_model.latest_version() == 2
    assert gbt_model.list_versions() == [1, 2]
    assert gbt_model.load_metrics(1)["n_labels_train"] == 5  # v1 untouched by the v2 train


def test_activate_deactivate_and_status_version(client, tmp_path, monkeypatch):
    """Activate/deactivate set a persistent active version; training never auto-activates;
    GET /api/model reports both the latest and the active version."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.pipeline import gbt_model

    # No trained model yet -> activate refuses.
    assert client.post("/api/model/activate", json={}).status_code == 400

    gbt_model.model_file().write_text("b", encoding="utf-8")
    gbt_model.feature_cols_file().write_text('["a"]', encoding="utf-8")
    gbt_model.save_metrics({"n_labels_train": 8})  # v1, trained but NOT active

    assert gbt_model.get_active_version() is None  # training did not auto-activate

    r = client.post("/api/model/activate", json={})
    assert r.status_code == 200 and r.json()["active_version"] == 1
    assert gbt_model.get_active_version() == 1

    status = client.get("/api/model").json()
    assert status["version"] == 1 and status["active_version"] == 1

    # A proxy-only version (no labels) is refused for activation.
    gbt_model.save_metrics({"n_labels_train": 0})  # v2
    assert client.post("/api/model/activate", json={"version": 2}).status_code == 400

    r = client.post("/api/model/deactivate")
    assert r.status_code == 200 and r.json()["active_version"] is None
    assert gbt_model.get_active_version() is None
    assert client.get("/api/model").json()["active_version"] is None


def test_labels_batch_skips_invalid(client):
    r = client.post("/api/labels/batch", json={"labels": [
        {"ocod_name_clean": "A LTD", "jurisdiction_clean": "ENGLAND",
         "roe_company_number": "OE1", "is_true_match": "true", "provenance": "bulk_range"},
        {"ocod_name_clean": "B LTD", "jurisdiction_clean": "ENGLAND",
         "roe_company_number": "OE2", "is_true_match": "nonsense"},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert body["failed"] == 1

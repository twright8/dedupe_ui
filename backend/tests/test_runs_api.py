# backend/tests/test_runs_api.py
"""Tests for the runs API router and pipeline_runner queue logic."""

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must be set before app.main / app.auth are imported
os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.main as _main_mod
import app.auth as _auth_mod
from app.db import query_db, write_db
from app.services import pipeline_runner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    """Create a temporary data directory with required sub-dirs."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "uploads").mkdir()
    (d / "runs").mkdir()
    return d


@pytest.fixture
def client(db_path, data_dir, monkeypatch):
    """TestClient with DB and DATA_DIR wired to temp dirs, auth bypassed."""
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )

    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake"})


@pytest.fixture(autouse=True)
def reset_runner_state():
    """Reset pipeline_runner module state between tests."""
    yield
    with pipeline_runner._run_lock:
        pipeline_runner._run_queue.clear()
        pipeline_runner._active_run_id = None
        pipeline_runner._event_queues.clear()


def _seed_config(db_path):
    """Insert a minimal config version for runs to reference."""
    from app.services.config_manager import save_version
    from tests.rulesets import default_ruleset

    return save_version(
        db_path,
        created_by="test",
        note="test config",
        ruleset=default_ruleset(),
        linkage_settings={
            "match_probability_threshold_high": 0.85,
            "match_probability_threshold_review": 0.50,
        },
    )


# ---------------------------------------------------------------------------
# pipeline_runner unit tests — queue logic
# ---------------------------------------------------------------------------


class TestPipelineRunnerQueue:
    """Test enqueue/dequeue logic without running actual pipeline stages."""

    def test_subscribe_and_unsubscribe(self):
        import asyncio

        q = pipeline_runner.subscribe_progress("run_test_001")
        assert "run_test_001" in pipeline_runner._event_queues
        assert q in pipeline_runner._event_queues["run_test_001"]

        pipeline_runner.unsubscribe_progress("run_test_001", q)
        assert len(pipeline_runner._event_queues["run_test_001"]) == 0

    def test_cancel_run(self, db_path):
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            ("run_cancel_test", "running"),
        )
        pipeline_runner.cancel_run(db_path, "run_cancel_test")
        rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", ("run_cancel_test",))
        assert rows[0]["status"] == "failed"
        assert "Cancelled" in rows[0]["error_message"]

    def test_enqueue_when_no_active_starts_immediately(self, db_path, data_dir, monkeypatch):
        """When no run is active, enqueue_run should call start_run immediately."""
        started = []

        def mock_start_run(**kwargs):
            started.append(kwargs["run_id"])

        monkeypatch.setattr(pipeline_runner, "start_run", mock_start_run)

        # Create a run row first
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            ("run_001", "pending"),
        )

        pipeline_runner.enqueue_run(
            db_path=db_path,
            data_dir=str(data_dir),
            run_id="run_001",
            input_path="/fake/donations.xlsx",
            config_version=1,
            threshold_high=0.85,
            threshold_review=0.50,
        )

        assert started == ["run_001"]

    def test_enqueue_when_active_queues(self, db_path, data_dir, monkeypatch):
        """When a run is already active, enqueue_run should queue without starting."""
        started = []

        def mock_start_run(**kwargs):
            started.append(kwargs["run_id"])

        monkeypatch.setattr(pipeline_runner, "start_run", mock_start_run)

        # Simulate an active run
        pipeline_runner._active_run_id = "run_000"

        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            ("run_002", "pending"),
        )

        pipeline_runner.enqueue_run(
            db_path=db_path,
            data_dir=str(data_dir),
            run_id="run_002",
            input_path="/fake/donations.xlsx",
            config_version=1,
            threshold_high=0.85,
            threshold_review=0.50,
        )

        assert started == []  # should NOT have started
        assert len(pipeline_runner._run_queue) == 1


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestRunsAPI:
    """Test REST endpoints via TestClient."""

    def test_create_run_returns_run_object(self, client, db_path, data_dir, monkeypatch):
        """One dataset means one input file, recorded as input_filename."""
        _seed_config(db_path)

        # Create a fake upload file
        uploads = data_dir / "uploads"
        (uploads / "donations.xlsx").write_bytes(b"fake")

        # Mock start_run so no actual pipeline runs
        monkeypatch.setattr(pipeline_runner, "start_run", lambda **kw: None)

        r = client.post(
            "/api/runs",
            json={
                "input_filename": "donations.xlsx",
                "config_version": 1,
                "threshold_high": 0.85,
                "threshold_review": 0.50,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending"
        assert body["input_filename"] == "donations.xlsx"
        assert body["config_version"] == 1
        assert body["id"].startswith("run_")

    def test_create_run_accepts_csv_input(self, client, db_path, data_dir, monkeypatch):
        """A raw .csv is a valid input (not only .xlsx)."""
        _seed_config(db_path)
        uploads = data_dir / "uploads"
        (uploads / "donations.csv").write_bytes(b"fake")
        monkeypatch.setattr(pipeline_runner, "start_run", lambda **kw: None)
        monkeypatch.setattr(pipeline_runner, "enqueue_run", lambda **kw: None)

        r = client.post(
            "/api/runs",
            json={"input_filename": "donations.csv", "config_version": 1},
        )
        assert r.status_code == 200, r.text
        assert r.json()["input_filename"] == "donations.csv"

    def test_create_run_rejects_unsupported_extension(self, client, db_path, data_dir, monkeypatch):
        """An input the profile does not accept is rejected upfront, with a clear message."""
        _seed_config(db_path)
        uploads = data_dir / "uploads"
        (uploads / "donations.txt").write_bytes(b"fake")
        monkeypatch.setattr(pipeline_runner, "start_run", lambda **kw: None)
        monkeypatch.setattr(pipeline_runner, "enqueue_run", lambda **kw: None)

        r = client.post(
            "/api/runs",
            json={"input_filename": "donations.txt", "config_version": 1},
        )
        assert r.status_code == 400
        assert ".csv" in r.json()["detail"] and ".xlsx" in r.json()["detail"]

    def test_create_run_without_any_upload_is_rejected(self, client, db_path, data_dir, monkeypatch):
        _seed_config(db_path)
        monkeypatch.setattr(pipeline_runner, "start_run", lambda **kw: None)

        r = client.post("/api/runs", json={"config_version": 1})
        assert r.status_code == 400
        assert "Upload a file first" in r.json()["detail"]

    def test_list_runs(self, client, db_path):
        write_db(
            db_path,
            """INSERT INTO runs (id, status, started_at, config_version)
               VALUES (?, ?, datetime('now'), ?)""",
            ("run_list_a", "complete", 1),
        )
        write_db(
            db_path,
            """INSERT INTO runs (id, status, started_at, config_version)
               VALUES (?, ?, datetime('now'), ?)""",
            ("run_list_b", "running", 1),
        )

        r = client.get("/api/runs")
        assert r.status_code == 200
        runs = r.json()
        assert len(runs) >= 2

    def test_get_run_detail(self, client, db_path):
        write_db(
            db_path,
            """INSERT INTO runs (id, status, config_version, counts_json)
               VALUES (?, ?, ?, ?)""",
            ("run_detail_1", "complete", 1, json.dumps({
                "records_total": 1000, "units_total": 900, "pairs_scored": 200,
                "pairs_accept": 120, "entities_proposed": 850,
                "review_queue": 30, "decisions_total": 2,
            })),
        )

        r = client.get("/api/runs/run_detail_1")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "run_detail_1"
        assert body["counts"]["recordsTotal"] == 1000
        assert body["counts"]["pairsAccept"] == 120
        assert body["counts"]["entitiesProposed"] == 850
        assert body["counts"]["reviewQueue"] == 30
        assert body["counts"]["decisionsTotal"] == 2
        assert body["counts"]["hasEntities"] is True
        # Nothing from the two-dataset tool this app was copied from.
        for gone in ("ocod", "roe", "exact", "probAccept", "matchRate",
                     "matchedTitles", "distinctEntities", "decisionModel",
                     "preLabels"):
            assert gone not in body["counts"]

    def test_get_run_surfaces_error_detail(self, client, db_path):
        """A failed run with structured error_detail_json exposes parsed error_detail."""
        detail = {
            "type": "unmapped_jurisdictions",
            "unmapped": [
                {"source_dataset": "ocod", "raw_value": "PUERTO RICO"},
                {"source_dataset": "roe", "raw_value": "TAJIKISTAN"},
            ],
        }
        write_db(
            db_path,
            """INSERT INTO runs (id, status, error_message, error_detail_json, config_version)
               VALUES (?, ?, ?, ?, ?)""",
            ("run_err_detail", "failed", "Unmapped jurisdiction values found.", json.dumps(detail), 1),
        )
        r = client.get("/api/runs/run_err_detail")
        assert r.status_code == 200
        body = r.json()
        assert body["error_detail"]["type"] == "unmapped_jurisdictions"
        assert len(body["error_detail"]["unmapped"]) == 2

    def test_get_run_detail_not_found(self, client):
        r = client.get("/api/runs/nonexistent")
        assert r.status_code == 404

    def test_cancel_run(self, client, db_path):
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            ("run_cancel_api", "running"),
        )
        r = client.post("/api/runs/run_cancel_api/cancel")
        assert r.status_code == 200

        rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", ("run_cancel_api",))
        assert rows[0]["status"] == "failed"

    def test_list_files_no_dir(self, client, db_path, data_dir):
        """Files endpoint returns empty list when run dir doesn't exist."""
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            ("run_nodir", "pending"),
        )
        r = client.get("/api/runs/run_nodir/files")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_files_with_files(self, client, db_path, data_dir):
        run_id = "run_files_test"
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            (run_id, "complete"),
        )
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "matches_high_confidence.csv").write_text("a,b\n1,2\n")
        (run_dir / "unmatched_roe.csv").write_text("x\n")

        r = client.get(f"/api/runs/{run_id}/files")
        assert r.status_code == 200
        files = r.json()
        names = [f["name"] for f in files]
        assert "matches_high_confidence.csv" in names
        assert "unmatched_roe.csv" in names

    def test_download_file(self, client, db_path, data_dir):
        run_id = "run_dl_test"
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            (run_id, "complete"),
        )
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "output.csv").write_text("hello,world\n")

        r = client.get(f"/api/runs/{run_id}/files/output.csv")
        assert r.status_code == 200
        assert b"hello,world" in r.content

    def test_download_file_path_traversal_rejected(self, client, db_path, data_dir):
        run_id = "run_traversal"
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            (run_id, "complete"),
        )
        r = client.get(f"/api/runs/{run_id}/files/..%2F..%2Fetc%2Fpasswd")
        assert r.status_code == 400

    def test_timeline_returns_events(self, client, db_path, data_dir):
        run_id = "run_timeline"
        write_db(
            db_path,
            "INSERT INTO runs (id, status) VALUES (?, ?)",
            (run_id, "complete"),
        )
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        events = [
            {"event": "stage_start", "stage": 0, "name": "preprocess"},
            {"event": "stage_end", "stage": 0, "elapsed_seconds": 5.0},
        ]
        with open(run_dir / "events.jsonl", "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        r = client.get(f"/api/runs/{run_id}/timeline")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        assert data[0]["event"] == "stage_start"



    def test_run_id_format(self, client, db_path, data_dir, monkeypatch):
        """Run ID should follow run_YYYY_MM_DDx format."""
        _seed_config(db_path)
        uploads = data_dir / "uploads"
        (uploads / "donations.xlsx").write_bytes(b"fake")

        monkeypatch.setattr(pipeline_runner, "start_run", lambda **kw: None)

        r = client.post(
            "/api/runs",
            json={
                "input_filename": "donations.xlsx",
                "config_version": 1,
                "threshold_high": 0.85,
                "threshold_review": 0.50,
            },
        )
        run_id = r.json()["id"]
        # Format: run_YYYY_MM_DDx
        import re

        assert re.match(r"run_\d{4}_\d{2}_\d{2}[a-z]", run_id)

    def test_same_day_runs_increment_suffix(self, client, db_path, data_dir, monkeypatch):
        """Multiple runs on the same day should get a, b, c... suffixes."""
        _seed_config(db_path)
        uploads = data_dir / "uploads"
        (uploads / "donations.xlsx").write_bytes(b"fake")

        monkeypatch.setattr(pipeline_runner, "start_run", lambda **kw: None)

        ids = []
        for _ in range(3):
            r = client.post(
                "/api/runs",
                json={
                    "input_filename": "donations.xlsx",
                    "config_version": 1,
                    "threshold_high": 0.85,
                    "threshold_review": 0.50,
                },
            )
            ids.append(r.json()["id"])

        # Suffixes should be a, b, c
        assert ids[0].endswith("a")
        assert ids[1].endswith("b")
        assert ids[2].endswith("c")


# ---------------------------------------------------------------------------
# Diagnostics endpoint tests
# ---------------------------------------------------------------------------


class TestDiagnosticsAPI:
    """Tests for GET /api/runs/{run_id}/diagnostics."""

    def _seed_run(self, db_path, run_id, threshold_high=0.70, threshold_review=0.40):
        write_db(
            db_path,
            """INSERT INTO runs (id, status, threshold_high, threshold_review)
               VALUES (?, ?, ?, ?)""",
            (run_id, "complete", threshold_high, threshold_review),
        )

    def test_diagnostics_404_for_unknown_run(self, client):
        r = client.get("/api/runs/nonexistent_run/diagnostics")
        assert r.status_code == 404

    def test_diagnostics_default_when_no_files(self, client, db_path, data_dir):
        """With no parquet or model file, returns zeros and empty arrays."""
        run_id = "run_diag_empty"
        self._seed_run(db_path, run_id, threshold_high=0.70, threshold_review=0.40)
        # Don't create run dir — files missing

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        body = r.json()

        # Histogram: 20 zeros
        assert body["histogram"] == [0] * 20

        # Feature weights: empty
        assert body["feature_weights"] == []

        # Thresholds match DB values
        assert body["thresholds"]["threshold_high"] == pytest.approx(0.70)
        assert body["thresholds"]["threshold_review"] == pytest.approx(0.40)

        # Band counts: all zeros
        assert body["band_counts"] == {"auto_accept": 0, "review": 0, "dropped": 0}

    def test_diagnostics_histogram_from_parquet(self, client, db_path, data_dir):
        """Histogram is computed from match_probability column in linkage_scored.parquet."""
        import pandas as pd

        run_id = "run_diag_hist"
        self._seed_run(db_path, run_id, threshold_high=0.70, threshold_review=0.40)
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)

        # Create a parquet with known probabilities
        # 2 pairs at ~0.1 (bin 2), 3 pairs at ~0.8 (bin 16), 1 pair at ~0.95 (bin 19)
        probs = [0.10, 0.12, 0.80, 0.81, 0.82, 0.95]
        df = pd.DataFrame({"match_probability": probs})
        df.to_parquet(run_dir / "linkage_scored.parquet", index=False)

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        body = r.json()

        hist = body["histogram"]
        assert len(hist) == 20
        # Total across all bins must equal total number of rows
        assert sum(hist) == len(probs)
        # Bins are non-negative integers
        assert all(isinstance(v, int) and v >= 0 for v in hist)
        # Values around 0.10 should be in bins 2 (0.10-0.15)
        assert hist[2] == 2  # bin 2: 0.10-0.15
        # Values around 0.80 should be in bin 16 (0.80-0.85)
        assert hist[16] == 3
        # Value at 0.95 lands in bin 18 (0.90-0.95) — numpy uses half-open [lo, hi)
        # and closes the last bin, so 0.95 goes into bin 18 (0.90–0.9500...)
        assert hist[18] == 1

    def test_diagnostics_band_counts(self, client, db_path, data_dir):
        """Band counts split histogram correctly by thresholds."""
        import pandas as pd

        run_id = "run_diag_bands"
        self._seed_run(db_path, run_id, threshold_high=0.70, threshold_review=0.40)
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)

        # 2 dropped (<0.40), 3 review (0.40–0.70), 4 auto_accept (>=0.70)
        probs = [0.10, 0.20, 0.45, 0.55, 0.65, 0.72, 0.80, 0.85, 0.95]
        df = pd.DataFrame({"match_probability": probs})
        df.to_parquet(run_dir / "linkage_scored.parquet", index=False)

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        body = r.json()

        counts = body["band_counts"]
        assert counts["auto_accept"] + counts["review"] + counts["dropped"] == len(probs)
        assert counts["dropped"] == 2
        assert counts["review"] == 3
        assert counts["auto_accept"] == 4

    def test_diagnostics_feature_weights_from_model(self, client, db_path, data_dir):
        """Feature weights are extracted from splink_model.json."""
        import math

        run_id = "run_diag_weights"
        self._seed_run(db_path, run_id)
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)

        model = {
            "comparisons": [
                {
                    "output_column_name": "name_clean",
                    "comparison_levels": [
                        {"is_null_level": True, "m_probability": None, "u_probability": None},
                        {"label_for_charts": "exact", "m_probability": 0.85, "u_probability": 0.01},
                        {"label_for_charts": "else", "m_probability": 0.15, "u_probability": 0.99},
                    ],
                },
                {
                    "output_column_name": "jurisdiction_clean",
                    "comparison_levels": [
                        {"is_null_level": True, "m_probability": None, "u_probability": None},
                        {"label_for_charts": "exact", "m_probability": 0.60, "u_probability": 0.20},
                        {"label_for_charts": "else", "m_probability": 0.40, "u_probability": 0.80},
                    ],
                },
            ]
        }
        with open(run_dir / "splink_model.json", "w") as fh:
            json.dump(model, fh)

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        body = r.json()

        weights = body["feature_weights"]
        assert len(weights) == 2

        # name_clean: m=0.85, u=0.01 → importance = log2(0.85/0.01)
        name_w = next(w for w in weights if w["name"] == "name_clean")
        assert name_w["m"] == pytest.approx(0.85, rel=1e-4)
        assert name_w["u"] == pytest.approx(0.01, rel=1e-4)
        expected_importance = math.log2(0.85 / 0.01)
        assert name_w["importance"] == pytest.approx(expected_importance, rel=1e-3)

        # jurisdiction_clean: m=0.60, u=0.20
        jur_w = next(w for w in weights if w["name"] == "jurisdiction_clean")
        assert jur_w["m"] == pytest.approx(0.60, rel=1e-4)
        assert jur_w["u"] == pytest.approx(0.20, rel=1e-4)

    def test_diagnostics_skips_all_null_comparisons(self, client, db_path, data_dir):
        """Comparisons with only null levels are excluded from feature_weights."""
        run_id = "run_diag_null_comp"
        self._seed_run(db_path, run_id)
        run_dir = data_dir / "runs" / run_id
        run_dir.mkdir(parents=True)

        model = {
            "comparisons": [
                {
                    "output_column_name": "name_clean",
                    "comparison_levels": [
                        # Only a null level — no usable m/u
                        {"is_null_level": True, "m_probability": None, "u_probability": None},
                    ],
                },
            ]
        }
        with open(run_dir / "splink_model.json", "w") as fh:
            json.dump(model, fh)

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        assert r.json()["feature_weights"] == []

    def test_diagnostics_thresholds_from_db(self, client, db_path, data_dir):
        """Thresholds in response match what was stored in the DB."""
        run_id = "run_diag_thresh"
        self._seed_run(db_path, run_id, threshold_high=0.90, threshold_review=0.55)

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        body = r.json()
        assert body["thresholds"]["threshold_high"] == pytest.approx(0.90)
        assert body["thresholds"]["threshold_review"] == pytest.approx(0.55)

    def test_diagnostics_response_structure(self, client, db_path, data_dir):
        """Response always has all four top-level keys with correct types."""
        run_id = "run_diag_struct"
        self._seed_run(db_path, run_id)

        r = client.get(f"/api/runs/{run_id}/diagnostics")
        assert r.status_code == 200
        body = r.json()

        assert "histogram" in body
        assert "feature_weights" in body
        assert "thresholds" in body
        assert "band_counts" in body

        assert isinstance(body["histogram"], list)
        assert len(body["histogram"]) == 20

        assert isinstance(body["feature_weights"], list)

        assert "threshold_high" in body["thresholds"]
        assert "threshold_review" in body["thresholds"]

        assert "auto_accept" in body["band_counts"]
        assert "review" in body["band_counts"]
        assert "dropped" in body["band_counts"]

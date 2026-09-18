"""GET /api/runs/{run_id}/records, and the single-file run that produces it."""

import json
import os
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
    d = tmp_path / "data"
    (d / "uploads").mkdir(parents=True)
    (d / "runs").mkdir(parents=True)
    return d


@pytest.fixture
def client(db_path, data_dir, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )

    from fastapi.testclient import TestClient

    return TestClient(_main_mod.app, cookies={"session": "fake"})


@pytest.fixture(autouse=True)
def reset_runner_state():
    yield
    with pipeline_runner._run_lock:
        pipeline_runner._run_queue.clear()
        pipeline_runner._active_run_id = None
        pipeline_runner._event_queues.clear()


def _seed_config(db_path):
    from app.services.config_manager import save_version
    from tests.rulesets import default_ruleset

    return save_version(
        db_path,
        created_by="test",
        note="test config",
        ruleset=default_ruleset(),
        linkage_settings={
            "match_probability_threshold_high": 0.92,
            "match_probability_threshold_review": 0.50,
        },
    )


_RECORDS = [
    # record_id, track, name, review_state, existing_entity_id, first_year, total_value
    ("1", "person", "Alice Smith", "labelled", "1", 2001, 100.0),
    ("2", "person", "Bob Jones", "unreviewed", None, 2010, 5000.0),
    ("3", "organisation", "Acme Ltd", "unreviewed", None, 2015, 250.5),
    ("4", "organisation", "Beta Holdings Ltd", "labelled", "4", None, 900.0),
    ("TR5", "organisation", "Smith Family Trust", "unreviewed", None, 2020, 10.0),
]


def _seed_run_with_records(db_path, data_dir, run_id="run_records"):
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", (run_id, "complete"))
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    frame = pd.DataFrame(
        _RECORDS,
        columns=["record_id", "track", "name", "review_state", "existing_entity_id",
                 "first_year", "total_value"],
    )
    frame["all_names"] = frame["name"]
    frame["first_year"] = frame["first_year"].astype("Int64")
    frame.to_parquet(run_dir / "records.parquet", index=False)
    return run_id


# ---------------------------------------------------------------------------
# Reading records
# ---------------------------------------------------------------------------


class TestRecordsAPI:
    def test_returns_every_record_by_default(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/records").json()

        assert body["total"] == 5
        assert body["offset"] == 0
        assert body["limit"] == 100
        assert len(body["items"]) == 5
        # Default sort is by name, ascending
        assert [i["name"] for i in body["items"]][0] == "Acme Ltd"

    def test_counts_describe_the_whole_run(self, client, db_path, data_dir):
        """Filters move `total`; `counts` stay put so the tabs do not flicker."""
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/records?track=person&q=bob").json()

        assert body["total"] == 1
        assert body["counts"] == {
            "all": 5, "person": 2, "organisation": 3, "labelled": 2, "unreviewed": 3,
        }

    def test_track_filter(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/records?track=organisation").json()
        assert body["total"] == 3
        assert {i["track"] for i in body["items"]} == {"organisation"}

    def test_state_filter(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/records?state=labelled").json()
        assert body["total"] == 2
        assert {i["review_state"] for i in body["items"]} == {"labelled"}

    def test_filters_combine(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(
            f"/api/runs/{run_id}/records?track=organisation&state=labelled"
        ).json()
        assert [i["record_id"] for i in body["items"]] == ["4"]

    def test_search_is_case_insensitive_and_covers_the_record_id(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)

        assert client.get(f"/api/runs/{run_id}/records?q=SMITH").json()["total"] == 2
        assert client.get(f"/api/runs/{run_id}/records?q=tr5").json()["total"] == 1
        assert client.get(f"/api/runs/{run_id}/records?q=nothing here").json()["total"] == 0

    def test_search_does_not_let_sql_through(self, client, db_path, data_dir):
        """q is bound as a parameter, so quotes are just characters."""
        run_id = _seed_run_with_records(db_path, data_dir)
        r = client.get(f"/api/runs/{run_id}/records", params={"q": "' OR 1=1 --"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_sort_and_order(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(
            f"/api/runs/{run_id}/records?sort=total_value&order=desc"
        ).json()
        assert [i["record_id"] for i in body["items"]] == ["2", "4", "3", "1", "TR5"]

    def test_unknown_sort_column_is_rejected(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        r = client.get(f"/api/runs/{run_id}/records?sort=1;DROP TABLE runs")
        assert r.status_code == 400
        assert "Unknown sort column" in r.json()["detail"]

    def test_bad_order_is_rejected(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        r = client.get(f"/api/runs/{run_id}/records?order=sideways")
        assert r.status_code == 400

    def test_bad_track_or_state_is_rejected(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        assert client.get(f"/api/runs/{run_id}/records?track=animal").status_code == 400
        assert client.get(f"/api/runs/{run_id}/records?state=maybe").status_code == 400

    def test_paging(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        first = client.get(f"/api/runs/{run_id}/records?limit=2").json()
        second = client.get(f"/api/runs/{run_id}/records?limit=2&offset=2").json()

        assert first["total"] == second["total"] == 5
        assert len(first["items"]) == 2
        assert first["offset"] == 0 and second["offset"] == 2
        ids = [i["record_id"] for i in first["items"] + second["items"]]
        assert len(set(ids)) == 4  # no overlap between the pages

    def test_limit_is_capped(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        assert client.get(f"/api/runs/{run_id}/records?limit=99999").status_code == 422

    def test_nulls_serialise_as_null(self, client, db_path, data_dir):
        run_id = _seed_run_with_records(db_path, data_dir)
        body = client.get(f"/api/runs/{run_id}/records?q=Beta").json()
        item = body["items"][0]
        assert item["first_year"] is None
        assert "NaN" not in json.dumps(body)

    def test_404_for_an_unknown_run(self, client):
        assert client.get("/api/runs/nope/records").status_code == 404

    def test_404_when_the_run_has_no_records_file(self, client, db_path, data_dir):
        write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("run_empty", "running"))
        r = client.get("/api/runs/run_empty/records")
        assert r.status_code == 404
        assert "no records" in r.json()["detail"]

    def test_records_needs_a_login(self, db_path, data_dir, monkeypatch):
        monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
        monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
        run_id = _seed_run_with_records(db_path, data_dir)

        from fastapi.testclient import TestClient

        anonymous = TestClient(_main_mod.app)
        assert anonymous.get(f"/api/runs/{run_id}/records").status_code == 401


# ---------------------------------------------------------------------------
# One file in, records out
# ---------------------------------------------------------------------------


def _donation_rows():
    return pd.DataFrame([
        {"DonorId": 1, "DonorName": "Alice Smith", "DonorStatus": "Individual",
         "DonorIDStandardTR": 0, "RegulatedEntityName": "Labour Party",
         "AcceptedDate": "2015-06-01", "Value": 500},
        {"DonorId": 1, "DonorName": "Alice Smith", "DonorStatus": "Individual",
         "DonorIDStandardTR": 0, "RegulatedEntityName": "Labour Party",
         "AcceptedDate": "2016-06-01", "Value": 700},
        {"DonorId": 2, "DonorName": "Acme Ltd", "DonorStatus": "Company",
         "DonorIDStandardTR": 77, "RegulatedEntityName": "Green Party",
         "AcceptedDate": "2019-01-01", "Value": 1000},
        {"DonorId": 3, "DonorName": "Smith Family Trust", "DonorStatus": "Trust",
         "DonorIDStandardTR": 0, "RegulatedEntityName": "Green Party",
         "AcceptedDate": "2020-01-01", "Value": 25},
    ])


def _wait_for_status(db_path, run_id, status, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = query_db(db_path, "SELECT status, error_message FROM runs WHERE id = ?", (run_id,))
        if rows and rows[0]["status"] == status:
            return rows[0]
        if rows and rows[0]["status"] == "failed":
            raise AssertionError(f"Run failed: {rows[0]['error_message']}")
        time.sleep(0.05)
    raise AssertionError(f"Run did not reach '{status}' in {timeout}s")


def test_single_file_run_completes_and_writes_records(client, db_path, data_dir):
    """Upload one file, start a run, and the records are there to read."""
    _seed_config(db_path)
    _donation_rows().to_csv(data_dir / "uploads" / "donations.csv", index=False)

    r = client.post("/api/runs", json={"input_filename": "donations.csv", "config_version": 1})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    assert r.json()["input_filename"] == "donations.csv"

    _wait_for_status(db_path, run_id, "complete")

    assert (data_dir / "runs" / run_id / "records.parquet").is_file()

    counts = client.get(f"/api/runs/{run_id}").json()["counts"]
    assert counts["recordsTotal"] == 3
    assert counts["recordsPerson"] == 1
    assert counts["recordsOrganisation"] == 2
    assert counts["recordsLabelled"] == 1
    assert counts["inputRows"] == 4

    body = client.get(f"/api/runs/{run_id}/records").json()
    assert body["total"] == 3
    assert {i["record_id"] for i in body["items"]} == {"1", "2", "TR3"}
    alice = next(i for i in body["items"] if i["record_id"] == "1")
    assert alice["n_donations"] == 2
    assert alice["total_value"] == 1200
    assert alice["track"] == "person"


def test_a_failed_load_marks_the_run_failed(client, db_path, data_dir):
    """A file the profile cannot read must fail the run, not half-finish it."""
    _seed_config(db_path)
    (data_dir / "uploads" / "wrong.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    r = client.post("/api/runs", json={"input_filename": "wrong.csv", "config_version": 1})
    run_id = r.json()["id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        rows = query_db(db_path, "SELECT status, error_message FROM runs WHERE id = ?", (run_id,))
        if rows and rows[0]["status"] == "failed":
            assert "missing required column" in rows[0]["error_message"]
            return
        time.sleep(0.05)
    raise AssertionError("Run never failed")

# backend/tests/test_stage_1_clean.py
"""Stage 1 (clean), and the run that fails when a lookup cannot map a value."""

import json
import os
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset

import app.auth as _auth_mod
import app.main as _main_mod
from app.db import query_db
from app.pipeline.dedupe.stage_0_load import run_stage_0_load
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME, clean_records, run_stage_1_clean
from app.profiles.donations import RAW_COLUMNS
from app.rules import engine
from app.services import pipeline_runner


DONATION_ROWS = [
    {"DonorId": 1, "DonorName": "Mr John Smith MP", "DonorStatus": "Individual",
     "Postcode": "le1 1fb", "Value": 100},
    {"DonorId": 2, "DonorName": "The (AQ) Networks Ltd", "DonorStatus": "Company",
     "CompanyRegistrationNumber": "4250076", "Value": 200},
    {"DonorId": 3, "DonorName": "(AQ) Networks Ltd", "DonorStatus": "Company",
     "CompanyRegistrationNumber": "04250076 ?", "Value": 300},
    {"DonorId": 4, "DonorName": "West End Club", "DonorStatus": "Other", "Value": 50},
]


@pytest.fixture
def run_dir(tmp_path):
    path = tmp_path / "run"
    (path / "config").mkdir(parents=True)
    pd.DataFrame(DONATION_ROWS).to_csv(tmp_path / "donations.csv", index=False)
    run_stage_0_load(run_dir=str(path), input_path=str(tmp_path / "donations.csv"))
    return path


def _write_ruleset(run_dir, ruleset=None):
    (run_dir / "config" / "ruleset.json").write_text(
        json.dumps(ruleset or default_ruleset()), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# clean_records — the shape of the output frame
# ---------------------------------------------------------------------------


def _raw_frame(rows):
    frame = pd.DataFrame(rows)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[RAW_COLUMNS]


def test_both_tracks_land_in_one_frame_with_the_other_track_null():
    frame = _raw_frame([
        {"record_id": "1", "name": "Mr John Smith", "donor_status": "Individual"},
        {"record_id": "2", "name": "Acme Ltd", "donor_status": "Company",
         "company_number": "4250076"},
    ])
    cleaned = clean_records(frame, default_ruleset())

    assert list(cleaned.columns)[:len(RAW_COLUMNS)] == RAW_COLUMNS
    assert list(cleaned["track"]) == ["person", "organisation"]

    person, organisation = cleaned.iloc[0], cleaned.iloc[1]
    assert person["name_clean"] == "JOHN SMITH" and person["surname"] == "SMITH"
    assert organisation["name_core"] == "ACME"
    assert organisation["company_number_clean"] == "04250076"
    # A column only the other track produces is null, never missing.
    assert person["company_number_clean"] is None
    assert organisation["surname"] is None


def test_the_row_order_is_the_input_order():
    frame = _raw_frame([
        {"record_id": str(i), "name": f"Name {i}",
         "donor_status": "Individual" if i % 2 else "Company"}
        for i in range(10)
    ])
    cleaned = clean_records(frame, default_ruleset())
    assert list(cleaned["record_id"]) == [str(i) for i in range(10)]


def test_a_track_with_no_records_still_contributes_its_columns():
    frame = _raw_frame([{"record_id": "1", "name": "Acme Ltd", "donor_status": "Company"}])
    cleaned = clean_records(frame, default_ruleset())
    assert set(cleaned["track"]) == {"organisation"}
    for column in ("surname", "forename_canon", "surname_metaphone"):
        assert column in cleaned.columns
        assert cleaned[column].isna().all()


def test_an_empty_frame_produces_an_empty_cleaned_frame():
    cleaned = clean_records(_raw_frame([]), default_ruleset())
    assert len(cleaned) == 0
    assert "track" in cleaned.columns and "name_core" in cleaned.columns


# ---------------------------------------------------------------------------
# run_stage_1_clean
# ---------------------------------------------------------------------------


def test_stage_1_writes_the_records_parquet(run_dir):
    _write_ruleset(run_dir)
    stats = run_stage_1_clean(run_dir=str(run_dir), config_dir=str(run_dir / "config"))

    assert (run_dir / RECORDS_FILENAME).is_file()
    assert stats == {"records_total": 4, "records_person": 1, "records_organisation": 3}

    records = pd.read_parquet(run_dir / RECORDS_FILENAME)
    by_id = records.set_index("record_id")
    assert by_id.loc["1", "track"] == "person"
    assert by_id.loc["1", "title"] == "MR"
    assert by_id.loc["1", "post_nominals"] == "MP"
    assert by_id.loc["2", "company_number_clean"] == by_id.loc["3", "company_number_clean"]


def test_stage_1_emits_the_usual_progress_events(run_dir):
    _write_ruleset(run_dir)
    events = []
    run_stage_1_clean(
        run_dir=str(run_dir), config_dir=str(run_dir / "config"),
        progress_callback=lambda kind, detail: events.append((kind, detail)),
    )
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "stage_start" and kinds[-1] == "stage_end"
    assert events[0][1] == {"stage": 1, "name": "clean"}
    end = events[-1][1]
    assert end["stage"] == 1 and end["records_person"] == 1
    assert "elapsed_seconds" in end


def test_stage_1_says_so_when_the_run_has_no_ruleset(run_dir):
    with pytest.raises(FileNotFoundError, match="no ruleset.json"):
        run_stage_1_clean(run_dir=str(run_dir), config_dir=str(run_dir / "config"))


def test_stage_1_raises_the_unmapped_lookup_error(run_dir):
    ruleset = default_ruleset()
    ruleset["lookups"]["nicknames"]["fallback"] = "error"
    ruleset["lookups"]["nicknames"]["rows"] = []
    _write_ruleset(run_dir, ruleset)

    with pytest.raises(engine.UnmappedLookupValuesError) as caught:
        run_stage_1_clean(run_dir=str(run_dir), config_dir=str(run_dir / "config"))
    assert caught.value.table == "nicknames"
    assert caught.value.values == ["JOHN"]


# ---------------------------------------------------------------------------
# The two stages together, through the runner
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "uploads").mkdir(parents=True)
    (d / "runs").mkdir(parents=True)
    pd.DataFrame(DONATION_ROWS).to_csv(d / "uploads" / "donations.csv", index=False)
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


def _seed(db_path, ruleset=None):
    from app.services.config_manager import save_version
    return save_version(db_path, created_by="test", note="t",
                        ruleset=ruleset or default_ruleset(), linkage_settings={})


def _wait(db_path, run_id, status, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
        if rows and rows[0]["status"] == status:
            return rows[0]
        if rows and rows[0]["status"] == "failed" and status != "failed":
            raise AssertionError(f"Run failed: {rows[0]['error_message']}")
        time.sleep(0.05)
    raise AssertionError(f"Run did not reach '{status}' in {timeout}s")


def test_a_run_loads_then_cleans_and_snapshots_its_rules(client, db_path, data_dir):
    _seed(db_path)
    run_id = client.post(
        "/api/runs", json={"input_filename": "donations.csv", "config_version": 1}
    ).json()["id"]
    _wait(db_path, run_id, "complete")

    run_dir = data_dir / "runs" / run_id
    assert (run_dir / "records_raw.parquet").is_file()
    assert (run_dir / "records.parquet").is_file()
    assert (run_dir / "config" / "ruleset.json").is_file()
    assert (run_dir / "config" / "linkage_settings.json").is_file()
    # The legacy artefacts are gone.
    assert not (run_dir / "config" / "name_rules.json").exists()
    assert not (run_dir / "config" / "jurisdiction_map.csv").exists()

    counts = client.get(f"/api/runs/{run_id}").json()["counts"]
    assert counts["recordsTotal"] == 4
    assert counts["recordsPerson"] == 1
    assert counts["recordsOrganisation"] == 3


def test_every_stage_reports_progress(client, db_path, data_dir):
    _seed(db_path)
    run_id = client.post(
        "/api/runs", json={"input_filename": "donations.csv", "config_version": 1}
    ).json()["id"]
    _wait(db_path, run_id, "complete")

    timeline = client.get(f"/api/runs/{run_id}/timeline").json()
    starts = [e for e in timeline if e.get("event") == "stage_start"]
    assert [(e["stage"], e["name"]) for e in starts] == [
        (0, "load"), (1, "clean"), (2, "exact"), (3, "score"),
        (4, "cluster"), (5, "entities"),
    ]


def test_an_unmapped_lookup_fails_the_run_with_a_structured_error(client, db_path, data_dir):
    ruleset = default_ruleset()
    ruleset["lookups"]["nicknames"]["fallback"] = "error"
    ruleset["lookups"]["nicknames"]["rows"] = []
    _seed(db_path, ruleset)

    run_id = client.post(
        "/api/runs", json={"input_filename": "donations.csv", "config_version": 1}
    ).json()["id"]
    _wait(db_path, run_id, "failed")

    body = client.get(f"/api/runs/{run_id}").json()
    assert body["error_detail"] == {
        "kind": "unmapped_lookup_values", "table": "nicknames", "values": ["JOHN"],
    }
    assert "nicknames" in body["error_message"]


def test_the_pipeline_stages_endpoint_lists_the_stages_that_exist(client):
    stages = client.get("/api/pipeline/stages").json()
    assert [s["key"] for s in stages] == [
        "load", "clean", "exact", "score", "cluster", "entities",
    ]
    for stage in stages:
        assert set(stage) == {"key", "label", "description"}
        assert stage["label"] and stage["description"]


# ---------------------------------------------------------------------------
# Batching (slice 8b): the file is cleaned in row batches, and the batch size
# must not be able to change the answer.
# ---------------------------------------------------------------------------


def _many_rows(n: int):
    """Enough rows, varied enough, to span several batches."""
    import pandas as pd

    from app.profiles.donations import RAW_COLUMNS as DONATION_COLUMNS

    rows = []
    for i in range(n):
        rows.append({
            "record_id": str(i),
            "name": ["Mr John Smith", "Acme Ltd", "UNISON", "N/A", "Dr Jane Doe"][i % 5],
            "donor_status": ["Individual", "Company", "Trade Union", "Other",
                             "Impermissible Donor"][i % 5],
            "company_number": ["4250076", None, None, "OC314414", None][i % 5],
            "postcode": ["le1 1fb", None, "SW1A 1AA", None, None][i % 5],
            "review_state": "unreviewed",
            "existing_entity_id": None,
        })
    frame = pd.DataFrame(rows)
    for column in DONATION_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[DONATION_COLUMNS]


def _clean_with_batch_size(tmp_path, frame, size, monkeypatch, name):
    import pandas as pd

    from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME, run_stage_1_clean

    run = tmp_path / name
    (run / "config").mkdir(parents=True)
    frame.to_parquet(run / "records_raw.parquet", index=False)
    (run / "config" / "ruleset.json").write_text(
        json.dumps(default_ruleset()), encoding="utf-8"
    )
    monkeypatch.setenv("CLEAN_BATCH_ROWS", str(size))
    stats = run_stage_1_clean(run_dir=str(run), config_dir=str(run / "config"))
    return stats, pd.read_parquet(run / RECORDS_FILENAME)


def test_the_batch_size_cannot_change_the_answer(tmp_path, monkeypatch):
    """Every op is row-independent, so 1,000 rows at a time and 1,000,000 at a
    time have to agree exactly — values, counts and column order."""
    frame = _many_rows(2500)
    small_stats, small = _clean_with_batch_size(tmp_path, frame, 1000, monkeypatch, "a")
    big_stats, big = _clean_with_batch_size(tmp_path, frame, 1_000_000, monkeypatch, "b")

    assert small_stats == big_stats
    assert list(small.columns) == list(big.columns)
    pd.testing.assert_frame_equal(
        small.sort_values("record_id").reset_index(drop=True),
        big.sort_values("record_id").reset_index(drop=True),
    )


def test_a_batch_of_one_row_still_agrees(tmp_path, monkeypatch):
    frame = _many_rows(25)
    one, single = _clean_with_batch_size(tmp_path, frame, 1, monkeypatch, "c")
    all_at_once, whole = _clean_with_batch_size(tmp_path, frame, 1_000_000, monkeypatch, "d")
    assert one == all_at_once
    pd.testing.assert_frame_equal(
        single.sort_values("record_id").reset_index(drop=True),
        whole.sort_values("record_id").reset_index(drop=True),
    )


def test_a_batch_whose_track_is_empty_writes_the_same_columns(tmp_path, monkeypatch):
    """A batch of only organisations must still carry the person columns, or the
    parquet's schema changes half way through the file."""
    import pandas as pd

    from app.profiles.donations import RAW_COLUMNS as DONATION_COLUMNS

    rows = [
        {"record_id": "1", "name": "Acme Ltd", "donor_status": "Company"},
        {"record_id": "2", "name": "Beta Ltd", "donor_status": "Company"},
        {"record_id": "3", "name": "Mr John Smith", "donor_status": "Individual"},
        {"record_id": "4", "name": "Ms Jane Doe", "donor_status": "Individual"},
    ]
    frame = pd.DataFrame(rows)
    for column in DONATION_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame["review_state"] = "unreviewed"
    frame = frame[DONATION_COLUMNS]

    # Two batches: the first is all organisations, the second all people.
    stats, cleaned = _clean_with_batch_size(tmp_path, frame, 2, monkeypatch, "e")
    assert stats["records_person"] == 2 and stats["records_organisation"] == 2
    for column in ("surname", "forename_canon", "surname_metaphone",
                   "name_core", "legal_form", "company_number_clean"):
        assert column in cleaned.columns, column
    # The organisations carry no surname and the people no core name.
    by_id = cleaned.set_index("record_id")
    assert pd.isna(by_id.loc["1", "surname"])
    assert pd.isna(by_id.loc["3", "name_core"])


def test_the_written_columns_are_known_before_a_row_is_read():
    """The schema comes from the ruleset, not from whatever the first batch
    happened to contain."""
    from app.pipeline.dedupe.stage_1_clean import written_columns
    from app.profiles.donations import RAW_COLUMNS as DONATION_COLUMNS

    columns = written_columns(list(DONATION_COLUMNS), default_ruleset())
    assert columns[:len(DONATION_COLUMNS)] == list(DONATION_COLUMNS)
    assert columns[len(DONATION_COLUMNS)] == "track"
    for expected in ("name_clean", "surname", "name_core", "donor_status_std",
                     "donor_status_std_rule"):
        assert expected in columns


def test_a_duplicate_record_id_is_caught_across_batches(tmp_path, monkeypatch):
    """Uniqueness spans the file, so it cannot be checked inside one batch."""
    import pandas as pd

    from app.profiles.donations import RAW_COLUMNS as DONATION_COLUMNS

    frame = pd.DataFrame([
        {"record_id": "1", "name": "A", "donor_status": "Company"},
        {"record_id": "2", "name": "B", "donor_status": "Company"},
        {"record_id": "1", "name": "C", "donor_status": "Company"},
    ])
    for column in DONATION_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame["review_state"] = "unreviewed"

    with pytest.raises(ValueError, match="record_id must be unique"):
        _clean_with_batch_size(tmp_path, frame[DONATION_COLUMNS], 2, monkeypatch, "f")

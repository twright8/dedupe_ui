"""``scripts/adopt_run.py``: registering a finished run folder with an instance.

A full PSC run is hours of work on a machine that is not the server, so the
folder exists long before any instance knows about it. Nothing in the API goes
that way round: ``POST /api/runs`` creates the row and then fills the folder.
These pin what the script writes, that it never overwrites a run, and that the
readers can find the run afterwards.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import adopt_run  # noqa: E402
from app.db import query_db  # noqa: E402


@pytest.fixture
def run_folder(tmp_path):
    """A minimal finished run: the five files, a config snapshot, one report."""
    data = tmp_path / "data"
    run = data / "runs" / "psc_full"
    (run / "config").mkdir(parents=True)

    pd.DataFrame({"record_id": ["r1", "r2", "r3"],
                  "track": ["person", "person", "organisation"],
                  "name": ["A", "B", "C"]}).to_parquet(run / "records.parquet")
    pd.DataFrame({"unit_id": ["r1", "r3"], "unit_size": [2, 1],
                  "track": ["person", "organisation"]}).to_parquet(run / "units.parquet")
    pd.DataFrame({"unit_id_l": ["r1"], "unit_id_r": ["r3"],
                  "score_bucket": ["accept"], "track": ["person"],
                  "veto_reason": [None]}).to_parquet(run / "pairs.parquet")
    pd.DataFrame({"cluster_id": ["c1"], "status": ["ok"]}).to_parquet(
        run / "clusters.parquet")
    pd.DataFrame({"record_id": ["r1", "r2", "r3"],
                  "entity_id": ["E1", "E1", "E2"]}).to_parquet(
        run / "entities.parquet")

    (run / "config" / "ruleset.json").write_text('{"version": 1}', encoding="utf-8")
    (run / "config" / "linkage_settings.json").write_text(json.dumps({
        "match_probability_threshold_high": 0.95,
        "match_probability_threshold_review": 0.5,
    }), encoding="utf-8")
    (run / "entity_report.json").write_text(
        json.dumps({"entities_proposed": 2, "attribute_ties": 0}), encoding="utf-8")
    return run


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "data" / "linkage.db")


def test_adopting_a_folder_writes_a_complete_run(run_folder, db_path):
    result = adopt_run.adopt(run_folder, db_path, label="PSC full")
    assert result["adopted"] is True

    rows = query_db(db_path, "SELECT * FROM runs")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "psc_full"
    assert row["status"] == "complete"
    assert row["label"] == "PSC full"
    assert row["triggered_by"] == "adopt_run.py"
    assert row["threshold_high"] == 0.95
    assert row["threshold_review"] == 0.5
    assert row["finished_at"] and row["started_at"]


def test_the_counts_come_off_the_folder_when_none_are_given(run_folder, db_path):
    adopt_run.adopt(run_folder, db_path)
    counts = json.loads(query_db(db_path, "SELECT counts_json FROM runs")[0]["counts_json"])
    assert counts["records_total"] == 3
    assert counts["units_total"] == 2
    assert counts["units_person"] == 1
    assert counts["clusters_total"] == 1
    assert counts["pairs_scored"] == 1
    assert counts["pairs_accept"] == 1
    assert counts["entities_proposed"] == 2


def test_given_counts_win_over_the_derived_ones(run_folder, db_path):
    adopt_run.adopt(run_folder, db_path,
                    counts={"units_total": 11_799_425, "pairs_accept": 42})
    counts = json.loads(query_db(db_path, "SELECT counts_json FROM runs")[0]["counts_json"])
    assert counts["units_total"] == 11_799_425
    assert counts["pairs_accept"] == 42
    # and anything the caller left out is still derived
    assert counts["records_total"] == 3


def test_the_config_snapshot_becomes_a_config_version(run_folder, db_path):
    result = adopt_run.adopt(run_folder, db_path)
    versions = query_db(db_path, "SELECT * FROM config_versions")
    assert len(versions) == 1
    assert versions[0]["version"] == result["config_version"]
    assert versions[0]["ruleset"] == '{"version": 1}'


def test_a_second_folder_with_the_same_config_reuses_the_version(run_folder, db_path,
                                                                 tmp_path):
    first = adopt_run.adopt(run_folder, db_path)
    import shutil

    second = run_folder.parent / "psc_full_2"
    shutil.copytree(run_folder, second)
    again = adopt_run.adopt(second, db_path)
    assert again["config_version"] == first["config_version"]
    assert len(query_db(db_path, "SELECT * FROM config_versions")) == 1


def test_adopting_twice_is_idempotent(run_folder, db_path):
    adopt_run.adopt(run_folder, db_path, label="PSC full")
    again = adopt_run.adopt(run_folder, db_path, label="PSC full")
    assert again["adopted"] is False
    assert "already registered" in again["message"]
    assert len(query_db(db_path, "SELECT * FROM runs")) == 1


def test_it_refuses_to_overwrite_a_different_run_of_the_same_id(run_folder, db_path):
    adopt_run.adopt(run_folder, db_path, label="PSC full")
    with pytest.raises(adopt_run.AdoptError, match="Refusing to overwrite"):
        adopt_run.adopt(run_folder, db_path, label="something else")
    assert query_db(db_path, "SELECT label FROM runs")[0]["label"] == "PSC full"


def test_an_unfinished_folder_is_refused_and_names_what_is_missing(run_folder, db_path):
    (run_folder / "entities.parquet").unlink()
    with pytest.raises(adopt_run.AdoptError, match="entities.parquet"):
        adopt_run.adopt(run_folder, db_path)
    assert not os.path.exists(db_path), "nothing is written for a folder it refuses"


def test_a_folder_that_does_not_exist_is_refused(tmp_path, db_path):
    with pytest.raises(adopt_run.AdoptError, match="No such run folder"):
        adopt_run.adopt(tmp_path / "nope", db_path)


def test_the_command_line_finds_the_database_beside_the_runs_directory(run_folder,
                                                                      capsys):
    assert adopt_run.main([str(run_folder)]) == 0
    assert "registered" in capsys.readouterr().out
    expected = run_folder.parent.parent / "linkage.db"
    assert query_db(str(expected), "SELECT id FROM runs")[0]["id"] == "psc_full"


def test_the_command_line_exits_non_zero_on_a_clash(run_folder, capsys):
    assert adopt_run.main([str(run_folder)]) == 0
    assert adopt_run.main([str(run_folder), "--label", "other"]) == 2
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_an_adopted_run_is_visible_to_the_runs_api(run_folder, monkeypatch):
    """The point of the script: the web tool finds it like any other run."""
    from fastapi.testclient import TestClient

    import app.auth as auth_mod
    import app.main as main_mod

    data_dir = run_folder.parent.parent
    db_path = str(data_dir / "linkage.db")
    adopt_run.adopt(run_folder, db_path, label="PSC full")
    monkeypatch.setattr(main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom"},
    )

    client = TestClient(main_mod.app, cookies={"session": "fake", "user": "fake"})
    response = client.get("/api/runs")
    assert response.status_code == 200
    ids = [r["id"] for r in response.json()]
    assert "psc_full" in ids


def test_the_default_database_is_the_one_the_app_opens(run_folder):
    """`adopt_run` with no --db must write where `app.main` will read.

    The application opens ``<DATA_DIR>/linkage.db``. A default of anything else
    registers the run into a file nothing looks at, and the only symptom is an
    empty runs list — which is exactly what the full PSC run hit.
    """
    from app import main as app_main

    assert adopt_run.DB_FILENAME == Path(app_main.DB_PATH).name

    adopt_run.main([str(run_folder)])
    expected = run_folder.parent.parent / adopt_run.DB_FILENAME
    assert expected.is_file()
    rows = query_db(str(expected), "SELECT * FROM runs")
    assert [row["id"] for row in rows] == ["psc_full"]

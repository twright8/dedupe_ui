"""What produced a run, and what a file says about itself — items B5 to B9, B13.

Three questions these tests hold the code to:

* what went into this run, and what read it (the manifest)
* which lines put a pair in its bucket, and who moved them (the history)
* can someone who has only the export answer both (the run sheet)
"""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.services import bucketing_history, run_manifest


# ---------------------------------------------------------------------------
# The code version
# ---------------------------------------------------------------------------


def test_the_code_version_is_the_commit_when_there_is_a_git_folder(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    assert run_manifest.code_version(tmp_path) == "a" * 40


def test_a_packed_ref_is_read_too(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        + "b" * 40 + " refs/heads/main\n", encoding="utf-8")
    assert run_manifest.code_version(tmp_path) == "b" * 40


def test_a_detached_head_is_the_commit_itself(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("c" * 40 + "\n", encoding="utf-8")
    assert run_manifest.code_version(tmp_path) == "c" * 40


def test_without_a_git_folder_the_environment_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "2026.09.21")
    assert run_manifest.code_version(tmp_path) == "2026.09.21"


def test_with_neither_it_says_unknown_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert run_manifest.code_version(tmp_path) == "unknown"


def test_the_real_checkout_has_a_commit():
    """The server deploys by git checkout, so this must not read 'unknown'."""
    version = run_manifest.code_version()
    assert version != "unknown"
    assert len(version) >= 7


# ---------------------------------------------------------------------------
# The input file
# ---------------------------------------------------------------------------


def test_the_input_file_is_hashed_and_sized(tmp_path):
    path = tmp_path / "donations.csv"
    path.write_text("DonorId,DonorName\n1,Ann Smith\n", encoding="utf-8")
    facts = run_manifest.input_facts(path, uploaded_at="2026-09-18T09:00:00+00:00")
    assert facts["filename"] == "donations.csv"
    assert facts["size_bytes"] == path.stat().st_size
    assert len(facts["sha256"]) == 64
    assert facts["uploaded_at"] == "2026-09-18T09:00:00+00:00"


def test_two_files_with_the_same_name_and_different_contents_hash_differently(tmp_path):
    a, b = tmp_path / "a" / "in.csv", tmp_path / "b" / "in.csv"
    a.parent.mkdir(); b.parent.mkdir()
    a.write_text("x\n1\n", encoding="utf-8")
    b.write_text("x\n2\n", encoding="utf-8")
    assert run_manifest.sha256_of(a) != run_manifest.sha256_of(b)


def test_a_missing_file_is_reported_and_never_raises(tmp_path):
    facts = run_manifest.input_facts(tmp_path / "gone.csv")
    assert facts["filename"] == "gone.csv"
    assert facts["sha256"] is None


# ---------------------------------------------------------------------------
# The manifest as a whole
# ---------------------------------------------------------------------------


def test_the_manifest_names_everything_a_defence_needs(tmp_path):
    run_dir = tmp_path / "runs" / "run_x"
    run_dir.mkdir(parents=True)
    source = tmp_path / "in.csv"
    source.write_text("a\n1\n", encoding="utf-8")

    manifest = run_manifest.build(
        run_id="run_x", run_dir=run_dir, config_version=4, input_path=source,
        thresholds={"accept_line": 0.92, "review_line": 0.5,
                    "lowest_score_kept": 0.05},
        triggered_by="Tom", uploaded_at="2026-09-18T09:00:00+00:00",
    )
    run_manifest.write(run_dir, manifest)
    read_back = run_manifest.read(run_dir)

    assert read_back["config_version"] == 4
    assert read_back["triggered_by"] == "Tom"
    assert read_back["code_version"]
    assert read_back["thresholds"]["accept_line"] == 0.92
    assert read_back["thresholds"]["lowest_score_kept"] == 0.05
    assert read_back["input"]["sha256"]
    # The libraries whose version changes an answer.
    for library in ("pandas", "duckdb", "splink", "lightgbm"):
        assert library in read_back["libraries"]
    assert read_back["finished_at"] is None

    run_manifest.finish(run_dir, counts={"records_total": 12}, row_count=12)
    closed = run_manifest.read(run_dir)
    assert closed["finished_at"]
    assert closed["input"]["row_count"] == 12


def test_a_manifest_that_cannot_be_written_does_not_raise(tmp_path):
    """Losing the provenance of a run is bad. Losing the run is worse."""
    blocked = tmp_path / "file-not-a-folder"
    blocked.write_text("x", encoding="utf-8")
    assert run_manifest.write(blocked, {"run_id": "x"}) is None


# ---------------------------------------------------------------------------
# The bucketing history
# ---------------------------------------------------------------------------


def test_the_history_records_every_change_and_names_the_one_in_force(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert bucketing_history.current(run_dir) is None

    bucketing_history.append(run_dir, "scored", accept_line=0.92, review_line=0.5,
                             lowest_score_kept=0.05, scorer="splink",
                             counts={"pairs_accept": 10, "pairs_review": 3,
                                     "pairs_reject": 40},
                             who="system")
    bucketing_history.append(run_dir, "re-bucketed", accept_line=0.85,
                             review_line=0.4, lowest_score_kept=0.05,
                             scorer="splink",
                             counts={"pairs_accept": 18, "pairs_review": 6,
                                     "pairs_reject": 29},
                             who="Tom")

    history = bucketing_history.read(run_dir)
    assert len(history) == 2
    assert [entry["action"] for entry in history] == ["scored", "re-bucketed"]

    now = bucketing_history.current(run_dir)
    assert now["accept_line"] == 0.85
    assert now["who"] == "Tom"
    assert now["counts"] == {"accept": 18, "review": 6, "reject": 29}
    assert now["at"]


def test_applying_a_model_says_which_version(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bucketing_history.append(run_dir, "model applied", accept_line=0.9,
                             review_line=0.4, scorer="model",
                             model_version={"person": 3, "organisation": 2},
                             who="Tom")
    now = bucketing_history.current(run_dir)
    assert now["scorer"] == "model"
    assert now["model_version"] == {"person": 3, "organisation": 2}


def test_the_history_does_not_grow_without_limit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for index in range(bucketing_history.MAX_ENTRIES + 10):
        bucketing_history.append(run_dir, "re-bucketed", accept_line=index / 1000)
    history = bucketing_history.read(run_dir)
    assert len(history) == bucketing_history.MAX_ENTRIES
    assert history[-1]["accept_line"] == (bucketing_history.MAX_ENTRIES + 9) / 1000


# ---------------------------------------------------------------------------
# The model version travels with the score
# ---------------------------------------------------------------------------


def test_the_model_version_is_written_beside_the_score():
    """A rescore must not be able to erase which model produced a number."""
    from app.pipeline.dedupe import stage_3b_model

    assert stage_3b_model.MODEL_VERSION_COLUMN == "gbt_model_version"
    source = (
        __import__("inspect").getsource(stage_3b_model.score_pairs)
    )
    assert "MODEL_VERSION_COLUMN] = int(model.version)" in source


# ---------------------------------------------------------------------------
# The export run sheet
# ---------------------------------------------------------------------------


def test_the_run_sheet_uses_plain_names_and_never_a_raw_count_key():
    from app.profiles import export_provenance

    rows = export_provenance.run_rows({
        "run_id": "run_x",
        "scope": "published",
        "config_version": 4,
        "exported_by": "Tom",
        "exported_at": "2026-09-21T10:00:00+00:00",
        "published_at": "2026-09-20T10:00:00+00:00",
        "manifest": {
            "started_at": "2026-09-18T09:00:00+00:00",
            "finished_at": "2026-09-18T10:00:00+00:00",
            "triggered_by": "Tom",
            "code_version": "d45df0e",
            "thresholds": {"accept_line": 0.92, "review_line": 0.5,
                           "lowest_score_kept": 0.05},
            "input": {"filename": "donations.csv", "size_bytes": 1234,
                      "sha256": "a" * 64, "row_count": 94141,
                      "uploaded_at": "2026-09-18T08:00:00+00:00"},
            "libraries": {"pandas": "3.0.6", "duckdb": "1.5.5"},
            "references": [{"name": "uk_name_frequencies",
                            "label": "Name-frequency table", "rows": 4196886,
                            "built_at": "2026-09-18T16:03:06+00:00",
                            "sha256": "b" * 64}],
            "scorers": {"person": {"scorer": "model", "model_version": 3,
                                   "graded": True, "trained_at": "2026-09-19",
                                   "n_human_labels": 757}},
        },
        "counts": {"pairs_vetoed_from_accept": 12, "entities_proposed": 17568,
                   "clusters_by_status": {"too_large": 2, "weak_link": 5}},
    })
    labels = {str(row[0]) for row in rows}

    assert "Code version" in labels
    assert "Input file sha256" in labels
    assert "Accept line" in labels and "Lowest score kept" in labels
    assert "Exported by" in labels
    assert "Pairs a veto rule stopped being accepted" in labels
    assert "Entities proposed" in labels
    assert "Clusters for review — Too large" in labels
    assert "Scored by — People" in labels
    assert "Model version — People" in labels
    assert "Name-frequency table — sha256" in labels
    assert "pandas" in labels

    # No camelCase and no snake_case reaches a reader.
    for label in labels:
        assert "_" not in label, f"raw key on the run sheet: {label!r}"
        assert not (label[:1].islower() and any(c.isupper() for c in label)), label


def test_an_unknown_count_still_reads_as_a_sentence():
    from app.profiles import export_provenance

    assert export_provenance.count_label("some_new_count") == "Some new count"
    assert export_provenance.count_label("someNewCount") == "Some new count"


def test_the_how_to_read_sheet_defines_every_added_column_and_the_list():
    from app.profiles import export_provenance

    rows = export_provenance.how_to_read_rows(
        export_provenance.DONATIONS_ADDED_COLUMNS)
    first = {str(row[0]) for row in rows}
    for name, _ in export_provenance.DONATIONS_ADDED_COLUMNS:
        assert name in first
    for label in ("On its own", "Match key", "Score", "Veto rule",
                  "Earlier grouping", "Reviewer", "Suggested"):
        assert label in first
    # And the three questions that are not "how it was decided".
    assert "Most members" in first
    assert "Survived a merge" in first


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "runs").mkdir(parents=True)
    (d / "uploads").mkdir(parents=True)
    return d


@pytest.fixture
def client(db_path, data_dir, monkeypatch):
    import app.auth as _auth_mod
    import app.main as _main_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign",
        lambda token, max_age=None: {"authenticated": True, "name": "Tom"},
    )
    return TestClient(_main_mod.app, cookies={"session": "fake", "user": "fake"})


def test_the_manifest_endpoint_answers_for_a_run_that_predates_it(client, db_path,
                                                                  data_dir):
    from app.db import write_db

    write_db(db_path,
             "INSERT INTO runs (id, status, config_version, input_filename, "
             "threshold_high, threshold_review) VALUES (?,?,?,?,?,?)",
             ("run_old", "complete", 2, "donations.csv", 0.92, 0.5))
    (data_dir / "runs" / "run_old").mkdir(parents=True)

    body = client.get("/api/runs/run_old/manifest").json()
    assert body["partial"] is True
    assert body["code_version"] == "unknown"
    assert body["input"]["filename"] == "donations.csv"
    assert body["thresholds"]["accept_line"] == 0.92
    assert body["bucketing"] == []


def test_the_manifest_endpoint_serves_the_file_and_the_history(client, db_path,
                                                               data_dir):
    from app.db import write_db

    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?,?)",
             ("run_m", "complete"))
    run_dir = data_dir / "runs" / "run_m"
    run_dir.mkdir(parents=True)
    source = data_dir / "uploads" / "in.csv"
    source.write_text("a\n1\n", encoding="utf-8")
    run_manifest.write(run_dir, run_manifest.build(
        run_id="run_m", run_dir=run_dir, config_version=3, input_path=source,
        thresholds={"accept_line": 0.9, "review_line": 0.4,
                    "lowest_score_kept": 0.05},
        triggered_by="Tom"))
    bucketing_history.append(run_dir, "scored", accept_line=0.9, review_line=0.4,
                             scorer="splink", who="system")

    body = client.get("/api/runs/run_m/manifest").json()
    assert body.get("partial") is not True
    assert body["input"]["sha256"]
    assert body["libraries"]["pandas"]
    assert body["bucketing_now"]["action"] == "scored"


def test_saving_the_methodology_notes_is_audited(client, db_path):
    from app.db import query_db

    client.put("/api/notes/methodology", json={"content": "<p>How we decide.</p>"})
    rows = query_db(db_path,
                    "SELECT * FROM audit_log WHERE description LIKE '%methodology%'")
    assert len(rows) == 1
    assert rows[0]["user_name"] == "Tom"
    assert json.loads(rows[0]["metadata_json"])["characters"] > 0


def test_adopting_a_run_is_audited(tmp_path, db_path):
    """A PSC run built offline used to leave no trace of who or when."""
    from app.db import query_db
    from scripts import adopt_run

    run_dir = tmp_path / "runs" / "psc_full"
    (run_dir / "config").mkdir(parents=True)
    for name in adopt_run.REQUIRED:
        pd.DataFrame({"record_id": ["1"], "entity_id": ["1"], "unit_id": ["1"],
                      "cluster_id": ["C-1"], "unit_id_l": ["1"], "unit_id_r": ["2"],
                      "track": ["person"]}).to_parquet(run_dir / name, index=False)
    (run_dir / "config" / "ruleset.json").write_text("{}", encoding="utf-8")
    (run_dir / "config" / "linkage_settings.json").write_text("{}", encoding="utf-8")

    adopt_run.adopt(run_dir, db_path, who="Tom")
    rows = query_db(db_path, "SELECT * FROM audit_log WHERE kind = 'run'")
    assert any("Adopted run psc_full" in (row["description"] or "") for row in rows)
    assert any(row["user_name"] == "Tom" for row in rows)


# ---------------------------------------------------------------------------
# One word for which score decided, wherever it is asked for
# ---------------------------------------------------------------------------


def test_the_score_column_and_its_word_come_from_the_runs_own_state(tmp_path):
    from app.services import pairs_reader

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # No model applied.
    answer = pairs_reader.score_column_for(run_dir)
    assert answer == {"score_column": "match_probability",
                      "score_column_label": "Splink score",
                      "scorer": "splink", "model_version": None}

    (run_dir / "model_state.json").write_text(json.dumps({
        "applied": True,
        "tracks": {"person": {"version": 3, "graded": True}},
    }), encoding="utf-8")
    answer = pairs_reader.score_column_for(run_dir)
    assert answer["score_column"] == "gbt_score"
    assert answer["score_column_label"] == "Model score"
    assert answer["scorer"] == "model"
    assert answer["model_version"] == {"person": 3}


def test_a_model_that_was_taken_off_reads_as_splink_again(tmp_path):
    from app.services import pairs_reader

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "model_state.json").write_text(json.dumps({
        "applied": False, "tracks": {},
    }), encoding="utf-8")
    assert pairs_reader.score_column_for(run_dir)["scorer"] == "splink"


def test_the_diagnostics_say_which_score_without_a_histogram(client, db_path,
                                                             data_dir):
    from app.db import write_db

    write_db(db_path,
             "INSERT INTO runs (id, status, threshold_high, threshold_review) "
             "VALUES (?,?,?,?)", ("run_d", "complete", 0.92, 0.5))
    (data_dir / "runs" / "run_d").mkdir(parents=True)

    body = client.get("/api/runs/run_d/diagnostics").json()
    assert body["score_column"] == "match_probability"
    assert body["score_column_label"] == "Splink score"
    assert body["scorer"] == "splink"


def test_the_model_panel_says_which_score_without_a_histogram(client):
    body = client.get("/api/model/person").json()
    assert body["scorer"] == "splink"
    assert body["score_column"] == "match_probability"
    assert body["score_column_label"] == "Splink score"


def test_the_words_are_the_vocabularys(client):
    """The three places must agree, and agree with GET /api/vocabulary."""
    from app import vocabulary
    from app.services import pairs_reader

    served = {entry["value"]: entry["label"]
              for entry in vocabulary.as_dict()["fields"]["scorer"]["values"]}
    for column, meta in pairs_reader.SCORE_COLUMNS.items():
        assert served[meta["scorer"]] == meta["label"], column


# ---------------------------------------------------------------------------
# The accept line each track used
# ---------------------------------------------------------------------------


def test_the_manifest_records_the_accept_line_of_every_track():
    """One number cannot answer "which line put this pair here" once the two
    tracks read different lines, so the manifest records them all."""
    manifest = run_manifest.build(
        run_id="r1", run_dir="/tmp/nowhere", config_version=3, input_path=None,
        thresholds={"accept_line": 0.92, "review_line": 0.50,
                    "lowest_score_kept": 0.05,
                    "accept_line_by_track": {"person": 0.96}},
    )
    assert manifest["thresholds"] == {
        "accept_line": 0.92,
        "accept_line_by_track": {"person": 0.96},
        "review_line": 0.50,
        "lowest_score_kept": 0.05,
    }


def test_a_manifest_from_before_the_lines_could_differ_still_reads():
    manifest = run_manifest.build(
        run_id="r1", run_dir="/tmp/nowhere", config_version=3, input_path=None,
        thresholds={"accept_line": 0.92, "review_line": 0.50},
    )
    assert manifest["thresholds"]["accept_line_by_track"] == {}


def test_the_history_records_the_accept_line_of_every_track(tmp_path):
    entry = bucketing_history.append(
        tmp_path, "scored", accept_line=0.92, review_line=0.50,
        accept_line_by_track={"person": 0.96}, scorer="splink", who="system",
    )
    assert entry["accept_line"] == 0.92
    assert entry["accept_line_by_track"] == {"person": 0.96}
    assert bucketing_history.read(tmp_path)[-1]["accept_line_by_track"] == \
        {"person": 0.96}


def test_a_run_snapshots_the_per_track_lines_it_was_started_with(tmp_path):
    """The run's own config folder is what stage 3 reads, so what the user
    chose has to land there and not only in the database row."""
    from app.services.pipeline_runner import _write_config_files

    config_row = {"ruleset": {}, "linkage_settings": json.dumps({
        "match_probability_threshold_high": 0.92,
        "match_probability_threshold_high_by_track": {"person": 0.96},
        "tracks": {"person": {}, "organisation": {}},
    })}
    config_dir = tmp_path / "config"

    # No per-track lines sent: the version's own survive, so moving the shared
    # line alone does not quietly wipe them.
    _write_config_files(config_row, config_dir, threshold_high=0.93)
    written = json.loads((config_dir / "linkage_settings.json").read_text())
    assert written["match_probability_threshold_high"] == 0.93
    assert written["match_probability_threshold_high_by_track"] == {"person": 0.96}

    # Per-track lines sent: they replace what the version had.
    _write_config_files(config_row, config_dir, threshold_high=0.92,
                        threshold_high_by_track={"person": 0.98,
                                                 "organisation": 0.94})
    written = json.loads((config_dir / "linkage_settings.json").read_text())
    assert written["match_probability_threshold_high_by_track"] == {
        "person": 0.98, "organisation": 0.94}

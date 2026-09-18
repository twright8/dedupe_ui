"""Stage 0 (load), and the config version 1 seeded from the profile defaults."""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.main as _main_mod
from app.db import write_db
from app.pipeline.dedupe.stage_0_load import run_stage_0_load
from app.rules import engine
from app.services.config_manager import get_current


@pytest.fixture
def input_csv(tmp_path):
    path = tmp_path / "donations.csv"
    pd.DataFrame([
        {"DonorId": 1, "DonorName": "Alice Smith", "DonorStatus": "Individual", "Value": 10},
        {"DonorId": 2, "DonorName": "Acme Ltd", "DonorStatus": "Company", "Value": 20},
    ]).to_csv(path, index=False)
    return path


def test_stage_0_writes_the_raw_records_parquet(tmp_path, input_csv):
    run_dir = tmp_path / "run"
    stats = run_stage_0_load(run_dir=str(run_dir), input_path=str(input_csv))

    parquet = run_dir / "records_raw.parquet"
    assert parquet.is_file()
    assert not (run_dir / "records.parquet").exists()  # stage 1 writes that one

    records = pd.read_parquet(parquet)
    assert len(records) == 2
    assert "track" not in records.columns
    assert stats["records_total"] == 2
    assert stats["input_rows"] == 2


def test_stage_0_emits_the_usual_progress_events(tmp_path, input_csv):
    events = []
    run_stage_0_load(
        run_dir=str(tmp_path / "run"),
        input_path=str(input_csv),
        progress_callback=lambda kind, detail: events.append((kind, detail)),
    )

    kinds = [kind for kind, _ in events]
    assert kinds[0] == "stage_start"
    assert kinds[-1] == "stage_end"

    start = events[0][1]
    assert start == {"stage": 0, "name": "load"}

    end = events[-1][1]
    assert end["stage"] == 0 and end["name"] == "load"
    assert "elapsed_seconds" in end
    assert end["records_total"] == 2  # the stats ride along with the stage_end


def test_stage_0_rejects_a_frame_without_the_shared_columns(tmp_path, input_csv, monkeypatch):
    """A profile that forgets a shared column must fail here, not three screens later."""
    import app.pipeline.dedupe.stage_0_load as stage

    class BrokenProfile:
        key = "broken"

        def load_records(self, path):
            return pd.DataFrame({"record_id": ["1"]}), {}

    monkeypatch.setattr(stage, "get_profile", lambda: BrokenProfile())
    with pytest.raises(ValueError, match="missing required column"):
        run_stage_0_load(run_dir=str(tmp_path / "run"), input_path=str(input_csv))


# ---------------------------------------------------------------------------
# Config seeding
# ---------------------------------------------------------------------------


def test_config_version_1_is_seeded_from_the_profile_defaults(db_path, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    _main_mod._seed_initial_config()

    config = get_current(db_path)
    assert config is not None
    assert config["version"] == 1
    assert "donations profile" in config["note"]

    ruleset = config["ruleset"]
    assert ruleset["default_track"] == "organisation"
    assert "person_titles" in ruleset["token_lists"]
    assert "nicknames" in ruleset["lookups"]
    assert [r["id"] for r in ruleset["track_rules"]] == ["t1", "t2", "t3", "t4"]
    assert ruleset["vetoes"] == []

    settings = json.loads(config["linkage_settings"])
    assert "match_probability_threshold_high" in settings
    assert set(settings["tracks"]) == {"person", "organisation"}
    for track in settings["tracks"].values():
        assert track["blocking_rules"] and track["comparisons"]


def test_the_seeded_ruleset_validates_against_the_profile(db_path, monkeypatch):
    """The shipped default must be a ruleset the app would also accept from a
    user — otherwise version 1 cannot be edited and re-saved."""
    from app.profiles import get_profile

    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    _main_mod._seed_initial_config()

    ruleset = get_current(db_path)["ruleset"]
    assert engine.validate_ruleset(ruleset, get_profile().raw_columns) == []


def test_seeding_is_skipped_when_a_config_already_exists(db_path, monkeypatch):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    _main_mod._seed_initial_config()
    _main_mod._seed_initial_config()

    assert get_current(db_path)["version"] == 1


def test_a_version_with_no_ruleset_is_upgraded_by_seeding_a_new_one(db_path, monkeypatch, caplog):
    """The dev database has versions from before the ruleset existed. Seeding
    cannot invent one, so it adds a fresh version and says so in the log."""
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    write_db(
        db_path,
        "INSERT INTO config_versions (created_by, note, linkage_settings) VALUES (?, ?, ?)",
        ("someone", "pre-ruleset config", "{}"),
    )

    _main_mod._seed_initial_config()

    config = get_current(db_path)
    assert config["version"] == 2
    assert config["ruleset"]["default_track"] == "organisation"
    assert "predates the ruleset" in config["note"]
    assert "has no ruleset" in caplog.text

    # And it is not done twice.
    _main_mod._seed_initial_config()
    assert get_current(db_path)["version"] == 2


def test_missing_defaults_folder_logs_a_warning_and_skips(db_path, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "PROFILE_DEFAULTS_DIR", tmp_path / "nowhere")

    _main_mod._seed_initial_config()

    assert get_current(db_path) is None
    assert "skipping initial config seed" in caplog.text

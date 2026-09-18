"""Two things, both about controls that must not go dead.

`_write_config_files` still honours the `cross_jurisdiction_name_matching`
argument: the run's config snapshot must differ when it is False. The run
creation API no longer sends it — one dataset has no second jurisdiction side —
so what the API half now guards is the single input file: it must survive
Pydantic and reach `enqueue_run` (this repo has a history of dead run-creation
controls silently dropped by Pydantic — CONCERNS_LOG H/I/J).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import app.main as _main_mod
import app.auth as _auth_mod
from app.db import write_db
from app.services import pipeline_runner
from app.services.pipeline_runner import NAME_CORE_BLOCKING_RULE, _write_config_files


def _config_row(blocking_rules):
    """A minimal config_versions-style row (JSON string fields)."""
    return {
        "name_rules": json.dumps([{"pattern": "-", "replace": " "}]),
        "legal_tokens": json.dumps(["LTD"]),
        "jurisdiction_map": json.dumps(
            [{"source_dataset": "ocod", "raw_value": "JERSEY", "standardised_value": "JERSEY"}]
        ),
        "linkage_settings": json.dumps({
            "blocking_rules": blocking_rules,
            "match_probability_threshold_high": 0.9,
            "match_probability_threshold_review": 0.4,
        }),
    }


def _written_blocking_rules(config_dir):
    settings = json.loads((config_dir / "linkage_settings.json").read_text(encoding="utf-8"))
    return settings["blocking_rules"]


def test_snapshot_includes_name_rule_when_enabled(tmp_path):
    cfg = tmp_path / "on"
    _write_config_files(_config_row(["l.jurisdiction_clean = r.jurisdiction_clean"]), cfg,
                        cross_jurisdiction_name_matching=True)
    assert NAME_CORE_BLOCKING_RULE in _written_blocking_rules(cfg)


def test_snapshot_strips_name_rule_when_disabled(tmp_path):
    # Base config already contains the name_core rule; disabling must remove it.
    cfg = tmp_path / "off"
    _write_config_files(
        _config_row([
            "l.jurisdiction_clean = r.jurisdiction_clean AND l.jurisdiction_clean <> 'UNKNOWN'",
            NAME_CORE_BLOCKING_RULE,
        ]),
        cfg,
        cross_jurisdiction_name_matching=False,
    )
    rules = _written_blocking_rules(cfg)
    assert all("name_core" not in r for r in rules)
    assert len(rules) == 1


def test_snapshot_differs_between_flag_values(tmp_path):
    on, off = tmp_path / "a", tmp_path / "b"
    base = ["l.jurisdiction_clean = r.jurisdiction_clean"]
    _write_config_files(_config_row(base), on, cross_jurisdiction_name_matching=True)
    _write_config_files(_config_row(base), off, cross_jurisdiction_name_matching=False)
    assert _written_blocking_rules(on) != _written_blocking_rules(off)


# ---------------------------------------------------------------------------
# API wiring: the input file must survive Pydantic and reach enqueue_run.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    (data_dir / "runs").mkdir(parents=True)
    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(_auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True})
    from fastapi.testclient import TestClient
    return TestClient(_main_mod.app, cookies={"session": "fake"}), data_dir


def _seed_config(db_path):
    from app.services.config_manager import save_version
    return save_version(
        db_path, created_by="test", note="t",
        name_rules=[{"pattern": "-", "replace": " "}],
        jurisdiction_map=[{"source_dataset": "ocod", "raw_value": "JERSEY", "standardised_value": "JERSEY"}],
        legal_tokens=["LTD"],
        linkage_settings={"match_probability_threshold_high": 0.9, "match_probability_threshold_review": 0.4},
    )


def test_input_filename_reaches_enqueue_run(client, db_path, monkeypatch):
    tc, data_dir = client
    _seed_config(db_path)
    (data_dir / "uploads" / "donations.xlsx").write_bytes(b"fake")
    (data_dir / "uploads" / "other.csv").write_bytes(b"fake")

    captured = {}
    monkeypatch.setattr(pipeline_runner, "enqueue_run", lambda **kw: captured.update(kw))

    r = tc.post("/api/runs", json={
        "input_filename": "donations.xlsx", "config_version": 1,
    })
    assert r.status_code == 200, r.text
    assert captured["input_path"].endswith("donations.xlsx")
    assert "ocod_path" not in captured and "ch_path" not in captured


def test_input_upload_id_reaches_enqueue_run(client, db_path, monkeypatch):
    """The upload-session route to the same file must arrive too."""
    tc, data_dir = client
    _seed_config(db_path)
    (data_dir / "uploads" / "stored.xlsx").write_bytes(b"fake")
    write_db(
        db_path,
        """INSERT INTO upload_sessions
           (upload_id, filename, stored_filename, total_chunks, chunks_received, status)
           VALUES (?, ?, ?, 1, 1, 'complete')""",
        ("up-1", "donations.xlsx", "stored.xlsx"),
    )

    captured = {}
    monkeypatch.setattr(pipeline_runner, "enqueue_run", lambda **kw: captured.update(kw))

    r = tc.post("/api/runs", json={"input_upload_id": "up-1", "config_version": 1})
    assert r.status_code == 200, r.text
    assert captured["input_path"].endswith("stored.xlsx")
    assert r.json()["input_filename"] == "donations.xlsx"

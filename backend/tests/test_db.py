# backend/tests/test_db.py
import sqlite3
from app.db import get_db, init_db

def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "runs" in tables
    assert "config_versions" in tables
    assert "labels" in tables
    assert "audit_log" in tables
    assert "users" in tables

def test_init_db_creates_indexes(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    indexes = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "idx_labels_key" in indexes
    assert "idx_labels_raw_key_unique" in indexes
    assert "idx_labels_raw_key" not in indexes  # replaced by the unique variant
    assert "idx_labels_active_reviewer" in indexes
    assert "idx_audit_timestamp" in indexes
    assert "idx_runs_started" in indexes

def test_wal_mode_enabled(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


# Pre-unique-index labels schema, as an old database would have it.
_OLD_LABELS_SQL = """
CREATE TABLE labels (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ocod_name_clean       TEXT,
    jurisdiction_clean    TEXT,
    roe_company_number    TEXT,
    ocod_name_raw         TEXT,
    ocod_jurisdiction_raw TEXT,
    is_true_match         TEXT,
    reviewer              TEXT,
    reviewer_notes        TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    run_id                TEXT,
    active                INTEGER NOT NULL DEFAULT 1,
    provenance            TEXT,
    held_out              INTEGER NOT NULL DEFAULT 0,
    superseded_by         INTEGER
);
"""


def test_init_db_dedupes_duplicate_active_raw_labels(tmp_path):
    """Migration: an old DB where a cleaning-rule change left the same raw
    identity with TWO active labels (different clean keys, so the clean unique
    index never fired). init_db keeps the newest, supersedes the older, and can
    then create the unique raw-key index."""
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_LABELS_SQL)
    insert = """INSERT INTO labels
        (ocod_name_clean, jurisdiction_clean, roe_company_number,
         ocod_name_raw, ocod_jurisdiction_raw, is_true_match, reviewer, created_at, active)
        VALUES (?,?,?,?,?,?,?,?,1)"""
    # Same raw triple under two clean keys (old rules vs new rules), plus an
    # unrelated label that must be left alone.
    conn.execute(insert, ("ACME UK LIMITED", "ENGLAND", "OE000001",
                          "Acme (UK) Limited", "England", "TRUE", "Alice",
                          "2026-01-01T00:00:00"))
    conn.execute(insert, ("ACME UK LTD", "ENGLAND", "OE000001",
                          "Acme (UK) Limited", "England", "FALSE", "Bob",
                          "2026-02-01T00:00:00"))
    conn.execute(insert, ("OTHER LTD", "JERSEY", "OE000002",
                          "Other Ltd", "Jersey", "TRUE", "Alice",
                          "2026-01-15T00:00:00"))
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM labels")}
    # Newest of the duplicated pair kept active; older superseded, not deleted.
    assert rows[2]["active"] == 1
    assert rows[1]["active"] == 0
    assert rows[1]["superseded_by"] == 2
    # The unrelated label is untouched.
    assert rows[3]["active"] == 1 and rows[3]["superseded_by"] is None
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}
    assert "idx_labels_raw_key_unique" in indexes
    conn.close()

    # Idempotent: a second init_db is a no-op on an already-deduped DB.
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    active = conn.execute("SELECT COUNT(*) FROM labels WHERE active = 1").fetchone()[0]
    conn.close()
    assert active == 2

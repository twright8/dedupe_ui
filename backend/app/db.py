# backend/app/db.py
"""SQLite database helpers — schema, connection pool, thread-safe writes."""

import logging
import sqlite3
import threading
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_connections: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    label           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT,
    finished_at     TEXT,
    duration_secs   REAL,
    triggered_by    TEXT,
    config_version  INTEGER,
    ocod_filename   TEXT,
    ch_filename     TEXT,
    error_message   TEXT,
    error_detail_json TEXT,
    counts_json     TEXT,
    threshold_high  REAL,
    threshold_review REAL,
    current_stage   TEXT
);

CREATE TABLE IF NOT EXISTS config_versions (
    version         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    note            TEXT,
    name_rules      TEXT,
    jurisdiction_map TEXT,
    legal_tokens    TEXT,
    linkage_settings TEXT
);

CREATE TABLE IF NOT EXISTS labels (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ocod_name_clean       TEXT,
    jurisdiction_clean    TEXT,
    roe_company_number    TEXT,
    ocod_name_raw         TEXT,
    ocod_jurisdiction_raw TEXT,
    is_true_match         TEXT,                      -- 'TRUE' | 'FALSE' | 'UNCERTAIN'
    reviewer              TEXT,
    reviewer_notes        TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    run_id                TEXT,
    active                INTEGER NOT NULL DEFAULT 1,
    provenance            TEXT,                      -- manual | bulk_range | llm | proxy | implied_negative | import
    held_out              INTEGER NOT NULL DEFAULT 0,-- 1 = frozen eval set, excluded from training
    superseded_by         INTEGER                    -- id of the label row that replaced this one
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    user_name       TEXT,
    kind            TEXT NOT NULL,
    description     TEXT,
    metadata_json   TEXT
);

CREATE TABLE IF NOT EXISTS users (
    name            TEXT PRIMARY KEY,
    initials        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS upload_sessions (
    upload_id       TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    stored_filename TEXT,
    total_chunks    INTEGER NOT NULL,
    chunks_received INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'uploading',
    error_message   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);

-- Shared, app-wide key/value settings (e.g. the methodology notes shown on the
-- review screen). One row per key; the value is read/written by every user.
CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_key
    ON labels (ocod_name_clean, jurisdiction_clean, roe_company_number)
    WHERE active = 1;

-- Raw values are the durable label identity (cleaning rules are editable
-- config, so cleaned keys can drift). At most one ACTIVE label per raw triple;
-- legacy clean-only rows (no raw name) are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_raw_key_unique
    ON labels (ocod_name_raw, ocod_jurisdiction_raw, roe_company_number)
    WHERE active = 1 AND ocod_name_raw IS NOT NULL AND ocod_name_raw != '';

CREATE INDEX IF NOT EXISTS idx_labels_active_reviewer
    ON labels (active, reviewer);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp
    ON audit_log (timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_runs_started
    ON runs (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_upload_sessions_status
    ON upload_sessions (status, created_at DESC);
"""


# Idempotent additive migrations for DBs created before a column existed.
# (table, column, type) — applied only if the column is missing.
_MIGRATIONS = [
    ("runs", "error_detail_json", "TEXT"),
    ("labels", "provenance", "TEXT"),
    ("labels", "held_out", "INTEGER NOT NULL DEFAULT 0"),
    ("labels", "superseded_by", "INTEGER"),
    ("labels", "roe_name", "TEXT"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, col_type in _MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _dedupe_active_raw_labels(conn: sqlite3.Connection) -> None:
    """One-off migration guard for ``idx_labels_raw_key_unique``.

    A cleaning-rule change used to let the same raw (name, jurisdiction, ROE)
    triple acquire a second ACTIVE label under its new clean key. Before the
    active-only unique index on the raw triple can be created, deactivate all
    but the newest label in each duplicated group (append-only: rows are
    superseded, never deleted) and log what was done.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "labels" not in tables:
        return  # fresh database — nothing to migrate
    cols = {row[1] for row in conn.execute("PRAGMA table_info(labels)")}
    groups = conn.execute(
        """SELECT group_concat(id) FROM labels
           WHERE active = 1 AND ocod_name_raw IS NOT NULL AND ocod_name_raw != ''
           GROUP BY upper(trim(ocod_name_raw)),
                    upper(trim(coalesce(ocod_jurisdiction_raw, ''))),
                    upper(trim(roe_company_number))
           HAVING COUNT(*) > 1"""
    ).fetchall()
    for (ids_csv,) in groups:
        ids = [int(x) for x in ids_csv.split(",")]
        marks = ",".join("?" * len(ids))
        ordered = [row[0] for row in conn.execute(
            f"SELECT id FROM labels WHERE id IN ({marks}) ORDER BY created_at DESC, id DESC",
            ids,
        )]
        keeper, losers = ordered[0], ordered[1:]
        loser_marks = ",".join("?" * len(losers))
        if "superseded_by" in cols:
            conn.execute(
                f"UPDATE labels SET active = 0, superseded_by = ? WHERE id IN ({loser_marks})",
                [keeper, *losers],
            )
        else:  # pre-supersede schema; the column is added by _apply_migrations later
            conn.execute(f"UPDATE labels SET active = 0 WHERE id IN ({loser_marks})", losers)
        logger.warning(
            "labels migration: raw identity had %d active labels; kept id=%d, deactivated ids=%s",
            len(ordered), keeper, losers,
        )


def init_db(db_path: str) -> None:
    """Create schema, run additive migrations, and enable WAL mode."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _dedupe_active_raw_labels(conn)  # must precede the unique raw-key index
    conn.executescript(SCHEMA)
    # Superseded by idx_labels_raw_key_unique (same columns, now unique).
    conn.execute("DROP INDEX IF EXISTS idx_labels_raw_key")
    _apply_migrations(conn)
    conn.commit()
    conn.close()


def _get_or_create_conn(db_path: str) -> sqlite3.Connection:
    """Internal: get or create connection. Caller must hold _lock."""
    if db_path not in _connections:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _connections[db_path] = conn
    return _connections[db_path]


def get_db(db_path: str) -> sqlite3.Connection:
    """Return a thread-safe singleton connection with Row factory."""
    with _lock:
        return _get_or_create_conn(db_path)


def write_db(db_path: str, sql: str, params: tuple[Any, ...] = ()) -> int:
    """Thread-safe write. Returns lastrowid."""
    with _lock:
        conn = _get_or_create_conn(db_path)
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.lastrowid


def query_db(db_path: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    """Query and return list of dicts."""
    with _lock:
        conn = _get_or_create_conn(db_path)
        cursor = conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def _reset_db() -> None:
    """For tests only — close and discard all singleton connections."""
    with _lock:
        for conn in _connections.values():
            try:
                conn.close()
            except Exception:
                pass
        _connections.clear()

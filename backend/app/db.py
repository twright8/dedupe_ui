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
    input_filename  TEXT,
    -- Dead. They belonged to the two-dataset tool this app was copied from,
    -- which was deleted on 2026-09-21 (docs/DESIGN.md D21). Nothing reads or
    -- writes them. They stay because dropping a column is destructive and the
    -- rows cost nothing.
    ocod_filename   TEXT,
    ch_filename     TEXT,
    error_message   TEXT,
    error_detail_json TEXT,
    counts_json     TEXT,
    threshold_high  REAL,
    threshold_review REAL,
    current_stage   TEXT,
    -- What produced this run. The full record is run_manifest.json in the run
    -- folder; these five are here so a list view and an export can read them
    -- without opening a file (docs/PROVENANCE.md).
    code_version    TEXT,
    input_sha256    TEXT,
    input_bytes     INTEGER,
    input_rows      INTEGER,
    input_uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS config_versions (
    version         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    note            TEXT,
    -- The whole user-editable rules document (docs/RULESET.md).
    ruleset         TEXT,
    linkage_settings TEXT,
    -- Written by the two-dataset linkage tool this app was copied from. Kept so
    -- old rows still read; every new version leaves all three null.
    name_rules      TEXT,
    jurisdiction_map TEXT,
    legal_tokens    TEXT
);

-- Dead, like the two columns above: the two-dataset tool's own label table,
-- replaced by `pair_labels`. Nothing reads or writes it. Kept, not dropped, so
-- the rows a researcher may still want to read are still there.
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
    provenance            TEXT,
    held_out              INTEGER NOT NULL DEFAULT 0,-- 1 = frozen eval set, excluded from training
    superseded_by         INTEGER                    -- id of the label row that replaced this one
);

-- Human decisions about two RECORDS of one dataset (docs/LINKAGE.md, D10).
-- Append-only: a new decision on the same pair deactivates the old row and
-- points its superseded_by at the new one, so disagreement stays on record.
-- record_id_a is always the smaller of the two ids compared as strings.
CREATE TABLE IF NOT EXISTS pair_labels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id_a     TEXT NOT NULL,
    record_id_b     TEXT NOT NULL,
    track           TEXT,
    is_match        TEXT NOT NULL,             -- 'TRUE' | 'FALSE'
    provenance      TEXT,                      -- manual | bulk_range | llm | import
                                               -- | cluster_merge | cluster_split
    held_out        INTEGER NOT NULL DEFAULT 0,-- 1 = frozen eval set, excluded from training
    reviewer        TEXT,
    notes           TEXT,
    evidence_url    TEXT,                      -- a source link for outside evidence (D13a)
    name_a          TEXT,
    name_b          TEXT,
    run_id          TEXT,
    config_version  INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    active          INTEGER NOT NULL DEFAULT 1,
    superseded_by   INTEGER,
    -- Every label a single group decision wrote shares one id, so the decision
    -- can be shown, superseded or undone as a whole (docs/ENTITIES.md).
    decision_id     TEXT,
    -- The cluster or held exact group the decision was about, so it can be
    -- found, superseded and undone as a whole.
    decision_scope  TEXT
);

-- The durable registry (docs/ENTITIES.md). A run never writes it; publishing a
-- run does, in one transaction. Entity IDs outlive runs, so a retired ID stays
-- as a redirect to the entity that absorbed it rather than being deleted.
CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    track           TEXT,
    created_run     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'active',   -- active | retired
    alias_of        TEXT,                             -- the survivor, when retired
    retired_run     TEXT
);

CREATE TABLE IF NOT EXISTS entity_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    since_run       TEXT,
    until_run       TEXT,                             -- null while current
    -- Why this record is in this entity, and where the entity's ID came from.
    -- Both are copied from the run's proposal at publish, so "how was this
    -- decided" survives the deletion of the run folder (docs/PROVENANCE.md).
    entity_basis    TEXT,                             -- app/vocabulary.ENTITY_BASES
    id_status       TEXT                              -- app/vocabulary.ID_STATUSES
);

-- One settled value per entity per column, versioned like entity_members.
-- Publishing closes the live row and opens a new one, so an earlier answer and
-- the run that gave it stay on the record (gap 8).
CREATE TABLE IF NOT EXISTS entity_attributes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL,
    column_name     TEXT NOT NULL,
    value           TEXT,
    basis           TEXT,                             -- app/vocabulary.ATTRIBUTE_BASES
    run_id          TEXT,
    since_run       TEXT,
    until_run       TEXT,                             -- null while current
    -- Which derived-column rule won, when the basis is `rule`.
    rule_id         TEXT,
    -- How the members voted, when the basis is `majority` or `tie`:
    -- {"value": count, ...} as JSON, so a tie can be shown, not just named.
    tally_json      TEXT
);

-- The join log (gap 2). One row per accepted link inside a published entity,
-- written at publish and never rebuilt. An exact-group merge is logged as star
-- edges from the group's smallest record id, not as every pair: a group of
-- 1,000 records is 999 rows here and 499,500 the other way.
--
-- Sized for PSC: tens of millions of rows, loaded in one transaction with the
-- indexes dropped first and rebuilt after (app/registry/provenance.py).
CREATE TABLE IF NOT EXISTS entity_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    -- One of app/vocabulary.EDGE_SOURCES, so the chain reads in the same
    -- words as every screen.
    source          TEXT NOT NULL,
    record_id_a     TEXT NOT NULL,
    record_id_b     TEXT NOT NULL,
    unit_id_a       TEXT,
    unit_id_b       TEXT,
    -- Match-key merges
    match_key       TEXT,                             -- the key's name
    match_key_id    TEXT,
    group_id        TEXT,
    -- Score links
    score           REAL,
    scorer          TEXT,                             -- splink | model
    model_version   TEXT,
    -- A veto a reviewer overrode when they joined these two anyway
    veto_overridden TEXT,
    veto_reason     TEXT,
    -- Reviewer links
    label_id        INTEGER,
    reviewer        TEXT,
    decided_at      TEXT,
    note            TEXT,
    evidence_url    TEXT,
    -- Earlier-grouping links
    earlier_entity_id TEXT,
    config_version  INTEGER
);

-- Two proposed entities that claimed one ID, copied out of the run's
-- entity_report.json so it survives the run folder (gap 8).
CREATE TABLE IF NOT EXISTS entity_id_collisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    entity_id         TEXT,
    kept_by_key       TEXT,
    n_records_kept    INTEGER,
    minted            TEXT,
    minted_for_key    TEXT,
    n_records_minted  INTEGER,
    from_registry     INTEGER,
    recorded_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which runs have been published. Publishing the same run twice is then a
-- no-op, and publishing an older run over a newer one can be refused.
CREATE TABLE IF NOT EXISTS entity_publications (
    run_id          TEXT PRIMARY KEY,
    published_at    TEXT NOT NULL DEFAULT (datetime('now')),
    published_by    TEXT,
    summary_json    TEXT
);

-- A reviewer settling a consensus column the entity could not settle itself
-- (docs/ENTITIES_API.md). Stored per RECORD so it outlives the entity: a rerun
-- may put those records in a different cluster, and the decision still holds.
CREATE TABLE IF NOT EXISTS attribute_overrides (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       TEXT NOT NULL,
    column_name     TEXT NOT NULL,
    value           TEXT,
    reviewer        TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    active          INTEGER NOT NULL DEFAULT 1,
    superseded_by   INTEGER,
    decision_id     TEXT
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
    completed_at    TEXT,
    -- The sha256 of the reassembled file, taken once at upload. A run copies
    -- it onto its own row, so a file swapped under the same name shows up.
    sha256          TEXT
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

-- At most one ACTIVE decision per pair of records. The ids are normalised on
-- the way in (pair_labels.pair_key), so this index is the last line of defence
-- against two live opinions on one pair.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pair_labels_active_key
    ON pair_labels (record_id_a, record_id_b)
    WHERE active = 1;

CREATE INDEX IF NOT EXISTS idx_pair_labels_active
    ON pair_labels (active, id DESC);

-- A record belongs to one entity at a time; the index is on the live row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_members_current
    ON entity_members (record_id)
    WHERE until_run IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_attribute_overrides_active
    ON attribute_overrides (record_id, column_name)
    WHERE active = 1;

CREATE INDEX IF NOT EXISTS idx_attribute_overrides_decision
    ON attribute_overrides (decision_id);

CREATE INDEX IF NOT EXISTS idx_entity_members_entity
    ON entity_members (entity_id, until_run);


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
    ("runs", "input_filename", "TEXT"),
    ("labels", "provenance", "TEXT"),
    ("labels", "held_out", "INTEGER NOT NULL DEFAULT 0"),
    ("labels", "superseded_by", "INTEGER"),
    ("labels", "roe_name", "TEXT"),
    ("config_versions", "ruleset", "TEXT"),
    ("pair_labels", "decision_id", "TEXT"),
    ("pair_labels", "decision_scope", "TEXT"),
    ("entity_members", "entity_basis", "TEXT"),
    ("entity_members", "id_status", "TEXT"),
    ("entity_attributes", "since_run", "TEXT"),
    ("entity_attributes", "until_run", "TEXT"),
    ("entity_attributes", "rule_id", "TEXT"),
    ("entity_attributes", "tally_json", "TEXT"),
    # What produced a run (docs/PROVENANCE.md). The whole record is
    # run_manifest.json in the run folder; these four are on the row so a list
    # view and an export can read them without opening a file.
    ("runs", "code_version", "TEXT"),
    ("runs", "input_sha256", "TEXT"),
    ("runs", "input_bytes", "INTEGER"),
    ("runs", "input_rows", "INTEGER"),
    ("runs", "input_uploaded_at", "TEXT"),
    ("upload_sessions", "sha256", "TEXT"),
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
    # Indexes over a migrated column have to wait for the column to exist: on a
    # database made before slice 4 the schema above runs first and pair_labels
    # has no decision_id yet.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pair_labels_decision "
        "ON pair_labels (decision_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pair_labels_scope "
        "ON pair_labels (decision_scope, active)"
    )
    # entity_attributes used to hold one row per entity and column, replaced in
    # place. It now keeps history, so the unique index moves onto the live row —
    # the same shape entity_members has used since it was written.
    conn.execute("DROP INDEX IF EXISTS idx_entity_attributes_key")
    conn.execute(
        "UPDATE entity_attributes SET since_run = run_id "
        "WHERE since_run IS NULL AND run_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_attributes_current "
        "ON entity_attributes (entity_id, column_name) WHERE until_run IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_id_collisions_run "
        "ON entity_id_collisions (run_id)"
    )
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

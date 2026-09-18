# backend/app/services/attribute_overrides.py
"""A reviewer settling a consensus column the entity could not settle itself.

An attribute tie is two equally supported values and no way to choose between
them (`docs/ENTITIES.md`). A human picks one, and the choice is stored per
**record**, not per entity: a later run may put those records in a different
cluster, and the decision is about the records, not about that day's grouping —
the same reasoning as a pair label.

Append-only, like every other human decision here.
"""

from datetime import datetime, timezone

import pandas as pd

from app.db import query_db, write_db

BASIS = "human"


def active_overrides(db_path: str) -> pd.DataFrame:
    """The live overrides as a frame, ready to join onto the records."""
    rows = [dict(row) for row in query_db(
        db_path, "SELECT * FROM attribute_overrides WHERE active = 1 ORDER BY id"
    )]
    columns = ["id", "record_id", "column_name", "value", "reviewer", "notes",
               "created_at", "decision_id"]
    frame = pd.DataFrame(rows, columns=columns) if rows \
        else pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
    frame["record_id"] = frame["record_id"].astype(str)
    return frame


def save_overrides(
    db_path: str,
    record_ids,
    column: str,
    value,
    reviewer: str,
    notes: str | None = None,
    decision_id: str | None = None,
) -> int:
    """Set one column for a set of records. Returns how many rows were written."""
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for record_id in sorted({str(r) for r in record_ids if str(r or "").strip()}):
        write_db(
            db_path,
            "UPDATE attribute_overrides SET active = 0 "
            "WHERE active = 1 AND record_id = ? AND column_name = ?",
            (record_id, column),
        )
        write_db(
            db_path,
            """INSERT INTO attribute_overrides
                   (record_id, column_name, value, reviewer, notes, created_at,
                    active, decision_id)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (record_id, column, None if value is None else str(value), reviewer,
             notes or None, now, decision_id),
        )
        written += 1
    return written


def withdraw_decision(db_path: str, decision_id: str) -> int:
    """Deactivate every override one decision wrote."""
    rows = query_db(
        db_path,
        "SELECT COUNT(*) AS n FROM attribute_overrides "
        "WHERE active = 1 AND decision_id = ?",
        (decision_id,),
    )
    write_db(
        db_path,
        "UPDATE attribute_overrides SET active = 0 WHERE active = 1 AND decision_id = ?",
        (decision_id,),
    )
    return int(rows[0]["n"]) if rows else 0


def latest_for_scope(db_path: str, decision_ids) -> list[dict]:
    ids = [d for d in decision_ids if d]
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    return [dict(row) for row in query_db(
        db_path,
        f"SELECT * FROM attribute_overrides WHERE active = 1 "
        f"AND decision_id IN ({marks}) ORDER BY id",
        tuple(ids),
    )]

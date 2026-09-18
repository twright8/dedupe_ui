# backend/app/services/pair_labels.py
"""The `pair_labels` table: read it, write it, and turn it into an overlay.

A label is a statement about two **records**, not about a run (`DESIGN.md` D10).
The table is append-only: a new decision on a pair deactivates the old row and
points its ``superseded_by`` at the new one, so who said what and when survives
every later change of mind.

``pair_key`` is the one place the two ids are put in order. Everything that
reads or writes a label goes through it, so the pair (a, b) and the pair (b, a)
can never become two labels.
"""

import csv as csvmod
import io
import re
from datetime import datetime, timezone

import pandas as pd

from app.db import query_db, write_db

VERDICTS = ("TRUE", "FALSE")
PROVENANCES = ("manual", "bulk_range", "llm", "import")
# What a reviewer's own screen may send. `llm` and `import` are written by the
# machine, never by a person clicking a button.
HUMAN_PROVENANCES = ("manual", "bulk_range")

MAX_BATCH = 500

PAIR_ID_SEPARATOR = "|"

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

CSV_COLUMNS = [
    "record_id_a", "record_id_b", "track", "is_match", "provenance", "held_out",
    "reviewer", "notes", "evidence_url", "name_a", "name_b", "run_id",
    "config_version", "created_at",
]

LABEL_FIELDS = ("is_match", "reviewer", "created_at", "notes", "evidence_url",
                "provenance", "held_out")


class LabelError(ValueError):
    """A label the caller may not store, with a message fit to show them."""


# ---------------------------------------------------------------------------
# The one key helper
# ---------------------------------------------------------------------------


def pair_key(a, b) -> tuple[str, str]:
    """``(smaller, larger)`` of two record ids, compared as strings.

    Every read and every write goes through here. Record ids compare as text so
    the key does not depend on a profile's ids happening to be numeric — the
    same rule stage 2 uses for group ids.
    """
    left, right = str(a or "").strip(), str(b or "").strip()
    if not left or not right:
        raise LabelError("A label needs two record ids")
    if left == right:
        raise LabelError("A label needs two different records")
    return (left, right) if left < right else (right, left)


def split_pair_id(pair_id: str) -> tuple[str, str]:
    """``(a, b)`` from ``<id>|<id>``, already in order."""
    left, separator, right = str(pair_id or "").partition(PAIR_ID_SEPARATOR)
    if not separator:
        raise LabelError(
            f"A pair id is '<unit_id_l>{PAIR_ID_SEPARATOR}<unit_id_r>'"
        )
    return pair_key(left, right)


def pair_id_of(a, b) -> str:
    left, right = pair_key(a, b)
    return f"{left}{PAIR_ID_SEPARATOR}{right}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalise_verdict(value) -> str:
    verdict = str(value or "").strip().upper()
    if verdict not in VERDICTS:
        raise LabelError(
            f"is_match must be TRUE or FALSE, not {value!r}"
        )
    return verdict


def normalise_evidence_url(value) -> str | None:
    """An http(s) link, or nothing. A half-typed URL is a mistake worth catching."""
    url = str(value or "").strip()
    if not url:
        return None
    if not _URL_RE.match(url):
        raise LabelError(
            f"evidence_url must start with http:// or https:// — got {value!r}"
        )
    return url


def normalise_provenance(value, allowed=PROVENANCES, default="manual") -> str:
    provenance = str(value or default).strip().lower()
    if provenance not in allowed:
        raise LabelError(
            f"provenance must be one of {', '.join(allowed)}, not {value!r}"
        )
    return provenance


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def active_labels(db_path: str) -> list[dict]:
    return [dict(row) for row in query_db(
        db_path, "SELECT * FROM pair_labels WHERE active = 1 ORDER BY id"
    )]


def labels_frame(db_path: str) -> pd.DataFrame:
    """The active labels as a frame, ready to join onto pairs or units.

    Always carries the columns, even when empty, so no caller has to branch.
    """
    rows = active_labels(db_path)
    columns = ["id", "record_id_a", "record_id_b", "track", "is_match",
               "provenance", "held_out", "reviewer", "notes", "evidence_url",
               "name_a", "name_b", "run_id", "config_version", "created_at"]
    frame = pd.DataFrame(rows, columns=columns) if rows \
        else pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
    for column in ("record_id_a", "record_id_b"):
        frame[column] = frame[column].astype(str)
    return frame


def active_label(db_path: str, a, b) -> dict | None:
    left, right = pair_key(a, b)
    rows = query_db(
        db_path,
        "SELECT * FROM pair_labels WHERE active = 1 AND record_id_a = ? AND record_id_b = ?",
        (left, right),
    )
    return dict(rows[0]) if rows else None


def as_label(row: dict | None) -> dict | None:
    """The subset of a row the pairs API puts on an item."""
    if not row:
        return None
    return {field: row.get(field) for field in LABEL_FIELDS}


def library_counts(db_path: str) -> dict:
    """What the whole library holds, ignoring any filter the list applied."""
    row = query_db(
        db_path,
        """SELECT count(*) FILTER (WHERE active = 1) AS active,
                  count(*) FILTER (WHERE active = 1 AND upper(is_match) = 'TRUE') AS t,
                  count(*) FILTER (WHERE active = 1 AND upper(is_match) = 'FALSE') AS f,
                  count(*) FILTER (WHERE active = 1 AND held_out = 1) AS held_out,
                  count(*) FILTER (WHERE active = 1 AND track = 'person') AS person,
                  count(*) FILTER (WHERE active = 1 AND track = 'organisation') AS organisation,
                  count(*) FILTER (WHERE active = 1 AND provenance = 'manual') AS manual,
                  count(*) FILTER (WHERE active = 1 AND provenance = 'bulk_range') AS bulk_range,
                  count(*) FILTER (WHERE active = 1 AND provenance = 'llm') AS llm,
                  count(*) FILTER (WHERE active = 1 AND provenance = 'import') AS imported
           FROM pair_labels""",
    )[0]
    return {
        "active": row["active"], "true": row["t"], "false": row["f"],
        "held_out": row["held_out"], "person": row["person"],
        "organisation": row["organisation"], "manual": row["manual"],
        "bulk_range": row["bulk_range"], "llm": row["llm"], "import": row["imported"],
    }


def list_labels(
    db_path: str,
    track: str | None = None,
    is_match: str | None = None,
    provenance: str | None = None,
    reviewer: str | None = None,
    held_out: int | None = None,
    q: str | None = None,
    active: int = 1,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """One page of the library, newest first. ``counts`` ignore the filters."""
    where: list[str] = []
    params: list = []
    if active is not None:
        where.append("active = ?")
        params.append(int(active))
    if track is not None:
        where.append("track = ?")
        params.append(track)
    if is_match is not None:
        where.append("upper(is_match) = ?")
        params.append(normalise_verdict(is_match))
    if provenance is not None:
        where.append("provenance = ?")
        params.append(provenance)
    if reviewer is not None:
        where.append("reviewer = ?")
        params.append(reviewer)
    if held_out is not None:
        where.append("held_out = ?")
        params.append(int(held_out))
    if q:
        pattern = f"%{q.lower()}%"
        where.append(
            "(lower(coalesce(name_a, '')) LIKE ? OR lower(coalesce(name_b, '')) LIKE ? "
            "OR lower(record_id_a) LIKE ? OR lower(record_id_b) LIKE ? "
            "OR lower(coalesce(notes, '')) LIKE ? OR lower(coalesce(reviewer, '')) LIKE ?)"
        )
        params.extend([pattern] * 6)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    total = query_db(
        db_path, f"SELECT count(*) AS n FROM pair_labels{where_sql}", tuple(params)
    )[0]["n"]
    items = [dict(row) for row in query_db(
        db_path,
        f"SELECT * FROM pair_labels{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple([*params, int(limit), int(offset)]),
    )]
    return {
        "total": int(total), "offset": int(offset), "limit": int(limit),
        "items": items, "counts": library_counts(db_path),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save_label(
    db_path: str,
    a,
    b,
    is_match: str,
    reviewer: str,
    provenance: str = "manual",
    notes: str | None = None,
    evidence_url: str | None = None,
    track: str | None = None,
    name_a: str | None = None,
    name_b: str | None = None,
    run_id: str | None = None,
    config_version: int | None = None,
    created_at: str | None = None,
    held_out: int | None = None,
) -> tuple[dict, int]:
    """Append one decision. Returns ``(the new active row, rows superseded)``.

    ``held_out`` is inherited from the row this one replaces, so re-deciding a
    pair never drops an entity out of the frozen evaluation set — the rule
    ``roe_ui`` already follows. A new label starts at 0, meaning it teaches the
    model rather than tests it.
    """
    left, right = pair_key(a, b)
    verdict = normalise_verdict(is_match)
    url = normalise_evidence_url(evidence_url)
    source = normalise_provenance(provenance)
    now = created_at or datetime.now(timezone.utc).isoformat()

    prior = query_db(
        db_path,
        "SELECT id, held_out FROM pair_labels "
        "WHERE active = 1 AND record_id_a = ? AND record_id_b = ?",
        (left, right),
    )
    inherited = max((row["held_out"] or 0 for row in prior), default=0)
    if held_out is None:
        held_out = inherited

    # Deactivate first, so the active-only unique index never sees two rows.
    for row in prior:
        write_db(db_path, "UPDATE pair_labels SET active = 0 WHERE id = ?", (row["id"],))

    label_id = write_db(
        db_path,
        """INSERT INTO pair_labels
               (record_id_a, record_id_b, track, is_match, provenance, held_out,
                reviewer, notes, evidence_url, name_a, name_b, run_id,
                config_version, created_at, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (left, right, track, verdict, source, int(held_out or 0), reviewer,
         notes or None, url, name_a, name_b, run_id, config_version, now),
    )
    for row in prior:
        write_db(
            db_path, "UPDATE pair_labels SET superseded_by = ? WHERE id = ?",
            (label_id, row["id"]),
        )

    new_row = dict(query_db(
        db_path, "SELECT * FROM pair_labels WHERE id = ?", (label_id,)
    )[0])
    return new_row, len(prior)


def withdraw_label(db_path: str, a, b) -> dict | None:
    """Deactivate the active decision on a pair, keeping the row. None if there is none."""
    left, right = pair_key(a, b)
    rows = query_db(
        db_path,
        "SELECT * FROM pair_labels WHERE active = 1 AND record_id_a = ? AND record_id_b = ?",
        (left, right),
    )
    if not rows:
        return None
    label = dict(rows[0])
    write_db(db_path, "UPDATE pair_labels SET active = 0 WHERE id = ?", (label["id"],))
    return label


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def export_csv(db_path: str) -> str:
    buffer = io.StringIO()
    writer = csvmod.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in active_labels(db_path):
        writer.writerow({c: ("" if row.get(c) is None else row.get(c)) for c in CSV_COLUMNS})
    return buffer.getvalue()


def parse_csv(text: str) -> list[dict]:
    """The rows a CSV holds, with the row number kept for the rejection report."""
    reader = csvmod.DictReader(io.StringIO(text or ""))
    return [{**row, "_row": index} for index, row in enumerate(reader, start=2)]

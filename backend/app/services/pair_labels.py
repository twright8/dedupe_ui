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
import uuid
from datetime import datetime, timezone

import pandas as pd

from app.db import query_db, write_db

VERDICTS = ("TRUE", "FALSE")
PROVENANCES = ("manual", "bulk_range", "llm", "import", "cluster_merge",
               "cluster_split")
# What a reviewer's own screen may send on a single pair. `llm` and `import` are
# written by the machine; the two cluster provenances come from a whole-group
# decision, which has an endpoint of its own.
HUMAN_PROVENANCES = ("manual", "bulk_range")
# A group decision writes one of these (docs/ENTITIES.md).
DECISION_PROVENANCES = {"merge": "cluster_merge", "split": "cluster_split"}
DECISION_KINDS = tuple(DECISION_PROVENANCES)

MAX_BATCH = 500

PAIR_ID_SEPARATOR = "|"

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

CSV_COLUMNS = [
    "record_id_a", "record_id_b", "track", "is_match", "provenance", "held_out",
    "reviewer", "notes", "evidence_url", "name_a", "name_b", "run_id",
    "config_version", "created_at", "decision_id", "decision_scope",
]

LABEL_FIELDS = ("is_match", "reviewer", "created_at", "notes", "evidence_url",
                "provenance", "held_out", "decision_id")


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
               "name_a", "name_b", "run_id", "config_version", "created_at",
               "decision_id", "decision_scope"]
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
    decision_id: str | None = None,
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
    if decision_id is not None:
        where.append("decision_id = ?")
        params.append(decision_id)
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
    decision_id: str | None = None,
    decision_scope: str | None = None,
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
                config_version, created_at, active, decision_id, decision_scope)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (left, right, track, verdict, source, int(held_out or 0), reviewer,
         notes or None, url, name_a, name_b, run_id, config_version, now,
         decision_id, decision_scope),
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


def new_decision_id() -> str:
    """A short id every label of one group decision shares."""
    return "d_" + uuid.uuid4().hex[:12]


def star_pairs(parts: list[list[str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(the TRUE pairs, the FALSE pairs)`` a group decision writes.

    Inside a part, a star from its smallest record to each of the others: n−1
    labels instead of the n(n−1)/2 a clique would need, and enough, because a
    cluster is a connected component and a star connects the part. Between two
    parts, one FALSE between their smallest records, which is what keeps them
    apart when the exact keys or the scorer try to join them again.
    """
    cleaned = [sorted({str(r) for r in part if str(r or "").strip()}) for part in parts]
    cleaned = [part for part in cleaned if part]
    true_pairs: list[tuple[str, str]] = []
    for part in cleaned:
        head = part[0]
        true_pairs.extend(pair_key(head, other) for other in part[1:])
    false_pairs: list[tuple[str, str]] = []
    heads = [part[0] for part in cleaned]
    for index, left in enumerate(heads):
        for right in heads[index + 1:]:
            false_pairs.append(pair_key(left, right))
    return true_pairs, false_pairs


def save_decision(
    db_path: str,
    scope: str,
    kind: str,
    parts: list[list[str]],
    reviewer: str,
    names: dict | None = None,
    track: str | None = None,
    notes: str | None = None,
    evidence_url: str | None = None,
    run_id: str | None = None,
    config_version: int | None = None,
) -> dict:
    """Write one whole-group decision as ordinary labels sharing a decision id.

    A merge is one part. A split is several: a TRUE star inside each, and a
    FALSE between every two. Everything goes through ``save_label``, so the
    append-only rule, the unique active pair and the inherited Teaches/Tests
    role all still hold.
    """
    if kind not in DECISION_PROVENANCES:
        raise LabelError(f"kind must be one of {', '.join(DECISION_KINDS)}")
    true_pairs, false_pairs = star_pairs(parts)
    if kind == "split" and len(false_pairs) == 0:
        raise LabelError("A split needs at least two parts")
    written = true_pairs + false_pairs
    if not written:
        raise LabelError("That decision would not label anything")
    if len(written) > MAX_BATCH:
        raise LabelError(
            f"A decision writes at most {MAX_BATCH} labels; that one needs {len(written)}"
        )

    decision_id = new_decision_id()
    provenance = DECISION_PROVENANCES[kind]
    names = names or {}
    superseded = 0
    for verdict, pairs in (("TRUE", true_pairs), ("FALSE", false_pairs)):
        for left, right in pairs:
            _label, replaced = save_label(
                db_path, left, right, verdict, reviewer=reviewer,
                provenance=provenance, notes=notes, evidence_url=evidence_url,
                track=track, name_a=names.get(left), name_b=names.get(right),
                run_id=run_id, config_version=config_version,
                decision_id=decision_id, decision_scope=str(scope),
            )
            superseded += replaced
    return {
        "decision_id": decision_id,
        "kind": kind,
        "labels_written": len(written),
        "superseded": superseded,
    }


def latest_decision(db_path: str, scope: str) -> dict | None:
    """The newest active decision on a cluster or held group, or None."""
    rows = query_db(
        db_path,
        """SELECT decision_id, provenance, reviewer, created_at, notes, evidence_url,
                  COUNT(*) AS n_labels, MAX(id) AS newest
           FROM pair_labels
           WHERE active = 1 AND decision_scope = ? AND decision_id IS NOT NULL
           GROUP BY decision_id ORDER BY newest DESC LIMIT 1""",
        (str(scope),),
    )
    if not rows:
        return None
    row = dict(rows[0])
    kind = {v: k for k, v in DECISION_PROVENANCES.items()}.get(row["provenance"])
    return {
        "decision_id": row["decision_id"],
        "kind": kind,
        "reviewer": row["reviewer"],
        "created_at": row["created_at"],
        "n_labels": int(row["n_labels"]),
        "notes": row["notes"],
        "evidence_url": row["evidence_url"],
    }


def decisions_by_scope(db_path: str) -> dict[str, dict]:
    """The newest active decision for every scope that has one."""
    rows = query_db(
        db_path,
        """SELECT decision_scope, decision_id, provenance, reviewer, created_at,
                  notes, evidence_url, COUNT(*) AS n_labels, MAX(id) AS newest
           FROM pair_labels
           WHERE active = 1 AND decision_scope IS NOT NULL AND decision_id IS NOT NULL
           GROUP BY decision_scope, decision_id ORDER BY newest""",
        (),
    )
    kinds = {v: k for k, v in DECISION_PROVENANCES.items()}
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["decision_scope"]] = {
            "decision_id": row["decision_id"],
            "kind": kinds.get(row["provenance"]),
            "reviewer": row["reviewer"],
            "created_at": row["created_at"],
            "n_labels": int(row["n_labels"]),
            "notes": row["notes"],
            "evidence_url": row["evidence_url"],
        }
    return latest


def withdraw_decision(db_path: str, scope: str) -> dict | None:
    """Deactivate every label of the newest decision on *scope*, as a unit."""
    decision = latest_decision(db_path, scope)
    if decision is None:
        return None
    write_db(
        db_path,
        "UPDATE pair_labels SET active = 0 WHERE active = 1 AND decision_id = ?",
        (decision["decision_id"],),
    )
    return {**decision, "labels_withdrawn": decision["n_labels"]}


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

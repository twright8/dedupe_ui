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

from app import vocabulary
from app.db import query_db, write_db

VERDICTS = vocabulary.ANSWERS
PROVENANCES = vocabulary.LABEL_PROVENANCES
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
            "The answer must be Match or Not a match. The API carries those as "
            f"TRUE and FALSE; it was given {value!r}."
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


# What the list may be sorted by, and the SQL each one means. Anything else is
# refused rather than interpolated, because this string reaches the query.
SORTS = {
    "created_at": "created_at",
    "name": "lower(coalesce(name_a, ''))",
    "reviewer": "lower(coalesce(reviewer, ''))",
    "is_match": "upper(is_match)",
    "provenance": "coalesce(provenance, '')",
}
DEFAULT_SORT = "created_at"

# The same, for the grouped view, where a column is one decision's worth.
GROUP_SORTS = {
    "created_at": "created_at",
    "name": "lower(coalesce(first_name, ''))",
    "reviewer": "lower(coalesce(reviewer, ''))",
    "is_match": "n_true",
    "provenance": "coalesce(provenance, '')",
}

# How many member names a grouped row carries.
GROUP_NAMES = 4


def build_filters(
    track: str | None = None,
    is_match: str | None = None,
    provenance: str | None = None,
    reviewer: str | None = None,
    held_out: int | None = None,
    q: str | None = None,
    active: int | None = 1,
    decision_id: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> tuple[str, list]:
    """``(WHERE clause, parameters)`` — the one place a label filter is written.

    The list and the export share it, so a download is always exactly what was
    on screen. Every value is bound, never interpolated.
    """
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
    if created_from:
        # Inclusive, and a bare date means from its first moment.
        where.append("created_at >= ?")
        params.append(_from_bound(created_from))
    if created_to:
        # Inclusive, so a bare date covers the whole of that day.
        where.append("created_at <= ?")
        params.append(_to_bound(created_to))
    if q:
        pattern = f"%{q.lower()}%"
        where.append(
            "(lower(coalesce(name_a, '')) LIKE ? OR lower(coalesce(name_b, '')) LIKE ? "
            "OR lower(record_id_a) LIKE ? OR lower(record_id_b) LIKE ? "
            "OR lower(coalesce(notes, '')) LIKE ? OR lower(coalesce(reviewer, '')) LIKE ?)"
        )
        params.extend([pattern] * 6)
    return (f" WHERE {' AND '.join(where)}" if where else ""), params


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _from_bound(value: str) -> str:
    value = str(value).strip()
    return f"{value}T00:00:00" if _DATE_ONLY.match(value) else value


def _to_bound(value: str) -> str:
    """A bare date means the end of that day, so `to` is inclusive."""
    value = str(value).strip()
    return f"{value}T23:59:59.999999" if _DATE_ONLY.match(value) else value


def _order_by(sort: str | None, order: str | None, allowed: dict) -> str:
    key = (sort or DEFAULT_SORT).strip().lower()
    if key not in allowed:
        raise LabelError(f"sort must be one of {', '.join(allowed)}")
    direction = (order or "desc").strip().lower()
    if direction not in ("asc", "desc"):
        raise LabelError("order must be asc or desc")
    # id breaks every tie, so a page boundary never repeats or skips a row.
    return f"{allowed[key]} {direction.upper()}, id DESC"


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
    created_from: str | None = None,
    created_to: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    group_by: str | None = None,
) -> dict:
    """One page of the library. ``counts`` describe the whole library.

    ``group_by="decision"`` folds the labels one group decision wrote into a
    single row: a cluster merge can write a thousand of them, and a reviewer
    wants to see the decision, not the star it produced.
    """
    where_sql, params = build_filters(
        track, is_match, provenance, reviewer, held_out, q, active, decision_id,
        created_from, created_to,
    )
    if (group_by or "").strip().lower() == "decision":
        return _grouped(db_path, where_sql, params, offset, limit, sort, order)

    order_sql = _order_by(sort, order, SORTS)
    total = query_db(
        db_path, f"SELECT count(*) AS n FROM pair_labels{where_sql}", tuple(params)
    )[0]["n"]
    items = [dict(row) for row in query_db(
        db_path,
        f"SELECT * FROM pair_labels{where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        tuple([*params, int(limit), int(offset)]),
    )]
    return {
        "total": int(total), "offset": int(offset), "limit": int(limit),
        "grouped": False, "items": items, "counts": library_counts(db_path),
    }


def _grouped(db_path, where_sql, params, offset, limit, sort, order) -> dict:
    """The list with one row per decision, and single labels left as they are."""
    order_sql = _order_by(sort, order, GROUP_SORTS)
    # A label with no decision is its own group, so nothing is hidden by the mode.
    key = "coalesce(decision_id, 'label:' || id)"
    base = f"""
        SELECT {key} AS group_key,
               decision_id,
               max(decision_scope) AS decision_scope,
               count(*) AS n_labels,
               sum(CASE WHEN upper(is_match) = 'TRUE' THEN 1 ELSE 0 END) AS n_true,
               sum(CASE WHEN upper(is_match) = 'FALSE' THEN 1 ELSE 0 END) AS n_false,
               max(provenance) AS provenance,
               max(reviewer) AS reviewer,
               min(created_at) AS created_at,
               max(id) AS id,
               max(notes) AS notes,
               max(evidence_url) AS evidence_url,
               min(name_a) AS first_name
        FROM pair_labels{where_sql}
        GROUP BY {key}
    """
    total = query_db(
        db_path, f"SELECT count(*) AS n FROM ({base})", tuple(params)
    )[0]["n"]
    rows = [dict(row) for row in query_db(
        db_path, f"SELECT * FROM ({base}) ORDER BY {order_sql} LIMIT ? OFFSET ?",
        tuple([*params, int(limit), int(offset)]),
    )]

    names = _group_names(db_path, where_sql, params,
                         [row["group_key"] for row in rows])
    kinds = {v: k for k, v in DECISION_PROVENANCES.items()}
    items = []
    for row in rows:
        items.append({
            "decision_id": row["decision_id"],
            "decision_scope": row["decision_scope"],
            "kind": kinds.get(row["provenance"]),
            "provenance": row["provenance"],
            "n_labels": int(row["n_labels"]),
            "n_true": int(row["n_true"]),
            "n_false": int(row["n_false"]),
            "names": names.get(row["group_key"], []),
            "reviewer": row["reviewer"],
            "created_at": row["created_at"],
            "notes": row["notes"],
            "evidence_url": row["evidence_url"],
        })
    return {
        "total": int(total), "offset": int(offset), "limit": int(limit),
        "grouped": True, "items": items, "counts": library_counts(db_path),
    }


def _group_names(db_path, where_sql, params, group_keys) -> dict:
    """Up to four member names per group, for the page's groups only."""
    if not group_keys:
        return {}
    key = "coalesce(decision_id, 'label:' || id)"
    marks = ", ".join("?" * len(group_keys))
    rows = query_db(
        db_path,
        f"""SELECT {key} AS group_key, name_a, name_b
            FROM pair_labels{where_sql or " WHERE 1=1"}
              AND {key} IN ({marks})
            ORDER BY id""",
        tuple([*params, *group_keys]),
    )
    found: dict[str, list] = {}
    for row in rows:
        seen = found.setdefault(row["group_key"], [])
        for name in (row["name_a"], row["name_b"]):
            if name and name not in seen and len(seen) < GROUP_NAMES:
                seen.append(name)
    return found


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


# ---------------------------------------------------------------------------
# The frozen test set (docs/MODEL.md)
# ---------------------------------------------------------------------------

# Only a reviewer's own decision on that pair may be frozen. A cluster decision
# writes a star of labels at once, so putting them in a test set would grade the
# model on many correlated rows that one click produced, and an imported label
# was never confirmed in this UI at all (D11).
TEST_SET_PROVENANCES = HUMAN_PROVENANCES


def test_set(db_path: str, track: str | None = None) -> dict:
    """What the frozen test set holds, and what is left to designate.

    `MODEL.md`: the test set is human labels with ``held_out = 1``. It is never
    trained on, and grading and the accept and reject lines come from it alone.
    """
    where = "active = 1"
    params: list = []
    if track is not None:
        where += " AND track = ?"
        params.append(track)
    marks = ", ".join("?" * len(TEST_SET_PROVENANCES))
    row = query_db(
        db_path,
        f"""SELECT
              count(*) FILTER (WHERE held_out = 1) AS held_out,
              count(*) FILTER (WHERE held_out = 1 AND upper(is_match) = 'TRUE') AS t,
              count(*) FILTER (WHERE held_out = 1 AND upper(is_match) = 'FALSE') AS f,
              count(*) FILTER (WHERE held_out = 0) AS training,
              count(*) FILTER (WHERE held_out = 0 AND provenance IN ({marks}))
                AS designatable
            FROM pair_labels WHERE {where}""",
        tuple([*TEST_SET_PROVENANCES, *params]),
    )[0]
    return {
        "track": track,
        "total": int(row["held_out"]),
        # The API keeps TRUE and FALSE (docs/DESIGN.md D21). `by_answer` is the
        # same two numbers under the words a reviewer is shown, so no screen has
        # to know that TRUE means Match.
        "by_verdict": {"TRUE": int(row["t"]), "FALSE": int(row["f"])},
        "by_answer": {
            vocabulary.ANSWER["TRUE"]["label"]: int(row["t"]),
            vocabulary.ANSWER["FALSE"]["label"]: int(row["f"]),
        },
        "training": int(row["training"]),
        "designatable": int(row["designatable"]),
    }


def designate_test_set(db_path: str, n: int = 200, track: str | None = None) -> dict:
    """Freeze up to *n* human labels as the test set, balanced TRUE and FALSE.

    Two rules, both `roe_ui`'s and both earned.

    * Never take more than half of either verdict's labels. Designating a test
      set must never empty the training pool, which is what happened in `roe_ui`
      when there were only a handful of labels and the old code took them all.
    * Newest first, because the newest labels were made with the most context.

    Additive: a second call tops the set up rather than replacing it, so the
    frozen set only ever grows and a number already quoted stays quotable.
    """
    half = max(1, int(n) // 2)
    marks = ", ".join("?" * len(TEST_SET_PROVENANCES))
    where = f"active = 1 AND held_out = 0 AND provenance IN ({marks})"
    params: list = list(TEST_SET_PROVENANCES)
    if track is not None:
        where += " AND track = ?"
        params.append(track)

    chosen: list[int] = []
    left = 0
    for verdict in VERDICTS:
        rows = query_db(
            db_path,
            f"""SELECT id FROM pair_labels
                WHERE {where} AND upper(is_match) = ?
                ORDER BY id DESC""",
            tuple([*params, verdict]),
        )
        take = min(half, len(rows) // 2)
        chosen.extend(row["id"] for row in rows[:take])
        left += len(rows) - take

    for label_id in chosen:
        write_db(db_path, "UPDATE pair_labels SET held_out = 1 WHERE id = ?",
                 (label_id,))
    return {"designated": len(chosen), "left_for_training": left,
            **test_set(db_path, track)}


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


def withdraw_label_by_id(db_path: str, label_id: int) -> dict:
    """Deactivate one active label by its id, keeping the row.

    Append-only, like every other write here: the row stays and ``active``
    goes to 0, so who said what is never lost.

    A label that a group decision wrote is refused. Those are one answer about
    a whole cluster, written as a star of labels sharing a ``decision_id``;
    pulling one out would leave the decision half-undone and the screen unable
    to say so. The cluster screen undoes them as a unit.
    """
    rows = query_db(db_path, "SELECT * FROM pair_labels WHERE id = ?", (int(label_id),))
    if not rows:
        raise LabelError(f"No label {label_id} in the library")
    label = dict(rows[0])
    if not label["active"]:
        raise LabelError(
            f"Label {label_id} was already withdrawn, so there is nothing to undo"
        )
    if label.get("decision_id"):
        kind = {v: k for k, v in DECISION_PROVENANCES.items()}.get(
            label.get("provenance"), "group")
        scope = label.get("decision_scope") or "a cluster"
        raise LabelError(
            f"This answer is part of one {kind} decision about {scope} "
            f"(decision {label['decision_id']}). Undo the whole decision on the "
            "cluster screen; a single pair cannot be taken out of it."
        )
    write_db(db_path, "UPDATE pair_labels SET active = 0 WHERE id = ?",
             (label["id"],))
    return label


def freeze_labels(db_path: str, label_ids, track: str | None = None) -> dict:
    """Move named labels into the test set, keeping the rules ``designate`` keeps.

    The library used to let a reviewer move chosen labels between the training
    set and the test set by id. ``designate_test_set`` picks them itself, so
    there was no way to say "freeze these ones". This is that, and only that.

    The three rules hold whichever way a label is chosen:

    * only a reviewer's own answer, one at a time or from a brushed band, may
      be frozen. A machine-written one and a group decision may not.
    * never more than half of either answer, so freezing can never empty the
      training pool.
    * freezing is permanent. There is no unfreeze, because a number quoted off
      a frozen set has to stay quotable.
    """
    wanted = [int(value) for value in (label_ids or [])]
    if not wanted:
        raise LabelError("Name at least one label to freeze")
    marks = ",".join("?" * len(wanted))
    rows = query_db(
        db_path, f"SELECT * FROM pair_labels WHERE id IN ({marks})", tuple(wanted)
    )
    found = {int(row["id"]): dict(row) for row in rows}
    missing = [value for value in wanted if value not in found]
    if missing:
        raise LabelError(f"No label {missing[0]} in the library")

    refused: list[dict] = []
    candidates: list[dict] = []
    for value in wanted:
        label = found[value]
        if not label["active"]:
            refused.append({"label_id": value, "reason": "This answer was withdrawn"})
        elif label["held_out"]:
            refused.append({"label_id": value,
                            "reason": "This answer is already in the test set"})
        elif label["provenance"] not in TEST_SET_PROVENANCES:
            refused.append({
                "label_id": value,
                "reason": ("Only an answer a reviewer saved one at a time, or from "
                           "a band of scores, can go in the test set"),
            })
        elif track is not None and label["track"] != track:
            refused.append({"label_id": value,
                            "reason": f"This answer is not on the {track} track"})
        else:
            candidates.append(label)

    # Never more than half of either answer. The cap counts what is already
    # frozen, so two calls cannot do what one call is refused.
    marks = ", ".join("?" * len(TEST_SET_PROVENANCES))
    where = f"active = 1 AND provenance IN ({marks})"
    params: list = list(TEST_SET_PROVENANCES)
    if track is not None:
        where += " AND track = ?"
        params.append(track)

    frozen: list[int] = []
    for verdict in VERDICTS:
        row = query_db(
            db_path,
            f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE held_out = 1) AS held
                FROM pair_labels WHERE {where} AND upper(is_match) = ?""",
            tuple([*params, verdict]),
        )[0]
        room = max(0, int(row["total"]) // 2 - int(row["held"]))
        mine = [label for label in candidates
                if str(label["is_match"]).upper() == verdict]
        for label in mine[:room]:
            write_db(db_path, "UPDATE pair_labels SET held_out = 1 WHERE id = ?",
                     (label["id"],))
            frozen.append(int(label["id"]))
        for label in mine[room:]:
            refused.append({
                "label_id": int(label["id"]),
                "reason": (f"Freezing this would leave fewer than half the "
                           f"{ANSWER_LABELS[verdict]} answers to train on"),
            })

    return {"frozen": frozen, "refused": refused, **test_set(db_path, track)}


#: The two answers in the words a reviewer reads, for a message.
ANSWER_LABELS = {value: meta["label"] for value, meta in vocabulary.ANSWER.items()}


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


def export_rows(db_path: str, where_sql: str, params: list, sort=None, order=None):
    """Yield the CSV a line at a time, so a large download holds no more than a row.

    The filters are the list's own, so the file is always what was on screen.
    """
    # Validated before the first byte, so a bad sort is a 400 and not a
    # half-written download the caller has to notice for themselves.
    order_sql = _order_by(sort, order, SORTS)
    header = io.StringIO()
    csvmod.DictWriter(header, fieldnames=CSV_COLUMNS).writeheader()
    yield header.getvalue()
    for row in query_db(
        db_path, f"SELECT * FROM pair_labels{where_sql} ORDER BY {order_sql}",
        tuple(params),
    ):
        line = io.StringIO()
        csvmod.DictWriter(line, fieldnames=CSV_COLUMNS, extrasaction="ignore").writerow(
            {c: ("" if dict(row).get(c) is None else dict(row).get(c))
             for c in CSV_COLUMNS}
        )
        yield line.getvalue()


def export_csv(db_path: str, where_sql: str = "", params: list | None = None,
               sort=None, order=None) -> str:
    """The whole file as one string. Tests use it; the endpoint streams instead."""
    if not where_sql:
        where_sql, params = build_filters()
    return "".join(export_rows(db_path, where_sql, params or [], sort, order))


def parse_csv(text: str) -> list[dict]:
    """The rows a CSV holds, with the row number kept for the rejection report."""
    reader = csvmod.DictReader(io.StringIO(text or ""))
    return [{**row, "_row": index} for index, row in enumerate(reader, start=2)]

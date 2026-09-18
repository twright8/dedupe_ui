# backend/app/services/pairs_reader.py
"""Filtered, sorted, paginated reads of a run's pairs.parquet.

The review screen shows two units side by side, so every query joins the pairs
to the units twice. DuckDB reads the parquet files in place and hands each unit
back as one struct, which keeps the SQL short and stops the two sides' columns
colliding.

Nothing here loads a frame into pandas. The contract these functions serve is
written out in ``docs/PAIRS_API.md``.
"""

import json
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from app.profiles import get_profile
from app.services.records_reader import describe_columns

PAIRS_FILENAME = "pairs.parquet"
UNITS_FILENAME = "units.parquet"
UNIT_MEMBERS_FILENAME = "unit_members.parquet"
RECORDS_FILENAME = "records.parquet"
EVENTS_FILENAME = "events.parquet"
SCORE_EVAL_FILENAME = "score_eval.json"
BLOCKING_REPORT_FILENAME = "blocking_report.json"

DEFAULT_SORT = "score"
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_MEMBERS = 200
MAX_EVENTS = 200

DEFAULT_BINS = 50
MAX_BINS = 200

TRACKS = ("person", "organisation")
BUCKETS = ("accept", "review", "reject")
# `model` joins the three when a graded model is what put the pair in its bucket
# (docs/MODEL.md, stage 3b).
DECIDED_BY = ("score", "import", "human", "model")
IMPORT_STATES = ("agrees", "disagrees", "unknown")
HELD_STATES = ("hide", "only")
LABELLED_STATES = ("yes", "no")
SORTS = ("score", "priority", "name", "useful")

PAIR_ID_SEPARATOR = "|"

# The model's calibrated score, beside Splink's match_probability.
MODEL_SCORE_COLUMN = "gbt_score"

# How `sort=useful` is weighted (docs/MODEL_API.md). The evidence half and the
# money half count equally, and the money half never falls below half of itself:
# a big pair is moved up the queue, and a small but genuinely uncertain one is
# never buried under it.
USEFUL_UNCERTAINTY_WEIGHT = 0.5
USEFUL_PRIORITY_FLOOR = 0.5


class PairsNotFound(Exception):
    """The run has no pairs.parquet — stage 3 has not run for it."""


class InvalidQuery(ValueError):
    """A filter, sort or order value the caller may not use."""


def pairs_path(run_dir: str) -> Path:
    return Path(run_dir) / PAIRS_FILENAME


def units_path(run_dir: str) -> Path:
    return Path(run_dir) / UNITS_FILENAME


def score_eval_path(run_dir: str) -> Path:
    return Path(run_dir) / SCORE_EVAL_FILENAME


def blocking_report_path(run_dir: str) -> Path:
    return Path(run_dir) / BLOCKING_REPORT_FILENAME


def _json_safe(value):
    """Pandas and Arrow nulls must reach the client as JSON null, not NaN."""
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise InvalidQuery(f"{label} must be one of {', '.join(allowed)}")


def _column_names(con, path: Path) -> list[str]:
    cursor = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)])
    return [d[0] for d in cursor.description]


def _open(run_dir: str):
    pairs = pairs_path(run_dir)
    units = units_path(run_dir)
    if not pairs.is_file() or not units.is_file():
        raise PairsNotFound(str(pairs))
    return duckdb.connect(), pairs, units


# ---------------------------------------------------------------------------
# The one query both list and detail build on
# ---------------------------------------------------------------------------


LABEL_TABLE = "pair_labels_frame"

# The human overlay, joined on at read time. pairs.parquet holds the score and
# the import overlay and nothing else, because a label write must not rewrite a
# file that will one day hold millions of rows. A label names two RECORDS, so
# the join goes through unit_members to reach the units that hold them now.
# label_overlay.apply_to_pairs does the same thing in pandas, and a test holds
# the two to one answer.
_LABEL_CTE = f"""
         lab AS (
             SELECT CASE WHEN ma.unit_id <= mb.unit_id THEN CAST(ma.unit_id AS VARCHAR)
                         ELSE CAST(mb.unit_id AS VARCHAR) END AS unit_id_l,
                    CASE WHEN ma.unit_id <= mb.unit_id THEN CAST(mb.unit_id AS VARCHAR)
                         ELSE CAST(ma.unit_id AS VARCHAR) END AS unit_id_r,
                    upper(lr.is_match) AS is_match, lr.reviewer, lr.created_at,
                    lr.notes, lr.evidence_url, lr.provenance, lr.held_out
             FROM {LABEL_TABLE} lr
             JOIN read_parquet(?) ma
               ON CAST(ma.record_id AS VARCHAR) = CAST(lr.record_id_a AS VARCHAR)
             JOIN read_parquet(?) mb
               ON CAST(mb.record_id AS VARCHAR) = CAST(lr.record_id_b AS VARCHAR)
             WHERE ma.unit_id <> mb.unit_id
         ),"""

_NO_LABEL_SELECT = """
           CAST(NULL AS VARCHAR) AS label_is_match,
           CAST(NULL AS VARCHAR) AS label_reviewer,
           CAST(NULL AS VARCHAR) AS label_created_at,
           CAST(NULL AS VARCHAR) AS label_notes,
           CAST(NULL AS VARCHAR) AS label_evidence_url,
           CAST(NULL AS VARCHAR) AS label_provenance,
           CAST(NULL AS BIGINT)  AS label_held_out,
           p.bucket AS bucket,
           p.decided_by AS decided_by,"""

_LABEL_SELECT = """
           lab.is_match AS label_is_match,
           lab.reviewer AS label_reviewer,
           CAST(lab.created_at AS VARCHAR) AS label_created_at,
           lab.notes AS label_notes,
           lab.evidence_url AS label_evidence_url,
           lab.provenance AS label_provenance,
           lab.held_out AS label_held_out,
           CASE WHEN lab.is_match = 'TRUE' THEN 'accept'
                WHEN lab.is_match = 'FALSE' THEN 'reject'
                ELSE p.bucket END AS bucket,
           CASE WHEN lab.is_match IS NOT NULL THEN 'human'
                ELSE p.decided_by END AS decided_by,"""

_LABEL_JOIN = """
    LEFT JOIN lab ON lab.unit_id_l = CAST(p.unit_id_l AS VARCHAR)
                 AND lab.unit_id_r = CAST(p.unit_id_r AS VARCHAR)"""


def _base_sql(unit_columns: list[str], with_labels: bool = False) -> str:
    """The one query the list, the detail and the histogram all read from.

    DuckDB binds ``?`` in the order it meets them in the text: the pairs file,
    the units file, then — when there are labels — the members file twice, once
    for each side of a label.
    """
    sql = f"""
    WITH p AS (SELECT * FROM read_parquet(?)),
         u AS (SELECT * FROM read_parquet(?)),{_LABEL_CTE if with_labels else ""}
         _base AS (SELECT 1)
    SELECT p.* EXCLUDE (unit_id_l, unit_id_r, bucket, decided_by),
           CAST(p.unit_id_l AS VARCHAR) AS unit_id_l,
           CAST(p.unit_id_r AS VARCHAR) AS unit_id_r,
           CAST(p.unit_id_l AS VARCHAR) || '{PAIR_ID_SEPARATOR}'
               || CAST(p.unit_id_r AS VARCHAR) AS pair_id,{
        _LABEL_SELECT if with_labels else _NO_LABEL_SELECT}
           lu AS left_unit,
           ru AS right_unit,
           CASE
               WHEN lu.existing_entity_id IS NOT NULL
                    AND ru.existing_entity_id IS NOT NULL
                    AND lu.existing_entity_id = ru.existing_entity_id THEN 'agrees'
               WHEN lu.existing_entity_id IS NOT NULL
                    AND ru.existing_entity_id IS NOT NULL THEN 'disagrees'
               ELSE 'unknown'
           END AS import_agreement,
           lower(COALESCE(CAST(lu.name AS VARCHAR), '')) AS left_name_lower,
           lower(COALESCE(CAST(ru.name AS VARCHAR), '')) AS right_name_lower,
           CAST(lu.name AS VARCHAR) AS left_name
    FROM p
    JOIN u lu ON CAST(lu.unit_id AS VARCHAR) = CAST(p.unit_id_l AS VARCHAR)
    JOIN u ru ON CAST(ru.unit_id AS VARCHAR) = CAST(p.unit_id_r AS VARCHAR)
    {_LABEL_JOIN if with_labels else ""}
"""
    # A profile without a name column still has to produce the search columns.
    if "name" not in unit_columns:
        sql = sql.replace("CAST(lu.name AS VARCHAR)", "CAST(NULL AS VARCHAR)")
        sql = sql.replace("CAST(ru.name AS VARCHAR)", "CAST(NULL AS VARCHAR)")
    return sql


def _prepare_labels(con, run_dir: str, labels) -> tuple[bool, list[str]]:
    """Register the active labels, and say what extra parameters they need.

    Returns ``(the query should join them, the extra bound paths)``. There are
    few labels and many pairs, so the frame is the build side of one hash join.
    """
    if labels is None or not len(labels):
        return False, []
    members = Path(run_dir) / UNIT_MEMBERS_FILENAME
    if not members.is_file():
        # Without the membership file a label cannot be placed on a unit.
        return False, []
    con.register(LABEL_TABLE, labels)
    return True, [str(members), str(members)]


def _priority_columns(pair_columns: list[str]) -> list[str]:
    """The profile's priority columns stage 3 summed onto the pair."""
    return [c for c in get_profile().priority_columns if f"priority_{c}" in pair_columns]


def _gamma_columns(pair_columns: list[str]) -> list[str]:
    return sorted(c for c in pair_columns if c.startswith("gamma_"))


def _usefulness_sql(has_model: bool, priority: list[str],
                    priority_max: float | None) -> dict[str, str]:
    """The three parts of `sort=useful`, and the score they make, as SQL.

    Uncertainty is distance from a half, on the model's dial when there is one
    and on Splink's when there is not, so the sort works before a model exists.
    Disagreement is how far apart the two scores are, and is zero with only one
    of them. The priority weight is the log of the pair's summed priority column
    over the log of the largest in the run, so money tilts the order without a
    huge donor outranking every uncertain pair.
    """
    score = f"COALESCE({MODEL_SCORE_COLUMN}, match_probability)" if has_model \
        else "match_probability"
    uncertainty = f"(1 - abs(2 * COALESCE({score}, 0) - 1))"
    disagreement = (f"COALESCE(abs({MODEL_SCORE_COLUMN} - match_probability), 0)"
                    if has_model else "0")
    total = " + ".join(f'COALESCE("priority_{c}", 0)' for c in priority)
    if total and priority_max and priority_max > 0:
        weight = f"(ln(1 + greatest({total}, 0)) / {math.log(1 + priority_max)!r})"
    else:
        weight = "1"
    evidence = (f"({USEFUL_UNCERTAINTY_WEIGHT} * {uncertainty} "
                f"+ {1 - USEFUL_UNCERTAINTY_WEIGHT} * {disagreement})")
    money = (f"({USEFUL_PRIORITY_FLOOR} + {1 - USEFUL_PRIORITY_FLOOR} "
             f"* least(greatest({weight}, 0), 1))")
    return {
        "_uncertainty": uncertainty,
        "_disagreement": disagreement,
        "_weight": f"least(greatest({weight}, 0), 1)",
        "_usefulness": f"{evidence} * {money}",
    }


def _item(row: dict, priority: list[str], gammas: list[str]) -> dict:
    item = {
        "pair_id": row["pair_id"],
        "track": row.get("track"),
        "match_probability": _json_safe(row.get("match_probability")),
        "match_weight": _json_safe(row.get("match_weight")),
        "gbt_score": _json_safe(row.get(MODEL_SCORE_COLUMN)),
        "bucket": row.get("bucket"),
        "score_bucket": row.get("score_bucket"),
        "decided_by": row.get("decided_by"),
        "import_disagrees": bool(row.get("import_disagrees")),
        "import_agreement": row.get("import_agreement"),
        "held_group_id": _json_safe(row.get("held_group_id")),
        "left": _json_safe(row.get("left_unit") or {}),
        "right": _json_safe(row.get("right_unit") or {}),
        "priority": {
            column: _json_safe(row.get(f"priority_{column}")) for column in priority
        },
        "label": _label(row),
        "gammas": {
            column[len("gamma_"):]: _json_safe(row.get(column)) for column in gammas
        },
    }
    if row.get("_usefulness") is not None:
        item["usefulness"] = {
            "score": _json_safe(row.get("_usefulness")),
            "uncertainty": _json_safe(row.get("_uncertainty")),
            "disagreement": _json_safe(row.get("_disagreement")),
            "weight": _json_safe(row.get("_weight")),
        }
    return item


def _label(row: dict) -> dict | None:
    """The active human decision on this pair, or None."""
    if not row.get("label_is_match"):
        return None
    return {
        "is_match": row["label_is_match"],
        "reviewer": _json_safe(row.get("label_reviewer")),
        "created_at": _json_safe(row.get("label_created_at")),
        "notes": _json_safe(row.get("label_notes")),
        "evidence_url": _json_safe(row.get("label_evidence_url")),
        "provenance": _json_safe(row.get("label_provenance")),
        "held_out": int(row.get("label_held_out") or 0),
    }


def _rows(cursor) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _filters(
    track, bucket, decided_by, import_state, held, min_score, max_score, q,
    labelled=None, min_gbt=None, max_gbt=None, has_model=False,
) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if track is not None:
        where.append("track = ?")
        params.append(track)
    if bucket is not None:
        where.append("bucket = ?")
        params.append(bucket)
    if decided_by is not None:
        where.append("decided_by = ?")
        params.append(decided_by)
    if import_state is not None:
        where.append("import_agreement = ?")
        params.append(import_state)
    if labelled == "yes":
        where.append("label_is_match IS NOT NULL")
    elif labelled == "no":
        where.append("label_is_match IS NULL")
    if held == "hide":
        where.append("held_group_id IS NULL")
    elif held == "only":
        where.append("held_group_id IS NOT NULL")
    if min_score is not None:
        where.append("match_probability >= ?")
        params.append(float(min_score))
    if max_score is not None:
        where.append("match_probability <= ?")
        params.append(float(max_score))
    # The model filters do nothing on a run no model has scored, rather than
    # emptying the list: a saved screen with a gbt filter on must not go blank
    # when someone deactivates the model.
    if has_model and min_gbt is not None:
        where.append(f"{MODEL_SCORE_COLUMN} >= ?")
        params.append(float(min_gbt))
    if has_model and max_gbt is not None:
        where.append(f"{MODEL_SCORE_COLUMN} <= ?")
        params.append(float(max_gbt))
    if q:
        pattern = f"%{q.lower()}%"
        where.append(
            "(left_name_lower LIKE ? OR right_name_lower LIKE ? "
            "OR lower(unit_id_l) LIKE ? OR lower(unit_id_r) LIKE ?)"
        )
        params.extend([pattern] * 4)
    return where, params


def get_pairs(
    run_dir: str,
    track: str | None = None,
    bucket: str | None = None,
    decided_by: str | None = None,
    import_state: str | None = None,
    held: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    q: str | None = None,
    sort: str = DEFAULT_SORT,
    order: str = "desc",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    labelled: str | None = None,
    labels=None,
    min_gbt: float | None = None,
    max_gbt: float | None = None,
) -> dict:
    """One page of a run's scored pairs, both units on every row.

    ``total`` follows the filters. ``counts`` describe the whole run and ignore
    them, so the chips above the list stay still while a search narrows it —
    the same contract as the records and exact-groups endpoints.

    *labels* is the active human decisions. They are joined on here rather than
    stored in the file, so recording one costs an insert and not a rewrite.
    """
    _check(track, TRACKS, "track")
    _check(bucket, BUCKETS, "bucket")
    _check(decided_by, DECIDED_BY, "decided_by")
    _check(import_state, IMPORT_STATES, "import")
    _check(held, HELD_STATES, "held")
    _check(labelled, LABELLED_STATES, "labelled")
    sort_key = sort or DEFAULT_SORT
    if sort_key not in SORTS:
        raise InvalidQuery(f"sort must be one of {', '.join(SORTS)}")
    order_sql = {"asc": "ASC", "desc": "DESC"}.get((order or "desc").lower())
    if order_sql is None:
        raise InvalidQuery("order must be asc or desc")

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    con, pairs, units = _open(run_dir)
    try:
        pair_columns = _column_names(con, pairs)
        unit_columns = _column_names(con, units)
        priority = _priority_columns(pair_columns)
        gammas = _gamma_columns(pair_columns)
        has_model = MODEL_SCORE_COLUMN in pair_columns
        with_labels, label_params = _prepare_labels(con, run_dir, labels)
        base = _base_sql(unit_columns, with_labels)
        base_params = [str(pairs), str(units), *label_params]

        where, params = _filters(track, bucket, decided_by, import_state, held,
                                 min_score, max_score, q, labelled,
                                 min_gbt, max_gbt, has_model)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        counts_row = con.execute(
            f"""SELECT count(*),
                       count(*) FILTER (WHERE bucket = 'accept'),
                       count(*) FILTER (WHERE bucket = 'review'),
                       count(*) FILTER (WHERE bucket = 'reject'),
                       count(*) FILTER (WHERE decided_by = 'score'),
                       count(*) FILTER (WHERE decided_by = 'import'),
                       count(*) FILTER (WHERE decided_by = 'human'),
                       count(*) FILTER (WHERE import_agreement = 'agrees'),
                       count(*) FILTER (WHERE import_agreement = 'disagrees'),
                       count(*) FILTER (WHERE import_agreement = 'unknown'),
                       count(*) FILTER (WHERE held_group_id IS NOT NULL),
                       count(*) FILTER (WHERE label_is_match IS NOT NULL),
                       count(*) FILTER (WHERE label_is_match IS NULL),
                       count(*) FILTER (WHERE track = 'person'),
                       count(*) FILTER (WHERE track = 'organisation')
                FROM ({base})""",
            base_params,
        ).fetchone()
        counts = dict(zip(
            ("all", "accept", "review", "reject", "score", "import", "human",
             "import_agrees", "import_disagrees", "import_unknown", "held",
             "labelled", "unlabelled", "person", "organisation"),
            (int(v) for v in counts_row),
        ))

        total = int(con.execute(
            f"SELECT count(*) FROM ({base}){where_sql}", [*base_params, *params]
        ).fetchone()[0])

        listing, extra_params = base, []
        if sort_key == "useful":
            # The weight needs the biggest priority in the RUN, not in the page
            # or the filtered set, or the order would move as a filter narrows.
            total = " + ".join(f'COALESCE("priority_{c}", 0)' for c in priority)
            priority_max = None
            if total:
                priority_max = con.execute(
                    f"SELECT max({total}) FROM ({base})", base_params
                ).fetchone()[0]
            parts = _usefulness_sql(has_model, priority, priority_max)
            columns = ", ".join(f"{sql} AS {name}" for name, sql in parts.items())
            listing = f"SELECT *, {columns} FROM ({base})"
            extra_params = base_params

        sort_sql = {
            "score": "match_probability",
            # Blank is not a name: NULLIF sends the nameless units to the end
            # rather than to the top of an A-to-Z.
            "name": "NULLIF(left_name_lower, '')",
            "priority": " + ".join(
                f'COALESCE("priority_{c}", 0)' for c in priority
            ) or "match_probability",
            "useful": "_usefulness",
        }[sort_key]

        cursor = con.execute(
            f"""SELECT * FROM ({listing}){where_sql}
                ORDER BY {sort_sql} {order_sql} NULLS LAST, pair_id ASC
                LIMIT ? OFFSET ?""",
            [*(extra_params or base_params), *params, limit, offset],
        )
        items = [_item(row, priority, gammas) for row in _rows(cursor)]
    finally:
        con.close()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
        "counts": counts,
        "columns": describe_columns(unit_columns),
        "priority_columns": priority,
    }


# ---------------------------------------------------------------------------
# One pair, explained
# ---------------------------------------------------------------------------


def split_pair_id(pair_id: str) -> tuple[str, str]:
    left, separator, right = (pair_id or "").partition(PAIR_ID_SEPARATOR)
    if not separator:
        raise InvalidQuery(
            f"A pair id is '<unit_id_l>{PAIR_ID_SEPARATOR}<unit_id_r>'"
        )
    return left, right


def _model_path(run_dir: str, track: str) -> Path:
    return Path(run_dir) / f"splink_model_{track}.json"


def comparison_levels(run_dir: str, track: str) -> dict:
    """``{column: {gamma: {label, m, u, match_weight}}}`` from the trained model.

    Splink numbers the levels of a comparison from the top down: the first
    non-null level takes the highest value and the last takes zero, with the
    null level at -1. Reading the saved model the same way is what turns a bare
    ``gamma_surname = 2`` into "exact match, weight 8.1".
    """
    path = _model_path(run_dir, track)
    if not path.is_file():
        return {}
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    result: dict = {}
    for comparison in model.get("comparisons", []):
        column = comparison.get("output_column_name")
        levels = comparison.get("comparison_levels", [])
        counter = len([lv for lv in levels if not lv.get("is_null_level")]) - 1
        by_gamma = {}
        for level in levels:
            if level.get("is_null_level"):
                value = -1
            else:
                value = counter
                counter -= 1
            m = level.get("m_probability")
            u = level.get("u_probability")
            weight = None
            if m and u:
                try:
                    weight = round(math.log2(float(m) / float(u)), 4)
                except (ValueError, ZeroDivisionError):
                    weight = None
            by_gamma[value] = {
                "label": level.get("label_for_charts") or level.get("sql_condition"),
                "m_probability": _json_safe(m),
                "u_probability": _json_safe(u),
                "match_weight": weight,
            }
        result[column] = by_gamma
    return result


def _explain(item: dict, levels: dict) -> list[dict]:
    """Per comparison, which agreement level this pair reached and what it was worth."""
    explanation = []
    for column, gamma in item["gammas"].items():
        # The two tracks share one pairs file, so a person pair carries null
        # gammas for every organisation comparison. Those say nothing about it.
        if gamma is None:
            continue
        by_gamma = levels.get(column) or {}
        level = by_gamma.get(int(gamma))
        explanation.append({
            "column": column,
            "gamma": _json_safe(gamma),
            "label": (level or {}).get("label"),
            "match_weight": (level or {}).get("match_weight"),
            "m_probability": (level or {}).get("m_probability"),
            "u_probability": (level or {}).get("u_probability"),
        })
    return explanation


def _members(con, run_dir: str, unit_id: str) -> tuple[list[dict], bool]:
    members = Path(run_dir) / UNIT_MEMBERS_FILENAME
    records = Path(run_dir) / RECORDS_FILENAME
    if not members.is_file() or not records.is_file():
        return [], False
    cursor = con.execute(
        """SELECT r.* FROM read_parquet(?) m
           JOIN read_parquet(?) r
             ON CAST(r.record_id AS VARCHAR) = CAST(m.record_id AS VARCHAR)
           WHERE CAST(m.unit_id AS VARCHAR) = ?
           ORDER BY CAST(r.record_id AS VARCHAR)
           LIMIT ?""",
        [str(members), str(records), str(unit_id), MAX_MEMBERS + 1],
    )
    rows = _rows(cursor)
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows[:MAX_MEMBERS]], \
        len(rows) > MAX_MEMBERS


def unit_events(con, run_dir: str, unit_id: str) -> tuple[list[dict], bool]:
    """A unit's evidence rows, newest first (D13b). Empty when a profile has none."""
    members = Path(run_dir) / UNIT_MEMBERS_FILENAME
    events = Path(run_dir) / EVENTS_FILENAME
    if not members.is_file() or not events.is_file():
        return [], False
    cursor = con.execute(
        """SELECT e.* FROM read_parquet(?) m
           JOIN read_parquet(?) e
             ON CAST(e.record_id AS VARCHAR) = CAST(m.record_id AS VARCHAR)
           WHERE CAST(m.unit_id AS VARCHAR) = ?
           ORDER BY e.date DESC NULLS LAST, CAST(e.record_id AS VARCHAR)
           LIMIT ?""",
        [str(members), str(events), str(unit_id), MAX_EVENTS + 1],
    )
    rows = _rows(cursor)
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows[:MAX_EVENTS]], \
        len(rows) > MAX_EVENTS


def unit_summary(run_dir: str, unit_ids: list[str]) -> dict[str, dict]:
    """``{unit_id: {name, track}}`` for the units that exist, ready for a label row."""
    wanted = sorted({str(u) for u in unit_ids if str(u or "").strip()})
    if not wanted:
        return {}
    units = units_path(run_dir)
    if not units.is_file():
        raise PairsNotFound(str(units))
    con = duckdb.connect()
    try:
        columns = _column_names(con, units)
        name = "CAST(name AS VARCHAR)" if "name" in columns else "CAST(NULL AS VARCHAR)"
        track = "CAST(track AS VARCHAR)" if "track" in columns else "CAST(NULL AS VARCHAR)"
        marks = ", ".join("?" * len(wanted))
        cursor = con.execute(
            f"""SELECT CAST(unit_id AS VARCHAR) AS unit_id, {name} AS name,
                       {track} AS track
                FROM read_parquet(?)
                WHERE CAST(unit_id AS VARCHAR) IN ({marks})""",
            [str(units), *wanted],
        )
        return {
            row["unit_id"]: {"name": _json_safe(row["name"]),
                             "track": _json_safe(row["track"])}
            for row in _rows(cursor)
        }
    finally:
        con.close()


def event_columns() -> list[dict]:
    """How to label the evidence rows, from the profile."""
    return [column.as_dict() for column in getattr(get_profile(), "event_columns", [])]


def get_pair(run_dir: str, pair_id: str, labels=None) -> dict | None:
    """One pair with its members, its evidence rows and a per-comparison explanation."""
    left_id, right_id = split_pair_id(pair_id)
    con, pairs, units = _open(run_dir)
    try:
        pair_columns = _column_names(con, pairs)
        unit_columns = _column_names(con, units)
        priority = _priority_columns(pair_columns)
        gammas = _gamma_columns(pair_columns)
        with_labels, label_params = _prepare_labels(con, run_dir, labels)
        base = _base_sql(unit_columns, with_labels)
        rows = _rows(con.execute(
            f"SELECT * FROM ({base}) WHERE unit_id_l = ? AND unit_id_r = ?",
            [str(pairs), str(units), *label_params, left_id, right_id],
        ))
        if not rows:
            return None
        item = _item(rows[0], priority, gammas)
        left_members, left_truncated = _members(con, run_dir, left_id)
        right_members, right_truncated = _members(con, run_dir, right_id)
        left_events, left_events_cut = unit_events(con, run_dir, left_id)
        right_events, right_events_cut = unit_events(con, run_dir, right_id)
    finally:
        con.close()

    item["left"]["members"] = left_members
    item["left"]["members_truncated"] = left_truncated
    item["right"]["members"] = right_members
    item["right"]["members_truncated"] = right_truncated
    item["events"] = {
        "left": left_events, "right": right_events,
        "left_truncated": left_events_cut, "right_truncated": right_events_cut,
    }
    item["event_columns"] = event_columns()
    item["explanation"] = _explain(item, comparison_levels(run_dir, item["track"]))
    item["model_explanation"] = model_explanation(run_dir, left_id, right_id,
                                                  item["track"])
    item["columns"] = describe_columns(unit_columns)
    return item


def model_explanation(run_dir: str, left_id: str, right_id: str,
                      track: str | None) -> dict | None:
    """The model's own explanation of one pair, or None when no model is active.

    Only the two units' evidence rows are read; the whole units frame goes to the
    feature builder because the organisation TF-IDF weights are fitted over every
    unit, and a feature's value must not depend on how much was asked for.
    """
    if not track:
        return None
    from app.model import explain as explain_lib
    from app.pipeline.dedupe import stage_3b_model

    model = stage_3b_model.models_from_state(run_dir).get(track) \
        or stage_3b_model.active_models((track,)).get(track)
    if model is None:
        return None

    import pandas as pd

    try:
        # One pair and its evidence, fetched by key in DuckDB. Reading the whole
        # pairs and events files to explain a single pair was affordable at
        # 52,000 donations and is not at 16 million PSC records.
        con = duckdb.connect()
        try:
            row = con.execute(
                "SELECT * FROM read_parquet(?) WHERE CAST(unit_id_l AS VARCHAR) = ? "
                "AND CAST(unit_id_r AS VARCHAR) = ?",
                [str(pairs_path(run_dir)), str(left_id), str(right_id)],
            ).df()
            if not len(row):
                return None

            events = None
            events_path = Path(run_dir) / EVENTS_FILENAME
            members_path = Path(run_dir) / UNIT_MEMBERS_FILENAME
            if events_path.is_file() and members_path.is_file():
                events = con.execute(
                    """SELECT e.*, m.unit_id FROM read_parquet(?) e
                       JOIN (SELECT record_id, unit_id FROM read_parquet(?)
                             WHERE CAST(unit_id AS VARCHAR) IN (?, ?)) m
                         ON CAST(e.record_id AS VARCHAR) = CAST(m.record_id AS VARCHAR)""",
                    [str(events_path), str(members_path), str(left_id), str(right_id)],
                ).df()
                if not len(events):
                    events = None
        finally:
            con.close()

        # The units frame stays whole: the organisation feature builder fits its
        # TF-IDF weights over every unit, and a feature's value must not depend
        # on how much was asked for. That read is the one still to fix for PSC.
        units = pd.read_parquet(units_path(run_dir))
        return explain_lib.explain(row.reset_index(drop=True), units, track,
                                   model.version, events=events,
                                   profile=get_profile(),
                                   gamma_levels=comparison_levels(run_dir, track))
    except Exception:  # noqa: BLE001 — an explanation must never break a pair view
        import logging

        logging.getLogger(__name__).exception(
            "Could not explain pair %s|%s", left_id, right_id)
        return None


# ---------------------------------------------------------------------------
# The histogram behind the threshold panel
# ---------------------------------------------------------------------------


def get_histogram(run_dir: str, track: str | None = None, bins: int = DEFAULT_BINS,
                  labels=None) -> dict:
    """Score histogram split by bucket and by what the imported labels say."""
    _check(track, TRACKS, "track")
    bins = max(1, min(int(bins or DEFAULT_BINS), MAX_BINS))

    con, pairs, units = _open(run_dir)
    try:
        pair_columns = _column_names(con, pairs)
        unit_columns = _column_names(con, units)
        has_model = MODEL_SCORE_COLUMN in pair_columns
        with_labels, label_params = _prepare_labels(con, run_dir, labels)
        base = _base_sql(unit_columns, with_labels)
        where_sql = " WHERE track = ?" if track is not None else ""
        base_params = [str(pairs), str(units), *label_params]

        def _bin(column: str) -> list[dict]:
            cursor = con.execute(
                f"""SELECT bin,
                           count(*) AS total,
                           count(*) FILTER (WHERE bucket = 'accept') AS accept,
                           count(*) FILTER (WHERE bucket = 'review') AS review,
                           count(*) FILTER (WHERE bucket = 'reject') AS reject,
                           count(*) FILTER (WHERE import_agreement = 'agrees') AS agrees,
                           count(*) FILTER (WHERE import_agreement = 'disagrees') AS disagrees,
                           count(*) FILTER (WHERE import_agreement = 'unknown') AS unknown
                    FROM (
                        SELECT *, least(CAST(floor({column} * ?) AS BIGINT), ? - 1)
                                   AS bin
                        FROM ({base})
                    ){where_sql}
                    GROUP BY bin ORDER BY bin""",
                # DuckDB binds ? in the order they appear in the text: the bin
                # arithmetic comes before the base query's own paths.
                [bins, bins, *base_params] + ([track] if track else []),
            )
            return _rows(cursor)

        rows = _bin("match_probability")
        # The model's own histogram, so the threshold panel can show both dials
        # side by side rather than one replacing the other.
        model_rows = _bin(MODEL_SCORE_COLUMN) if has_model else None
    finally:
        con.close()

    keys = ("total", "accept", "review", "reject", "agrees", "disagrees", "unknown")

    def _series(source):
        out = {key: [0] * bins for key in keys}
        for row in source or []:
            index = row["bin"]
            if index is None:
                continue
            index = int(index)
            if 0 <= index < bins:
                for key in keys:
                    out[key][index] = int(row[key])
        return out

    edges = [round(i / bins, 6) for i in range(bins + 1)]
    return {
        "track": track, "bins": bins, "edges": edges,
        "score_column": MODEL_SCORE_COLUMN if has_model else "match_probability",
        "by_score_column": _series(model_rows) if has_model else None,
        # The same four model keys the run's counts carry, so the threshold
        # panel can draw the lines the pairs were actually bucketed on without a
        # second request — and without reading them off a model that has been
        # retrained since (docs/MODEL_API.md).
        **model_state(run_dir, track),
        **_series(rows),
    }


def model_state(run_dir: str, track: str | None = None) -> dict:
    """Which model decided this run, and on what lines, in the API's camelCase.

    Narrowed to *track* when one is asked for, so a single-track histogram
    carries a single-track answer.
    """
    from app.pipeline.dedupe import stage_3b_model

    counts = stage_3b_model.counts_from_state(run_dir)

    def _narrow(mapping: dict) -> dict:
        if track is None:
            return mapping
        return {track: mapping[track]} if track in mapping else {}

    return {
        "modelActive": bool(counts["model_active"]),
        "modelVersion": _narrow(counts["model_version"]),
        "modelAcceptLine": _narrow(counts["model_accept_line"]),
        "modelRejectLine": _narrow(counts["model_reject_line"]),
        "modelGraded": bool(counts["model_graded"]),
        "modelWarning": counts["model_warning"],
    }

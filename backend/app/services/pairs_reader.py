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
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from app import duckdb_conn, vocabulary

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
BUCKETS = vocabulary.BUCKETS
# `model` joins the three when a graded model is what put the pair in its bucket
# (docs/MODEL.md, stage 3b), and `veto` when a pair rule did (RULESET.md).
DECIDED_BY = vocabulary.DECIDED_BY
IMPORT_STATES = ("agrees", "disagrees", "unknown")
HELD_STATES = ("hide", "only")
LABELLED_STATES = ("yes", "no")
VETOED_STATES = ("yes", "no")
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
    return duckdb_conn.connect(), pairs, units


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


#: The unit columns anything but the page itself reads. The counts, the total,
#: every filter and every sort need these three and nothing else; the other
#: sixty-five are only ever shown, and only for the fifty rows on the page.
_NARROW_UNIT_COLUMNS = ("unit_id", "existing_entity_id", "name")


def _unit_cte(unit_columns: list[str], narrow: bool) -> str:
    if not narrow:
        return "SELECT * FROM read_parquet(?)"
    wanted = [c for c in _NARROW_UNIT_COLUMNS
              if c == "unit_id" or c in unit_columns]
    return "SELECT " + ", ".join(f'"{c}"' for c in wanted) + " FROM read_parquet(?)"


def _base_sql(unit_columns: list[str], with_labels: bool = False,
              narrow: bool = False) -> str:
    """The one query the list, the detail and the histogram all read from.

    DuckDB binds ``?`` in the order it meets them in the text: the pairs file,
    the units file, then — when there are labels — the members file twice, once
    for each side of a label.

    *narrow* reads three unit columns instead of all of them and leaves the
    ``left_unit`` / ``right_unit`` structs out. It is the same rows, the same
    filters and the same order — it just does not carry the sixty-odd columns
    that only the page shows. The list asks three questions of this query (the
    chip counts, the filtered total, the page), and at PSC scale carrying the
    whole unit row through all three was most of the time.
    """
    struct = "" if narrow else """
           lu AS left_unit,
           ru AS right_unit,"""
    sql = f"""
    WITH p AS (SELECT * FROM read_parquet(?)),
         u AS ({_unit_cte(unit_columns, narrow)}),{_LABEL_CTE if with_labels else ""}
         _base AS (SELECT 1)
    SELECT p.* EXCLUDE (unit_id_l, unit_id_r, bucket, decided_by),
           CAST(p.unit_id_l AS VARCHAR) AS unit_id_l,
           CAST(p.unit_id_r AS VARCHAR) AS unit_id_r,
           CAST(p.unit_id_l AS VARCHAR) || '{PAIR_ID_SEPARATOR}'
               || CAST(p.unit_id_r AS VARCHAR) AS pair_id,{
        _LABEL_SELECT if with_labels else _NO_LABEL_SELECT}{struct}
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
        # A veto is materialised into pairs.parquet at scoring time, so these
        # come straight off the row (docs/RULESET.md, "Vetoes").
        "vetoed_by": _json_safe(row.get("vetoed_by")),
        "veto_reason": _json_safe(row.get("veto_reason")),
        "veto_conflicts_import": bool(row.get("veto_conflicts_import")),
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
    vetoed=None, has_veto=False,
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
    # A run scored before vetoes existed has no such column. Asking for the
    # vetoed pairs of one is an empty list, and asking for the rest is all of
    # them — which is the truth, not a missing feature.
    if vetoed == "yes":
        where.append("vetoed_by IS NOT NULL" if has_veto else "FALSE")
    elif vetoed == "no" and has_veto:
        where.append("vetoed_by IS NULL")
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
    vetoed: str | None = None,
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
    _check(vetoed, VETOED_STATES, "vetoed")
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
        has_veto = "vetoed_by" in pair_columns
        with_labels, label_params = _prepare_labels(con, run_dir, labels)
        # Everything but the page reads three unit columns, not sixty-eight.
        base = _base_sql(unit_columns, with_labels, narrow=True)
        base_params = [str(pairs), str(units), *label_params]

        where, params = _filters(track, bucket, decided_by, import_state, held,
                                 min_score, max_score, q, labelled,
                                 min_gbt, max_gbt, has_model, vetoed, has_veto)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        vetoed_sql = "vetoed_by IS NOT NULL" if has_veto else "FALSE"
        conflict_sql = "veto_conflicts_import" if \
            "veto_conflicts_import" in pair_columns else "FALSE"
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
                       count(*) FILTER (WHERE track = 'organisation'),
                       count(*) FILTER (WHERE {vetoed_sql}),
                       count(*) FILTER (WHERE {conflict_sql})
                FROM ({base})""",
            base_params,
        ).fetchone()
        counts = dict(zip(
            ("all", "accept", "review", "reject", "score", "import", "human",
             "import_agrees", "import_disagrees", "import_unknown", "held",
             "labelled", "unlabelled", "person", "organisation",
             "vetoed", "veto_conflicts_import"),
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

        # The page, in two steps. The first picks fifty pair ids out of the
        # narrow query; the second fetches the two whole unit rows for each of
        # them. `units.parquet` is written in `unit_id` order, so the id filter
        # is answered from the row-group statistics rather than by a scan.
        cursor = con.execute(
            f"""WITH page AS (
                    SELECT * FROM ({listing}){where_sql}
                    ORDER BY {sort_sql} {order_sql} NULLS LAST, pair_id ASC
                    LIMIT ? OFFSET ?
                ),
                wu AS (
                    SELECT * FROM read_parquet(?)
                    WHERE CAST(unit_id AS VARCHAR) IN (
                        SELECT unit_id_l FROM page UNION SELECT unit_id_r FROM page)
                )
                SELECT page.*, lu AS left_unit, ru AS right_unit
                FROM page
                LEFT JOIN wu lu ON CAST(lu.unit_id AS VARCHAR) = page.unit_id_l
                LEFT JOIN wu ru ON CAST(ru.unit_id AS VARCHAR) = page.unit_id_r
                ORDER BY {sort_sql} {order_sql} NULLS LAST, pair_id ASC""",
            [*(extra_params or base_params), *params, limit, offset, str(units)],
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


#: The two scores a run can be bucketed on, and the words for them
#: (`docs/GLOSSARY.md`: "Splink score" and "model score").
SCORE_COLUMNS = {
    "match_probability": {"scorer": "splink", "label": "Splink score"},
    MODEL_SCORE_COLUMN: {"scorer": "model", "label": "Model score"},
}


def score_column_for(run_dir: str) -> dict:
    """Which score this run is bucketed on, as a column and as a word.

    One definition, read by the pairs histogram, the diagnostics and the model
    panel, so none of them has to fetch a histogram to learn one word. It reads
    the run's own ``model_state.json``, which is what says whether a model was
    applied to *this* run — not which model is active now.
    """
    state = {}
    path = Path(run_dir) / "model_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    tracks = state.get("tracks") if isinstance(state.get("tracks"), dict) else {}
    versions = {str(track): detail.get("version")
                for track, detail in (tracks or {}).items()
                if isinstance(detail, dict) and detail.get("version") is not None}
    applied = bool(state.get("applied")) and bool(versions)
    column = MODEL_SCORE_COLUMN if applied else "match_probability"
    return {
        "score_column": column,
        "score_column_label": SCORE_COLUMNS[column]["label"],
        "scorer": SCORE_COLUMNS[column]["scorer"],
        "model_version": versions or None,
    }


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


#: Suffixes a cleaning step adds that say nothing a reader needs: the value is
#: the same thing, tidied. Anything else — ``_metaphone``, ``_district``,
#: ``_sorted`` — tells two columns apart and has to stay in the phrase.
_NEUTRAL_SUFFIXES = (" clean", " std", " padded")

#: Phrases that read badly as words, with what to say instead.
_COLUMN_PHRASES = (
    ("forename canon", "standard forename"),
    ("dob year", "birth year"),
    ("dob month", "birth month"),
    ("dob day", "birth day"),
    ("dob", "date of birth"),
    ("name tokens sorted", "name words"),
    ("name tokens", "name words"),
    ("name core", "core name"),
    ("name first token", "first word of the name"),
    ("surname metaphone", "surname sound"),
    ("forename metaphone", "forename sound"),
    ("metaphone", "sound"),
    ("forename initial", "forename initial"),
)


def plain_column(column: str) -> str:
    """A cleaned column name as a phrase: ``dob_year_clean`` -> ``birth year``.

    The Review screen printed the raw name — "Equal dob_year_clean", "Exact
    match on forename_canon" — which is the engine's vocabulary, not a
    reviewer's (docs/BACKEND_STRINGS.md §4).

    Two columns must not come out with one phrase, or the screen would say the
    same thing about different evidence. ``surname`` and ``surname_metaphone``
    are "surname" and "surname sound", never both "surname".
    """
    text = str(column or "").replace("_", " ").strip().lower()
    for suffix in _NEUTRAL_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    for phrase, plain in _COLUMN_PHRASES:
        if text == phrase:
            return plain
        if text.startswith(phrase + " ") or text.endswith(" " + phrase):
            text = text.replace(phrase, plain)
            break
    return text.strip() or str(column)


def column_labels(run_dir: str, track: str) -> dict[str, str]:
    """``{column: label}`` from the run's own comparisons, where one is named."""
    path = Path(run_dir) / "config" / "linkage_settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    block = ((settings.get("tracks") or {}).get(track) or {})
    labels = {}
    for comparison in block.get("comparisons") or []:
        column = comparison.get("column")
        if not column:
            continue
        labels[column] = comparison.get("label") or plain_column(column)
    return labels


def plain_level_label(column: str, raw_label, label: str | None = None) -> str:
    """One agreement level in plain words, with no cleaned column name in it."""
    name = label or plain_column(column)
    text = str(raw_label or "").strip()
    lowered = text.lower()
    if not text:
        return name[:1].upper() + name[1:]
    if lowered.startswith("all other"):
        return f"{name[:1].upper()}{name[1:]}: neither side is close enough to count"
    if lowered.startswith("null") or " is null" in lowered:
        return f"{name[:1].upper()}{name[1:]} is missing on at least one side"

    # Any raw column name inside the engine's own label is replaced by the
    # plain one, however the engine phrased it.
    text = re.sub(re.escape(column), name, text, flags=re.IGNORECASE)
    text = re.sub(r"^exact match on\s+", "Same ", text, flags=re.IGNORECASE)
    text = re.sub(r"^exact match\b", f"Same {name}", text, flags=re.IGNORECASE)
    text = re.sub(r"^equal\s+", "Same ", text, flags=re.IGNORECASE)
    # A string-distance level names its measure and its cut-off. Neither means
    # anything to a reviewer; "a close spelling" does.
    text = re.sub(
        r"^[\w-]+ (?:distance|similarity) (?:of )?(.+?) *(?:>=|>|≥) *([\d.]+)$",
        lambda m: f"{m.group(1)} is a close spelling", text, flags=re.IGNORECASE)
    # "within 1" on a year or a month is within one of those.
    for unit in ("year", "month", "day"):
        if name.endswith(unit):
            text = re.sub(rf"within (\d+)$",
                          lambda m: f"within {m.group(1)} "
                                    f"{unit}{'' if m.group(1) == '1' else 's'}",
                          text)
            break
    return text[:1].upper() + text[1:]


def _explain(item: dict, levels: dict, labels: dict | None = None) -> list[dict]:
    """Per comparison, which agreement level this pair reached and what it was worth.

    ``label`` is the level in plain words. ``engine_label`` keeps what Splink
    called it, because a diagnostic screen and a bug report both want it.
    """
    labels = labels or {}
    explanation = []
    for column, gamma in item["gammas"].items():
        # The two tracks share one pairs file, so a person pair carries null
        # gammas for every organisation comparison. Those say nothing about it.
        if gamma is None:
            continue
        by_gamma = levels.get(column) or {}
        level = by_gamma.get(int(gamma))
        engine_label = (level or {}).get("label")
        explanation.append({
            "column": column,
            "column_label": labels.get(column) or plain_column(column),
            "gamma": _json_safe(gamma),
            "label": plain_level_label(column, engine_label, labels.get(column)),
            "engine_label": engine_label,
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
    """A unit's evidence rows, newest first (D13b). Empty when a profile has none.

    "Newest first" needs a `date`, and only donations has one: PSC's evidence
    rows are the companies a person controls, keyed on `notified_on`. Ordering
    on a column the file does not carry is a hard DuckDB error, so the sort
    falls back to the record id and the rows still come back.
    """
    members = Path(run_dir) / UNIT_MEMBERS_FILENAME
    events = Path(run_dir) / EVENTS_FILENAME
    if not members.is_file() or not events.is_file():
        return [], False
    columns = _column_names(con, events)
    newest = next((c for c in ("date", "notified_on") if c in columns), None)
    order = f"e.{newest} DESC NULLS LAST, " if newest else ""
    cursor = con.execute(
        f"""SELECT e.* FROM read_parquet(?) m
           JOIN read_parquet(?) e
             ON CAST(e.record_id AS VARCHAR) = CAST(m.record_id AS VARCHAR)
           WHERE CAST(m.unit_id AS VARCHAR) = ?
           ORDER BY {order}CAST(e.record_id AS VARCHAR)
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
    con = duckdb_conn.connect()
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
    item["explanation"] = _explain(
        item, comparison_levels(run_dir, item["track"]),
        column_labels(run_dir, item["track"]),
    )
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
        con = duckdb_conn.connect()
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

        # The corpus statistics the run fitted, read back from its folder — or
        # fitted now and stored, for a run made before they were written down.
        # This is what lets one pair explained on its own reproduce the number
        # the scoring run wrote (`app/model/corpus.py`).
        from app.model import corpus as corpus_lib

        profile = get_profile()
        fitted = corpus_lib.for_run(run_dir, units_path(run_dir), track, profile)
        # The units frame is read whole only when a feature builder needs more
        # than the corpus gives it; the two units themselves are all the rest of
        # the explanation touches.
        units = pd.read_parquet(units_path(run_dir))
        return explain_lib.explain(row.reset_index(drop=True), units, track,
                                   model.version, events=events,
                                   profile=profile, corpus=fitted,
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
        # The histogram counts; it never shows a unit column.
        base = _base_sql(unit_columns, with_labels, narrow=True)
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
        # The field name is a column; the label is what a reader is shown
        # (docs/GLOSSARY.md: "model score" and "Splink score").
        "score_column_label": "Model score" if has_model else "Splink score",
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

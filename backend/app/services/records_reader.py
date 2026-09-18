# backend/app/services/records_reader.py
"""Records reader: filtered, sorted, paginated reads of a run's records.parquet.

DuckDB queries the parquet file directly instead of loading it into pandas, so
the same code will cope with the 16 million PSC records later.
"""

import json
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from app import duckdb_conn

from app.profiles import get_profile

RECORDS_FILENAME = "records.parquet"

DEFAULT_SORT = "name"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

TRACKS = ("person", "organisation")
STATES = ("labelled", "unreviewed")

# Columns the free-text query searches, when the profile's frame has them.
SEARCH_COLUMNS = ("name", "all_names", "record_id")


class RecordsNotFound(Exception):
    """The run has no records.parquet — it has not loaded, or does not exist."""


class InvalidQuery(ValueError):
    """A filter, sort or order value the caller may not use."""


def records_path(run_dir: str) -> Path:
    return Path(run_dir) / RECORDS_FILENAME


def _json_safe(value):
    """Pandas and Arrow nulls must reach the client as JSON null, not NaN."""
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _column_names(con, path: Path) -> list[str]:
    cursor = con.execute(
        "SELECT * FROM read_parquet(?) LIMIT 0", [str(path)]
    )
    return [d[0] for d in cursor.description]


def derived_target_columns(run_dir: str) -> set[str]:
    """The columns the run's derived rules wrote, from its own ruleset snapshot.

    Read from the run rather than from the current config, because the run is
    what the parquet beside it was made with.
    """
    path = Path(run_dir) / "config" / "ruleset.json"
    try:
        ruleset = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    targets: set[str] = set()
    for column in ruleset.get("derived_columns") or []:
        target = column.get("target") if isinstance(column, dict) else None
        if target:
            targets.add(target)
            targets.add(f"{target}_rule")
    return targets


def describe_columns(columns: list[str], derived: set[str] = frozenset()) -> list[dict]:
    """Label and type every column of a run's records, in frame order.

    A column the profile declares is described by the profile. Everything else
    was written by a rule, whose target name is the only label there is — the
    user chose it, so it is the honest one to show.

    A derived column is flagged with ``derived`` and still reports ``source:
    "cleaning"``: it is one of the columns the rules added, which is what the
    records table's toggle groups together.
    """
    profile_columns = {c.key: c for c in get_profile().display_columns}
    described = []
    for key in columns:
        declared = profile_columns.get(key)
        if declared is not None:
            described.append({
                "key": key, "label": declared.label,
                "type": declared.type, "source": "profile", "derived": False,
            })
        else:
            described.append({
                "key": key, "label": key, "type": "text", "source": "cleaning",
                "derived": key in derived,
            })
    return described


def get_records(
    run_dir: str,
    track: str | None = None,
    state: str | None = None,
    q: str | None = None,
    sort: str = DEFAULT_SORT,
    order: str = "asc",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Return one page of a run's records.

    ``total`` reflects the filters. ``counts`` describe the whole run and ignore
    them, so the track / state tabs can show a stable total while a search
    narrows the list below.
    """
    path = records_path(run_dir)
    if not path.is_file():
        raise RecordsNotFound(str(path))

    if track is not None and track not in TRACKS:
        raise InvalidQuery(f"track must be one of {', '.join(TRACKS)}")
    if state is not None and state not in STATES:
        raise InvalidQuery(f"state must be one of {', '.join(STATES)}")
    order_sql = {"asc": "ASC", "desc": "DESC"}.get((order or "asc").lower())
    if order_sql is None:
        raise InvalidQuery("order must be asc or desc")

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    con = duckdb_conn.connect()
    try:
        columns = _column_names(con, path)

        # Sort is interpolated into the SQL, so it is checked against the
        # parquet's real column names — never taken on trust from the query
        # string.
        sort_column = sort or DEFAULT_SORT
        if sort_column not in columns:
            raise InvalidQuery(
                f"Unknown sort column '{sort_column}'. "
                f"Sortable columns: {', '.join(columns)}."
            )

        where: list[str] = []
        params: list = []
        if track is not None:
            where.append("track = ?")
            params.append(track)
        if state is not None:
            where.append("review_state = ?")
            params.append(state)
        if q:
            searchable = [c for c in SEARCH_COLUMNS if c in columns]
            if searchable:
                pattern = f"%{q.lower()}%"
                clauses = [f"lower(CAST({c} AS VARCHAR)) LIKE ?" for c in searchable]
                where.append("(" + " OR ".join(clauses) + ")")
                params.extend([pattern] * len(searchable))

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        source = "read_parquet(?)"

        counts_row = con.execute(
            f"""SELECT COUNT(*) AS all_records,
                       COUNT(*) FILTER (WHERE track = 'person') AS person,
                       COUNT(*) FILTER (WHERE track = 'organisation') AS organisation,
                       COUNT(*) FILTER (WHERE review_state = 'labelled') AS labelled,
                       COUNT(*) FILTER (WHERE review_state = 'unreviewed') AS unreviewed
                FROM {source}""",
            [str(path)],
        ).fetchone()
        counts = {
            "all": int(counts_row[0]),
            "person": int(counts_row[1]),
            "organisation": int(counts_row[2]),
            "labelled": int(counts_row[3]),
            "unreviewed": int(counts_row[4]),
        }

        total = int(con.execute(
            f"SELECT COUNT(*) FROM {source}{where_sql}", [str(path), *params]
        ).fetchone()[0])

        # record_id breaks ties so paging stays stable when the sort column
        # repeats (thousands of donors share a first year, for instance).
        cursor = con.execute(
            f'''SELECT * FROM {source}{where_sql}
                ORDER BY "{sort_column}" {order_sql} NULLS LAST, record_id ASC
                LIMIT ? OFFSET ?''',
            [str(path), *params, limit, offset],
        )
        item_columns = [d[0] for d in cursor.description]
        items = [
            {name: _json_safe(value) for name, value in zip(item_columns, row)}
            for row in cursor.fetchall()
        ]
    finally:
        con.close()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
        "counts": counts,
        "columns": describe_columns(columns, derived_target_columns(run_dir)),
    }

# backend/app/services/entities_reader.py
"""Filtered, sorted, paginated reads of a run's proposed entities.

``entities.parquet`` is one row per record. The Entities tab wants one row per
entity, so everything here is a groupby in DuckDB over that file joined to the
records.

**Out of core (B5).** The filters, the counts, the sort and the page are all
SQL. Building a Python dict per entity and then slicing it, which is what this
used to do, costs one dict per entity: fine for the 18,000 of a donations run
and impossible for the 15 million of a full PSC one.
"""

from pathlib import Path

import duckdb
import pandas as pd

from app import duckdb_conn

from app.profiles import get_profile
from app.services.clusters_reader import (  # the same helpers, one definition
    MAX_EVENTS,
    _column_names,
    _events_of,
    _json_safe,
    _rows,
)
from app.services.records_reader import describe_columns

ENTITIES_FILENAME = "entities.parquet"
RECORDS_FILENAME = "records.parquet"
REPORT_FILENAME = "entity_report.json"

DEFAULT_SORT = "size"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_MEMBERS = 500
MAX_NAMES = 5

TRACKS = ("person", "organisation")
BASES = ("single", "exact_key", "import", "score", "human")
ID_STATUSES = ("new", "kept", "survivor", "minted_after_collision")
SORTS = ("size", "priority", "name", "entity_id")


class EntitiesNotFound(Exception):
    """The run has no entities.parquet — stage 5 has not run for it."""


class InvalidQuery(ValueError):
    """A filter, sort or order value the caller may not use."""


def entities_path(run_dir: str) -> Path:
    return Path(run_dir) / ENTITIES_FILENAME


def report_path(run_dir: str) -> Path:
    return Path(run_dir) / REPORT_FILENAME


def _open(run_dir: str):
    path = entities_path(run_dir)
    records = Path(run_dir) / RECORDS_FILENAME
    if not path.is_file() or not records.is_file():
        raise EntitiesNotFound(str(path))
    # The run's own temp directory, so anything DuckDB spills belongs to a run
    # and is bounded by DUCKDB_MAX_TEMP (`docs/PSC_HANDOVER.md` section 11).
    return duckdb_conn.reader_connect(run_dir), path, records


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise InvalidQuery(f"{label} must be one of {', '.join(allowed)}")


def _consensus_columns(entity_columns: list[str]) -> list[str]:
    return [c for c in get_profile().consensus_columns
            if f"{c}_entity" in entity_columns]


BASIS_RANK = {name: index for index, name in enumerate(BASES)}

# The strongest basis of an entity, the same answer ``max(bases, key=rank)``
# gives: the highest-ranked one, and among equals the alphabetically first,
# because ``bases`` arrives sorted.
_RANK_CASE = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in BASIS_RANK.items())
_STRONGEST = (
    f"COALESCE(list_filter(bases, x -> (CASE x {_RANK_CASE} ELSE 0 END) = "
    f"list_max(list_transform(bases, x -> CASE x {_RANK_CASE} ELSE 0 END)))[1],"
    f" 'single')"
)

# A separator a search needle cannot sensibly carry, so joining the names for
# one LIKE cannot invent a match across two of them.
_NAME_JOIN = "chr(1)"


def _base_sql(run_dir: str, record_columns: list[str], entity_columns: list[str],
              priority: list[str], consensus: list[str]) -> tuple[str, list]:
    name = "CAST(r.name AS VARCHAR)" if "name" in record_columns \
        else "CAST(NULL AS VARCHAR)"
    label = "NULLIF(trim(CAST(r.existing_entity_id AS VARCHAR)), '')" \
        if "existing_entity_id" in record_columns else "CAST(NULL AS VARCHAR)"
    priority_select = "".join(
        f', sum(TRY_CAST(r."{column}" AS DOUBLE)) AS "priority_{index}"'
        for index, column in enumerate(priority)
    )
    consensus_select = "".join(
        f', any_value(e."{column}_entity") AS "attr_{index}"'
        f', any_value(e."{column}_entity_basis") AS "attr_basis_{index}"'
        for index, column in enumerate(consensus)
    )
    stored_status = "any_value(CAST(e.id_status AS VARCHAR))" \
        if "id_status" in entity_columns else "CAST(NULL AS VARCHAR)"
    sql = f"""
        SELECT CAST(e.entity_id AS VARCHAR) AS entity_id,
               any_value(e.track) AS track,
               any_value(e.cluster_id) AS cluster_id,
               count(*) AS n_records,
               count(DISTINCT CAST(e.unit_id AS VARCHAR)) AS n_units,
               list_sort(list_distinct(list(e.entity_basis))) AS bases,
               list_slice(list_sort(list_distinct(list({name}))), 1, {MAX_NAMES}) AS names,
               min({name}) AS first_name,
               list_slice(list_sort(list_distinct(list({label}))), 1, {MAX_NAMES})
                   AS existing_entity_ids,
               {stored_status} AS stored_id_status
               {priority_select}{consensus_select}
        FROM read_parquet(?) e
        JOIN read_parquet(?) r
          ON CAST(r.record_id AS VARCHAR) = CAST(e.record_id AS VARCHAR)
        GROUP BY CAST(e.entity_id AS VARCHAR)
    """
    return sql, [str(entities_path(run_dir)), str(Path(run_dir) / RECORDS_FILENAME)]


def _item(row: dict, priority: list[str], consensus: list[str],
          id_status: dict) -> dict:
    bases = [b for b in (row.get("bases") or []) if b]
    strongest = row.get("entity_basis") \
        or (max(bases, key=lambda b: BASIS_RANK.get(b, 0)) if bases else "single")
    return {
        "entity_id": row["entity_id"],
        "entity_basis": strongest,
        "bases": {basis: bases.count(basis) for basis in set(bases)},
        "track": row.get("track"),
        "cluster_id": row.get("cluster_id"),
        "n_records": int(row.get("n_records") or 0),
        "n_units": int(row.get("n_units") or 0),
        "names": [n for n in (row.get("names") or []) if n][:MAX_NAMES],
        "existing_entity_ids": [v for v in (row.get("existing_entity_ids") or []) if v],
        "priority": {
            column: _json_safe(row.get(f"priority_{index}"))
            for index, column in enumerate(priority)
        },
        "id_status": (row.get("id_status") or row.get("stored_id_status")
                      or id_status.get(row["entity_id"], "new")),
        "attributes": {
            column: {"value": _json_safe(row.get(f"attr_{index}")),
                     "basis": _json_safe(row.get(f"attr_basis_{index}"))}
            for index, column in enumerate(consensus)
        },
    }


def get_entities(
    run_dir: str,
    track: str | None = None,
    basis: str | None = None,
    id_status: str | None = None,
    min_size: int = 1,
    q: str | None = None,
    sort: str = DEFAULT_SORT,
    order: str = "desc",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    statuses: dict | None = None,
) -> dict:
    """One page of the proposed entities. ``counts`` describe the whole run."""
    _check(track, TRACKS, "track")
    _check(basis, BASES, "basis")
    sort_key = sort or DEFAULT_SORT
    if sort_key not in SORTS:
        raise InvalidQuery(f"sort must be one of {', '.join(SORTS)}")
    order_sql = {"asc": "ASC", "desc": "DESC"}.get((order or "desc").lower())
    if order_sql is None:
        raise InvalidQuery("order must be asc or desc")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    statuses = statuses or {}

    con, path, records = _open(run_dir)
    try:
        entity_columns = _column_names(con, path)
        record_columns = _column_names(con, records)
        priority = [c for c in get_profile().priority_columns if c in record_columns]
        consensus = _consensus_columns(entity_columns)
        base, params = _base_sql(run_dir, record_columns, entity_columns,
                                 priority, consensus)
        # One pass over the two parquet files, into a table the filters, the
        # counts, the sort and the page then read. Without it every one of those
        # would scan the files again.
        con.execute(f"CREATE OR REPLACE TEMP TABLE er_base AS {base}", params)
        _id_status_table(con, entity_columns, statuses)

        tie = " OR ".join(
            f"\"attr_basis_{index}\" = 'tie'" for index in range(len(consensus))
        ) or "FALSE"
        counts_row = con.execute(f"""
            SELECT count(*),
                   {", ".join(f"count(*) FILTER (WHERE entity_basis = '{name}')"
                              for name in BASES)},
                   {", ".join(f"count(*) FILTER (WHERE id_status = '{name}')"
                              for name in ID_STATUSES)},
                   count(*) FILTER (WHERE track = 'person'),
                   count(*) FILTER (WHERE track = 'organisation'),
                   count(*) FILTER (WHERE {tie})
            FROM er_rows
        """).fetchone()
        counts = dict(zip(
            ("all", *BASES, *ID_STATUSES, "person", "organisation", "attribute_ties"),
            (int(value) for value in counts_row),
        ))

        where, binds = [], []
        if track is not None:
            where.append("track = ?")
            binds.append(track)
        if basis is not None:
            where.append("entity_basis = ?")
            binds.append(basis)
        if id_status is not None:
            where.append("id_status = ?")
            binds.append(id_status)
        if min_size and min_size > 1:
            where.append("n_records >= ?")
            binds.append(int(min_size))
        if q:
            pattern = f"%{q.lower()}%"
            where.append(
                f"(lower(entity_id) LIKE ? OR lower(COALESCE("
                f"list_aggregate(names, 'string_agg', {_NAME_JOIN}), '')) LIKE ?)"
            )
            binds.extend([pattern] * 2)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        sort_sql = {
            "size": "n_records",
            "priority": " + ".join(
                f'COALESCE("priority_{index}", 0)' for index in range(len(priority))
            ) or "n_records",
            "name": "lower(COALESCE(names[1], ''))",
            "entity_id": "entity_id",
        }[sort_key]

        total = int(con.execute(
            f"SELECT count(*) FROM er_rows{where_sql}", binds
        ).fetchone()[0])
        # The tie-break follows the key, as the Python sort it replaces did:
        # `sort(key=(key, entity_id), reverse=...)` reverses both.
        rows = _rows(con.execute(
            f"""SELECT * FROM er_rows{where_sql}
                ORDER BY {sort_sql} {order_sql}, entity_id {order_sql}
                LIMIT ? OFFSET ?""",
            [*binds, limit, offset],
        ))
    finally:
        con.close()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_item(row, priority, consensus, statuses) for row in rows],
        "counts": counts,
        "columns": describe_columns(record_columns),
        "priority_columns": priority,
    }


def _id_status_table(con, entity_columns: list[str], statuses: dict) -> None:
    """``er_rows``: the base with ``entity_basis`` and ``id_status`` settled.

    ``id_status`` is a column of the proposal, and the caller's map is built
    from that column, so the file is read first and the map is the fallback for
    a run written before the column existed.
    """
    if "id_status" in entity_columns:
        status = "COALESCE(stored_id_status, 'new')"
    elif statuses:
        con.register("er_status_in", pd.DataFrame({
            "entity_id": [str(key) for key in statuses],
            "id_status": [statuses[key] for key in statuses],
        }))
        con.execute(
            "CREATE OR REPLACE TEMP TABLE er_status AS "
            "SELECT CAST(entity_id AS VARCHAR) AS entity_id, "
            "CAST(id_status AS VARCHAR) AS id_status FROM er_status_in"
        )
        status = "COALESCE((SELECT s.id_status FROM er_status s "
        status += "WHERE s.entity_id = b.entity_id), 'new')"
    else:
        status = "'new'"
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW er_rows AS
        SELECT b.*, {_STRONGEST} AS entity_basis, {status} AS id_status
        FROM er_base b
    """)


def get_entity(run_dir: str, entity_id: str, statuses: dict | None = None,
               with_events: bool = False) -> dict | None:
    """One entity with its member records, and its evidence rows when asked."""
    # One entity is looked up directly. Paging the whole list first and then
    # searching it would build a row per entity to find one of them.
    con, path, records = _open(run_dir)
    try:
        entity_columns = _column_names(con, path)
        record_columns = _column_names(con, records)
        priority = [c for c in get_profile().priority_columns if c in record_columns]
        consensus = _consensus_columns(entity_columns)
        base, params = _base_sql(run_dir, record_columns, entity_columns,
                                 priority, consensus)
        rows = _rows(con.execute(
            f"SELECT * FROM ({base}) WHERE entity_id = ?",
            [*params, str(entity_id)],
        ))
    finally:
        con.close()
    if not rows:
        return None
    found = _item(rows[0], priority, consensus, statuses or {})

    con, path, records = _open(run_dir)
    try:
        record_columns = _column_names(con, records)
        cursor = con.execute(
            """SELECT r.*, e.entity_basis AS entity_basis
               FROM read_parquet(?) e
               JOIN read_parquet(?) r
                 ON CAST(r.record_id AS VARCHAR) = CAST(e.record_id AS VARCHAR)
               WHERE CAST(e.entity_id AS VARCHAR) = ?
               ORDER BY CAST(r.record_id AS VARCHAR) LIMIT ?""",
            [str(path), str(records), str(entity_id), MAX_MEMBERS + 1],
        )
        rows = [{k: _json_safe(v) for k, v in row.items()} for row in _rows(cursor)]
        units = con.execute(
            "SELECT DISTINCT CAST(unit_id AS VARCHAR) FROM read_parquet(?) "
            "WHERE CAST(entity_id AS VARCHAR) = ? ORDER BY 1",
            [str(path), str(entity_id)],
        ).fetchall()
        events, events_truncated = [], False
        if with_events:
            for (unit_id,) in units:
                found_events, truncated = _events_of(con, run_dir, unit_id)
                events.extend(found_events)
                events_truncated = events_truncated or truncated
    finally:
        con.close()

    found = dict(found)
    found["members"] = rows[:MAX_MEMBERS]
    found["members_truncated"] = len(rows) > MAX_MEMBERS
    found["unit_ids"] = [row[0] for row in units]
    found["columns"] = describe_columns(record_columns)
    if with_events:
        found["events"] = events[:MAX_EVENTS]
        found["events_truncated"] = events_truncated or len(events) > MAX_EVENTS
        found["event_columns"] = [
            c.as_dict() for c in getattr(get_profile(), "event_columns", [])
        ]
    return found

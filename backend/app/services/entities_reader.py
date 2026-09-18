# backend/app/services/entities_reader.py
"""Filtered, sorted, paginated reads of a run's proposed entities.

``entities.parquet`` is one row per record. The Entities tab wants one row per
entity, so everything here is a groupby in DuckDB over that file joined to the
records.
"""

from pathlib import Path

import duckdb

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
    return duckdb.connect(), path, records


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise InvalidQuery(f"{label} must be one of {', '.join(allowed)}")


def _consensus_columns(entity_columns: list[str]) -> list[str]:
    return [c for c in get_profile().consensus_columns
            if f"{c}_entity" in entity_columns]


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
                   AS existing_entity_ids
               {priority_select}{consensus_select}
        FROM read_parquet(?) e
        JOIN read_parquet(?) r
          ON CAST(r.record_id AS VARCHAR) = CAST(e.record_id AS VARCHAR)
        GROUP BY CAST(e.entity_id AS VARCHAR)
    """
    return sql, [str(entities_path(run_dir)), str(Path(run_dir) / RECORDS_FILENAME)]


BASIS_RANK = {name: index for index, name in enumerate(BASES)}


def _item(row: dict, priority: list[str], consensus: list[str],
          id_status: dict) -> dict:
    bases = [b for b in (row.get("bases") or []) if b]
    strongest = max(bases, key=lambda b: BASIS_RANK.get(b, 0)) if bases else "single"
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
        "id_status": id_status.get(row["entity_id"], "new"),
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
        rows = _rows(con.execute(base, params))
    finally:
        con.close()

    items = [_item(row, priority, consensus, statuses) for row in rows]
    counts = {
        "all": len(items),
        **{name: sum(1 for i in items if i["entity_basis"] == name) for name in BASES},
        **{name: sum(1 for i in items if i["id_status"] == name)
           for name in ("new", "kept", "survivor", "minted_after_collision")},
        "person": sum(1 for i in items if i["track"] == "person"),
        "organisation": sum(1 for i in items if i["track"] == "organisation"),
        "attribute_ties": sum(
            1 for i in items
            if any(a.get("basis") == "tie" for a in i["attributes"].values())
        ),
    }

    if track is not None:
        items = [i for i in items if i["track"] == track]
    if basis is not None:
        items = [i for i in items if i["entity_basis"] == basis]
    if id_status is not None:
        items = [i for i in items if i["id_status"] == id_status]
    if min_size and min_size > 1:
        items = [i for i in items if i["n_records"] >= int(min_size)]
    if q:
        needle = q.lower()
        items = [
            i for i in items
            if needle in i["entity_id"].lower()
            or any(needle in (n or "").lower() for n in i["names"])
        ]

    reverse = order_sql == "DESC"
    key = {
        "size": lambda i: i["n_records"],
        "priority": lambda i: sum(v or 0 for v in i["priority"].values()),
        "name": lambda i: (i["names"][0] or "").lower() if i["names"] else "",
        "entity_id": lambda i: i["entity_id"],
    }[sort_key]
    items.sort(key=lambda i: (key(i), i["entity_id"]), reverse=reverse)

    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "items": items[offset:offset + limit],
        "counts": counts,
        "columns": describe_columns(record_columns),
        "priority_columns": priority,
    }


def get_entity(run_dir: str, entity_id: str, statuses: dict | None = None,
               with_events: bool = False) -> dict | None:
    """One entity with its member records, and its evidence rows when asked."""
    page = get_entities(run_dir, limit=MAX_LIMIT, statuses=statuses, min_size=1)
    found = next((i for i in page["items"] if i["entity_id"] == str(entity_id)), None)
    if found is None:
        # The page above is capped; look the one entity up directly.
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

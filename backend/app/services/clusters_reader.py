# backend/app/services/clusters_reader.py
"""Filtered, sorted, paginated reads of a run's clusters and its review queue.

The queue holds two different things that a reviewer treats the same way: a
cluster the gate withheld, and a held exact group waiting for a human
(`docs/ENTITIES.md`). They are unioned here so one screen, one filter set and
one decision endpoint cover both.

DuckDB reads the parquet files in place, as everywhere else in this app.
"""

import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from app import duckdb_conn, vocabulary

from app.profiles import get_profile
from app.services import exact_groups_reader
from app.services.records_reader import describe_columns

CLUSTERS_FILENAME = "clusters.parquet"
UNITS_FILENAME = "units.parquet"
UNIT_MEMBERS_FILENAME = "unit_members.parquet"
RECORDS_FILENAME = "records.parquet"
GROUPS_FILENAME = "exact_groups.parquet"
ENTITIES_FILENAME = "entities.parquet"
EVENTS_FILENAME = "events.parquet"
PAIRS_FILENAME = "pairs.parquet"

DEFAULT_SORT = "size"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_MEMBERS = 200
MAX_EVENTS = 200
# How many units a detail sends. A held group can have hundreds; the queue item
# reports the true size, and a decision covers all of them however few are shown.
MAX_UNITS = 200
MAX_NAMES = 5
MAX_IDS = 5

TRACKS = ("person", "organisation")
STATUSES = vocabulary.CLUSTER_STATUSES
YES_NO = ("yes", "no")
SORTS = ("size", "records", "priority", "name")

HELD_PREFIX = "H-"


class ClustersNotFound(Exception):
    """The run has no clusters.parquet — stage 4 has not run for it."""


class InvalidQuery(ValueError):
    """A filter, sort or order value the caller may not use."""


def clusters_path(run_dir: str) -> Path:
    return Path(run_dir) / CLUSTERS_FILENAME


def _json_safe(value):
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


def _rows(cursor) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise InvalidQuery(f"{label} must be one of {', '.join(allowed)}")


def _column_names(con, path: Path) -> list[str]:
    return [d[0] for d in con.execute(
        "SELECT * FROM read_parquet(?) LIMIT 0", [str(path)]
    ).description]


def _open(run_dir: str):
    path = clusters_path(run_dir)
    units = Path(run_dir) / UNITS_FILENAME
    if not path.is_file() or not units.is_file():
        raise ClustersNotFound(str(path))
    # The run's own temp directory, so anything DuckDB spills belongs to a run
    # and is bounded by DUCKDB_MAX_TEMP (`docs/PSC_HANDOVER.md` section 11).
    return duckdb_conn.connect(Path(run_dir) / "duckdb_tmp"), path, units


def _priority_columns(unit_columns: list[str]) -> list[str]:
    return [c for c in get_profile().priority_columns if c in unit_columns]


# ---------------------------------------------------------------------------
# The one query
# ---------------------------------------------------------------------------


def _base_sql(run_dir: str, unit_columns: list[str], priority: list[str]) -> tuple[str, list]:
    """One row per cluster and per held exact group, with the numbers a chip needs."""
    name = "CAST(u.name AS VARCHAR)" if "name" in unit_columns else "CAST(NULL AS VARCHAR)"
    label = "u.existing_entity_id" if "existing_entity_id" in unit_columns \
        else "CAST(NULL AS VARCHAR)"
    priority_select = "".join(
        f', sum(TRY_CAST(u."{column}" AS DOUBLE)) AS "priority_{index}"'
        for index, column in enumerate(priority)
    )
    priority_zero = "".join(
        f', CAST(NULL AS DOUBLE) AS "priority_{index}"' for index in range(len(priority))
    )
    groups = Path(run_dir) / GROUPS_FILENAME
    records = Path(run_dir) / RECORDS_FILENAME
    params = [str(clusters_path(run_dir)), str(Path(run_dir) / UNITS_FILENAME)]

    held_sql = ""
    if groups.is_file() and records.is_file():
        held_sql = f"""
        UNION ALL
        SELECT g.group_id AS cluster_id,
               any_value(g.track) AS track,
               'held_key' AS status,
               'held_key' AS statuses,
               TRUE AS withheld,
               count(*) AS n_units,
               count(*) AS n_records,
               count(*) AS n_parts,
               list_slice(list_sort(list_distinct(list(
                   NULLIF(trim(CAST(r.existing_entity_id AS VARCHAR)), '')))), 1, {MAX_IDS})
                   AS existing_entity_ids,
               count(DISTINCT NULLIF(trim(CAST(r.existing_entity_id AS VARCHAR)), ''))
                   AS n_existing_ids,
               list_slice(list_sort(list_distinct(list(CAST(r.name AS VARCHAR)))), 1, {MAX_NAMES})
                   AS names,
               min(CAST(r.name AS VARCHAR)) AS first_name,
               any_value(CAST(g.guard AS VARCHAR)) AS guard
               {"".join(f', sum(TRY_CAST(r."{c}" AS DOUBLE)) AS "priority_{i}"' for i, c in enumerate(priority))}
        FROM read_parquet(?) g
        JOIN read_parquet(?) r ON CAST(r.record_id AS VARCHAR) = CAST(g.record_id AS VARCHAR)
        WHERE g.status = 'held'
        GROUP BY g.group_id
        """
        params.extend([str(groups), str(records)])

    sql = f"""
        SELECT c.cluster_id,
               any_value(c.track) AS track,
               any_value(c.status) AS status,
               any_value(c.statuses) AS statuses,
               any_value(c.withheld) AS withheld,
               count(*) AS n_units,
               CAST(sum(COALESCE(u.unit_size, 1)) AS BIGINT) AS n_records,
               count(DISTINCT c.proposed_entity_key) AS n_parts,
               list_slice(list_sort(list_distinct(list({label}))), 1, {MAX_IDS})
                   AS existing_entity_ids,
               count(DISTINCT {label}) AS n_existing_ids,
               list_slice(list_sort(list_distinct(list({name}))), 1, {MAX_NAMES}) AS names,
               min({name}) AS first_name,
               CAST(NULL AS VARCHAR) AS guard
               {priority_select if priority else ""}
        FROM read_parquet(?) c
        JOIN read_parquet(?) u ON CAST(u.unit_id AS VARCHAR) = CAST(c.unit_id AS VARCHAR)
        GROUP BY c.cluster_id
        {held_sql}
    """
    if not priority:
        sql = sql.replace(priority_zero, "")
    return sql, params


def _item(row: dict, priority: list[str], decisions: dict, ties: set) -> dict:
    cluster_id = row["cluster_id"]
    statuses = [s for s in str(row.get("statuses") or "").split("|") if s]
    status = row.get("status") or "ok"
    if cluster_id in ties:
        if "attribute_tie" not in statuses:
            statuses = statuses + ["attribute_tie"]
        if status == "ok":
            status = "attribute_tie"
    return {
        "cluster_id": cluster_id,
        "track": row.get("track"),
        "status": status,
        "statuses": statuses or ([status] if status != "ok" else []),
        "withheld": bool(row.get("withheld")),
        "n_units": int(row.get("n_units") or 0),
        "n_records": int(row.get("n_records") or 0),
        "existing_entity_ids": [v for v in (row.get("existing_entity_ids") or []) if v],
        "n_existing_ids": int(row.get("n_existing_ids") or 0),
        "names": [v for v in (row.get("names") or []) if v][:MAX_NAMES],
        "priority": {
            column: _json_safe(row.get(f"priority_{index}"))
            for index, column in enumerate(priority)
        },
        "parts": int(row.get("n_parts") or 1),
        "decision": decisions.get(cluster_id),
        # Why the match key held this group back, for a held_key item.
        "guard": _json_safe(row.get("guard")),
        "guard_text": exact_groups_reader.guard_text(row.get("guard")),
    }


def _attribute_tie_clusters(con, run_dir: str) -> set:
    """The clusters whose consensus column could not be settled.

    A tie is rare — 0 on the PSC sample — so the set itself is small whatever
    the run's size, and the query behind it is a filtered scan, not a read.
    """
    path = Path(run_dir) / ENTITIES_FILENAME
    if not path.is_file():
        return set()
    columns = _column_names(con, path)
    basis = [c for c in columns if c.endswith("_entity_basis")]
    if not basis or "cluster_id" not in columns:
        return set()
    clause = " OR ".join(f'"{c}" = \'tie\'' for c in basis)
    rows = con.execute(
        f"SELECT DISTINCT cluster_id FROM read_parquet(?) WHERE {clause}",
        [str(path)],
    ).fetchall()
    return {row[0] for row in rows}


def attribute_tie_clusters(run_dir: str) -> set:
    """The same, on a connection of its own."""
    con = duckdb_conn.connect(Path(run_dir) / "duckdb_tmp")
    try:
        return _attribute_tie_clusters(con, run_dir)
    finally:
        con.close()


def get_clusters(
    run_dir: str,
    track: str | None = None,
    status: str | None = None,
    withheld: str | None = None,
    decided: str | None = None,
    min_units: int = 2,
    q: str | None = None,
    sort: str = DEFAULT_SORT,
    order: str = "desc",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    decisions: dict | None = None,
) -> dict:
    """One page of the clusters, the held groups included.

    ``total`` follows the filters; ``counts`` describe the whole run.
    """
    _check(track, TRACKS, "track")
    _check(status, STATUSES, "status")
    _check(withheld, YES_NO, "withheld")
    _check(decided, YES_NO, "decided")
    sort_key = sort or DEFAULT_SORT
    if sort_key not in SORTS:
        raise InvalidQuery(f"sort must be one of {', '.join(SORTS)}")
    order_sql = {"asc": "ASC", "desc": "DESC"}.get((order or "desc").lower())
    if order_sql is None:
        raise InvalidQuery("order must be asc or desc")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    decisions = decisions or {}

    con, path, units = _open(run_dir)
    try:
        unit_columns = _column_names(con, units)
        priority = _priority_columns(unit_columns)
        base, params = _base_sql(run_dir, unit_columns, priority)
        # One pass over the parquet files into a table the six queries below
        # then read. A PSC run has 458,000 clusters and the old code built a
        # Python dict for every one of them before showing fifty.
        con.execute(f"CREATE OR REPLACE TEMP TABLE cr_base AS {base}", params)
        ties = _attribute_tie_clusters(con, run_dir)
        con.execute("CREATE OR REPLACE TEMP TABLE cr_ties(cluster_id VARCHAR)")
        if ties:
            con.execute("INSERT INTO cr_ties SELECT UNNEST(?)", [sorted(ties)])
        decided_ids = sorted(decisions)
        con.execute("CREATE OR REPLACE TEMP TABLE cr_decided(cluster_id VARCHAR)")
        if decided_ids:
            con.execute("INSERT INTO cr_decided SELECT UNNEST(?)", [decided_ids])

        counts_row = con.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE withheld OR status = 'held_key'),
                      count(*) FILTER (WHERE withheld),
                      count(*) FILTER (WHERE status = 'ok'),
                      count(*) FILTER (WHERE status = 'conflict'),
                      count(*) FILTER (WHERE status = 'too_large'),
                      count(*) FILTER (WHERE status = 'weak_link'),
                      count(*) FILTER (WHERE status = 'mixed_ids'),
                      count(*) FILTER (WHERE status = 'held_key'),
                      count(*) FILTER (WHERE track = 'person'),
                      count(*) FILTER (WHERE track = 'organisation')
               FROM cr_base"""
        ).fetchone()
        counts = dict(zip(
            ("all", "reviewable", "withheld", "ok", "conflict", "too_large",
             "weak_link", "mixed_ids", "held_key", "person", "organisation"),
            (int(value) for value in counts_row),
        ))
        counts["attribute_tie"] = len(ties)
        # The queue counts what is still open: a decided item is done with, and
        # a total that never fell however much work was done would be useless.
        # A decided item may also have stopped being withheld — a merge stands
        # the gate down — so "decided" counts the decisions this run has, not
        # what is left flagged.
        open_rows, decided_here, still_open = con.execute(
            """SELECT count(*) FILTER (WHERE withheld OR status = 'held_key'),
                      count(*) FILTER (WHERE decided),
                      count(*) FILTER (WHERE decided
                                         AND (withheld OR status = 'held_key'))
               FROM (SELECT withheld, status,
                            cluster_id IN (SELECT cluster_id FROM cr_decided) AS decided
                     FROM cr_base)"""
        ).fetchone()
        counts["reviewable"] = int(open_rows) - int(still_open)
        counts["decided"] = int(decided_here)
        counts["decisions"] = len(decisions)

        where, binds = [], []
        if track is not None:
            where.append("track = ?")
            binds.append(track)
        if status is not None and status != "attribute_tie":
            where.append("status = ?")
            binds.append(status)
        if status == "attribute_tie":
            # `_item` adds the status to any cluster the proposal could not
            # settle, so the filter is that same set.
            where.append("cluster_id IN (SELECT cluster_id FROM cr_ties)")
        if withheld == "yes":
            where.append("(withheld OR status = 'held_key')")
        elif withheld == "no":
            where.append("NOT withheld AND status <> 'held_key'")
        if decided == "yes":
            where.append("cluster_id IN (SELECT cluster_id FROM cr_decided)")
        elif decided == "no":
            where.append("cluster_id NOT IN (SELECT cluster_id FROM cr_decided)")
        if min_units is not None:
            where.append("n_units >= ?")
            binds.append(int(min_units))
        if q:
            pattern = f"%{q.lower()}%"
            where.append(
                "(lower(cluster_id) LIKE ? OR lower(COALESCE(first_name, '')) LIKE ? "
                "OR lower(list_aggregate(names, 'string_agg', ' ')) LIKE ?)"
            )
            binds.extend([pattern] * 3)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        sort_sql = {
            "size": "n_units", "records": "n_records",
            "name": "NULLIF(lower(first_name), '')",
            "priority": " + ".join(
                f'COALESCE("priority_{i}", 0)' for i in range(len(priority))
            ) or "n_units",
        }[sort_key]

        total = int(con.execute(
            f"SELECT count(*) FROM cr_base{where_sql}", binds
        ).fetchone()[0])
        rows = _rows(con.execute(
            f"""SELECT * FROM cr_base{where_sql}
                ORDER BY {sort_sql} {order_sql} NULLS LAST, cluster_id ASC
                LIMIT ? OFFSET ?""",
            [*binds, limit, offset],
        ))
    finally:
        con.close()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_item(row, priority, decisions, ties) for row in rows],
        "counts": counts,
        "columns": describe_columns(unit_columns),
        "priority_columns": priority,
    }


# ---------------------------------------------------------------------------
# One cluster
# ---------------------------------------------------------------------------


def _unit_rows(con, run_dir: str, unit_ids: list[str]) -> list[dict]:
    if not unit_ids:
        return []
    marks = ", ".join("?" * len(unit_ids))
    cursor = con.execute(
        f"""SELECT * FROM read_parquet(?)
            WHERE CAST(unit_id AS VARCHAR) IN ({marks})
            ORDER BY CAST(unit_id AS VARCHAR)""",
        [str(Path(run_dir) / UNITS_FILENAME), *unit_ids],
    )
    return [{k: _json_safe(v) for k, v in row.items()} for row in _rows(cursor)]


def _grouped(cursor, cap: int) -> dict[str, tuple[list[dict], bool]]:
    """Rows carrying a ``_unit`` column, split per unit and capped per unit."""
    found: dict[str, list[dict]] = {}
    for row in _rows(cursor):
        unit_id = str(row.pop("_unit"))
        found.setdefault(unit_id, []).append(
            {k: _json_safe(v) for k, v in row.items()})
    return {unit: (rows[:cap], len(rows) > cap) for unit, rows in found.items()}


def _members_for(con, run_dir: str, unit_ids: list[str]
                 ) -> dict[str, tuple[list[dict], bool]]:
    """Every shown unit's member records, in one query rather than one each.

    A cluster shows up to ``MAX_UNITS`` units, and this used to run a query per
    unit — each one a scan of ``unit_members.parquet`` joined to
    ``records.parquet``. On the 500,000-record sample one cluster detail took
    twelve seconds; at sixteen million records both files are thirty times
    bigger and the call is made once per unit shown. One query with a window
    function does the same work once.
    """
    members = Path(run_dir) / UNIT_MEMBERS_FILENAME
    records = Path(run_dir) / RECORDS_FILENAME
    if not members.is_file() or not records.is_file() or not unit_ids:
        return {}
    marks = ", ".join("?" * len(unit_ids))
    cursor = con.execute(
        f"""SELECT * EXCLUDE (_rank) FROM (
                SELECT r.*, CAST(m.unit_id AS VARCHAR) AS _unit,
                       row_number() OVER (
                           PARTITION BY CAST(m.unit_id AS VARCHAR)
                           ORDER BY CAST(r.record_id AS VARCHAR)
                       ) AS _rank
                FROM read_parquet(?) m
                JOIN read_parquet(?) r
                  ON CAST(r.record_id AS VARCHAR) = CAST(m.record_id AS VARCHAR)
                WHERE CAST(m.unit_id AS VARCHAR) IN ({marks})
            ) WHERE _rank <= ?
            ORDER BY _unit, CAST(record_id AS VARCHAR)""",
        [str(members), str(records), *unit_ids, MAX_MEMBERS + 1],
    )
    return _grouped(cursor, MAX_MEMBERS)


def _events_for(con, run_dir: str, unit_ids: list[str]
                ) -> dict[str, tuple[list[dict], bool]]:
    """Every shown unit's evidence rows, in one query. See ``_members_for``."""
    members = Path(run_dir) / UNIT_MEMBERS_FILENAME
    events = Path(run_dir) / EVENTS_FILENAME
    if not members.is_file() or not events.is_file() or not unit_ids:
        return {}
    marks = ", ".join("?" * len(unit_ids))
    cursor = con.execute(
        f"""SELECT * EXCLUDE (_rank) FROM (
                SELECT e.*, CAST(m.unit_id AS VARCHAR) AS _unit,
                       row_number() OVER (
                           PARTITION BY CAST(m.unit_id AS VARCHAR)
                           ORDER BY e.date DESC NULLS LAST
                       ) AS _rank
                FROM read_parquet(?) m
                JOIN read_parquet(?) e
                  ON CAST(e.record_id AS VARCHAR) = CAST(m.record_id AS VARCHAR)
                WHERE CAST(m.unit_id AS VARCHAR) IN ({marks})
            ) WHERE _rank <= ?
            ORDER BY _unit, _rank""",
        [str(members), str(events), *unit_ids, MAX_EVENTS + 1],
    )
    return _grouped(cursor, MAX_EVENTS)


def _members_of(con, run_dir: str, unit_id: str) -> tuple[list[dict], bool]:
    """One unit's member records. ``_members_for`` for a whole screen's worth."""
    return _members_for(con, run_dir, [str(unit_id)]).get(str(unit_id), ([], False))


def _events_of(con, run_dir: str, unit_id: str) -> tuple[list[dict], bool]:
    """One unit's evidence rows. ``_events_for`` for a whole screen's worth."""
    return _events_for(con, run_dir, [str(unit_id)]).get(str(unit_id), ([], False))


def _held_detail(con, run_dir: str, cluster_id: str, decisions: dict,
                 with_events: bool) -> dict | None:
    """A held exact group, dressed as a cluster so one screen can show both."""
    groups = Path(run_dir) / GROUPS_FILENAME
    records = Path(run_dir) / RECORDS_FILENAME
    if not groups.is_file() or not records.is_file():
        return None
    total = con.execute(
        "SELECT count(*) FROM read_parquet(?) WHERE group_id = ? AND status = 'held'",
        [str(groups), cluster_id],
    ).fetchone()[0]
    cursor = con.execute(
        """SELECT r.*, g.track AS group_track, g.guard AS guard, g.key_ids AS key_ids
           FROM read_parquet(?) g
           JOIN read_parquet(?) r
             ON CAST(r.record_id AS VARCHAR) = CAST(g.record_id AS VARCHAR)
           WHERE g.group_id = ? AND g.status = 'held'
           ORDER BY CAST(r.record_id AS VARCHAR) LIMIT ?""",
        [str(groups), str(records), cluster_id, MAX_UNITS],
    )
    rows = _rows(cursor)
    if not rows:
        return None
    members = [{k: _json_safe(v) for k, v in row.items()} for row in rows]
    units = []
    record_ids = [str(member["record_id"]) for member in members]
    # A held group's "units" are its records, one each, so the record id is the
    # unit id and the batch lookup is the same one the real clusters use.
    events_by_unit = _events_for(con, run_dir, record_ids) if with_events else {}
    for member in members:
        record_id = str(member["record_id"])
        unit = {"unit_id": record_id, "unit_size": 1,
                "name": member.get("name"), "track": member.get("track"),
                "proposed_entity_key": record_id,
                "members": [member], "members_truncated": False}
        if with_events:
            unit["events"], unit["events_truncated"] = \
                events_by_unit.get(record_id, ([], False))
        units.append(unit)
    return {
        "cluster_id": cluster_id,
        "track": members[0].get("track"),
        "status": "held_key",
        "statuses": ["held_key"],
        "withheld": True,
        "guard": rows[0].get("guard"),
        "guard_text": exact_groups_reader.guard_text(rows[0].get("guard")),
        "key_ids": [k for k in str(rows[0].get("key_ids") or "").split("|") if k],
        # The true size, not the page size: a decision covers the whole group.
        "n_units": int(total),
        "n_records": int(total),
        "units_shown": len(units),
        "units_truncated": int(total) > len(units),
        "units": units,
        "edges": [],
        "parts": [{"proposed_entity_key": u["unit_id"], "unit_ids": [u["unit_id"]],
                   "n_records": 1} for u in units],
        "weak_pairs": [],
        "decision": decisions.get(cluster_id),
    }


def get_cluster(run_dir: str, cluster_id: str, decisions: dict | None = None,
                with_events: bool = False, cluster_floor: float = 0.20) -> dict | None:
    """One cluster with its units, the pairs between them and the proposed parts."""
    decisions = decisions or {}
    con, path, units_path = _open(run_dir)
    try:
        unit_columns = _column_names(con, units_path)
        priority = _priority_columns(unit_columns)
        if str(cluster_id).startswith(HELD_PREFIX):
            detail = _held_detail(con, run_dir, cluster_id, decisions, with_events)
            if detail is not None:
                detail["columns"] = describe_columns(unit_columns)
                detail["event_columns"] = [
                    c.as_dict() for c in getattr(get_profile(), "event_columns", [])
                ]
                detail["priority"] = {}
                detail["attributes"] = {}
                detail["existing_entity_ids"] = []
            return detail

        rows = _rows(con.execute(
            """SELECT * FROM read_parquet(?) WHERE cluster_id = ?
               ORDER BY CAST(unit_id AS VARCHAR)""",
            [str(path), str(cluster_id)],
        ))
        if not rows:
            return None
        unit_ids = [str(row["unit_id"]) for row in rows]
        part_of = {str(row["unit_id"]): str(row["proposed_entity_key"]) for row in rows}
        shown_ids = unit_ids[:MAX_UNITS]
        unit_rows = _unit_rows(con, run_dir, shown_ids)

        by_unit = _members_for(con, run_dir, shown_ids)
        events_by_unit = _events_for(con, run_dir, shown_ids) if with_events else {}
        for unit in unit_rows:
            key = str(unit["unit_id"])
            unit["proposed_entity_key"] = part_of.get(key)
            unit["members"], unit["members_truncated"] = by_unit.get(key, ([], False))
            if with_events:
                unit["events"], unit["events_truncated"] = \
                    events_by_unit.get(key, ([], False))

        edges = _edges(con, run_dir, shown_ids, part_of)
        total_records = con.execute(
            """SELECT CAST(sum(COALESCE(u.unit_size, 1)) AS BIGINT)
               FROM read_parquet(?) c
               JOIN read_parquet(?) u
                 ON CAST(u.unit_id AS VARCHAR) = CAST(c.unit_id AS VARCHAR)
               WHERE c.cluster_id = ?""",
            [str(path), str(units_path), str(cluster_id)],
        ).fetchone()[0] or 0
        # Every unit counts towards its part, shown or not.
        parts: dict[str, dict] = {}
        for row in rows:
            key = str(row["proposed_entity_key"])
            entry = parts.setdefault(key, {"proposed_entity_key": key,
                                           "unit_ids": [], "n_records": 0})
            entry["unit_ids"].append(str(row["unit_id"]))
        for unit in unit_rows:
            parts[str(unit["proposed_entity_key"])]["n_records"] += int(
                unit.get("unit_size") or 1
            )
    finally:
        con.close()

    weak = sorted(
        (e for e in edges
         if e["match_probability"] is not None
         and e["match_probability"] < cluster_floor
         and e["decided_by"] != "human"),
        key=lambda e: e["match_probability"],
    )
    return {
        "cluster_id": str(cluster_id),
        "track": rows[0].get("track"),
        "status": rows[0].get("status") or "ok",
        "statuses": [s for s in str(rows[0].get("statuses") or "").split("|") if s],
        "withheld": bool(rows[0].get("withheld")),
        "n_units": len(unit_ids),
        "n_records": int(total_records),
        "units_shown": len(unit_rows),
        "units_truncated": len(unit_ids) > len(unit_rows),
        "existing_entity_ids": sorted({
            str(u["existing_entity_id"]) for u in unit_rows
            if u.get("existing_entity_id")
        })[:MAX_IDS],
        "priority": {
            column: sum(float(u.get(column) or 0) for u in unit_rows)
            for column in priority
        },
        "units": unit_rows,
        "edges": edges,
        "parts": sorted(parts.values(), key=lambda p: p["proposed_entity_key"]),
        "weak_pairs": [
            {"pair_id": e["pair_id"], "match_probability": e["match_probability"],
             "bucket": e["bucket"]} for e in weak[:MAX_NAMES * 4]
        ],
        "decision": decisions.get(str(cluster_id)),
        # Only a held group has one; a real cluster is null, so the shape matches.
        "guard": None,
        "guard_text": None,
        "columns": describe_columns(unit_columns),
        "event_columns": [c.as_dict() for c in getattr(get_profile(), "event_columns", [])],
    }


def _edges(con, run_dir: str, unit_ids: list[str], part_of: dict) -> list[dict]:
    """Every scored pair whose two units are both in this cluster."""
    pairs = Path(run_dir) / PAIRS_FILENAME
    if not pairs.is_file() or len(unit_ids) < 2:
        return []
    marks = ", ".join("?" * len(unit_ids))
    cursor = con.execute(
        f"""SELECT CAST(unit_id_l AS VARCHAR) AS unit_id_l,
                   CAST(unit_id_r AS VARCHAR) AS unit_id_r,
                   match_probability, match_weight, bucket, score_bucket, decided_by
            FROM read_parquet(?)
            WHERE CAST(unit_id_l AS VARCHAR) IN ({marks})
              AND CAST(unit_id_r AS VARCHAR) IN ({marks})
            ORDER BY match_probability DESC NULLS LAST""",
        [str(pairs), *unit_ids, *unit_ids],
    )
    edges = []
    for row in _rows(cursor):
        left, right = row["unit_id_l"], row["unit_id_r"]
        edges.append({
            "pair_id": f"{left}|{right}",
            "unit_id_l": left, "unit_id_r": right,
            "match_probability": _json_safe(row["match_probability"]),
            "match_weight": _json_safe(row["match_weight"]),
            "bucket": row["bucket"], "score_bucket": row["score_bucket"],
            "decided_by": row["decided_by"],
            "source": row["decided_by"] if row["bucket"] == "accept" else None,
            "inside_part": part_of.get(left) == part_of.get(right),
        })
    return edges


def cluster_representatives(run_dir: str, cluster_id: str) -> list[str]:
    """One record per unit — the smallest — which is what a merge decision joins.

    `docs/ENTITIES.md`: a merge is a star from the smallest record to ONE
    representative of every other unit. Starring every record instead would
    write a label per record (364 for a 26-unit cluster) and say nothing the
    exact keys have not already settled inside each unit.
    """
    if str(cluster_id).startswith(HELD_PREFIX):
        # A held group's records are each their own unit until a human decides.
        return cluster_members(run_dir, cluster_id)
    con, path, _units = _open(run_dir)
    try:
        members = Path(run_dir) / UNIT_MEMBERS_FILENAME
        if not members.is_file():
            return []
        rows = con.execute(
            """SELECT min(CAST(m.record_id AS VARCHAR)) FROM read_parquet(?) c
               JOIN read_parquet(?) m
                 ON CAST(m.unit_id AS VARCHAR) = CAST(c.unit_id AS VARCHAR)
               WHERE c.cluster_id = ?
               GROUP BY CAST(c.unit_id AS VARCHAR) ORDER BY 1""",
            [str(path), str(members), str(cluster_id)],
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        con.close()


def cluster_members(run_dir: str, cluster_id: str) -> list[str]:
    """Every record id in a cluster or a held group — what a decision covers."""
    con, path, _units = _open(run_dir)
    try:
        if str(cluster_id).startswith(HELD_PREFIX):
            groups = Path(run_dir) / GROUPS_FILENAME
            if not groups.is_file():
                return []
            rows = con.execute(
                "SELECT DISTINCT CAST(record_id AS VARCHAR) FROM read_parquet(?) "
                "WHERE group_id = ? AND status = 'held' ORDER BY 1",
                [str(groups), str(cluster_id)],
            ).fetchall()
            return [row[0] for row in rows]
        members = Path(run_dir) / UNIT_MEMBERS_FILENAME
        if not members.is_file():
            return []
        rows = con.execute(
            """SELECT DISTINCT CAST(m.record_id AS VARCHAR) FROM read_parquet(?) c
               JOIN read_parquet(?) m
                 ON CAST(m.unit_id AS VARCHAR) = CAST(c.unit_id AS VARCHAR)
               WHERE c.cluster_id = ? ORDER BY 1""",
            [str(path), str(members), str(cluster_id)],
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        con.close()

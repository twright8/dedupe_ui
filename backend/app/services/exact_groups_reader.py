# backend/app/services/exact_groups_reader.py
"""Filtered, sorted, paginated reads of a run's exact_groups.parquet.

DuckDB joins the groups to the records in the parquet files themselves, the way
``records_reader`` reads a run's records, so nothing is loaded into pandas and
the same code will cope with the 16 million PSC records later.

The groups file holds membership only (``rules/keys.py`` writes it). Everything
a reviewer wants on a group — how many members carry an old entity id, whether
those ids agree, how much money the group represents — is derived here by
joining to the records, so one fact is never stored in two places.
"""

import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from app import duckdb_conn, vocabulary

from app.profiles import get_profile
from app.services import index_chunks

GROUPS_FILENAME = "exact_groups.parquet"
#: One row per exact group, with what the screen shows. Written by stage 2
#: (`write_index`). Without it every request joined `exact_groups.parquet` to
#: `records.parquet` and grouped it — twice, once for the chips and once for
#: the page.
GROUP_INDEX_FILENAME = "exact_groups_index.parquet"
RECORDS_FILENAME = "records.parquet"
EVENTS_FILENAME = "events.parquet"
EVAL_FILENAME = "exact_eval.json"

DEFAULT_SORT = "size"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_MEMBERS = 500
MAX_EVENTS = 200

TRACKS = ("person", "organisation")
STATUSES = vocabulary.EXACT_GROUP_STATUSES
AGREEMENTS = vocabulary.AGREEMENTS
SORTS = ("size", "priority", "name")

MAX_EXISTING_IDS = 5
MAX_NAMES = 4


class ExactGroupsNotFound(Exception):
    """The run has no exact_groups.parquet — stage 2 has not run for it."""


class InvalidQuery(ValueError):
    """A filter, sort or order value the caller may not use."""


def groups_path(run_dir: str) -> Path:
    return Path(run_dir) / GROUPS_FILENAME


def index_path(run_dir: str) -> Path:
    return Path(run_dir) / GROUP_INDEX_FILENAME


def _index_fits(run_dir: str, priority: list[str]) -> bool:
    """The index is used only when it carries this profile's priority columns,
    which are positional, and is at least as new as the file it was made from."""
    index = index_path(run_dir)
    groups = groups_path(run_dir)
    if not index.is_file() or not groups.is_file():
        return False
    try:
        if index.stat().st_mtime < groups.stat().st_mtime:
            return False
        import pyarrow.parquet as pq

        held = set(pq.ParquetFile(index).schema_arrow.names)
    except Exception:
        return False
    wanted = {"group_id", "track", "status", "guard", "key_ids", "size",
              "n_labelled", "n_ids", "existing_ids", "names", "first_name",
              "member_hit", "agreement"}
    wanted |= {f"priority_{i}" for i in range(len(priority))}
    return wanted <= held


def write_index(run_dir) -> Path | None:
    """Write the per-group index. Stage 2 calls this when it has finished.

    The screen's own SQL builds it, so the file and the query it replaces
    cannot drift apart. The search is left out — it is per request — so a
    search still reads the two files and everything else reads this.
    """
    run_dir = Path(run_dir)
    if not groups_path(run_dir).is_file() or not records_path(run_dir).is_file():
        return None
    out = index_path(run_dir)
    con = duckdb_conn.reader_connect(run_dir)
    try:
        record_columns = _column_names(con, records_path(run_dir))
        priority = _priority_columns(record_columns)
        sql = _aggregate_sql(record_columns, priority, with_search=False)
        index_chunks.write(
            con, out, lambda where: f"SELECT * FROM ({sql}){where}",
            key="group_id", work=run_dir / "duckdb_tmp", prefix="exact_groups_index",
            params=[str(groups_path(run_dir)), str(records_path(run_dir))],
        )
    finally:
        con.close()
    return out


def records_path(run_dir: str) -> Path:
    return Path(run_dir) / RECORDS_FILENAME


def eval_path(run_dir: str) -> Path:
    return Path(run_dir) / EVAL_FILENAME


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
    cursor = con.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)])
    return [d[0] for d in cursor.description]


def _priority_columns(record_columns: list[str]) -> list[str]:
    """The profile's priority columns that this run's records actually carry."""
    return [c for c in get_profile().priority_columns if c in record_columns]


# ---------------------------------------------------------------------------
# The one query both endpoints build on
# ---------------------------------------------------------------------------


def _label_expression(record_columns: list[str]) -> str:
    """The existing entity id, with a blank read as "never reviewed"."""
    if "existing_entity_id" not in record_columns:
        return "CAST(NULL AS VARCHAR)"
    return "NULLIF(trim(CAST(r.existing_entity_id AS VARCHAR)), '')"


def _name_expression(record_columns: list[str]) -> str:
    return "CAST(r.name AS VARCHAR)" if "name" in record_columns else "CAST(NULL AS VARCHAR)"


def _aggregate_sql(record_columns: list[str], priority: list[str], with_search: bool) -> str:
    """One row per group: the counts, the sample names and the priority totals."""
    priority_select = "".join(
        f', sum(TRY_CAST("{column}" AS DOUBLE)) AS "priority_{index}"'
        for index, column in enumerate(priority)
    )
    search = (
        ", max(CASE WHEN lower(member_name) LIKE ? OR lower(record_id) LIKE ? "
        "THEN 1 ELSE 0 END) AS member_hit"
        if with_search else ", 0 AS member_hit"
    )
    return f"""
        WITH j AS (
            SELECT g.group_id, g.track, g.status, g.guard, g.key_ids, g.record_id,
                   {_name_expression(record_columns)} AS member_name,
                   {_label_expression(record_columns)} AS label
                   {"".join(f', r."{c}"' for c in priority)}
            FROM read_parquet(?) g
            JOIN read_parquet(?) r USING (record_id)
        ),
        agg AS (
            SELECT group_id,
                   any_value(track) AS track,
                   any_value(status) AS status,
                   any_value(guard) AS guard,
                   any_value(key_ids) AS key_ids,
                   count(*) AS size,
                   count(label) AS n_labelled,
                   count(DISTINCT label) AS n_ids,
                   list_slice(list_sort(list_distinct(list(label))), 1, {MAX_EXISTING_IDS})
                       AS existing_ids,
                   list_slice(list_sort(list_distinct(list(member_name))), 1, {MAX_NAMES})
                       AS names,
                   min(member_name) AS first_name
                   {priority_select}
                   {search}
            FROM j GROUP BY group_id
        )
        SELECT *, CASE WHEN status <> 'merged' THEN NULL
                       WHEN n_ids >= 2 THEN 'conflict'
                       WHEN n_labelled = 0 THEN 'new'
                       WHEN size > n_labelled THEN 'extends'
                       ELSE 'consistent' END AS agreement
        FROM agg
    """


#: A guard's reason is machine-readable — ``max_group_size:12>10`` — so the
#: frontend can parse it and a test can assert on it. It is also printed on the
#: Exact groups screen, where ``name_core`` and a bare ``>`` mean nothing to a
#: researcher. ``guard_text`` is the same fact as a sentence, built here at read
#: time so the stored value never changes (docs/BACKEND_STRINGS.md §4).
def guard_text(guard, column_labels: dict | None = None) -> str | None:
    """One guard reason as a sentence, or None when the group passed."""
    if not guard:
        return None
    text = str(guard)
    kind, _, detail = text.partition(":")
    labels = column_labels or {}
    if kind == "max_group_size" and ">" in detail:
        found, limit = detail.split(">", 1)
        return (f"This match key would have put {found} records together, and the "
                f"limit is {limit}.")
    if kind == "max_distinct" and "=" in detail and ">" in detail:
        column, rest = detail.split("=", 1)
        found, limit = rest.split(">", 1)
        name = labels.get(column) or _plain_column(column)
        return (f"The records here hold {found} different values of {name}, and "
                f"the limit is {limit}.")
    if kind == "require_any_equal":
        name = labels.get(detail) or _plain_column(detail)
        return f"The records here do not all agree on {name}."
    if kind == "blocklist":
        return "The value this key matched on is on the blocklist."
    return text


def _plain_column(column: str) -> str:
    """A cleaned column name as a phrase. One definition, in ``pairs_reader``.

    Imported inside the function because ``pairs_reader`` reads this module's
    neighbours at import time, and one word must not cost a cycle.
    """
    from app.services.pairs_reader import plain_column

    return plain_column(column)


def _item(row: dict, priority: list[str]) -> dict:
    key_ids = [k for k in str(row.get("key_ids") or "").split("|") if k]
    return {
        "group_id": row["group_id"],
        "track": row["track"],
        "status": row["status"],
        "guard": row["guard"],
        "guard_text": guard_text(row["guard"]),
        "key_ids": key_ids,
        "size": int(row["size"]),
        "n_labelled": int(row["n_labelled"]),
        "existing_ids": list(row["existing_ids"] or [])[:MAX_EXISTING_IDS],
        "agreement": row["agreement"],
        "names": [n for n in list(row["names"] or []) if n][:MAX_NAMES],
        "priority": {
            column: _json_safe(row.get(f"priority_{index}"))
            for index, column in enumerate(priority)
        },
    }


def _rows(cursor) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise InvalidQuery(f"{label} must be one of {', '.join(allowed)}")


def _open(run_dir: str):
    groups = groups_path(run_dir)
    records = records_path(run_dir)
    if not groups.is_file() or not records.is_file():
        raise ExactGroupsNotFound(str(groups))
    return duckdb_conn.reader_connect(run_dir), groups, records


def get_groups(
    run_dir: str,
    track: str | None = None,
    key: str | None = None,
    status: str | None = None,
    agreement: str | None = None,
    q: str | None = None,
    sort: str = DEFAULT_SORT,
    order: str = "desc",
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """One page of a run's exact groups.

    ``total`` reflects the filters. ``counts`` describe the whole run and ignore
    them, so the status and agreement tabs stay still while a search narrows the
    list below — the same contract as the records API.
    """
    _check(track, TRACKS, "track")
    _check(status, STATUSES, "status")
    _check(agreement, AGREEMENTS, "agreement")
    sort_key = sort or DEFAULT_SORT
    if sort_key not in SORTS:
        raise InvalidQuery(f"sort must be one of {', '.join(SORTS)}")
    order_sql = {"asc": "ASC", "desc": "DESC"}.get((order or "desc").lower())
    if order_sql is None:
        raise InvalidQuery("order must be asc or desc")

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    con, groups, records = _open(run_dir)
    try:
        record_columns = _column_names(con, records)
        priority = _priority_columns(record_columns)
        # A search asks a question about the members, so it still reads the two
        # files. Everything else reads the index.
        indexed = not q and _index_fits(run_dir, priority)
        if indexed:
            base = "SELECT * FROM read_parquet(?)"
            base_params: list = [str(index_path(run_dir))]
        else:
            base = _aggregate_sql(record_columns, priority, with_search=bool(q))
            # DuckDB binds ? in the order they appear in the text: the two
            # parquet paths in the first CTE, then the search in the aggregate.
            base_params = [str(groups), str(records)]
            if q:
                pattern = f"%{q.lower()}%"
                base_params = [str(groups), str(records), pattern, pattern]

        where: list[str] = []
        params: list = []
        if track is not None:
            where.append("track = ?")
            params.append(track)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if agreement is not None:
            where.append("agreement = ?")
            params.append(agreement)
        if key is not None:
            # key_ids is a '|'-joined list, so a bare LIKE would match k1 inside
            # k12. Padding both sides makes the comparison whole-token.
            where.append("('|' || key_ids || '|') LIKE ?")
            params.append(f"%|{key}|%")
        if q:
            where.append("(member_hit = 1 OR lower(group_id) LIKE ?)")
            params.append(f"%{q.lower()}%")
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        # The counts describe the whole run, so they are taken from a base with
        # no search in it at all.
        unfiltered, unfiltered_params = (
            (base, base_params) if indexed
            else (_aggregate_sql(record_columns, priority, with_search=False),
                  [str(groups), str(records)])
        )
        counts_row = con.execute(
            f"""SELECT count(*) FILTER (WHERE status = 'merged'),
                       count(*) FILTER (WHERE status = 'held'),
                       count(*) FILTER (WHERE agreement = 'consistent'),
                       count(*) FILTER (WHERE agreement = 'conflict'),
                       count(*) FILTER (WHERE agreement = 'extends'),
                       count(*) FILTER (WHERE agreement = 'new')
                FROM ({unfiltered})""",
            unfiltered_params,
        ).fetchone()
        counts = dict(zip(
            ("merged", "held", "consistent", "conflict", "extends", "new"),
            (int(value) for value in counts_row),
        ))

        total = int(con.execute(
            f"SELECT count(*) FROM ({base}){where_sql}", [*base_params, *params]
        ).fetchone()[0])

        sort_sql = {
            "size": "size",
            "name": "first_name",
            "priority": " + ".join(
                f'COALESCE("priority_{index}", 0)' for index in range(len(priority))
            ) or "size",
        }[sort_key]

        cursor = con.execute(
            f"""SELECT * FROM ({base}){where_sql}
                ORDER BY {sort_sql} {order_sql} NULLS LAST, group_id ASC
                LIMIT ? OFFSET ?""",
            [*base_params, *params, limit, offset],
        )
        items = [_item(row, priority) for row in _rows(cursor)]
    finally:
        con.close()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": items,
        "counts": counts,
    }


def _group_events(con, groups: Path, run_dir: str, group_id: str) -> tuple[list[dict], bool]:
    """The evidence rows behind a group's members, newest first (D13b)."""
    events = Path(run_dir) / EVENTS_FILENAME
    if not events.is_file():
        return [], False
    cursor = con.execute(
        """SELECT e.* FROM read_parquet(?) g
           JOIN read_parquet(?) e
             ON CAST(e.record_id AS VARCHAR) = CAST(g.record_id AS VARCHAR)
           WHERE g.group_id = ?
           ORDER BY e.date DESC NULLS LAST, CAST(e.record_id AS VARCHAR)
           LIMIT ?""",
        [str(groups), str(events), group_id, MAX_EVENTS + 1],
    )
    rows = _rows(cursor)
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows[:MAX_EVENTS]], \
        len(rows) > MAX_EVENTS


def get_group(run_dir: str, group_id: str, with_events: bool = False) -> dict | None:
    """One group with its member records, or None when there is no such group.

    *with_events* adds the profile's evidence rows for those members. They are
    off by default: the group list does not need them, and a large group carries
    a great many.
    """
    con, groups, records = _open(run_dir)
    events: list[dict] = []
    events_truncated = False
    try:
        record_columns = _column_names(con, records)
        priority = _priority_columns(record_columns)
        base = _aggregate_sql(record_columns, priority, with_search=False)
        rows = _rows(con.execute(
            f"SELECT * FROM ({base}) WHERE group_id = ?",
            [str(groups), str(records), group_id],
        ))
        if not rows:
            return None
        item = _item(rows[0], priority)

        cursor = con.execute(
            """SELECT r.* FROM read_parquet(?) g
               JOIN read_parquet(?) r USING (record_id)
               WHERE g.group_id = ?
               ORDER BY r.record_id
               LIMIT ?""",
            [str(groups), str(records), group_id, MAX_MEMBERS + 1],
        )
        names = [d[0] for d in cursor.description]
        members = [
            {name: _json_safe(value) for name, value in zip(names, row)}
            for row in cursor.fetchall()
        ]
        if with_events:
            events, events_truncated = _group_events(con, groups, run_dir, group_id)
    finally:
        con.close()

    item["members_truncated"] = len(members) > MAX_MEMBERS
    item["members"] = members[:MAX_MEMBERS]
    if with_events:
        item["events"] = events
        item["events_truncated"] = events_truncated
        item["event_columns"] = [
            column.as_dict() for column in getattr(get_profile(), "event_columns", [])
        ]
    return item

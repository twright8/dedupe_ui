# backend/app/pipeline/dedupe/units.py
"""Turn records and exact groups into the units stage 3 scores.

``LINKAGE.md`` sets the rule: a merged exact group is one unit, and every other
record is a unit of its own — including each member of a held group, because a
guard held that group back precisely so a human could decide it.

A unit gets one representative row. For every column the records carry, the
representative takes the most frequent non-null value among the members, with
ties broken by the smallest ``record_id``. The profile's priority columns are
summed instead, because a reviewer sorting by money wants the pair's total.

**The build never holds the units frame in pandas.** It reads records and exact
groups, and it writes ``units.parquet`` and ``unit_members.parquet``, entirely
in DuckDB. The old pandas build needed about 2.6 GB of peak RSS per million
records, which put the 15 million PSC records at roughly 84 GB on a 29 GB
machine. Two things made that so, and neither is the modal vote:

* scattering the voted values back into a pandas column, one column at a time;
* materialising the whole units frame, then ordering its columns and sorting it.

So the build is two SQL branches that are ``UNION ALL``ed and copied to parquet:

1. **a unit of one passes straight through.** Its representative is its own row,
   its ``unit_size`` is 1, its priority columns are its own values and its
   entity ids come from that one record. About 94% of the PSC sample and two
   thirds of the full snapshot are in this case, so this is where the win is.
2. **a pooled unit is voted on**, per column, with the tie-break above.

``build_units_files`` is the out-of-core entry point: it takes paths (or
frames), writes the two parquet files and returns the counts. ``build_units``
keeps the old in-memory signature for callers and tests that want the frames;
it builds the same two files and reads them back.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app import duckdb_conn
from app.profiles import get_profile
from app.rules import keys

UNITS_FILENAME = "units.parquet"
UNIT_MEMBERS_FILENAME = "unit_members.parquet"

# The joined form of several existing entity ids, matching the donations
# profile's separator for multi-valued cells.
ID_SEPARATOR = " | "

LABEL_COLUMN = "existing_entity_id"

# Columns the representative never carries a modal value for: they are either
# replaced by a unit-level column or meaningless once members are pooled.
_NOT_REPRESENTED = (LABEL_COLUMN,)

#: How many member rows the profile's own aggregate hook may hold at once. The
#: hook is a pandas one and takes whole frames, so it is fed whole units a
#: batch at a time rather than the whole pooled frame.
AGGREGATE_BATCH_ENV = "UNIT_AGGREGATE_BATCH_ROWS"
DEFAULT_AGGREGATE_BATCH = 500_000

#: How many threads write the two parquet files. The sort and the parquet
#: writer each hold their working set per thread, and that is the one part of
#: this build that still grows with the number of units: at four million
#: records the write peaked at 7.3 GB on 24 threads and 5.5 GB on four, for
#: the same wall time. DuckDB's own advice for a large write is fewer threads.
WRITE_THREADS_ENV = "UNIT_WRITE_THREADS"
DEFAULT_WRITE_THREADS = 4

#: How many per-column vote tables are folded into ``voted`` at once. A hash
#: join's build side is pinned and cannot be spilled, so joining all of a
#: sixty-column frame's votes in one statement is bounded by nothing — see
#: ``_vote_table``.
VOTE_JOIN_BATCH_ENV = "UNIT_VOTE_JOIN_BATCH"
DEFAULT_VOTE_JOIN_BATCH = 8

#: What ``astype(str)`` produces here. pandas 3 has a ``str`` dtype; pandas 2
#: gives object. The unit ids carried that dtype in the old build, so they
#: still do.
_TEXT_DTYPE = str(pd.Series([""], dtype=object).astype(str).dtype)

_NUMPY_INTS = {"int8", "int16", "int32", "int64",
               "uint8", "uint16", "uint32", "uint64"}

_DUCK_BY_DTYPE = {
    "object": "VARCHAR", "str": "VARCHAR",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
    "int8": "TINYINT", "int16": "SMALLINT", "int32": "INTEGER", "int64": "BIGINT",
    "Int8": "TINYINT", "Int16": "SMALLINT", "Int32": "INTEGER", "Int64": "BIGINT",
    "uint8": "UTINYINT", "uint16": "USMALLINT",
    "uint32": "UINTEGER", "uint64": "UBIGINT",
    "float32": "FLOAT", "float64": "DOUBLE",
    "Float32": "FLOAT", "Float64": "DOUBLE",
}


# ---------------------------------------------------------------------------
# Small helpers kept from the pandas build
# ---------------------------------------------------------------------------


def _as_text(series: pd.Series) -> pd.Series:
    return series.astype(str)


def plain_strings(frame: pd.DataFrame) -> pd.DataFrame:
    """Arrow-backed string columns as plain object ones.

    Parquet hands pandas Arrow-backed strings. Anything that groups or compares
    them row by row pays for a pyarrow scalar each time, so the frame is put
    back on plain Python objects before any of that. Note that rebuilding a
    Series from ``to_numpy`` does not do it — pandas re-infers the string dtype;
    ``astype(object)`` is the conversion that sticks.
    """
    converted = {}
    for column in frame.columns:
        if _plain_dtype(frame[column].dtype) == "object" != str(frame[column].dtype):
            converted[column] = frame[column].astype(object)
    return frame.assign(**converted) if converted else frame


def _quote(name: str) -> str:
    """An identifier DuckDB will read as one name, whatever is in it."""
    return '"' + str(name).replace('"', '""') + '"'


def _text(value) -> str:
    """A SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


#: What Python's ``str.strip()`` removes from an ASCII string. DuckDB's bare
#: ``trim`` takes spaces and nothing else, and the pandas build trimmed the
#: existing entity id with ``str.strip()``, so a label ending in a tab would
#: have come out differently in the two builds.
_TRIM_CHARS = " \t\n\r\v\f"


def _trim(expression: str) -> str:
    """``str.strip()`` for an ASCII label, in SQL."""
    return f"trim({expression}, {_text(_TRIM_CHARS)})"


def _nullable(values, keep, index) -> pd.Series:
    """An object column whose missing entries are ``None``, never ``NaN``.

    ``Series.where(cond, None)`` fills with NaN, which then reads back as a
    float the API has to strip out again. numpy keeps the None.
    """
    return pd.Series(np.where(keep, values, None), index=index, dtype="object")


def unit_membership(records: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    """``record_id``, ``unit_id`` and ``held_group_id`` for every record.

    A record in a merged group joins that group's unit. Everything else is its
    own unit, so a held group's members stay separate and can still be told
    apart on the review screen by the ``held_group_id`` they share.

    The out-of-core build does this in SQL. This is the pandas statement of the
    same rule, kept because it is the readable one and the tests read it.
    """
    record_ids = _as_text(records["record_id"])
    members = pd.DataFrame({"record_id": record_ids.to_numpy()})
    members["unit_id"] = members["record_id"]

    if len(groups):
        group_records = _as_text(groups["record_id"])
        merged = groups["status"].to_numpy() == keys.MERGED

        if merged.any():
            # The unit id is the smallest member record id, which is exactly what
            # the group id spells, but recomputing it keeps the two independent.
            merged_frame = pd.DataFrame({
                "record_id": group_records[merged].to_numpy(),
                "group_id": groups.loc[merged, "group_id"].to_numpy(),
            })
            smallest = merged_frame.groupby("group_id")["record_id"].min()
            merged_frame["unit_id"] = merged_frame["group_id"].map(smallest)
            mapping = merged_frame.set_index("record_id")["unit_id"]
            mapped = members["record_id"].map(mapping)
            members["unit_id"] = mapped.where(mapped.notna(), members["unit_id"])

        held = ~merged
        if held.any():
            held_frame = pd.DataFrame({
                "record_id": group_records[held].to_numpy(),
                "held_group_id": groups.loc[held, "group_id"].to_numpy(),
            })
            # A record may sit in several held groups; the smallest id is the
            # stable one to show.
            first = held_frame.groupby("record_id")["held_group_id"].min()
            mapped = members["record_id"].map(first)
            members["held_group_id"] = _nullable(mapped, mapped.notna(), members.index)
        else:
            members["held_group_id"] = None
    else:
        members["held_group_id"] = None

    return members


def representatives(joined: pd.DataFrame, columns: list[str],
                    temp_dir=None) -> pd.DataFrame:
    """The representative value of every column, for every unit, in one pass.

    The rule (`docs/LINKAGE.md`) is the most frequent non-null value, ties going
    to the smallest ``record_id``. Pandas does that with a group-by per column,
    and on Arrow-backed strings it falls back to a Python loop per group — ten
    seconds for 52,000 records, and the best part of an hour at PSC scale.

    DuckDB does the same work set-based. ``mode()`` is no use because its
    tie-break is not the one the contract names, so each column is counted and
    ranked: ``count(*) DESC`` then ``min(record_id) ASC``, which is the rule
    exactly. One query per column keeps every column's own type, which an
    unpivot into a single VARCHAR would throw away.

    The out-of-core build runs this same SQL over a DuckDB table rather than a
    pandas frame. This entry point takes a frame, and is what pins the rule.
    """
    result = pd.DataFrame(index=pd.Index([], name="unit_id"))
    if not len(joined) or not columns:
        return result

    con = duckdb_conn.connect(temp_dir)
    try:
        con.register("j", joined)
        frames = []
        for column in columns:
            frames.append(
                con.execute(_vote_sql("j", column)).df().set_index("unit_id")
            )
    finally:
        con.close()
    return pd.concat(frames, axis=1) if frames else result


def _vote_sql(source: str, column: str) -> str:
    """The modal vote for one column: most frequent non-null, ties to min id.

    ``record_id`` is compared as text, because that is the rule
    ``docs/LINKAGE.md`` states and the pandas build compared strings — it put
    the ids through ``astype(str)`` before anything grouped them. The cast is
    free inside ``build_units_files``, where the column is already VARCHAR, and
    it is what makes ``representatives()`` obey the rule for a caller whose
    frame still carries integer ids.
    """
    quoted = _quote(column)
    return f"""
        SELECT unit_id, {quoted} FROM (
            SELECT unit_id, {quoted},
                   row_number() OVER (
                       PARTITION BY unit_id
                       ORDER BY count(*) DESC, min(CAST(record_id AS VARCHAR)) ASC
                   ) AS vote_rank
            FROM {source}
            WHERE {quoted} IS NOT NULL
            GROUP BY unit_id, {quoted}
        ) WHERE vote_rank = 1
    """


# ---------------------------------------------------------------------------
# Dtypes: what the pandas build produced, so the parquet still says it
# ---------------------------------------------------------------------------


def _plain_dtype(dtype) -> str:
    """A dtype name with every string dtype flattened to ``object``.

    ``plain_strings`` put Arrow-backed strings on plain objects before anything
    grouped them, so that is the dtype the old build wrote.
    """
    name = str(dtype)
    if name in ("str", "string", "large_string") or name.startswith("string["):
        return "object"
    return name


def _pandas_type(dtype: str) -> str:
    """The ``pandas_type`` key pyarrow writes beside a column."""
    if dtype in ("object", "str"):
        return "unicode"
    if dtype.startswith("datetime"):
        return "datetime"
    return dtype.lower()


def _duck_type(dtype: str, fallback: str) -> str:
    """The DuckDB type a column is cast to before it is written.

    An object column that is not text — a list, a struct — keeps whatever type
    DuckDB read it as. Casting one of those to VARCHAR is the bug that turns a
    list column into a string and passes every shape check on the way.
    """
    if dtype in ("object", "str") and fallback and fallback.upper() != "VARCHAR":
        return fallback
    return _DUCK_BY_DTYPE.get(dtype, fallback or "VARCHAR")


def _numeric_dtype(dtype: str) -> str:
    """What ``pd.to_numeric`` then ``sum(min_count=1)`` leaves a column as."""
    if dtype in _NUMPY_INTS or dtype.startswith(("float", "Float", "Int", "UInt")):
        return dtype
    if dtype == "bool":
        return "int64"
    if dtype == "boolean":
        return "Int64"
    return "float64"


def _sum_type(dtype: str) -> str:
    """The DuckDB type a priority column is summed in."""
    return _DUCK_BY_DTYPE.get(_numeric_dtype(dtype), "DOUBLE")


def _records_shape(con, source) -> tuple[dict, set]:
    """``({column: pandas dtype}, {columns that are entirely null})``.

    The dtype is the one the caller's own ``read_parquet`` would give, because
    that is what the old build wrote out. An integer parquet column that holds
    a null reads back as ``float64``, so the null counts decide it — and they
    come out of the file's footer, not out of a scan.

    A column with no value anywhere matters for its own reason: pandas wrote
    such a column as an Arrow ``null``, which reads back as ``object`` where a
    string column reads back as text. A unit column is empty exactly when its
    records column is, because every unit takes some member's value.
    """
    if isinstance(source, pd.DataFrame):
        dtypes = {c: _plain_dtype(source[c].dtype) for c in source.columns}
        empty = {c for c in source.columns
                 if len(source) and bool(source[c].isna().all())}
        return dtypes, empty

    parquet = pq.ParquetFile(os.fspath(source))
    schema = parquet.schema_arrow
    dtypes = {str(c): _plain_dtype(d)
              for c, d in schema.empty_table().to_pandas().dtypes.items()}
    total = parquet.metadata.num_rows
    nulls = {name: 0 for name in dtypes}
    unknown = set()
    metadata = parquet.metadata
    for group in range(metadata.num_row_groups):
        row_group = metadata.row_group(group)
        for index in range(row_group.num_columns):
            chunk = row_group.column(index)
            name = chunk.path_in_schema
            if name not in nulls:
                continue
            if chunk.is_stats_set and chunk.statistics.has_null_count:
                nulls[name] += chunk.statistics.null_count
            else:
                unknown.add(name)
    if unknown:
        counts = ", ".join(f"count(*) - count({_quote(c)})" for c in sorted(unknown))
        row = con.execute(f"SELECT {counts} FROM rec").fetchone()
        for name, count in zip(sorted(unknown), row):
            nulls[name] = int(count)

    empty = set()
    for name, dtype in list(dtypes.items()):
        missing = nulls.get(name, 0)
        if total and missing == total:
            empty.add(name)
        if missing:
            if dtype in _NUMPY_INTS:
                dtypes[name] = "float64"
            elif dtype == "bool":
                dtypes[name] = "object"
    return dtypes, empty


def _pandas_metadata(dtypes: dict) -> str:
    """The ``pandas`` key pyarrow writes into a parquet footer.

    DuckDB's ``COPY`` can carry it through ``KV_METADATA``, and it is the only
    way a column comes back as ``Int64`` rather than ``int64``: the Arrow type
    is the same either way and the dtype lives in this block.
    """
    return json.dumps({
        "index_columns": [],
        "column_indexes": [],
        "columns": [
            {"name": name, "field_name": name,
             "pandas_type": _pandas_type(dtype), "numpy_type": dtype,
             "metadata": None}
            for name, dtype in dtypes.items()
        ],
        "creator": {"library": "pyarrow", "version": pa.__version__},
        "pandas_version": pd.__version__,
    })


# ---------------------------------------------------------------------------
# Binding the inputs
# ---------------------------------------------------------------------------


@contextmanager
def _scratch_database(temp_dir):
    """A DuckDB connection whose own tables can be paged out to disk.

    The build keeps a few tables of its own — the membership, the unit sizes,
    the pooled rows, the per-column votes. In an in-memory DuckDB those are
    pinned: they count against ``memory_limit`` and cannot be offloaded, so at
    sixteen million records the membership alone filled the budget and the
    build died before it reached the write. Backed by a file they are ordinary
    table storage, and the buffer manager evicts what it is not using.

    The file is the run's scratch, beside the spill, and it goes when the build
    does.
    """
    import duckdb

    holder = None
    if temp_dir is None:
        holder = tempfile.TemporaryDirectory()
        scratch = Path(holder.name)
    else:
        scratch = Path(temp_dir)
        scratch.mkdir(parents=True, exist_ok=True)
    database = scratch / f"units_build_{os.getpid()}.duckdb"
    for stale in (database, Path(f"{database}.wal")):
        stale.unlink(missing_ok=True)
    con = duckdb_conn.configure(duckdb.connect(str(database)), scratch)
    try:
        yield con
    finally:
        con.close()
        for leftover in (database, Path(f"{database}.wal")):
            leftover.unlink(missing_ok=True)
        if holder is not None:
            holder.cleanup()


def _bind(con, name: str, source) -> bool:
    """Expose *source* — a frame or a parquet path — as a DuckDB view."""
    if source is None:
        return False
    if isinstance(source, pd.DataFrame):
        con.register(f"_src_{name}", source)
        con.execute(f"CREATE OR REPLACE VIEW {name} AS "
                    f"SELECT * FROM _src_{name}")
    else:
        con.execute(f"CREATE OR REPLACE VIEW {name} AS "
                    f"SELECT * FROM read_parquet({_text(os.fspath(source))})")
    return True


def _describe(con, name: str) -> dict:
    return {row[0]: row[1] for row in con.execute(f"DESCRIBE {name}").fetchall()}


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def _representative_columns(columns: list[str], priority: list[str]) -> list[str]:
    skip = {"record_id", *priority, *_NOT_REPRESENTED}
    return [c for c in columns if c not in skip]


def aggregate_batch_rows() -> int:
    try:
        return max(1, int(os.environ.get(AGGREGATE_BATCH_ENV,
                                         DEFAULT_AGGREGATE_BATCH)))
    except ValueError:
        return DEFAULT_AGGREGATE_BATCH


def vote_join_batch() -> int:
    try:
        return max(1, int(os.environ.get(VOTE_JOIN_BATCH_ENV,
                                         DEFAULT_VOTE_JOIN_BATCH)))
    except ValueError:
        return DEFAULT_VOTE_JOIN_BATCH


def write_threads() -> int:
    try:
        return max(1, int(os.environ.get(WRITE_THREADS_ENV,
                                         DEFAULT_WRITE_THREADS)))
    except ValueError:
        return DEFAULT_WRITE_THREADS


def _any_single_label(con, trimmed: str, pooled_units: int) -> bool:
    """Does any unit carry exactly one existing entity id?

    A unit of one always does when its record is labelled, and a pooled unit
    does when its members agree. If nothing does, ``existing_entity_id`` has no
    value anywhere and has to be written as an Arrow ``null``, which is what
    pandas wrote.
    """
    if pooled_units and con.execute(
            "SELECT EXISTS(SELECT 1 FROM labels WHERE n_existing_ids = 1)"
    ).fetchone()[0]:
        return True
    return bool(con.execute(f"""
        SELECT EXISTS(
            SELECT 1 FROM rec r
            LEFT JOIN pooled_members m
              ON m.record_id = CAST(r.record_id AS VARCHAR)
            WHERE m.record_id IS NULL AND {trimmed} <> ''
        )
    """).fetchone()[0])


def _profile_aggregates(con, has_events: bool, pooled_units: int,
                        batch_rows: int) -> tuple[dict, set, list]:
    """Run the profile's own per-unit hook over the pooled units.

    ``Profile.aggregate_unit_columns`` is a pandas hook that takes whole frames,
    and the frames it used to take were the whole records join and the whole
    events join. Two things fix that without changing what it computes.

    **It runs only over the pooled units.** A unit of one is its own
    representative, so the hook's answer for it is the value the record already
    carries — measured, not assumed: on the donations run every one of the
    hook's eight columns agrees with the record's own value for all 16,410
    single-record units, and on PSC ``n_companies`` is 1 for every record and no
    record has a null company number, which is what ``nunique`` gives. A single
    therefore passes its own value straight through.

    **It is fed whole units, a batch at a time.** The hook is defined per unit —
    "per-unit columns a modal vote would get wrong", indexed by ``unit_id`` — so
    a batch that never splits a unit gives the same answer as one call. That is
    what keeps its memory flat: without it the pooled frame at the full PSC
    snapshot is five million rows of sixty-odd columns.

    Returns ``({column: dtype}, {columns that are entirely null}, [columns])``
    and leaves the values in the table ``agg``.
    """
    if not pooled_units:
        return {}, set(), []

    profile = get_profile()
    con.execute(f"""
        CREATE OR REPLACE TABLE agg_batch AS
        SELECT unit_id,
               CAST((sum(unit_size) OVER (ORDER BY unit_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                     - unit_size) / {int(batch_rows)} AS BIGINT) AS batch
        FROM pooled_sizes
    """)
    batches = con.execute("SELECT coalesce(max(batch), 0) FROM agg_batch").fetchone()[0]
    event_select = ""
    if has_events:
        event_select = ", ".join(
            f"CAST(e.{_quote(c)} AS VARCHAR) AS {_quote(c)}" if c == "record_id"
            else f"e.{_quote(c)}"
            for c in _describe(con, "ev")
        )

    parts = []
    for batch in range(int(batches) + 1):
        members = con.execute(
            "SELECT j.* FROM pooled j JOIN agg_batch b ON b.unit_id = j.unit_id "
            f"WHERE b.batch = {batch}"
        ).df()
        events = None
        if has_events:
            events = con.execute(f"""
                SELECT {event_select}, m.unit_id, m.held_group_id
                FROM ev e
                JOIN pooled_members m ON m.record_id = CAST(e.record_id AS VARCHAR)
                JOIN agg_batch b ON b.unit_id = m.unit_id
                WHERE b.batch = {batch}
            """).df()
        found = profile.aggregate_unit_columns(members, events)
        if found is not None and len(found):
            if found.index.name is None:
                found = found.rename_axis("unit_id")
            parts.append(found.reset_index())
        del members, events

    if not parts:
        return {}, set(), []

    replacements = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    del parts
    columns = [c for c in replacements.columns if c != "unit_id"]
    dtypes = {c: str(replacements[c].dtype) for c in columns}
    empty = {c for c in columns if bool(replacements[c].isna().all())}
    con.register("_src_agg", replacements)
    con.execute("CREATE OR REPLACE TABLE agg AS SELECT * FROM _src_agg")
    con.unregister("_src_agg")
    return dtypes, empty, columns


def _vote_table(con, represented: list[str], batch: int) -> None:
    """``voted``: one row per pooled unit, one column per represented column.

    **The joins have to be batched.** Each column's vote is its own table, and
    joining all of them to the unit list in one statement gives DuckDB one hash
    table per column, all live at once. A hash join's build side is *pinned* —
    the buffer manager cannot evict it — so the memory limit does not bound it
    and the query fails rather than spilling. At the full PSC snapshot that is
    61 hash tables over 1.8 million pooled units, and it died on an 8 GB limit
    with ``failed to pin block of size 256.0 KiB (7.4 GiB/7.4 GiB used)``.

    So ``voted`` is grown a batch of columns at a time. Each step has at most
    *batch* hash tables live, and each vote table is dropped once it has been
    folded in. The cost is rewriting ``voted`` once per batch, which is a
    sequential scan and write of a table with at most one row per pooled unit —
    a third of the records at PSC scale and a sixteenth on the sample.
    """
    for index, column in enumerate(represented):
        con.execute(f"CREATE OR REPLACE TABLE vote_{index} AS "
                    + _vote_sql("pooled", column))
    con.execute("CREATE OR REPLACE TABLE voted AS SELECT unit_id FROM pooled_sizes")
    for start in range(0, len(represented), max(1, batch)):
        chunk = list(enumerate(represented))[start:start + max(1, batch)]
        joins = "\n".join(
            f"LEFT JOIN vote_{index} v{index} ON v{index}.unit_id = u.unit_id"
            for index, _ in chunk
        )
        columns = "".join(f", v{index}.{_quote(column)}" for index, column in chunk)
        con.execute(f"""
            CREATE OR REPLACE TABLE voted_next AS
            SELECT u.*{columns} FROM voted u
            {joins}
        """)
        con.execute("DROP TABLE voted")
        con.execute("ALTER TABLE voted_next RENAME TO voted")
        for index, _ in chunk:
            con.execute(f"DROP TABLE vote_{index}")


def _check_aggregate_columns(agg_columns, represented, priority) -> None:
    """A hook column a unit of one could not be given a value for.

    The hook runs over the pooled units only, because a unit of one is its own
    representative and the record already carries the answer. That holds
    exactly while every column the hook returns is also a records column: both
    shipped profiles are in that case, and the donations run was measured
    column by column against the record's own value for all 16,410
    single-record units.

    A hook that invents a column is a different thing. The pooled units would
    get it and every unit of one would get a null, and nothing would say so.
    The pandas build ran the hook over every unit, so this is where that
    contract narrowed, and it fails here rather than in the file.
    """
    known = set(represented) | set(priority)
    unknown = [c for c in agg_columns if c not in known]
    if unknown:
        raise ValueError(
            "Profile.aggregate_unit_columns returned "
            + ", ".join(repr(c) for c in unknown)
            + ", which the records do not carry. The hook runs over the pooled "
            "units only, so a unit of one has no value to fall back on and the "
            "column would be null for every single-record unit. Add the column "
            "to the records, or compute it in the profile's loader."
        )


def _write(con, sql: str, path, dtypes: dict, empty: set) -> None:
    """Write the result of *sql* to parquet, with the dtypes pandas will read.

    ``COPY`` does it, and carries the pandas block that makes an ``Int64``
    column come back as one. The exception is a column with no value anywhere:
    pandas wrote those as an Arrow ``null``, DuckDB has no such type, and a
    VARCHAR of nulls reads back as text rather than as objects. Those files go
    out through a streaming Arrow writer instead — one record batch at a time,
    so the frame is still never whole in memory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = _pandas_metadata(dtypes)
    threads = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    con.execute(f"SET threads={min(threads, write_threads())}")
    try:
        _write_rows(con, sql, path, metadata, dtypes, empty)
    finally:
        con.execute(f"SET threads={threads}")


def _write_rows(con, sql, path, metadata, dtypes, empty) -> None:
    if not empty:
        con.execute(f"COPY ({sql}) TO {_text(path)} "
                    f"(FORMAT parquet, KV_METADATA {{ pandas: {_text(metadata)} }})")
        return

    result = con.execute(sql)
    reader = (result.to_arrow_reader(100_000)
              if hasattr(result, "to_arrow_reader")
              else result.fetch_record_batch(100_000))
    schema = pa.schema(
        [pa.field(f.name, pa.null()) if f.name in empty else f for f in reader.schema],
        metadata={b"pandas": metadata.encode("utf-8")},
    )
    blank = {field.name for field in schema if pa.types.is_null(field.type)}
    writer = pq.ParquetWriter(path, schema)
    try:
        for batch in reader:
            arrays = [
                pa.nulls(batch.num_rows) if field.name in blank
                else batch.column(index).cast(field.type)
                for index, field in enumerate(schema)
            ]
            writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
    finally:
        writer.close()


def build_units_files(records, groups, units_path, members_path,
                      events=None, temp_dir=None, batch_rows: int | None = None) -> dict:
    """Write ``units.parquet`` and ``unit_members.parquet``, out of core.

    *records*, *groups* and *events* are parquet paths or pandas frames. The
    whole build happens in DuckDB: the units frame is never a pandas object.
    Returns ``{"units": n, "unit_members": n}``.

    The shape of it, and why it is that shape. Only a merged exact group pools
    records, so the set of pooled records is exactly the records a merged group
    names — a sixteenth of the PSC sample and a third of the full snapshot.
    Everything expensive is kept inside that set: the modal vote, the priority
    totals, the entity ids of a pooled unit and the profile's own hook. The
    other records never take part in a join wider than a lookup against it.
    """
    profile = get_profile()
    batch_rows = batch_rows or aggregate_batch_rows()
    with _scratch_database(temp_dir) as con:
        _bind(con, "rec", records)
        _bind(con, "grp", groups)
        has_events = _bind(con, "ev", events)

        record_types = _describe(con, "rec")
        record_columns = list(record_types)
        dtypes, empty_records = _records_shape(con, records)
        priority = [c for c in profile.priority_columns if c in record_columns]
        represented = _representative_columns(record_columns, priority)
        has_label = LABEL_COLUMN in record_columns
        label = _quote(LABEL_COLUMN)
        trimmed = _trim(f"CAST(r.{label} AS VARCHAR)")
        merged = _text(keys.MERGED)

        # A floating point total depends on the order the values are added in,
        # and pandas adds them in records order, so the row number rides along
        # with them — see the priority block below.
        float_priority = any(
            _sum_type(dtypes.get(c, "object")) in ("DOUBLE", "FLOAT")
            for c in priority
        )
        carried = "".join(
            f", TRY_CAST({_quote(c)} AS {_sum_type(dtypes.get(c, 'object'))})"
            f" AS {_quote(c)}"
            for c in priority
        )
        if float_priority:
            carried += ", row_number() OVER () AS record_seq"

        con.execute(f"""
            CREATE OR REPLACE TABLE ids AS
            SELECT CAST(record_id AS VARCHAR) AS record_id{carried} FROM rec
        """)
        # Which unit a merged group makes: the smallest record id in it, which
        # is what the group id spells, recomputed so the two stay independent.
        con.execute(f"""
            CREATE OR REPLACE TABLE gmap AS
            WITH g AS (SELECT CAST(record_id AS VARCHAR) AS record_id,
                              CAST(group_id AS VARCHAR) AS group_id,
                              CAST(status AS VARCHAR) AS status FROM grp),
                 mg AS (SELECT record_id, group_id FROM g WHERE status = {merged}),
                 gu AS (SELECT group_id, min(record_id) AS unit_id
                        FROM mg GROUP BY group_id)
            SELECT mg.record_id, min(gu.unit_id) AS unit_id
            FROM mg JOIN gu ON gu.group_id = mg.group_id
            GROUP BY mg.record_id
        """)
        # A record may sit in several held groups; the smallest id is the
        # stable one to show.
        con.execute(f"""
            CREATE OR REPLACE TABLE held AS
            SELECT CAST(record_id AS VARCHAR) AS record_id,
                   min(CAST(group_id AS VARCHAR)) AS held_group_id
            FROM grp WHERE CAST(status AS VARCHAR) IS DISTINCT FROM {merged}
            GROUP BY 1
        """)
        # Every record with its unit, which is unit_members and nothing else.
        con.execute("""
            CREATE OR REPLACE VIEW mem AS
            SELECT i.record_id, COALESCE(m.unit_id, i.record_id) AS unit_id
            FROM ids i LEFT JOIN gmap m ON m.record_id = i.record_id
        """)

        # -- the pooled records, and only those -------------------------------
        con.execute("""
            CREATE OR REPLACE TABLE grouped AS
            SELECT i.*, m.unit_id FROM ids i JOIN gmap m ON m.record_id = i.record_id
        """)
        con.execute("""
            CREATE OR REPLACE TABLE pooled_sizes AS
            SELECT unit_id, CAST(count(*) AS BIGINT) AS unit_size
            FROM grouped GROUP BY unit_id HAVING count(*) > 1
        """)
        con.execute("""
            CREATE OR REPLACE TABLE pooled_members AS
            SELECT g.*, h.held_group_id FROM grouped g
            JOIN pooled_sizes s ON s.unit_id = g.unit_id
            LEFT JOIN held h ON h.record_id = g.record_id
        """)
        pooled_units = con.execute(
            "SELECT count(*) FROM pooled_sizes").fetchone()[0]

        select = ", ".join(
            f"CAST(r.{_quote(c)} AS VARCHAR) AS {_quote(c)}" if c == "record_id"
            else f"r.{_quote(c)}"
            for c in record_columns
        )
        if pooled_units:
            con.execute(f"""
                CREATE OR REPLACE TABLE pooled AS
                SELECT {select}, p.unit_id, p.held_group_id
                FROM rec r JOIN pooled_members p
                  ON p.record_id = CAST(r.record_id AS VARCHAR)
            """)
            con.execute("""
                CREATE OR REPLACE TABLE heldu AS
                SELECT unit_id, min(held_group_id) AS held_group_id
                FROM pooled_members WHERE held_group_id IS NOT NULL
                GROUP BY unit_id
            """)
            # -- the members' entity ids (LINKAGE.md, "What gets compared") ---
            if has_label:
                con.execute(f"""
                    CREATE OR REPLACE TABLE labels AS
                    WITH lab AS (
                        SELECT DISTINCT unit_id,
                               {_trim(f'CAST({label} AS VARCHAR)')} AS label
                        FROM pooled
                        WHERE {label} IS NOT NULL
                          AND {_trim(f'CAST({label} AS VARCHAR)')} <> ''
                    )
                    SELECT unit_id,
                           string_agg(label, {_text(ID_SEPARATOR)} ORDER BY label)
                               AS existing_entity_ids,
                           CAST(count(*) AS BIGINT) AS n_existing_ids
                    FROM lab GROUP BY unit_id
                """)
            else:
                con.execute("""
                    CREATE OR REPLACE TABLE labels AS
                    SELECT CAST(NULL AS VARCHAR) AS unit_id,
                           CAST(NULL AS VARCHAR) AS existing_entity_ids,
                           CAST(0 AS BIGINT) AS n_existing_ids
                    WHERE false
                """)
            # -- the per-column modal vote ------------------------------------
            _vote_table(con, represented, vote_join_batch())

        # -- the priority columns are summed, not voted on --------------------
        if priority and pooled_units:
            # Only a pooled unit needs this: a unit of one is its own total,
            # and the single-record branch reads its value straight off.
            #
            # Three things make the floating-point ones come out bit for bit
            # what pandas' ``groupby.sum`` gave. ``kahan_sum``, because pandas'
            # group sum is compensated and a plain running total disagreed on
            # 178 of the donations run's 22,375 totals. Records order, because
            # a compensated sum still depends on the order the values arrive
            # in and the membership join does not preserve it — that was one
            # total. And one thread, because several threads add their own
            # partial totals in their own order — that was sixteen.
            sums = ", ".join(
                (f"kahan_sum({_quote(c)})"
                 if _sum_type(dtypes.get(c, "object")) in ("DOUBLE", "FLOAT")
                 else f"sum({_quote(c)})") + f" AS {_quote(c)}"
                for c in priority
            )
            columns = ", ".join(_quote(c) for c in priority)
            con.execute(f"""
                CREATE OR REPLACE TABLE prio_rows AS
                SELECT unit_id, {columns} FROM pooled_members
                {'ORDER BY record_seq' if float_priority else ''}
            """)
            threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
            if float_priority:
                con.execute("SET threads=1")
            try:
                con.execute(f"""
                    CREATE OR REPLACE TABLE prio AS
                    SELECT unit_id, {sums} FROM prio_rows GROUP BY unit_id
                """)
            finally:
                if float_priority:
                    con.execute(f"SET threads={int(threads)}")

        # -- the profile's own per-unit columns -------------------------------
        agg_dtypes, agg_empty, agg_columns = _profile_aggregates(
            con, has_events, pooled_units, batch_rows)
        _check_aggregate_columns(agg_columns, represented, priority)

        # -- the output schema, in the order the pandas build wrote it --------
        head = ["unit_id", "unit_size", "held_group_id", LABEL_COLUMN,
                "existing_entity_ids", "n_existing_ids"]
        rest = list(represented) + list(priority)
        rest += [c for c in agg_columns if c not in rest and c not in head]
        out: dict[str, str] = {
            "unit_id": _TEXT_DTYPE,
            "unit_size": "int64",
            "held_group_id": "object",
            LABEL_COLUMN: "object",
            "existing_entity_ids": "object",
            "n_existing_ids": "int64",
        }
        for column in represented:
            out[column] = dtypes.get(column, "object")
        for column in priority:
            out[column] = _numeric_dtype(dtypes.get(column, "object"))
        for column in agg_columns:
            out[column] = agg_dtypes[column]

        # A column with no value anywhere was written by pandas as an Arrow
        # ``null``, which reads back as object where a text column reads back
        # as text, so it has to be spotted before the file is written.
        empty_out = set()
        if not con.execute(
                "SELECT EXISTS(SELECT 1 FROM ids i JOIN held h "
                "ON h.record_id = i.record_id)").fetchone()[0]:
            empty_out.add("held_group_id")
        labelled = has_label and con.execute(
            f"SELECT EXISTS(SELECT 1 FROM rec r WHERE {trimmed} <> '')"
        ).fetchone()[0]
        if not labelled:
            empty_out.add("existing_entity_ids")
            empty_out.add(LABEL_COLUMN)
        elif not _any_single_label(con, trimmed, pooled_units):
            empty_out.add(LABEL_COLUMN)
        for column in represented:
            if column in empty_records:
                empty_out.add(column)
        for column in agg_columns:
            if column in agg_empty and column not in represented \
                    and column not in priority:
                empty_out.add(column)

        def cast(expression: str, column: str, fallback: str = "VARCHAR") -> str:
            return (f"CAST({expression} AS {_duck_type(out[column], fallback)})"
                    f" AS {_quote(column)}")

        def branch(pooled: bool) -> str:
            if pooled:
                ids_here = ["s.unit_id", "s.unit_size",
                            cast("h.held_group_id", "held_group_id"),
                            cast("CASE WHEN l.n_existing_ids = 1 "
                                 "THEN l.existing_entity_ids END", LABEL_COLUMN),
                            cast("l.existing_entity_ids", "existing_entity_ids"),
                            cast("COALESCE(l.n_existing_ids, 0)", "n_existing_ids")]
            else:
                one = (f"CASE WHEN {trimmed} <> '' THEN {trimmed} END"
                       if has_label else "CAST(NULL AS VARCHAR)")
                count = (f"CASE WHEN {trimmed} <> '' THEN 1 ELSE 0 END"
                         if has_label else "0")
                ids_here = ["CAST(r.record_id AS VARCHAR) AS unit_id",
                            "CAST(1 AS BIGINT) AS unit_size",
                            cast("h.held_group_id", "held_group_id"),
                            cast(one, LABEL_COLUMN),
                            cast(one, "existing_entity_ids"),
                            cast(count, "n_existing_ids")]
            rows = list(ids_here)
            for column in rest:
                quoted = _quote(column)
                if column in represented:
                    base = f"v.{quoted}" if pooled else f"r.{quoted}"
                elif column in priority:
                    base = f"p.{quoted}" if pooled else f"TRY_CAST(r.{quoted} AS " \
                        f"{_sum_type(dtypes.get(column, 'object'))})"
                else:
                    base = None
                if column in agg_columns:
                    if not pooled:
                        expression = base if base is not None else "NULL"
                    elif base is None:
                        expression = f"a.{quoted}"
                    else:
                        expression = f"COALESCE(a.{quoted}, {base})"
                else:
                    expression = base if base is not None else "NULL"
                rows.append(cast(expression, column,
                                 record_types.get(column, "VARCHAR")))
            body = ",\n       ".join(rows)
            if pooled:
                joins = ["FROM pooled_sizes s",
                         "LEFT JOIN heldu h ON h.unit_id = s.unit_id",
                         "LEFT JOIN labels l ON l.unit_id = s.unit_id",
                         "LEFT JOIN voted v ON v.unit_id = s.unit_id"]
                if priority:
                    joins.append("LEFT JOIN prio p ON p.unit_id = s.unit_id")
                if agg_columns:
                    joins.append("LEFT JOIN agg a ON a.unit_id = s.unit_id")
            else:
                joins = ["FROM rec r",
                         "LEFT JOIN held h ON h.record_id = CAST(r.record_id AS VARCHAR)",
                         "LEFT JOIN pooled_members m"
                         " ON m.record_id = CAST(r.record_id AS VARCHAR)",
                         "WHERE m.record_id IS NULL"]
            return "SELECT " + body + "\n" + "\n".join(joins)

        branches = [branch(False)]
        if pooled_units:
            branches.append(branch(True))
        union = "\nUNION ALL\n".join(f"({b})" for b in branches)
        _write(con, f"SELECT * FROM ({union}) ORDER BY unit_id",
               units_path, out, empty_out)
        _write(con, "SELECT unit_id, record_id FROM mem ORDER BY unit_id, record_id",
               members_path,
               {"unit_id": _TEXT_DTYPE, "record_id": _TEXT_DTYPE}, set())

        records_total = int(con.execute("SELECT count(*) FROM ids").fetchone()[0])
        pooled_rows = int(
            con.execute("SELECT count(*) FROM pooled_members").fetchone()[0])
        return {
            "units": records_total - pooled_rows + int(pooled_units),
            "unit_members": records_total,
        }


def build_units(
    records: pd.DataFrame, groups: pd.DataFrame, events: pd.DataFrame | None = None,
    temp_dir=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(units, unit_members)`` for *records* grouped by *groups*.

    ``units`` is one representative row per unit: every record column plus
    ``unit_id``, ``unit_size``, ``held_group_id``, the summed priority columns,
    ``existing_entity_id`` (the members' single id, null when they carry none or
    disagree), ``existing_entity_ids`` and ``n_existing_ids``.

    A modal vote is right for a name and wrong for a median, so a profile may
    then correct its own columns through ``Profile.aggregate_unit_columns``.
    *events* is the run's evidence rows, which that hook reads when there are any.

    This is the in-memory face of ``build_units_files``: it writes the same two
    files to a scratch directory and reads them back. A caller at PSC scale
    wants ``build_units_files`` and the parquet, not these frames.
    """
    parent = Path(temp_dir) if temp_dir is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as scratch:
        units_path = Path(scratch) / UNITS_FILENAME
        members_path = Path(scratch) / UNIT_MEMBERS_FILENAME
        build_units_files(records, groups, units_path, members_path, events,
                          temp_dir=temp_dir)
        units = _restore(units_path)
        members = _restore(members_path)
    return units, members


def _restore(path) -> pd.DataFrame:
    """A written file read back on the dtypes the pandas build handed out.

    pandas 3 reads every Arrow string as its ``str`` dtype, whose missing value
    is ``NaN``. The old build handed back plain object columns holding ``None``,
    and the API strips ``None`` where it cannot strip a float. The pandas block
    in the file says which column was which, so the read-back is put back.
    """
    frame = pd.read_parquet(path)
    metadata = (pq.ParquetFile(os.fspath(path)).schema_arrow.metadata or {})
    block = metadata.get(b"pandas")
    if not block:
        return frame
    wanted = {column["name"]: column["numpy_type"]
              for column in json.loads(block)["columns"]}
    for column in frame.columns:
        target = wanted.get(column)
        if target is None or str(frame[column].dtype) == target:
            continue
        if target == "object":
            frame[column] = pd.Series(
                frame[column].to_numpy(dtype=object, na_value=None),
                index=frame.index, dtype=object,
            )
        else:
            try:
                frame[column] = frame[column].astype(target)
            except (TypeError, ValueError):
                pass
    return frame


def counts_from(units: pd.DataFrame) -> dict:
    """The run counts the unit build contributes, in the pipeline's snake_case."""
    by_track = units["track"].value_counts() if "track" in units.columns else pd.Series(dtype=int)
    counts = {"units_total": int(len(units))}
    for track, n in by_track.items():
        counts[f"units_{track}"] = int(n)
    return counts


def counts_from_file(units_path, temp_dir=None) -> dict:
    """``counts_from`` without reading the units frame.

    The same answer, counted in DuckDB off the parquet. At the full PSC
    snapshot the ``track`` column alone is about a gigabyte of pandas, read
    once for two integers.
    """
    path = os.fspath(units_path)
    has_track = "track" in set(pq.ParquetFile(path).schema_arrow.names)
    con = duckdb_conn.connect(temp_dir)
    try:
        total = con.execute(
            f"SELECT count(*) FROM read_parquet({_text(path)})").fetchone()[0]
        counts = {"units_total": int(total)}
        if has_track:
            rows = con.execute(
                f"SELECT track, count(*) AS n FROM read_parquet({_text(path)}) "
                "WHERE track IS NOT NULL GROUP BY track ORDER BY n DESC, track"
            ).fetchall()
            for track, n in rows:
                counts[f"units_{track}"] = int(n)
    finally:
        con.close()
    return counts


def _fingerprint_sql(units_path) -> str:
    path = os.fspath(units_path)
    names = set(pq.ParquetFile(path).schema_arrow.names)
    size = '"unit_size"' if "unit_size" in names else "CAST(1 AS BIGINT)"
    return (f'SELECT CAST("unit_id" AS VARCHAR) AS unit_id, '
            f"CAST({size} AS BIGINT) AS unit_size "
            f"FROM read_parquet({_text(path)}) ORDER BY unit_id")


def fingerprint_from_file(units_path, temp_dir=None) -> pd.DataFrame:
    """``unit_id`` and ``unit_size`` off the parquet, as two narrow columns."""
    con = duckdb_conn.connect(temp_dir)
    try:
        frame = con.execute(_fingerprint_sql(units_path)).df()
    finally:
        con.close()
    frame["unit_id"] = frame["unit_id"].astype(str)
    return frame


def write_fingerprint(units_path, out_path, temp_dir=None) -> None:
    """``fingerprint_from_file`` straight to parquet, never through pandas.

    ``scored_units.parquet`` is two columns of every unit. At the full PSC
    snapshot the frame is about 1.5 GB and the copy pandas makes on the way to
    the file is another, for a file the run reads once and only on a re-run.
    """
    con = duckdb_conn.connect(temp_dir)
    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({_fingerprint_sql(units_path)}) "
                    f"TO {_text(os.fspath(out_path))} (FORMAT parquet)")
    finally:
        con.close()


def record_count(records) -> int:
    """How many rows a records source holds, from a parquet footer if it is one."""
    if isinstance(records, pd.DataFrame):
        return len(records)
    return int(pq.ParquetFile(os.fspath(records)).metadata.num_rows)


def track_units(units: pd.DataFrame, track: str) -> pd.DataFrame:
    """The units of one track, ready for Splink (``unit_id`` is the unique id)."""
    if "track" not in units.columns:
        return units
    return units[units["track"] == track].reset_index(drop=True)

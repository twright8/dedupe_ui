# backend/app/pipeline/dedupe/stage_1_clean.py
"""Stage 1: assign a track to every record and run that track's cleaning rules.

Reads the raw records stage 0 wrote and the ruleset the run snapshotted, and
writes ``records.parquet``: the raw columns, ``track``, and every cleaning
target. A column only one track produces is null for the other — one frame, so
the records API and every later stage read one file.

The work is done in row batches (``CLEAN_BATCH_ROWS``, default 500,000) and
streamed into the parquet with an incremental writer, so peak memory follows the
batch and not the file. That is sound because every op the engine has is
row-independent: a track rule, a cleaning step and a derived rule all read one
record's own columns, and the distinct-value mapping inside a step is an
optimisation over whatever rows it is given. Batching therefore changes nothing
about what the rules mean — only how much of the file is in memory at once.

One file rather than a partitioned directory: every reader already opens
``records.parquet`` by name, and a single incremental writer keeps all of them
working untouched.

All the thinking lives in ``app.rules.engine``. This stage is the plumbing.
"""

import json
import os
import time
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.pipeline.dedupe.stage_0_load import RECORDS_RAW_FILENAME
from app.profiles.base import TRACK_COLUMN, TRACK_KEYS, validate_records
from app.rules import engine

RECORDS_FILENAME = "records.parquet"
RULESET_FILENAME = "ruleset.json"

STAGE = 1
STAGE_NAME = "clean"

# How many raw rows one batch carries. Big enough that the per-batch overhead is
# noise, small enough that a 16-million-record snapshot never has more than this
# many rows of cleaned columns in memory.
DEFAULT_BATCH_ROWS = 500_000
BATCH_ROWS_ENV = "CLEAN_BATCH_ROWS"

PARQUET_ROW_GROUP = 100_000


def batch_rows() -> int:
    try:
        value = int(os.environ.get(BATCH_ROWS_ENV, DEFAULT_BATCH_ROWS))
    except ValueError:
        return DEFAULT_BATCH_ROWS
    return max(1, value)


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def load_ruleset(config_dir: str | Path) -> dict:
    """Read the ruleset a run snapshotted into its config directory."""
    path = Path(config_dir) / RULESET_FILENAME
    if not path.is_file():
        raise FileNotFoundError(
            f"The run has no {RULESET_FILENAME}. Save a config version and start a new run."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Cleaning one batch — unchanged semantics, called once per batch
# ---------------------------------------------------------------------------


def clean_records(records: pd.DataFrame, ruleset: dict) -> pd.DataFrame:
    """Assign tracks, clean, and derive. See ``clean_records_detailed``."""
    return clean_records_detailed(records, ruleset)[0]


def clean_records_detailed(records: pd.DataFrame, ruleset: dict):
    """``(frame, derived)`` — the cleaned records and each derived column's report.

    Returns the raw columns, ``track``, every cleaning target, and then every
    derived column with its ``<target>_rule``, in the original row order. The
    derived columns run last, so their rules may read a cleaning target.
    """
    raw_columns = list(records.columns)
    frame = records.copy()
    frame[TRACK_COLUMN] = engine.assign_tracks(frame, ruleset)

    # Both tracks run even when one is empty, so its target columns still exist
    # in the output frame (null for every row of the other track).
    cleaned_parts = [
        engine.apply_cleaning(frame[frame[TRACK_COLUMN] == track], ruleset, track)
        for track in TRACK_KEYS
    ]
    populated = [part for part in cleaned_parts if len(part)]
    cleaned = pd.concat(populated) if populated else cleaned_parts[0]
    cleaned = cleaned.reindex(frame.index)

    # Raw columns first, then track, then the targets in the order the rules
    # create them — the order the Config screen shows.
    ordered = list(raw_columns) + [TRACK_COLUMN]
    for part in cleaned_parts:
        for column in part.columns:
            if column not in ordered:
                ordered.append(column)
    for column in ordered:
        if column not in cleaned.columns:
            cleaned[column] = None
        elif column not in raw_columns and column != TRACK_COLUMN:
            # Concatenating the two tracks leaves NaN where a column belongs to
            # the other track. Make every missing cleaning value one thing.
            values = cleaned[column].astype("object")
            cleaned[column] = values.where(values.notna(), None)

    # Derived columns run once over both tracks, after cleaning, so a rule may
    # read a cleaning target and a column may be scoped to one track.
    cleaned, derived = engine.derived_detailed(cleaned[ordered], ruleset)
    for report in derived:
        for column in engine.derived_targets({"target": report["target"]}):
            if column not in ordered:
                ordered.append(column)
    return cleaned[ordered], derived


# ---------------------------------------------------------------------------
# The schema every batch is written against
# ---------------------------------------------------------------------------


def written_columns(raw_columns: list[str], ruleset: dict) -> list[str]:
    """Every column stage 1 writes, in order, from the ruleset alone.

    Worked out before any row is read, so the parquet schema is fixed for the
    whole file. A batch in which one track happens to be empty, or in which a
    cleaning target comes out all-null, still writes the same columns.
    """
    ordered = list(raw_columns) + [TRACK_COLUMN]
    for track in TRACK_KEYS:
        for step in engine.cleaning_steps(ruleset, track):
            for column in engine.step_targets(step):
                if column not in ordered:
                    ordered.append(column)
    for column in engine.derived_columns(ruleset):
        for name in engine.derived_targets(column):
            if name not in ordered:
                ordered.append(name)
    return ordered


def _output_schema(raw_schema: pa.Schema, columns: list[str]) -> pa.Schema:
    """The Arrow schema for the cleaned file.

    Raw columns keep the type stage 0 gave them. Everything a rule writes is
    stored as a string: a cleaning target holds text by construction, and
    pinning the type here is what stops an all-null batch from being written as
    a null column and breaking the file's schema.
    """
    fields = []
    raw_names = set(raw_schema.names)
    for column in columns:
        if column in raw_names:
            fields.append(raw_schema.field(column))
        else:
            fields.append(pa.field(column, pa.string()))
    return pa.schema(fields)


def _as_table(frame: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    """One cleaned batch as an Arrow table on the fixed schema."""
    prepared = {}
    for field in schema:
        if field.name in frame.columns:
            column = frame[field.name]
        else:
            column = pd.Series([None] * len(frame), index=frame.index, dtype="object")
        if pa.types.is_string(field.type):
            column = column.astype("object").where(column.notna(), None)
            column = column.map(lambda v: None if v is None else str(v))
        prepared[field.name] = column
    return pa.Table.from_pandas(
        pd.DataFrame(prepared, columns=schema.names), schema=schema,
        preserve_index=False,
    )


# ---------------------------------------------------------------------------
# Accumulating what the batches each saw
# ---------------------------------------------------------------------------


class _Tally:
    """Per-track counts and per-derived-column hit counts, summed over batches.

    The numbers have to come out the same as a single-pass run would give, so
    everything here is a sum over disjoint row sets — never an average and never
    a distinct count, which could not be accumulated this way.
    """

    def __init__(self):
        self.total = 0
        self.by_track = {track: 0 for track in TRACK_KEYS}
        self.derived: dict[str, dict] = {}

    def add(self, cleaned: pd.DataFrame, derived: list[dict]) -> None:
        self.total += len(cleaned)
        counts = cleaned[TRACK_COLUMN].value_counts()
        for track in TRACK_KEYS:
            self.by_track[track] += int(counts.get(track, 0))
        for report in derived:
            entry = self.derived.setdefault(
                report["target"],
                {"target": report["target"], "changed": 0, "total": 0,
                 "rules": {r["id"]: 0 for r in report["rules"]}},
            )
            entry["changed"] += int(report["changed"])
            entry["total"] += int(report["total"])
            for rule in report["rules"]:
                entry["rules"][rule["id"]] = entry["rules"].get(rule["id"], 0) + rule["hits"]

    def stats(self) -> dict:
        return {
            "records_total": self.total,
            "records_person": self.by_track["person"],
            "records_organisation": self.by_track["organisation"],
        }


def _check_ids(path: Path) -> None:
    """record_id must still be unique across the whole file.

    Done in DuckDB rather than by holding 16 million ids in a Python set.
    """
    con = duckdb.connect()
    try:
        row = con.execute(
            f"SELECT record_id, count(*) c FROM '{path}' "
            f"GROUP BY 1 HAVING c > 1 LIMIT 3"
        ).fetchall()
    finally:
        con.close()
    if row:
        repeated = [r[0] for r in row]
        raise ValueError(f"record_id must be unique — repeated: {repeated}")


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def run_stage_1_clean(
    run_dir: str,
    config_dir: str,
    progress_callback=None,
) -> dict:
    """Clean ``<run_dir>/records_raw.parquet`` into ``<run_dir>/records.parquet``.

    Parameters
    ----------
    run_dir : str
        The run's directory, holding the raw records from stage 0.
    config_dir : str
        The run's config snapshot, holding ``ruleset.json``.
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    """
    t_start = time.time()
    run_dir = Path(run_dir)

    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    ruleset = load_ruleset(config_dir)
    source = pq.ParquetFile(run_dir / RECORDS_RAW_FILENAME)
    raw_schema = source.schema_arrow
    columns = written_columns(list(raw_schema.names), ruleset)
    schema = _output_schema(raw_schema, columns)

    rows = source.metadata.num_rows
    size = batch_rows()
    _step(
        f"Assigning tracks and cleaning {rows:,} records "
        f"in batches of {size:,}...",
        progress_callback,
    )

    out_path = run_dir / RECORDS_FILENAME
    tally = _Tally()
    writer = None
    try:
        for n, batch in enumerate(source.iter_batches(batch_size=size), start=1):
            frame = batch.to_pandas()
            cleaned, derived = clean_records_detailed(frame, ruleset)
            # The shared-column contract is checked per batch; uniqueness needs
            # the whole file, so it is checked once at the end.
            validate_records(cleaned)
            tally.add(cleaned, derived)

            if writer is None:
                writer = pq.ParquetWriter(out_path, schema, compression="zstd")
            writer.write_table(_as_table(cleaned, schema),
                               row_group_size=PARQUET_ROW_GROUP)
            if rows > size:
                _step(f"  batch {n}: {tally.total:,} of {rows:,} records",
                      progress_callback)
        if writer is None:
            # An empty input still has to leave a readable, correctly shaped file.
            writer = pq.ParquetWriter(out_path, schema, compression="zstd")
            writer.write_table(schema.empty_table())
    finally:
        if writer is not None:
            writer.close()

    _check_ids(out_path)

    for entry in tally.derived.values():
        _step(
            f"Derived {entry['target']}: {entry['changed']:,} of "
            f"{entry['total']:,} records changed.",
            progress_callback,
        )

    stats = tally.stats()
    elapsed = time.time() - t_start
    _step(
        f"Stage 1 complete in {elapsed:.1f}s — "
        f"{stats['records_person']:,} people, "
        f"{stats['records_organisation']:,} organisations.",
        progress_callback,
    )

    if progress_callback:
        progress_callback("stage_end", {
            "stage": STAGE,
            "name": STAGE_NAME,
            "elapsed_seconds": round(elapsed, 1),
            **stats,
        })

    return stats

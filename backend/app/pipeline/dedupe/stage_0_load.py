# backend/app/pipeline/dedupe/stage_0_load.py
"""Stage 0: load the input file into the run's raw records frame.

The profile owns every dataset-specific decision (which columns, how to
aggregate). This stage only calls it, checks the shared columns are there, and
writes the result where stage 1 will look for it. The track is not decided here
— the ruleset decides it, and stage 1 adds the column.
"""

import time
from pathlib import Path

import shutil

from app.profiles import get_profile
from app.profiles.base import LoadOptions, validate_records, validate_records_file

RECORDS_RAW_FILENAME = "records_raw.parquet"
EVENTS_FILENAME = "events.parquet"

STAGE = 0
STAGE_NAME = "load"


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def _place(records, out_path: Path, check_frame=None, check_file=None) -> int:
    """Put a profile's records at *out_path*, whichever form they arrive in.

    Returns the row count. A frame is written; a path is moved when it is the
    profile's own temporary file and copied when it is not, so a profile that
    hands back something it still owns is never left without it.
    """
    if isinstance(records, (str, Path)):
        source = Path(records)
        if source.resolve() != out_path.resolve():
            try:
                shutil.move(str(source), out_path)
            except OSError:
                shutil.copyfile(source, out_path)
        if check_file is not None:
            check_file(out_path)
        from app import duckdb_conn

        con = duckdb_conn.connect(Path(out_path).parent / "duckdb_tmp")
        try:
            return int(con.execute(
                f"SELECT count(*) FROM read_parquet('{out_path}')"
            ).fetchone()[0])
        finally:
            con.close()

    if check_frame is not None:
        check_frame(records)
    if not len(records):
        return 0
    records.to_parquet(out_path, index=False)
    return int(len(records))


def run_stage_0_load(
    run_dir: str,
    input_path: str,
    progress_callback=None,
    options: LoadOptions | None = None,
) -> dict:
    """Load *input_path* into ``<run_dir>/records_raw.parquet`` and return load stats.

    Parameters
    ----------
    run_dir : str
        Directory for writing output files.
    input_path : str
        The single uploaded input file.
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    options : LoadOptions, optional
        What the run asked for — today only the quick-mode sample.
    """
    t_start = time.time()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    profile = get_profile()

    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    _step(f"Loading {Path(input_path).name} with the {profile.key} profile...",
          progress_callback)
    options = options or LoadOptions()
    if options.quick_mode:
        _step("  quick mode: reading a sample, not the whole file", progress_callback)
    records, stats = profile.load_records(Path(input_path), options)

    # A profile may hand back a frame or a parquet the loader already wrote. The
    # second form is what lets a 13 GB snapshot through: the rows went
    # decompressor -> DuckDB -> parquet and are never assembled in memory here.
    out_path = run_dir / RECORDS_RAW_FILENAME
    written = _place(records, out_path, validate_records, validate_records_file)

    # The evidence rows behind the records (D13b), when the profile has any.
    # A profile that reads a slow file serves both frames from one parse.
    events = profile.load_events(Path(input_path), options)
    if events is not None:
        rows = _place(events, run_dir / EVENTS_FILENAME)
        if rows:
            stats["event_rows"] = rows
            _step(f"  {rows:,} evidence rows", progress_callback)

    elapsed = time.time() - t_start
    _step(
        f"Stage 0 complete in {elapsed:.1f}s — "
        f"{stats.get('records_total', written):,} records from "
        f"{stats.get('input_rows', 0):,} input rows.",
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

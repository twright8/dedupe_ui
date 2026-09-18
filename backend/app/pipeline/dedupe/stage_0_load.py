# backend/app/pipeline/dedupe/stage_0_load.py
"""Stage 0: load the input file into the run's raw records frame.

The profile owns every dataset-specific decision (which columns, how to
aggregate). This stage only calls it, checks the shared columns are there, and
writes the result where stage 1 will look for it. The track is not decided here
— the ruleset decides it, and stage 1 adds the column.
"""

import time
from pathlib import Path

from app.profiles import get_profile
from app.profiles.base import validate_records

RECORDS_RAW_FILENAME = "records_raw.parquet"

STAGE = 0
STAGE_NAME = "load"


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def run_stage_0_load(
    run_dir: str,
    input_path: str,
    progress_callback=None,
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
    """
    t_start = time.time()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    profile = get_profile()

    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    _step(f"Loading {Path(input_path).name} with the {profile.key} profile...",
          progress_callback)
    records, stats = profile.load_records(Path(input_path))
    validate_records(records)

    out_path = run_dir / RECORDS_RAW_FILENAME
    records.to_parquet(out_path, index=False)

    elapsed = time.time() - t_start
    _step(
        f"Stage 0 complete in {elapsed:.1f}s — "
        f"{stats.get('records_total', len(records)):,} records from "
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

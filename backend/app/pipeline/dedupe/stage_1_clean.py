# backend/app/pipeline/dedupe/stage_1_clean.py
"""Stage 1: assign a track to every record and run that track's cleaning rules.

Reads the raw records stage 0 wrote and the ruleset the run snapshotted, and
writes ``records.parquet``: the raw columns, ``track``, and every cleaning
target. A column only one track produces is null for the other — one frame, so
the records API and every later stage read one file.

All the thinking lives in ``app.rules.engine``. This stage is the plumbing.
"""

import json
import time
from pathlib import Path

import pandas as pd

from app.pipeline.dedupe.stage_0_load import RECORDS_RAW_FILENAME
from app.profiles.base import TRACK_KEYS, validate_records
from app.rules import engine

RECORDS_FILENAME = "records.parquet"
RULESET_FILENAME = "ruleset.json"

STAGE = 1
STAGE_NAME = "clean"


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


def clean_records(records: pd.DataFrame, ruleset: dict) -> pd.DataFrame:
    """Assign tracks and apply each track's cleaning to *records*.

    Returns the raw columns plus ``track`` plus every cleaning target, in the
    original row order.
    """
    raw_columns = list(records.columns)
    frame = records.copy()
    frame["track"] = engine.assign_tracks(frame, ruleset)

    # Both tracks run even when one is empty, so its target columns still exist
    # in the output frame (null for every row of the other track).
    cleaned_parts = [
        engine.apply_cleaning(frame[frame["track"] == track], ruleset, track)
        for track in TRACK_KEYS
    ]
    populated = [part for part in cleaned_parts if len(part)]
    cleaned = pd.concat(populated) if populated else cleaned_parts[0]
    cleaned = cleaned.reindex(frame.index)

    # Raw columns first, then track, then the targets in the order the rules
    # create them — the order the Config screen shows.
    ordered = list(raw_columns) + ["track"]
    for part in cleaned_parts:
        for column in part.columns:
            if column not in ordered:
                ordered.append(column)
    for column in ordered:
        if column not in cleaned.columns:
            cleaned[column] = None
        elif column not in raw_columns and column != "track":
            # Concatenating the two tracks leaves NaN where a column belongs to
            # the other track. Make every missing cleaning value one thing.
            values = cleaned[column].astype("object")
            cleaned[column] = values.where(values.notna(), None)
    return cleaned[ordered]


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
    records = pd.read_parquet(run_dir / RECORDS_RAW_FILENAME)

    _step(f"Assigning tracks and cleaning {len(records):,} records...", progress_callback)
    cleaned = clean_records(records, ruleset)
    validate_records(cleaned)

    cleaned.to_parquet(run_dir / RECORDS_FILENAME, index=False)

    stats = {
        "records_total": int(len(cleaned)),
        "records_person": int((cleaned["track"] == "person").sum()),
        "records_organisation": int((cleaned["track"] == "organisation").sum()),
    }

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

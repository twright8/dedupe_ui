# backend/app/services/bucketing_history.py
"""Which lines put a pair in its bucket — kept once per change, not per pair.

``pairs.parquet`` stores ``bucket`` and ``score_bucket`` and not the lines that
set them, and a re-bucket rewrites ``runs.threshold_high`` in place. To say
which lines were in force when a pair landed in review you had to correlate
audit rows by timestamp (`docs/TERMINOLOGY_AUDIT.md`, gap 5).

The obvious fix — stamp the lines onto every pair row — is the wrong one at PSC
scale: 100 million rows would each carry three floats, a scorer name and a model
version that are the same for all of them. The lines change perhaps ten times in
a run's life. So this file records the change, not the row.

``bucketing_history.json`` in the run folder is a list, oldest first. Every entry
says when, who, which lines, which scorer and model version, and how many pairs
landed in each bucket. The entry in force is the last one, and the pairs list and
the pair detail return it beside the pairs, so a reader never has to guess.

Four things append an entry: the score stage's caller when a run is first
bucketed, and then re-bucket, apply-model and revert-model.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "bucketing_history.json"

#: What put the pairs where they are. One of these per entry.
ACTIONS = ("scored", "re-bucketed", "model applied", "model reverted")

#: Enough history to answer any question about a run without the file growing
#: without limit. A run whose lines move more than this many times has a
#: different problem.
MAX_ENTRIES = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_for(run_dir: str | Path) -> Path:
    return Path(run_dir) / HISTORY_FILENAME


def read(run_dir: str | Path) -> list[dict]:
    """The whole history, oldest first. Empty when the run has none."""
    try:
        loaded = json.loads(path_for(run_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return loaded if isinstance(loaded, list) else []


def current(run_dir: str | Path) -> dict | None:
    """The entry in force — the last one written, or None."""
    history = read(run_dir)
    return history[-1] if history else None


def append(
    run_dir: str | Path,
    action: str,
    accept_line: float | None = None,
    review_line: float | None = None,
    lowest_score_kept: float | None = None,
    scorer: str | None = None,
    model_version=None,
    counts: dict | None = None,
    who: str = "",
    note: str = "",
) -> dict:
    """Record one change to the lines, and return the entry that was written.

    *counts* is ``{bucket: n}`` after the change. A failure to write is logged
    and never raises: losing a line of history must not fail a re-bucket.
    """
    entry = {
        "at": _now(),
        "who": who or "",
        "action": action,
        "accept_line": accept_line,
        "review_line": review_line,
        "lowest_score_kept": lowest_score_kept,
        "scorer": scorer,
        "model_version": model_version,
        "counts": _bucket_counts(counts),
        "note": note or "",
    }
    history = read(run_dir)
    history.append(entry)
    if len(history) > MAX_ENTRIES:
        history = history[-MAX_ENTRIES:]
    try:
        path = path_for(run_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")
    except OSError:
        logger.exception("Could not write %s", path_for(run_dir))
    return entry


def _bucket_counts(counts: dict | None) -> dict:
    """Just the three bucket counts, under the names the API already uses."""
    if not counts:
        return {}
    from app import vocabulary

    wanted = {}
    for bucket in vocabulary.BUCKETS:
        for key in (bucket, f"pairs_{bucket}", f"{bucket}_pairs"):
            if key in counts:
                wanted[bucket] = counts[key]
                break
    return wanted

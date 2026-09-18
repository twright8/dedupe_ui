# backend/app/services/run_counts.py
"""The one place a run's counts are written.

A run's `counts_json` is built by six stages, each owning a handful of keys.
Anything that reruns **part** of the pipeline — a re-bucket, a recluster, a label
write, applying or reverting a model — recomputes only its own keys. Writing
that partial dict straight back wipes every other stage's numbers, and because
the run screen decides which tabs exist from the `has*` flags derived from those
numbers, a run loses its Records, Exact groups and Entities tabs and looks
half-empty.

So nothing writes `counts_json` directly. Everything goes through `merge`, which
reads what is stored, lays the new keys on top, and writes the whole thing back.
A stage that needs to *clear* one of its own keys sets it explicitly rather than
leaving it out — leaving it out means "I did not measure this", not "this is now
zero".
"""

from __future__ import annotations

import json
import logging

from app.db import query_db, write_db

logger = logging.getLogger(__name__)


def stored(db_path: str, run_id: str) -> dict:
    """The run's counts as they stand, or an empty dict."""
    rows = query_db(db_path, "SELECT counts_json FROM runs WHERE id = ?", (run_id,))
    if not rows or not rows[0]["counts_json"]:
        return {}
    try:
        counts = json.loads(rows[0]["counts_json"])
    except (TypeError, ValueError):
        logger.warning("Run %s has unreadable counts_json; treating it as empty",
                       run_id)
        return {}
    return counts if isinstance(counts, dict) else {}


def merge(db_path: str, run_id: str, updates: dict | None,
          extra_sql: str = "", extra_params: tuple = ()) -> dict:
    """Lay *updates* over the run's stored counts, save, and return the whole lot.

    *extra_sql* and *extra_params* let a caller set other columns of the same row
    in the same statement — the thresholds a re-bucket moved, for instance — so
    the two can never drift apart.
    """
    counts = {**stored(db_path, run_id), **(updates or {})}
    write_db(
        db_path,
        f"UPDATE runs SET {extra_sql}counts_json = ? WHERE id = ?",
        (*extra_params, json.dumps(counts), run_id),
    )
    return counts

# backend/app/pipeline/dedupe/stage_2_exact.py
"""Stage 2: run the ruleset's match keys over the cleaned records.

Reads ``records.parquet`` and the ruleset the run snapshotted, and writes:

  ``exact_groups.parquet``  one row per record per group it belongs to
  ``exact_eval.json``       the per-key stats, the overall totals, and what the
                            groups do to the labels a previous review made

All the thinking lives in ``app.rules.keys`` and ``app.rules.keys_eval``, which
the Config screen's key preview also calls. This stage is the plumbing.
"""

import json
import time
from pathlib import Path

import pandas as pd

from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME, load_ruleset
from app.rules import keys, keys_eval

EXACT_GROUPS_FILENAME = "exact_groups.parquet"
EXACT_EVAL_FILENAME = "exact_eval.json"

STAGE = 2
STAGE_NAME = "exact"


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def build_report(records: pd.DataFrame, ruleset: dict) -> tuple[pd.DataFrame, dict]:
    """``(groups, report)`` for *records* under *ruleset*.

    The report is exactly what ``exact_eval.json`` holds and what the key
    preview returns, so the Config screen and a finished run describe a ruleset
    in the same words.
    """
    groups, stats = keys.apply_match_keys(records, ruleset)
    evaluation = keys_eval.evaluate(records, groups)
    return groups, {
        "keys": stats["keys"],
        "overall": stats["overall"],
        "eval": evaluation,
    }


def counts_from(report: dict) -> dict:
    """The run counts stage 2 contributes, in the pipeline's snake_case."""
    overall = report["overall"]
    evaluation = report["eval"]
    return {
        "exact_merged_groups": overall["merged_groups"],
        "exact_merged_records": overall["merged_records"],
        "exact_held_groups": overall["held_groups"],
        "exact_held_records": overall["held_records"],
        "exact_entities_after": overall["entities_after"],
        "exact_conflicts": evaluation["conflicts"],
        "exact_pair_precision": evaluation["pair_precision"],
        "exact_pair_recall": evaluation["pair_recall"],
    }


def run_stage_2_exact(
    run_dir: str,
    config_dir: str,
    progress_callback=None,
) -> dict:
    """Group ``<run_dir>/records.parquet`` under the run's match keys.

    Parameters
    ----------
    run_dir : str
        The run's directory, holding the cleaned records from stage 1.
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
    records = pd.read_parquet(run_dir / RECORDS_FILENAME)

    _step(
        f"Applying {len(keys.engine.match_keys(ruleset))} match key(s) to "
        f"{len(records):,} records...",
        progress_callback,
    )
    groups, report = build_report(records, ruleset)

    groups.to_parquet(run_dir / EXACT_GROUPS_FILENAME, index=False)
    (run_dir / EXACT_EVAL_FILENAME).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    counts = counts_from(report)
    elapsed = time.time() - t_start
    precision = report["eval"]["pair_precision"]
    _step(
        f"Stage 2 complete in {elapsed:.1f}s — "
        f"{counts['exact_merged_groups']:,} merged groups over "
        f"{counts['exact_merged_records']:,} records, "
        f"{counts['exact_held_groups']:,} held for review, "
        f"{counts['exact_entities_after']:,} entities left"
        + (f", pair precision {precision:.3f}." if precision is not None else "."),
        progress_callback,
    )

    if progress_callback:
        progress_callback("stage_end", {
            "stage": STAGE,
            "name": STAGE_NAME,
            "elapsed_seconds": round(elapsed, 1),
            **counts,
        })

    return counts

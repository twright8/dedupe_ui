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

from app.pipeline.dedupe import exact_overlay
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME, load_ruleset
from app.rules import engine, keys, keys_eval

EXACT_GROUPS_FILENAME = "exact_groups.parquet"
EXACT_EVAL_FILENAME = "exact_eval.json"

STAGE = 2
STAGE_NAME = "exact"


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


#: Columns the stage needs whatever the ruleset says: identity, the track the
#: keys are grouped by, and the imported id the evaluation reports against.
ALWAYS_NEEDED = ("record_id", "track", keys_eval.LABEL_COLUMN)


def required_columns(ruleset: dict) -> list[str]:
    """Every column stage 2 reads, and nothing else.

    A match key touches four kinds of column and no others: the key's own
    columns, its guards (``require_any_equal`` and ``max_distinct``), the
    columns its ``when`` conditions test, and the three in ``ALWAYS_NEEDED``.
    Everything else in a record — the whole address, the natures of control,
    every raw field — is carried to stage 3 untouched and never read here.

    Reading only these is what makes the stage affordable at 16 million records:
    the cleaned PSC frame is 58 columns wide and this asks for about a dozen.

    ``keys.referenced_columns`` already knows the first two kinds, and it is the
    same function the engine normalises from, so the two cannot drift apart.
    """
    wanted: set[str] = set(ALWAYS_NEEDED)
    for columns in keys.referenced_columns(ruleset).values():
        wanted.update(columns)
    # A key may be conditional on a column no key groups by.
    for key in engine.match_keys(ruleset):
        for condition in (key.get("when") or []):
            column = condition.get("column") if isinstance(condition, dict) else None
            if isinstance(column, str):
                wanted.add(column)
    return sorted(wanted)


def _columns_present(path, ruleset: dict) -> list[str]:
    """The projection, narrowed to the columns the file actually has.

    A ruleset may legally name a column the data does not carry — a key written
    for one profile's shape, or a cleaning step that produced nothing. The key
    engine has always treated that as "this key holds for nobody" rather than an
    error, and reading the whole frame hid the difference. Asking Parquet for a
    column that is not there is a hard failure, so the intersection has to
    happen here, not in the engine.
    """
    import pyarrow.parquet as pq

    wanted = required_columns(ruleset)
    try:
        available = set(pq.ParquetFile(path).schema.names)
    except Exception:  # unreadable schema: let the normal read raise instead
        return None
    return [column for column in wanted if column in available]


def build_report(
    records: pd.DataFrame,
    ruleset: dict,
    labels: pd.DataFrame | None = None,
    decisions: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """``(groups, report)`` for *records* under *ruleset*.

    The report is exactly what ``exact_eval.json`` holds and what the key
    preview returns, so the Config screen and a finished run describe a ruleset
    in the same words.

    *labels* and *decisions* let a reviewer's decisions change the grouping: a
    merged group holding a human FALSE pair is dissolved into the parts its TRUE
    labels connect, and a held group a reviewer merged becomes a merged one
    (`docs/ENTITIES.md`). Without them the keys have the last word, which is
    what the Config preview wants to show.
    """
    groups, stats = keys.apply_match_keys(records, ruleset)
    groups, human = exact_overlay.apply_human_overlay(groups, labels, decisions)
    overall = _restate(stats["overall"], groups, len(records)) \
        if human["split_groups"] or human["merged_held_groups"] else stats["overall"]
    evaluation = keys_eval.evaluate(records, groups)
    return groups, {
        "keys": stats["keys"],
        "overall": overall,
        "eval": evaluation,
        "human": human,
    }


def _restate(overall: dict, groups: pd.DataFrame, n_records: int) -> dict:
    """The overall totals after a human changed what the keys grouped.

    The per-key stats stay as the keys reported them — they describe the rules,
    not the outcome — but the totals have to describe what the run actually has.
    """
    merged = groups[groups["status"] == keys.MERGED] if len(groups) else groups
    held = groups[groups["status"] == keys.HELD] if len(groups) else groups
    merged_groups = int(merged["group_id"].nunique()) if len(merged) else 0
    merged_records = int(merged["record_id"].nunique()) if len(merged) else 0
    return {
        **overall,
        "merged_groups": merged_groups,
        "merged_records": merged_records,
        "held_groups": int(held["group_id"].nunique()) if len(held) else 0,
        "held_records": int(held["record_id"].nunique()) if len(held) else 0,
        "entities_after": n_records - (merged_records - merged_groups),
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
        "exact_split_by_human": len(report.get("human", {}).get("split_groups", [])),
        "exact_merged_by_human": len(
            report.get("human", {}).get("merged_held_groups", [])),
    }


def run_stage_2_exact(
    run_dir: str,
    config_dir: str,
    labels: pd.DataFrame | None = None,
    decisions: dict | None = None,
    progress_callback=None,
) -> dict:
    """Group ``<run_dir>/records.parquet`` under the run's match keys.

    Parameters
    ----------
    run_dir : str
        The run's directory, holding the cleaned records from stage 1.
    config_dir : str
        The run's config snapshot, holding ``ruleset.json``.
    labels, decisions : optional
        The active human labels and the newest decision per cluster or held
        group. They can dissolve a merged group and merge a held one.
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    """
    t_start = time.time()
    run_dir = Path(run_dir)

    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    ruleset = load_ruleset(config_dir)
    records = pd.read_parquet(run_dir / RECORDS_FILENAME,
                              columns=_columns_present(run_dir / RECORDS_FILENAME,
                                                       ruleset))

    _step(
        f"Applying {len(keys.engine.match_keys(ruleset))} match key(s) to "
        f"{len(records):,} records...",
        progress_callback,
    )
    groups, report = build_report(records, ruleset, labels, decisions)

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

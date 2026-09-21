# backend/app/services/model_apply.py
"""Apply a trained model to a finished run, and take it off again.

`MODEL.md`: "Applying a model to an existing run does not rerun Splink." Splink
is the expensive half — an hour on PSC — and the model only re-reads the pairs
it already produced. So this reads `pairs.parquet` back, adds `gbt_score`,
re-buckets where a graded model is in charge, and rewrites the pairs, the
evaluation and the run's counts. Nothing else in the run folder moves.

Reverting is the exact inverse: the `gbt_score` column and the run's model state
both go, and the buckets return to the Splink score. A run that was never
applied reverts to itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from app.pipeline.dedupe import label_overlay, score_eval, stage_3b_model
from app.pipeline.dedupe import units as units_module
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME
from app.pipeline.dedupe.stage_2_exact import EXACT_GROUPS_FILENAME
from app.model import corpus as corpus_lib
from app.pipeline.dedupe.stage_0_load import EVENTS_FILENAME
from app.pipeline.dedupe.stage_3_score import (
    PAIRS_FILENAME, _strip_overlays, _write_evaluation, apply_overlays,
    counts_from, events_by_unit, finalise_pairs, label_outcomes, run_ruleset,
    write_contradictions, write_pair_index,
)
from app.profiles import get_profile
from app.rules import linkage
from app.services import bucketing_history

logger = logging.getLogger(__name__)


class ModelApplyError(RuntimeError):
    """Applying the model is refused, in words fit to show a user."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _read(run_dir: Path) -> dict:
    pairs_path = run_dir / PAIRS_FILENAME
    if not pairs_path.is_file():
        raise ModelApplyError("Run has no scored pairs yet")
    events_path = run_dir / EVENTS_FILENAME
    return {
        "pairs": pd.read_parquet(pairs_path),
        "units": pd.read_parquet(run_dir / units_module.UNITS_FILENAME),
        "members": pd.read_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME),
        "records": pd.read_parquet(run_dir / RECORDS_FILENAME),
        "groups": pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME),
        "events": pd.read_parquet(events_path) if events_path.is_file() else None,
    }


def _thresholds(run_dir: Path) -> tuple[float, float, float]:
    """The run's own Splink lines, which the model never overwrites."""
    path = run_dir / "config" / "linkage_settings.json"
    if not path.is_file():
        return (linkage.DEFAULT_CANDIDATE, linkage.DEFAULT_REVIEW, linkage.DEFAULT_HIGH)
    return linkage.thresholds(json.loads(path.read_text(encoding="utf-8")))


def _buckets_changed(before: pd.DataFrame, after: pd.DataFrame) -> bool:
    """Whether any pair ended up in a different bucket, or decided by something else.

    A cold-start model scores every pair and moves none of them, and reclustering
    a run whose clusters cannot have changed is minutes of work for no answer.
    """
    if len(before) != len(after):
        return True
    for column in ("bucket", "decided_by"):
        if column not in before.columns or column not in after.columns:
            return True
        if not before[column].reset_index(drop=True).equals(
                after[column].reset_index(drop=True)):
            return True
    return False


def _recluster(run_dir: Path, db_path: str | None, labels) -> dict:
    """Rebuild the clusters and the proposed entities from the new buckets.

    Stages 4 and 5 only, the same two the recluster service runs. Stage 2's
    exact groups have not moved — a model changes which pairs are accepted, not
    which records an exact key joined — and Splink is not re-run, which is the
    whole point of applying a model to a finished run.
    """
    from app.pipeline.dedupe.stage_4_cluster import run_stage_4_cluster
    from app.pipeline.dedupe.stage_5_entities import run_stage_5_entities
    from app.services.pair_labels import decisions_by_scope

    counts = run_stage_4_cluster(
        run_dir=str(run_dir), config_dir=str(run_dir / "config"), labels=labels,
        decisions=decisions_by_scope(db_path) if db_path else None,
    )
    counts.update(run_stage_5_entities(run_dir=str(run_dir), db_path=db_path))
    return counts


def _finish(run_dir: Path, data: dict, pairs: pd.DataFrame, labels,
            lines: dict, review: float, high: float, candidate: float,
            db_path: str | None = None) -> tuple[dict, bool]:
    """Write the pairs, redo everything downstream, and hand back the counts.

    Returns ``(counts, reclustered)``. The counts cover stage 3, and stages 4
    and 5 as well when the buckets actually moved. They are the keys those
    stages own and no others, so the caller merges them into the run's stored
    counts rather than replacing them (`services/run_counts`).
    """
    changed = _buckets_changed(data["pairs"], pairs)
    pairs.to_parquet(run_dir / PAIRS_FILENAME, index=False)
    # The pairs file changed, so the listing index made from it has to change
    # with it (item 6). A stale index would be ignored, not served, but a
    # correct one is what keeps the pairs list under two seconds.
    write_pair_index(run_dir)
    outcome = label_outcomes(labels, data["members"], data["groups"])
    write_contradictions(run_dir, outcome["contradictions"])
    evaluation = score_eval.evaluate(
        data["records"], data["groups"], data["units"], data["members"], pairs,
        thresholds={"candidate": candidate, "review": review, "high": high},
        applied=outcome["applied"], model_lines=lines,
    )
    _write_evaluation(run_dir, evaluation)
    counts = counts_from(data["units"], pairs, evaluation, outcome, run_dir=run_dir)
    if changed:
        # A cluster is a connected component over the accepted pairs, so a bucket
        # that moved leaves the clusters and the entities behind them stale.
        counts.update(_recluster(run_dir, db_path, labels))
    return counts, changed


def apply_model(run_dir, labels: pd.DataFrame | None = None,
                force: bool = False, db_path: str | None = None,
                who: str = "") -> dict:
    """Score a finished run with each track's active model and re-bucket.

    When the buckets move, stages 4 and 5 run again so the clusters and the
    proposed entities match them; when they do not — a cold-start model scores
    every pair and decides none — that work is skipped and ``reclustered`` says
    so.

    Raises `ModelApplyError` when no track has an active model, or when the
    collapse guard fires and *force* is off — and in that second case the run has
    already been put back on the Splink score, so nothing is left half applied.
    """
    run_dir = Path(run_dir)
    data = _read(run_dir)
    models = stage_3b_model.active_models()
    if not models:
        raise ModelApplyError("No active model for any track")

    candidate, review, high = _thresholds(run_dir)
    review_before = int((data["pairs"]["bucket"] == "review").sum())

    # The corpus statistics the run fitted, read back from its folder — the
    # same vocabulary and IDF the scoring run used, so applying a model to a
    # finished run cannot quietly move a feature value (`app/model/corpus.py`).
    fitted = {track: corpus_lib.for_run(run_dir, data["units"], track, get_profile())
              for track in models}
    pairs, used = stage_3b_model.score_pairs(
        data["pairs"], data["units"], models,
        events=events_by_unit(data["events"], data["members"]),
        profile=get_profile(), corpus=fitted,
    )
    if not used:
        raise ModelApplyError(
            "No active model could score this run — the version's files are "
            "missing or unreadable. Train the model again."
        )

    lines = stage_3b_model.deciding_lines(used)
    # The vetoes are re-applied here: a model decides a bucket, and a veto
    # overrides the model exactly as it overrides Splink (RULESET.md, Vetoes).
    pairs = finalise_pairs(
        apply_overlays(_strip_overlays(pairs), data["units"], review, high,
                       model_lines=lines, ruleset=run_ruleset(run_dir)),
        data["units"],
    )
    review_after = int((pairs["bucket"] == "review").sum())
    reason = stage_3b_model.collapse_reason(
        review_before, review_after, stage_3b_model.distinct_scores(pairs)
    ) if lines else None

    if reason and not force:
        # Put it back before refusing, so a refused apply changes nothing.
        revert_model(run_dir, labels, db_path=db_path, who=who)
        raise ModelApplyError(
            f"Applying this model was refused: {reason}. Reverted to Splink; "
            "nothing changed. Add human labels and a frozen test set, retrain, "
            "then apply — or send force: true.",
            {"reason": reason, "review_before": review_before,
             "review_after": review_after},
        )

    state = stage_3b_model.write_state(run_dir, used, warning=reason, applied=True)
    counts, reclustered = _finish(run_dir, data, pairs, labels, lines, review, high,
                                  candidate, db_path)
    bucketing_history.append(
        run_dir, "model applied",
        accept_line=high, review_line=review, lowest_score_kept=candidate,
        scorer="model",
        model_version={track: model.version for track, model in used.items()},
        counts=counts, who=who or "",
        note=reason or "",
    )
    return {
        "ok": True,
        "tracks": [
            {"track": track, "version": model.version, "graded": model.graded,
             "applied": True,
             "decided_by_model": int(
                 ((pairs["track"] == track) & (pairs["decided_by"] == "model")).sum()),
             "warning": reason}
            for track, model in used.items()
        ],
        "counts": counts,
        "reclustered": reclustered,
        "review_before": review_before,
        "review_after": review_after,
        "state": state,
    }


def revert_model(run_dir, labels: pd.DataFrame | None = None,
                 db_path: str | None = None, who: str = "") -> dict:
    """Take the model off a run: drop `gbt_score` and bucket on Splink again.

    Dropping the score rather than leaving it behind is deliberate. A number on
    a pair that nothing is deciding by reads as if it were, and re-applying is
    one request.

    Reverting a graded model moves buckets back, so the clusters and the
    entities are rebuilt for the same reason applying one rebuilds them.
    """
    run_dir = Path(run_dir)
    data = _read(run_dir)
    candidate, review, high = _thresholds(run_dir)

    pairs = data["pairs"]
    dropped = [column for column in (stage_3b_model.MODEL_SCORE_COLUMN,
                                     stage_3b_model.MODEL_VERSION_COLUMN)
               if column in pairs.columns]
    if dropped:
        pairs = pairs.drop(columns=dropped)
    pairs = finalise_pairs(
        apply_overlays(_strip_overlays(pairs), data["units"], review, high,
                       ruleset=run_ruleset(run_dir)),
        data["units"],
    )
    stage_3b_model.clear_state(run_dir)
    counts, reclustered = _finish(run_dir, data, pairs, labels, {}, review, high,
                                  candidate, db_path)
    bucketing_history.append(
        run_dir, "model reverted",
        accept_line=high, review_line=review, lowest_score_kept=candidate,
        scorer="splink", counts=counts, who=who or "",
    )
    return {"ok": True, "counts": counts, "reclustered": reclustered}

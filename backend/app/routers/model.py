# backend/app/routers/model.py
"""GBT model management: train, status, apply-to-run, active-learning batch, import."""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import current_user
from app.db import query_db, write_db
from app.pipeline import gbt_model
from app.services.audit_logger import log_event
# Collapse guard + GBT defaults live in the pipeline service (shared with the in-band
# auto-apply path); re-exported here so this router and its tests can reference them.
from app.services.pipeline_runner import (
    GBT_DEFAULT_HIGH,
    GBT_DEFAULT_REVIEW,
    _collapse_reason,
    _distinct_gbt_scores,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/model", tags=["model"])


def _db_path() -> str:
    from app.main import DB_PATH
    return DB_PATH


def _run_paths(run_id: str) -> tuple[str, str]:
    from app.main import DATA_DIR
    run_dir = DATA_DIR / "runs" / run_id
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    return str(run_dir), str(run_dir / "config")


class RunRef(BaseModel):
    run_id: str
    force: bool = False


class ActiveBatchRequest(BaseModel):
    run_id: str
    n: int = 50


class ImportLabelsRequest(BaseModel):
    run_id: Optional[str] = None
    results: list[dict]


class DesignateEvalRequest(BaseModel):
    n: int = 200


@router.get("")
def model_status():
    """Whether a trained GBT exists, its current (latest) version and the ACTIVE version
    fresh runs auto-apply, plus its last training metrics and the held-out threshold
    precision/recall (surfaced at the top level for the model panel)."""
    metrics = gbt_model.load_metrics()
    return {
        "exists": gbt_model.model_exists(),
        "version": (metrics or {}).get("version"),
        "active_version": gbt_model.get_active_version(),
        "metrics": metrics,
        "threshold_metrics": (metrics or {}).get("threshold_metrics"),
    }


@router.post("/train")
def train_model(body: RunRef, user: str = Depends(current_user)):
    """Train the global GBT from the label store + cold-start proxy, sourcing
    feature inputs from the given completed run's Phase-2 parquets."""
    from app.pipeline.gbt_train import train

    run_dir, _ = _run_paths(body.run_id)
    try:
        metrics = train(_db_path(), run_dir)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Trained GBT (n_train={metrics.get('n_train')}, auc={metrics.get('auc')})",
        metadata=metrics,
    )
    return metrics


@router.post("/apply")
def apply_model_to_run(body: RunRef, user: str = Depends(current_user)):
    """Score a completed run with the GBT and re-bucket on the calibrated score,
    re-applying labels afterwards (labels stay paramount).

    Collapse guard: small / too-separable labels make the GBT over-confident (bimodal
    near 0 and 1), which empties the review band and bins every uncertain pair without a
    human. We refuse (revert to Splink, HTTP 409, ``force=True`` to override) when the
    applied scores are near-bimodal (regardless of band counts, so a first-ever apply on an
    already-empty band is caught too) OR when a previously non-empty review band collapses
    to zero or a tiny remnant. This keeps a model from silently deciding everything.
    """
    import json

    if not gbt_model.model_exists():
        raise HTTPException(status_code=400, detail="No trained model. Train one first.")
    metrics = gbt_model.load_metrics() or {}
    if (metrics.get("n_labels_train") or 0) == 0:
        raise HTTPException(
            status_code=400,
            detail=("Model is proxy-only (trained without labels) — it has only learned "
                    "exact-vs-random and would collapse the fuzzy review band. Label some pairs "
                    "(or import LLM labels for the active batch), retrain, then apply."),
        )
    from app.services.pipeline_runner import rebucket_run, revert_run_to_splink

    run_dir, config_dir = _run_paths(body.run_id)
    version = gbt_model.latest_version()

    # Review-band size before applying (the Splink baseline), for collapse detection.
    rows = query_db(
        _db_path(),
        "SELECT counts_json FROM runs WHERE id = ?",
        (body.run_id,),
    )
    review_before = 0
    if rows and rows[0]["counts_json"]:
        try:
            review_before = int(json.loads(rows[0]["counts_json"]).get("matches_for_review", 0))
        except (TypeError, ValueError):
            review_before = 0

    # Bucket the calibrated GBT score on GBT-scale lines. Derive them from the model's
    # own held-out eval set (conformal-style) so they match this model's calibrated
    # range; fall back to fixed defaults only when there is no eval set. preserve the
    # pre-apply Splink thresholds so an un-apply/revert can restore them.
    from app.pipeline.gbt_train import derive_gbt_thresholds
    derived = derive_gbt_thresholds(_db_path(), run_dir)
    gbt_high, gbt_review = derived or (GBT_DEFAULT_HIGH, GBT_DEFAULT_REVIEW)
    counts = rebucket_run(
        _db_path(), run_dir, config_dir, run_id=body.run_id,
        threshold_high=gbt_high, threshold_review=gbt_review,
        gbt_score_column="gbt_score", score_with_gbt=True,
        gbt_model_version=version, preserve_splink_baseline=True,
    )
    review_after = int(counts.get("matches_for_review", 0))

    # Collapse guard: refuse when the applied score is near-bimodal or the review band
    # collapsed (empty / tiny remnant). Revert to Splink (restoring its thresholds) and
    # refuse, unless the caller overrides with force=true.
    distinct_scores = _distinct_gbt_scores(run_dir)
    reason = _collapse_reason(review_before, review_after, distinct_scores)
    if reason and not body.force:
        revert_run_to_splink(_db_path(), run_dir, config_dir, run_id=body.run_id)
        log_event(
            _db_path(), user=user or "unknown", kind="model",
            description=f"Blocked GBT apply on run {body.run_id}: {reason}; reverted to Splink",
            metadata={"run_id": body.run_id, "review_before": review_before,
                      "review_after": review_after, "distinct_scores": distinct_scores},
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Applying this model was refused: {reason} — the classic too-few / too-separable "
                "label failure. Reverted to Splink; nothing changed. Add more human labels and a "
                "held-out eval set, retrain, then apply (or re-apply with force=true to override)."
            ),
        )

    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Applied GBT v{version} scores to run {body.run_id}"
                    + (" (forced past collapse guard)" if body.force else ""),
        metadata={"run_id": body.run_id, "version": version, "counts": counts,
                  "review_before": review_before, "review_after": review_after},
    )
    return {"ok": True, "version": version, "counts": counts,
            "review_before": review_before, "review_after": review_after}


class ActivateRequest(BaseModel):
    version: Optional[int] = None


@router.post("/activate")
def activate_model(body: ActivateRequest, user: str = Depends(current_user)):
    """Mark a trained model version ACTIVE so fresh runs auto-apply it (defaults to the
    latest-trained version). Training never auto-activates. Refuses a proxy-only version,
    which would collapse the review band (same rationale as the apply guard)."""
    version = body.version if body.version is not None else gbt_model.latest_version()
    if version is None or not gbt_model.model_exists(version):
        raise HTTPException(status_code=400, detail="No such trained model version to activate.")
    metrics = gbt_model.load_metrics(version) or {}
    if (metrics.get("n_labels_train") or 0) == 0:
        raise HTTPException(
            status_code=400,
            detail=("Model version is proxy-only (trained without labels) — activating it would "
                    "collapse the review band on every run. Label some pairs, retrain, then activate."),
        )
    gbt_model.set_active_version(version)
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Activated GBT model v{version} (fresh runs now auto-apply it)",
        metadata={"version": version},
    )
    return {"ok": True, "active_version": version}


@router.post("/deactivate")
def deactivate_model(user: str = Depends(current_user)):
    """Clear the active model so fresh runs bucket on Splink again."""
    prev = gbt_model.get_active_version()
    gbt_model.clear_active_version()
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Deactivated GBT model (was v{prev}); fresh runs bucket on Splink",
        metadata={"previous_active_version": prev},
    )
    return {"ok": True, "active_version": None}


@router.post("/revert")
def revert_run(body: RunRef, user: str = Depends(current_user)):
    """Un-apply the GBT from a run: re-bucket on its preserved Splink thresholds and clear
    the GBT flags. The inverse of /apply, so a per-run GBT decision is reversible."""
    from app.services.pipeline_runner import revert_run_to_splink

    run_dir, config_dir = _run_paths(body.run_id)
    counts = revert_run_to_splink(_db_path(), run_dir, config_dir, run_id=body.run_id)
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Reverted run {body.run_id} to Splink bucketing",
        metadata={"run_id": body.run_id, "counts": counts},
    )
    return {"ok": True, "counts": counts}


@router.post("/active-batch")
def active_batch(body: ActiveBatchRequest, user: str = Depends(current_user)):
    """Return the next high-value batch of unlabelled pairs to label, plus the criteria."""
    from app.pipeline.gbt_active import sample_batch
    from app.services.llm_labeler import LABELLING_CRITERIA

    run_dir, _ = _run_paths(body.run_id)
    batch = sample_batch(run_dir, _db_path(), n=body.n)
    return {"criteria": LABELLING_CRITERIA, "batch": batch, "count": len(batch)}


@router.get("/eval-set")
def eval_set_status():
    """Size + verdict breakdown of the frozen, human-only evaluation set (held_out=1)."""
    rows = query_db(
        _db_path(),
        "SELECT is_true_match, COUNT(*) AS c FROM labels WHERE active = 1 AND held_out = 1 GROUP BY is_true_match",
    )
    by_verdict = {str(r["is_true_match"]).upper(): r["c"] for r in rows}
    return {"total": sum(by_verdict.values()), "by_verdict": by_verdict}


@router.post("/eval-set/designate")
def designate_eval_set(body: DesignateEvalRequest, user: str = Depends(current_user)):
    """Freeze a human-only evaluation set: sample up to N explicit manual labels,
    balanced TRUE/FALSE, and set held_out=1. Frozen + excluded from training; refreshed
    only by an explicit re-designation."""
    db_path = _db_path()
    half = max(1, body.n // 2)
    chosen: list[int] = []
    left_for_training = 0
    for verdict in ("TRUE", "FALSE"):
        rows = query_db(
            db_path,
            """SELECT id FROM labels
               WHERE active = 1 AND held_out = 0
                 AND upper(is_true_match) = ?
                 AND (provenance = 'manual' OR provenance IS NULL)
               ORDER BY created_at DESC""",
            (verdict,),
        )
        available = len(rows)
        # Never set aside more than half of each verdict's labels, so an equal-or-larger
        # remainder is always left to train on — designating a test set must never empty
        # the training pool (with few labels the old code took them all).
        take = min(half, available // 2)
        chosen.extend(r["id"] for r in rows[:take])
        left_for_training += available - take

    for label_id in chosen:
        write_db(db_path, "UPDATE labels SET held_out = 1 WHERE id = ?", (label_id,))

    log_event(
        db_path, user=user or "unknown", kind="model",
        description=f"Designated {len(chosen)} labels as the frozen eval set "
                    f"(left {left_for_training} for training)",
        metadata={"designated": len(chosen), "left_for_training": left_for_training},
    )
    return {"designated": len(chosen), "left_for_training": left_for_training}


@router.post("/import-labels")
def import_labels(body: ImportLabelsRequest, user: str = Depends(current_user)):
    """Import LLM/subagent-labelled results into the store (provenance='llm')."""
    from app.services.llm_labeler import import_labels as _import

    result = _import(_db_path(), body.results, reviewer="llm", run_id=body.run_id)
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Imported {result['imported']} LLM labels ({result['skipped']} skipped)",
        metadata=result,
    )
    return result


class ImportLabelsCsvRequest(BaseModel):
    csv: str
    run_id: Optional[str] = None


@router.post("/import-labels-csv")
def import_labels_csv(body: ImportLabelsCsvRequest, user: str = Depends(current_user)):
    """ADD labelled results from a CSV (e.g. the 'download active batch' file after an
    LLM/analyst has filled in is_true_match). Additive — provenance='llm', does NOT
    overwrite the library (unlike /api/labels/import-csv). Rows with a blank verdict are
    skipped (still to do)."""
    import csv as csvmod
    import io
    from app.services.llm_labeler import import_labels as _import

    reader = csvmod.DictReader(io.StringIO(body.csv))
    results = []
    for r in reader:
        if not (r.get("is_true_match") or "").strip():
            continue
        results.append({
            "ocod_name_clean": r.get("ocod_name_clean"),
            "jurisdiction_clean": r.get("jurisdiction_clean"),
            "roe_company_number": r.get("roe_company_number"),
            "ocod_name_raw": r.get("ocod_name_raw"),
            "is_true_match": r.get("is_true_match"),
        })
    result = _import(_db_path(), results, reviewer="llm", run_id=body.run_id)
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Imported {result['imported']} labelled-batch rows from CSV ({result['skipped']} skipped)",
        metadata=result,
    )
    return result


# --- Per-pair "why this score": exact Tree-SHAP via LightGBM pred_contrib ---
_EXPLAIN_CACHE: dict = {}


def _explain_ctx(run_id: str):
    """Load + cache (ocod, roe, scored, booster, feature_cols) for a run, keyed by the
    model file's mtime so a retrain transparently invalidates the cache."""
    import pandas as pd
    from app.pipeline import gbt_model

    run_dir, _ = _run_paths(run_id)
    mf = gbt_model.model_file()
    mtime = mf.stat().st_mtime if mf.exists() else 0
    key = (run_id, mtime)
    ctx = _EXPLAIN_CACHE.get(key)
    if ctx is None:
        booster = gbt_model.load_booster()
        cols = gbt_model.load_feature_cols()
        if booster is None or not cols:
            return None
        rd = Path(run_dir)
        scored_path = rd / "linkage_scored.parquet"
        ctx = {
            "ocod": pd.read_parquet(rd / "ocod_phase2.parquet"),
            "roe": pd.read_parquet(rd / "roe_phase2.parquet"),
            "scored": pd.read_parquet(scored_path) if scored_path.exists() else None,
            "booster": booster,
            "cols": cols,
        }
        _EXPLAIN_CACHE.clear()   # keep only the most recent run/model
        _EXPLAIN_CACHE[key] = ctx
    return ctx


class ExplainRequest(BaseModel):
    run_id: str
    ocod_name_clean: str
    jurisdiction_clean: Optional[str] = None
    roe_company_number: str


@router.post("/explain")
def explain_pair(body: ExplainRequest, user: str = Depends(current_user)):
    """Explain ONE OCOD<->ROE pair's GBT score: the base rate plus each feature's signed
    push (exact Tree-SHAP from LightGBM ``pred_contrib``), the raw model probability and
    the calibrated probability. Contributions are in log-odds space (they sum to the raw
    margin); the calibrated probability is what the run buckets on."""
    import numpy as np
    import pandas as pd
    from app.pipeline import gbt_model
    from app.pipeline.gbt_features import build_features

    ctx = _explain_ctx(body.run_id)
    if ctx is None:
        raise HTTPException(status_code=400, detail="No trained model to explain with.")
    ocod, roe = ctx["ocod"], ctx["roe"]

    om = ocod[ocod["name_clean"].astype(str) == str(body.ocod_name_clean)]
    if body.jurisdiction_clean:
        omj = om[om["jurisdiction_clean"].astype(str) == str(body.jurisdiction_clean)]
        if len(omj):
            om = omj
    rm = roe[roe["roe_company_number"].astype(str) == str(body.roe_company_number)]
    if om.empty or rm.empty:
        raise HTTPException(status_code=404, detail="Pair not found in this run's Phase-2 data.")
    uid_l, uid_r = om.iloc[0]["unique_id"], rm.iloc[0]["unique_id"]

    # The Splink probability is itself a feature (splink_p1); pull it from the scored pairs.
    splink_p = -1.0
    scored = ctx["scored"]
    if scored is not None and {"unique_id_l", "unique_id_r"}.issubset(scored.columns):
        hit = scored[(scored["unique_id_l"] == uid_l) & (scored["unique_id_r"] == uid_r)]
        if len(hit):
            splink_p = float(pd.to_numeric(pd.Series([hit.iloc[0].get("match_probability")]),
                                           errors="coerce").fillna(-1.0).iloc[0])

    pairs = pd.DataFrame({"unique_id_l": [uid_l], "unique_id_r": [uid_r], "match_probability": [splink_p]})
    feats = build_features(pairs, ocod, roe, splink_prob_col="match_probability")
    cols = ctx["cols"]
    X = feats.reindex(columns=cols, fill_value=-1.0).astype(float).fillna(-1.0)
    booster = ctx["booster"]
    raw = float(booster.predict(X.values)[0])
    calibrated = float(gbt_model.apply_calibration(np.asarray([raw]))[0])
    contrib = booster.predict(X.values, pred_contrib=True)[0]
    items = [{"feature": c, "value": float(X.iloc[0][c]), "contribution": float(v)}
             for c, v in zip(cols, contrib[:-1])]
    items.sort(key=lambda d: -abs(d["contribution"]))
    return {"ok": True, "raw": raw, "calibrated": calibrated,
            "base": float(contrib[-1]), "contributions": items}

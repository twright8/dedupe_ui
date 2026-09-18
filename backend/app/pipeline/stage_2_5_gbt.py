"""Stage 2.5: GBT re-scoring of Splink candidate pairs.

Reads ``linkage_scored.parquet`` (Splink output), builds the portable
company-name feature set, runs the trained GBT, calibrates, and writes a
``gbt_score`` column back into ``linkage_scored.parquet``.

Continuous GBT scores replace Splink's discrete Fellegi-Sunter levels — this is
what de-clumps the score distribution so thresholds, margins and mark-by-range
become meaningful (Stage 3 swaps ``gbt_score`` in for ``match_probability``
when ``gbt_score_column`` is set in linkage_settings).

No-op (leaves the parquet untouched) if no model has been trained yet, so a
cold pipeline still runs end-to-end on raw Splink scores.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from app.pipeline import gbt_model
from app.pipeline.gbt_features import FEATURE_COLS, build_features

logger = logging.getLogger(__name__)


def _step(label, progress_callback=None):
    print(f"[{time.strftime('%H:%M:%S')}] {label}", flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def run_stage_2_5_gbt(run_dir: str, progress_callback=None, version: int | None = None, **_kwargs) -> dict:
    """Score Phase-2 pairs with the GBT and add a ``gbt_score`` column.

    ``version`` selects an immutable model snapshot (the *active* version, for the
    in-band auto-apply path); ``None`` loads the latest-trained model. No-op if the
    requested model has no artifacts yet, so a cold pipeline still runs end-to-end.
    """
    run_dir = Path(run_dir)
    if progress_callback:
        progress_callback("stage_start", {"stage": 2.5, "name": "gbt_score"})

    scored_path = run_dir / "linkage_scored.parquet"
    if not scored_path.exists():
        _step("  No linkage_scored.parquet — skipping GBT scoring.", progress_callback)
        return {"scored": 0, "model": False, "version": version}

    booster = gbt_model.load_booster(version)
    feature_cols = gbt_model.load_feature_cols(version)
    if booster is None or not feature_cols:
        _step("  No trained GBT model yet — skipping (pipeline runs on Splink scores).", progress_callback)
        return {"scored": 0, "model": False, "version": version}

    scored = pd.read_parquet(scored_path)
    if len(scored) == 0:
        return {"scored": 0, "model": True, "version": version}

    ocod = pd.read_parquet(run_dir / "ocod_phase2.parquet")
    roe = pd.read_parquet(run_dir / "roe_phase2.parquet")

    vlabel = f" (model v{version})" if version is not None else ""
    _step(f"  Building GBT features for {len(scored):,} candidate pairs{vlabel}...", progress_callback)
    feats = build_features(scored, ocod, roe, splink_prob_col="match_probability")
    # Align to the model's persisted feature order; tolerate schema drift.
    for col in feature_cols:
        if col not in feats.columns:
            feats[col] = -1.0
    X = feats[feature_cols].astype(float).fillna(-1.0)

    raw = booster.predict(X)
    calibrated = gbt_model.apply_calibration(raw, version)

    scored["gbt_score_raw"] = raw
    scored["gbt_score"] = calibrated
    scored.to_parquet(scored_path, index=False)

    _step(f"  GBT scoring done ({len(scored):,} pairs){vlabel}.", progress_callback)
    if progress_callback:
        progress_callback("stage_end", {"stage": 2.5, "name": "gbt_score",
                                         "scored": len(scored), "version": version})
    return {"scored": int(len(scored)), "model": True, "version": version}

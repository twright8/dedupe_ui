"""Train the global GBT re-scoring model from labels + cold-start proxy.

Pipeline (pillar 4 of the framework):
  Splink/EM  -> candidate generation + features (p1)
  GBT        -> the decision model, trained on human/LLM TRUE *and* FALSE labels
  Platt      -> logistic calibration so the score reads as a probability

Training set  = cold-start proxy (synthetic) + active DB labels with held_out=0.
Evaluation    = active DB labels with held_out=1 (frozen, human-only) when present;
                otherwise a stratified split of the real labels (best effort).
The proxy is NEVER used for evaluation.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from app.db import query_db
from app.pipeline import gbt_model
from app.pipeline.gbt_features import FEATURE_COLS, build_features
from app.pipeline.gbt_proxy import build_proxy_frame

logger = logging.getLogger(__name__)

_LGBM_PARAMS = dict(
    objective="binary",
    n_estimators=300,
    learning_rate=0.05,
    num_leaves=15,
    min_child_samples=4,
    reg_lambda=0.5,
    random_state=42,
    verbosity=-1,
)


def _monotone_constraints() -> list[int]:
    """Per-feature monotonic direction for LightGBM, aligned to FEATURE_COLS.
    ``unit_mismatch`` is pinned to -1 so a unit/ordinal mismatch can only ever lower the
    match probability, never raise it (a guaranteed hard-negative). ``jurisdiction_match``
    is pinned to +1 so its ordinal 0.0/0.5/1.0 encoding is honoured monotonically: a
    genuine jurisdiction mismatch can only lower the score, an agreement can only raise
    it, and unknown (0.5) sits structurally between — never punished below a mismatch."""
    _MONO = {"unit_mismatch": -1, "jurisdiction_match": 1}
    return [_MONO.get(c, 0) for c in FEATURE_COLS]


def _norm(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.upper()


def _labelled_feature_frame(
    db_path: str,
    ocod: pd.DataFrame,
    roe: pd.DataFrame,
    scored: pd.DataFrame | None,
    held_out: int,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Resolve active labels (TRUE/FALSE) to Phase-2 pairs and featurise them.

    Resolution goes through the shared label resolver (raw values first, the
    stored clean key as the legacy fallback), so training resolves exactly the
    labels that display and export do and a cleaning-rule change cannot
    silently drop labels out of training. The OCOD side is raw-first only when
    the frame carries raw columns — ocod_phase2.parquet is clean-only today,
    so its labels resolve via the clean fallback."""
    from app.services.label_resolver import build_frame_key_index, find_indexed_rows, norm

    rows = query_db(
        db_path,
        """SELECT ocod_name_clean, jurisdiction_clean, roe_company_number,
                  ocod_name_raw, ocod_jurisdiction_raw, is_true_match, provenance
           FROM labels
           WHERE active = 1 AND held_out = ?
             AND upper(is_true_match) IN ('TRUE', 'FALSE')""",
        (held_out,),
    )
    if not rows or "roe_company_number" not in roe.columns:
        return pd.DataFrame(columns=FEATURE_COLS), np.array([], dtype=int)

    ocod_index = build_frame_key_index(ocod)
    ocod_ids = ocod["unique_id"]
    roe_ids: dict[str, list] = {}
    for uid, number in zip(roe["unique_id"], _norm(roe["roe_company_number"])):
        if number:
            roe_ids.setdefault(number, []).append(uid)

    pairs = []
    for label in rows:
        uid_rs = roe_ids.get(norm(label["roe_company_number"]))
        if not uid_rs:
            continue
        y = int(norm(label["is_true_match"]) == "TRUE")
        for i in find_indexed_rows(ocod_index, label):
            for uid_r in uid_rs:
                pairs.append((ocod_ids.at[i], uid_r, y, label["provenance"]))
    if not pairs:
        return pd.DataFrame(columns=FEATURE_COLS), np.array([], dtype=int)

    matched = pd.DataFrame(pairs, columns=["unique_id_l", "unique_id_r", "_y", "provenance"])
    matched = matched.drop_duplicates(subset=["unique_id_l", "unique_id_r"]).reset_index(drop=True)

    # Join the Splink probability where the pair was scored in Phase 2.
    if scored is not None and {"unique_id_l", "unique_id_r", "match_probability"}.issubset(scored.columns):
        sp = scored[["unique_id_l", "unique_id_r", "match_probability"]]
        matched = matched.merge(sp, on=["unique_id_l", "unique_id_r"], how="left")

    feats = build_features(matched, ocod, roe, splink_prob_col="match_probability").reset_index(drop=True)

    # Do NOT train on implied-negatives at all. They encode a RANKING decision ("this
    # candidate lost to a slightly better one"), not a fact about the pair — the same
    # features can be a genuine match in another context, so training them as hard negatives
    # distorts the global decision boundary. The near-miss number cases they used to cover
    # (13 vs 4) are now carried by the unit_mismatch feature instead, and one-true-per-entity
    # is enforced at output time, not by minting negatives.
    prov = matched["provenance"].fillna("").to_numpy()
    keep = prov != "implied_negative"
    return feats[keep].reset_index(drop=True), matched["_y"].to_numpy()[keep]


def derive_gbt_thresholds(db_path: str, run_dir: str,
                          accept_purity: float = 0.90, reject_purity: float = 0.90) -> tuple | None:
    """Data-driven bucket lines for the calibrated GBT score, from the model's own
    held-out eval set (conformal-style). Auto-REJECT below the highest score where a
    Wilson-95%-lower-bound says >= reject_purity of those are non-matches; auto-ACCEPT
    above the lowest score where >= accept_purity are matches. Returns (accept, reject)
    on the 0..1 dial, or None if there is no usable eval set. This adapts to whatever
    range the calibration produces (a fixed threshold can't, since the range moves)."""
    rd = Path(run_dir)
    try:
        ocod = pd.read_parquet(rd / "ocod_phase2.parquet")
        roe = pd.read_parquet(rd / "roe_phase2.parquet")
        sp = rd / "linkage_scored.parquet"
        scored = pd.read_parquet(sp) if sp.exists() else None
    except Exception:
        return None
    eX, ey = _labelled_feature_frame(db_path, ocod, roe, scored, held_out=1)
    if len(ey) < 20 or len(np.unique(ey)) < 2:
        return None
    booster = gbt_model.load_booster()
    cols = gbt_model.load_feature_cols()
    if booster is None or not cols:
        return None
    for c in cols:
        if c not in eX.columns:
            eX[c] = -1.0
    cal = np.asarray(gbt_model.apply_calibration(booster.predict(eX[cols])), dtype=float)

    uniq = np.unique(cal)
    accept = next((float(t) for t in uniq
                   if (cal >= t).sum()
                   and _wilson_lower_bound(int(ey[cal >= t].sum()), int((cal >= t).sum())) >= accept_purity),
                  float(cal.max()))
    reject = next((float(t) for t in uniq[::-1]
                   if (cal < t).sum()
                   and _wilson_lower_bound(int((ey[cal < t] == 0).sum()), int((cal < t).sum())) >= reject_purity),
                  float(cal.min()))
    if accept <= reject:
        return None
    return round(accept, 3), round(reject, 3)


def train(db_path: str, run_dir: str) -> dict:
    """Train and persist the GBT model. Returns a metrics dict."""
    import lightgbm as lgb

    run_dir = Path(run_dir)
    ocod = pd.read_parquet(run_dir / "ocod_phase2.parquet")
    roe = pd.read_parquet(run_dir / "roe_phase2.parquet")
    scored_path = run_dir / "linkage_scored.parquet"
    scored = pd.read_parquet(scored_path) if scored_path.exists() else None

    # Training data: proxy + non-held-out labels.
    proxy_X, proxy_y = build_proxy_frame(str(run_dir))
    lbl_X, lbl_y = _labelled_feature_frame(db_path, ocod, roe, scored, held_out=0)
    eval_X, eval_y = _labelled_feature_frame(db_path, ocod, roe, scored, held_out=1)

    parts_X = [df for df in (proxy_X, lbl_X) if len(df)]
    parts_y = [y for y in (proxy_y, lbl_y) if len(y)]
    if not parts_X:
        raise RuntimeError("No training data: need proxy inputs (exact_matches/phase2) or labels.")

    X = pd.concat(parts_X, ignore_index=True)[FEATURE_COLS]
    y = np.concatenate(parts_y)

    if len(np.unique(y)) < 2:
        raise RuntimeError("Training data has only one class; cannot train a classifier.")

    clf = lgb.LGBMClassifier(**_LGBM_PARAMS, monotone_constraints=_monotone_constraints())
    clf.fit(X, y)

    metrics: dict = {
        "n_train": int(len(y)),
        "n_train_pos": int(y.sum()),
        "n_proxy": int(len(proxy_y)),
        "n_labels_train": int(len(lbl_y)),
        "n_labels_eval": int(len(eval_y)),
        "features": FEATURE_COLS,
    }

    # Calibration: fit a smooth Platt (logistic) curve OUT-OF-FOLD on the training
    # labels, keeping the frozen eval set purely for grading. Isotonic was replaced
    # (see _platt_grid) because on sparse/separable labels its staircase re-clumps
    # the de-clumped score, ties pairs together (hurting ranking), and goes bimodal —
    # emptying the review band. Platt stays smooth, monotone and stable.
    oof = _oof_predictions(proxy_X, proxy_y, lbl_X, lbl_y)
    calibrator = None
    human_oof = human_oof_y = None
    if oof is not None:
        cal_raw, cal_y, is_human = oof
        # Fit Platt on the HUMAN out-of-fold rows only (proxy is masked out) so the
        # synthetic 0/1 extremes cannot drag the calibrator to a bimodal collapse.
        grid, curve, calibrator = _platt_grid(cal_raw, cal_y, is_human)
        gbt_model.save_calibration(
            grid.tolist(), curve.tolist(), {"method": "platt", "n": int(is_human.sum())}
        )
        metrics["calibration"] = "platt"
        human_oof, human_oof_y = cal_raw[is_human], cal_y[is_human]
    else:
        gbt_model.save_calibration([0.0, 1.0], [0.0, 1.0], {"method": "identity"})
        metrics["calibration"] = "identity"

    # Grade on the frozen human eval set when present (never trained on, never used
    # for calibration -> honest). Otherwise fall back to the OOF training predictions.
    eval_raw = clf.predict_proba(eval_X[FEATURE_COLS])[:, 1] if len(eval_y) else None
    if len(eval_y) and len(np.unique(eval_y)) == 2:
        metrics["eval_source"] = "held_out"
        _record_metrics(metrics, eval_raw, eval_y, calibrator)
    elif human_oof is not None:
        metrics["eval_source"] = "oof_train"
        _record_metrics(metrics, human_oof, human_oof_y, calibrator)
    else:
        metrics["eval_source"] = "none"

    # Threshold precision/recall + Wilson bounds — ALWAYS from the frozen human eval set
    # (held_out=1), never training data (CONCERNS_LOG S). threshold_metrics() emits an
    # explicit unavailable+reason when the eval set is empty or single-class. Computed on
    # the CALIBRATED score, the same dial Stage 3 buckets on.
    if eval_raw is not None:
        eval_cal = (calibrator.predict_proba(eval_raw.reshape(-1, 1))[:, 1]
                    if calibrator is not None else eval_raw)
        metrics["threshold_metrics"] = threshold_metrics(eval_cal, eval_y)
    else:
        metrics["threshold_metrics"] = {
            "available": False, "reason": "no held-out eval set (held_out=1)"
        }

    clf.booster_.save_model(str(gbt_model.model_file()))
    gbt_model.feature_cols_file().write_text(__import__("json").dumps(FEATURE_COLS), encoding="utf-8")
    gbt_model.save_metrics(metrics)
    logger.info("GBT trained: %s", metrics)
    return metrics


def _platt_grid(raw: np.ndarray, y: np.ndarray, is_human: np.ndarray):
    """Fit a Platt (logistic) calibrator raw_score -> probability on the HUMAN rows only
    (``is_human`` mask) and sample it onto a 0..1 lookup grid. Proxy rows are dropped
    before the fit so the synthetic exact-vs-random extremes never enter calibration —
    fitting on them re-separates the score and pushes it bimodal, emptying the review band
    (CONCERNS_LOG R). A smooth, monotone curve: unlike isotonic it cannot flatten the
    score into steps or tie pairs together; sklearn's default L2 keeps it from going
    over-confident on near-separable labels. Returns (grid, calibrated_grid, fitted_model)."""
    from sklearn.linear_model import LogisticRegression

    mask = np.asarray(is_human, dtype=bool)
    raw = np.asarray(raw, dtype=float)[mask]
    y = np.asarray(y)[mask]
    lr = LogisticRegression()
    lr.fit(raw.reshape(-1, 1), y)
    grid = np.linspace(0.0, 1.0, 201)
    cal = lr.predict_proba(grid.reshape(-1, 1))[:, 1]
    return grid, np.asarray(cal, dtype=float), lr


def _oof_predictions(proxy_X, proxy_y, lbl_X, lbl_y, k: int = 4):
    """Out-of-fold GBT scores for the calibrator, tagged with per-row provenance.

    HUMAN labels (held_out=0) are scored OUT-OF-FOLD — each by a fold-model that never
    saw it — the honest signal for calibration. The cold-start PROXY stays in EVERY
    fold's training split (so the booster still learns the separable extremes) but is
    returned with ``is_human=False`` so calibration can be fit on human rows ONLY.
    Returns ``(scores, y, is_human)`` aligned row-for-row, or ``None`` when there are
    fewer than 12 HUMAN labels or a class is missing (calibration then falls back to
    identity)."""
    if len(lbl_y) < 12 or len(np.unique(lbl_y)) < 2:
        return None
    import lightgbm as lgb
    from sklearn.model_selection import StratifiedKFold

    folds = min(k, int(np.bincount(lbl_y).min()))
    if folds < 2:
        return None
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
    Xr = lbl_X.reset_index(drop=True)
    oof = np.zeros(len(lbl_y))
    proxy_scores = np.zeros(len(proxy_y))
    for tr_idx, te_idx in skf.split(Xr, lbl_y):
        X_tr = pd.concat([proxy_X, Xr.iloc[tr_idx]], ignore_index=True)[FEATURE_COLS]
        y_tr = np.concatenate([proxy_y, lbl_y[tr_idx]])
        c = lgb.LGBMClassifier(**_LGBM_PARAMS, monotone_constraints=_monotone_constraints())
        c.fit(X_tr, y_tr)
        oof[te_idx] = c.predict_proba(Xr.iloc[te_idx][FEATURE_COLS])[:, 1]
        if len(proxy_y):
            # In-fold (proxy is always in train) — tagged non-human and dropped at fit
            # time; carried only so provenance is explicit at calibration.
            proxy_scores += c.predict_proba(proxy_X[FEATURE_COLS])[:, 1] / folds
    scores = np.concatenate([oof, proxy_scores])
    y = np.concatenate([lbl_y, proxy_y]).astype(int)
    is_human = np.concatenate([
        np.ones(len(lbl_y), dtype=bool), np.zeros(len(proxy_y), dtype=bool)
    ])
    return scores, y, is_human


def _wilson_lower_bound(x: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval LOWER bound for ``x`` successes in ``n`` trials (95% at
    z=1.96). Honest on tiny n where a raw proportion over-claims (e.g. 3/3 -> 0.44)."""
    if n == 0:
        return 0.0
    p = x / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def threshold_metrics(scores: np.ndarray, y: np.ndarray) -> dict:
    """Precision & recall at a 0..1 threshold grid (0.05 steps), each with a Wilson 95%
    LOWER bound. The eval set is tiny (~tens of labels), so point estimates alone mislead;
    the lower bound is the honest number. Caller MUST pass the held-out eval scores/labels
    only — never training data (CONCERNS_LOG S). Returns ``{"available": True, ...}`` with
    a per-threshold list, or ``{"available": False, "reason": ...}`` when the eval set is
    empty or single-class (no fabricated perfect metrics)."""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=int)
    if len(y) == 0:
        return {"available": False, "reason": "held-out eval set is empty"}
    if len(np.unique(y)) < 2:
        return {"available": False, "reason": "held-out eval set is single-class"}

    n_pos = int((y == 1).sum())
    rows = []
    for t in np.round(np.arange(0.0, 1.0001, 0.05), 2):
        pred = scores >= t
        tp = int((pred & (y == 1)).sum())
        n_pred = int(pred.sum())
        rows.append({
            "threshold": float(t),
            "n_predicted_positive": n_pred,
            "tp": tp,
            "fp": n_pred - tp,
            "precision": (tp / n_pred) if n_pred else None,
            "precision_wilson_lower": _wilson_lower_bound(tp, n_pred) if n_pred else None,
            "recall": (tp / n_pos) if n_pos else None,
            "recall_wilson_lower": _wilson_lower_bound(tp, n_pos) if n_pos else None,
        })
    return {
        "available": True,
        "n_eval": int(len(y)),
        "n_pos": n_pos,
        "n_neg": int((y == 0).sum()),
        "thresholds": rows,
    }


def _record_metrics(metrics: dict, raw: np.ndarray, y: np.ndarray, calibrator) -> None:
    """Record ranking (AUC) + probability quality (Brier, raw and calibrated) on a
    grading set."""
    from sklearn.metrics import brier_score_loss, roc_auc_score

    raw = np.asarray(raw, dtype=float)
    try:
        metrics["auc"] = float(roc_auc_score(y, raw))
    except Exception:
        metrics["auc"] = None
    metrics["brier_raw"] = float(brier_score_loss(y, raw))
    if calibrator is not None:
        cal = calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]
        metrics["brier_calibrated"] = float(brier_score_loss(y, cal))

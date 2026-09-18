# backend/app/model/train.py
"""Train one track's GBT: fit, calibrate, grade, explain, and write the version.

The order matters and each step has a reason.

1. **Collect the labels** (`dataset`) and work out the folds — connected
   components over units, so one donor never sits on both sides of a fold.
2. **Build the features** (`features`) for the whole track at once, then index
   the training rows out of that frame. The model must score every pair later,
   so building once is both faster and the only way to be sure training and
   scoring see identical columns.
3. **Fit** LightGBM with the monotone constraints the feature metadata carries
   and the sample weights `MODEL.md` sets.
4. **Calibrate** with Platt, fitted out of fold on human rows only. Isotonic was
   tried in `roe_ui` and rejected: on sparse, nearly separable labels its
   staircase re-clumps the score, ties pairs together and goes bimodal, which
   empties the review band (`CONCERNS_LOG` R). Platt stays smooth and monotone.
5. **Grade** on `held_out = 1` human labels — never trained on, never
   calibrated on. Precision and recall carry Wilson 95% lower bounds, because a
   test set of tens of rows makes a bare proportion a lie (`CONCERNS_LOG` S).
   With no test set, nothing is graded and no line is set.
6. **Explain**: gain, mean |SHAP|, and an ablation that retrains without each
   feature group.

Given a seed, the whole thing is deterministic: LightGBM runs single-threaded in
deterministic mode, the fold assignment is a greedy walk over sorted groups, and
the imported-positive sample is a hash order rather than a random draw.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.model import dataset, features as feature_lib, references as reference_lib
from app.model import settings as model_settings
from app.model import store

logger = logging.getLogger(__name__)

# The sentence `MODEL.md` requires in every training report.
KNOWN_LIMIT = (
    "The imported labels were made mostly on the name. They cannot teach the model "
    "when two people with one name are different people. Only new keep-apart labels can."
)

# The calibrated-score grid the threshold table walks.
THRESHOLD_STEP = 0.05
# Bins in the reliability diagram.
CALIBRATION_BINS = 10
# The lookup grid the calibration is sampled onto.
CALIBRATION_GRID = 201

STEPS = (
    ("labels", "Collecting labels", 5),
    ("features", "Building features", 15),
    ("folds", "Splitting into folds", 40),
    ("fit", "Fitting the model", 45),
    ("calibrate", "Calibrating", 60),
    ("grade", "Grading on the test set", 65),
    ("importance", "Working out which features matter", 70),
    ("save", "Saving the version", 95),
)
STEP_LABELS = {key: (label, percent) for key, label, percent in STEPS}


class TrainingError(RuntimeError):
    """Training cannot go ahead, in words fit to show a user."""


# ---------------------------------------------------------------------------
# Small statistics
# ---------------------------------------------------------------------------


def wilson_bounds(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for *x* successes in *n* trials, 95% at z=1.96.

    Honest where a bare proportion over-claims: 3 out of 3 is 0.44 to 1.00, not
    "100%".
    """
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator)


def wilson_lower(x: int, n: int, z: float = 1.96) -> float:
    return wilson_bounds(x, n, z)[0]


def _auc(y, scores) -> float | None:
    from sklearn.metrics import roc_auc_score

    y = np.asarray(y, dtype=int)
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, np.asarray(scores, dtype=float)))


def _average_precision(y, scores) -> float | None:
    from sklearn.metrics import average_precision_score

    y = np.asarray(y, dtype=int)
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    return float(average_precision_score(y, np.asarray(scores, dtype=float)))


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _fit(X: pd.DataFrame, y, weight, params: dict, categorical=None):
    """Fit LightGBM, telling it which columns are categories rather than numbers.

    A category is split by set membership, not by ``<=``. "Kind of donor" has no
    order — a trade union does not sit between a company and a trust — so
    letting a tree cut it on a threshold would invent one (D13c).
    """
    import lightgbm as lgb

    columns = [c for c in (categorical or []) if c in X.columns]
    model = lgb.LGBMClassifier(**params)
    model.fit(
        # LightGBM reads a category from an integer column, and the feature
        # frame is float throughout so one dtype serves training and scoring.
        X.astype({c: "int32" for c in columns}) if columns else X,
        np.asarray(y, dtype=int),
        sample_weight=np.asarray(weight, dtype=float),
        categorical_feature=columns or "auto",
    )
    return model


def _out_of_fold(X: pd.DataFrame, y, weight, fold, params,
                 categorical=None) -> np.ndarray:
    """A score for every training row, from a model that never saw it."""
    y = np.asarray(y, dtype=int)
    out = np.full(len(y), np.nan)
    for number in np.unique(fold):
        holdout = fold == number
        train = ~holdout
        if not train.any() or not holdout.any():
            continue
        if len(np.unique(y[train])) < 2:
            # A fold with one class cannot be fitted; leave those rows unscored
            # rather than inventing a constant.
            continue
        model = _fit(X[train], y[train], np.asarray(weight)[train], params,
                     categorical)
        out[holdout] = model.predict_proba(X[holdout])[:, 1]
    return out


def _platt(raw: np.ndarray, y: np.ndarray):
    """Fit a logistic curve raw score -> probability, and sample it onto a grid."""
    from sklearn.linear_model import LogisticRegression

    raw = np.asarray(raw, dtype=float).reshape(-1, 1)
    y = np.asarray(y, dtype=int)
    model = LogisticRegression()
    model.fit(raw, y)
    grid = np.linspace(0.0, 1.0, CALIBRATION_GRID)
    curve = model.predict_proba(grid.reshape(-1, 1))[:, 1]
    return grid, curve


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def threshold_grid(scores: np.ndarray, y: np.ndarray) -> list[dict]:
    """Precision and recall at a 0.05 grid, each with a Wilson 95% lower bound."""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=int)
    n_positive = int((y == 1).sum())
    rows = []
    for threshold in np.round(np.arange(0.0, 1.0 + THRESHOLD_STEP / 2, THRESHOLD_STEP), 2):
        rows.append(_threshold_row(float(threshold), scores, y, n_positive))
    return rows


def _threshold_row(threshold: float, scores, y, n_positive: int) -> dict:
    predicted = scores >= threshold
    n_predicted = int(predicted.sum())
    tp = int((predicted & (y == 1)).sum())
    return {
        "threshold": round(float(threshold), 4),
        "n_predicted_positive": n_predicted,
        "tp": tp,
        "fp": n_predicted - tp,
        "precision": (tp / n_predicted) if n_predicted else None,
        "precision_wilson_lower": wilson_lower(tp, n_predicted) if n_predicted else None,
        "recall": (tp / n_positive) if n_positive else None,
        "recall_wilson_lower": wilson_lower(tp, n_positive) if n_positive else None,
    }


def derive_thresholds(scores: np.ndarray, y: np.ndarray, target: float) -> dict:
    """The accept and reject lines the frozen test set supports.

    Accept is the lowest score at which the Wilson lower bound on precision
    reaches *target*; reject is the highest score below which the same test says
    the pairs are non-matches at *target*. Either may be absent, and absent means
    "no line" — everything the model scores then goes to review, which is the
    honest answer when the test set is too small to promise anything.
    """
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=int)
    n_positive = int((y == 1).sum())
    n_negative = int((y == 0).sum())
    candidates = np.unique(scores)

    accept = None
    for threshold in candidates:
        chosen = scores >= threshold
        n = int(chosen.sum())
        if n and wilson_lower(int(y[chosen].sum()), n) >= target:
            accept = float(threshold)
            break

    reject = None
    for threshold in candidates[::-1]:
        chosen = scores < threshold
        n = int(chosen.sum())
        if n and wilson_lower(int((y[chosen] == 0).sum()), n) >= target:
            reject = float(threshold)
            break

    if accept is not None and reject is not None and reject > accept:
        # The two lines crossed, so the same score would be both accepted and
        # rejected. The test set cannot support both; keep accept, which is the
        # line that decides a pair, and drop reject. Equal lines are fine: accept
        # covers scores at or above the line and reject everything below it, so
        # they meet without overlapping — the review band is simply empty, which
        # the collapse guard on the run is there to catch.
        reject = None

    accept_metrics = _threshold_row(accept, scores, y, n_positive) if accept is not None else None
    reject_metrics = None
    if reject is not None:
        chosen = scores < reject
        n = int(chosen.sum())
        tn = int((y[chosen] == 0).sum())
        reject_metrics = {
            "threshold": round(float(reject), 4),
            "n_predicted_negative": n,
            "tn": tn,
            "fn": n - tn,
            "precision": (tn / n) if n else None,
            "precision_wilson_lower": wilson_lower(tn, n) if n else None,
            "recall": (tn / n_negative) if n_negative else None,
            "recall_wilson_lower": wilson_lower(tn, n_negative) if n_negative else None,
        }
    return {
        "accept": None if accept is None else round(accept, 4),
        "reject": None if reject is None else round(reject, 4),
        "accept_metrics": accept_metrics,
        "reject_metrics": reject_metrics,
    }


def calibration_points(scores: np.ndarray, y: np.ndarray) -> list[dict]:
    """The reliability diagram: predicted against observed, in ten bins."""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=int)
    if not len(y):
        return []
    edges = np.linspace(0.0, 1.0, CALIBRATION_BINS + 1)
    which = np.clip(np.digitize(scores, edges[1:-1], right=False), 0, CALIBRATION_BINS - 1)
    points = []
    for index in range(CALIBRATION_BINS):
        chosen = which == index
        n = int(chosen.sum())
        if not n:
            continue
        observed = int(y[chosen].sum())
        lower, upper = wilson_bounds(observed, n)
        points.append({
            "bin": index,
            "score_from": round(float(edges[index]), 4),
            "score_to": round(float(edges[index + 1]), 4),
            "mean_predicted": round(float(scores[chosen].mean()), 6),
            "observed": round(observed / n, 6),
            "observed_wilson_lower": round(lower, 6),
            "observed_wilson_upper": round(upper, 6),
            "n": n,
        })
    return points


# ---------------------------------------------------------------------------
# Importance
# ---------------------------------------------------------------------------


def _shares(values: list[float]) -> list[float]:
    total = float(sum(abs(v) for v in values))
    return [round(abs(v) / total, 6) if total else 0.0 for v in values]


def _importance_rows(meta, values) -> list[dict]:
    shares = _shares(list(values))
    rows = [
        {"name": f.name, "label": f.label, "group": f.group,
         "value": round(float(v), 6), "share": s}
        for f, v, s in zip(meta, values, shares)
    ]
    return sorted(rows, key=lambda r: -r["value"])


def shap_importance(model, X: pd.DataFrame, meta, limit: int, seed: int):
    """Mean absolute SHAP value per feature, from LightGBM's own `pred_contrib`.

    Exact Tree-SHAP, not an approximation, and one pass over the trees. Capped at
    *limit* rows: the mean settles long before a hundred thousand rows, and the
    cost is linear.
    """
    if not len(X):
        return [], 0
    rows = X
    if len(X) > limit:
        rng = np.random.default_rng(seed)
        chosen = np.sort(rng.choice(len(X), size=limit, replace=False))
        rows = X.iloc[chosen]
    contributions = model.booster_.predict(rows.to_numpy(), pred_contrib=True)
    mean_abs = np.abs(np.asarray(contributions)[:, :-1]).mean(axis=0)
    return _importance_rows(meta, mean_abs), int(len(rows))


def group_ablation(X: pd.DataFrame, training: dataset.TrainingSet, meta,
                   settings: dict, seed: int, progress=None) -> dict:
    """Average precision out of fold with each feature group removed.

    The comparison is like for like: the full model is scored on the same fold
    split, with the same parameters, so the deltas answer "what does this group
    add" and not "what do different folds give". A negative delta means the group
    was helping.
    """
    groups = []
    for feature in meta:
        if feature.group not in groups:
            groups.append(feature.group)
    y = training.y
    if len(np.unique(y)) < 2 or not len(groups):
        return {"metric": "average_precision_out_of_fold", "full": None, "rows": []}

    fold = dataset.assign_folds(training.group, int(settings["ablation_folds"]))
    full_params = model_settings.lgbm_params(settings, [f.monotone for f in meta], seed)
    full_oof = _out_of_fold(X, y, training.weight, fold, full_params,
                            feature_lib.categorical_names(meta))
    scored = ~np.isnan(full_oof)
    full = _average_precision(y[scored], full_oof[scored])

    rows = []
    for index, group in enumerate(groups):
        if progress:
            progress(f"Ablating group {index + 1} of {len(groups)}")
        kept = [f for f in meta if f.group != group]
        if not kept:
            continue
        params = model_settings.lgbm_params(settings, [f.monotone for f in kept], seed)
        oof = _out_of_fold(X[[f.name for f in kept]], y, training.weight, fold,
                           params, feature_lib.categorical_names(kept))
        scored = ~np.isnan(oof)
        value = _average_precision(y[scored], oof[scored])
        rows.append({
            "group": group,
            "label": feature_lib.GROUP_LABELS.get(group, group.title()),
            "n_features": sum(1 for f in meta if f.group == group),
            "value": None if value is None else round(value, 6),
            "delta": None if (value is None or full is None) else round(value - full, 6),
        })
    rows.sort(key=lambda r: (r["delta"] is None, r["delta"]))
    return {
        "metric": "average_precision_out_of_fold",
        "n_folds": int(settings["ablation_folds"]),
        "full": None if full is None else round(full, 6),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------


def _read_run(run_dir: Path) -> dict:
    pairs_path = run_dir / "pairs.parquet"
    units_path = run_dir / "units.parquet"
    if not pairs_path.exists() or not units_path.exists():
        raise TrainingError("Run has no scored pairs yet")
    events = None
    events_path = run_dir / "events.parquet"
    members_path = run_dir / "unit_members.parquet"
    members = pd.read_parquet(members_path) if members_path.exists() \
        else pd.DataFrame({"unit_id": [], "record_id": []})
    if events_path.exists() and len(members):
        events = pd.read_parquet(events_path)
        events["record_id"] = events["record_id"].astype(str)
        joined = members.copy()
        joined["record_id"] = joined["record_id"].astype(str)
        events = events.merge(joined[["record_id", "unit_id"]], on="record_id",
                              how="inner")
    linkage = {}
    settings_path = run_dir / "config" / "linkage_settings.json"
    if settings_path.exists():
        try:
            linkage = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError:
            linkage = {}
    return {
        "pairs": pd.read_parquet(pairs_path),
        "units": pd.read_parquet(units_path),
        "events": events,
        "members": members,
        "linkage_settings": linkage,
    }


def train(run_dir, db_path: str | None, track: str, seed: int | None = None,
          note: str | None = None, progress=None, profile=None) -> dict:
    """Train, grade and store one version of *track*'s model. Returns its summary."""
    from app.services import pair_labels

    def step(key: str, message: str | None = None):
        if progress:
            label, percent = STEP_LABELS[key]
            progress(key, label, percent, message)

    started = time.perf_counter()
    run_dir = Path(run_dir)
    step("labels")
    run = _read_run(run_dir)
    settings = model_settings.get(run["linkage_settings"])
    seed = int(settings["seed"] if seed is None else seed)

    pairs = run["pairs"]
    if "track" in pairs.columns:
        pairs = pairs[pairs["track"] == track]
    pairs = pairs.reset_index(drop=True)
    if not len(pairs):
        raise TrainingError(f"This run scored no {track} pairs")

    labels = pair_labels.labels_frame(db_path) if db_path else pd.DataFrame()
    training = dataset.build(pairs, labels, run["members"], track, settings)
    if not training.n_rows:
        raise TrainingError(
            f"Nothing to train on: this run has no labels and no imported entity "
            f"ids for the {track} track"
        )
    if len(np.unique(training.y)) < 2:
        raise TrainingError(
            "Every training row has the same verdict, so there is nothing for a "
            "classifier to separate. Label some pairs FALSE, or run on a config "
            "whose imported ids disagree somewhere."
        )

    step("features")
    feature_started = time.perf_counter()
    loaded_references = reference_lib.load(profile)
    X_all, meta = feature_lib.build(
        pairs, run["units"], track, events=run["events"],
        references=loaded_references, profile=profile,
    )
    feature_secs = time.perf_counter() - feature_started

    X = X_all.iloc[training.index].reset_index(drop=True)
    monotone = [f.monotone for f in meta]
    categorical = feature_lib.categorical_names(meta)
    params = model_settings.lgbm_params(settings, monotone, seed)

    step("folds", f"{training.n_groups:,} groups over {int(settings['n_folds'])} folds")
    step("fit", f"{training.n_rows:,} rows, {len(meta)} features")
    fit_started = time.perf_counter()
    model = _fit(X, training.y, training.weight, params, categorical)
    out_of_fold = _out_of_fold(X, training.y, training.weight, training.fold,
                               params, categorical)
    fit_secs = time.perf_counter() - fit_started

    # --- calibration -------------------------------------------------------
    step("calibrate")
    cold_start = training.n_human < int(settings["min_human_labels"])
    scored = ~np.isnan(out_of_fold)
    human = training.is_human & scored
    fit_on = "human" if (not cold_start and human.sum() >= 2
                         and len(np.unique(training.y[human])) == 2) else "imported"
    calibration_rows = human if fit_on == "human" else scored
    calibration = {"method": "identity", "fitted_on": None, "n": 0,
                   "x_grid": [0.0, 1.0], "y_grid": [0.0, 1.0]}
    if calibration_rows.sum() >= 2 and len(np.unique(training.y[calibration_rows])) == 2:
        grid, curve = _platt(out_of_fold[calibration_rows], training.y[calibration_rows])
        calibration = {
            "method": "platt", "fitted_on": fit_on,
            "n": int(calibration_rows.sum()),
            "x_grid": [round(float(v), 6) for v in grid],
            "y_grid": [round(float(v), 6) for v in curve],
        }

    # --- grading -----------------------------------------------------------
    step("grade")
    test_scores = np.array([])
    if len(training.test_index):
        test_scores = store.apply_calibration(
            model.predict_proba(X_all.iloc[training.test_index])[:, 1], calibration
        )
    has_test = len(training.test_y) >= int(settings["min_test_labels"]) \
        and len(np.unique(training.test_y)) == 2
    graded = bool(not cold_start and has_test)

    oof_calibrated = store.apply_calibration(out_of_fold[scored], calibration)
    if has_test:
        auc_source, auc_y, auc_scores = "held_out", training.test_y, test_scores
    else:
        auc_source, auc_y, auc_scores = "out_of_fold", training.y[scored], out_of_fold[scored]

    thresholds = {
        "available": False,
        "reason": _no_test_reason(training, settings),
        "target_precision": float(settings["target_precision"]),
        "accept": None, "reject": None,
        "accept_metrics": None, "reject_metrics": None,
        "n_test": int(len(training.test_y)),
        "n_test_positive": int((training.test_y == 1).sum()) if len(training.test_y) else 0,
        "n_test_negative": int((training.test_y == 0).sum()) if len(training.test_y) else 0,
        "grid": [],
    }
    if graded:
        lines = derive_thresholds(test_scores, training.test_y,
                                  float(settings["target_precision"]))
        thresholds.update({
            "available": True, "reason": None,
            "grid": threshold_grid(test_scores, training.test_y),
            **lines,
        })

    # --- importance --------------------------------------------------------
    step("importance")
    importance_started = time.perf_counter()
    gain = _importance_rows(meta, model.booster_.feature_importance("gain"))
    shap_rows, shap_n = shap_importance(model, X, meta,
                                        int(settings["shap_max_rows"]), seed)
    ablation = group_ablation(
        X, training, meta, settings, seed,
        progress=(lambda message: step("importance", message)) if progress else None,
    )
    importance_secs = time.perf_counter() - importance_started

    # --- the report --------------------------------------------------------
    reference_status = reference_lib.status(profile, loaded_references)
    warnings = _warnings(training, settings, cold_start, graded, has_test,
                         reference_status, X, meta, auc_source)
    brier = _brier(training.y[scored], out_of_fold[scored], oof_calibrated)

    report = {
        "track": track,
        "trained_at": None,
        "seed": seed,
        "run_id": run_dir.name,
        "config_version": run["linkage_settings"].get("config_version"),
        "graded": graded,
        "cold_start": cold_start,
        "known_limit": KNOWN_LIMIT,
        "labels": {
            "total_rows": training.n_rows,
            "positives": int((training.y == 1).sum()),
            "negatives": int((training.y == 0).sum()),
            "human_total": training.n_human,
            "by_source": training.counts,
            "weights": {
                "human": float(settings["human_weight"]),
                "decision": float(settings["decision_weight"]),
                "import_agree": float(settings["import_agree_weight"]),
                "import_disagree": float(settings["import_disagree_weight"]),
            },
            "sampling": training.sampling,
        },
        "folds": {
            "n_folds": int(settings["n_folds"]),
            "grouping": "connected components over units",
            "n_groups": training.n_groups,
            "rows_per_fold": [int(n) for n in np.bincount(
                training.fold, minlength=int(settings["n_folds"]))],
        },
        "auc": {"value": _round(_auc(auc_y, auc_scores)), "source": auc_source,
                "n": int(len(auc_y))},
        "average_precision": {"value": _round(_average_precision(auc_y, auc_scores)),
                              "source": auc_source, "n": int(len(auc_y))},
        "brier": brier,
        "calibration": {
            "method": calibration["method"],
            "fitted_on": calibration["fitted_on"],
            "n": calibration["n"],
            "curve": [{"raw": round(float(x), 4), "calibrated": round(float(y), 6)}
                      for x, y in zip(calibration["x_grid"], calibration["y_grid"])],
            "points": calibration_points(oof_calibrated, training.y[scored]),
        },
        "thresholds": thresholds,
        "importance": {
            "gain": gain,
            "shap": shap_rows,
            "shap_sample_rows": shap_n,
            "ablation": ablation,
        },
        "references": reference_status,
        "warnings": warnings,
        "timings": {
            "features_secs": round(feature_secs, 2),
            "train_secs": round(fit_secs, 2),
            "ablation_secs": round(importance_secs, 2),
            "total_secs": round(time.perf_counter() - started, 2),
        },
        "features": [f.as_dict() for f in meta],
    }

    step("save")
    meta_payload = {
        "track": track,
        "graded": graded,
        "cold_start": cold_start,
        "seed": seed,
        "run_id": run_dir.name,
        "config_version": report["config_version"],
        "note": note,
        "n_features": len(meta),
        "n_train_rows": training.n_rows,
        "n_human_labels": training.n_human,
        "auc": report["auc"]["value"],
        "average_precision": report["average_precision"]["value"],
        "accept": thresholds["accept"],
        "reject": thresholds["reject"],
        "settings": settings,
    }
    version = store.save(track, model.booster_, calibration,
                         [f.as_dict() for f in meta], report, meta_payload)
    logger.info("Trained %s model v%s: %s rows, auc=%s", track, version,
                training.n_rows, report["auc"]["value"])
    return store.summary(track, version)


def _round(value, places: int = 6):
    return None if value is None else round(float(value), places)


def _brier(y, raw, calibrated) -> dict:
    from sklearn.metrics import brier_score_loss

    y = np.asarray(y, dtype=int)
    if not len(y) or len(np.unique(y)) < 2:
        return {"raw": None, "calibrated": None}
    return {
        "raw": round(float(brier_score_loss(y, np.asarray(raw, dtype=float))), 6),
        "calibrated": round(
            float(brier_score_loss(y, np.asarray(calibrated, dtype=float))), 6),
    }


def _no_test_reason(training: dataset.TrainingSet, settings: dict) -> str:
    n = len(training.test_y)
    if n == 0:
        return "no frozen test set (no human labels with held_out = 1)"
    if len(np.unique(training.test_y)) < 2:
        return "the frozen test set is all one verdict, so precision cannot be measured"
    if n < int(settings["min_test_labels"]):
        return (f"the frozen test set has {n} labels and {int(settings['min_test_labels'])} "
                "are needed before a threshold means anything")
    if training.n_human < int(settings["min_human_labels"]):
        return (f"cold start: {training.n_human} human labels, and "
                f"{int(settings['min_human_labels'])} are needed")
    return "no threshold could be set"


def _warnings(training, settings, cold_start, graded, has_test, reference_status,
              X, meta, auc_source: str) -> list[dict]:
    out = []
    if auc_source != "held_out" and training.n_human == 0:
        out.append({
            "code": "metrics_on_imported_labels",
            "message": (
                "AUC and average precision here are measured against the imported "
                "labels, and those labels were made mostly on the name. A high number "
                "says the model reproduces the earlier name-based work, not that it "
                "decides pairs correctly. Only human labels can show that."
            ),
        })
    if cold_start:
        out.append({
            "code": "cold_start",
            "message": (
                f"Trained on imported labels only — {training.n_human} human labels, "
                f"and {int(settings['min_human_labels'])} are needed. The model "
                "re-orders the review queue and cannot decide a pair."
            ),
        })
    if not has_test:
        out.append({
            "code": "no_test_set",
            "message": ("No frozen test set, so no accept or reject line was set and "
                        "every pair the model scores goes to review."),
        })
    elif not graded:
        out.append({
            "code": "test_set_single_class",
            "message": "The frozen test set cannot grade this model; nothing was set.",
        })
    folds = np.bincount(training.fold, minlength=int(settings["n_folds"]))
    if len(folds) > 1 and folds.sum() and folds.max() > 2 * folds.sum() / len(folds):
        out.append({
            "code": "uneven_folds",
            "message": (
                f"One fold holds {folds.max():,} of {folds.sum():,} rows. Folds split on "
                "connected components of units, and one component is very large — a "
                "chain of pairs that all share a unit. The out-of-fold numbers are "
                "still honest, but they rest on fewer independent splits than four."
            ),
        })
    negatives = int((training.y == 0).sum())
    if 0 < negatives < 20:
        out.append({
            "code": "few_negatives",
            "message": (f"Only {negatives} training rows say 'not the same'. The model "
                        "has barely seen a non-match and will be over-confident."),
        })
    for reference in reference_status:
        if not reference["present"]:
            out.append({
                "code": "missing_reference",
                "message": (f"The {reference['label']} table is not built, so "
                            f"{len(reference['affects'])} feature(s) are blank: "
                            f"{', '.join(reference['affects'])}."),
            })
    blank = [f.name for f in meta if f.name in X.columns and not X[f.name].notna().any()]
    if blank:
        out.append({
            "code": "feature_all_null",
            "message": (f"{len(blank)} feature(s) had no value on any training row: "
                        f"{', '.join(blank)}."),
        })
    return out

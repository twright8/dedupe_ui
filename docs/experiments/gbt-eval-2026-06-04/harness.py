"""GBT-vs-Splink evaluation harness.

Mirrors the production GBT EXACTLY for the classifier (same LightGBM params from
gbt_train._LGBM_PARAMS, same cold-start proxy from gbt_proxy.build_proxy_frame,
same 7 features from gbt_features.build_features) so the trained model == the one
the app ships. The ONE deviation is calibration: the app fits isotonic on the
held-out eval labels (in-sample -> optimistic); we fit it OUT-OF-FOLD on the
training labels so the gold metrics are honest.

Everything is evaluated on a frozen Sonnet-labelled GOLD set the model never sees
(neither classifier nor calibration). De-clumping/collapse is measured on the full
1081-pair candidate universe (the operational distribution).

Deterministic: proxy seed=42, LGBM random_state=42, fixed sampling seeds.
"""
from __future__ import annotations
import sys, json
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats as sps

sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/backend")
from app.pipeline.gbt_features import FEATURE_COLS, build_features
from app.pipeline.gbt_proxy import build_proxy_frame
from app.pipeline.gbt_train import _LGBM_PARAMS

import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss

R = Path("/home/tomwright/gbt_eval_data/runs/run_2026_06_04a")
OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
HIGH, REVIEW = 0.70, 0.40          # run thresholds (from linkage_settings.json)

OCOD = pd.read_parquet(R / "ocod_phase2.parquet")
ROE = pd.read_parquet(R / "roe_phase2.parquet")
PROXY_X, PROXY_Y = build_proxy_frame(str(R), seed=42)


def _pairs(df):
    return df.rename(columns={"ocod_uid": "unique_id_l", "roe_uid": "unique_id_r",
                              "splink_prob": "match_probability"})

def feats(df):
    return build_features(_pairs(df), OCOD, ROE, splink_prob_col="match_probability")

def fit_clf(TX, Ty):
    """Fit LightGBM on proxy + training labels (production-identical)."""
    parts_X = [d for d in (PROXY_X, TX) if len(d)]
    parts_y = [y for y in (PROXY_Y, Ty) if len(y)]
    X = pd.concat(parts_X, ignore_index=True)[FEATURE_COLS]
    y = np.concatenate(parts_y)
    # n_jobs=1: identical model, ~130x faster on this tiny data (thread-spawn overhead
    # dominates with the default -1 on a 24-core box), and fully deterministic.
    mono = [-1 if c == "unit_mismatch" else 0 for c in FEATURE_COLS]
    clf = lgb.LGBMClassifier(**_LGBM_PARAMS, n_jobs=1, monotone_constraints=mono)
    clf.fit(X, y)
    return clf

def oof_iso(TX, Ty, k=4):
    """Honest isotonic: fit on out-of-fold predictions of the training labels."""
    if len(Ty) < 12 or len(np.unique(Ty)) < 2:
        return None  # identity
    from sklearn.model_selection import StratifiedKFold
    kk = min(k, int(np.bincount(Ty).min()))
    if kk < 2:
        return None
    skf = StratifiedKFold(n_splits=kk, shuffle=True, random_state=0)
    oof = np.zeros(len(Ty))
    TXr = TX.reset_index(drop=True)
    for tr, te in skf.split(TXr, Ty):
        c = fit_clf(TXr.iloc[tr], Ty[tr])
        oof[te] = c.predict_proba(TXr.iloc[te][FEATURE_COLS])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(oof, Ty)
    return iso

def score(clf, iso, X):
    raw = clf.predict_proba(X[FEATURE_COLS])[:, 1]
    cal = raw if iso is None else iso.predict(raw)
    return raw, np.asarray(cal, dtype=float)


# ---------------- metrics ----------------
def ece(y, p, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, bins) - 1, 0, n_bins - 1)
    e = 0.0
    for b in range(n_bins):
        m = idx == b
        if m.sum():
            e += m.mean() * abs(y[m].mean() - p[m].mean())
    return float(e)

def reliability(y, p, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, bins) - 1, 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        if m.sum():
            rows.append({"bin_lo": bins[b], "bin_hi": bins[b+1], "n": int(m.sum()),
                         "mean_pred": float(p[m].mean()), "obs_rate": float(y[m].mean())})
    return rows

def pr_at(y, p, thr):
    pred = p >= thr
    tp = int((pred & (y == 1)).sum()); fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    return prec, rec, int(pred.sum())

def monotonic_truerate(y, p, n_bins=10):
    """Observed TRUE-rate per score bin; Spearman(score_bin, rate) + is_monotone."""
    q = pd.qcut(pd.Series(p).rank(method="first"), min(n_bins, len(np.unique(p))),
                labels=False, duplicates="drop")
    g = pd.DataFrame({"y": y, "p": p, "q": q}).groupby("q")
    rates = g.y.mean().values
    mids = g.p.mean().values
    if len(rates) < 3:
        return float("nan"), True
    rho = sps.spearmanr(mids, rates).correlation
    is_mono = bool(np.all(np.diff(rates) >= -1e-9))
    return float(rho), is_mono

def bimodality(p):
    """Sarle's bimodality coefficient + extreme/mid mass fractions."""
    p = np.asarray(p, float)
    n = len(p)
    if n < 4 or np.allclose(p, p[0]):
        return dict(bc=float("nan"), frac_extreme=float("nan"), frac_mid=float("nan"))
    g = sps.skew(p); k = sps.kurtosis(p, fisher=True)  # excess kurtosis
    bc = (g**2 + 1) / (k + 3 * (n-1)**2 / ((n-2)*(n-3)))
    return dict(bc=float(bc),
                frac_extreme=float(np.mean((p < 0.05) | (p > 0.95))),
                frac_mid=float(np.mean((p >= REVIEW) & (p < HIGH))))

def eval_block(label, y, splink, raw, cal, universe_cal):
    """All metrics for one config. `universe_cal` = calibrated score over all 1081 pairs."""
    out = {"config": label, "n_gold": int(len(y)), "pos_rate": float(y.mean())}
    # ranking
    out["auc_splink"] = float(roc_auc_score(y, splink))
    out["auc_gbt"] = float(roc_auc_score(y, cal))
    out["ap_splink"] = float(average_precision_score(y, splink))
    out["ap_gbt"] = float(average_precision_score(y, cal))
    # calibration / accuracy (probability quality)
    out["brier_splink"] = float(brier_score_loss(y, splink))
    out["brier_gbt_raw"] = float(brier_score_loss(y, raw))
    out["brier_gbt_cal"] = float(brier_score_loss(y, cal))
    out["ece_splink"] = ece(y, splink); out["ece_gbt_raw"] = ece(y, raw); out["ece_gbt_cal"] = ece(y, cal)
    # precision/recall of the auto-accept decision (score>=HIGH)
    ps, rs, ns = pr_at(y, splink, HIGH); pg, rg, ng = pr_at(y, cal, HIGH)
    out.update(prec_splink_hi=ps, rec_splink_hi=rs, naccept_splink=ns,
               prec_gbt_hi=pg, rec_gbt_hi=rg, naccept_gbt=ng)
    # monotonic gradient
    out["spearman_gbt"], out["mono_gbt"] = monotonic_truerate(y, cal)
    # de-clumping (gold + universe)
    out["distinct_splink_gold"] = int(pd.Series(splink).round(4).nunique())
    out["distinct_gbt_raw_gold"] = int(pd.Series(raw).round(4).nunique())
    out["distinct_gbt_cal_gold"] = int(pd.Series(cal).round(4).nunique())
    out["distinct_gbt_cal_univ"] = int(pd.Series(universe_cal).round(4).nunique())
    bm = bimodality(universe_cal)
    out["bc_univ"] = bm["bc"]; out["frac_extreme_univ"] = bm["frac_extreme"]
    out["reviewband_frac_univ"] = bm["frac_mid"]
    out["reviewband_n_univ"] = int(np.sum((universe_cal >= REVIEW) & (universe_cal < HIGH)))
    # collapse flag: review band on universe basically emptied vs Splink's 645
    out["collapse"] = bool(out["reviewband_n_univ"] <= 10)
    return out


def active_order(pool, seed_n=15, batch=15):
    """Pool-based active learning trajectory over the usable training pool.
    Returns an ordering (list of pool index positions). Seed = first seed_n of a
    fixed random shuffle; then iteratively add the most uncertain + Splink-disagreeing
    unlabelled pairs, retraining each batch (LGBM is seeded -> deterministic)."""
    poolX = feats(pool).reset_index(drop=True)
    pooly = pool["y"].to_numpy()
    splink = pool["splink_prob"].to_numpy()
    rng = np.random.default_rng(0)
    shuffled = rng.permutation(len(pool))
    chosen = list(shuffled[:seed_n])
    remaining = [i for i in shuffled if i not in set(chosen)]
    while remaining:
        clf = fit_clf(poolX.iloc[chosen], pooly[chosen])
        raw = clf.predict_proba(poolX.iloc[remaining][FEATURE_COLS])[:, 1]
        uncertainty = -np.abs(raw - 0.5)
        disagree = np.abs(raw - splink[remaining])
        prio = uncertainty + disagree
        order = np.argsort(-prio, kind="stable")
        take = [remaining[j] for j in order[:batch]]
        chosen += take
        tset = set(take)
        remaining = [i for i in remaining if i not in tset]
    return chosen


def main():
    gold = pd.read_csv(OUT / "gold_labels.csv")
    train = pd.read_csv(OUT / "train_labels.csv")
    universe = pd.read_csv(OUT / "universe.csv")

    # usable = decided labels only (UNCERTAIN carries no training/eval signal)
    gold = gold[gold.verdict.isin(["TRUE", "FALSE"])].copy()
    gold["y"] = (gold.verdict == "TRUE").astype(int)
    train = train[train.verdict.isin(["TRUE", "FALSE"])].copy()
    train["y"] = (train.verdict == "TRUE").astype(int)

    goldX = feats(gold); goldy = gold.y.to_numpy(); gold_splink = gold.splink_prob.to_numpy()
    univX = feats(universe.assign(y=0)); univ_splink = universe.splink_prob.to_numpy()

    # nested random order (clean learning curve) + active trajectory
    rng = np.random.default_rng(0)
    rand_order = list(rng.permutation(len(train)))
    act_order = active_order(train)
    trainX_all = feats(train).reset_index(drop=True)
    trainy_all = train.y.to_numpy()

    budgets = [0, 25, 50, 100, 150, 200, 300, 400, len(train)]
    rows, gold_scores, rel_rows = [], [], []

    # ---- baseline: raw Splink ----
    base = {"config": "splink", "selection": "baseline", "n_labels": 0, "n_pos": None}
    b = eval_block("splink", goldy, gold_splink, gold_splink, gold_splink, univ_splink)
    base.update(b); rows.append(base)
    for r in reliability(goldy, gold_splink):
        rel_rows.append({"config": "splink", "score": "splink", **r})

    # ---- sweep ----
    for sel, order in [("random", rand_order), ("active", act_order)]:
        for N in budgets:
            idx = order[:N]
            TX = trainX_all.iloc[idx]; Ty = trainy_all[idx]
            npos = int(Ty.sum()) if N else 0
            clf = fit_clf(TX, Ty)
            iso = oof_iso(TX, Ty)
            graw, gcal = score(clf, iso, goldX)
            _, ucal = score(clf, iso, univX)
            label = f"{sel}_N{N}"
            blk = eval_block(label, goldy, gold_splink, graw, gcal, ucal)
            blk.update(selection=sel, n_labels=N, n_pos=npos,
                       calibrated=("oof_iso" if iso is not None else "identity"))
            rows.append(blk)
            # save gold scores for plotting (a few key configs)
            if sel == "random" or N in (0, 50, 200, len(train)):
                for pid, yy, sp, rw, cl in zip(gold.pair_id, goldy, gold_splink, graw, gcal):
                    gold_scores.append({"config": label, "selection": sel, "n_labels": N,
                                        "pair_id": int(pid), "y": int(yy),
                                        "splink": float(sp), "gbt_raw": float(rw), "gbt_cal": float(cl)})
                for r in reliability(goldy, gcal):
                    rel_rows.append({"config": label, "score": "gbt_cal", **r})

    pd.DataFrame(rows).to_csv(OUT / "sweep_metrics.csv", index=False)
    pd.DataFrame(gold_scores).to_csv(OUT / "gold_scores.csv", index=False)
    pd.DataFrame(rel_rows).to_csv(OUT / "reliability.csv", index=False)
    print("wrote sweep_metrics.csv (%d rows), gold_scores.csv, reliability.csv" % len(rows))

    # console summary
    df = pd.DataFrame(rows)
    cols = ["config", "selection", "n_labels", "n_pos", "auc_splink", "auc_gbt",
            "brier_splink", "brier_gbt_cal", "ece_splink", "ece_gbt_cal",
            "prec_gbt_hi", "rec_gbt_hi", "distinct_gbt_cal_univ",
            "reviewband_n_univ", "bc_univ", "collapse"]
    with pd.option_context("display.width", 200, "display.max_columns", 50):
        print(df[cols].round(3).to_string(index=False))


if __name__ == "__main__":
    main()

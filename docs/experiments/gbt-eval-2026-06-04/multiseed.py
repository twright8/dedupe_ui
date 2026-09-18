"""Multi-seed random-budget sweep -> variance bands + per-N collapse frequency.

Single-seed runs showed volatile collapse (N=50 empties the review band while
N=25/100 do not). This repeats the random-selection learning curve over many
seeds so the danger zone is a *frequency*, not an anecdote.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H
from sklearn.metrics import roc_auc_score, brier_score_loss

OUT = H.OUT
SEEDS = list(range(12))
BUDGETS = [25, 50, 75, 100, 150, 200, 300, 400]

gold = pd.read_csv(OUT / "gold_labels.csv")
gold = gold[gold.verdict.isin(["TRUE", "FALSE"])].copy(); gold["y"] = (gold.verdict == "TRUE").astype(int)
train = pd.read_csv(OUT / "train_labels.csv")
train = train[train.verdict.isin(["TRUE", "FALSE"])].copy(); train["y"] = (train.verdict == "TRUE").astype(int)
universe = pd.read_csv(OUT / "universe.csv")

goldX = H.feats(gold); goldy = gold.y.to_numpy(); gsplink = gold.splink_prob.to_numpy()
trainX = H.feats(train).reset_index(drop=True); trainy = train.y.to_numpy()
univX = H.feats(universe.assign(y=0))

rows = []
for s in SEEDS:
    order = list(np.random.default_rng(s).permutation(len(train)))
    for N in BUDGETS:
        idx = order[:N]
        clf = H.fit_clf(trainX.iloc[idx], trainy[idx])
        iso = H.oof_iso(trainX.iloc[idx], trainy[idx])
        graw, gcal = H.score(clf, iso, goldX)
        _, ucal = H.score(clf, iso, univX)
        rb = int(np.sum((ucal >= H.REVIEW) & (ucal < H.HIGH)))
        rows.append(dict(seed=s, N=N, n_pos=int(trainy[idx].sum()),
                         auc=float(roc_auc_score(goldy, gcal)),
                         brier=float(brier_score_loss(goldy, gcal)),
                         ece=H.ece(goldy, gcal),
                         reviewband=rb,
                         frac_extreme=float(np.mean((ucal < 0.05) | (ucal > 0.95))),
                         collapse=bool(rb <= 10)))
df = pd.DataFrame(rows)
df.to_csv(OUT / "multiseed_metrics.csv", index=False)

agg = df.groupby("N").agg(
    auc_mean=("auc", "mean"), auc_std=("auc", "std"),
    brier_mean=("brier", "mean"), brier_std=("brier", "std"),
    ece_mean=("ece", "mean"),
    reviewband_med=("reviewband", "median"),
    reviewband_min=("reviewband", "min"), reviewband_max=("reviewband", "max"),
    collapse_rate=("collapse", "mean"),
).reset_index()
agg.to_csv(OUT / "multiseed_agg.csv", index=False)
print(f"{len(SEEDS)} seeds x {len(BUDGETS)} budgets")
with pd.option_context("display.width", 200):
    print(agg.round(3).to_string(index=False))
print("\nSplink baseline: AUC=%.3f Brier=%.3f ECE=%.3f" % (
    roc_auc_score(goldy, gsplink), brier_score_loss(goldy, gsplink), H.ece(goldy, gsplink)))

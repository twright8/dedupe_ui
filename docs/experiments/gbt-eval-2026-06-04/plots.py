"""Generate the metric-vs-N curves and distribution figures for the report."""
from __future__ import annotations
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pathlib import Path

OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
RUN = Path("/home/tomwright/gbt_eval_data/runs/run_2026_06_04a")
HIGH, REVIEW = 0.70, 0.40

agg = pd.read_csv(OUT / "multiseed_agg.csv")
sweep = pd.read_csv(OUT / "sweep_metrics.csv")
act = sweep[sweep.selection == "active"].sort_values("n_labels")
base = sweep[sweep.config == "splink"].iloc[0]
SPL = dict(auc=base.auc_splink, brier=base.brier_splink, ece=base.ece_splink)

# ---- Fig 1: AUC / Brier / ECE vs N ----
fig, ax = plt.subplots(1, 3, figsize=(15, 4.2))
for a, key, amean, astd, ttl in [
    (ax[0], "auc", "auc_mean", "auc_std", "AUC (ranking) — higher better"),
    (ax[1], "brier", "brier_mean", "brier_std", "Brier (prob. accuracy) — lower better"),
    (ax[2], "ece", "ece_mean", None, "ECE (calibration) — lower better")]:
    a.fill_between(agg.N, agg[amean] - (agg[astd] if astd else 0),
                   agg[amean] + (agg[astd] if astd else 0), alpha=0.18, color="C0",
                   label="GBT random ±1σ" if astd else None)
    a.plot(agg.N, agg[amean], "o-", color="C0", label="GBT random (mean of 12 seeds)")
    acol = {"auc": "auc_gbt", "brier": "brier_gbt_cal", "ece": "ece_gbt_cal"}[key]
    a.plot(act.n_labels, act[acol], "s--", color="C1", label="GBT active-learning")
    a.axhline(SPL[key], color="k", ls=":", label=f"raw Splink = {SPL[key]:.3f}")
    a.set_xlabel("training labels  N"); a.set_title(ttl); a.grid(alpha=0.3)
    a.legend(fontsize=8)
ax[0].axvspan(0, 100, color="red", alpha=0.06)
ax[0].text(12, ax[0].get_ylim()[0] + 0.01, "danger\nzone", color="red", fontsize=8)
fig.suptitle("GBT vs raw Splink on the frozen 227-pair gold set, vs label budget N", fontweight="bold")
fig.tight_layout(); fig.savefig(OUT / "fig_metrics_vs_N.png", dpi=110); plt.close(fig)

# ---- Fig 2: collapse / review-band vs N ----
fig, ax = plt.subplots(figsize=(8, 4.6))
ax.fill_between(agg.N, agg.reviewband_min, agg.reviewband_max, alpha=0.15, color="C0",
                label="review-band size range (12 seeds)")
ax.plot(agg.N, agg.reviewband_med, "o-", color="C0", label="review-band size (median)")
ax.axhline(645, color="k", ls=":", label="Splink review band = 645")
ax.set_xlabel("training labels  N"); ax.set_ylabel("pairs left in review band (of 1081)")
ax2 = ax.twinx()
ax2.bar(agg.N, agg.collapse_rate, width=12, alpha=0.3, color="red", label="collapse rate")
ax2.set_ylabel("collapse rate (review band ≤ 10)", color="red"); ax2.set_ylim(0, 1)
ax.set_title("Over-confidence collapse: the review band empties erratically below ~N=300")
ax.legend(loc="upper right", fontsize=8); ax.grid(alpha=0.3)
fig.tight_layout(); fig.savefig(OUT / "fig_collapse_vs_N.png", dpi=110); plt.close(fig)

# ---- Fig 3: reliability diagram + Splink-clump miscalibration ----
rel = pd.read_csv(OUT / "reliability.csv")
gold = pd.read_csv(OUT / "gold_labels.csv")
gd = gold[gold.verdict.isin(["TRUE", "FALSE"])].copy(); gd["y"] = (gd.verdict == "TRUE").astype(int)
fig, ax = plt.subplots(1, 2, figsize=(12, 4.6))
ax[0].plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
for cfg, sc, c, lab in [("splink", "splink", "k", "raw Splink"),
                        ("random_N200", "gbt_cal", "C1", "GBT (N=200)")]:
    r = rel[(rel.config == cfg) & (rel.score == sc)]
    ax[0].plot(r.mean_pred, r.obs_rate, "o-", color=c, label=lab)
ax[0].set_xlabel("predicted probability"); ax[0].set_ylabel("observed TRUE-rate")
ax[0].set_title("Reliability diagram (gold set)"); ax[0].legend(); ax[0].grid(alpha=0.3)
cl = gd.groupby("splink_clump").y.agg(["mean", "count"]).reset_index()
ax[1].bar(range(len(cl)), cl["mean"], color="C0")
ax[1].plot(range(len(cl)), cl["splink_clump"], "k^--", label="Splink 'probability'")
ax[1].set_xticks(range(len(cl))); ax[1].set_xticklabels([f"{v:.2f}\n(n={n})" for v, n in zip(cl.splink_clump, cl["count"])], fontsize=7)
ax[1].set_ylabel("actual TRUE-rate (bars)"); ax[1].set_title("Splink score vs reality: 0.42 ⇒ only 6% true")
ax[1].legend(fontsize=8); ax[1].grid(alpha=0.3)
fig.tight_layout(); fig.savefig(OUT / "fig_reliability.png", dpi=110); plt.close(fig)

# ---- Fig 4: de-clumping — score distributions over the 1081 universe ----
sc = pd.read_parquet(RUN / "linkage_scored.parquet")
fig, ax = plt.subplots(1, 3, figsize=(15, 4.2), sharey=True)
for a, col, ttl in [(ax[0], "match_probability", f"Splink (clumpy): {sc.match_probability.round(4).nunique()} distinct values"),
                    (ax[1], "gbt_score_raw", f"raw GBT (de-clumped): {sc.gbt_score_raw.round(4).nunique()} distinct"),
                    (ax[2], "gbt_score", f"calibrated GBT (re-clumped): {sc.gbt_score.round(4).nunique()} distinct")]:
    a.hist(sc[col], bins=50, color="C0", edgecolor="white")
    a.axvspan(REVIEW, HIGH, color="orange", alpha=0.18)
    a.set_xlabel("score"); a.set_title(ttl, fontsize=10); a.grid(alpha=0.3)
ax[0].set_ylabel("candidate pairs (of 1081)")
fig.suptitle("De-clumping: the RAW GBT spreads the score; isotonic calibration re-clumps it (orange = review band 0.4–0.7)", fontweight="bold")
fig.tight_layout(); fig.savefig(OUT / "fig_score_dist.png", dpi=110); plt.close(fig)

print("wrote: fig_metrics_vs_N.png, fig_collapse_vs_N.png, fig_reliability.png, fig_score_dist.png")

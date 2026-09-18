"""Label-noise (Sonnet vs Haiku on the same 499 train pairs) + manual-vs-quant
validation (does the score pick the gold candidate, esp. on ambiguous entities).
Also emits a 50-pair gold self-consistency batch for an independent Sonnet pass.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path

OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")

# ---------- 1. Sonnet vs Haiku label noise on identical train pairs ----------
s = pd.read_csv(OUT / "train_labels.csv")[["pair_id", "splink_clump", "n_cand", "verdict"]].rename(columns={"verdict": "sonnet"})
h = pd.read_csv(OUT / "train_labels_haiku.csv")[["pair_id", "verdict"]].rename(columns={"verdict": "haiku"})
m = s.merge(h, on="pair_id")
print("=== LABEL NOISE: Sonnet vs Haiku on the same 499 train pairs ===")
print("Sonnet:", dict(m.sonnet.value_counts()), "| Haiku:", dict(m.haiku.value_counts()))
both = m[(m.sonnet != "UNCERTAIN") & (m.haiku != "UNCERTAIN")]
agree = (both.sonnet == both.haiku).mean()
print(f"Agreement on pairs BOTH decided (n={len(both)}): {agree:.3f}")
# Cohen's kappa on the decided TRUE/FALSE
from collections import Counter
a = (both.sonnet == "TRUE").astype(int); b = (both.haiku == "TRUE").astype(int)
po = (a == b).mean(); pe = a.mean()*b.mean() + (1-a.mean())*(1-b.mean())
print(f"Cohen's kappa: {(po-pe)/(1-pe):.3f}")
print("Haiku UNCERTAIN that Sonnet decided:", int(((m.haiku=='UNCERTAIN')&(m.sonnet!='UNCERTAIN')).sum()),
      "of", int((m.haiku=='UNCERTAIN').sum()), "Haiku-UNCERTAIN")
print("\nConfusion (rows=Sonnet, cols=Haiku):")
print(pd.crosstab(m.sonnet, m.haiku))
print("\nDisagreement rate by Splink clump (both decided):")
both = both.assign(dis=(both.sonnet != both.haiku).astype(int))
print(both.groupby("splink_clump").dis.agg(["mean", "count"]).round(3).to_string())

# ---------- 2. manual-vs-quant: reconciliation accuracy on gold ----------
print("\n=== MANUAL vs QUANTITATIVE: does the score pick the gold candidate? ===")
gs = pd.read_csv(OUT / "gold_scores.csv")
g200 = gs[gs.config == "random_N200"].copy()         # GBT @ N=200
uni = pd.read_csv(OUT / "universe.csv")[["pair_id", "ocod_uid", "roe_company_number", "n_cand"]]
g200 = g200.merge(uni, on="pair_id")
# entities that HAVE a gold TRUE
def pick_acc(df, score_col):
    hit = tot = 0
    for uid, grp in df.groupby("ocod_uid"):
        if grp.y.sum() == 0:        # no true match -> reconciliation = "none"; skip pick test
            continue
        tot += 1
        gold_roe = grp.loc[grp.y == 1, "roe_company_number"].iloc[0]
        pick_roe = grp.loc[grp[score_col].idxmax(), "roe_company_number"]
        hit += int(pick_roe == gold_roe)
    return hit, tot
for col, name in [("splink", "Splink"), ("gbt_cal", "GBT")]:
    for amb, lab in [(g200[g200.n_cand >= 2], "ambiguous (>=2 cand)"), (g200, "all entities")]:
        h_, t_ = pick_acc(amb, col)
        print(f"{name:6s} top-candidate accuracy, {lab:20s}: {h_}/{t_} = {h_/t_:.3f}" if t_ else f"{name} {lab}: n/a")

# margin on ambiguous: gold-true score minus best rival, splink vs gbt
amb = g200[g200.n_cand >= 2]
mar = []
for uid, grp in amb.groupby("ocod_uid"):
    if grp.y.sum() == 0:
        continue
    for col in ["splink", "gbt_cal"]:
        t = grp.loc[grp.y == 1, col].iloc[0]; riv = grp.loc[grp.y == 0, col].max()
        mar.append({"col": col, "margin": t - (riv if pd.notna(riv) else 0)})
md = pd.DataFrame(mar).groupby("col").margin.agg(["mean", "median"]).round(3)
print("\nMargin (gold-true score - best rival) on ambiguous entities:")
print(md.to_string())

# ---------- 3. emit gold self-consistency batch (50 pairs) ----------
gold = pd.read_csv(OUT / "gold_pairs.csv")
sub = gold.groupby(pd.read_csv(OUT/"gold_labels.csv").set_index("pair_id").loc[gold.pair_id, "splink_clump"].values) \
          if False else gold.sample(n=50, random_state=7)
fields = ["pair_id","ocod_uid","ocod_name_raw","ocod_name_clean","jurisdiction_clean",
          "roe_name_raw","roe_name_clean","roe_company_number"]
sub[fields].to_csv(OUT / "batches" / "gold_selfconsistency_in.csv", index=False)
print(f"\nwrote gold self-consistency batch: {len(sub)} pairs")

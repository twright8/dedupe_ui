"""Build the candidate-pair universe and a disjoint gold/train entity split.

Universe = every Splink candidate pair in linkage_scored.parquet (>= review floor).
We attach raw names + the ROE company number (the label key) and the per-OCOD
candidate count (ambiguity). Then we split OCOD *entities* (not pairs) disjointly
into a frozen GOLD eval set (Sonnet-labelled, held_out=1) enriched for ambiguous
entities, and a TRAINING pool (Haiku-labelled, held_out=0) that covers the review
band so the active-learning sampler has material. Splitting by entity prevents an
entity's sibling candidates leaking across the train/eval boundary.

Deterministic: numpy default_rng(SEED).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path

SEED = 42
R = Path("/home/tomwright/gbt_eval_data/runs/run_2026_06_04a")
OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
rng = np.random.default_rng(SEED)

scored = pd.read_parquet(R / "linkage_scored.parquet")
roe_p2 = pd.read_parquet(R / "roe_phase2.parquet")[
    ["unique_id", "roe_company_number", "roe_name_raw"]
].rename(columns={"unique_id": "roe_uid"})

# One raw OCOD name per (clean name, jurisdiction).
ocod_pre = pd.read_parquet(R / "ocod_preprocessed.parquet")[
    ["name_clean", "jurisdiction_clean", "ocod_name_raw", "ocod_jurisdiction_raw"]
].drop_duplicates(subset=["name_clean", "jurisdiction_clean"])

u = scored[[
    "unique_id_l", "unique_id_r", "name_clean_l", "name_clean_r",
    "jurisdiction_clean_l", "match_probability",
]].copy()
u = u.rename(columns={
    "unique_id_l": "ocod_uid", "unique_id_r": "roe_uid",
    "name_clean_l": "ocod_name_clean", "name_clean_r": "roe_name_clean",
    "jurisdiction_clean_l": "jurisdiction_clean", "match_probability": "splink_prob",
})
u = u.merge(roe_p2, on="roe_uid", how="left")
u = u.merge(
    ocod_pre.rename(columns={"name_clean": "ocod_name_clean"}),
    on=["ocod_name_clean", "jurisdiction_clean"], how="left",
)
u["ocod_name_raw"] = u["ocod_name_raw"].fillna(u["ocod_name_clean"])
u["roe_name_raw"] = u["roe_name_raw"].fillna(u["roe_name_clean"])

# Ambiguity = how many ROE candidates this OCOD entity has.
ncand = u.groupby("ocod_uid")["roe_uid"].transform("size")
u["n_cand"] = ncand
u["is_ambiguous"] = (u["n_cand"] >= 2).astype(int)
u["pair_id"] = range(len(u))

# Splink clump as a stratum label.
u["splink_clump"] = u["splink_prob"].round(4)

# ----- Entity-level split (disjoint) -----
ent = u.groupby("ocod_uid").agg(
    n_cand=("roe_uid", "size"),
    max_prob=("splink_prob", "max"),
).reset_index()
ent["is_ambiguous"] = (ent["n_cand"] >= 2).astype(int)

# Stratify single-candidate entities by Splink clump of their one candidate.
def clump_band(p):
    if p < 0.45:   return "c042"   # 0.4186
    if p < 0.55:   return "c052"   # 0.5171
    if p < 0.78:   return "c075"   # 0.7458
    if p < 0.90:   return "c081"   # 0.8070
    return "chigh"                 # >=0.97 tail
ent["band"] = ent["max_prob"].map(clump_band)

amb = ent[ent.is_ambiguous == 1]
sng = ent[ent.is_ambiguous == 0]

# Gold: ~55 ambiguous entities + ~90 single entities (stratified) -> ~200 pairs.
gold_amb = amb.sample(n=min(55, len(amb)), random_state=SEED)
gold_sng_parts = []
SNG_PER_BAND = {"c042": 22, "c052": 20, "c075": 16, "c081": 16, "chigh": 16}
for b, k in SNG_PER_BAND.items():
    pool = sng[sng.band == b]
    gold_sng_parts.append(pool.sample(n=min(k, len(pool)), random_state=SEED))
gold_sng = pd.concat(gold_sng_parts)
gold_ent = pd.concat([gold_amb, gold_sng])
gold_uids = set(gold_ent.ocod_uid)

# Training pool: from remaining entities. Take ALL remaining ambiguous (review-band
# rich) + a stratified single sample to reach ~360 entities (~450 pairs).
rem_amb = amb[~amb.ocod_uid.isin(gold_uids)]
rem_sng = sng[~sng.ocod_uid.isin(gold_uids)]
train_sng_parts = []
TR_PER_BAND = {"c042": 95, "c052": 80, "c075": 45, "c081": 45, "chigh": 35}
for b, k in TR_PER_BAND.items():
    pool = rem_sng[rem_sng.band == b]
    train_sng_parts.append(pool.sample(n=min(k, len(pool)), random_state=SEED))
train_ent = pd.concat([rem_amb] + train_sng_parts)
train_uids = set(train_ent.ocod_uid)

assert not (gold_uids & train_uids), "gold/train entity overlap!"

gold = u[u.ocod_uid.isin(gold_uids)].copy().sort_values(["ocod_uid", "splink_prob"])
train = u[u.ocod_uid.isin(train_uids)].copy().sort_values(["ocod_uid", "splink_prob"])

u.to_csv(OUT / "universe.csv", index=False)
gold.to_csv(OUT / "gold_pairs.csv", index=False)
train.to_csv(OUT / "train_pairs.csv", index=False)

def summary(name, df):
    print(f"\n{name}: {len(df)} pairs, {df.ocod_uid.nunique()} entities, "
          f"{int((df.n_cand>=2).sum())} ambiguous-pairs")
    print("  splink clump:", dict(df.splink_clump.value_counts().sort_index()))

print(f"UNIVERSE: {len(u)} pairs, {u.ocod_uid.nunique()} entities")
summary("GOLD", gold)
summary("TRAIN", train)
print(f"\nGold entities: amb={len(gold_amb)} single={len(gold_sng)}")
print(f"Train entities: amb={len(rem_amb)} single={sum(len(p) for p in train_sng_parts)}")
print("disjoint OK:", not (gold_uids & train_uids))

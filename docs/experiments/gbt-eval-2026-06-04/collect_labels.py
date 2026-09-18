"""Collect + validate subagent label outputs; join to the pair universe.

Usage: collect_labels.py {gold|train}
Validates: every input pair_id is covered exactly once; verdicts in vocab;
one-TRUE-per-OCOD. Writes {gold|train}_labels.csv (pair_id + all pair fields +
verdict/confidence/reason).
"""
from __future__ import annotations
import sys, glob
import pandas as pd
from pathlib import Path

OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
kind = sys.argv[1]
suffix = sys.argv[2] if len(sys.argv) > 2 else "_out"      # _out (haiku) | _sonnet_out
outname = sys.argv[3] if len(sys.argv) > 3 else f"{kind}_labels.csv"
pairs = pd.read_csv(OUT / f"{kind}_pairs.csv")

outs = sorted(glob.glob(str(OUT / "batches" / f"{kind}_batch_*{suffix}.csv")))
if suffix == "_out":  # plain Haiku files only — exclude the _sonnet_out re-labels
    outs = [o for o in outs if "_sonnet_" not in o]
frames = []
for f in outs:
    d = pd.read_csv(f, dtype={"pair_id": int})
    d.columns = [c.strip().lower() for c in d.columns]
    d["verdict"] = d["verdict"].astype(str).str.strip().str.upper()
    frames.append(d[["pair_id", "verdict"] + [c for c in ("confidence", "reason") if c in d.columns]])
lab = pd.concat(frames, ignore_index=True)

# --- validation ---
errs = []
dup = lab[lab.pair_id.duplicated()].pair_id.tolist()
if dup: errs.append(f"duplicate pair_ids: {dup[:10]}")
missing = set(pairs.pair_id) - set(lab.pair_id)
extra = set(lab.pair_id) - set(pairs.pair_id)
if missing: errs.append(f"missing {len(missing)} pair_ids: {sorted(missing)[:10]}")
if extra: errs.append(f"extra {len(extra)} pair_ids: {sorted(extra)[:10]}")
badv = set(lab.verdict) - {"TRUE", "FALSE", "UNCERTAIN"}
if badv: errs.append(f"bad verdicts: {badv}")

m = pairs.merge(lab, on="pair_id", how="left")
# one-TRUE-per-OCOD
true_per = m[m.verdict == "TRUE"].groupby("ocod_uid").size()
viol = true_per[true_per > 1]
if len(viol):
    msg = f"{len(viol)} OCOD entities with >1 TRUE: {viol.index.tolist()[:10]}"
    (errs if kind == "gold" else None) and errs.append(msg)  # hard-fail for gold only
    print("WARN(one-TRUE-per-OCOD):", msg)

print(f"=== {kind}: {len(lab)} labels over {len(pairs)} pairs ===")
print("verdict counts:", dict(lab.verdict.value_counts()))
if "confidence" in lab.columns:
    print("confidence counts:", dict(lab.confidence.astype(str).str.lower().value_counts()))
print("TRUE rate (excl UNCERTAIN): %.3f" % ((lab.verdict=="TRUE").sum() / max(1,(lab.verdict!="UNCERTAIN").sum())))
# TRUE rate by splink clump (does manual label track splink?)
mm = m[m.verdict.isin(["TRUE","FALSE"])]
print("\nTRUE-rate by Splink clump:")
print(mm.groupby("splink_clump").verdict.apply(lambda s:(s=="TRUE").mean()).round(3).to_string())
print(mm.groupby("splink_clump").size().rename("n").to_string())

if errs:
    print("\n!!! VALIDATION ERRORS:")
    for e in errs: print("  -", e)
    sys.exit(1)
print("\nVALIDATION OK")
m.to_csv(OUT / outname, index=False)
print("wrote", OUT / outname)

"""Pack the gold/train pairs into per-subagent labelling batches.

Whole OCOD entities are kept together in one batch so a labeller can apply the
one-TRUE-per-OCOD reconciliation rule (pick the single best ROE, or none). The
Splink score is deliberately withheld from the labeller so the gold labels are
independent of the model under evaluation.
"""
from __future__ import annotations
import pandas as pd
from pathlib import Path

OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
BATCHES = OUT / "batches"
BATCHES.mkdir(exist_ok=True)

FIELDS = ["pair_id", "ocod_uid", "ocod_name_raw", "ocod_name_clean",
          "jurisdiction_clean", "roe_name_raw", "roe_name_clean", "roe_company_number"]

def pack(df: pd.DataFrame, prefix: str, target: int) -> list[dict]:
    """Greedily pack whole entities into batches of ~target pairs."""
    manifest = []
    groups = [g for _, g in df.groupby("ocod_uid", sort=False)]
    batch, idx = [], 0
    def flush(batch, idx):
        if not batch:
            return
        b = pd.concat(batch)[FIELDS]
        inp = BATCHES / f"{prefix}_batch_{idx:02d}_in.csv"
        b.to_csv(inp, index=False)
        manifest.append({"batch": f"{prefix}_{idx:02d}", "n_pairs": len(b),
                         "n_entities": b.ocod_uid.nunique(),
                         "input": str(inp),
                         "output": str(BATCHES / f"{prefix}_batch_{idx:02d}_out.csv")})
    for g in groups:
        if sum(len(x) for x in batch) + len(g) > target and batch:
            flush(batch, idx); idx += 1; batch = []
        batch.append(g)
    flush(batch, idx)
    return manifest

gold = pd.read_csv(OUT / "gold_pairs.csv")
train = pd.read_csv(OUT / "train_pairs.csv")

gm = pack(gold, "gold", 40)
tm = pack(train, "train", 55)

man = pd.DataFrame(gm + tm)
man.to_csv(OUT / "batch_manifest.csv", index=False)
print("GOLD batches:", len(gm), "| pairs:", sum(m["n_pairs"] for m in gm))
print("TRAIN batches:", len(tm), "| pairs:", sum(m["n_pairs"] for m in tm))
print(man.to_string(index=False))

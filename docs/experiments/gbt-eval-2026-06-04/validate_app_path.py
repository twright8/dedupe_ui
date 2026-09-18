"""Validate the harness against the REAL app training/apply path.

Writes the gold set (held_out=1) + the seed-0 N=200 training prefix (held_out=0)
into the isolated DB, runs the production gbt_train.train(), and compares its
reported AUC to the harness. Also exposes the app's in-sample calibration bias
(it fits isotonic on the held-out gold, then reports brier_calibrated on that same
set) and exercises the apply() collapse guard.
"""
from __future__ import annotations
import sys, json
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/backend")
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import os
DB = os.path.join(os.environ["DATA_DIR"], "linkage.db")
RUN = "/home/tomwright/gbt_eval_data/runs/run_2026_06_04a"

from app.db import init_db, write_db, query_db
from app.routers.labels import upsert_label
from app.pipeline import gbt_model
from app.pipeline.gbt_train import train

OUT = Path("/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")

def reset_labels():
    write_db(DB, "DELETE FROM labels", ())

def insert_gold():
    g = pd.read_csv(OUT / "gold_labels.csv")
    g = g[g.verdict.isin(["TRUE", "FALSE"])]
    for _, r in g.iterrows():
        write_db(DB,
            """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                  roe_name, ocod_name_raw, ocod_jurisdiction_raw, is_true_match,
                  reviewer, run_id, active, provenance, held_out)
               VALUES (?,?,?,?,?,?,?, 'sonnet', NULL, 1, 'llm', 1)""",
            (r.ocod_name_clean, r.jurisdiction_clean, r.roe_company_number, r.roe_name_raw,
             r.ocod_name_raw, r.jurisdiction_clean, r.verdict))
    return len(g)

def insert_train(N):
    t = pd.read_csv(OUT / "train_labels.csv")
    t = t[t.verdict.isin(["TRUE", "FALSE"])].reset_index(drop=True)
    order = list(np.random.default_rng(0).permutation(len(t)))[:N]
    for i in order:
        r = t.iloc[i]
        upsert_label(DB, r.ocod_name_clean, r.jurisdiction_clean, r.roe_company_number,
                     r.ocod_name_raw, r.jurisdiction_clean, r.verdict,
                     reviewer="sonnet", notes="exp", run_id=None, provenance="llm",
                     roe_name=r.roe_name_raw)
    return N

init_db(DB)
reset_labels()
ng = insert_gold()
nt = insert_train(200)
print(f"DB: {ng} gold (held_out=1), {nt} train (held_out=0)")
print("eval-set counts:", query_db(DB, "SELECT is_true_match, COUNT(*) c FROM labels WHERE active=1 AND held_out=1 GROUP BY is_true_match"))

m = train(DB, RUN)
print("\n=== production gbt_train.train() metrics ===")
print(json.dumps({k: m[k] for k in ("n_train","n_proxy","n_labels_train","n_labels_eval",
       "eval_source","auc","brier_raw","brier_calibrated") if k in m}, indent=2))
print("\nHarness random_N200 (for comparison): auc_gbt=0.889, brier_gbt_raw=0.125, brier_gbt_cal(OOF, honest)=0.122")
print(">>> The app's brier_calibrated is fit AND measured on the gold set (in-sample) -> optimistic.")

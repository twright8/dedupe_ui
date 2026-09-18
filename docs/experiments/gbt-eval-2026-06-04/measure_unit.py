"""Measure the unit_mismatch feature: coverage beyond the existing digit feature,
its labels on the gold set, and AUC with vs without it (does it regress?)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
from app.pipeline.gbt_train import _LGBM_PARAMS

universe = pd.read_csv(H.OUT/"universe.csv")
uX = H.feats(universe.assign(y=0))
um = uX.unit_mismatch
print(f"universe pairs flagged unit_mismatch=1: {int(um.sum())} of {len(uX)}")
new = (um == 1) & (uX.name_digits_sorted_exact == 1)
print(f"  of those, NEW (digits matched, a letter/roman unit differed — digit feature missed): {int(new.sum())}")
ex = universe[new.values].head(6)
for _,r in ex.iterrows(): print(f"    {r.ocod_name_clean!r}  vs  {r.roe_name_clean!r}")

gold = pd.read_csv(H.OUT/"gold_labels.csv"); gold=gold[gold.verdict.isin(["TRUE","FALSE"])].copy(); gold["y"]=(gold.verdict=="TRUE").astype(int)
gX = H.feats(gold)
flagged = gold[gX.unit_mismatch.values == 1]
print(f"\ngold pairs flagged unit_mismatch=1: {len(flagged)}  (verdicts: {dict(flagged.verdict.value_counts())})")
print("  -> a clean feature should see these as mostly FALSE")

# AUC with vs without the feature at N=491
train = pd.read_csv(H.OUT/"train_labels.csv"); train=train[train.verdict.isin(["TRUE","FALSE"])].copy(); train["y"]=(train.verdict=="TRUE").astype(int)
tX=H.feats(train).reset_index(drop=True); ty=train.y.to_numpy(); y=gold.y.to_numpy()
cols_no = [c for c in H.FEATURE_COLS if c != "unit_mismatch"]
def auc(cols, mono):
    Xtr=pd.concat([H.PROXY_X[cols], tX[cols]],ignore_index=True); ytr=np.concatenate([H.PROXY_Y, ty])
    c=lgb.LGBMClassifier(**_LGBM_PARAMS, n_jobs=1, monotone_constraints=mono); c.fit(Xtr,ytr)
    return roc_auc_score(y, c.predict_proba(gX[cols])[:,1])
a_with=auc(H.FEATURE_COLS, [-1 if c=='unit_mismatch' else 0 for c in H.FEATURE_COLS])
a_without=auc(cols_no, [0]*len(cols_no))
print(f"\ngold AUC @N=491  without unit_mismatch: {a_without:.4f}   with: {a_with:.4f}   (delta {a_with-a_without:+.4f})")

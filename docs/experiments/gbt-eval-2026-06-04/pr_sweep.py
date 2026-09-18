"""Precision / recall / F1 of the auto-accept decision (score >= 0.70), GBT vs the
Splink baseline, across label budgets. Multi-seed averaged for the GBT. Also draws
fig_pr_vs_N.png."""
from __future__ import annotations
import sys
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H

SEEDS = list(range(12)); BUDGETS = [25, 50, 100, 150, 200, 300, 400, 491]; THR = 0.70

gold = pd.read_csv(H.OUT/"gold_labels.csv"); gold=gold[gold.verdict.isin(["TRUE","FALSE"])].copy(); gold["y"]=(gold.verdict=="TRUE").astype(int)
train = pd.read_csv(H.OUT/"train_labels.csv"); train=train[train.verdict.isin(["TRUE","FALSE"])].copy(); train["y"]=(train.verdict=="TRUE").astype(int)
goldX=H.feats(gold); y=gold.y.to_numpy(); gsp=gold.splink_prob.to_numpy()
trainX=H.feats(train).reset_index(drop=True); trainy=train.y.to_numpy()

def prf(pred, y):
    tp=int((pred&(y==1)).sum()); fp=int((pred&(y==0)).sum()); fn=int((~pred&(y==1)).sum())
    p=tp/(tp+fp) if tp+fp else float("nan"); r=tp/(tp+fn) if tp+fn else float("nan")
    f=2*p*r/(p+r) if p and r and p+r else float("nan")
    return p,r,f,int(pred.sum())

# Splink baseline (constant)
sp_p,sp_r,sp_f,sp_n = prf(gsp>=THR, y)

rows=[]
for N in BUDGETS:
    accs=[]
    for s in SEEDS:
        idx=list(np.random.default_rng(s).permutation(len(train)))[:N]
        clf=H.fit_clf(trainX.iloc[idx], trainy[idx]); iso=H.oof_iso(trainX.iloc[idx], trainy[idx])
        _,cal=H.score(clf, iso, goldX)
        accs.append(prf(cal>=THR, y))
    A=np.array([[a[0],a[1],a[2],a[3]] for a in accs], float)
    rows.append(dict(N=N, prec=np.nanmean(A[:,0]), prec_sd=np.nanstd(A[:,0]),
                     rec=np.nanmean(A[:,1]), rec_sd=np.nanstd(A[:,1]),
                     f1=np.nanmean(A[:,2]), n_accept=np.nanmean(A[:,3])))
df=pd.DataFrame(rows)
df.to_csv(H.OUT/"pr_sweep.csv", index=False)
print(f"Splink baseline @0.70:  precision={sp_p:.3f}  recall={sp_r:.3f}  F1={sp_f:.3f}  (accepts {sp_n})")
print(df.round(3).to_string(index=False))

fig,ax=plt.subplots(figsize=(8,4.6))
ax.fill_between(df.N, df.prec-df.prec_sd, df.prec+df.prec_sd, alpha=.15, color="C0")
ax.plot(df.N, df.prec, "o-", color="C0", label="GBT precision")
ax.fill_between(df.N, df.rec-df.rec_sd, df.rec+df.rec_sd, alpha=.15, color="C1")
ax.plot(df.N, df.rec, "s-", color="C1", label="GBT recall")
ax.axhline(sp_p, color="C0", ls=":", label=f"Splink precision = {sp_p:.2f}")
ax.axhline(sp_r, color="C1", ls=":", label=f"Splink recall = {sp_r:.2f}")
ax.set_xlabel("training labels  N"); ax.set_ylabel("precision / recall at the 0.70 auto-accept line")
ax.set_title("Auto-accept decision quality vs labels (GBT, 12-seed mean) vs Splink")
ax.legend(fontsize=8); ax.grid(alpha=.3); ax.set_ylim(0.4,1.0)
fig.tight_layout(); fig.savefig(H.OUT/"fig_pr_vs_N.png", dpi=110)
print("wrote fig_pr_vs_N.png")

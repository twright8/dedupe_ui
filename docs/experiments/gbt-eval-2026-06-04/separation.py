"""The reviewer's two-threshold problem, measured.

For a reviewer who sets an auto-ACCEPT line (above it = accept untouched), an
auto-REJECT line (below it = bin untouched), and reviews the CONTESTED middle by
hand, the questions that matter are:

  1. How far DOWN can the auto-accept line go before a false positive creeps in?
     -> how many pairs can be accepted at ~100% / ~95% purity (and what share of
        all true matches that captures "for free").
  2. How far UP can the auto-reject line go before a real match is lost?
  3. How WIDE is the contested middle (both TRUE and FALSE labels present) — the
     pairs a human is forced to review.

These are pure RANKING properties: any monotonic calibration leaves them
unchanged. So we compare Splink's ordering vs the GBT's ordering, across label
budgets (random, 12 seeds). Purity is measured on the frozen gold set; the
band counts are then projected onto the full 1,081-pair universe (the workload).
"""
from __future__ import annotations
import sys
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H
from sklearn.metrics import roc_auc_score, average_precision_score, precision_recall_curve

SEEDS = list(range(12)); BUDGETS = [50, 100, 200, 300, 400, 491]

gold = pd.read_csv(H.OUT/"gold_labels.csv"); gold=gold[gold.verdict.isin(["TRUE","FALSE"])].copy(); gold["y"]=(gold.verdict=="TRUE").astype(int)
train = pd.read_csv(H.OUT/"train_labels.csv"); train=train[train.verdict.isin(["TRUE","FALSE"])].copy(); train["y"]=(train.verdict=="TRUE").astype(int)
universe = pd.read_csv(H.OUT/"universe.csv")
goldX=H.feats(gold); y=gold.y.to_numpy(); gsp=gold.splink_prob.to_numpy()
univX=H.feats(universe.assign(y=0)); usp=universe.splink_prob.to_numpy()
trainX=H.feats(train).reset_index(drop=True); trainy=train.y.to_numpy()
P=int(y.sum()); NU=len(universe)

def profile(sg, y, su, purity):
    """sg=gold scores, su=universe scores. Returns the lowest auto-accept line and
    highest auto-reject line achieving `purity`, plus band sizes on gold + universe."""
    uniq=np.unique(sg)
    # auto-accept: lowest t with precision(>=t) >= purity, maximising n accepted
    acc_t=np.inf; acc_n=0; acc_true=0
    for t in uniq:
        m=sg>=t; n=int(m.sum())
        if n and y[m].mean()>=purity and n>acc_n:
            acc_t=float(t); acc_n=n; acc_true=int(y[m].sum())
    # auto-reject: highest t with (fraction FALSE among <t) >= purity, maximising n rejected
    rej_t=-np.inf; rej_n=0; rej_true=0
    for t in uniq:
        m=sg<t; n=int(m.sum())
        if n and (y[m]==0).mean()>=purity and n>rej_n:
            rej_t=float(t); rej_n=n; rej_true=int(y[m].sum())
    contested_gold=int(((sg>=rej_t)&(sg<acc_t)).sum()) if acc_t>rej_t else 0
    # project onto the universe (the actual workload)
    u_acc=int((su>=acc_t).sum()); u_rej=int((su<rej_t).sum()); u_rev=NU-u_acc-u_rej
    return dict(acc_t=acc_t, acc_n=acc_n, acc_capture=acc_true/P,
                rej_t=rej_t, rej_n=rej_n, rej_true_lost=rej_true,
                contested_gold=contested_gold, contested_gold_frac=contested_gold/len(y),
                u_accept=u_acc, u_review=u_rev, u_reject=u_rej)

def row(label, sg, su, n=None, seedstats=None):
    d=dict(scorer=label, n_labels=n, auc=roc_auc_score(y,sg), ap=average_precision_score(y,sg))
    for pur,tag in [(1.0,"p100"),(0.95,"p95"),(0.90,"p90")]:
        pr=profile(sg,y,su,pur)
        for k,v in pr.items(): d[f"{tag}_{k}"]=v
    return d

rows=[]
# Splink baseline
rows.append(row("Splink", gsp, usp))
# GBT per budget (multi-seed): average the key scale-free numbers
agg_rows=[]
gbt_curves={}
for N in BUDGETS:
    seed_d=[]
    raw_gold_acc=[]
    for s in SEEDS:
        idx=list(np.random.default_rng(s).permutation(len(train)))[:N]
        clf=H.fit_clf(trainX.iloc[idx], trainy[idx])
        sg=clf.predict_proba(goldX[H.FEATURE_COLS])[:,1]
        su=clf.predict_proba(univX[H.FEATURE_COLS])[:,1]
        seed_d.append(row(f"GBT", sg, su, n=N))
        if s==0: gbt_curves[N]=(sg.copy(),)  # for PR curve fig
    df=pd.DataFrame(seed_d).replace([np.inf,-np.inf], np.nan)
    a={"scorer":"GBT","n_labels":N}
    for c in df.columns:
        if c in ("scorer","n_labels"): continue
        a[c]=df[c].mean(); a[c+"_sd"]=df[c].std()
    agg_rows.append(a)
allrows=pd.DataFrame(rows+agg_rows)
allrows.to_csv(H.OUT/"separation_metrics.csv", index=False)

cols=["scorer","n_labels","auc","ap",
      "p100_acc_capture","p100_u_accept","p100_u_review","p100_u_reject",
      "p95_acc_capture","p95_u_accept","p95_u_review","p95_u_reject","p95_contested_gold"]
sb=profile(gsp,y,usp,1.0); sb95=profile(gsp,y,usp,0.95)
print("=== Splink baseline ===")
print(f"AUC={roc_auc_score(y,gsp):.3f} AP={average_precision_score(y,gsp):.3f}")
print(f"  ZERO-FP auto-accept: line>={sb['acc_t']:.3f}, captures {sb['acc_capture']*100:.0f}% of true matches; universe accept/review/reject = {sb['u_accept']}/{sb['u_review']}/{sb['u_reject']}")
print(f"  95%-pure auto-accept: line>={sb95['acc_t']:.3f}, captures {sb95['acc_capture']*100:.0f}%; auto-reject line<{sb95['rej_t'] if np.isfinite(sb95['rej_t']) else None}; universe a/r/r = {sb95['u_accept']}/{sb95['u_review']}/{sb95['u_reject']}")
print("\n=== GBT (mean of 12 seeds) ===")
print(allrows[allrows.scorer=="GBT"][cols].round(3).to_string(index=False))

# ---- purity sweep: Splink vs GBT-400 (mean of 12 seeds) ----
def gbt400_profile(pur):
    accs=[]; revs=[]; rejs=[]
    for s in SEEDS:
        idx=list(np.random.default_rng(s).permutation(len(train)))[:400]
        clf=H.fit_clf(trainX.iloc[idx], trainy[idx])
        sg=clf.predict_proba(goldX[H.FEATURE_COLS])[:,1]; su=clf.predict_proba(univX[H.FEATURE_COLS])[:,1]
        g=profile(sg,y,su,pur); accs.append(g['u_accept']); revs.append(g['u_review']); rejs.append(g['u_reject'])
    return int(np.mean(accs)),int(np.mean(revs)),int(np.mean(rejs))
print("\n=== Accept/Review/Reject of the 1,081 universe, by purity target (GBT=400 labels, 12-seed mean) ===")
print(f"{'purity':>7} | {'Splink  acc/rev/rej':>22} | {'GBT-400 acc/rev/rej':>22}")
for pur in (0.90,0.95,0.99,1.0):
    s=profile(gsp,y,usp,pur); ga,gr,gj=gbt400_profile(pur)
    print(f"{pur:>7.2f} | {s['u_accept']:>6}/{s['u_review']:>5}/{s['u_reject']:>5}      | {ga:>6}/{gr:>5}/{gj:>5}")

# ---- figures ----
# Fig 1: precision-recall (purity vs coverage of true matches) -- "the clean top"
fig,ax=plt.subplots(figsize=(8,5))
pr_s=precision_recall_curve(y,gsp); ax.plot(pr_s[1],pr_s[0],color="k",lw=2,label="Splink")
for N,c in [(200,"C1"),(400,"C2"),(491,"C0")]:
    sg=gbt_curves[N][0]
    p,r,_=precision_recall_curve(y,sg); ax.plot(r,p,color=c,lw=1.8,label=f"GBT N={N}")
ax.axhline(0.95,ls=":",color="grey"); ax.axhline(1.0,ls=":",color="grey")
ax.text(0.02,0.96,"95% purity",fontsize=8,color="grey")
ax.set_xlabel("coverage  (share of all true matches captured)")
ax.set_ylabel("purity of the accepted pile  (precision)")
ax.set_title("How big a CLEAN auto-accept top can each score give you?\n(top-left = lots of true matches with no false positives)")
ax.legend(); ax.grid(alpha=.3); ax.set_ylim(0.4,1.02)
fig.tight_layout(); fig.savefig(H.OUT/"fig_clean_top.png",dpi=110); plt.close(fig)

# Fig 2: universe split into accept/contested/reject at 95% purity -- the contested middle
fig,ax=plt.subplots(figsize=(8,3.2))
labels=["Splink","GBT N=100","GBT N=200","GBT N=400","GBT N=491"]
def bands(scorer,N):
    if scorer=="Splink":
        pr=profile(gsp,y,usp,0.95); return pr["u_accept"],pr["u_review"],pr["u_reject"]
    r=allrows[(allrows.scorer=="GBT")&(allrows.n_labels==N)].iloc[0]
    return float(r["p95_u_accept"]),float(r["p95_u_review"]),float(r["p95_u_reject"])
data=[bands("Splink",None)]+[bands("GBT",n) for n in (100,200,400,491)]
data=np.array(data)
ax.barh(labels,data[:,0],color="#2ca02c",label="auto-accept (≥95% pure)")
ax.barh(labels,data[:,1],left=data[:,0],color="#ff7f0e",label="CONTESTED — human review")
ax.barh(labels,data[:,2],left=data[:,0]+data[:,1],color="#d62728",label="auto-reject")
ax.set_xlabel("candidate pairs (of 1,081)"); ax.set_title("The contested middle: how many pairs actually need a human (95%-pure lines)")
ax.legend(fontsize=8,loc="lower right"); fig.tight_layout(); fig.savefig(H.OUT/"fig_contested_bands.png",dpi=110); plt.close(fig)

# Fig 3: contested width (% universe) + clean-top capture vs N
g=allrows[allrows.scorer=="GBT"].sort_values("n_labels")
fig,ax=plt.subplots(figsize=(8,4.6))
ax.plot(g.n_labels, g.p95_u_review/NU*100,"o-",color="C1",label="GBT contested middle (% of universe)")
ax.axhline(profile(gsp,y,usp,0.95)["u_review"]/NU*100,ls=":",color="k",label="Splink contested middle")
ax.plot(g.n_labels, g.p95_acc_capture*100,"s-",color="C2",label="GBT true matches in clean top (%)")
ax.axhline(profile(gsp,y,usp,0.95)["acc_capture"]*100,ls="--",color="grey",label="Splink clean-top capture")
ax.set_xlabel("training labels  N"); ax.set_ylabel("percent")
ax.set_title("Does the GBT narrow the contested middle and grow the clean top?")
ax.legend(fontsize=8); ax.grid(alpha=.3); fig.tight_layout(); fig.savefig(H.OUT/"fig_separation_vs_N.png",dpi=110); plt.close(fig)
print("\nwrote separation_metrics.csv + fig_clean_top.png, fig_contested_bands.png, fig_separation_vs_N.png")

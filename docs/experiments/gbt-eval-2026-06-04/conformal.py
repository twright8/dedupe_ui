"""Conformal-style risk control: pick auto-accept / auto-reject lines with a
statistical guarantee on the error rate, run OFF to the side of the pipeline.

Idea in plain terms: take checked labels the model didn't train on, split them
into a "pick" half and a "verify" half. On the pick half, find the lowest
auto-accept line where we're 95%-confident the accepted pile is >= target pure
(Wilson lower bound on precision). Do the mirror for auto-reject. Then prove the
guarantee holds by measuring the realised error on the verify half (never used to
pick the line). Lines are reported on the calibrated 0-1 dial the product buckets on.
"""
from __future__ import annotations
import sys, math
import numpy as np, pandas as pd
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H
from sklearn.model_selection import StratifiedKFold
from sklearn.linear_model import LogisticRegression

def wilson_lb(x, n, z=1.96):
    """95% lower confidence bound on a proportion x/n."""
    if n == 0: return 0.0
    p = x / n
    d = 1 + z*z/n
    c = p + z*z/(2*n)
    m = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))
    return (c - m) / d

# --- pool ALL 718 checked labels; score each one OUT-OF-FOLD (a model that never
#     saw it), so every label is honest calibration data. More labels = tighter bound. ---
gold = pd.read_csv(H.OUT/"gold_labels.csv"); gold=gold[gold.verdict.isin(["TRUE","FALSE"])].copy()
train = pd.read_csv(H.OUT/"train_labels.csv"); train=train[train.verdict.isin(["TRUE","FALSE"])].copy()
allL = pd.concat([gold, train], ignore_index=True); allL["y"]=(allL.verdict=="TRUE").astype(int)
universe = pd.read_csv(H.OUT/"universe.csv")
allX=H.feats(allL).reset_index(drop=True); ally=allL.y.to_numpy()
univX=H.feats(universe.assign(y=0)); NU=len(universe)

skf=StratifiedKFold(5, shuffle=True, random_state=42); oofcal=np.zeros(len(ally))
for tr,te in skf.split(allX, ally):
    c=H.fit_clf(allX.iloc[tr], ally[tr])
    praw=c.predict_proba(allX.iloc[tr][H.FEATURE_COLS])[:,1]
    p=LogisticRegression().fit(praw.reshape(-1,1), ally[tr])
    oofcal[te]=p.predict_proba(c.predict_proba(allX.iloc[te][H.FEATURE_COLS])[:,1].reshape(-1,1))[:,1]
# universe scored by a model trained on ALL labels
clf=H.fit_clf(allX, ally)
allraw=clf.predict_proba(allX[H.FEATURE_COLS])[:,1]
platt=LogisticRegression().fit(allraw.reshape(-1,1), ally)
ucal=platt.predict_proba(clf.predict_proba(univX[H.FEATURE_COLS])[:,1].reshape(-1,1))[:,1]

# split the 718 honest scores: pick lines on CALIB, prove the guarantee on TEST
from sklearn.model_selection import train_test_split
ci, ti = train_test_split(np.arange(len(ally)), test_size=0.5, stratify=ally, random_state=1)
gcal=oofcal; y=ally
sc_c, y_c = oofcal[ci], ally[ci]; sc_t, y_t = oofcal[ti], ally[ti]
print(f"(calibration labels: {len(ci)} pick / {len(ti)} verify; rule of thumb: to GUARANTEE")
print(f" <=E% error you need ~3/E clean points above the line, e.g. <=1% needs ~300.)\n")

def accept_line(target):
    """lowest threshold with 95%-confident precision >= target (max coverage)."""
    best=(np.inf, 0, 0.0)
    for t in np.unique(sc_c):
        m=sc_c>=t; n=int(m.sum()); x=int(y_c[m].sum())
        if n and wilson_lb(x,n)>=target and n>best[1]:
            best=(float(t), n, x/n)
    return best  # (threshold, n_accept_calib, observed_precision)

def reject_line(target):
    """highest threshold with 95%-confident reject-purity (fraction FALSE) >= target."""
    best=(-np.inf, 0, 0.0)
    for t in np.unique(sc_c):
        m=sc_c<t; n=int(m.sum()); x=int((y_c[m]==0).sum())
        if n and wilson_lb(x,n)>=target and n>best[1]:
            best=(float(t), n, x/n)
    return best

print("=== Conformal-style auto-ACCEPT lines (GBT, calibrated dial) ===")
print(f"{'target':>7} | {'line>=':>6} | {'universe accept':>15} | {'% matches':>9} | {'realised precision on held-out':>30}")
for target in (0.99, 0.95, 0.90, 0.85):
    t,nc,obs = accept_line(target)
    if not np.isfinite(t): print(f"{target:>7.0%} |   none reachable at 95% confidence with this calib size"); continue
    ua=int((ucal>=t).sum())
    cov = (gcal[y==1]>=t).mean()
    m=sc_t>=t; realised = (y_t[m].mean() if m.sum() else float('nan'))
    print(f"{target:>7.0%} | {t:>6.2f} | {ua:>15} | {cov:>8.0%} | {realised:>26.1%}  (n={int(m.sum())})")

# empirical zero-false-positive line (the "0% wrong" the user asked about)
fps = sc_c[y_c==0]
zfp = float(fps.max()) if len(fps) else 1.0
ua = int((ucal>zfp).sum()); cov=(gcal[y==1]>zfp).mean()
print(f"\n'zero wrong' empirical line: accept > {zfp:.2f} (above every false match SEEN) -> {ua} universe pairs, {cov:.0%} of matches.")
print("  (no statistical GUARANTEE of exactly 0% on unseen data — finite calibration set.)")

print("\n=== auto-REJECT lines ===")
for target in (0.99, 0.97, 0.95, 0.90):
    t,nc,obs = reject_line(target)
    if not np.isfinite(t): print(f"{target:>7.0%} |  none reachable"); continue
    ur=int((ucal<t).sum())
    m=sc_t<t; realised=((y_t[m]==0).mean() if m.sum() else float('nan'))
    print(f"{target:>7.0%} | reject<{t:.2f}: universe auto-reject {ur:>4} | realised purity on held-out {realised:.1%} (n={int(m.sum())})")

# put it together at the best reachable operating point
ta,_,_=accept_line(0.85); tr_,_,_=reject_line(0.95)
acc=int((ucal>=ta).sum()) if np.isfinite(ta) else 0
rej=int((ucal<tr_).sum()) if np.isfinite(tr_) else 0
al = f">= {ta:.2f}" if np.isfinite(ta) else "NONE (top too contaminated to certify)"
print(f"\nWorked example: auto-accept {al} ({acc}), auto-reject < {tr_:.2f} ({rej}), "
      f"REVIEW the remaining {NU-acc-rej} of {NU}.")

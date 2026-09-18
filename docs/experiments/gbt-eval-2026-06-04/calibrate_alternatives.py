"""How much of the GBT's win is just 'calibrate Splink'? And does a smoother
calibrator (Platt/logistic) beat isotonic on collapse while keeping de-clumping?

Compares, on the frozen gold set, across label budgets:
  A) raw Splink                          (baseline)
  B) Splink + isotonic   calibration     (NO new model -- just fix the number)
  C) Splink + Platt(logistic) calibration
  D) GBT + isotonic      (the app today)
  E) GBT + Platt(logistic) calibration
"""
from __future__ import annotations
import sys
import numpy as np, pandas as pd
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, brier_score_loss

gold = pd.read_csv(H.OUT/"gold_labels.csv"); gold=gold[gold.verdict.isin(["TRUE","FALSE"])].copy(); gold["y"]=(gold.verdict=="TRUE").astype(int)
train = pd.read_csv(H.OUT/"train_labels.csv"); train=train[train.verdict.isin(["TRUE","FALSE"])].copy(); train["y"]=(train.verdict=="TRUE").astype(int)
universe = pd.read_csv(H.OUT/"universe.csv")
goldX=H.feats(gold); goldy=gold.y.to_numpy(); gsp=gold.splink_prob.to_numpy()
trainX=H.feats(train).reset_index(drop=True); trainy=train.y.to_numpy(); tsp=train.splink_prob.to_numpy()
univX=H.feats(universe.assign(y=0)); usp=universe.splink_prob.to_numpy()
order=list(np.random.default_rng(0).permutation(len(train)))

def platt(xs, ys):
    lr=LogisticRegression(C=1.0); lr.fit(np.asarray(xs).reshape(-1,1), ys);
    return lambda z: lr.predict_proba(np.asarray(z).reshape(-1,1))[:,1]
def iso(xs, ys):
    m=IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1); m.fit(xs, ys)
    return lambda z: m.predict(z)

def metrics(y, p, uni=None):
    d=dict(auc=roc_auc_score(y,p), brier=brier_score_loss(y,p), ece=H.ece(y,p),
           distinct=int(pd.Series(p).round(4).nunique()))
    if uni is not None:
        d["rb_univ"]=int(np.sum((uni>=H.REVIEW)&(uni<H.HIGH)))
        d["frac_extreme"]=float(np.mean((uni<0.05)|(uni>0.95)))
    return d

rows=[]
for N in [50,100,200,491]:
    idx=order[:N]
    # Splink-only calibrators (trained on N labels' splink scores)
    ci=iso(tsp[idx], trainy[idx]); cp=platt(tsp[idx], trainy[idx])
    rows.append(dict(N=N, model="B Splink+iso", **metrics(goldy, ci(gsp), ci(usp))))
    rows.append(dict(N=N, model="C Splink+Platt", **metrics(goldy, cp(gsp), cp(usp))))
    # GBT
    clf=H.fit_clf(trainX.iloc[idx], trainy[idx])
    graw=clf.predict_proba(goldX[H.FEATURE_COLS])[:,1]; uraw=clf.predict_proba(univX[H.FEATURE_COLS])[:,1]
    traw=clf.predict_proba(trainX.iloc[idx][H.FEATURE_COLS])[:,1]
    gi=iso(traw, trainy[idx]); gp=platt(traw, trainy[idx])
    rows.append(dict(N=N, model="D GBT+iso", **metrics(goldy, gi(graw), gi(uraw))))
    rows.append(dict(N=N, model="E GBT+Platt", **metrics(goldy, gp(graw), gp(uraw))))

base=metrics(goldy, gsp, usp)
print("A raw Splink (any N): AUC=%.3f Brier=%.3f ECE=%.3f distinct=%d review_band=%d" % (
    base["auc"], base["brier"], base["ece"], base["distinct"], base["rb_univ"]))
df=pd.DataFrame(rows)
with pd.option_context("display.width",200):
    print(df.round(3).to_string(index=False))

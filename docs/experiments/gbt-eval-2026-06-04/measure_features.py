"""A/B the new feature groups against the standard metrics, so we keep only what
helps. Baseline = the 8-feature set (incl unit_mismatch); then + Splink's extra
per-comparison signals, + IDF token overlap, + both. The experiment CSVs don't carry
Splink's gamma/bf columns, so we join them back from linkage_scored for this test."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04")
import harness as H
from app.pipeline.gbt_features import FEATURE_COLS, SPLINK_EXTRA_COLS, build_features
from app.pipeline.gbt_train import _LGBM_PARAMS
import lightgbm as lgb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, brier_score_loss

RUN = "/home/tomwright/gbt_eval_data/runs/run_2026_06_04a"
sc = pd.read_parquet(RUN + "/linkage_scored.parquet")
extra_cols = [c for c in SPLINK_EXTRA_COLS if c in sc.columns]
extra = sc[["unique_id_l", "unique_id_r"] + extra_cols]
print("Splink extra columns found in linkage_scored:", extra_cols)

def feats_rich(df):
    p = df.rename(columns={"ocod_uid": "unique_id_l", "roe_uid": "unique_id_r", "splink_prob": "match_probability"})
    p = p.merge(extra, on=["unique_id_l", "unique_id_r"], how="left")
    return build_features(p, H.OCOD, H.ROE, splink_prob_col="match_probability")

gold = pd.read_csv(H.OUT/"gold_labels.csv"); gold=gold[gold.verdict.isin(["TRUE","FALSE"])].copy(); gold["y"]=(gold.verdict=="TRUE").astype(int)
train = pd.read_csv(H.OUT/"train_labels.csv"); train=train[train.verdict.isin(["TRUE","FALSE"])].copy(); train["y"]=(train.verdict=="TRUE").astype(int)
universe = pd.read_csv(H.OUT/"universe.csv")
gX = feats_rich(gold); y = gold.y.to_numpy()
tX = feats_rich(train).reset_index(drop=True); ty = train.y.to_numpy()
uX = feats_rich(universe.assign(y=0))

base8 = FEATURE_COLS[:8]
SUBSETS = {
    "base (8)":        base8,
    "+ splink extras": base8 + SPLINK_EXTRA_COLS,
    "+ idf":           base8 + ["idf_token_overlap"],
    "+ both (18)":     FEATURE_COLS,
}

def evaluate(cols, N):
    idx = list(np.random.default_rng(0).permutation(len(ty)))[:N]
    Xtr = pd.concat([H.PROXY_X[cols], tX[cols].iloc[idx]], ignore_index=True)
    ytr = np.concatenate([H.PROXY_Y, ty[idx]])
    mono = [-1 if c == "unit_mismatch" else 0 for c in cols]
    clf = lgb.LGBMClassifier(**_LGBM_PARAMS, n_jobs=1, monotone_constraints=mono); clf.fit(Xtr, ytr)
    raw = clf.predict_proba(gX[cols])[:, 1]
    tr_raw = clf.predict_proba(tX[cols].iloc[idx])[:, 1]
    lr = LogisticRegression().fit(tr_raw.reshape(-1, 1), ty[idx])
    cal = lr.predict_proba(raw.reshape(-1, 1))[:, 1]
    ucal = lr.predict_proba(clf.predict_proba(uX[cols])[:, 1].reshape(-1, 1))[:, 1]
    return dict(AUC=roc_auc_score(y, raw), Brier=brier_score_loss(y, raw), ECE=H.ece(y, cal),
                review_band=int(((ucal >= H.REVIEW) & (ucal < H.HIGH)).sum()))

for N in (200, 491):
    print(f"\n=== N={N} labels ===")
    print(f"{'feature set':<16} {'AUC↑':>7} {'Brier↓':>7} {'ECE↓':>7} {'review band':>12}")
    for name, cols in SUBSETS.items():
        m = evaluate(cols, N)
        print(f"{name:<16} {m['AUC']:>7.4f} {m['Brier']:>7.4f} {m['ECE']:>7.4f} {m['review_band']:>12}")

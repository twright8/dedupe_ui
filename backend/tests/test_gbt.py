# backend/tests/test_gbt.py
"""Tests for the GBT re-scoring layer: features, cold-start proxy, training, scoring."""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.db import write_db
from app.pipeline import gbt_model
from app.pipeline.gbt_features import FEATURE_COLS, build_features


def _records(prefix, names, jur="ENGLAND"):
    rows = []
    for i, name in enumerate(names):
        rows.append(
            {
                "unique_id": f"{prefix}-{i}",
                "name_clean": name,
                "jurisdiction_clean": jur,
                "name_core": name.replace(" LTD", "").strip(),
                "name_tokens_sorted": " ".join(sorted(name.split())),
                "name_digits_sorted": "",
                "roe_company_number": f"OE{i:06d}",
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A synthetic completed-run directory with the parquets GBT training reads."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # model artifacts land under tmp/models

    rd = tmp_path / "runs" / "run_test"
    rd.mkdir(parents=True)

    ocod_names = [f"ACME {i} LTD" for i in range(40)]
    roe_names = [f"ACME {i} LIMITED" for i in range(40)]
    ocod = _records("ocod", ocod_names)
    roe = _records("roe", roe_names)
    ocod.to_parquet(rd / "ocod_phase2.parquet")
    roe.to_parquet(rd / "roe_phase2.parquet")

    # Exact matches (proxy positives) — only len() is used by the proxy builder.
    pd.DataFrame({"unique_id_ocod": ["ocod-0"], "unique_id_roe": ["roe-0"],
                  "name_clean": ["ACME 0 LTD"]}).to_parquet(rd / "exact_matches.parquet")

    # Scored Phase-2 pairs with a Splink probability.
    scored = pd.DataFrame(
        {
            "unique_id_l": [f"ocod-{i}" for i in range(40)],
            "unique_id_r": [f"roe-{i}" for i in range(40)],
            "match_probability": np.linspace(0.4, 0.95, 40),
        }
    )
    scored.to_parquet(rd / "linkage_scored.parquet")
    return str(rd)


def test_build_features_shape_and_values():
    ocod = _records("ocod", ["ACME LTD", "BETA LTD"])
    roe = _records("roe", ["ACME LTD", "ZZZ LTD"])
    pairs = pd.DataFrame({"unique_id_l": ["ocod-0", "ocod-1"],
                          "unique_id_r": ["roe-0", "roe-1"],
                          "match_probability": [0.9, 0.5]})
    feats = build_features(pairs, ocod, roe)
    assert list(feats.columns) == FEATURE_COLS
    # Row 0: identical cleaned names -> exact + jw 1.0
    assert feats.iloc[0]["base_exact"] == 1
    assert feats.iloc[0]["base_name_jw"] == pytest.approx(1.0)
    assert feats.iloc[0]["splink_p1"] == pytest.approx(0.9)
    # Row 1: different names -> not exact
    assert feats.iloc[1]["base_exact"] == 0


def test_proxy_frame_has_both_classes(run_dir):
    from app.pipeline.gbt_proxy import build_proxy_frame

    X, y = build_proxy_frame(run_dir)
    assert len(X) == len(y) > 0
    assert set(np.unique(y)) == {0, 1}


def test_train_then_score_adds_gbt_column(run_dir, db_path):
    from app.pipeline.gbt_train import train
    from app.pipeline.stage_2_5_gbt import run_stage_2_5_gbt

    # A couple of real labels to exercise the label-resolution path.
    for i, verdict in ((10, "TRUE"), (11, "FALSE")):
        write_db(
            db_path,
            """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                                   is_true_match, reviewer, active, provenance, held_out)
               VALUES (?, ?, ?, ?, 'tester', 1, 'manual', 0)""",
            (f"ACME {i} LTD", "ENGLAND", f"OE{i:06d}", verdict),
        )

    metrics = train(db_path, run_dir)
    assert metrics["n_train"] > 0
    assert gbt_model.model_exists()

    # Cold scoring before model existed would have been a no-op; now it scores.
    result = run_stage_2_5_gbt(run_dir=run_dir)
    assert result["model"] is True
    scored = pd.read_parquet(os.path.join(run_dir, "linkage_scored.parquet"))
    assert "gbt_score" in scored.columns
    assert scored["gbt_score"].between(0.0, 1.0).all()


def test_scoring_is_noop_without_model(run_dir):
    """With no trained model, scoring leaves the parquet untouched (cold pipeline runs)."""
    from app.pipeline.stage_2_5_gbt import run_stage_2_5_gbt

    # Ensure no model exists under this tmp DATA_DIR.
    assert not gbt_model.model_exists()
    result = run_stage_2_5_gbt(run_dir=run_dir)
    assert result["model"] is False
    scored = pd.read_parquet(os.path.join(run_dir, "linkage_scored.parquet"))
    assert "gbt_score" not in scored.columns


def test_labelled_feature_frame_resolves_raw_after_rule_change(db_path):
    """Training path: a label stored under OLD cleaning rules (stale clean key,
    stable raw identity) still resolves onto a run whose frames carry raw
    columns — via the shared resolver's raw-first join. A clean-only join
    (the old private re-implementation) would silently drop it."""
    from app.pipeline.gbt_train import _labelled_feature_frame

    ocod = _records("ocod", ["ACME UK LTD"])            # NEW-rules clean name
    ocod["ocod_name_raw"] = ["Acme (UK) Limited"]
    ocod["ocod_jurisdiction_raw"] = ["England"]
    roe = _records("roe", ["ACME UK LIMITED"])

    write_db(
        db_path,
        """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                               ocod_name_raw, ocod_jurisdiction_raw,
                               is_true_match, reviewer, active, provenance, held_out)
           VALUES (?, ?, ?, ?, ?, 'TRUE', 'tester', 1, 'manual', 0)""",
        ("ACME UK LIMITED", "ENGLAND", "OE000000",      # stale OLD-rules clean key
         "Acme (UK) Limited", "England"),
    )

    X, y = _labelled_feature_frame(db_path, ocod, roe, None, held_out=0)
    assert len(y) == 1
    assert y[0] == 1
    assert list(X.columns) == FEATURE_COLS


# --- Change 1: the Platt calibrator is fit on human rows only ---------------------------

def test_platt_grid_fits_on_human_rows_only():
    """Proxy rows tagged is_human=False must not enter the calibrator fit — including
    them would separate the score and steepen Platt toward a bimodal collapse."""
    from app.pipeline.gbt_train import _platt_grid

    # Human rows: a gentle, overlapping relationship (mid-range scores, mixed labels).
    human_raw = np.array([0.30, 0.38, 0.44, 0.48, 0.52, 0.56, 0.62, 0.70])
    human_y = np.array([0, 0, 1, 0, 1, 0, 1, 1])
    # Proxy rows: perfectly separable synthetic extremes that would dominate the fit.
    proxy_raw = np.concatenate([np.full(300, 0.001), np.full(300, 0.999)])
    proxy_y = np.concatenate([np.zeros(300, int), np.ones(300, int)])

    raw = np.concatenate([human_raw, proxy_raw])
    y = np.concatenate([human_y, proxy_y])
    is_human = np.concatenate([np.ones(len(human_y), bool), np.zeros(len(proxy_y), bool)])

    # Masked fit (proxy present but dropped) == fit on the human rows alone.
    _, curve_masked, _ = _platt_grid(raw, y, is_human)
    _, curve_human, _ = _platt_grid(human_raw, human_y, np.ones(len(human_y), bool))
    assert np.allclose(curve_masked, curve_human)

    # A naive fit that DID include proxy would be visibly different (much steeper).
    _, curve_contaminated, _ = _platt_grid(raw, y, np.ones(len(y), bool))
    assert not np.allclose(curve_contaminated, curve_masked)
    span = lambda c: float(c.max() - c.min())
    assert span(curve_contaminated) > span(curve_masked)


def test_train_calibration_platt_vs_identity(run_dir, db_path):
    """>=12 resolvable human labels (both classes) -> Platt calibration; a held-out eval
    set (held_out=1) -> threshold metrics become available and numeric."""
    from app.pipeline.gbt_train import train

    def _add(i, verdict, held_out):
        write_db(
            db_path,
            """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                                   is_true_match, reviewer, active, provenance, held_out)
               VALUES (?, ?, ?, ?, 'tester', 1, 'manual', ?)""",
            (f"ACME {i} LTD", "ENGLAND", f"OE{i:06d}", verdict, held_out),
        )

    # 14 training labels (7 TRUE / 7 FALSE), held_out=0.
    for i in range(14):
        _add(i, "TRUE" if i % 2 == 0 else "FALSE", 0)
    # 4 held-out eval labels (2 TRUE / 2 FALSE), held_out=1.
    for i in range(20, 24):
        _add(i, "TRUE" if i % 2 == 0 else "FALSE", 1)

    metrics = train(db_path, run_dir)
    assert metrics["calibration"] == "platt"
    assert metrics["n_labels_train"] == 14
    assert metrics["n_labels_eval"] == 4

    tm = metrics["threshold_metrics"]
    assert tm["available"] is True
    assert tm["n_eval"] == 4
    assert len(tm["thresholds"]) == 21  # 0.0 .. 1.0 step 0.05
    row = tm["thresholds"][0]
    for key in ("threshold", "precision", "precision_wilson_lower", "recall",
                "recall_wilson_lower", "n_predicted_positive", "tp", "fp"):
        assert key in row


def test_train_threshold_metrics_unavailable_without_eval(run_dir, db_path):
    """No held-out eval set -> threshold_metrics is an explicit unavailable+reason, NEVER
    numbers computed on training data (CONCERNS_LOG S)."""
    from app.pipeline.gbt_train import train

    for i, verdict in ((10, "TRUE"), (11, "FALSE")):
        write_db(
            db_path,
            """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                                   is_true_match, reviewer, active, provenance, held_out)
               VALUES (?, ?, ?, ?, 'tester', 1, 'manual', 0)""",
            (f"ACME {i} LTD", "ENGLAND", f"OE{i:06d}", verdict),
        )

    metrics = train(db_path, run_dir)
    tm = metrics["threshold_metrics"]
    assert tm["available"] is False
    assert "reason" in tm and tm["reason"]
    assert "thresholds" not in tm


# --- Change 4: threshold_metrics unit behaviour -----------------------------------------

def test_threshold_metrics_unavailable_paths():
    from app.pipeline.gbt_train import threshold_metrics

    empty = threshold_metrics(np.array([]), np.array([]))
    assert empty["available"] is False and "empty" in empty["reason"]

    single = threshold_metrics(np.array([0.2, 0.8, 0.9]), np.array([1, 1, 1]))
    assert single["available"] is False and "single-class" in single["reason"]


def test_threshold_metrics_wilson_below_point_estimate():
    """On a perfectly-separated tiny set the point precision is 1.0 but the Wilson lower
    bound stays well below 1.0 — the honest number the panel should read."""
    from app.pipeline.gbt_train import threshold_metrics

    scores = np.array([0.9, 0.8, 0.7, 0.2, 0.1])
    y = np.array([1, 1, 1, 0, 0])
    tm = threshold_metrics(scores, y)
    assert tm["available"] is True and tm["n_pos"] == 3 and tm["n_neg"] == 2
    at_half = next(r for r in tm["thresholds"] if r["threshold"] == 0.5)
    assert at_half["precision"] == pytest.approx(1.0)
    assert at_half["precision_wilson_lower"] < 0.9


# --- jurisdiction_match: ordinal three-state jurisdiction agreement --------------------

def test_jurisdiction_match_three_states():
    """Encoding: match=1.0, genuine mismatch=0.0, unknown/empty-neutral=0.5 — computed
    from jurisdiction_clean on both sides of each pair, mirroring Splink's three levels."""
    ocod = pd.DataFrame({
        "unique_id": ["o-match", "o-mismatch", "o-unknown", "o-empty"],
        "name_clean": ["ACME LTD"] * 4,
        "jurisdiction_clean": ["ENGLAND", "ENGLAND", "UNKNOWN", ""],
    })
    roe = pd.DataFrame({
        "unique_id": ["r-match", "r-mismatch", "r-unknown", "r-empty"],
        "name_clean": ["ACME LTD"] * 4,
        "jurisdiction_clean": ["ENGLAND", "SCOTLAND", "ENGLAND", "ENGLAND"],
    })
    pairs = pd.DataFrame({
        "unique_id_l": ["o-match", "o-mismatch", "o-unknown", "o-empty"],
        "unique_id_r": ["r-match", "r-mismatch", "r-unknown", "r-empty"],
    })
    feats = build_features(pairs, ocod, roe, splink_prob_col=None)
    assert feats["jurisdiction_match"].tolist() == [1.0, 0.0, 0.5, 0.5]


def test_jurisdiction_match_absent_columns_default_neutral():
    """When the record frames carry no jurisdiction_clean at all, the feature is the
    neutral 0.5 (no evidence) — never a spurious match or mismatch."""
    ocod = pd.DataFrame({"unique_id": ["o-0"], "name_clean": ["ACME LTD"]})
    roe = pd.DataFrame({"unique_id": ["r-0"], "name_clean": ["ACME LTD"]})
    pairs = pd.DataFrame({"unique_id_l": ["o-0"], "unique_id_r": ["r-0"]})
    feats = build_features(pairs, ocod, roe, splink_prob_col=None)
    assert list(feats.columns) == FEATURE_COLS
    assert feats["jurisdiction_match"].iloc[0] == 0.5


def test_monotone_constraints_pin_jurisdiction_and_unit():
    """jurisdiction_match is pinned monotone-increasing (+1) alongside unit_mismatch's
    -1; every other feature is unconstrained."""
    from app.pipeline.gbt_train import _monotone_constraints

    mc = _monotone_constraints()
    assert len(mc) == len(FEATURE_COLS)
    assert mc[FEATURE_COLS.index("jurisdiction_match")] == 1
    assert mc[FEATURE_COLS.index("unit_mismatch")] == -1
    assert all(v == 0 for c, v in zip(FEATURE_COLS, mc)
               if c not in ("jurisdiction_match", "unit_mismatch"))


def test_trained_model_carries_monotone_constraints(run_dir, db_path):
    """The +1/-1 constraints are threaded into the actual LightGBM booster and survive
    the persist/reload round-trip."""
    from app.pipeline.gbt_train import train

    for i, verdict in ((10, "TRUE"), (11, "FALSE")):
        write_db(
            db_path,
            """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                                   is_true_match, reviewer, active, provenance, held_out)
               VALUES (?, ?, ?, ?, 'tester', 1, 'manual', 0)""",
            (f"ACME {i} LTD", "ENGLAND", f"OE{i:06d}", verdict),
        )
    train(db_path, run_dir)

    booster = gbt_model.load_booster()
    cols = gbt_model.load_feature_cols()
    mc = booster.params["monotone_constraints"]
    if isinstance(mc, str):
        mc = [int(x) for x in mc.split(",")]
    assert mc[cols.index("jurisdiction_match")] == 1
    assert mc[cols.index("unit_mismatch")] == -1


def test_proxy_negatives_include_cross_jurisdiction(tmp_path, monkeypatch):
    """A modest share of proxy negatives are cross-jurisdiction (jurisdiction_match=0.0)
    so the feature has training support before human labels exist; same-jurisdiction
    negatives (1.0) still dominate."""
    from app.pipeline.gbt_proxy import build_proxy_frame

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    rd = tmp_path / "runs" / "multi"
    rd.mkdir(parents=True)

    jurs = ["ENGLAND", "SCOTLAND", "WALES", "JERSEY"]
    # Distinct OCOD/ROE name prefixes => no accidental exact-name collisions to drop.
    ocod = pd.concat(
        [_records(f"ocod-{jur}", [f"OCODCO {jur} {i}" for i in range(30)], jur=jur) for jur in jurs],
        ignore_index=True,
    )
    roe = pd.concat(
        [_records(f"roe-{jur}", [f"ROECO {jur} {i}" for i in range(30)], jur=jur) for jur in jurs],
        ignore_index=True,
    )
    ocod.to_parquet(rd / "ocod_phase2.parquet")
    roe.to_parquet(rd / "roe_phase2.parquet")

    X, y = build_proxy_frame(str(rd))
    neg = X[y == 0]
    assert len(neg) > 0
    assert (neg["jurisdiction_match"] == 0.0).any()   # cross-jurisdiction support present
    assert (neg["jurisdiction_match"] == 1.0).any()   # same-jurisdiction still dominates
    assert (neg["jurisdiction_match"] == 1.0).mean() > 0.5


def test_old_model_without_jurisdiction_still_scores(run_dir):
    """Versioned backward compat: a model persisted BEFORE jurisdiction_match existed
    keeps scoring. The new feature computation returns the union; scoring selects the
    model's persisted column list, which drops the unseen feature (CONCERNS: schema drift)."""
    import json

    import lightgbm as lgb

    from app.pipeline.stage_2_5_gbt import run_stage_2_5_gbt

    old_cols = [c for c in FEATURE_COLS if c != "jurisdiction_match"]
    ocod = pd.read_parquet(os.path.join(run_dir, "ocod_phase2.parquet"))
    roe = pd.read_parquet(os.path.join(run_dir, "roe_phase2.parquet"))
    scored = pd.read_parquet(os.path.join(run_dir, "linkage_scored.parquet"))

    feats = build_features(scored, ocod, roe)[old_cols]
    y = (scored["match_probability"] > 0.7).astype(int).to_numpy()
    clf = lgb.LGBMClassifier(n_estimators=10, verbosity=-1)
    clf.fit(feats, y)
    clf.booster_.save_model(str(gbt_model.model_file()))
    gbt_model.feature_cols_file().write_text(json.dumps(old_cols), encoding="utf-8")

    result = run_stage_2_5_gbt(run_dir=run_dir)
    assert result["model"] is True
    out = pd.read_parquet(os.path.join(run_dir, "linkage_scored.parquet"))
    assert "gbt_score" in out.columns
    assert out["gbt_score"].between(0.0, 1.0).all()

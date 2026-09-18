"""Stage 2 is a purely unsupervised Splink candidate generator.

Covers the three architectural changes to candidate generation:
  1. Splink never trains from labels (labels train only the GBT) — its output is
     identical whether or not labels exist in the store.
  2. Splink predicts down to the candidate floor (not the review floor), so weak
     sub-review pairs are retained in linkage_scored.parquet.
  3. Blocking is jurisdiction OR name_core, so same-name records are compared across
     jurisdictions (and the UNKNOWN sentinel); jurisdiction is a real comparison with
     a NULL level for UNKNOWN.
"""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import write_db
from app.pipeline.stage_2_probabilistic_link import (
    _add_jurisdiction_cmp,
    _build_blocking_rule,
    _equality_cols,
    run_stage_2,
)

SPLINK_COLS = ["unique_id", "name_clean", "jurisdiction_clean",
               "name_digits_sorted", "name_core", "name_tokens_sorted"]

# The guarded jurisdiction rule keeps the UNKNOWN sentinel out of jurisdiction
# blocking; the name_core rule is what compares same-name records across
# jurisdictions (including UNKNOWN).
JURISDICTION_RULE = "l.jurisdiction_clean = r.jurisdiction_clean AND l.jurisdiction_clean <> 'UNKNOWN'"
NAME_CORE_RULE = "l.name_core = r.name_core AND l.name_core <> ''"


# ---------------------------------------------------------------------------
# Pure helpers (no Splink)
# ---------------------------------------------------------------------------


def test_build_blocking_rule_simple_equality_uses_block_on():
    from splink.blocking_rule_library import CustomRule

    simple = _build_blocking_rule("l.jurisdiction_clean = r.jurisdiction_clean")
    guarded = _build_blocking_rule(NAME_CORE_RULE)
    # A pure equality is a plain block_on; anything compound is a raw CustomRule.
    assert not isinstance(simple, CustomRule)
    assert isinstance(guarded, CustomRule)


def test_equality_cols_extracts_forced_equal_columns():
    assert _equality_cols(JURISDICTION_RULE) == {"jurisdiction_clean"}
    assert _equality_cols(NAME_CORE_RULE) == {"name_core"}
    # An OR of the two forces no single column equal across all pairs.
    assert _equality_cols(JURISDICTION_RULE) & _equality_cols(NAME_CORE_RULE) == set()


def test_add_jurisdiction_cmp_nulls_unknown_and_empty():
    df = pd.DataFrame({"jurisdiction_clean": ["JERSEY", "UNKNOWN", "", "  guernsey  "]})
    out = _add_jurisdiction_cmp(df.copy())
    cmp = out["jurisdiction_cmp"].tolist()
    assert cmp[0] == "JERSEY"
    assert pd.isna(cmp[1])         # UNKNOWN -> NULL (no evidence, treated as null by Splink)
    assert pd.isna(cmp[2])         # empty -> NULL
    assert cmp[3] == "GUERNSEY"    # trimmed + upper-cased


# ---------------------------------------------------------------------------
# Integration: a real (tiny) Splink run
# ---------------------------------------------------------------------------


def _rec(uid, name_clean, jur, core):
    return {"unique_id": uid, "name_clean": name_clean, "jurisdiction_clean": jur,
            "name_digits_sorted": "", "name_core": core,
            "name_tokens_sorted": " ".join(sorted(name_clean.split()))}


def _write_phase2(run_dir, config_dir, cross_jurisdiction=True):
    config_dir.mkdir(parents=True, exist_ok=True)
    # Focus group: identical names, only jurisdiction varies (share name_core ACME).
    ocod = [
        _rec("o-acme-jer", "ACME LTD", "JERSEY", "ACME"),
        _rec("o-acme-unk", "ACME LTD", "UNKNOWN", "ACME"),
    ]
    roe = [
        _rec("r-acme-jer", "ACME LTD", "JERSEY", "ACME"),     # jurisdiction agrees
        _rec("r-acme-gue", "ACME LTD", "GUERNSEY", "ACME"),   # jurisdiction mismatch
        _rec("r-acme-unk", "ACME LTD", "UNKNOWN", "ACME"),    # jurisdiction UNKNOWN
    ]
    # Background noise so EM / u-sampling has within-jurisdiction variety.
    for i, nm in enumerate(["BETA", "GAMMA", "DELTA", "EPSILON", "ZETA", "OMEGA", "SIGMA", "KAPPA"]):
        jur = ["JERSEY", "GUERNSEY"][i % 2]
        ocod.append(_rec(f"o-{nm}", f"{nm} LTD", jur, nm))
        roe.append(_rec(f"r-{nm}", f"{nm} LIMITED", jur, nm))

    pd.DataFrame(ocod).to_parquet(run_dir / "ocod_phase2.parquet")
    pd.DataFrame(roe).to_parquet(run_dir / "roe_phase2.parquet")

    blocking_rules = [JURISDICTION_RULE]
    if cross_jurisdiction:
        blocking_rules.append(NAME_CORE_RULE)
    settings = {
        "blocking_rules": blocking_rules,
        "comparisons": {
            "name_clean": {"splink_function": "cl.JaroWinklerAtThresholds",
                           "splink_args": {"score_threshold_or_thresholds": [0.9, 0.8]}},
            "name_core": {"splink_function": "cl.ExactMatch", "splink_args": {}},
            "jurisdiction_clean": {"splink_function": "cl.ExactMatch", "splink_args": {}},
        },
        "probability_two_random_records_match": 0.01,
        "match_probability_threshold_candidate": 0.01,
        "match_probability_threshold_review": 0.40,
        "match_probability_threshold_high": 0.90,
        "random_seed": 42,
    }
    (config_dir / "linkage_settings.json").write_text(json.dumps(settings))


def _run(run_dir, config_dir, cross_jurisdiction=True):
    _write_phase2(run_dir, config_dir, cross_jurisdiction=cross_jurisdiction)
    run_stage_2(str(run_dir), str(config_dir))
    return pd.read_parquet(run_dir / "linkage_scored.parquet")


def _pair(df, a, b):
    m = df[(df["unique_id_l"] == a) & (df["unique_id_r"] == b)]
    if len(m) == 0:
        m = df[(df["unique_id_l"] == b) & (df["unique_id_r"] == a)]
    return m.iloc[0] if len(m) else None


def test_name_core_rule_generates_cross_jurisdiction_pairs_without_double_counting(tmp_path):
    df = _run(tmp_path / "run", tmp_path / "run" / "config")
    # Cross-jurisdiction and UNKNOWN pairs only exist because of the name_core rule.
    assert _pair(df, "o-acme-jer", "r-acme-gue") is not None
    assert _pair(df, "o-acme-jer", "r-acme-unk") is not None
    # A pair captured by BOTH rules (same jurisdiction AND same name_core) appears once.
    key = df["unique_id_l"].astype(str) + "|" + df["unique_id_r"].astype(str)
    assert key.nunique() == len(df)


def test_without_name_rule_cross_jurisdiction_pairs_are_not_generated(tmp_path):
    df = _run(tmp_path / "run", tmp_path / "run" / "config", cross_jurisdiction=False)
    # Jurisdiction-only blocking: the cross-jurisdiction ACME pair cannot be compared.
    assert _pair(df, "o-acme-jer", "r-acme-gue") is None
    # UNKNOWN records reach the pool but block on nothing -> no pairs.
    assert _pair(df, "o-acme-jer", "r-acme-unk") is None


def test_jurisdiction_comparison_has_three_levels(tmp_path):
    """Agreement is positive, a genuine mismatch is negative, and UNKNOWN is a
    NULL/no-evidence level — an UNKNOWN pair scores the same as one with the
    jurisdiction absent from evidence, and strictly higher than a true mismatch."""
    df = _run(tmp_path / "run", tmp_path / "run" / "config")

    match = _pair(df, "o-acme-jer", "r-acme-jer")
    mismatch = _pair(df, "o-acme-jer", "r-acme-gue")
    unk_one = _pair(df, "o-acme-jer", "r-acme-unk")
    unk_both = _pair(df, "o-acme-unk", "r-acme-unk")
    assert all(p is not None for p in (match, mismatch, unk_one, unk_both))

    # Splink gamma levels: match=1, no-match=0, null=-1.
    assert match["gamma_jurisdiction_cmp"] == 1
    assert mismatch["gamma_jurisdiction_cmp"] == 0
    assert unk_one["gamma_jurisdiction_cmp"] == -1
    assert unk_both["gamma_jurisdiction_cmp"] == -1

    # UNKNOWN == "jurisdiction absent from evidence": identical score whether one or
    # both sides are UNKNOWN (the null level contributes no evidence either way).
    assert unk_one["match_probability"] == pytest.approx(unk_both["match_probability"])
    # Ordering: agreement > no-evidence > genuine mismatch.
    assert match["match_probability"] > unk_one["match_probability"]
    assert unk_one["match_probability"] > mismatch["match_probability"]


def test_predicts_down_to_candidate_floor_retaining_sub_review_pairs(tmp_path):
    """Weak pairs below the review floor (0.40) are retained in the parquet because
    Stage 2 predicts down to the candidate floor (0.01), not the review floor."""
    df = _run(tmp_path / "run", tmp_path / "run" / "config")
    assert (df["match_probability"] < 0.40).any()
    # Nothing below the candidate floor is emitted.
    assert (df["match_probability"] >= 0.01).all()


def test_labels_do_not_affect_stage_2_output(tmp_path, db_path):
    """Splink is purely unsupervised: its scored output is byte-identical whether or
    not TRUE labels exist in the store (labels train only the GBT)."""
    before = _run(tmp_path / "a", tmp_path / "a" / "config")

    # Add TRUE labels that, under the old supervised-m path, would have re-fit EM.
    for uid_ocod, roe_no in [("ACME LTD", "OE000001"), ("BETA LTD", "OE000002")]:
        write_db(
            db_path,
            """INSERT INTO labels
               (ocod_name_clean, jurisdiction_clean, roe_company_number, is_true_match, active)
               VALUES (?, 'JERSEY', ?, 'TRUE', 1)""",
            (uid_ocod, roe_no),
        )

    after = _run(tmp_path / "b", tmp_path / "b" / "config")

    cols = ["unique_id_l", "unique_id_r", "match_probability"]
    a = before[cols].sort_values(cols[:2]).reset_index(drop=True)
    b = after[cols].sort_values(cols[:2]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)

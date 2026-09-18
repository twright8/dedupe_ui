# backend/tests/test_entity_and_roe_export.py
"""Entity identity columns, the ROE-side merged export, and the pre/post-label counts.

These cover the three things the OCOD-only export could not answer: which distinct
entity a row belongs to, whether a registered ROE company owns any property we can
find, and what the matcher decided before human labels were overlaid.
"""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.pipeline.stage_3_evaluate import _add_entity_identity, _write_merged_roe


# ---------------------------------------------------------------------------
# Entity identity
# ---------------------------------------------------------------------------

def _merged_frame():
    return pd.DataFrame({
        "title_number": ["T1", "T2", "T3", "T4"],
        "name_clean": ["ALPHA LTD", "ALPHA LTD", "BETA LTD", "GAMMA LTD"],
        "jurisdiction_clean": ["JERSEY", "JERSEY", "BVI", "BVI"],
        # T1/T2 matched to the same company; T3 matched; T4 unmatched.
        "roe_company_number": ["OE000001", "OE000001", "OE000002", None],
        "match_probability": [1.0, 0.97, 0.95, None],
    })


def test_matched_rows_key_on_the_oe_number():
    df = _merged_frame()
    _add_entity_identity(df)
    assert df.loc[0, "entity_uid"] == "OE000001"
    assert df.loc[0, "entity_identified"] is True or bool(df.loc[0, "entity_identified"])
    # Two titles held by the same company collapse to one entity.
    assert df.loc[0, "entity_uid"] == df.loc[1, "entity_uid"]


def test_unmatched_rows_fall_back_to_a_name_key_and_are_flagged():
    df = _merged_frame()
    _add_entity_identity(df)
    assert df.loc[3, "entity_id"] == ""
    assert df.loc[3, "entity_uid"] == "NAME:GAMMA LTD|BVI"
    assert not bool(df.loc[3, "entity_identified"])


def test_distinct_entity_count_never_mixes_real_and_name_based_ids():
    df = _merged_frame()
    _add_entity_identity(df)
    ident = df[df["entity_identified"]]["entity_uid"].nunique()
    unident = df[~df["entity_identified"]]["entity_uid"].nunique()
    assert (ident, unident) == (2, 1)
    assert df["entity_uid"].nunique() == 3


def test_entity_identity_survives_missing_columns():
    """Legacy fixtures lack jurisdiction_clean — the key degrades, it does not raise."""
    df = pd.DataFrame({"name_clean": ["ALPHA LTD"], "roe_company_number": [None]})
    _add_entity_identity(df)
    assert df.loc[0, "entity_uid"] == "NAME:ALPHA LTD|"


# ---------------------------------------------------------------------------
# ROE-side merged export
# ---------------------------------------------------------------------------

def _roe_companies():
    return pd.DataFrame({
        "roe_company_number": ["OE000001", "OE000002", "OE000003"],
        "roe_name_raw": ["Alpha Ltd", "Beta Ltd", "Delta Ltd"],
        "roe_name_type": ["current", "current", "current"],
    })


def test_merged_roe_reports_every_company_with_its_matched_titles(tmp_path):
    merged = _merged_frame()
    n_matched = _write_merged_roe(tmp_path, merged, _roe_companies(), "splink")

    out = pd.read_csv(tmp_path / "merged_roe.csv", encoding="utf-8-sig")
    assert len(out) == 3, "every ROE company appears, matched or not"
    assert n_matched == 2

    by_num = out.set_index("roe_company_number")
    # OE000001 holds two titles, OE000002 one, OE000003 none.
    assert by_num.loc["OE000001", "matched_titles"] == 2
    assert by_num.loc["OE000002", "matched_titles"] == 1
    assert by_num.loc["OE000003", "matched_titles"] == 0
    assert bool(by_num.loc["OE000003", "is_matched"]) is False


def test_merged_roe_carries_best_probability_and_decision_model(tmp_path):
    _write_merged_roe(tmp_path, _merged_frame(), _roe_companies(), "gbt:4")
    out = pd.read_csv(tmp_path / "merged_roe.csv", encoding="utf-8-sig")
    by_num = out.set_index("roe_company_number")
    assert by_num.loc["OE000001", "best_match_probability"] == 1.0
    assert set(out["decision_model"]) == {"gbt:4"}


def test_merged_roe_handles_a_run_with_no_matches_at_all(tmp_path):
    merged = _merged_frame()
    merged["roe_company_number"] = None
    n_matched = _write_merged_roe(tmp_path, merged, _roe_companies(), "splink")
    out = pd.read_csv(tmp_path / "merged_roe.csv", encoding="utf-8-sig")
    assert n_matched == 0
    assert len(out) == 3
    assert out["matched_titles"].sum() == 0


# ---------------------------------------------------------------------------
# Pre/post-label counts
# ---------------------------------------------------------------------------

def test_refresh_counts_after_labels_updates_and_preserves_context(db_path, tmp_path):
    from app.db import query_db, write_db
    from app.services.pipeline_runner import refresh_counts_after_labels

    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    pd.DataFrame({
        "title_number": ["T1", "T2"],
        "roe_company_number": ["OE000001", "OE000002"],
        "match_method": ["exact", "probabilistic"],
    }).to_csv(run_dir / "merged_dataset.csv", index=False, encoding="utf-8-sig")

    stored = {
        "matched_titles": 1,                       # stale: written before labels landed
        "pre_labels": {"matched_titles": 1},       # the model-only baseline
        "decision_model": "gbt:2",                 # not derivable from the CSVs
        "labels_in_library": 52,
    }
    write_db(db_path, "INSERT INTO runs (id, status, counts_json) VALUES (?, ?, ?)",
             ("run_x", "complete", json.dumps(stored)))

    counts = refresh_counts_after_labels(db_path, str(run_dir), "run_x", {"applied": 3, "unmatched": 1})

    # Recomputed from the rewritten export...
    assert counts["matched_titles"] == 2
    assert counts["labels_applied"] == 3
    assert counts["labels_unmatched"] == 1
    # ...while everything the CSVs cannot supply survives.
    assert counts["pre_labels"] == {"matched_titles": 1}
    assert counts["decision_model"] == "gbt:2"
    assert counts["labels_in_library"] == 52

    # And it is persisted, not just returned — that was the original bug.
    row = query_db(db_path, "SELECT counts_json FROM runs WHERE id = ?", ("run_x",))[0]
    assert json.loads(row["counts_json"])["matched_titles"] == 2


def test_refresh_counts_survives_a_run_with_no_stored_counts(db_path, tmp_path):
    from app.db import write_db
    from app.services.pipeline_runner import refresh_counts_after_labels

    run_dir = tmp_path / "run_y"
    run_dir.mkdir()
    pd.DataFrame({"title_number": ["T1"], "roe_company_number": ["OE1"],
                  "match_method": ["exact"]}).to_csv(
        run_dir / "merged_dataset.csv", index=False, encoding="utf-8-sig")
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("run_y", "complete"))

    counts = refresh_counts_after_labels(db_path, str(run_dir), "run_y", {})
    assert counts["matched_titles"] == 1
    assert counts["labels_applied"] == 0


# ---------------------------------------------------------------------------
# Wiring: the full Stage 3 export actually produces these
# ---------------------------------------------------------------------------

def test_stage_3_writes_merged_roe_and_entity_columns(tmp_path):
    """The helpers are wired into the real export, not just unit-tested in isolation."""
    from tests.test_stage_3_gbt import _write_inputs
    from app.pipeline.stage_3_evaluate import run_stage_3_bucket_only

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=True)
    run_stage_3_bucket_only(str(run_dir), str(run_dir / "config"))

    merged = pd.read_csv(run_dir / "merged_dataset.csv", encoding="utf-8-sig")
    for col in ("entity_id", "entity_key", "entity_uid", "entity_identified"):
        assert col in merged.columns, f"{col} missing from merged_dataset.csv"

    assert (run_dir / "merged_roe.csv").exists()
    roe_out = pd.read_csv(run_dir / "merged_roe.csv", encoding="utf-8-sig")
    # Every ROE company in the fixture is present, matched or not.
    assert len(roe_out) == 3
    for col in ("matched_titles", "is_matched", "entity_id", "decision_model"):
        assert col in roe_out.columns


def test_rebucket_stores_a_pre_label_baseline(tmp_path, db_path):
    """A FALSE label suppresses a match; the baseline must still show the model's view."""
    from tests.test_stage_3_gbt import _write_inputs
    from app.db import query_db, write_db
    from app.services.pipeline_runner import rebucket_run

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_inputs(run_dir, run_dir / "config", gbt_enabled=True)
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)", ("run", "complete"))
    write_db(
        db_path,
        """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
                               is_true_match, reviewer, active, provenance, held_out)
           VALUES ('ALPHA LTD', 'ENGLAND', 'OE0000A', 'FALSE', 'tester', 1, 'manual', 0)""",
    )

    counts = rebucket_run(db_path, str(run_dir), str(run_dir / "config"), run_id="run",
                          threshold_high=0.9, threshold_review=0.4)

    assert "pre_labels" in counts, "no model-only baseline captured"
    # The FALSE label removes a match, so the post-label figure must be lower.
    assert counts["matched_proprietors"] < counts["pre_labels"]["matched_proprietors"]

    stored = json.loads(query_db(db_path, "SELECT counts_json FROM runs WHERE id = ?", ("run",))[0]["counts_json"])
    assert stored["pre_labels"]["matched_proprietors"] == counts["pre_labels"]["matched_proprietors"]

"""Stage 1 exact match: UNKNOWN-jurisdiction records are kept out of the exact join
but still flow into the Phase 2 pool, so Stage 2's name_core blocking rule can
compare (and rescue) them."""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pipeline.stage_1_exact_match import run_stage_1


def _write_inputs(run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    # OCOD dedup: one exact-matchable JERSEY row + one UNKNOWN-jurisdiction row.
    pd.DataFrame({
        "unique_id": [0, 1],
        "name_clean": ["ACME LTD", "MYSTERY LTD"],
        "jurisdiction_clean": ["JERSEY", "UNKNOWN"],
        "name_digits_sorted": ["", ""],
        "name_core": ["ACME", "MYSTERY"],
        "name_tokens_sorted": ["ACME LTD", "LTD MYSTERY"],
    }).to_parquet(run_dir / "ocod_dedup.parquet")

    pd.DataFrame({
        "unique_id": [0, 1],
        "name_clean": ["ACME LTD", "MYSTERY LTD"],
        "jurisdiction_clean": ["JERSEY", "UNKNOWN"],
        "roe_company_number": ["OE000001", "OE000002"],
        "roe_name_raw": ["Acme Ltd", "Mystery Ltd"],
        "roe_jurisdiction_raw": ["Jersey", ""],
    }).to_parquet(run_dir / "roe_preprocessed.parquet")


def test_unknown_records_excluded_from_exact_join_but_reach_phase_2(tmp_path):
    run_dir = tmp_path / "run"
    _write_inputs(run_dir)

    run_stage_1(str(run_dir), str(run_dir))

    exact = pd.read_parquet(run_dir / "exact_matches.parquet")
    ocod_p2 = pd.read_parquet(run_dir / "ocod_phase2.parquet")
    roe_p2 = pd.read_parquet(run_dir / "roe_phase2.parquet")

    # ACME exact-matched on (name, jurisdiction); MYSTERY (UNKNOWN) did not.
    assert set(exact["name_clean"]) == {"ACME LTD"}

    # The UNKNOWN row is NOT dropped — it flows into both Phase 2 pools so the
    # name_core blocking rule can still compare it.
    assert "MYSTERY LTD" in set(ocod_p2["name_clean"])
    assert "UNKNOWN" in set(ocod_p2["jurisdiction_clean"])
    assert "MYSTERY LTD" in set(roe_p2["name_clean"])
    assert "UNKNOWN" in set(roe_p2["jurisdiction_clean"])

    # The exact-matched ACME rows are consumed (not in Phase 2).
    assert "ACME LTD" not in set(ocod_p2["name_clean"])
    assert "OE000001" not in set(roe_p2["roe_company_number"])


def test_raw_ocod_columns_carry_through_to_phase2(tmp_path):
    """ocod_dedup carries a representative raw (name, jurisdiction) from Stage 0; Stage 1
    must pass those columns through to the phase-2 pool so raw-first label resolution works
    for the GBT-training path (which reads ocod_phase2.parquet), not just the cleaned key.
    The fixed-schema exact_matches.parquet stays unchanged."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame({
        "unique_id": [0, 1],
        "name_clean": ["ACME LTD", "MYSTERY LTD"],
        "jurisdiction_clean": ["JERSEY", "UNKNOWN"],
        "name_digits_sorted": ["", ""],
        "name_core": ["ACME", "MYSTERY"],
        "name_tokens_sorted": ["ACME LTD", "LTD MYSTERY"],
        "ocod_name_raw": ["ACME LTD", "MYSTERY LTD"],
        "ocod_jurisdiction_raw": ["JERSEY", ""],
    }).to_parquet(run_dir / "ocod_dedup.parquet")
    pd.DataFrame({
        "unique_id": [0, 1],
        "name_clean": ["ACME LTD", "MYSTERY LTD"],
        "jurisdiction_clean": ["JERSEY", "UNKNOWN"],
        "roe_company_number": ["OE000001", "OE000002"],
        "roe_name_raw": ["Acme Ltd", "Mystery Ltd"],
        "roe_jurisdiction_raw": ["Jersey", ""],
    }).to_parquet(run_dir / "roe_preprocessed.parquet")

    run_stage_1(str(run_dir), str(run_dir))

    ocod_p2 = pd.read_parquet(run_dir / "ocod_phase2.parquet")
    assert {"ocod_name_raw", "ocod_jurisdiction_raw"}.issubset(ocod_p2.columns)
    mystery = ocod_p2[ocod_p2["name_clean"] == "MYSTERY LTD"].iloc[0]
    assert mystery["ocod_name_raw"] == "MYSTERY LTD"

    # exact_matches.parquet keeps its fixed schema (no raw OCOD passthrough columns).
    exact = pd.read_parquet(run_dir / "exact_matches.parquet")
    assert "ocod_name_raw" not in exact.columns

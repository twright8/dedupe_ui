# backend/tests/test_match_grouping.py
"""Entity-centric and ROE-centric grouping readers."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.match_reader import get_matches_by_ocod, get_matches_by_roe


def _pair(ocod_id, roe_id, roe_no, prob, name="ACME LTD"):
    return {
        "ocod_unique_id": ocod_id, "roe_unique_id": roe_id,
        "ocod_name_raw": name.title(), "ocod_name_clean": name,
        "jurisdiction_clean": "JERSEY",
        "roe_name_raw": f"{roe_no} LIMITED", "roe_name_clean": name,
        "roe_jurisdiction_raw": "Jersey", "roe_company_number": roe_no,
        "match_probability": prob, "splink_probability": prob, "match_method": "probabilistic",
        "name_clean_l": name, "name_clean_r": name,
        "gamma_name_core": 1, "gamma_name_tokens_sorted": 1, "gamma_name_digits_sorted": 1,
    }


def _setup(run_dir):
    os.makedirs(run_dir, exist_ok=True)
    # OCOD X -> roe-1 (0.90, high) and roe-2 (0.70, review)  => margin 0.20
    # OCOD Y -> roe-1 (0.85, high)                            => roe-1 has 2 claimants
    pd.DataFrame([
        _pair("ocod-X", "roe-1", "OE0001", 0.90),
        _pair("ocod-Y", "roe-1", "OE0001", 0.85, name="ALPHA LTD"),
    ]).to_csv(os.path.join(run_dir, "matches_high_confidence.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame([
        _pair("ocod-X", "roe-2", "OE0002", 0.70),
    ]).to_csv(os.path.join(run_dir, "matches_for_review.csv"), index=False, encoding="utf-8-sig")


def test_by_ocod_groups_with_margin(tmp_path, db_path):
    run_dir = str(tmp_path / "run")
    _setup(run_dir)
    result = get_matches_by_ocod(run_dir, db_path)
    by_id = {e["ocod_unique_id"]: e for e in result["items"]}

    assert by_id["ocod-X"]["candidate_count"] == 2
    # ranked: top 0.90, second 0.70 -> margin 0.20
    assert by_id["ocod-X"]["candidates"][0]["match_probability"] == 0.90
    assert by_id["ocod-X"]["margin"] == pytest.approx(0.20)
    assert by_id["ocod-Y"]["candidate_count"] == 1
    assert by_id["ocod-Y"]["margin"] is None


def test_by_roe_groups_claimants(tmp_path, db_path):
    run_dir = str(tmp_path / "run")
    _setup(run_dir)
    result = get_matches_by_roe(run_dir, db_path)
    by_roe = {g["roe_company_number"]: g for g in result["items"]}

    # roe-1 is claimed by both OCOD X and Y (dedup signal); contested groups first.
    assert by_roe["OE0001"]["claimant_count"] == 2
    assert result["items"][0]["roe_company_number"] == "OE0001"
    assert by_roe["OE0002"]["claimant_count"] == 1

# backend/tests/test_label_applier.py
"""Tests for the label applier service."""

import os

import pandas as pd

from app.db import write_db
from app.services.label_applier import apply_labels
from datetime import datetime


def _make_merged_csv(run_dir):
    """Create a minimal merged_dataset.csv and supporting match CSVs."""
    os.makedirs(run_dir, exist_ok=True)
    df = pd.DataFrame([
        {"title_number": "T001", "ocod_name_raw": "Acme Ltd", "ocod_name_clean": "ACME LTD",
         "jurisdiction_clean": "JERSEY", "roe_company_number": "OE001234",
         "roe_name_raw": "ACME LIMITED", "match_method": "probabilistic", "match_probability": 0.65},
        {"title_number": "T002", "ocod_name_raw": "Beta Corp", "ocod_name_clean": "BETA CORP",
         "jurisdiction_clean": "BVI", "roe_company_number": "OE005678",
         "roe_name_raw": "BETA CORPORATION", "match_method": "probabilistic", "match_probability": 0.55},
        {"title_number": "T003", "ocod_name_raw": "Gamma SA", "ocod_name_clean": "GAMMA SA",
         "jurisdiction_clean": "JERSEY", "roe_company_number": "",
         "roe_name_raw": "", "match_method": "", "match_probability": ""},
    ])
    df.to_csv(os.path.join(run_dir, "merged_dataset.csv"), index=False, encoding="utf-8-sig")
    # Also create matches_high_confidence.csv and matches_exact.csv for matches_final
    df[df["match_method"] == "exact"].to_csv(
        os.path.join(run_dir, "matches_exact.csv"), index=False, encoding="utf-8-sig")
    df[df["match_probability"] != ""].to_csv(
        os.path.join(run_dir, "matches_high_confidence.csv"), index=False, encoding="utf-8-sig")


def _insert_label(db_path, clean, jur, roe, raw_name, raw_jur, verdict):
    """Insert a label row into the test database."""
    write_db(db_path, """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
        ocod_name_raw, ocod_jurisdiction_raw, is_true_match, reviewer, reviewer_notes, created_at, run_id, active)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (clean, jur, roe, raw_name, raw_jur, verdict, "Tom", "", datetime.now().isoformat(), "run_test", 1))


def test_apply_true_label(tmp_path, db_path):
    """A TRUE label sets match_method to reviewed_true_review."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "ACME LTD", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "TRUE")
    result = apply_labels(db_path, run_dir)
    assert result["applied"] >= 1
    # Verify the CSV was updated
    df = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    acme = df[df["title_number"] == "T001"].iloc[0]
    assert acme["match_method"] == "reviewed_true_review"


def test_apply_label_to_pipeline_merged_name_clean_alias(tmp_path, db_path):
    """Current merged_dataset.csv uses name_clean, not ocod_name_clean."""
    run_dir = str(tmp_path / "run_test")
    os.makedirs(run_dir, exist_ok=True)
    df = pd.DataFrame([
        {"title_number": "T001", "ocod_name_raw": "Acme Ltd", "name_clean": "ACME LTD",
         "jurisdiction_clean": "JERSEY", "roe_company_number": "OE001234",
         "match_method": "probabilistic"},
        {"title_number": "T002", "ocod_name_raw": "Acme Ltd", "name_clean": "ACME LTD",
         "jurisdiction_clean": "JERSEY", "roe_company_number": "OE001234",
         "match_method": "probabilistic"},
    ])
    df.to_csv(os.path.join(run_dir, "merged_dataset.csv"), index=False, encoding="utf-8-sig")
    df.iloc[:0].to_csv(os.path.join(run_dir, "matches_exact.csv"), index=False, encoding="utf-8-sig")
    df.iloc[:0].to_csv(os.path.join(run_dir, "matches_high_confidence.csv"), index=False, encoding="utf-8-sig")

    _insert_label(db_path, "ACME LTD", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "TRUE")
    result = apply_labels(db_path, run_dir)

    assert result["applied"] == 1
    updated = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    assert set(updated["match_method"]) == {"reviewed_true_review"}


def test_apply_true_review_label_adds_unmatched_candidate(tmp_path, db_path):
    """A TRUE review-band label can add an OE number not already in merged_dataset."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    review = pd.DataFrame([
        {
            "match_method": "probabilistic",
            "match_probability": "0.61",
            "ocod_name_raw": "Gamma SA",
            "ocod_name_clean": "GAMMA SA",
            "jurisdiction_clean": "JERSEY",
            "roe_name_raw": "GAMMA HOLDINGS SA",
            "roe_company_number": "OE009999",
        }
    ])
    review.to_csv(os.path.join(run_dir, "matches_for_review.csv"), index=False, encoding="utf-8-sig")

    _insert_label(db_path, "GAMMA SA", "JERSEY", "OE009999", "Gamma SA", "JERSEY", "TRUE")
    result = apply_labels(db_path, run_dir)

    assert result["applied"] == 1
    updated = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    gamma = updated[updated["title_number"] == "T003"].iloc[0]
    assert gamma["roe_company_number"] == "OE009999"
    assert gamma["roe_name_raw"] == "GAMMA HOLDINGS SA"
    assert gamma["match_method"] == "reviewed_true_review"
    assert float(gamma["match_probability"]) == 0.61


def test_apply_ambiguous_true_switches_best_candidate(tmp_path, db_path):
    """Ambiguous decisions can switch the merged dataset to the chosen candidate."""
    run_dir = str(tmp_path / "run_test")
    os.makedirs(run_dir, exist_ok=True)
    merged = pd.DataFrame([
        {
            "title_number": "T010",
            "ocod_name_raw": "Switch Ltd",
            "name_clean": "SWITCH LTD",
            "jurisdiction_clean": "BVI",
            "roe_company_number": "OE000001",
            "roe_name_raw": "SWITCH ONE LTD",
            "match_method": "probabilistic",
            "match_probability": "0.95",
            "match_count": "2",
            "is_ambiguous": "True",
        }
    ])
    merged.to_csv(os.path.join(run_dir, "merged_dataset.csv"), index=False, encoding="utf-8-sig")
    merged.to_csv(os.path.join(run_dir, "matches_high_confidence.csv"), index=False, encoding="utf-8-sig")
    merged.iloc[:0].to_csv(os.path.join(run_dir, "matches_exact.csv"), index=False, encoding="utf-8-sig")
    ambiguous = pd.DataFrame([
        {
            "match_method": "probabilistic",
            "match_probability": "0.95",
            "ocod_name_raw": "Switch Ltd",
            "ocod_name_clean": "SWITCH LTD",
            "jurisdiction_clean": "BVI",
            "roe_name_raw": "SWITCH ONE LTD",
            "roe_company_number": "OE000001",
        },
        {
            "match_method": "probabilistic",
            "match_probability": "0.94",
            "ocod_name_raw": "Switch Ltd",
            "ocod_name_clean": "SWITCH LTD",
            "jurisdiction_clean": "BVI",
            "roe_name_raw": "SWITCH TWO LTD",
            "roe_company_number": "OE000002",
        },
    ])
    ambiguous.to_csv(os.path.join(run_dir, "matches_ambiguous.csv"), index=False, encoding="utf-8-sig")

    _insert_label(db_path, "SWITCH LTD", "BVI", "OE000001", "Switch Ltd", "BVI", "FALSE")
    _insert_label(db_path, "SWITCH LTD", "BVI", "OE000002", "Switch Ltd", "BVI", "TRUE")
    result = apply_labels(db_path, run_dir)

    assert result["applied"] == 2
    updated = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    row = updated.iloc[0]
    assert row["roe_company_number"] == "OE000002"
    assert row["roe_name_raw"] == "SWITCH TWO LTD"
    assert row["match_method"] == "reviewed_true_ambiguous"
    assert float(row["match_probability"]) == 0.94


def test_apply_false_label(tmp_path, db_path):
    """A FALSE label clears the match (roe_company_number and match_method emptied)."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "BETA CORP", "BVI", "OE005678", "Beta Corp", "BVI", "FALSE")
    result = apply_labels(db_path, run_dir)
    assert result["applied"] >= 1
    df = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    beta = df[df["title_number"] == "T002"].iloc[0]
    assert beta["roe_company_number"] == "" or pd.isna(beta["roe_company_number"])
    assert beta["match_method"] == "" or pd.isna(beta["match_method"])

    df_final = pd.read_csv(os.path.join(run_dir, "matches_final.csv"), encoding="utf-8-sig")
    assert "OE005678" not in df_final["roe_company_number"].astype(str).values


def test_fallback_to_raw_key(tmp_path, db_path):
    """When cleaned key does not match, fall back to raw name + raw jurisdiction."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    # Label with DIFFERENT cleaned name (simulating a rule change)
    _insert_label(db_path, "ACME COMPANY", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "TRUE")
    result = apply_labels(db_path, run_dir)
    assert result["applied"] >= 1  # Should match via raw fallback
    df = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    acme = df[df["title_number"] == "T001"].iloc[0]
    assert acme["match_method"] == "reviewed_true_review"


def test_candidate_lookup_survives_rule_change(tmp_path, db_path):
    """The candidate lookup (which decides reviewed_true_ambiguous vs _review and
    carries the ROE name across) also resolves raw-first: a label whose stored
    clean key went stale still finds its ambiguous-bucket candidate."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    ambiguous = pd.DataFrame([
        {
            "match_method": "probabilistic",
            "match_probability": "0.62",
            "ocod_name_raw": "Gamma SA",
            "ocod_name_clean": "GAMMA SA",     # NEW rules
            "jurisdiction_clean": "JERSEY",
            "roe_name_raw": "GAMMA GROUP SA",
            "roe_company_number": "OE009998",
        }
    ])
    ambiguous.to_csv(os.path.join(run_dir, "matches_ambiguous.csv"), index=False, encoding="utf-8-sig")

    # Label stored under the OLD rules: stale clean key, stable raw identity.
    _insert_label(db_path, "GAMMA S A", "JERSEY", "OE009998", "Gamma SA", "JERSEY", "TRUE")
    result = apply_labels(db_path, run_dir)

    assert result["applied"] == 1
    updated = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    gamma = updated[updated["title_number"] == "T003"].iloc[0]
    assert gamma["roe_company_number"] == "OE009998"
    assert gamma["roe_name_raw"] == "GAMMA GROUP SA"
    assert gamma["match_method"] == "reviewed_true_ambiguous"


def test_unmatched_labels_counted(tmp_path, db_path):
    """Labels that don't match any row are counted as unmatched."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "NONEXISTENT", "NOWHERE", "OE999999", "Nonexistent", "Nowhere", "TRUE")
    result = apply_labels(db_path, run_dir)
    assert result["unmatched"] >= 1


def test_matches_final_csv_written(tmp_path, db_path):
    """apply_labels writes matches_final.csv and matches_user_confirmed.csv."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "ACME LTD", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "TRUE")
    apply_labels(db_path, run_dir)
    assert os.path.exists(os.path.join(run_dir, "matches_final.csv"))
    assert os.path.exists(os.path.join(run_dir, "matches_user_confirmed.csv"))


def test_matches_final_contains_exact_and_high_and_reviewed(tmp_path, db_path):
    """matches_final.csv includes exact, high-confidence, and user-confirmed TRUE rows."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "ACME LTD", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "TRUE")
    apply_labels(db_path, run_dir)
    df_final = pd.read_csv(os.path.join(run_dir, "matches_final.csv"), encoding="utf-8-sig")
    # ACME was marked TRUE so should appear
    assert "OE001234" in df_final["roe_company_number"].values


def test_matches_user_confirmed_only_reviewed(tmp_path, db_path):
    """matches_user_confirmed.csv contains only rows confirmed TRUE by reviewers."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "ACME LTD", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "TRUE")
    _insert_label(db_path, "BETA CORP", "BVI", "OE005678", "Beta Corp", "BVI", "FALSE")
    apply_labels(db_path, run_dir)
    df_uc = pd.read_csv(os.path.join(run_dir, "matches_user_confirmed.csv"), encoding="utf-8-sig")
    assert len(df_uc) == 1
    assert df_uc.iloc[0]["roe_company_number"] == "OE001234"


def test_no_labels_returns_zero_applied(tmp_path, db_path):
    """When no labels exist, applied=0 and unmatched=0."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    result = apply_labels(db_path, run_dir)
    assert result["applied"] == 0
    assert result["unmatched"] == 0


def test_case_insensitive_verdict(tmp_path, db_path):
    """Verdicts stored as 'true'/'false' (lowercase) are handled correctly."""
    run_dir = str(tmp_path / "run_test")
    _make_merged_csv(run_dir)
    _insert_label(db_path, "ACME LTD", "JERSEY", "OE001234", "Acme Ltd", "JERSEY", "true")
    result = apply_labels(db_path, run_dir)
    assert result["applied"] >= 1
    df = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    acme = df[df["title_number"] == "T001"].iloc[0]
    assert acme["match_method"] == "reviewed_true_review"

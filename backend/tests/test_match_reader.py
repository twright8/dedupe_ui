# backend/tests/test_match_reader.py
import os
from datetime import datetime

import pandas as pd

from app.db import write_db
from app.services.match_reader import get_matches


def _make_review_csv(run_dir):
    """Create a minimal matches_for_review.csv for testing."""
    os.makedirs(run_dir, exist_ok=True)
    df = pd.DataFrame([
        {"ocod_name_raw": "ACME LTD", "ocod_name_clean": "ACME LTD", "roe_name_raw": "ACME LIMITED",
         "roe_company_number": "OE001234", "jurisdiction_clean": "JERSEY",
         "match_probability": 0.65, "name_clean_l": "ACME LTD", "name_clean_r": "ACME LTD",
         "gamma_name_core": 1, "gamma_name_tokens_sorted": 1, "gamma_name_digits_sorted": 1},
        {"ocod_name_raw": "BETA CORP", "ocod_name_clean": "BETA CORP", "roe_name_raw": "BETA CORPORATION",
         "roe_company_number": "OE005678", "jurisdiction_clean": "BVI",
         "match_probability": 0.55, "name_clean_l": "BETA CORP", "name_clean_r": "BETA CORPORATION",
         "gamma_name_core": 0, "gamma_name_tokens_sorted": 0, "gamma_name_digits_sorted": 1},
        {"ocod_name_raw": "GAMMA SA", "ocod_name_clean": "GAMMA SA", "roe_name_raw": "GAMMA SA",
         "roe_company_number": "OE009012", "jurisdiction_clean": "JERSEY",
         "match_probability": 0.72, "name_clean_l": "GAMMA SA", "name_clean_r": "GAMMA SA",
         "gamma_name_core": 1, "gamma_name_tokens_sorted": 1, "gamma_name_digits_sorted": 1},
    ])
    df.to_csv(os.path.join(run_dir, "matches_for_review.csv"), index=False, encoding="utf-8-sig")


def test_get_matches_basic(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    result = get_matches(run_dir, db_path, bucket="review")
    assert result["total"] == 3
    assert len(result["items"]) == 3
    assert "features" in result["items"][0]


def test_get_matches_jurisdiction_filter(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    result = get_matches(run_dir, db_path, bucket="review", jurisdiction="JERSEY")
    assert result["total"] == 2


def test_get_matches_search_filter(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    result = get_matches(run_dir, db_path, bucket="review", search="BETA")
    assert result["total"] == 1
    assert result["items"][0]["ocod_name_raw"] == "BETA CORP"


def test_get_matches_pagination(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    result = get_matches(run_dir, db_path, bucket="review", page=1, per_page=2)
    assert len(result["items"]) == 2
    assert result["total"] == 3
    assert result["total_pages"] == 2


def test_get_matches_with_labels(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    # Insert a label
    write_db(db_path, """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
        ocod_name_raw, ocod_jurisdiction_raw, is_true_match, reviewer, reviewer_notes, created_at, run_id, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("ACME LTD", "JERSEY", "OE001234", "ACME LTD", "JERSEY", "TRUE", "Tom", "confirmed", datetime.now().isoformat(), "run_test", 1))
    result = get_matches(run_dir, db_path, bucket="review")
    acme = [r for r in result["items"] if r["roe_company_number"] == "OE001234"][0]
    assert acme["label"] == "TRUE"
    assert acme["label_reviewer"] == "Tom"


def _make_address_parquets(run_dir):
    """Write the preprocessed parquet artifacts the address panel joins against.

    Keyed to the review CSV: ACME (agreeing postcode + town), BETA (differing
    postcode, absent town), GAMMA (no comparable fields either side).
    """
    os.makedirs(run_dir, exist_ok=True)
    ocod = pd.DataFrame([
        {"name_clean": "ACME LTD", "jurisdiction_clean": "JERSEY",
         "ocod_address_1": "1 KING STREET", "ocod_address_2": "LONDON", "ocod_address_3": "SW1A 1AA"},
        {"name_clean": "BETA CORP", "jurisdiction_clean": "BVI",
         "ocod_address_1": "5 QUEEN ROAD", "ocod_address_2": "MANCHESTER", "ocod_address_3": "M1 2AB"},
        {"name_clean": "GAMMA SA", "jurisdiction_clean": "JERSEY",
         "ocod_address_1": "OVERSEAS PO BOX 123", "ocod_address_2": "", "ocod_address_3": ""},
    ])
    ocod.to_parquet(os.path.join(run_dir, "ocod_preprocessed.parquet"), index=False)
    roe = pd.DataFrame([
        {"roe_company_number": "OE001234", "roe_address_line1": "1 KING STREET",
         "roe_post_town": "LONDON", "roe_postcode": "SW1A 1AA"},
        {"roe_company_number": "OE005678", "roe_address_line1": "ROAD TOWN",
         "roe_post_town": "ROAD TOWN", "roe_postcode": "VG1110"},
        {"roe_company_number": "OE009012", "roe_address_line1": "",
         "roe_post_town": "", "roe_postcode": ""},
    ])
    roe.to_parquet(os.path.join(run_dir, "roe_preprocessed.parquet"), index=False)


def _by_number(result):
    return {r["roe_company_number"]: r for r in result["items"]}


def test_address_fields_present_on_review_rows(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    _make_address_parquets(run_dir)
    result = get_matches(run_dir, db_path, bucket="review")
    for item in result["items"]:
        assert "address" in item
        assert set(item["address"]) == {"ocod", "roe", "postcode_match", "town_match"}
    acme = _by_number(result)["OE001234"]["address"]
    assert acme["ocod"]["postcode"] == "SW1A 1AA"
    assert acme["ocod"]["lines"] == ["1 KING STREET", "LONDON", "SW1A 1AA"]
    assert acme["roe"]["post_town"] == "LONDON"
    assert acme["roe"]["postcode"] == "SW1A 1AA"


def test_address_match_flags_true(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    _make_address_parquets(run_dir)
    result = get_matches(run_dir, db_path, bucket="review")
    acme = _by_number(result)["OE001234"]["address"]
    assert acme["postcode_match"] is True
    assert acme["town_match"] is True


def test_address_match_flags_false(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    _make_address_parquets(run_dir)
    result = get_matches(run_dir, db_path, bucket="review")
    beta = _by_number(result)["OE005678"]["address"]
    # OCOD extracts "M1 2AB"; ROE registered postcode is "VG1110" -> differ.
    assert beta["postcode_match"] is False
    # "ROAD TOWN" is not a substring of the Manchester service address.
    assert beta["town_match"] is False


def test_address_match_flags_null(tmp_path, db_path):
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    _make_address_parquets(run_dir)
    result = get_matches(run_dir, db_path, bucket="review")
    gamma = _by_number(result)["OE009012"]["address"]
    # ROE has no postcode/town and OCOD address has no postcode -> nothing to compare.
    assert gamma["postcode_match"] is None
    assert gamma["town_match"] is None


def test_address_missing_artifacts_tolerated(tmp_path, db_path):
    """A run created before this change (no preprocessed parquet) must not 500;
    the address block is present with null/empty fields."""
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)  # deliberately no parquet artifacts
    result = get_matches(run_dir, db_path, bucket="review")
    assert result["total"] == 3
    addr = result["items"][0]["address"]
    assert addr["ocod"]["text"] is None
    assert addr["ocod"]["lines"] == []
    assert addr["roe"]["text"] is None
    assert addr["postcode_match"] is None
    assert addr["town_match"] is None


def test_get_matches_label_survives_rule_change(tmp_path, db_path):
    """Display path: a label whose stored clean key predates a cleaning-rule
    change still shows on the review panel, resolved via the raw identity."""
    run_dir = str(tmp_path / "run_test")
    _make_review_csv(run_dir)
    # Stored under the OLD rules: clean key no longer matches any run row, the
    # raw name still does ("BETA CORP" raw in the review CSV).
    write_db(db_path, """INSERT INTO labels (ocod_name_clean, jurisdiction_clean, roe_company_number,
        ocod_name_raw, ocod_jurisdiction_raw, is_true_match, reviewer, reviewer_notes, created_at, run_id, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("BETA CORPORATION", "BVI", "OE005678", "BETA CORP", "BVI", "FALSE", "Alice", "", datetime.now().isoformat(), "run_old", 1))
    result = get_matches(run_dir, db_path, bucket="review")
    beta = [r for r in result["items"] if r["roe_company_number"] == "OE005678"][0]
    assert beta["label"] == "FALSE"
    assert beta["label_reviewer"] == "Alice"

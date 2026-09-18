# backend/tests/test_feature_mapper.py
from app.services.feature_mapper import compute_jw, map_features


def test_compute_jw_identical():
    assert compute_jw("ACME LTD", "ACME LTD") == 1.0


def test_compute_jw_similar():
    score = compute_jw("ACME HOLDING LTD", "ACME HOLDINGS LTD")
    assert 0.9 < score < 1.0


def test_compute_jw_different():
    score = compute_jw("ACME LTD", "BETA CORP")
    assert score < 0.7


def test_compute_jw_empty_strings():
    # jellyfish.jaro_winkler_similarity("", "") == 0.0
    assert compute_jw("", "") == 0.0


def test_map_features_all_exact():
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "ACME LTD",
        "gamma_name_core": 1,
        "gamma_name_tokens_sorted": 1,
        "gamma_name_digits_sorted": 1,
        "jurisdiction_clean": "JERSEY",
        "roe_jurisdiction_clean": "JERSEY",
    }
    features = map_features(row)
    assert features["name_jw"] == 1.0
    assert features["name_core"] == 1.0
    assert features["tokens_sorted"] == 1.0
    assert features["digits"] == 1.0
    assert features["jurisdiction"] == 1.0


def test_map_features_partial():
    row = {
        "name_clean_l": "ACME HOLDING LTD",
        "name_clean_r": "ACME HOLDINGS LTD",
        "gamma_name_core": 0,
        "gamma_name_tokens_sorted": 1,
        "gamma_name_digits_sorted": 1,
    }
    features = map_features(row)
    assert 0.9 < features["name_jw"] < 1.0
    assert features["name_core"] == 0.0
    assert features["tokens_sorted"] == 1.0


def test_map_features_missing_gammas():
    row = {"name_clean_l": "ACME LTD", "name_clean_r": "ACME LTD"}
    features = map_features(row)
    assert features["name_jw"] == 1.0
    assert features["name_core"] == 1.0


def test_map_features_jurisdiction_match():
    """Same cleaned jurisdiction on both sides -> full agreement (1.0)."""
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "ACME LTD",
        "jurisdiction_clean": "JERSEY",
        "roe_jurisdiction_clean": "JERSEY",
    }
    assert map_features(row)["jurisdiction"] == 1.0


def test_map_features_jurisdiction_mismatch():
    """Genuinely different jurisdictions (a cross-jurisdiction name_core block) -> 0.0."""
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "ACME LTD",
        "jurisdiction_clean": "JERSEY",
        "roe_jurisdiction_clean": "BVI",
    }
    assert map_features(row)["jurisdiction"] == 0.0


def test_map_features_jurisdiction_unknown_neutral():
    """An UNKNOWN sentinel on either side is no-evidence -> neutral 0.5."""
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "ACME LTD",
        "jurisdiction_clean": "JERSEY",
        "roe_jurisdiction_clean": "UNKNOWN",
    }
    assert map_features(row)["jurisdiction"] == 0.5


def test_map_features_jurisdiction_raw_fallback():
    """Runs predating roe_jurisdiction_clean fall back to the raw ROE jurisdiction."""
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "ACME LTD",
        "jurisdiction_clean": "JERSEY",
        "roe_jurisdiction_raw": "jersey",  # cased/whitespace-normalised by the helper
    }
    assert map_features(row)["jurisdiction"] == 1.0


def test_map_features_missing_jurisdiction_is_neutral():
    """With no jurisdiction fields at all (minimal fixtures / Phase 1 CSVs), the
    honest three-state answer is neutral 0.5, not a fabricated agreement."""
    row = {"name_clean_l": "ALPHA CORP", "name_clean_r": "ALPHA CORP"}
    features = map_features(row)
    assert features["name_jw"] == 1.0
    assert features["name_core"] == 1.0
    assert features["tokens_sorted"] == 1.0
    assert features["digits"] == 1.0
    assert features["jurisdiction"] == 0.5


def test_map_features_gamma_zero_gives_zero():
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "BETA CORP",
        "gamma_name_core": 0,
        "gamma_name_tokens_sorted": 0,
        "gamma_name_digits_sorted": 0,
    }
    features = map_features(row)
    assert features["name_core"] == 0.0
    assert features["tokens_sorted"] == 0.0
    assert features["digits"] == 0.0


def test_map_features_gamma_nonzero_non_one_gives_zero():
    """Intermediate Splink levels (e.g. 2 out of 3) are not exact, so map to 0.0."""
    row = {
        "name_clean_l": "ACME LTD",
        "name_clean_r": "ACME LIMITED",
        "gamma_name_core": 2,
        "gamma_name_tokens_sorted": 2,
        "gamma_name_digits_sorted": 2,
        # Splink highest level for these comparisons is 3 (exact)
        # Level 2 = high similarity, not exact → 0.0
    }
    # We need to know the max level; the spec says "highest value = exact match"
    # Without knowing the max, level != 1 means "not the highest"; spec says
    # gamma == 1 → exact only when max is 1. For multi-level comparisons the
    # task description specifically says gamma==1 means exact for these columns.
    features = map_features(row)
    # gamma == 2 is not 1, so these map to 0.0 per the spec
    assert features["name_core"] == 0.0
    assert features["tokens_sorted"] == 0.0
    assert features["digits"] == 0.0

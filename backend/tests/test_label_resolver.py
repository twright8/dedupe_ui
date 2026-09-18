# backend/tests/test_label_resolver.py
"""Tests for the shared label resolver — the single source of truth that keeps
the review-panel display, the export applier and GBT training resolving labels
identically (raw key first, stored clean key as the legacy fallback)."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.db import write_db
from app.services.label_resolver import (
    entity_key_variants,
    find_dataframe_rows,
    lookup_active_label,
)


def _insert_label(db_path, **kw):
    cols = ", ".join(kw.keys())
    qs = ", ".join("?" for _ in kw)
    return write_db(db_path, f"INSERT INTO labels ({cols}) VALUES ({qs})", tuple(kw.values()))


def test_entity_key_variants_ordered_unique():
    variants = entity_key_variants("acme ltd", "england", "Acme Ltd.", "England")
    # Raw key first (the durable identity), clean fallback after; normalised to
    # upper; no duplicates.
    assert variants[0] == ("ACME LTD.", "ENGLAND")
    assert ("ACME LTD", "ENGLAND") in variants
    assert len(variants) == len(set(variants))


def test_lookup_by_clean_key(db_path):
    _insert_label(
        db_path,
        ocod_name_clean="ACME LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="OE000001",
        ocod_name_raw="Acme Limited",
        ocod_jurisdiction_raw="England",
        is_true_match="TRUE",
        reviewer="Tom",
        active=1,
    )
    row = lookup_active_label(db_path, "ACME LTD", "ENGLAND", "OE000001")
    assert row is not None
    assert row["is_true_match"] == "TRUE"


def test_lookup_falls_back_to_raw_key(db_path):
    """A label survives a cleaning-rule change: the cleaned key shifted but the
    raw key is stable, so display resolves it just like the export does."""
    _insert_label(
        db_path,
        ocod_name_clean="ACME LIMITED",   # produced by the OLD cleaning rules
        jurisdiction_clean="ENGLAND",
        roe_company_number="OE000002",
        ocod_name_raw="Acme Limited",     # stable raw value
        ocod_jurisdiction_raw="England",
        is_true_match="FALSE",
        reviewer="Tom",
        active=1,
    )
    # The record now cleans to a DIFFERENT name (new rules) but carries the same raw.
    row = lookup_active_label(
        db_path,
        "ACME LTD",            # new clean key — would have missed under clean-only lookup
        "ENGLAND",
        "OE000002",
        name_raw="Acme Limited",
        jur_raw="England",
    )
    assert row is not None
    assert row["is_true_match"] == "FALSE"


def test_lookup_ignores_inactive(db_path):
    _insert_label(
        db_path,
        ocod_name_clean="GONE LTD",
        jurisdiction_clean="ENGLAND",
        roe_company_number="OE000003",
        is_true_match="TRUE",
        reviewer="Tom",
        active=0,
    )
    assert lookup_active_label(db_path, "GONE LTD", "ENGLAND", "OE000003") is None


def test_find_dataframe_rows_clean_and_raw():
    df = pd.DataFrame(
        [
            {"name_clean": "ACME LTD", "jurisdiction_clean": "ENGLAND",
             "ocod_name_raw": "Acme Limited", "roe_company_number": "OE1"},
            {"name_clean": "OTHER LTD", "jurisdiction_clean": "ENGLAND",
             "ocod_name_raw": "Other Ltd", "roe_company_number": ""},
        ]
    )
    # Clean key hit
    idx = find_dataframe_rows(df, {"ocod_name_clean": "ACME LTD", "jurisdiction_clean": "ENGLAND"})
    assert idx == [0]
    # Raw key hit (clean key absent from the label)
    idx2 = find_dataframe_rows(
        df,
        {"ocod_name_clean": "", "jurisdiction_clean": "",
         "ocod_name_raw": "Acme Limited", "ocod_jurisdiction_raw": "ENGLAND"},
    )
    assert idx2 == [0]


def test_find_dataframe_rows_prefers_raw_over_clean():
    """When a label's raw key and stale clean key point at DIFFERENT rows, the
    raw key (the durable identity) must win."""
    df = pd.DataFrame(
        [
            {"name_clean": "ACME UK LTD", "jurisdiction_clean": "ENGLAND",
             "ocod_name_raw": "Acme (UK) Limited"},
            # A different entity whose NEW clean name happens to equal the
            # label's stale stored clean key.
            {"name_clean": "ACME UK LIMITED", "jurisdiction_clean": "ENGLAND",
             "ocod_name_raw": "Acme U.K. Limited"},
        ]
    )
    label = {
        "ocod_name_clean": "ACME UK LIMITED",   # stale (old cleaning rules)
        "jurisdiction_clean": "ENGLAND",
        "ocod_name_raw": "Acme (UK) Limited",   # stable raw identity
        "ocod_jurisdiction_raw": "England",
    }
    assert find_dataframe_rows(df, label) == [0]


# ---------------------------------------------------------------------------
# One rule-change fixture, three resolution paths (display / export / training)
# ---------------------------------------------------------------------------

_RAW_NAME = "Acme (UK) Limited"
_RAW_JUR = "England"
_OLD_CLEAN = "ACME UK LIMITED"   # produced by the OLD cleaning rules (stored on the label)
_NEW_CLEAN = "ACME UK LTD"       # produced by the NEW rules (in the run artifacts)
_ROE_NO = "OE777001"


def _entity_frame(prefix, name_clean, **extra):
    row = {
        "unique_id": f"{prefix}-0",
        "name_clean": name_clean,
        "jurisdiction_clean": "ENGLAND",
        "name_core": name_clean.replace(" LTD", "").replace(" LIMITED", ""),
        "name_tokens_sorted": " ".join(sorted(name_clean.split())),
        "name_digits_sorted": "",
        **extra,
    }
    return pd.DataFrame([row])


def _rule_change_run(tmp_path, db_path):
    """A run produced under NEW cleaning rules plus a label written under the
    OLD rules: the clean keys differ, the raw identity is unchanged."""
    run_dir = str(tmp_path / "run_rule_change")
    os.makedirs(run_dir, exist_ok=True)

    review = pd.DataFrame([
        {"ocod_name_raw": _RAW_NAME, "ocod_name_clean": _NEW_CLEAN,
         "jurisdiction_clean": "ENGLAND", "roe_name_raw": "ACME UK LIMITED",
         "roe_company_number": _ROE_NO, "match_probability": 0.7},
    ])
    review.to_csv(os.path.join(run_dir, "matches_for_review.csv"),
                  index=False, encoding="utf-8-sig")

    merged = pd.DataFrame([
        {"title_number": "T001", "ocod_name_raw": _RAW_NAME,
         "ocod_jurisdiction_raw": _RAW_JUR, "name_clean": _NEW_CLEAN,
         "jurisdiction_clean": "ENGLAND", "roe_company_number": _ROE_NO,
         "roe_name_raw": "ACME UK LIMITED", "match_method": "probabilistic",
         "match_probability": 0.7},
    ])
    merged.to_csv(os.path.join(run_dir, "merged_dataset.csv"),
                  index=False, encoding="utf-8-sig")

    _insert_label(
        db_path,
        ocod_name_clean=_OLD_CLEAN,          # stale under the new rules
        jurisdiction_clean="ENGLAND",
        roe_company_number=_ROE_NO,
        ocod_name_raw=_RAW_NAME,             # stable raw identity
        ocod_jurisdiction_raw=_RAW_JUR,
        is_true_match="TRUE",
        reviewer="Tom",
        active=1,
        provenance="manual",
        held_out=0,
    )
    return run_dir


def _resolves_for_display(run_dir, db_path):
    from app.services.match_reader import get_matches

    items = get_matches(run_dir, db_path, bucket="review")["items"]
    return items[0]["label"] == "TRUE"


def _resolves_for_export(run_dir, db_path):
    from app.services.label_applier import apply_labels

    result = apply_labels(db_path, run_dir)
    merged = pd.read_csv(os.path.join(run_dir, "merged_dataset.csv"), encoding="utf-8-sig")
    return result["applied"] == 1 and merged.iloc[0]["match_method"] == "reviewed_true_review"


def _resolves_for_training(run_dir, db_path):
    from app.pipeline.gbt_train import _labelled_feature_frame

    ocod = _entity_frame("ocod", _NEW_CLEAN,
                         ocod_name_raw=_RAW_NAME, ocod_jurisdiction_raw=_RAW_JUR)
    roe = _entity_frame("roe", "ACME UK LIMITED", roe_company_number=_ROE_NO)
    X, y = _labelled_feature_frame(db_path, ocod, roe, None, held_out=0)
    return len(y) == 1 and y[0] == 1


@pytest.mark.parametrize(
    "resolves",
    [_resolves_for_display, _resolves_for_export, _resolves_for_training],
    ids=["display", "export", "training"],
)
def test_label_survives_rule_change_on_every_path(tmp_path, db_path, resolves):
    """The same rule-change fixture must resolve on ALL THREE consumer paths —
    review display, export applier, and GBT training — via the shared resolver's
    raw-first join. A clean-only resolver would miss on every one of them."""
    run_dir = _rule_change_run(tmp_path, db_path)
    assert resolves(run_dir, db_path)

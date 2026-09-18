# backend/tests/test_jurisdiction_map.py
"""Jurisdiction map: case-insensitive dataset label + a 'both' row covering
OCOD and ROE at once."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pipeline.standardise import (
    load_jurisdiction_map,
    standardise_jurisdiction,
    validate_jurisdiction_coverage,
)

CSV = (
    "source_dataset,raw_value,standardised_value\n"
    "ocod,BRUNEI,BRUNEI\n"
    "OCOD,BRUNEI DARUSSALAM,BRUNEI\n"          # uppercase label (saved by the UI)
    "ROE,BRUNEI DARUSSALAM,BRUNEI\n"           # uppercase label
    "both,ALDERNEY,ALDERNEY\n"                 # applies to OCOD and ROE
)


def _write_map(tmp_path):
    (tmp_path / "jurisdiction_map.csv").write_text(CSV, encoding="utf-8")
    return tmp_path


def test_uppercase_label_matches_lowercase_pipeline(tmp_path):
    m = load_jurisdiction_map(_write_map(tmp_path))
    # Pipeline always looks up with a lowercase dataset; the UPPERCASE 'OCOD'/'ROE'
    # rows must still be found.
    assert standardise_jurisdiction("BRUNEI DARUSSALAM", "ocod", m) == "BRUNEI"
    assert standardise_jurisdiction("BRUNEI DARUSSALAM", "roe", m) == "BRUNEI"


def test_both_covers_ocod_and_roe(tmp_path):
    m = load_jurisdiction_map(_write_map(tmp_path))
    assert standardise_jurisdiction("ALDERNEY", "ocod", m) == "ALDERNEY"
    assert standardise_jurisdiction("ALDERNEY", "roe", m) == "ALDERNEY"


def test_raw_value_lookup_is_case_insensitive(tmp_path):
    m = load_jurisdiction_map(_write_map(tmp_path))
    assert standardise_jurisdiction("brunei", "ocod", m) == "BRUNEI"


def test_validate_coverage_respects_both(tmp_path):
    m = load_jurisdiction_map(_write_map(tmp_path))
    # ALDERNEY (a 'both' row) is covered for ROE; ANDORRA is not mapped at all.
    unmapped = validate_jurisdiction_coverage({"ALDERNEY", "ANDORRA"}, "roe", m)
    assert unmapped == ["ANDORRA"]

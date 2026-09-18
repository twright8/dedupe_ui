# backend/app/services/feature_mapper.py
"""
Feature mapper: translates Splink comparison levels into 0-1 display values
for the review UI.

Key insight: for Jaro-Winkler (JW) name similarity we recompute the actual
similarity from the raw name strings rather than using the Splink gamma
threshold bucket.  This gives the UI a continuous score rather than a
coarse bucket number.

For the binary features (name_core, tokens_sorted, digits) the Splink gamma
column already encodes a clear exact-match signal: gamma == 1 means the
two values were identical (the highest comparison level for these columns),
any other value means they were not identical.

jurisdiction is a real three-state signal (match 1.0 / mismatch 0.0 /
unknown-neutral 0.5). Blocking no longer guarantees a shared jurisdiction:
Splink now also blocks on exact name_core across jurisdictions, so candidate
pairs can genuinely disagree on jurisdiction or carry the UNKNOWN sentinel.
We derive the display value from the pair's actual jurisdiction values using
the same helper the GBT trains on (``gbt_features._jurisdiction_match``) so the
review panel and the model agree on what a jurisdiction agreement means.
"""

import jellyfish
import re

from app.pipeline.gbt_features import _jurisdiction_match

_LEGAL_TOKENS = {
    "LTD", "LIMITED", "INC", "CORP", "CORPORATION", "CO", "PLC", "PTY",
    "PTE", "LLC", "LP", "LLP", "SA", "SARL", "SRL", "BV", "NV", "AG",
    "GMBH", "BHD", "SDN", "PARTNERSHIP", "PRIVATE", "PUBLIC",
}


def compute_jw(name_a: str, name_b: str) -> float:
    """Return the Jaro-Winkler similarity between two name strings.

    Args:
        name_a: Left-side cleaned company name.
        name_b: Right-side cleaned company name.

    Returns:
        Float in [0, 1] — 1.0 for identical strings.
    """
    return jellyfish.jaro_winkler_similarity(name_a, name_b)


def map_features(row: dict) -> dict:
    """Map a scored prediction row to a dict of 0-1 display features.

    Args:
        row: A dict representing one row from the linkage_scored output.
             Expected keys (all optional — absent gamma columns default to 1.0):
               name_clean_l, name_clean_r  — cleaned name strings (Splink _l/_r)
               gamma_name_core             — Splink gamma for exact core name
               gamma_name_tokens_sorted    — Splink gamma for token-sorted name
               gamma_name_digits_sorted    — Splink gamma for digit-sorted name

    Returns:
        Dict with keys:
          name_jw      — continuous JW similarity recomputed from raw strings
          name_core    — 1.0 if gamma_name_core == 1, else 0.0
          tokens_sorted— 1.0 if gamma_name_tokens_sorted == 1, else 0.0
          digits       — 1.0 if gamma_name_digits_sorted == 1, else 0.0
          jurisdiction — three-state agreement of the pair's jurisdictions:
                         1.0 match / 0.0 mismatch / 0.5 unknown-or-missing
                         (OCOD ``jurisdiction_clean`` vs ROE
                         ``roe_jurisdiction_clean``, falling back to
                         ``roe_jurisdiction_raw`` for runs predating that column)
    """
    left = str(row.get("name_clean_l") or row.get("ocod_name_clean") or "")
    right = str(row.get("name_clean_r") or row.get("roe_name_clean") or "")

    # Gamma columns may be absent for Phase 1 exact matches and review CSVs.
    # When absent, recompute transparent approximations from the displayed names
    # rather than pretending all binary features are perfect.
    gammas_present = "gamma_name_core" in row

    if gammas_present:
        name_core = 1.0 if row["gamma_name_core"] == 1 else 0.0
        tokens_sorted = 1.0 if row["gamma_name_tokens_sorted"] == 1 else 0.0
        digits = 1.0 if row["gamma_name_digits_sorted"] == 1 else 0.0
    else:
        name_core = 1.0 if _name_core(left) == _name_core(right) else 0.0
        tokens_sorted = 1.0 if _tokens_sorted(left) == _tokens_sorted(right) else 0.0
        digits = 1.0 if _digits(left) == _digits(right) else 0.0

    name_jw = compute_jw(str(left), str(right)) if left and right else 1.0

    # Three-state jurisdiction agreement from the pair's real values, using the same
    # helper the GBT trains on so the panel and the model never disagree. Prefer the
    # cleaned ROE jurisdiction; fall back to the raw value (best-effort) for runs whose
    # CSVs predate the roe_jurisdiction_clean column. Absent on both sides -> neutral 0.5.
    ocod_jur = str(row.get("jurisdiction_clean") or "")
    roe_jur = row.get("roe_jurisdiction_clean")
    if roe_jur is None or str(roe_jur).strip() == "":
        roe_jur = row.get("roe_jurisdiction_raw")
    jurisdiction = _jurisdiction_match(ocod_jur, str(roe_jur or ""))

    return {
        "name_jw": name_jw,
        "name_core": name_core,
        "tokens_sorted": tokens_sorted,
        "digits": digits,
        "jurisdiction": jurisdiction,
    }


def _name_core(name: str) -> str:
    tokens = [t for t in str(name).split() if t]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def _tokens_sorted(name: str) -> str:
    return " ".join(sorted(set(str(name).split())))


def _digits(name: str) -> str:
    return ",".join(sorted(re.findall(r"\d+", str(name))))

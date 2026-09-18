"""GBT feature engineering for OCOD<->ROE company-name matching.

A minimal, portable feature set adapted from the ``psc reconcile`` corporate
GBT (``train_gbt_corporate.py:_build_features``), with all person-specific
signals (DOB, gender, nationality, forename, generational suffix) removed —
this pipeline matches *company names* only.

Features are built by joining a pair table ``(unique_id_l, unique_id_r)`` back
to the Phase-2 OCOD/ROE record tables (so we never depend on which intermediate
columns Splink happened to retain), plus the Splink match probability (``p1``)
as a feature where available.
"""

from __future__ import annotations

import re

import jellyfish
import numpy as np
import pandas as pd

# Order matters and is persisted alongside the model (gbt_feature_cols.json).
FEATURE_COLS = [
    "base_name_jw",            # Jaro-Winkler on cleaned names (continuous — the de-clumper)
    "base_exact",              # cleaned names identical
    "base_len_diff",           # abs length difference of cleaned names
    "name_core_exact",         # entity-suffix-stripped names identical
    "name_tokens_sorted_exact",# same unique token set
    "name_digits_sorted_exact",# same numeric token set
    "splink_p1",               # Splink/EM match probability (or -1 if unscored)
    "unit_mismatch",           # names differ by a unit/ordinal (27A vs 27B, FUND II vs III) — hard negative
]

# IDF-weighted token overlap (the rare-token signal) appended. Splink's per-comparison
# gamma/bf columns were trialled here too but measured ~no AUC gain on this data, so
# they were dropped rather than carry 9 unused features.
FEATURE_COLS = FEATURE_COLS + ["idf_token_overlap"]

# Jurisdiction agreement (ordinal three-state: mismatch 0.0 / unknown-neutral 0.5 /
# match 1.0). Splink now blocks on name_core across jurisdictions too, so candidate
# pairs can DISAGREE on jurisdiction or carry the UNKNOWN sentinel — this mirrors the
# three Splink levels (agreement = positive, mismatch = negative, unknown = null).
# Monotone-pinned to +1 in training so a mismatch can only lower the score.
FEATURE_COLS = FEATURE_COLS + ["jurisdiction_match"]

# Roman numerals with 2+ characters only — lone "I"/"V"/"X" are too easily company
# initials (e.g. "V G HOLDING") to treat as ordinals.
_ROMAN = {"II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII"}

# Legal-form and connective words that vary freely between OCOD and ROE spellings
# (LTD vs LIMITED, CO vs COMPANY, & vs AND). They are dropped from the CORE before the
# unit comparison, so "SIR TRUSTEE 13 LIMITED" vs "SIR TRUSTEE 4 LTD" is still recognised
# as same-name-different-number. Without this, LTD != LIMITED makes the "rest" differ and
# the high-precision unit signal silently fails to fire on exactly these real-world pairs.
_LEGAL_NOISE = {
    "LTD", "LIMITED", "CO", "COMPANY", "INC", "INCORPORATED", "CORP", "CORPORATION",
    "PLC", "LLP", "LP", "LLC", "SA", "SARL", "SRL", "BV", "NV", "AG", "GMBH",
    "PTE", "PTY", "BHD", "SDN", "AND", "&", "THE",
}


def _units_and_rest(name: str):
    """Split a cleaned name into its 'unit' tokens and the remaining CORE tokens.

    Unit tokens are an optional leading letter, digits, and an optional trailing letter
    (13, 27A, and crucially P4 / P15 — the property/SPV enumeration in names like
    'JAHAMA P4 LTD'), plus roman numerals. ``27 A`` is collapsed to ``27A`` first so a
    stray space isn't read as a different unit. Legal-form words are dropped and the core
    is returned as an order-insensitive set, so the unit comparison can't be disabled by
    an LTD/LIMITED variant, word order, or & vs AND."""
    s = re.sub(r"(\d+)\s+([A-Z])(?=\s|$)", r"\1\2", str(name))
    units, rest = set(), set()
    for tok in s.split():
        if re.fullmatch(r"[A-Z]?\d+[A-Z]?", tok) or tok in _ROMAN:
            units.add(tok)
        elif tok not in _LEGAL_NOISE:
            rest.add(tok)
    return frozenset(units), frozenset(rest)


def _unit_mismatch(a: str, b: str) -> int:
    """1 only when two names share the same CORE (suffix- and order-insensitive) but their
    unit/ordinal differs (BRINDLEY 5 vs 3, ACRIS 27A vs 27B, FUND II vs III, and crucially
    SIR TRUSTEE 13 LIMITED vs SIR TRUSTEE 4 LTD). The 'same core' requirement keeps it
    high-precision — spacing variants and initials don't fire. Monotone-pinned to -1 in
    training, so when it fires it can only lower the score."""
    ua, ra = _units_and_rest(a)
    ub, rb = _units_and_rest(b)
    return int(ra == rb and ua != ub)


def _jurisdiction_match(a: str, b: str) -> float:
    """Ordinal three-state jurisdiction agreement for a pair, from cleaned jurisdictions.
    match = 1.0, genuine mismatch = 0.0, and unknown-neutral = 0.5 when EITHER side is
    empty or the UNKNOWN sentinel (no evidence — mirrors Splink's null level). Pinned
    monotone-increasing (+1) in training, so a mismatch can only lower the score relative
    to a match and unknown sits structurally between the two (never punished below a
    mismatch)."""
    ja = str(a).strip().upper()
    jb = str(b).strip().upper()
    if not ja or not jb or ja == "UNKNOWN" or jb == "UNKNOWN":
        return 0.5
    return 1.0 if ja == jb else 0.0


_NAME_COLS = ["name_clean", "name_core", "name_tokens_sorted", "name_digits_sorted"]
# Columns looked up per side. jurisdiction_clean feeds jurisdiction_match; when a record
# table lacks it (absent in some cold/test frames), _side_lookup fills "" -> neutral 0.5.
_LOOKUP_COLS = _NAME_COLS + ["jurisdiction_clean"]


def _build_idf(ocod_records, roe_records):
    """Inverse-document-frequency of cleaned-name tokens over the OCOD+ROE corpus.
    Computed from the record tables passed in, so it is identical at train and score
    time (both get the same Phase-2 tables). Returns (idf_map, default_for_unseen)."""
    import math
    from collections import Counter

    df = Counter()
    n_docs = 0
    for recs in (ocod_records, roe_records):
        if "name_clean" not in recs.columns:
            continue
        for name in recs["name_clean"].dropna().astype(str):
            n_docs += 1
            for tok in set(name.upper().split()):
                df[tok] += 1
    idf = {t: math.log((1 + n_docs) / (1 + c)) + 1.0 for t, c in df.items()}
    default = math.log(1 + n_docs) + 1.0  # an unseen token is maximally rare
    return idf, default


def _idf_overlap(a: str, b: str, idf: dict, default: float) -> float:
    """Rare-token-weighted Jaccard of two names: shared rare tokens (MERIDIAN) count
    far more than shared boilerplate (HOLDINGS, LTD). 0..1, higher = more match evidence."""
    A = set(str(a).upper().split())
    B = set(str(b).upper().split())
    if not A and not B:
        return 0.0
    w_inter = sum(idf.get(t, default) for t in (A & B))
    w_union = sum(idf.get(t, default) for t in (A | B))
    return float(w_inter / w_union) if w_union else 0.0


def _jw(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        return float(jellyfish.jaro_winkler_similarity(a, b))
    except Exception:
        return 0.0


def _side_lookup(records: pd.DataFrame) -> pd.DataFrame:
    """Index a record table by unique_id, keeping the name + jurisdiction columns we use."""
    cols = ["unique_id"] + [c for c in _LOOKUP_COLS if c in records.columns]
    sub = records[cols].drop_duplicates(subset=["unique_id"]).set_index("unique_id")
    for c in _LOOKUP_COLS:
        if c not in sub.columns:
            sub[c] = ""
        sub[c] = sub[c].fillna("").astype(str)
    return sub


def build_features(
    pairs: pd.DataFrame,
    ocod_records: pd.DataFrame,
    roe_records: pd.DataFrame,
    splink_prob_col: str | None = "match_probability",
) -> pd.DataFrame:
    """Return a feature frame aligned to ``pairs`` (same row order/index).

    ``pairs`` must have ``unique_id_l`` (OCOD) and ``unique_id_r`` (ROE). If a
    Splink probability column is present it is used as ``splink_p1``; otherwise
    ``splink_p1`` is -1 (a sentinel LightGBM can split on).
    """
    if len(pairs) == 0:
        return pd.DataFrame(columns=FEATURE_COLS)

    ocod = _side_lookup(ocod_records)
    roe = _side_lookup(roe_records)

    l = ocod.reindex(pairs["unique_id_l"].values).reset_index(drop=True)
    r = roe.reindex(pairs["unique_id_r"].values).reset_index(drop=True)

    name_l = l["name_clean"].fillna("").astype(str)
    name_r = r["name_clean"].fillna("").astype(str)

    feat = pd.DataFrame(index=pairs.index)
    feat["base_name_jw"] = [_jw(a, b) for a, b in zip(name_l, name_r)]
    feat["base_exact"] = (name_l.values == name_r.values).astype(int)
    feat["base_len_diff"] = np.abs(name_l.str.len().values - name_r.str.len().values)
    feat["name_core_exact"] = (
        l["name_core"].values == r["name_core"].values
    ).astype(int)
    feat["name_tokens_sorted_exact"] = (
        l["name_tokens_sorted"].values == r["name_tokens_sorted"].values
    ).astype(int)
    feat["name_digits_sorted_exact"] = (
        l["name_digits_sorted"].values == r["name_digits_sorted"].values
    ).astype(int)
    feat["unit_mismatch"] = [_unit_mismatch(a, b) for a, b in zip(name_l, name_r)]
    # Jurisdiction agreement, from the record tables (works for every path — scored,
    # labelled, proxy — since all pass the Phase-2 frames; "" -> neutral 0.5).
    feat["jurisdiction_match"] = [
        _jurisdiction_match(a, b)
        for a, b in zip(l["jurisdiction_clean"], r["jurisdiction_clean"])
    ]

    if splink_prob_col and splink_prob_col in pairs.columns:
        feat["splink_p1"] = pd.to_numeric(pairs[splink_prob_col], errors="coerce").fillna(-1.0).values
    else:
        feat["splink_p1"] = -1.0

    # IDF-weighted token overlap: rare shared tokens count more than boilerplate.
    idf, default = _build_idf(ocod_records, roe_records)
    feat["idf_token_overlap"] = [_idf_overlap(a, b, idf, default) for a, b in zip(name_l, name_r)]

    return feat[FEATURE_COLS]

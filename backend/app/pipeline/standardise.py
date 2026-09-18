"""Standardisation utilities for OCOD-ROE record linkage.

Adapted from ``matching roe ocod/src/standardise.py``.  All functions that
previously fell back to a hardcoded ``CONFIG_DIR`` now *require* the caller to
supply the path explicitly via a ``config_dir`` parameter (or a direct file
path).  No default / fallback paths remain.
"""

import csv
import json
import re
import unicodedata
from pathlib import Path


# ---------------------------------------------------------------------------
# Config loaders  (config_dir is mandatory)
# ---------------------------------------------------------------------------

def load_jurisdiction_map(config_dir: Path) -> dict[tuple[str, str], str]:
    path = Path(config_dir) / "jurisdiction_map.csv"
    mapping = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # The dataset label is matched case-insensitively (the UI has saved a
            # mix of 'ocod'/'OCOD'), and a row tagged "both" applies to OCOD *and*
            # ROE so a shared country only has to be entered once. raw_value is
            # upper-cased to line up with standardise_jurisdiction's lookup key.
            dataset = (row.get("source_dataset") or "").strip().lower()
            raw = (row.get("raw_value") or "").strip().upper()
            value = (row.get("standardised_value") or "").strip()
            datasets = ("ocod", "roe") if dataset == "both" else (dataset,)
            for ds in datasets:
                mapping[(ds, raw)] = value
    return mapping


def load_name_rules(config_dir: Path) -> list[dict]:
    path = Path(config_dir) / "name_rules.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_legal_entity_tokens(config_dir: Path) -> set[str]:
    path = Path(config_dir) / "legal_entity_tokens.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {t.strip().upper() for t in data["tokens"]}


# ---------------------------------------------------------------------------
# Pure functions  (no I/O, unchanged from original)
# ---------------------------------------------------------------------------

def accent_fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def apply_name_rules(name: str, rules: list[dict]) -> str:
    # Structural normalisation that must always happen, before the configured rules:
    # brackets and slashes DELIMIT words, so they become a SPACE, never nothing.
    # Otherwise "SANWOT(CYP)" glues to "SANWOTCYP" and stops matching "SANWOT (CYP)".
    # (Dots/commas are left to the configured rules, which strip them with no space, so
    # initials like "J.M." still collapse to "JM".) The trailing \s+ rule re-collapses.
    name = re.sub(r"[()\[\]{}/\\|]", " ", name)
    for rule in rules:
        if rule["pattern"] == "accent_fold":
            name = accent_fold(name)
        else:
            name = re.sub(rule["pattern"], rule["replacement"], name)
    return name


def extract_digit_set(name: str) -> str:
    """Return sorted comma-joined digit tokens in name. Used as an exact-match
    comparison column to catch cases where names differ only by a numeric
    component (e.g. 'BRINDLEY 5 SARL' vs 'BRINDLEY 3 SARL')."""
    digits = re.findall(r"\d+", name or "")
    return ",".join(sorted(digits))


def extract_name_core(name: str, entity_tokens: set[str]) -> str:
    """Strip trailing legal-entity tokens iteratively. 'SOCIETA SEMPLICE KENGARDEN
    2005 LTD PARTNERSHIP' -> 'SOCIETA SEMPLICE KENGARDEN 2005'. Always leaves
    at least one token so pathological inputs (e.g. a name that is just 'LTD')
    don't produce empty cores."""
    if not name:
        return ""
    tokens = name.split()
    while len(tokens) > 1 and tokens[-1] in entity_tokens:
        tokens.pop()
    return " ".join(tokens)


def extract_sorted_tokens(name: str) -> str:
    """Return unique tokens of name sorted alphabetically, joined with space.
    Used as an exact-match comparison column to catch pairs that share all
    their tokens but in different word order (e.g. 'BEZALEL YERUSHALMY AND SON'
    vs 'YERUSHALMY BEZALEL AND SON')."""
    if not name:
        return ""
    return " ".join(sorted(set(name.split())))


def standardise_jurisdiction(
    raw: str, dataset: str, mapping: dict[tuple[str, str], str]
) -> str:
    raw_upper = raw.strip().upper()
    if not raw_upper:
        return "UNKNOWN"
    key = (dataset.strip().lower(), raw_upper)
    if key in mapping:
        return mapping[key]
    return None


def validate_jurisdiction_coverage(
    values: set[str], dataset: str, mapping: dict[tuple[str, str], str]
) -> list[str]:
    ds = dataset.strip().lower()
    unmapped = []
    for v in sorted(values):
        v_upper = v.strip().upper()
        if not v_upper:
            continue
        if (ds, v_upper) not in mapping:
            unmapped.append(v_upper)
    return unmapped


class UnmappedJurisdictionsError(RuntimeError):
    """Raised when the data contains jurisdiction values absent from the map.

    Subclasses RuntimeError so existing broad except-blocks keep working, but
    carries the unmapped values per dataset so the UI can offer a one-click
    "add these to the jurisdiction map" fix instead of leaving the user stuck.
    """

    def __init__(self, unmapped_ocod: list[str], unmapped_roe: list[str]):
        self.unmapped_ocod = unmapped_ocod
        self.unmapped_roe = unmapped_roe
        parts = ["Unmapped jurisdiction values found."]
        if unmapped_ocod:
            parts.append(f"OCOD unmapped ({len(unmapped_ocod)}): {unmapped_ocod[:20]}")
        if unmapped_roe:
            parts.append(f"ROE unmapped ({len(unmapped_roe)}): {unmapped_roe[:20]}")
        parts.append("Add these to the jurisdiction map (Config screen) and rerun.")
        super().__init__("\n".join(parts))

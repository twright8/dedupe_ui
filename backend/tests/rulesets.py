# backend/tests/rulesets.py
"""Rulesets the tests build on."""

import copy
import json
from pathlib import Path

DEFAULTS_DIR = Path(__file__).parent.parent / "app" / "profiles" / "defaults" / "donations"


def replace_derived_columns(ruleset: dict, *columns: dict) -> dict:
    """*ruleset* with its derived columns swapped for *columns*.

    A match key may read a derived target — the shipped trade-union key reads
    ``donor_status_std`` — so any key left pointing at a target that has just
    gone is dropped too. Without that a test swapping the derived columns would
    fail validation for a reason it was not testing.
    """
    ruleset = copy.deepcopy(ruleset)
    gone = {
        column.get("target")
        for column in ruleset.get("derived_columns") or []
        if isinstance(column, dict)
    } - {column.get("target") for column in columns}
    ruleset["derived_columns"] = [copy.deepcopy(c) for c in columns]
    ruleset["match_keys"] = [
        key for key in ruleset.get("match_keys") or []
        if not any(c.get("column") in gone for c in key.get("when") or [])
    ]
    return ruleset


def default_ruleset() -> dict:
    """The donations profile's shipped ruleset, freshly parsed each call."""
    return json.loads((DEFAULTS_DIR / "ruleset.json").read_text(encoding="utf-8"))


def default_linkage_settings() -> dict:
    return json.loads((DEFAULTS_DIR / "linkage_settings.json").read_text(encoding="utf-8"))


def small_ruleset() -> dict:
    """The smallest ruleset that passes validation — a base for focused tests."""
    return {
        "schema": 1,
        "token_lists": {"titles": {"description": "", "tokens": ["MR", "MRS"]}},
        "lookups": {},
        "track_rules": [
            {"id": "r1", "description": "Individuals are people",
             "when": [{"column": "donor_status", "op": "equals", "value": "Individual"}],
             "track": "person"},
        ],
        "default_track": "organisation",
        "cleaning": {
            "person": [
                {"id": "p1", "description": "", "op": "upper",
                 "source": "name", "target": "name_clean"},
            ],
            "organisation": [
                {"id": "o1", "description": "", "op": "upper",
                 "source": "name", "target": "name_clean"},
            ],
        },
        "match_keys": [],
        "vetoes": [],
    }

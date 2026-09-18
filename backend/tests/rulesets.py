# backend/tests/rulesets.py
"""Rulesets the tests build on."""

import json
from pathlib import Path

DEFAULTS_DIR = Path(__file__).parent.parent / "app" / "profiles" / "defaults" / "donations"


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

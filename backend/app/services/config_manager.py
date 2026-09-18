# backend/app/services/config_manager.py
"""Versioned configuration management — save, restore, diff, live rule preview."""

import json
import re
from app.pipeline.standardise import accent_fold
from typing import Any

from app.db import query_db, write_db
from app.services.audit_logger import log_event


def save_version(
    db_path: str,
    created_by: str,
    note: str,
    name_rules: list[dict],
    jurisdiction_map: list[dict],
    legal_tokens,
    linkage_settings: dict[str, Any],
) -> int:
    """Store a full snapshot of all 4 config fields. Returns the new version number."""
    version = write_db(
        db_path,
        """INSERT INTO config_versions (created_by, note, name_rules, jurisdiction_map,
                                        legal_tokens, linkage_settings)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            created_by,
            note,
            json.dumps(name_rules),
            json.dumps(jurisdiction_map),
            json.dumps(legal_tokens),
            json.dumps(linkage_settings),
        ),
    )
    log_event(
        db_path,
        user=created_by,
        kind="config",
        description=f"Saved config version {version}",
        metadata={"version": version, "note": note},
    )
    return version


def get_version(db_path: str, version: int) -> dict | None:
    """Return a single config version row as a dict, or None if not found."""
    rows = query_db(
        db_path,
        "SELECT * FROM config_versions WHERE version = ?",
        (version,),
    )
    return rows[0] if rows else None


def get_current(db_path: str) -> dict | None:
    """Return the highest-version config, or None if no versions exist."""
    rows = query_db(
        db_path,
        "SELECT * FROM config_versions ORDER BY version DESC LIMIT 1",
    )
    return rows[0] if rows else None


def list_versions(db_path: str) -> list[dict]:
    """Return all versions with metadata, ordered by version DESC."""
    return query_db(
        db_path,
        "SELECT version, created_at, created_by, note FROM config_versions ORDER BY version DESC",
    )


def diff_versions(db_path: str, v1: int, v2: int) -> dict:
    """Compare two config versions field-by-field.

    Returns a dict with one key per config field, each containing:
      {"changed": bool, "v1": <parsed>, "v2": <parsed>}
    """
    ver1 = get_version(db_path, v1)
    ver2 = get_version(db_path, v2)

    fields = ("name_rules", "jurisdiction_map", "legal_tokens", "linkage_settings")
    result = {}
    for field in fields:
        val1 = json.loads(ver1[field])
        val2 = json.loads(ver2[field])
        result[field] = {"changed": val1 != val2, "v1": val1, "v2": val2}
    return result


def test_rules(
    input_name: str,
    rules: list[dict] | None = None,
    db_path: str | None = None,
) -> dict:
    """Apply regex rules to an input name and return step-by-step results.

    If *rules* is provided, uses those directly (draft preview).
    If *rules* is None, loads name_rules from the current config version via *db_path*.
    Input is uppercased before processing.
    """
    if rules is None:
        if db_path is None:
            raise ValueError("Either rules or db_path must be provided")
        current = get_current(db_path)
        if current is None:
            raise ValueError("No config version exists to load rules from")
        rules = json.loads(current["name_rules"])

    current_value = input_name.upper()
    steps = []

    for i, rule in enumerate(rules):
        before = current_value
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", rule.get("replace", ""))
        if pattern == "accent_fold":
            current_value = accent_fold(current_value)
        else:
            current_value = re.sub(pattern, replacement, current_value)
        steps.append({
            "rule_index": i,
            "pattern": pattern,
            "before": before,
            "after": current_value,
            "result": current_value,
            "output": current_value,
            "changed": before != current_value,
        })

    return {"final": current_value, "output": current_value, "steps": steps}


# Prevent pytest from collecting this function as a test
test_rules.__test__ = False  # type: ignore[attr-defined]

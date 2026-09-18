# backend/app/services/config_manager.py
"""Versioned configuration management — save, read, diff.

A config version is one ruleset (``docs/RULESET.md``) plus ``linkage_settings``.
The three legacy columns stay in the table so old rows still read; nothing
writes them any more.
"""

import json
from typing import Any

from app.db import query_db, write_db
from app.services.audit_logger import log_event

# The ruleset sections a diff reports on, and how each is keyed.
#   "id"   — a list of objects with an id
#   "name" — an object keyed on a name
#   "value"— compared whole
DIFF_SECTIONS = (
    ("token_lists", "name"),
    ("lookups", "name"),
    ("track_rules", "id"),
    ("default_track", "value"),
    ("cleaning.person", "id"),
    ("cleaning.organisation", "id"),
    ("match_keys", "id"),
    ("vetoes", "id"),
)


def save_version(
    db_path: str,
    created_by: str,
    note: str,
    ruleset: dict[str, Any],
    linkage_settings: dict[str, Any],
) -> int:
    """Store a full snapshot of the ruleset and settings. Returns the new version."""
    version = write_db(
        db_path,
        """INSERT INTO config_versions (created_by, note, ruleset, linkage_settings)
           VALUES (?, ?, ?, ?)""",
        (created_by, note, json.dumps(ruleset), json.dumps(linkage_settings)),
    )
    log_event(
        db_path,
        user=created_by,
        kind="config",
        description=f"Saved config version {version}",
        metadata={"version": version, "note": note},
    )
    return version


def _parse_ruleset(row: dict | None) -> dict | None:
    """Return *row* with its ruleset parsed. linkage_settings stays a JSON
    string — the pipeline writes it straight to the run's config folder."""
    if row is None:
        return None
    result = dict(row)
    raw = result.get("ruleset")
    if isinstance(raw, str):
        try:
            result["ruleset"] = json.loads(raw)
        except (ValueError, TypeError):
            result["ruleset"] = None
    return result


def get_version(db_path: str, version: int) -> dict | None:
    """Return a single config version row as a dict, or None if not found."""
    rows = query_db(
        db_path,
        "SELECT * FROM config_versions WHERE version = ?",
        (version,),
    )
    return _parse_ruleset(rows[0]) if rows else None


def get_current(db_path: str) -> dict | None:
    """Return the highest-version config, or None if no versions exist."""
    rows = query_db(
        db_path,
        "SELECT * FROM config_versions ORDER BY version DESC LIMIT 1",
    )
    return _parse_ruleset(rows[0]) if rows else None


def list_versions(db_path: str) -> list[dict]:
    """Return all versions with metadata, ordered by version DESC."""
    return query_db(
        db_path,
        "SELECT version, created_at, created_by, note FROM config_versions ORDER BY version DESC",
    )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _section(ruleset: dict | None, path: str):
    """One diffable section of a ruleset, addressed by its dotted path."""
    if not isinstance(ruleset, dict):
        return None
    if "." not in path:
        return ruleset.get(path)
    head, tail = path.split(".", 1)
    parent = ruleset.get(head)
    return parent.get(tail) if isinstance(parent, dict) else None


def _keyed(section, keyed_by: str) -> dict:
    """A section as {item name: item}, whichever shape it arrives in."""
    if keyed_by == "name" and isinstance(section, dict):
        return dict(section)
    if isinstance(section, list):
        keyed = {}
        for index, item in enumerate(section):
            if isinstance(item, dict) and item.get("id"):
                keyed[str(item["id"])] = item
            else:
                keyed[str(index)] = item
        return keyed
    return {}


def _diff_section(before, after, keyed_by: str) -> dict:
    if keyed_by == "value":
        return {"changed": before != after, "v1": before, "v2": after}
    left, right = _keyed(before, keyed_by), _keyed(after, keyed_by)
    added = sorted(set(right) - set(left))
    removed = sorted(set(left) - set(right))
    modified = sorted(k for k in set(left) & set(right) if left[k] != right[k])
    return {
        "changed": bool(added or removed or modified),
        "added": added,
        "removed": removed,
        "modified": modified,
    }


def diff_versions(db_path: str, v1: int, v2: int) -> dict:
    """Compare two config versions, section by section.

    Each ruleset section reports the item ids (or names) added, removed and
    changed. ``default_track`` and ``linkage_settings`` are compared whole and
    report ``v1`` / ``v2`` instead.
    """
    ver1 = get_version(db_path, v1)
    ver2 = get_version(db_path, v2)
    if ver1 is None or ver2 is None:
        missing = v1 if ver1 is None else v2
        raise ValueError(f"Config version {missing} not found")

    result = {
        path: _diff_section(_section(ver1["ruleset"], path),
                            _section(ver2["ruleset"], path), keyed_by)
        for path, keyed_by in DIFF_SECTIONS
    }

    settings1 = json.loads(ver1["linkage_settings"] or "{}")
    settings2 = json.loads(ver2["linkage_settings"] or "{}")
    result["linkage_settings"] = {
        "changed": settings1 != settings2, "v1": settings1, "v2": settings2,
    }
    return result

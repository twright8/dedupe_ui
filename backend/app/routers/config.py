# backend/app/routers/config.py
"""Config versioning API — save, restore, diff, live rule preview."""

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import current_user
from app.services import config_manager

router = APIRouter(prefix="/api/config", tags=["config"])


def _db_path() -> str:
    """Resolve DB_PATH at call time so tests can monkeypatch app.main.DB_PATH."""
    from app.main import DB_PATH
    return DB_PATH


def _parse_json_fields(row: dict) -> dict:
    """Parse the 4 JSON-string config fields into Python objects."""
    result = dict(row)
    for field in ("name_rules", "jurisdiction_map", "legal_tokens", "linkage_settings"):
        if field in result and isinstance(result[field], str):
            result[field] = json.loads(result[field])
    return result


# ---- Request schemas ----

class SaveConfigBody(BaseModel):
    name_rules: list[dict[str, Any]]
    jurisdiction_map: list[dict[str, Any]]
    legal_tokens: Any
    linkage_settings: dict[str, Any]
    note: str = ""


class TestRulesBody(BaseModel):
    input_name: str
    rules: Optional[list[dict[str, str]]] = None


class JurisdictionEntry(BaseModel):
    source_dataset: str
    raw_value: str
    standardised_value: str


class AddJurisdictionsBody(BaseModel):
    entries: list[JurisdictionEntry]
    note: str = ""


# ---- Endpoints ----

@router.get("/current")
def get_current():
    row = config_manager.get_current(_db_path())
    if row is None:
        return None
    return _parse_json_fields(row)


@router.get("")
def get_current_alias():
    """Compatibility alias; frontend should prefer /current."""
    return get_current()


@router.get("/versions")
def list_versions():
    return config_manager.list_versions(_db_path())


@router.get("/versions/{version}")
def get_version(version: int):
    row = config_manager.get_version(_db_path(), version)
    if row is None:
        return None
    return _parse_json_fields(row)


@router.get("/diff/{v1}/{v2}")
def diff_versions(v1: int, v2: int):
    return config_manager.diff_versions(_db_path(), v1, v2)


def _validate_config(body: SaveConfigBody) -> None:
    if not isinstance(body.linkage_settings, dict):
        raise HTTPException(status_code=400, detail="linkage_settings must be an object")
    if not isinstance(body.name_rules, list):
        raise HTTPException(status_code=400, detail="name_rules must be a list")
    if not isinstance(body.jurisdiction_map, list):
        raise HTTPException(status_code=400, detail="jurisdiction_map must be a list")
    legal_tokens = body.legal_tokens
    if isinstance(legal_tokens, dict):
        if not isinstance(legal_tokens.get("tokens"), list):
            raise HTTPException(status_code=400, detail="legal_tokens.tokens must be a list")
    elif not isinstance(legal_tokens, list):
        raise HTTPException(status_code=400, detail="legal_tokens must be a list or {tokens: [...]}")


@router.post("")
def save_config(body: SaveConfigBody, user_name: str = Depends(current_user)):
    db_path = _db_path()
    _validate_config(body)
    version = config_manager.save_version(
        db_path,
        created_by=user_name,
        note=body.note,
        name_rules=body.name_rules,
        jurisdiction_map=body.jurisdiction_map,
        legal_tokens=body.legal_tokens,
        linkage_settings=body.linkage_settings,
    )
    return {"version": version}


@router.post("/jurisdictions")
def add_jurisdictions(body: AddJurisdictionsBody, user_name: str = Depends(current_user)):
    """Append jurisdiction mappings to the current config and save a new version.

    Powers the one-click fix on a run that failed with unmapped jurisdictions.
    Dedupes case-insensitively on (source_dataset, raw_value); returns the new
    version and how many entries were actually added.
    """
    db_path = _db_path()
    current = config_manager.get_current(db_path)
    if current is None:
        raise HTTPException(status_code=400, detail="No config version exists to add to")

    parsed = _parse_json_fields(current)
    jurisdiction_map = parsed["jurisdiction_map"]
    existing = {
        (e.get("source_dataset", "").strip().lower(), e.get("raw_value", "").strip().upper())
        for e in jurisdiction_map
    }
    added = 0
    for entry in body.entries:
        key = (entry.source_dataset.strip().lower(), entry.raw_value.strip().upper())
        if key in existing:
            continue
        existing.add(key)
        jurisdiction_map.append({
            "source_dataset": entry.source_dataset.strip().lower(),
            "raw_value": entry.raw_value.strip().upper(),
            "standardised_value": entry.standardised_value.strip().upper(),
        })
        added += 1

    # Nothing new — don't spam an identical version (e.g. a double-click).
    if added == 0:
        return {"version": current["version"], "added": 0}

    note = body.note or f"Added {added} jurisdiction mapping(s)"
    version = config_manager.save_version(
        db_path,
        created_by=user_name,
        note=note,
        name_rules=parsed["name_rules"],
        jurisdiction_map=jurisdiction_map,
        legal_tokens=parsed["legal_tokens"],
        linkage_settings=parsed["linkage_settings"],
    )
    return {"version": version, "added": added}


@router.post("/test-rules")
def test_rules(body: TestRulesBody):
    db_path = _db_path()
    return config_manager.test_rules(
        input_name=body.input_name,
        rules=body.rules,
        db_path=db_path,
    )

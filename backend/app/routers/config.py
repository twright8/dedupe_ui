# backend/app/routers/config.py
"""Config API — versioned rulesets, validation, and the two live previews.

Every preview calls ``app.rules.engine``, the same code the pipeline runs. A
preview here must never re-implement a rule; if it did, the Config screen would
promise something a run does not deliver.
"""

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import current_user
from app.profiles import get_profile
from app.rules import engine, functions, linkage
from app.services import config_manager

router = APIRouter(prefix="/api/config", tags=["config"])

PREVIEW_DEFAULT_N = 8
PREVIEW_MAX_N = 50
PREVIEW_MAX_EXAMPLES = 10
PREVIEW_MAX_KEY_EXAMPLES = 8
PREVIEW_MAX_KEY_NAMES = 4

# The key preview reruns cleaning and grouping in memory on every keystroke's
# worth of editing. Above this it would hold a PSC-sized frame in the web
# process; a later slice previews a sample instead.
PREVIEW_MAX_RECORDS = 1_000_000


def _db_path() -> str:
    """Resolve DB_PATH at call time so tests can monkeypatch app.main.DB_PATH."""
    from app.main import DB_PATH
    return DB_PATH


def _data_dir() -> Path:
    from app.main import DATA_DIR
    return Path(DATA_DIR)


def _raw_columns() -> list[str]:
    """The profile's record columns — what a rule may read and may not overwrite."""
    return list(get_profile().raw_columns)


def _as_version(row: dict | None) -> dict | None:
    """One config version as the API returns it."""
    if row is None:
        return None
    return {
        "version": row["version"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "note": row["note"],
        "ruleset": row.get("ruleset"),
        "linkage_settings": json.loads(row["linkage_settings"] or "{}"),
    }


def _current_ruleset() -> dict:
    current = config_manager.get_current(_db_path())
    if current is None or not current.get("ruleset"):
        raise HTTPException(status_code=400, detail="No config version with a ruleset exists")
    return current["ruleset"]


def _ruleset_or_current(draft: dict | None) -> dict:
    return draft if draft is not None else _current_ruleset()


def _all_errors(ruleset: dict, settings: dict | None) -> list[dict]:
    """Everything wrong with a draft ruleset and its linkage settings.

    The settings are checked against the ruleset, because which columns a
    blocking rule or a comparison may name depends on what that track's
    cleaning steps write.
    """
    raw = _raw_columns()
    errors = engine.validate_ruleset(ruleset, raw)
    if isinstance(ruleset, dict):
        errors = errors + linkage.validate_linkage_settings(settings, ruleset, raw)
    return errors


def _validated(ruleset: dict, settings: dict | None = None) -> dict:
    """A ruleset that is safe to run, or a 422 listing everything wrong with it."""
    errors = _all_errors(ruleset, settings)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    return ruleset


def _raw_records(run_id: str) -> pd.DataFrame:
    """A run's records as stage 0 loaded them — before any track or cleaning."""
    from app.pipeline.dedupe.stage_0_load import RECORDS_RAW_FILENAME

    path = _data_dir() / "runs" / run_id / RECORDS_RAW_FILENAME
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' has no loaded records to preview against",
        )
    return pd.read_parquet(path)


# ---- Request schemas ----


class SaveConfigBody(BaseModel):
    ruleset: dict[str, Any]
    # Omitted means "leave them as they are". Defaulting to {} would let a save
    # from the rules screen quietly wipe the thresholds.
    linkage_settings: Optional[dict[str, Any]] = None
    note: str = ""


class ValidateBody(BaseModel):
    ruleset: dict[str, Any]
    # Omitted means "only check the ruleset" — the rules screen has no thresholds
    # on it, and the Thresholds & Splink tab sends both.
    linkage_settings: Optional[dict[str, Any]] = None


class ColumnsBody(BaseModel):
    ruleset: Optional[dict[str, Any]] = None
    track: str


class PreviewCleaningBody(BaseModel):
    ruleset: Optional[dict[str, Any]] = None
    track: str
    values: Optional[list[dict[str, Any]]] = None
    run_id: Optional[str] = None
    q: Optional[str] = None
    n: int = PREVIEW_DEFAULT_N


class PreviewDerivedBody(BaseModel):
    ruleset: Optional[dict[str, Any]] = None
    run_id: str


class PreviewTracksBody(BaseModel):
    ruleset: Optional[dict[str, Any]] = None
    run_id: str


class PreviewKeysBody(BaseModel):
    ruleset: Optional[dict[str, Any]] = None
    run_id: str


class LookupRow(BaseModel):
    raw: str
    canonical: str


class AddLookupRowsBody(BaseModel):
    rows: list[LookupRow]
    note: str = ""


# ---- Versions ----


@router.get("/current")
def get_current():
    return _as_version(config_manager.get_current(_db_path()))


@router.get("")
def get_current_alias():
    """Compatibility alias; the frontend should prefer /current."""
    return get_current()


@router.get("/versions")
def list_versions():
    return config_manager.list_versions(_db_path())


@router.get("/versions/{version}")
def get_version(version: int):
    return _as_version(config_manager.get_version(_db_path(), version))


@router.get("/diff/{v1}/{v2}")
def diff_versions(v1: int, v2: int):
    try:
        return config_manager.diff_versions(_db_path(), v1, v2)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("")
def save_config(body: SaveConfigBody, user_name: str = Depends(current_user)):
    """Save a new config version. An invalid ruleset is never stored."""
    db_path = _db_path()
    settings = body.linkage_settings
    if settings is None:
        current = config_manager.get_current(db_path)
        settings = json.loads(current["linkage_settings"] or "{}") if current else {}
    _validated(body.ruleset, settings)
    version = config_manager.save_version(
        db_path,
        created_by=user_name,
        note=body.note,
        ruleset=body.ruleset,
        linkage_settings=settings,
    )
    return {"version": version}


@router.post("/validate")
def validate_config(body: ValidateBody):
    """Check a draft without saving it."""
    return {"errors": _all_errors(body.ruleset, body.linkage_settings)}


# ---- What the editor needs to offer ----


@router.get("/functions")
def list_functions():
    """The fixed function library. ``outputs: ["target"]`` means the function
    writes the step's target; anything else is a fixed set of column names."""
    return functions.library()


def _columns_response(ruleset: dict, track: str) -> dict:
    if track not in engine.TRACK_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"track must be one of {', '.join(engine.TRACK_KEYS)}",
        )
    labels = {c.key: c.label for c in get_profile().display_columns}
    columns = engine.available_columns(ruleset, track, _raw_columns())
    return {
        "raw": [{"key": key, "label": labels.get(key, key)} for key in columns["raw"]],
        "steps": columns["steps"],
        # Kept apart from `steps`: a derived column must not read as a clash with
        # itself when the Config screen checks its target against what exists.
        "derived": columns["derived"],
        "all": columns["all"],
    }


@router.get("/columns")
def get_columns(track: str = Query(..., description="person | organisation")):
    """Columns a step in *track* may read, taken from the current ruleset."""
    return _columns_response(_current_ruleset(), track)


@router.post("/columns")
def post_columns(body: ColumnsBody):
    """The same, for a draft ruleset the user has not saved yet."""
    return _columns_response(_ruleset_or_current(body.ruleset), body.track)


# ---- Previews ----


def _preview_frame(body: PreviewCleaningBody, ruleset: dict) -> pd.DataFrame:
    """The rows a cleaning preview runs on: typed values, or a sample of a run."""
    limit = max(1, min(int(body.n or PREVIEW_DEFAULT_N), PREVIEW_MAX_N))

    if body.values:
        frame = pd.DataFrame(body.values[:limit])
        for column in _raw_columns():
            if column not in frame.columns:
                frame[column] = None
        return frame

    if not body.run_id:
        raise HTTPException(
            status_code=400, detail="Give either values or a run_id to preview against"
        )

    records = _raw_records(body.run_id)
    # The DRAFT ruleset decides the track, so the sample shows what this edit
    # would actually clean — not what the last saved rules picked.
    records = records[engine.assign_tracks(records, ruleset) == body.track]
    if body.q and "name" in records.columns:
        records = records[
            records["name"].astype(str).str.contains(body.q, case=False, regex=False, na=False)
        ]
    return records.head(limit)


@router.post("/preview-cleaning")
def preview_cleaning(body: PreviewCleaningBody):
    """Run one track's cleaning over a handful of rows, step by step.

    A broken draft rule is reported on its own step; it never fails the request,
    because the user is mid-edit and needs to see where it went wrong.
    """
    ruleset = _ruleset_or_current(body.ruleset)
    if body.track not in engine.TRACK_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"track must be one of {', '.join(engine.TRACK_KEYS)}",
        )
    frame = _preview_frame(body, ruleset)
    try:
        samples = engine.trace_rows(frame, ruleset, body.track)
    except engine.UnmappedLookupValuesError as exc:
        raise HTTPException(
            status_code=422,
            detail={"kind": "unmapped_lookup_values", "table": exc.table, "values": exc.values},
        )
    return {"track": body.track, "samples": samples}


@router.post("/preview-tracks")
def preview_tracks(body: PreviewTracksBody):
    """How the track rules would split a run's records, rule by rule."""
    ruleset = _ruleset_or_current(body.ruleset)
    records = _raw_records(body.run_id)
    tracks, rules = engine.assign_tracks_detailed(records, ruleset)

    reported = []
    for rule in rules:
        shown = ["record_id", "name"] + [
            c for c in rule["columns"] if c not in ("record_id", "name")
        ]
        shown = [c for c in shown if c in records.columns]
        examples = records.loc[rule["mask"], shown].head(PREVIEW_MAX_EXAMPLES)
        reported.append({
            "id": rule["id"],
            "description": rule["description"],
            "track": rule["track"],
            "hits": rule["hits"],
            "examples": json.loads(examples.to_json(orient="records")),
        })

    return {
        "total": int(len(records)),
        "tracks": {
            track: int((tracks == track).sum()) for track in engine.TRACK_KEYS
        },
        "rules": reported,
    }


@router.post("/preview-derived")
def preview_derived(body: PreviewDerivedBody):
    """What the DRAFT derived columns would change, column by column.

    Like the key preview, the whole chain reruns in memory — tracks, cleaning,
    then the derived rules — so editing a cleaning step shows its effect here
    too rather than needing a run first.
    """
    from app.pipeline.dedupe.stage_1_clean import clean_records_detailed

    ruleset = _validated(_ruleset_or_current(body.ruleset))
    records = _raw_records(body.run_id)
    if len(records) > PREVIEW_MAX_RECORDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This run has {len(records):,} records. The derived preview runs the "
                f"whole ruleset in memory and is capped at {PREVIEW_MAX_RECORDS:,}; "
                "start the run to see the derived columns on a dataset this size."
            ),
        )

    try:
        cleaned, reports = clean_records_detailed(records, ruleset)
    except engine.UnmappedLookupValuesError as exc:
        raise HTTPException(
            status_code=422,
            detail={"kind": "unmapped_lookup_values", "table": exc.table, "values": exc.values},
        )

    return {"columns": [_derived_column_report(report, cleaned) for report in reports]}


def _derived_column_report(report: dict, records: pd.DataFrame) -> dict:
    """One derived column as the Config screen shows it."""
    rules = []
    for rule in report["rules"]:
        shown = ["record_id", "name"] + [
            c for c in rule["columns"] if c not in ("record_id", "name")
        ]
        shown = [c for c in shown if c in records.columns]
        rows = records.loc[rule["mask"], shown].head(PREVIEW_MAX_EXAMPLES).copy()
        # "What it was" is the whole point of an example here, so the value the
        # default would have given rides along with each one.
        rows["from"] = report["before"].loc[rows.index]
        examples = json.loads(rows.to_json(orient="records"))
        rules.append({
            "id": rule["id"],
            "description": rule["description"],
            "value": rule["value"],
            "hits": rule["hits"],
            "examples": examples,
        })

    return {
        "id": report["id"],
        "target": report["target"],
        "description": report["description"],
        "default_from": report["default_from"],
        "tracks": report["tracks"],
        "total": report["total"],
        "changed": report["changed"],
        "transitions": engine.transitions(report),
        "rules": rules,
    }


def _key_examples(groups: pd.DataFrame, records: pd.DataFrame, key_id: str) -> list[dict]:
    """The largest groups one key built, for the card on the Config screen."""
    if not len(groups):
        return []
    mine = groups[groups["key_ids"].astype(str).str.split("|").map(lambda ids: key_id in ids)]
    if not len(mine):
        return []
    names = records.set_index(records["record_id"].astype(str))["name"] \
        if "name" in records.columns else None

    examples = []
    sizes = mine.groupby("group_id").size().sort_values(ascending=False, kind="mergesort")
    for group_id in list(sizes.index)[:PREVIEW_MAX_KEY_EXAMPLES]:
        members = mine[mine["group_id"] == group_id]
        shown: list[str] = []
        if names is not None:
            for record_id in members["record_id"].astype(str):
                value = names.get(record_id)
                if isinstance(value, str) and value and value not in shown:
                    shown.append(value)
                if len(shown) >= PREVIEW_MAX_KEY_NAMES:
                    break
        first = members.iloc[0]
        examples.append({
            "group_id": group_id,
            "size": int(len(members)),
            "status": first["status"],
            # A merged group has no guard. pandas holds that as NaN, which is not JSON.
            "guard": None if pd.isna(first["guard"]) else first["guard"],
            "names": shown,
        })
    return examples


def _saved_baseline(run_id: str) -> dict | None:
    """What this run itself measured, so the preview can show a delta.

    None for a run made before stage 2 existed — the Config screen then shows
    the draft's numbers on their own rather than a delta against nothing.
    """
    from app.services.exact_groups_reader import eval_path

    path = eval_path(str(_data_dir() / "runs" / run_id))
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return {"overall": saved.get("overall"), "eval": saved.get("eval")}


@router.post("/preview-keys")
def preview_keys(body: PreviewKeysBody):
    """What the DRAFT match keys would group, measured against the old labels.

    The whole chain reruns in memory — tracks, cleaning, keys, evaluation — so
    editing a cleaning step shows its effect on the keys, not just on the names.
    """
    from app.pipeline.dedupe.stage_1_clean import clean_records
    from app.pipeline.dedupe.stage_2_exact import build_report

    ruleset = _validated(_ruleset_or_current(body.ruleset))
    records = _raw_records(body.run_id)
    if len(records) > PREVIEW_MAX_RECORDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This run has {len(records):,} records. The key preview runs the whole "
                f"ruleset in memory and is capped at {PREVIEW_MAX_RECORDS:,}; "
                "start the run to see the keys on a dataset this size."
            ),
        )

    try:
        cleaned = clean_records(records, ruleset)
    except engine.UnmappedLookupValuesError as exc:
        raise HTTPException(
            status_code=422,
            detail={"kind": "unmapped_lookup_values", "table": exc.table, "values": exc.values},
        )
    groups, report = build_report(cleaned, ruleset)

    keys = []
    for stats in report["keys"]:
        keys.append({**stats, "examples": _key_examples(groups, cleaned, stats["id"])})

    evaluation = report["eval"]
    return {
        "keys": keys,
        "overall": report["overall"],
        "eval": {
            "pair_precision": evaluation["pair_precision"],
            "pair_recall": evaluation["pair_recall"],
            "conflicts": evaluation["conflicts"],
            "by_agreement": evaluation["by_agreement"],
        },
        "baseline": _saved_baseline(body.run_id),
    }


# ---- The one-click fix for unmapped lookup values ----


@router.post("/lookups/{name}/rows")
def add_lookup_rows(name: str, body: AddLookupRowsBody, user_name: str = Depends(current_user)):
    """Append rows to a lookup and save the result as a new config version.

    This is what a run that failed on an unmapped lookup offers the user: the
    values it could not map, ready to be given canonical forms.
    """
    db_path = _db_path()
    current = config_manager.get_current(db_path)
    if current is None or not current.get("ruleset"):
        raise HTTPException(status_code=400, detail="No config version exists to add to")

    ruleset = current["ruleset"]
    table = (ruleset.get("lookups") or {}).get(name)
    if table is None:
        raise HTTPException(status_code=404, detail=f"Unknown lookup '{name}'")

    rows = list(table.get("rows") or [])
    existing = {" ".join(str(r.get("raw", "")).split()).upper() for r in rows}
    added = 0
    for row in body.rows:
        key = " ".join(row.raw.split()).upper()
        if not key or key in existing:
            continue
        existing.add(key)
        rows.append({"raw": key, "canonical": row.canonical.strip()})
        added += 1

    # Nothing new — don't spam an identical version (e.g. a double-click).
    if added == 0:
        return {"version": current["version"], "added": 0}

    table["rows"] = rows
    version = config_manager.save_version(
        db_path,
        created_by=user_name,
        note=body.note or f"Added {added} row(s) to the {name} lookup",
        ruleset=ruleset,
        linkage_settings=json.loads(current["linkage_settings"] or "{}"),
    )
    return {"version": version, "added": added}

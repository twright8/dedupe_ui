# backend/app/routers/runs.py
"""Runs API — create, list, detail, SSE progress, file download, timeline, diagnostics."""

import io
import json
import math
import os
import zipfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.auth import current_user
from app.db import query_db, write_db
from app.profiles import get_profile
from app.services.config_manager import get_version
from app import vocabulary
from app.services import bucketing_history
from app.services import exact_groups_reader
from app.services import pairs_reader
from app.services import pipeline_runner
from app.services import records_reader
from app.services import run_counts
from app.services import run_manifest
from app.services.audit_logger import log_event

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _allowed_input_suffixes() -> set[str]:
    """Accepted input file types, declared by the profile — never hard-coded here."""
    return {e.lower() for e in get_profile().input.extensions}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    """Resolve DATA_DIR at call time so tests can override it."""
    return Path(os.environ.get("DATA_DIR", "data"))


def _db_path() -> str:
    from app.main import DB_PATH
    return DB_PATH


def _data_dir_from_main() -> Path:
    from app.main import DATA_DIR
    return DATA_DIR


def _generate_run_id(db_path: str) -> str:
    """Generate a run ID in the format run_YYYY_MM_DDx (x = a, b, c...)."""
    today = date.today()
    prefix = f"run_{today.strftime('%Y_%m_%d')}"

    existing = query_db(
        db_path,
        "SELECT id FROM runs WHERE id LIKE ? ORDER BY id",
        (f"{prefix}%",),
    )
    existing_ids = {r["id"] for r in existing}

    for i in range(26):
        suffix = chr(ord("a") + i)
        candidate = f"{prefix}{suffix}"
        if candidate not in existing_ids:
            return candidate

    # Fallback — more than 26 runs in one day
    return f"{prefix}_{len(existing) + 1}"


def _human_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def _top_jurisdictions_from_outputs(paths: list[Path]) -> list[dict]:
    """Count jurisdictions from the first usable output CSV."""
    import csv
    from collections import Counter

    jurisdiction_columns = (
        "jurisdiction_clean",
        "ocod_jurisdiction_raw",
        "roe_jurisdiction_raw",
    )

    for path in paths:
        if not path.exists():
            continue
        counts: Counter[str] = Counter()
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                if not reader.fieldnames:
                    continue
                column = next((c for c in jurisdiction_columns if c in reader.fieldnames), None)
                if column is None:
                    continue
                for row in reader:
                    jurisdiction = (row.get(column) or "").strip()
                    if jurisdiction:
                        counts[jurisdiction] += 1
        except OSError:
            continue
        if counts:
            return [
                {"jurisdiction": jurisdiction, "count": count}
                for jurisdiction, count in counts.most_common(10)
            ]

    return []


# File descriptions for known output files
_FILE_DESCRIPTIONS = {
    "records_raw.parquet": "Loaded records, one row per record, before any rules",
    "records.parquet": "Records with their track and every cleaning target",
    "units.parquet": "One row per unit — an exact group, or a record on its own",
    "unit_members.parquet": "Which records make up each unit",
    "pairs.parquet": "Every scored pair, with its bucket and how it was decided",
    "blocking_report.json": "Pairs each blocking rule would make, per track, against the budget",
    "score_eval.json": "How the exact groups and the accepted pairs compare with the earlier grouping",
    "ruleset.json": "The rules this run used, frozen at the moment it started",
    "linkage_settings.json": "The comparisons, the blocking rules and the lines this run used",
    "exact_groups.parquet": "Every group a match key made, merged or held",
    "exact_eval.json": "How the match keys compare with the earlier grouping",
    "clusters.parquet": "One row per unit: its cluster, what the gate found, and the strongest link",
    "entities.parquet": "One row per record: its entity ID, why it is there, and the settled values",
    "entity_report.json": "What the entity stage did, and every ID that was claimed twice",
    "run_manifest.json": "What produced this run: the input file and its hash, the code, the rules, the models",
    "bucketing_history.json": "Every change to the lines that set the buckets, oldest first",
    "model_state.json": "Which model version scored this run, and where its lines are",
    "contradictions.json": "Pairs a reviewer kept apart that a match key then merged",
    "events.parquet": "The evidence rows behind each record",
    "events.jsonl": "What the run did, step by step, as it ran",
    "pipeline.log": "Everything the stages printed while the run was working",
    "splink_model_person.json": "The trained comparison weights for the people track",
    "splink_model_organisation.json": "The trained comparison weights for the organisations track",
}


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    # One dataset, so one input file: either the id of a completed chunked
    # upload, or the name of a file already sitting in the uploads directory.
    input_upload_id: str | None = None
    input_filename: str | None = None
    config_version: int = 1
    threshold_high: float | None = None
    threshold_review: float | None = None
    auto_accept_threshold: float | None = None
    review_lower_bound: float | None = None
    render_diagnostics: bool = True
    quick_mode: bool = False


class ReBucketRequest(BaseModel):
    threshold_high: float | None = None
    threshold_review: float | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _thresholds_for_config(db_path: str, config_version: int) -> tuple[float, float]:
    config = get_version(db_path, config_version)
    if not config:
        raise HTTPException(status_code=400, detail=f"Config version {config_version} not found")
    try:
        settings = json.loads(config["linkage_settings"])
    except (TypeError, json.JSONDecodeError):
        settings = {}
    return (
        float(settings.get("match_probability_threshold_high", 0.92)),
        float(settings.get("match_probability_threshold_review", 0.50)),
    )


@router.post("")
def create_run(body: CreateRunRequest, user_name: str = Depends(current_user)):
    """Create and enqueue a new pipeline run."""
    db_path = _db_path()
    data_dir = _data_dir_from_main()

    # Resolve thresholds (accept both naming conventions)
    default_high, default_review = _thresholds_for_config(db_path, body.config_version)
    t_high = body.threshold_high or body.auto_accept_threshold or default_high
    t_review = body.threshold_review or body.review_lower_bound or default_review

    # Resolve the one input file — accept either a direct filename or an upload_id
    uploads_dir = data_dir / "uploads"
    allowed = _allowed_input_suffixes()
    input_label = get_profile().input.label

    def _resolve_file(filename, upload_id):
        if upload_id:
            rows = query_db(db_path, "SELECT * FROM upload_sessions WHERE upload_id = ?", (upload_id,))
            if not rows:
                raise HTTPException(status_code=400, detail="Upload session not found")
            upload = rows[0]
            if upload["status"] != "complete":
                raise HTTPException(status_code=400, detail="Upload is not complete")
            stored = upload["stored_filename"] or upload["filename"]
            p = uploads_dir / stored
            if not p.exists():
                raise HTTPException(status_code=400, detail="Uploaded file is missing on disk")
            return p, upload["filename"]
        if filename:
            p = uploads_dir / filename
            if p.exists():
                return p, filename
        # The chunked upload handler saves reassembled files with the original
        # filename in the uploads dir. Fall back to the most recent candidate
        # when we can't match by name. With only 3-5 users and sequential runs,
        # this is adequate.
        candidates = sorted(
            [f for f in uploads_dir.iterdir() if f.is_file() and f.suffix.lower() in allowed],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0], candidates[0].name
        raise HTTPException(
            status_code=400,
            detail=f"{input_label} not found. Upload a file first.",
        )

    input_path, input_fname = _resolve_file(body.input_filename, body.input_upload_id)
    suffix = input_path.suffix.lower()
    if suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{input_path.name}' must be a {' or '.join(sorted(allowed))} file "
                f"(got '{suffix or 'no extension'}'). Re-select the correct file."
            ),
        )

    run_id = _generate_run_id(db_path)

    # Insert the run row
    write_db(
        db_path,
        """INSERT INTO runs (id, status, config_version, input_filename,
                             threshold_high, threshold_review, triggered_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            "pending",
            body.config_version,
            input_fname,
            t_high,
            t_review,
            user_name,
        ),
    )

    log_event(
        db_path,
        user=user_name,
        kind="run",
        description=f"Created pipeline run {run_id}",
        metadata={
            "run_id": run_id,
            "config_version": body.config_version,
            "input_filename": input_fname,
        },
    )

    # Enqueue (starts immediately if nothing is running)
    pipeline_runner.enqueue_run(
        db_path=db_path,
        data_dir=str(data_dir),
        run_id=run_id,
        input_path=str(input_path),
        config_version=body.config_version,
        threshold_high=t_high,
        threshold_review=t_review,
        render_diagnostics=body.render_diagnostics,
        quick_mode=body.quick_mode,
    )

    # Return the run row
    rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
    return rows[0] if rows else {"id": run_id, "status": "pending"}


def _normalize_counts(raw):
    """The run's counts, in the camelCase the screens read.

    Only what this pipeline produces. The two-dataset tool this app was copied
    from wrote a dozen more keys — matched titles, ambiguous candidates, a match
    rate — and they sat at zero on every dedupe run, which is worse than absent:
    a zero looks like a measurement.
    """
    if not raw:
        return None

    return {
        # Stage 0 — what the loader read and made of it.
        "inputRows": raw.get("input_rows", 0),
        "inputRowsDropped": raw.get("input_rows_dropped", 0),
        "eventRows": raw.get("event_rows", 0),
        "recordsTotal": raw.get("records_total", 0),
        "recordsPerson": raw.get("records_person", 0),
        "recordsOrganisation": raw.get("records_organisation", 0),
        "recordsLabelled": raw.get("records_labelled", 0),
        "recordsUnreviewed": raw.get("records_unreviewed", 0),
        # Stage 2 — what the match keys settled without a human. Precision and
        # recall are null when there is nothing to score against, which is a
        # different thing from scoring zero.
        "exactMergedGroups": raw.get("exact_merged_groups", 0),
        "exactMergedRecords": raw.get("exact_merged_records", 0),
        "exactHeldGroups": raw.get("exact_held_groups", 0),
        "exactHeldRecords": raw.get("exact_held_records", 0),
        "exactEntitiesAfter": raw.get("exact_entities_after", 0),
        "exactConflicts": raw.get("exact_conflicts", 0),
        "exactPairPrecision": raw.get("exact_pair_precision"),
        "exactPairRecall": raw.get("exact_pair_recall"),
        "exactSplitByHuman": raw.get("exact_split_by_human", 0),
        "exactMergedByHuman": raw.get("exact_merged_by_human", 0),
        # Stage 3 — the units and what Splink made of them.
        "unitsTotal": raw.get("units_total", 0),
        "unitsPerson": raw.get("units_person", 0),
        "unitsOrganisation": raw.get("units_organisation", 0),
        # Comparison levels EM could not learn: they contribute nothing to the
        # score, and the run is otherwise silent about it.
        "untrainedComparisons": raw.get("untrained_comparisons", 0),
        "pairsScored": raw.get("pairs_scored", 0),
        "pairsAccept": raw.get("pairs_accept", 0),
        "pairsReview": raw.get("pairs_review", 0),
        "pairsReject": raw.get("pairs_reject", 0),
        "pairsDecidedByImport": raw.get("pairs_decided_by_import", 0),
        "pairsImportDisagrees": raw.get("pairs_import_disagrees", 0),
        # Vetoes (docs/RULESET.md). `pairsVetoedFromAccept` is the one that
        # matters: pairs the run would otherwise have auto-accepted.
        "pairsVetoed": raw.get("pairs_vetoed", 0),
        "pairsVetoedFromAccept": raw.get("pairs_vetoed_from_accept", 0),
        "vetoConflictsImport": raw.get("veto_conflicts_import", 0),
        "entitiesAfterScore": raw.get("entities_after_score", 0),
        "scorePairPrecision": raw.get("score_pair_precision"),
        "scorePairRecall": raw.get("score_pair_recall"),
        # Human labels.
        "labelsTotal": raw.get("labels_total", 0),
        "labelsTrue": raw.get("labels_true", 0),
        "labelsFalse": raw.get("labels_false", 0),
        "labelsSatisfied": raw.get("labels_satisfied", 0),
        "labelsForced": raw.get("labels_forced", 0),
        "labelsApplied": raw.get("labels_applied", 0),
        "labelsInLibrary": raw.get("labels_in_library", 0),
        "labelContradictions": raw.get("label_contradictions", 0),
        "entitiesAfterHuman": raw.get("entities_after_human",
                                      raw.get("entities_after_score", 0)),
        "humanPairPrecision": raw.get("human_pair_precision",
                                      raw.get("score_pair_precision")),
        "humanPairRecall": raw.get("human_pair_recall", raw.get("score_pair_recall")),
        # Stages 4 and 5 — the clusters, the open queue and the proposal.
        "clustersTotal": raw.get("clusters_total", 0),
        "clustersWithheld": raw.get("clusters_withheld", 0),
        "clustersByStatus": raw.get("clusters_by_status", {}),
        "heldGroupsOpen": raw.get("held_groups_open", 0),
        "reviewQueue": raw.get("review_queue", 0),
        "decisionsTotal": raw.get("decisions_total", 0),
        "crossTrackIds": raw.get("cross_track_ids", 0),
        "entitiesProposed": raw.get("entities_proposed", 0),
        "entitiesNew": raw.get("entities_new", 0),
        "entitiesKept": raw.get("entities_kept", 0),
        "entitiesMerged": raw.get("entities_merged", 0),
        "attributeTies": raw.get("attribute_ties", 0),
        "idCollisions": raw.get("id_collisions", 0),
        "publishedAt": raw.get("published_at"),
        # Stage 3b — which model, if any, decided this run, and on what lines
        # (docs/MODEL_API.md). The three per-track values are objects keyed by
        # track. They are recorded on the run, so activating a newer version
        # never changes what a finished run says it was decided by.
        # `modelGraded` is true only when every track that scored is graded, so
        # a half-graded run never reads as decided by the model.
        "modelActive": bool(raw.get("model_active", False)),
        "modelVersion": raw.get("model_version") or {},
        "modelAcceptLine": raw.get("model_accept_line") or {},
        "modelRejectLine": raw.get("model_reject_line") or {},
        "modelGraded": bool(raw.get("model_graded", False)),
        "modelWarning": raw.get("model_warning"),
        # Every count above defaults to 0, so a screen cannot tell "nothing yet"
        # from "zero" by value. These flags say which stages have run.
        "hasRecords": "records_total" in raw,
        "hasExact": "exact_merged_groups" in raw,
        "hasPairs": "pairs_scored" in raw,
        "hasUnits": "units_total" in raw,
        "hasLabels": bool(raw.get("labels_total", 0)),
        "hasEntities": "entities_proposed" in raw,
    }


@router.get("")
def list_runs():
    """List all runs, ordered by started_at DESC."""
    db_path = _db_path()
    rows = query_db(
        db_path,
        """SELECT id, label, status, started_at, finished_at, duration_secs,
                  triggered_by, config_version, input_filename,
                  error_message, current_stage, threshold_high, threshold_review,
                  counts_json
           FROM runs ORDER BY started_at DESC""",
    )
    result = []
    for r in rows:
        row = dict(r)
        if row.get("counts_json"):
            try:
                row["counts"] = _normalize_counts(json.loads(row["counts_json"]))
            except (json.JSONDecodeError, TypeError):
                row["counts"] = None
        else:
            row["counts"] = None
        result.append(row)
    return result


@router.get("/{run_id}")
def get_run(run_id: str):
    """Return full run detail with parsed counts."""
    db_path = _db_path()
    rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    row = dict(rows[0])
    if row.get("counts_json"):
        try:
            row["counts"] = _normalize_counts(json.loads(row["counts_json"]))
        except (json.JSONDecodeError, TypeError):
            row["counts"] = None
    else:
        row["counts"] = None

    row["error_detail"] = None
    if row.get("error_detail_json"):
        try:
            row["error_detail"] = json.loads(row["error_detail_json"])
        except (json.JSONDecodeError, TypeError):
            row["error_detail"] = None

    return row


@router.delete("/{run_id}")
def delete_run(run_id: str, user_name: str = Depends(current_user)):
    """Delete a run and its output files."""
    db_path = _db_path()
    data_dir = _data_dir_from_main()
    rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")
    if rows[0]["status"] == "running":
        raise HTTPException(status_code=400, detail="Cannot delete a running pipeline")

    run_dir = data_dir / "runs" / run_id
    if run_dir.exists():
        import shutil
        shutil.rmtree(str(run_dir))

    write_db(db_path, "DELETE FROM runs WHERE id = ?", (run_id,))
    log_event(db_path, user=user_name, kind="run", description=f"Deleted run {run_id}")
    return Response(status_code=204)


@router.get("/{run_id}/progress")
async def run_progress(run_id: str):
    """SSE stream of pipeline progress events."""
    db_path = _db_path()
    rows = query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    queue = pipeline_runner.subscribe_progress(run_id)

    async def event_generator():
        try:
            while True:
                try:
                    event = await queue.get()
                    yield f"data: {json.dumps(event)}\n\n"
                    # Stop streaming after terminal events
                    if event.get("event") in ("complete", "failed", "cancelled"):
                        break
                except Exception:
                    break
        finally:
            pipeline_runner.unsubscribe_progress(run_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/cancel")
def cancel_run(run_id: str, user_name: str = Depends(current_user)):
    """Cancel a running or pending pipeline run."""
    db_path = _db_path()
    rows = query_db(db_path, "SELECT id, status FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    pipeline_runner.cancel_run(db_path, run_id)

    log_event(
        db_path,
        user=user_name,
        kind="run",
        description=f"Cancelled pipeline run {run_id}",
        metadata={"run_id": run_id},
    )

    return {"ok": True, "run_id": run_id, "status": "cancelled"}


@router.get("/{run_id}/manifest")
def get_manifest(run_id: str):
    """What produced this run: the input file, the code, the rules, the models.

    The file itself is ``run_manifest.json`` in the run folder; a few of its
    fields are also on the run row. ``bucketing`` is the history of every change
    to the lines that set the buckets, oldest first, and ``bucketing_now`` is the
    one in force.
    """
    run_dir = _run_dir_or_404(run_id)
    rows = query_db(_db_path(), "SELECT * FROM runs WHERE id = ?", (run_id,))
    row = dict(rows[0]) if rows else {}
    manifest = run_manifest.read(run_dir)
    if not manifest:
        # A run from before the manifest existed. Say what the row still knows
        # rather than 404: the answer is thin, not missing.
        manifest = {
            "run_id": run_id,
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "triggered_by": row.get("triggered_by"),
            "code_version": row.get("code_version") or "unknown",
            "config_version": row.get("config_version"),
            "thresholds": {
                "accept_line": row.get("threshold_high"),
                "review_line": row.get("threshold_review"),
                "lowest_score_kept": None,
            },
            "input": {
                "filename": row.get("input_filename"),
                "sha256": row.get("input_sha256"),
                "size_bytes": row.get("input_bytes"),
                "row_count": row.get("input_rows"),
                "uploaded_at": row.get("input_uploaded_at"),
            },
            "libraries": {}, "references": [], "scorers": {},
            "partial": True,
        }
    history = bucketing_history.read(run_dir)
    return {**manifest, "bucketing": history,
            "bucketing_now": history[-1] if history else None}


@router.get("/{run_id}/files")
def list_files(run_id: str):
    """List output files in the run directory."""
    data_dir = _data_dir_from_main()
    run_dir = data_dir / "runs" / run_id

    if not run_dir.exists():
        return []

    files = []
    for item in sorted(run_dir.iterdir()):
        if item.is_file():
            files.append({
                "name": item.name,
                "size": _human_size(item.stat().st_size),
                "size_bytes": item.stat().st_size,
                "description": _FILE_DESCRIPTIONS.get(item.name, ""),
            })
        elif item.is_dir():
            # List contents of sub-directories (config/, diagnostics/)
            for sub_item in sorted(item.iterdir()):
                if sub_item.is_file():
                    rel = f"{item.name}/{sub_item.name}"
                    files.append({
                        "name": rel,
                        "size": _human_size(sub_item.stat().st_size),
                        "size_bytes": sub_item.stat().st_size,
                        "description": _FILE_DESCRIPTIONS.get(sub_item.name, ""),
                    })

    return files


@router.get("/{run_id}/files/all")
def download_all_files(run_id: str, user_name: str = Depends(current_user)):
    """Download all run output files as a zip archive."""
    data_dir = _data_dir_from_main()
    run_dir = data_dir / "runs" / run_id

    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run directory not found")

    log_event(
        _db_path(),
        user=user_name,
        kind="export",
        description=f"Downloaded all files for run {run_id}",
        metadata={"run_id": run_id},
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in run_dir.rglob("*"):
            if item.is_file():
                arcname = str(item.relative_to(run_dir))
                zf.write(item, arcname)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={run_id}.zip"},
    )


@router.get("/{run_id}/files/{filename:path}")
def download_file(run_id: str, filename: str, user_name: str = Depends(current_user)):
    """Download a single file from the run directory."""
    # Path traversal safety
    if ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    data_dir = _data_dir_from_main()
    file_path = data_dir / "runs" / run_id / filename

    # Extra safety: ensure resolved path is under the run dir
    run_dir = data_dir / "runs" / run_id
    try:
        file_path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    log_event(
        _db_path(),
        user=user_name,
        kind="export",
        description=f"Downloaded {filename} from run {run_id}",
        metadata={"run_id": run_id, "filename": filename},
    )

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
    )


@router.get("/{run_id}/timeline")
def get_timeline(run_id: str):
    """Return the events.jsonl file as a JSON array."""
    data_dir = _data_dir_from_main()
    events_path = data_dir / "runs" / run_id / "events.jsonl"

    if not events_path.exists():
        return []

    events = []
    with open(events_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    return events


@router.get("/{run_id}/diagnostics")
def get_diagnostics(run_id: str):
    """Return diagnostics data: probability histogram, feature weights, thresholds, band counts."""
    db_path = _db_path()
    data_dir = _data_dir_from_main()
    run_dir = data_dir / "runs" / run_id

    # ------------------------------------------------------------------
    # 1. Fetch thresholds from DB
    # ------------------------------------------------------------------
    rows = query_db(
        db_path,
        "SELECT threshold_high, threshold_review FROM runs WHERE id = ?",
        (run_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    threshold_high = float(rows[0]["threshold_high"] or 0.92)
    threshold_review = float(rows[0]["threshold_review"] or 0.50)
    thresholds = {"threshold_high": threshold_high, "threshold_review": threshold_review}

    # ------------------------------------------------------------------
    # 2. Probability histogram from linkage_scored.parquet
    # ------------------------------------------------------------------
    parquet_path = run_dir / "linkage_scored.parquet"
    num_bins = 20
    histogram: list[int] = [0] * num_bins

    score_column = "match_probability"
    if parquet_path.exists():
        try:
            import pandas as pd
            import numpy as np

            df = pd.read_parquet(parquet_path)
            # Plot the score the run is ACTUALLY bucketed on, so the chart matches the
            # rows. Stage 3 uses gbt_score only when gbt_score_column is set in the run's
            # config; otherwise the raw Splink probability. A gbt_score column may exist
            # (auto-computed) without being the bucketing score — don't plot it then.
            use_gbt = False
            settings_path = run_dir / "config" / "linkage_settings.json"
            if settings_path.is_file():
                try:
                    use_gbt = bool(json.loads(settings_path.read_text(encoding="utf-8")).get("gbt_score_column"))
                except Exception:
                    use_gbt = False
            score_column = "gbt_score" if (use_gbt and "gbt_score" in df.columns) else "match_probability"
            probs = pd.to_numeric(df[score_column], errors="coerce").dropna().values
            counts, _ = np.histogram(probs, bins=num_bins, range=(0.0, 1.0))
            histogram = counts.tolist()
        except Exception:
            histogram = [0] * num_bins

    # ------------------------------------------------------------------
    # 3. Band counts derived from histogram + thresholds
    # ------------------------------------------------------------------
    bin_width = 1.0 / num_bins  # 0.05
    auto_accept = 0
    review = 0
    dropped = 0

    for i, count in enumerate(histogram):
        # Representative midpoint of this bin
        bin_mid = (i + 0.5) * bin_width
        if bin_mid >= threshold_high:
            auto_accept += count
        elif bin_mid >= threshold_review:
            review += count
        else:
            dropped += count

    band_counts = {"auto_accept": auto_accept, "review": review, "dropped": dropped}

    # ------------------------------------------------------------------
    # 4. Feature weights from splink_model.json
    # ------------------------------------------------------------------
    model_path = run_dir / "splink_model.json"
    feature_weights: list[dict] = []

    if model_path.exists():
        try:
            with open(model_path, encoding="utf-8") as fh:
                model = json.load(fh)

            for comp in model.get("comparisons", []):
                name = comp.get("output_column_name", "unknown")
                # Use the first non-null level's m/u as representative values
                # (typically the best-matching level, i.e. exact match level)
                best_m: float | None = None
                best_u: float | None = None
                for level in comp.get("comparison_levels", []):
                    if level.get("is_null_level"):
                        continue
                    m = level.get("m_probability")
                    u = level.get("u_probability")
                    if m is not None and u is not None:
                        best_m = float(m)
                        best_u = float(u)
                        break  # first non-null level is the highest-specificity match

                if best_m is None or best_u is None:
                    continue

                # Discriminating power: log2(m/u), clamped to avoid div-by-zero
                if best_u > 0 and best_m > 0:
                    importance = round(math.log2(best_m / best_u), 4)
                else:
                    importance = 0.0

                feature_weights.append({
                    "name": name,
                    "m": round(best_m, 6),
                    "u": round(best_u, 6),
                    "importance": importance,
                })
        except Exception:
            feature_weights = []

    # ------------------------------------------------------------------
    # 5. Top jurisdictions by match count
    # ------------------------------------------------------------------
    top_jurisdictions: list[dict] = []

    top_jurisdictions = _top_jurisdictions_from_outputs([
        run_dir / "matches_high_confidence.csv",
        run_dir / "merged_dataset.csv",
        run_dir / "matches_for_review.csv",
        run_dir / "matches_ambiguous.csv",
    ])

    # ------------------------------------------------------------------
    # 6. Most-applied name rules from standardisation_report.txt
    # ------------------------------------------------------------------
    top_rules: list[dict] = []

    report_path = run_dir / "standardisation_report.txt"
    if report_path.exists():
        try:
            import re as _re

            text = report_path.read_text(encoding="utf-8")
            # Parse "Total changed: N / M" lines per section to extract
            # rule hit summaries.  The report has four sections:
            #   OCOD Name Standardisation, ROE Name Standardisation,
            #   OCOD Jurisdiction Mapping, ROE Jurisdiction Mapping
            sections = _re.split(r"^---\s*(.+?)\s*---\s*$", text, flags=_re.MULTILINE)
            # sections[0] is header, then pairs of (section_title, section_body)
            for i in range(1, len(sections), 2):
                section_title = sections[i].strip()
                section_body = sections[i + 1] if i + 1 < len(sections) else ""
                total_match = _re.search(
                    r"Total changed:\s*([\d,]+)\s*/\s*([\d,]+)",
                    section_body,
                )
                if total_match:
                    hits = int(total_match.group(1).replace(",", ""))
                    total = int(total_match.group(2).replace(",", ""))
                    # Determine effect from section title
                    if "Name" in section_title:
                        effect = "name standardisation"
                    elif "Jurisdiction" in section_title:
                        effect = "jurisdiction mapping"
                    else:
                        effect = "standardisation"
                    top_rules.append({
                        "rule": section_title.strip("() "),
                        "hits": hits,
                        "effect": f"{hits:,} of {total:,} rows changed ({effect})",
                    })
        except Exception:
            top_rules = []

    # Which score this run is bucketed on, and the word for it. One definition,
    # in `pairs_reader`, so the model panel need not fetch a histogram to learn
    # one word. The chart's own column wins when it read a file of its own.
    scored = pairs_reader.score_column_for(str(run_dir))
    if parquet_path.exists():
        scored = {**scored, "score_column": score_column,
                  "score_column_label": pairs_reader.SCORE_COLUMNS.get(
                      score_column, {}).get("label", scored["score_column_label"])}
    return {
        "histogram": histogram,
        "score_column": scored["score_column"],
        "score_column_label": scored["score_column_label"],
        "scorer": scored["scorer"],
        "feature_weights": feature_weights,
        "thresholds": thresholds,
        "band_counts": band_counts,
        "top_jurisdictions": top_jurisdictions,
        "top_rules": top_rules,
    }


@router.get("/{run_id}/records")
def get_records(
    run_id: str,
    track: str | None = Query(None, description="person | organisation"),
    state: str | None = Query(None, description="labelled | unreviewed"),
    q: str | None = Query(None, description="Case-insensitive substring over name, all spellings and record id"),
    sort: str = Query(records_reader.DEFAULT_SORT, description="Any column key from the profile's records"),
    order: str = Query("asc", description="asc | desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(records_reader.DEFAULT_LIMIT, ge=1, le=records_reader.MAX_LIMIT),
):
    """Return one page of a run's loaded records.

    ``total`` reflects the filters; ``counts`` describe the whole run and ignore
    them, so the track and state tabs stay stable while a search narrows the list.
    """
    db_path = _db_path()
    run_dir = str(_data_dir_from_main() / "runs" / run_id)

    if not query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        return records_reader.get_records(
            run_dir=run_dir,
            track=track,
            state=state,
            q=q,
            sort=sort,
            order=order,
            offset=offset,
            limit=limit,
        )
    except records_reader.RecordsNotFound:
        raise HTTPException(status_code=404, detail="Run has no records yet")
    except records_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _run_dir_or_404(run_id: str) -> str:
    """The run's directory, once we know the run itself exists."""
    if not query_db(_db_path(), "SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="Run not found")
    return str(_data_dir_from_main() / "runs" / run_id)


def _read_json_or_404(path, missing: str):
    """A JSON file a stage wrote, or a 404 saying the stage has not run."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail=missing)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read {path.name}: {exc}")


@router.get("/{run_id}/exact-groups")
def get_exact_groups(
    run_id: str,
    track: str | None = Query(None, description="person | organisation"),
    key: str | None = Query(None, description="Only groups a given match key built"),
    status: str | None = Query(None, description="merged | held"),
    agreement: str | None = Query(
        None, description="consistent | conflict | extends | new (merged groups only)"
    ),
    q: str | None = Query(
        None, description="Case-insensitive substring over member names, record ids and the group id"
    ),
    sort: str = Query(exact_groups_reader.DEFAULT_SORT, description="size | priority | name"),
    order: str = Query("desc", description="asc | desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(
        exact_groups_reader.DEFAULT_LIMIT, ge=1, le=exact_groups_reader.MAX_LIMIT
    ),
):
    """One page of the groups the match keys made, merged and held.

    ``total`` follows the filters; ``counts`` describe the whole run and ignore
    them, so the tabs stay still while a search narrows the list.
    """
    run_dir = _run_dir_or_404(run_id)
    try:
        return exact_groups_reader.get_groups(
            run_dir=run_dir, track=track, key=key, status=status,
            agreement=agreement, q=q, sort=sort, order=order,
            offset=offset, limit=limit,
        )
    except exact_groups_reader.ExactGroupsNotFound:
        raise HTTPException(status_code=404, detail="Run has no exact groups yet")
    except exact_groups_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{run_id}/exact-groups/{group_id}")
def get_exact_group(
    run_id: str,
    group_id: str,
    events: int = Query(0, ge=0, le=1, description="1 adds the members' evidence rows"),
):
    """One group with its member records, capped at 500."""
    run_dir = _run_dir_or_404(run_id)
    try:
        group = exact_groups_reader.get_group(run_dir, group_id, with_events=bool(events))
    except exact_groups_reader.ExactGroupsNotFound:
        raise HTTPException(status_code=404, detail="Run has no exact groups yet")
    if group is None:
        raise HTTPException(status_code=404, detail=f"No group '{group_id}' in this run")
    return group


@router.get("/{run_id}/exact-eval")
def get_exact_eval(run_id: str):
    """What stage 2 measured: the per-key stats, the totals, and the label scores."""
    run_dir = _run_dir_or_404(run_id)
    path = exact_groups_reader.eval_path(run_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Run has no exact groups yet")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read the evaluation: {exc}")


@router.get("/{run_id}/pairs/histogram")
def get_pairs_histogram(
    run_id: str,
    track: str | None = Query(None, description="person | organisation"),
    bins: int = Query(pairs_reader.DEFAULT_BINS, ge=1, le=pairs_reader.MAX_BINS),
):
    """The scored pairs as a histogram, split by bucket and by import agreement.

    This is what the threshold panel draws, so moving a line can be previewed
    before it is committed.
    """
    run_dir = _run_dir_or_404(run_id)
    try:
        return pairs_reader.get_histogram(run_dir, track=track, bins=bins,
                                          labels=_run_labels(_db_path()))
    except pairs_reader.PairsNotFound:
        raise HTTPException(status_code=404, detail="Run has no scored pairs yet")
    except pairs_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{run_id}/pairs")
def get_pairs(
    run_id: str,
    track: str | None = Query(None, description="person | organisation"),
    bucket: str | None = Query(None, description="accept | review | reject"),
    decided_by: str | None = Query(
        None, description="score | import | human | model | veto"),
    import_state: str | None = Query(
        None, alias="import", description="agrees | disagrees | unknown"
    ),
    labelled: str | None = Query(None, description="yes | no"),
    vetoed: str | None = Query(None, description="yes | no"),
    held: str | None = Query(None, description="hide | only"),
    min_score: float | None = Query(None, ge=0, le=1),
    max_score: float | None = Query(None, ge=0, le=1),
    min_gbt: float | None = Query(None, ge=0, le=1),
    max_gbt: float | None = Query(None, ge=0, le=1),
    q: str | None = Query(
        None, description="Case-insensitive substring over either side's name or unit id"
    ),
    sort: str = Query(pairs_reader.DEFAULT_SORT,
                      description="score | priority | name | useful"),
    order: str = Query("desc", description="asc | desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(pairs_reader.DEFAULT_LIMIT, ge=1, le=pairs_reader.MAX_LIMIT),
):
    """One page of the pairs stage 3 scored, both units side by side.

    ``total`` follows the filters; ``counts`` describe the whole run and ignore
    them, so the chips stay still while a search narrows the list.
    """
    run_dir = _run_dir_or_404(run_id)
    try:
        body = pairs_reader.get_pairs(
            run_dir=run_dir, track=track, bucket=bucket, decided_by=decided_by,
            import_state=import_state, held=held, min_score=min_score,
            max_score=max_score, min_gbt=min_gbt, max_gbt=max_gbt,
            q=q, sort=sort, order=order,
            offset=offset, limit=limit, labelled=labelled, vetoed=vetoed,
            labels=_run_labels(_db_path()),
        )
        # Which lines put these pairs where they are. The entry in force, not
        # three numbers stamped on every row (docs/PROVENANCE.md).
        body["bucketing"] = bucketing_history.current(run_dir)
        return body
    except pairs_reader.PairsNotFound:
        raise HTTPException(status_code=404, detail="Run has no scored pairs yet")
    except pairs_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{run_id}/pairs/{pair_id}")
def get_pair(run_id: str, pair_id: str):
    """One pair with its members, its evidence rows and a per-comparison explanation."""
    run_dir = _run_dir_or_404(run_id)
    try:
        pair = pairs_reader.get_pair(run_dir, pair_id,
                                     labels=_run_labels(_db_path()))
        if isinstance(pair, dict):
            pair["bucketing"] = bucketing_history.current(run_dir)
    except pairs_reader.PairsNotFound:
        raise HTTPException(status_code=404, detail="Run has no scored pairs yet")
    except pairs_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if pair is None:
        raise HTTPException(status_code=404, detail=f"No pair '{pair_id}' in this run")
    return pair


class LabelItem(BaseModel):
    pair_id: str
    is_match: str
    notes: str | None = None
    evidence_url: str | None = None


class LabelBatch(BaseModel):
    labels: list[LabelItem]
    provenance: str = "manual"


def _run_labels(db_path: str):
    from app.services import pair_labels

    return pair_labels.labels_frame(db_path)


def _refresh_after_labels(db_path: str, run_dir: str, run_id: str) -> dict:
    """Redo a run's counts and evaluation after its labels changed.

    ``pairs.parquet`` is left exactly as scoring left it: it holds the score and
    the import overlay, and the human overlay is joined on wherever the pairs
    are read. Only the derived numbers have to move.
    """
    from app.pipeline.dedupe.stage_3_score import refresh_after_labels
    from app.services import run_counts

    return run_counts.merge(db_path, run_id,
                            refresh_after_labels(run_dir, _run_labels(db_path)))


@router.post("/{run_id}/labels")
def save_labels(run_id: str, body: LabelBatch, user_name: str = Depends(current_user)):
    """Record a human decision on up to 500 pairs of this run.

    Append-only: a new decision supersedes the old one and the old row stays, so
    who said what is never lost. The pairs file is not rewritten — only the
    run's counts and its evaluation move.
    """
    from app.services import pair_labels

    db_path = _db_path()
    run_dir = _run_dir_or_404(run_id)
    if len(body.labels) > pair_labels.MAX_BATCH:
        raise HTTPException(
            status_code=422,
            detail=(f"At most {pair_labels.MAX_BATCH} labels per request; "
                    f"got {len(body.labels)}"),
        )
    if not body.labels:
        raise HTTPException(status_code=400, detail="No labels to save")

    row = query_db(db_path, "SELECT config_version FROM runs WHERE id = ?", (run_id,))
    config_version = row[0]["config_version"] if row else None

    try:
        provenance = pair_labels.normalise_provenance(
            body.provenance, pair_labels.HUMAN_PROVENANCES
        )
        parsed = [(*pair_labels.split_pair_id(item.pair_id), item)
                  for item in body.labels]
    except pair_labels.LabelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        units = pairs_reader.unit_summary(
            run_dir, [value for left, right, _ in parsed for value in (left, right)]
        )
    except pairs_reader.PairsNotFound:
        raise HTTPException(status_code=404, detail="Run has no scored pairs yet")

    saved = superseded = 0
    for left, right, item in parsed:
        missing = [value for value in (left, right) if value not in units]
        if missing:
            raise HTTPException(
                status_code=400, detail=f"Unit '{missing[0]}' is not in run {run_id}"
            )
        try:
            label, replaced = pair_labels.save_label(
                db_path, left, right, item.is_match,
                reviewer=user_name, provenance=provenance,
                notes=item.notes, evidence_url=item.evidence_url,
                track=units[left]["track"],
                name_a=units[left]["name"], name_b=units[right]["name"],
                run_id=run_id, config_version=config_version,
            )
        except pair_labels.LabelError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        saved += 1
        superseded += replaced
        log_event(
            db_path, user=user_name or "unknown", kind="label",
            description=(f"Marked {label['is_match']} · "
                         f"{units[left]['name']} ↔ {units[right]['name']}"),
            metadata={"label_id": label["id"], "run_id": run_id,
                      "pair_id": f"{left}|{right}", "is_match": label["is_match"],
                      "provenance": provenance},
        )

    counts = _refresh_after_labels(db_path, run_dir, run_id)
    return {"saved": saved, "superseded": superseded,
            "counts": _normalize_counts(counts)}


@router.delete("/{run_id}/labels/{pair_id}")
def delete_label(run_id: str, pair_id: str, user_name: str = Depends(current_user)):
    """Withdraw the active decision on one pair, keeping the row on record."""
    from app.services import pair_labels

    db_path = _db_path()
    run_dir = _run_dir_or_404(run_id)
    try:
        left, right = pair_labels.split_pair_id(pair_id)
    except pair_labels.LabelError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    label = pair_labels.withdraw_label(db_path, left, right)
    if label is None:
        raise HTTPException(
            status_code=404, detail=f"No active label on pair '{left}|{right}'"
        )
    log_event(
        db_path, user=user_name or "unknown", kind="label",
        description=f"Withdrew the {label['is_match']} label on {left} ↔ {right}",
        metadata={"label_id": label["id"], "run_id": run_id,
                  "pair_id": f"{left}|{right}"},
    )

    counts = _refresh_after_labels(db_path, run_dir, run_id)
    # What the pair falls back to now the decision is gone.
    pair = pairs_reader.get_pair(run_dir, f"{left}|{right}",
                                 labels=_run_labels(db_path))
    return {
        "pair_id": f"{left}|{right}",
        "bucket": pair["bucket"] if pair else None,
        "decided_by": pair["decided_by"] if pair else None,
        "counts": _normalize_counts(counts),
    }


class ApplyModelRequest(BaseModel):
    force: bool = False


@router.post("/{run_id}/apply-model")
def apply_model(run_id: str, body: ApplyModelRequest | None = None,
                user_name: str = Depends(current_user)):
    """Score this run with each track's active model and re-bucket on it.

    Splink is not re-run (`docs/MODEL.md`): the pairs it already produced are
    read back, given a `gbt_score`, and re-bucketed where a graded model is in
    charge. The overlays are unchanged — an imported agreement still accepts, a
    human label still wins.
    """
    from app.services import model_apply

    db_path = _db_path()
    run_dir = _run_dir_or_404(run_id)
    try:
        result = model_apply.apply_model(run_dir, _run_labels(db_path),
                                         force=bool(body and body.force),
                                         db_path=db_path,
                                         who=user_name or "unknown")
    except model_apply.ModelApplyError as exc:
        status = 409 if exc.detail.get("reason") else 400
        log_event(db_path, user=user_name or "unknown", kind="model",
                  description=f"Model not applied to run {run_id}: {exc}",
                  metadata={"run_id": run_id, **exc.detail})
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    # Merged, never replaced: applying a model recomputes stage 3's keys and no
    # others, and writing that partial dict back would blank the record, exact
    # group and entity counts the run screen decides its tabs from.
    merged = run_counts.merge(db_path, run_id, result["counts"])
    log_event(
        db_path, user=user_name or "unknown", kind="model",
        description=f"Applied the model to run {run_id} ("
                    + ", ".join(f"{t['track']} v{t['version']}"
                                for t in result["tracks"]) + ")",
        metadata={"run_id": run_id, "tracks": result["tracks"],
                  "reclustered": result["reclustered"],
                  "review_before": result["review_before"],
                  "review_after": result["review_after"]},
    )
    return {"ok": True, "tracks": result["tracks"],
            "counts": _normalize_counts(merged) or {},
            "reclustered": result["reclustered"],
            "review_before": result["review_before"],
            "review_after": result["review_after"]}


@router.post("/{run_id}/revert-model")
def revert_model(run_id: str, user_name: str = Depends(current_user)):
    """Take the model off this run and bucket on the Splink score again."""
    from app.services import model_apply

    db_path = _db_path()
    run_dir = _run_dir_or_404(run_id)
    try:
        result = model_apply.revert_model(run_dir, _run_labels(db_path),
                                          db_path=db_path,
                                          who=user_name or "unknown")
    except model_apply.ModelApplyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    merged = run_counts.merge(db_path, run_id, result["counts"])
    log_event(db_path, user=user_name or "unknown", kind="model",
              description=f"Reverted run {run_id} to the Splink score",
              metadata={"run_id": run_id, "reclustered": result["reclustered"]})
    return {"ok": True, "counts": _normalize_counts(merged) or {},
            "reclustered": result["reclustered"]}


@router.get("/{run_id}/contradictions")
def get_contradictions(run_id: str):
    """FALSE labels an exact match key has since overruled."""
    run_dir = _run_dir_or_404(run_id)
    return _read_json_or_404(
        Path(run_dir) / "contradictions.json", "Run has no scored pairs yet"
    )


@router.get("/{run_id}/score-eval")
def get_score_eval(run_id: str):
    """What stage 3 measured: buckets, entities, and the label scores."""
    run_dir = _run_dir_or_404(run_id)
    return _read_json_or_404(
        pairs_reader.score_eval_path(run_dir), "Run has no scored pairs yet"
    )


@router.get("/{run_id}/blocking-report")
def get_blocking_report(run_id: str):
    """Pairs each blocking rule would make, per track, against the budget."""
    run_dir = _run_dir_or_404(run_id)
    return _read_json_or_404(
        pairs_reader.blocking_report_path(run_dir), "Run has no blocking report yet"
    )


@router.post("/{run_id}/re-bucket")
def re_bucket(run_id: str, body: ReBucketRequest, user_name: str = Depends(current_user)):
    """Commit a new auto-accept / review threshold: re-partition the already-scored
    pairs (no Splink re-run) and re-apply labels so they stay paramount.

    Threshold changes reclassify only *unlabelled* pairs; a FALSE-labelled pair is
    re-suppressed by the label re-application that runs last.
    """
    db_path = _db_path()
    run_dir = _data_dir_from_main() / "runs" / run_id
    config_dir = run_dir / "config"
    if not query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="Run not found")
    if not (config_dir / "linkage_settings.json").is_file():
        raise HTTPException(status_code=400, detail="Run has no config to re-bucket")

    # A dedupe run keeps every pair down to the candidate floor, so a new bucket
    # line is a re-read of pairs.parquet. A run from the two-dataset pipeline
    # still goes through the old path.
    if pairs_reader.pairs_path(str(run_dir)).is_file():
        from app.services.pipeline_runner import rebucket_pairs

        try:
            counts = rebucket_pairs(
                db_path, str(run_dir), str(config_dir), run_id=run_id,
                threshold_high=body.threshold_high,
                threshold_review=body.threshold_review,
                who=user_name or "unknown",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        log_event(
            db_path, user=user_name or "unknown", kind="threshold",
            description=(f"Re-bucketed run {run_id} "
                         f"(high={body.threshold_high}, review={body.threshold_review})"),
            metadata={"run_id": run_id, "threshold_high": body.threshold_high,
                      "threshold_review": body.threshold_review, "counts": counts},
        )
        return {"ok": True, "counts": _normalize_counts(counts)}

    # A run with no pairs.parquet came from the two-dataset tool this app was
    # copied from. That pipeline is gone (docs/DESIGN.md D21), so say so rather
    # than failing somewhere deeper.
    raise HTTPException(
        status_code=400,
        detail=("This run has no scored pairs, so its lines cannot be moved. "
                "Start the run again."),
    )

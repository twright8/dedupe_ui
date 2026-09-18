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
from app.services import pipeline_runner
from app.services import records_reader
from app.services.audit_logger import log_event
from app.services.label_applier import apply_labels as _apply_labels
from app.services.match_reader import get_matches as _get_matches
from app.services.match_reader import get_matches_by_ocod as _get_matches_by_ocod
from app.services.match_reader import get_matches_by_roe as _get_matches_by_roe
from app.routers.labels import upsert_label

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
    "records.parquet": "Loaded records, one row per record",
    "matches_exact.csv": "Phase 1 deterministic exact matches",
    "matches_high_confidence.csv": "All high-confidence matches (exact + probabilistic)",
    "matches_for_review.csv": "Probabilistic matches in the review band",
    "matches_ambiguous.csv": "Ambiguous OCOD records with multiple close candidates",
    "unmatched_ocod.csv": "OCOD proprietors with no high-confidence match",
    "unmatched_roe.csv": "ROE entities with no high-confidence match",
    "merged_dataset.csv": "Full OCOD dataset (one row per title-proprietor) with matched OE numbers",
    "merged_roe.csv": "Full ROE register (one row per company) with matched land titles",
    "standardisation_report.txt": "Name and jurisdiction standardisation report",
    "events.jsonl": "Pipeline event log",
    "roe_preprocessed.parquet": "Preprocessed ROE data",
    "ocod_preprocessed.parquet": "Preprocessed OCOD data",
    "ocod_dedup.parquet": "Deduplicated OCOD data (for linkage)",
    "exact_matches.parquet": "Phase 1 exact match pairs",
    "linkage_scored.parquet": "Phase 2 Splink scored pairs",
    "splink_model.json": "Trained Splink model parameters",
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


class MarkUnlabelledRequest(BaseModel):
    bucket: str = "review"
    is_true_match: str = "FALSE"
    reviewer_notes: str | None = "Bulk marked unlabelled as FALSE"


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
    )

    # Return the run row
    rows = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
    return rows[0] if rows else {"id": run_id, "status": "pending"}


# counts_json keys written by the matching stages (not by the stage 0 loader).
_PAIR_COUNT_KEYS = (
    "matches_exact", "exact", "matches_high_confidence", "high", "high_confidence",
    "matches_for_review", "review", "matches_ambiguous", "ambiguous",
)


def _normalize_counts(raw):
    """Convert pipeline counts_json keys to frontend-expected keys."""
    if not raw:
        return None
    merged = raw.get("merged_dataset", raw.get("ocod", 0))
    exact = raw.get("matches_exact", raw.get("exact", 0))
    high = raw.get("matches_high_confidence", raw.get("high", raw.get("high_confidence", 0)))
    review = raw.get("matches_for_review", raw.get("review", 0))
    ambiguous = raw.get("matches_ambiguous", raw.get("ambiguous", 0))
    prob_accept = high - exact if high > exact else 0

    # merged_dataset is now one row per (title, proprietor); total_titles is the DISTINCT
    # title count, so title-grain fields (matchRate, unmatchedTitles, the land-title panel)
    # stay per TITLE. Legacy runs predate total_titles and had one row per title, so fall
    # back to the merged row count there — matched + unmatched still reconcile to the total.
    total_titles = raw.get("total_titles", merged)

    # matched_titles = distinct titles with at least one matched proprietor row. Falls back
    # to the entity-level accept count for runs that predate the field.
    matched_titles = raw.get("matched_titles", 0)
    if matched_titles == 0 and (exact + prob_accept) > 0:
        matched_titles = exact + prob_accept

    return {
        "ocod": total_titles,                               # Total land titles (title grain)
        "roe": raw.get("roe_preprocessed", raw.get("roe", 0)),
        "exact": exact,
        "probAccept": prob_accept,
        "review": review,
        "ambiguous": ambiguous,
        "unmatched": raw.get("unmatched_ocod", 0),          # per-OWNER (deduped proprietors)
        "matchedTitles": matched_titles,                    # per-TITLE (exact + fuzzy + confirmed)
        "matchedTitlesExact": raw.get("matched_titles_exact", 0),  # per-TITLE, exact only
        "matchedTitlesFormer": raw.get("matched_titles_former", 0),  # per-TITLE, matched via a former company name
        "unmatchedTitles": max(0, total_titles - matched_titles),  # per-TITLE; reconciles: matched + unmatched = total
        "totalProprietors": raw.get("total_proprietors", merged),  # per-PROPRIETOR rows in merged_dataset (title x proprietor)
        "matchedProprietors": raw.get("matched_proprietors", matched_titles),  # per-PROPRIETOR matched rows
        "unmatchedRoe": raw.get("unmatched_roe", 0),        # ROE companies with no match
        "mergedRoe": raw.get("merged_roe", 0),              # rows in the ROE-side export
        # Distinct entities behind the proprietor rows. "identified" = keyed on a real OE
        # number; "unidentified" = keyed on cleaned name + jurisdiction because no OE
        # number was found. Kept apart so the two are never summed into one claim.
        "distinctEntities": raw.get("distinct_entities", 0),
        "distinctEntitiesIdentified": raw.get("distinct_entities_identified", 0),
        "distinctEntitiesUnidentified": raw.get("distinct_entities_unidentified", 0),
        "matchRate": matched_titles / total_titles if total_titles > 0 else 0,
        "labelsInLibrary": raw.get("labels_in_library", 0),
        "droppedBelowReview": raw.get("dropped_below_review", 0),  # scored but below the review floor (retained in parquet)
        "labelsApplied": raw.get("labels_applied", 0),
        "labelsUnmatched": raw.get("labels_unmatched", 0),
        # Which model decided this run — authoritative, so the run list/detail can show it
        # per run without opening diagnostics. None on legacy runs (frontend then falls back).
        "decisionModel": raw.get("decision_model"),                # 'splink' | 'gbt:<version>' | None
        "decisionModelVersion": raw.get("decision_model_version"),
        "gbtWarning": raw.get("gbt_warning"),                      # set when a collapsed active model fell back to Splink
        # What the matcher alone decided, before human labels were overlaid on the
        # export. None on runs made before this was captured. Recurses once — the
        # baseline never carries a baseline of its own.
        "preLabels": _normalize_counts(raw.get("pre_labels")),
        # Dedupe stage 0 — what the loader read and made of it. Zero on a run
        # that predates the loader.
        "inputRows": raw.get("input_rows", 0),
        "inputRowsDropped": raw.get("input_rows_dropped", 0),
        "recordsTotal": raw.get("records_total", 0),
        "recordsPerson": raw.get("records_person", 0),
        "recordsOrganisation": raw.get("records_organisation", 0),
        "recordsLabelled": raw.get("records_labelled", 0),
        "recordsUnreviewed": raw.get("records_unreviewed", 0),
        # Every key above defaults to 0, so the frontend cannot tell "no pairs yet"
        # from "zero pairs" by value. These two flags say which stages have run.
        "hasRecords": "records_total" in raw,
        "hasPairs": any(k in raw for k in _PAIR_COUNT_KEYS),
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

    return {
        "histogram": histogram,
        "score_column": score_column,
        "feature_weights": feature_weights,
        "thresholds": thresholds,
        "band_counts": band_counts,
        "top_jurisdictions": top_jurisdictions,
        "top_rules": top_rules,
    }


@router.post("/{run_id}/apply-labels")
def apply_labels(run_id: str, user_name: str = Depends(current_user)):
    """Re-apply labels from the labels table to a completed run."""
    db_path = _db_path()
    data_dir = _data_dir_from_main()
    run_dir = str(data_dir / "runs" / run_id)

    rows = query_db(db_path, "SELECT id, status FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    result = _apply_labels(db_path, run_dir)

    # Re-applying labels changes the export, so the stored counts must move with it —
    # otherwise the screen shows pre-label numbers over post-label data.
    counts = pipeline_runner.refresh_counts_after_labels(
        db_path, run_dir, run_id, result
    )

    log_event(
        db_path,
        user=user_name,
        kind="label",
        description=f"Applied labels to run {run_id}",
        metadata={"run_id": run_id, "result": result},
    )

    return {**result, "counts": _normalize_counts(counts)}


@router.post("/{run_id}/mark-unlabelled")
def mark_unlabelled(
    run_id: str,
    body: MarkUnlabelledRequest | None = None,
    user_name: str = Depends(current_user),
):
    """Create labels for every currently unlabelled match in a bucket."""
    body = body or MarkUnlabelledRequest()
    db_path = _db_path()
    data_dir = _data_dir_from_main()
    run_dir = str(data_dir / "runs" / run_id)

    rows = query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    bucket = (body.bucket or "review").strip().lower()
    verdict = (body.is_true_match or "FALSE").strip().upper()
    if verdict == "X":
        verdict = "FALSE"
    if verdict not in {"TRUE", "FALSE"}:
        raise HTTPException(status_code=400, detail="is_true_match must be TRUE or FALSE")

    marked = 0
    skipped = 0
    page = 1
    per_page = 10000
    total_pages = 1

    while page <= total_pages:
        match_data = _get_matches(
            run_dir=run_dir,
            db_path=db_path,
            bucket=bucket,
            page=page,
            per_page=per_page,
        )
        total_pages = max(1, int(match_data.get("total_pages") or 0))
        for item in match_data.get("items", []):
            label = item.get("label")
            if label is not None and str(label).strip() != "":
                skipped += 1
                continue

            ocod_name_clean = (
                item.get("ocod_name_clean")
                or item.get("name_clean")
                or item.get("ocod_name_raw")
            )
            jurisdiction_clean = item.get("jurisdiction_clean")
            roe_company_number = item.get("roe_company_number")
            if not all([ocod_name_clean, jurisdiction_clean, roe_company_number]):
                skipped += 1
                continue

            upsert_label(
                db_path=db_path,
                ocod_name_clean=str(ocod_name_clean),
                jurisdiction_clean=str(jurisdiction_clean),
                roe_company_number=str(roe_company_number),
                ocod_name_raw=item.get("ocod_name_raw"),
                ocod_jurisdiction_raw=item.get("ocod_jurisdiction_raw") or jurisdiction_clean,
                is_true_match=verdict,
                reviewer=user_name,
                notes=body.reviewer_notes,
                run_id=run_id,
                provenance="bulk_review",
            )
            marked += 1

        page += 1

    log_event(
        db_path,
        user=user_name,
        kind="label",
        description=f"Bulk marked {marked} unlabelled {bucket} matches as {verdict} for run {run_id}",
        metadata={
            "run_id": run_id,
            "bucket": bucket,
            "is_true_match": verdict,
            "marked": marked,
            "skipped": skipped,
        },
    )

    return {
        "run_id": run_id,
        "bucket": bucket,
        "is_true_match": verdict,
        "marked": marked,
        "skipped": skipped,
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


@router.get("/{run_id}/matches")
def get_matches(
    run_id: str,
    bucket: str = Query(..., description="Match bucket: exact, high, review, ambiguous, unmatched_ocod, unmatched_roe"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=10000),
    jurisdiction: str | None = Query(None),
    search: str | None = Query(None),
    match_method: str | None = Query(None, description="Filter by match_method column (e.g. 'probabilistic')"),
):
    """Return paginated matches for a run, with label joins and feature mapping."""
    db_path = _db_path()
    data_dir = _data_dir_from_main()
    run_dir = str(data_dir / "runs" / run_id)

    # Validate run exists
    rows = query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")

    result = _get_matches(
        run_dir=run_dir,
        db_path=db_path,
        bucket=bucket,
        page=page,
        per_page=per_page,
        jurisdiction=jurisdiction,
        search=search,
        match_method=match_method,
    )
    for item in result.get("items", []):
        match_id = item.get("match_id") or item.get("id")
        if match_id and not str(match_id).startswith(f"{run_id}:"):
            item["match_id"] = f"{run_id}:{match_id}"
            item["id"] = item["match_id"]
    return result


def _stamp_candidate_ids(run_id: str, candidates: list, bucket_label: str) -> None:
    for c in candidates or []:
        ocod = c.get("ocod_unique_id") or c.get("ocod_name_clean") or "ocod"
        roe = c.get("roe_unique_id") or c.get("roe_company_number") or "roe"
        c["match_id"] = f"{run_id}:{bucket_label}:{ocod}:{roe}"
        c["id"] = c["match_id"]


@router.get("/{run_id}/matches/by-ocod")
def get_matches_by_ocod(
    run_id: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=10000),
    jurisdiction: str | None = Query(None),
    search: str | None = Query(None),
):
    """Entity-centric review: one OCOD entity per item with ranked ROE candidates + margin."""
    db_path = _db_path()
    run_dir = str(_data_dir_from_main() / "runs" / run_id)
    if not query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="Run not found")
    result = _get_matches_by_ocod(run_dir, db_path, page, per_page, jurisdiction, search)
    for entity in result.get("items", []):
        _stamp_candidate_ids(run_id, entity.get("candidates"), "review")
    return result


@router.get("/{run_id}/matches/by-roe")
def get_matches_by_roe(
    run_id: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=10000),
    jurisdiction: str | None = Query(None),
    search: str | None = Query(None),
):
    """ROE-centric review: one ROE company per item with its OCOD claimants."""
    db_path = _db_path()
    run_dir = str(_data_dir_from_main() / "runs" / run_id)
    if not query_db(db_path, "SELECT id FROM runs WHERE id = ?", (run_id,)):
        raise HTTPException(status_code=404, detail="Run not found")
    result = _get_matches_by_roe(run_dir, db_path, page, per_page, jurisdiction, search)
    for group in result.get("items", []):
        _stamp_candidate_ids(run_id, group.get("claimants"), "roe")
    return result


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

    from app.services.pipeline_runner import rebucket_run

    counts = rebucket_run(
        db_path, str(run_dir), str(config_dir), run_id=run_id,
        threshold_high=body.threshold_high, threshold_review=body.threshold_review,
    )
    log_event(
        db_path, user=user_name or "unknown", kind="threshold",
        description=f"Re-bucketed run {run_id} (high={body.threshold_high}, review={body.threshold_review})",
        metadata={"run_id": run_id, "threshold_high": body.threshold_high,
                  "threshold_review": body.threshold_review, "counts": counts},
    )
    return {"ok": True, "counts": counts}

# backend/app/routers/model_track.py
"""The per-track GBT API (`docs/MODEL_API.md`).

One model per track: status, versions, features, training as a background job,
and activation. Applying a model to a run lives on the runs router, beside the
rest of that run's endpoints.

This router is mounted after `routers/model.py`, which is `roe_ui`'s original
whole-app model API and still serves the two-dataset tool's screens. FastAPI
matches routes in registration order, so the few fixed paths that router owns
(`/api/model/eval-set`, `/api/model/train`) still reach it, and everything of
the shape `/api/model/{track}/...` reaches this one. `person` and `organisation`
are the only tracks, so nothing here can shadow a legacy path.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth import current_user
from app.model import features as feature_lib
from app.model import jobs, references as reference_lib, store
from app.profiles import get_profile
from app.profiles.base import TRACK_KEYS
from app.services.audit_logger import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/model", tags=["model"])


def _db_path() -> str:
    from app.main import DB_PATH

    return DB_PATH


def _data_dir() -> Path:
    from app.main import DATA_DIR

    return Path(DATA_DIR)


def _track(track: str) -> str:
    if track not in TRACK_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"track must be {' or '.join(TRACK_KEYS)}",
        )
    return track


def _track_label(track: str) -> str:
    for entry in get_profile().tracks:
        if entry.key == track:
            return entry.label
    return track.title()


def _run_dir(run_id: str) -> Path:
    directory = _data_dir() / "runs" / run_id
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    return directory


def _newest_pairs_columns() -> list[str]:
    """Column names of the newest run's `pairs.parquet`, or an empty list.

    The generic features follow the config's comparisons, so the feature list
    cannot be described without seeing a real pairs file.
    """
    runs = _data_dir() / "runs"
    if not runs.is_dir():
        return []
    candidates = sorted(
        (p for p in runs.iterdir() if (p / "pairs.parquet").exists()),
        key=lambda p: (p / "pairs.parquet").stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return []
    import pyarrow.parquet as pq

    try:
        return list(pq.read_schema(candidates[0] / "pairs.parquet").names)
    except Exception:  # noqa: BLE001 — a missing description is not an error
        return []


def _warnings_for(summary: dict | None) -> list[dict]:
    if not summary:
        return []
    report = store.load_report(summary["track"], summary["version"]) or {}
    return report.get("warnings") or []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@router.get("/{track}")
def model_status(track: str):
    """Everything the top of the model panel needs, in one call."""
    track = _track(track)
    versions = [store.summary(track, v) for v in reversed(store.list_versions(track))]
    versions = [v for v in versions if v]
    active = store.active_summary(track)
    return {
        "track": track,
        "label": _track_label(track),
        "active_version": store.get_active(track),
        "latest_version": store.latest_version(track),
        "can_auto_accept": store.can_auto_accept(track),
        "training": jobs.latest(track),
        "warnings": _warnings_for(active),
        "active": active,
        "versions": versions,
    }


@router.get("/{track}/versions/{version}")
def model_version(track: str, version: int):
    """One version, with its whole training report."""
    track = _track(track)
    summary = store.summary(track, version)
    if summary is None:
        raise HTTPException(status_code=404,
                            detail=f"No version {version} for track {track}")
    return {
        **summary,
        "report": store.load_report(track, version),
        "features": store.load_features(track, version),
    }


@router.get("/{track}/features")
def model_features(track: str, version: Optional[int] = Query(default=None)):
    """The feature list, in plain words.

    With no `version`, the active model's own list when there is one, otherwise
    what the next training run would build from the newest run's pairs file.
    `source` on the response says which of the three it is, because "the
    features the active model uses" and "the features a fresh train would use"
    are not always the same list.
    """
    track = _track(track)
    profile = get_profile()
    chosen = version if version is not None else store.get_active(track)
    if chosen is not None:
        stored = store.load_features(track, chosen)
        if stored is None:
            raise HTTPException(status_code=404,
                                detail=f"No version {chosen} for track {track}")
        features = [feature_lib.Feature(**f) for f in stored]
        source = "version"
    else:
        features = feature_lib.metadata(_newest_pairs_columns(), track, profile)
        source = "next_train"
    return {
        "track": track,
        "source": source,
        "version": chosen,
        "n_features": len(features),
        "groups": feature_lib.group_summary(features),
        "features": [f.as_dict() for f in features],
        "references": reference_lib.status(profile),
    }


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


class TrainRequest(BaseModel):
    run_id: str
    seed: Optional[int] = None
    note: Optional[str] = None


@router.post("/{track}/train", status_code=202)
def train_model(track: str, body: TrainRequest, user: str = Depends(current_user)):
    """Start a training job. Returns at once; poll or stream for progress."""
    track = _track(track)
    run_dir = _run_dir(body.run_id)
    if not (run_dir / "pairs.parquet").exists():
        raise HTTPException(status_code=400, detail="Run has no scored pairs yet")
    try:
        job = jobs.start(track, run_dir, _db_path(), seed=body.seed, note=body.note,
                         run_id=body.run_id, profile=get_profile())
    except jobs.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Started training the {track} model on run {body.run_id}",
        metadata={"track": track, "run_id": body.run_id, "job_id": job["job_id"]},
    )
    return {"job_id": job["job_id"], "track": track, "state": job["state"],
            "run_id": body.run_id}


@router.get("/{track}/train/{job_id}")
def train_job(track: str, job_id: str):
    track = _track(track)
    job = jobs.get(job_id)
    if job is None or job["track"] != track:
        raise HTTPException(status_code=404, detail="No such training job")
    return job


@router.get("/{track}/train/{job_id}/progress")
async def train_progress(track: str, job_id: str):
    """SSE stream of a training job, the same shape as a run's progress."""
    track = _track(track)
    job = jobs.get(job_id)
    if job is None or job["track"] != track:
        raise HTTPException(status_code=404, detail="No such training job")
    queue = jobs.subscribe(job_id)

    async def events():
        try:
            while True:
                try:
                    event = await queue.get()
                except Exception:  # noqa: BLE001
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("event") in ("complete", "error"):
                    break
        finally:
            jobs.unsubscribe(job_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


class ActivateRequest(BaseModel):
    version: Optional[int] = None


@router.post("/{track}/activate")
def activate(track: str, body: ActivateRequest | None = None,
             user: str = Depends(current_user)):
    """Make one version active. A model that is not graded may still be activated:
    it re-orders the review queue and decides nothing (`MODEL.md`)."""
    track = _track(track)
    version = (body.version if body and body.version is not None
               else store.latest_version(track))
    if version is None or not store.exists(track, version):
        raise HTTPException(status_code=400,
                            detail=f"No version {version} for track {track}")
    store.set_active(track, version)
    summary = store.summary(track, version)
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Activated the {track} model v{version}"
                    + ("" if summary.get("graded") else " (not graded — re-orders only)"),
        metadata={"track": track, "version": version, "graded": summary.get("graded")},
    )
    return {
        "ok": True, "track": track, "active_version": version,
        "graded": bool(summary.get("graded")),
        "can_auto_accept": store.can_auto_accept(track),
        "warnings": _warnings_for(summary),
    }


# ---------------------------------------------------------------------------
# The frozen test set
# ---------------------------------------------------------------------------


class DesignateRequest(BaseModel):
    n: int = 200


@router.get("/{track}/test-set")
def get_test_set(track: str):
    """What the frozen test set holds, and what is left to designate.

    Nothing is graded without one (`MODEL.md`), so this is what the panel reads
    to tell a reviewer why their model has no accept line.
    """
    from app.services import pair_labels

    track = _track(track)
    return pair_labels.test_set(_db_path(), track)


@router.post("/{track}/test-set/designate")
def designate_test_set(track: str, body: DesignateRequest | None = None,
                       user: str = Depends(current_user)):
    """Freeze up to `n` of this track's human labels as the test set."""
    from app.services import pair_labels

    track = _track(track)
    result = pair_labels.designate_test_set(
        _db_path(), n=(body.n if body else 200), track=track
    )
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=(f"Designated {result['designated']} {track} label(s) as the "
                     f"frozen test set ({result['left_for_training']} left to train on)"),
        metadata={"track": track, **result},
    )
    return result


@router.post("/{track}/deactivate")
def deactivate(track: str, user: str = Depends(current_user)):
    """Clear the active model, so runs bucket on the Splink score again."""
    track = _track(track)
    previous = store.clear_active(track)
    log_event(
        _db_path(), user=user or "unknown", kind="model",
        description=f"Deactivated the {track} model (was v{previous})",
        metadata={"track": track, "previous_active_version": previous},
    )
    return {"ok": True, "track": track, "active_version": None}

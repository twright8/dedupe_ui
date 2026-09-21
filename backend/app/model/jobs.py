# backend/app/model/jobs.py
"""Training in the background, reported the way a run reports its progress.

Training a donations model takes about ten seconds and a PSC one will take
minutes, so the API starts it and returns. The shape copies
`services/pipeline_runner`: a thread does the work, a callback writes progress
into the job record and pushes it to any SSE subscriber, and the last event
closes the stream.

One job per track at a time. Two trainings of the same track would race for the
next version number and for the same feature build, and there is no reason to
want two.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATES = ("queued", "running", "done", "failed")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_latest: dict[str, str] = {}          # track -> job id
_queues: dict[str, list] = {}         # job id -> asyncio queues


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return "mj_" + uuid.uuid4().hex[:12]


def reset() -> None:
    """Forget every job. Tests use it; the app never needs to."""
    with _lock:
        _jobs.clear()
        _latest.clear()
        _queues.clear()


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------


def subscribe(job_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    with _lock:
        _queues.setdefault(job_id, []).append(queue)
        job = _jobs.get(job_id)
    # A subscriber that arrives after the job finished still gets its ending, so
    # a page opened one second too late does not hang on an empty stream.
    if job and job["state"] in ("done", "failed"):
        queue.put_nowait(_final_event(job))
    return queue


def unsubscribe(job_id: str, queue: asyncio.Queue) -> None:
    with _lock:
        queues = _queues.get(job_id)
        if queues and queue in queues:
            queues.remove(queue)


def _emit(job_id: str, event: dict) -> None:
    with _lock:
        queues = list(_queues.get(job_id, []))
    for queue in queues:
        try:
            queue.put_nowait({**event, "timestamp": time.time()})
        except Exception:  # noqa: BLE001 — a dead subscriber must not stop training
            pass


def _final_event(job: dict) -> dict:
    if job["state"] == "done":
        return {"event": "complete", "version": job["version"], "percent": 100,
                "timestamp": time.time()}
    return {"event": "error", "message": job["error"], "timestamp": time.time()}


# ---------------------------------------------------------------------------
# The job record
# ---------------------------------------------------------------------------


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def latest(track: str) -> dict | None:
    with _lock:
        job_id = _latest.get(track)
        job = _jobs.get(job_id) if job_id else None
        return dict(job) if job else None


def running(track: str) -> bool:
    job = latest(track)
    return bool(job and job["state"] in ("queued", "running"))


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


# ---------------------------------------------------------------------------
# Starting one
# ---------------------------------------------------------------------------


class JobConflict(RuntimeError):
    """A job for this track is already running."""


def _log(db_path, who, kind, description, metadata) -> None:
    """Write an audit row, and never let a failed write lose a finished model."""
    if not db_path:
        return
    try:
        from app.services.audit_logger import log_event

        log_event(db_path, user=who or "unknown", kind=kind,
                  description=description, metadata=metadata)
    except Exception:
        logger.exception("Could not write the audit row: %s", description)


def start(track: str, run_dir, db_path: str | None, seed: int | None = None,
          note: str | None = None, run_id: str | None = None,
          background: bool = True, profile=None, who: str = "") -> dict:
    """Start a training job and return its record.

    *background* off runs it here and now, which is what tests want and what a
    command-line rebuild would want.
    """
    if running(track):
        raise JobConflict(f"A model is already training for the {track} track")

    job_id = new_job_id()
    job = {
        "job_id": job_id,
        "track": track,
        "run_id": run_id or str(run_dir).rstrip("/").rsplit("/", 1)[-1],
        "state": "queued",
        "step": None,
        "step_label": None,
        "percent": 0,
        "message": None,
        "started_at": _now(),
        "finished_at": None,
        "version": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _latest[track] = job_id

    def progress(step: str, label: str, percent: int, message: str | None) -> None:
        _update(job_id, state="running", step=step, step_label=label,
                percent=int(percent), message=message)
        _emit(job_id, {"event": "step", "step": step, "step_label": label,
                       "percent": int(percent), "message": message})

    def work() -> None:
        from app.model.train import train

        _update(job_id, state="running")
        try:
            summary = train(run_dir, db_path, track, seed=seed, note=note,
                            progress=progress, profile=profile)
        except Exception as exc:  # noqa: BLE001 — the message is the whole point
            logger.exception("Training the %s model failed", track)
            _update(job_id, state="failed", error=str(exc), finished_at=_now())
            _emit(job_id, {"event": "error", "message": str(exc)})
            _log(db_path, who, "model",
                 f"Training the {track} model failed: {exc}",
                 {"track": track, "job_id": job_id, "run_id": job["run_id"],
                  "error": str(exc)})
            return
        _update(job_id, state="done", version=summary["version"], percent=100,
                step="save", step_label="Saved", message=None, finished_at=_now())
        _emit(job_id, {"event": "complete", "version": summary["version"],
                       "percent": 100})
        # The start of training is logged by the router. The end is logged here,
        # because the router has already answered by the time it happens.
        _log(db_path, who, "model",
             f"Trained the {track} model as v{summary['version']}",
             {"track": track, "job_id": job_id, "run_id": job["run_id"],
              "version": summary["version"],
              "graded": summary.get("graded"),
              "n_human_labels": summary.get("n_human_labels"),
              "n_train_rows": summary.get("n_train_rows")})

    if background:
        threading.Thread(target=work, daemon=True, name=f"train-{track}").start()
    else:
        work()
    return get(job_id)

"""One heavy job at a time, across every process that shares a data directory.

``pipeline_runner`` already serialises runs inside one process: ``_run_queue``
and ``_active_run_id`` are module globals under a ``threading.Lock``. That is
blind to the other process. The donations instance and the PSC instance are two
uvicorn processes on one server with about 10 GB of RAM between them, so
nothing stopped them both entering a heavy stage at the same moment and the
machine paging itself to a halt. Decision D17 asks for one run at a time across
both.

The lock is an ``fcntl.flock`` on a file both processes can see:

* It is advisory and process-wide, so two *threads* in one process that each
  ``os.open`` the file still contend — the in-process queue and this lock agree
  rather than fight.
* The kernel drops it when the holding process dies, however it dies. A run
  killed with SIGKILL, an OOM, or a power cut cannot wedge the other instance,
  which a lock file holding a PID would.
* It is released explicitly on success and on failure, by the context manager.

Where the lock lives: ``RUN_LOCK_DIR`` if it is set, otherwise the parent of
``DATA_DIR``. The default is what makes the two instances meet — they are
deployed as siblings under one root, ``/srv/dedupe/donations/data`` and
``/srv/dedupe/psc/data``, so the parent is shared. Set ``RUN_LOCK_DIR``
explicitly whenever that is not true.

Waiting is only ever done on a background worker thread. An endpoint that does
its heavy work inline (recluster, re-bucket, apply-model) asks for the lock
without blocking and reports that the tool is busy instead of holding a request
thread open behind another instance's ten-minute run.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Environment variable naming the directory that holds the lock file. The
#: deploy kit sets this to one directory shared by both instances.
LOCK_DIR_ENV = "RUN_LOCK_DIR"

LOCK_FILENAME = "dedupe_run.lock"

#: What a waiting run shows on the progress stream and in ``runs.status``.
QUEUED_MESSAGE = "queued — another tool is running"

#: How often a waiting run re-tries. Short enough that the queue drains
#: promptly, long enough that a ten-minute wait is not a spin.
POLL_SECONDS = 1.0


class RunBusy(RuntimeError):
    """Another process holds the run lock and the caller would not wait."""

    def __init__(self, holder: dict | None = None):
        self.holder = holder or {}
        who = self.holder.get("label") or self.holder.get("pid")
        detail = f" (held by {who})" if who else ""
        super().__init__(f"{QUEUED_MESSAGE}{detail}")


def lock_dir(data_dir: str | os.PathLike | None = None) -> Path:
    """The directory holding the lock file.

    ``RUN_LOCK_DIR`` wins. Otherwise the parent of *data_dir*, or of the
    running app's ``DATA_DIR`` when no data directory is named.
    """
    override = os.environ.get(LOCK_DIR_ENV)
    if override:
        return Path(override)
    if data_dir is not None:
        return Path(data_dir).resolve().parent
    try:  # imported late: app.main reads DATA_DIR at import time
        from app.main import DATA_DIR

        return Path(DATA_DIR).resolve().parent
    except Exception:
        return Path(os.environ.get("DATA_DIR", "data")).resolve().parent


def lock_path(data_dir: str | os.PathLike | None = None) -> Path:
    return lock_dir(data_dir) / LOCK_FILENAME


def _read_holder(fd: int) -> dict | None:
    """Whatever the current holder wrote about itself. Never raises."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096).decode("utf-8", "replace").strip()
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _write_holder(fd: int, label: str) -> None:
    """Record who holds the lock, for the other instance to name. Never raises.

    Only ever called while this process holds the lock, so the truncate is safe.
    """
    try:
        payload = json.dumps({
            "pid": os.getpid(),
            "label": label,
            "since": datetime.now(timezone.utc).isoformat(),
        })
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    except Exception:
        logger.debug("Could not record run-lock holder", exc_info=True)


def _acquire(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def held_by(data_dir: str | os.PathLike | None = None) -> dict | None:
    """Who holds the lock right now, or ``None`` if it is free.

    A read-only peek for status endpoints. It takes the lock briefly to find
    out, so treat the answer as a snapshot rather than a guarantee.
    """
    path = lock_path(data_dir)
    if not path.is_file():
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        if _acquire(fd):
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        return _read_holder(fd) or {}
    finally:
        os.close(fd)


@contextlib.contextmanager
def run_lock(
    data_dir: str | os.PathLike | None = None,
    *,
    label: str = "",
    blocking: bool = True,
    on_wait=None,
    poll_seconds: float = POLL_SECONDS,
):
    """Hold the cross-process run lock for the body of the ``with``.

    Parameters
    ----------
    data_dir :
        The instance's data directory, used to site the lock when
        ``RUN_LOCK_DIR`` is unset.
    label :
        Free text naming this job, shown to whoever is waiting on it.
    blocking :
        ``True`` waits for the lock — only ever call this from a worker
        thread. ``False`` raises :class:`RunBusy` at once, which is what an
        endpoint doing its work inline wants.
    on_wait :
        Called once, with the current holder, if the lock is not free. This is
        where the caller reports ``QUEUED_MESSAGE``.
    """
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _acquire(fd):
            holder = _read_holder(fd)
            if not blocking:
                raise RunBusy(holder)
            if on_wait is not None:
                try:
                    on_wait(holder)
                except Exception:
                    logger.exception("run lock on_wait callback failed")
            # Poll rather than block in the kernel: a blocking flock cannot be
            # interrupted and would hold the worker thread past a shutdown.
            while not _acquire(fd):
                time.sleep(poll_seconds)
        _write_holder(fd, label)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            logger.debug("run lock already released", exc_info=True)
        os.close(fd)

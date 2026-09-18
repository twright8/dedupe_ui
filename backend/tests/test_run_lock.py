"""The cross-process run lock (D17: one run at a time across both instances).

The point of this lock is that it works between *processes*, so the tests that
matter here start a real second process rather than a second thread. A thread
would prove nothing: the bug being fixed is that two uvicorn processes sharing a
server had no idea about each other.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services import run_lock
from app.services.run_lock import RunBusy, lock_path, run_lock as take_lock


# A child that takes the lock, says so, and holds it until it is stopped.
HOLDER = """
import os, sys, time
sys.path.insert(0, {backend!r})
os.environ["RUN_LOCK_DIR"] = {lock_dir!r}
from app.services.run_lock import run_lock
with run_lock(label="the other tool"):
    print("HELD", flush=True)
    time.sleep(120)
"""


def _spawn_holder(lock_dir: Path) -> subprocess.Popen:
    """Start a second process holding the lock, and wait until it really has it."""
    script = HOLDER.format(backend=str(Path(__file__).resolve().parents[1]),
                           lock_dir=str(lock_dir))
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        line = child.stdout.readline()
        if line.strip() == "HELD":
            return child
        if child.poll() is not None:
            raise AssertionError(f"holder died: {child.stderr.read()}")
    child.kill()
    raise AssertionError("holder never acquired the lock")


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    directory = tmp_path / "shared"
    directory.mkdir()
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(directory))
    return directory


def test_the_lock_file_sits_beside_the_data_dir_by_default(tmp_path, monkeypatch):
    # The two instances are deployed as siblings under one root, so the parent
    # of DATA_DIR is the directory they share.
    monkeypatch.delenv(run_lock.LOCK_DIR_ENV, raising=False)
    data_dir = tmp_path / "psc" / "data"
    data_dir.mkdir(parents=True)
    assert lock_path(data_dir) == tmp_path / "psc" / run_lock.LOCK_FILENAME


def test_run_lock_dir_overrides_the_default(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(elsewhere))
    assert lock_path(tmp_path / "psc" / "data").parent == elsewhere


def test_a_second_process_cannot_take_a_held_lock(lock_dir):
    child = _spawn_holder(lock_dir)
    try:
        with pytest.raises(RunBusy):
            with take_lock(blocking=False, label="mine"):
                pass
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_waiting_side_is_told_who_is_running(lock_dir):
    child = _spawn_holder(lock_dir)
    try:
        holder = run_lock.held_by()
        assert holder is not None
        assert holder["label"] == "the other tool"
        assert holder["pid"] == child.pid
        # This text is what the queued run shows on the progress stream.
        assert "another tool is running" in run_lock.QUEUED_MESSAGE
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_lock_is_free_again_once_the_other_process_finishes(lock_dir):
    child = _spawn_holder(lock_dir)
    child.kill()          # a run that dies without unlocking, the hard case
    child.wait(timeout=10)
    # The kernel drops a flock when the process dies, so nothing has to clean up
    # after a crash, an OOM kill or a power cut.
    with take_lock(blocking=False, label="mine"):
        pass
    assert run_lock.held_by() is None


def test_a_waiting_run_reports_once_and_then_proceeds(lock_dir):
    child = _spawn_holder(lock_dir)
    seen = []

    def on_wait(holder):
        seen.append(holder)
        # Let the holder go, so the wait ends and we do not sit here for 120s.
        child.kill()

    with take_lock(label="mine", on_wait=on_wait, poll_seconds=0.05):
        pass

    assert len(seen) == 1
    assert seen[0]["label"] == "the other tool"
    child.wait(timeout=10)


def test_the_lock_is_released_when_the_body_raises(lock_dir):
    with pytest.raises(ValueError):
        with take_lock(label="mine"):
            raise ValueError("stage 3 blew up")
    # A failed run must not wedge the other instance.
    assert run_lock.held_by() is None
    with take_lock(blocking=False, label="next"):
        pass


def test_two_threads_in_one_process_also_contend(lock_dir):
    # flock is per open file description, so the in-process queue and this lock
    # agree rather than fight: a second acquisition in the same process is
    # refused exactly as another process would be.
    with take_lock(label="first"):
        with pytest.raises(RunBusy):
            with take_lock(blocking=False, label="second"):
                pass


def test_recluster_refuses_rather_than_waits_when_another_tool_runs(lock_dir):
    # Recluster does its work inline on the request thread, so it must fail fast
    # instead of holding the browser open behind a ten-minute run elsewhere.
    from app.services import pipeline_runner

    child = _spawn_holder(lock_dir)
    try:
        with pytest.raises(RunBusy):
            pipeline_runner.recluster_run("db.sqlite",
                                          str(lock_dir / "data" / "runs" / "r1"),
                                          "r1")
    finally:
        child.kill()
        child.wait(timeout=10)

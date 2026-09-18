"""One place that opens a bounded DuckDB connection.

Every heavy query in this pipeline runs in DuckDB, and DuckDB answers memory
pressure by spilling to disk. Left alone it will spill until the disk is gone:
a PSC scoring run whose training rule made 170 million pairs wrote **53 GB** of
``duckdb_temp_storage_*.tmp`` and filled a 1 TB laptop, and because the job was
killed rather than closed, the files stayed there and blocked everything that
came after.

So a connection opened here always carries three limits, not one:

``memory_limit``
    How much RAM DuckDB will use before it starts spilling. From
    ``SPLINK_MEMORY_LIMIT``.
``temp_directory``
    *Where* it spills — inside the run folder, so the spill belongs to a run and
    can be found and cleared, rather than scattered through the system temp.
``max_temp_directory_size``
    How much it may spill before giving up. From ``DUCKDB_MAX_TEMP``. This is
    the one that was missing. With it, a runaway query fails in seconds with
    DuckDB's own "temporary directory size limit exceeded" error, which names
    the problem. Without it, the query succeeds slowly and takes the machine
    down with it — including, on a shared server, the other instance.

A bounded failure is the point. Filling the disk is not a worse version of
failing, it is a different and much more expensive kind of failure: it takes out
services that had nothing to do with the query.
"""

from __future__ import annotations

import os
from pathlib import Path

#: RAM before DuckDB spills. Shared with Splink's backend.
MEMORY_LIMIT_ENV = "SPLINK_MEMORY_LIMIT"
DEFAULT_MEMORY_LIMIT = "6GB"

#: Disk DuckDB may spill before the query fails.
MAX_TEMP_ENV = "DUCKDB_MAX_TEMP"
DEFAULT_MAX_TEMP = "20GB"


def memory_limit() -> str:
    return os.environ.get(MEMORY_LIMIT_ENV, DEFAULT_MEMORY_LIMIT)


def max_temp() -> str:
    return os.environ.get(MAX_TEMP_ENV, DEFAULT_MAX_TEMP)


def configure(con, temp_dir: str | os.PathLike | None = None, *, threads: int | None = None):
    """Put this project's limits on an existing connection, and return it.

    Splink owns its own connection, so the settings have to be applied to a
    connection somebody else opened as well as to one of ours.

    *temp_dir* is created if it does not exist. Without it DuckDB spills to the
    system temp directory, where nothing associates the files with a run.
    """
    con.execute(f"SET memory_limit='{memory_limit()}'")
    if temp_dir is not None:
        path = Path(temp_dir)
        path.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{path}'")
    # Set last: DuckDB applies the cap to whatever temp_directory is current.
    con.execute(f"SET max_temp_directory_size='{max_temp()}'")
    if threads is not None:
        con.execute(f"SET threads={int(threads)}")
    return con


def connect(temp_dir: str | os.PathLike | None = None, *, threads: int | None = None):
    """An in-memory DuckDB connection with the limits already on it."""
    import duckdb

    return configure(duckdb.connect(), temp_dir, threads=threads)


def settings_of(con) -> dict:
    """The three limits as DuckDB currently reports them. For tests and reports."""
    keys = ("memory_limit", "temp_directory", "max_temp_directory_size")
    return {
        key: con.execute(f"select current_setting('{key}')").fetchone()[0]
        for key in keys
    }


def clear_spill(temp_dir: str | os.PathLike) -> int:
    """Delete spill files an earlier, killed run left behind. Returns bytes freed.

    DuckDB removes these on a clean shutdown but not after a SIGKILL. Callers
    run this before opening their own connection, when nothing can hold them.
    Only DuckDB's own spill files are matched, never a run's outputs.
    """
    directory = Path(temp_dir)
    freed = 0
    if not directory.is_dir():
        return freed
    for path in directory.glob("duckdb_temp_storage_*.tmp"):
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
        except OSError:
            pass
    return freed

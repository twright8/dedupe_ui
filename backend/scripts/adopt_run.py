#!/usr/bin/env python
"""Register a finished run folder with an instance, so the web tool can show it.

A run normally gets its database row from ``POST /api/runs``, which creates the
row and then runs the pipeline into the folder. There is no way to go the other
way round — and a full PSC run is hours of work on a machine that is not the
server, so the folder exists long before any instance knows about it.

This is that way round. Given a run folder that already holds the pipeline's
output, it writes the ``runs`` row a completed run would have had, and the
``config_versions`` row the run's own config snapshot describes. It copies
nothing: the folder must already be under the instance's ``runs`` directory.

    python scripts/adopt_run.py /srv/dedupe/data/runs/psc_full \
        --db /srv/dedupe/data/app.db --counts counts.json \
        --label "PSC full snapshot 2026-09-18"

**It never overwrites.** A run id the database already holds is refused, unless
the row is the one this script would have written, in which case there is
nothing to do and it says so. Re-running it is therefore safe.

``--counts`` is the union of what the five stages returned, in the pipeline's
own snake_case (``units_total``, ``pairs_accept``, ``entities_proposed`` ...).
Any key the file leaves out is derived from the folder where the folder can
answer it, so a run driven outside the web tool still shows its numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, query_db, write_db  # noqa: E402
from app.services.audit_logger import log_event  # noqa: E402

#: Without these a run folder is not a finished run and there is nothing to show.
REQUIRED = ("records.parquet", "units.parquet", "pairs.parquet",
            "clusters.parquet", "entities.parquet")

#: Read for their counts when the caller supplies none.
REPORTS = ("exact_eval.json", "entity_report.json", "blocking_report.json")


class AdoptError(RuntimeError):
    """Something about the folder or the database stops the run being adopted."""


# ---------------------------------------------------------------------------
# Reading the folder
# ---------------------------------------------------------------------------


def _rows(path: Path) -> int:
    import pyarrow.parquet as pq

    return int(pq.ParquetFile(path).metadata.num_rows)


def _bucket_counts(path: Path) -> dict:
    """``pairs.parquet`` by bucket, counted in DuckDB.

    ``bucket`` is the decision after the vetoes and the human overlay, which is
    what the pipeline's own ``pairs_accept`` means and what the review screen's
    chips show. ``score_bucket`` is the scorer's own answer and is the fallback
    for a file written before ``bucket`` existed.
    """
    from app import duckdb_conn

    con = duckdb_conn.connect(path.parent / "duckdb_tmp")
    try:
        names = set(con.execute(
            f"SELECT * FROM read_parquet('{path}') LIMIT 0").df().columns)
        column = "bucket" if "bucket" in names else "score_bucket"
        if column not in names:
            return {}
        rows = con.execute(
            f"SELECT {column}, count(*) FROM read_parquet('{path}') "
            f"GROUP BY {column}"
        ).fetchall()
        counts = {f"pairs_{str(bucket)}": int(n) for bucket, n in rows}
        counts["pairs_scored"] = sum(counts.values())
        if "veto_reason" in names:
            counts["pairs_vetoed"] = int(con.execute(
                f"SELECT count(*) FROM read_parquet('{path}') "
                "WHERE veto_reason IS NOT NULL"
            ).fetchone()[0])
        return counts
    finally:
        con.close()


def derive_counts(run_dir: Path) -> dict:
    """Every count the folder itself can answer, in the pipeline's snake_case."""
    counts: dict = {}
    for name in REPORTS:
        path = run_dir / name
        if path.is_file():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(report, dict):
                for key, value in report.items():
                    if isinstance(value, (int, float, str, dict)) and key not in counts:
                        counts[key] = value

    for name, key in (("records.parquet", "records_total"),
                      ("units.parquet", "units_total"),
                      ("clusters.parquet", "clusters_total"),
                      ("events.parquet", "event_rows")):
        path = run_dir / name
        if path.is_file():
            counts[key] = _rows(path)

    entities = run_dir / "entities.parquet"
    if entities.is_file():
        from app import duckdb_conn

        con = duckdb_conn.connect(run_dir / "duckdb_tmp")
        try:
            counts["entities_proposed"] = int(con.execute(
                f"SELECT count(DISTINCT entity_id) FROM read_parquet('{entities}')"
            ).fetchone()[0])
        finally:
            con.close()

    for name, prefix in (("units.parquet", "units"), ("records.parquet", "records")):
        path = run_dir / name
        if not path.is_file():
            continue
        from app import duckdb_conn

        con = duckdb_conn.connect(run_dir / "duckdb_tmp")
        try:
            if "track" in set(con.execute(
                    f"SELECT * FROM read_parquet('{path}') LIMIT 0").df().columns):
                for track, n in con.execute(
                        f"SELECT track, count(*) FROM read_parquet('{path}') "
                        "WHERE track IS NOT NULL GROUP BY track").fetchall():
                    counts[f"{prefix}_{track}"] = int(n)
        finally:
            con.close()

    pairs = run_dir / "pairs.parquet"
    if pairs.is_file():
        counts.update(_bucket_counts(pairs))
    return counts


def _timestamps(run_dir: Path) -> tuple[str, str, float]:
    """``(started_at, finished_at, duration_secs)`` from the folder's own mtimes."""
    times = [p.stat().st_mtime for p in run_dir.rglob("*") if p.is_file()]
    if not times:
        now = datetime.now(timezone.utc)
        return now.isoformat(), now.isoformat(), 0.0
    first, last = min(times), max(times)
    return (datetime.fromtimestamp(first, timezone.utc).isoformat(),
            datetime.fromtimestamp(last, timezone.utc).isoformat(),
            round(last - first, 1))


def _thresholds(run_dir: Path) -> tuple[float | None, float | None]:
    path = run_dir / "config" / "linkage_settings.json"
    if not path.is_file():
        return None, None
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    high = settings.get("match_probability_threshold_high")
    review = settings.get("match_probability_threshold_review")
    return (float(high) if high is not None else None,
            float(review) if review is not None else None)


# ---------------------------------------------------------------------------
# Writing the rows
# ---------------------------------------------------------------------------


def config_version_for(db_path: str, run_dir: Path, note: str) -> int | None:
    """The config version matching the run's snapshot, inserting one if needed.

    The screens read a run's rules through its ``config_version``. A snapshot
    that already has a version reuses it rather than making a second identical
    row, so adopting several runs of one configuration leaves one version.
    """
    config = run_dir / "config"
    ruleset = config / "ruleset.json"
    linkage = config / "linkage_settings.json"
    if not ruleset.is_file() and not linkage.is_file():
        return None
    ruleset_text = ruleset.read_text(encoding="utf-8") if ruleset.is_file() else None
    linkage_text = linkage.read_text(encoding="utf-8") if linkage.is_file() else None

    for row in query_db(db_path, "SELECT version, ruleset, linkage_settings "
                                 "FROM config_versions ORDER BY version"):
        if row["ruleset"] == ruleset_text and row["linkage_settings"] == linkage_text:
            return int(row["version"])

    write_db(
        db_path,
        "INSERT INTO config_versions (created_by, note, ruleset, linkage_settings) "
        "VALUES (?, ?, ?, ?)",
        ("adopt_run.py", note, ruleset_text, linkage_text),
    )
    rows = query_db(db_path, "SELECT max(version) AS version FROM config_versions")
    return int(rows[0]["version"]) if rows else None


def adopt(run_dir, db_path: str, run_id: str | None = None, label: str | None = None,
          counts: dict | None = None, input_filename: str | None = None,
          who: str | None = None) -> dict:
    """Write the ``runs`` row for a finished folder. Returns what it wrote.

    Raises ``AdoptError`` when the folder is not a finished run, and when the
    database already holds a different run under the same id.
    """
    run_dir = Path(run_dir).resolve()
    run_id = run_id or run_dir.name
    if not run_dir.is_dir():
        raise AdoptError(f"No such run folder: {run_dir}")
    missing = [name for name in REQUIRED if not (run_dir / name).is_file()]
    if missing:
        raise AdoptError(
            f"{run_dir} is not a finished run: it has no " + ", ".join(missing)
        )

    init_db(db_path)
    derived = derive_counts(run_dir)
    derived.update(counts or {})
    started, finished, duration = _timestamps(run_dir)
    high, review = _thresholds(run_dir)
    version = config_version_for(db_path, run_dir, f"Adopted with run {run_id}")

    row = {
        "id": run_id,
        "label": label,
        "status": "complete",
        "started_at": started,
        "finished_at": finished,
        "duration_secs": duration,
        "triggered_by": "adopt_run.py",
        "config_version": version,
        "input_filename": input_filename,
        "counts_json": json.dumps(derived, sort_keys=True, default=str),
        "threshold_high": high,
        "threshold_review": review,
    }

    existing = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if existing:
        same = all(
            _comparable(dict(existing[0]).get(key)) == _comparable(value)
            for key, value in row.items()
            if key not in ("started_at", "finished_at", "duration_secs")
        )
        if same:
            return {**row, "adopted": False,
                    "message": f"Run '{run_id}' is already registered."}
        raise AdoptError(
            f"Run '{run_id}' already exists in {db_path} and is not the row this "
            "would write. Refusing to overwrite it. Adopt the folder under "
            "another id with --run-id, or remove the existing run first."
        )

    write_db(
        db_path,
        """INSERT INTO runs (id, label, status, started_at, finished_at,
                             duration_secs, triggered_by, config_version,
                             input_filename, counts_json, threshold_high,
                             threshold_review)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        tuple(row[key] for key in (
            "id", "label", "status", "started_at", "finished_at", "duration_secs",
            "triggered_by", "config_version", "input_filename", "counts_json",
            "threshold_high", "threshold_review")),
    )
    # A PSC run built offline and adopted used to leave no trace of who or
    # when (docs/TERMINOLOGY_AUDIT.md, gap 9). It does now.
    log_event(
        db_path, user=who or "adopt_run.py", kind="run",
        description=f"Adopted run {run_id} from {run_dir}",
        metadata={"run_id": run_id, "run_dir": str(run_dir),
                  "config_version": version, "label": label,
                  "input_filename": input_filename,
                  "threshold_high": high, "threshold_review": review},
    )
    return {**row, "adopted": True,
            "message": f"Run '{run_id}' registered from {run_dir}."}


def _comparable(value):
    """A stored column and a fresh one, comparable across SQLite's types."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _shell_user() -> str:
    """Who is running this, so the audit log names a person and not a script."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or "adopt_run.py"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", help="the finished run folder, under <data>/runs/")
    parser.add_argument("--db", help="the instance's SQLite file "
                                     "(default: <data>/app.db beside the folder)")
    parser.add_argument("--run-id", help="the id to register it under "
                                         "(default: the folder name)")
    parser.add_argument("--label", help="what the runs list should call it")
    parser.add_argument("--counts", help="a JSON file of the stages' counts")
    parser.add_argument("--input-filename", help="the file the run was made from")
    parser.add_argument("--who", help="who is adopting it, for the audit log "
                                      "(default: the shell user)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    db_path = args.db or str(run_dir.parent.parent / "app.db")
    counts = None
    if args.counts:
        counts = json.loads(Path(args.counts).read_text(encoding="utf-8"))
        if not all(isinstance(v, (int, float, str, dict, type(None)))
                   for v in counts.values()):
            raise SystemExit("--counts must be a flat JSON object of counts")

    try:
        result = adopt(run_dir, db_path, run_id=args.run_id, label=args.label,
                       counts=counts, input_filename=args.input_filename,
                       who=args.who or _shell_user())
    except AdoptError as exc:
        print(f"adopt_run: {exc}", file=sys.stderr)
        return 2
    print(result["message"])
    print(f"  database      {db_path}")
    print(f"  config version {result['config_version']}")
    print(f"  counts        {len(json.loads(result['counts_json']))} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

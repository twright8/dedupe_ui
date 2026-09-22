# backend/app/services/pipeline_runner.py
"""Pipeline execution service — run queue, background execution, SSE progress."""

import asyncio
import collections
import contextlib
import functools
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import query_db, write_db
from app.profiles.base import LoadOptions
from app.services.audit_logger import log_event
from app.rules import linkage
from app.services.config_manager import get_version
from app.services import bucketing_history, run_lock, run_manifest

logger = logging.getLogger(__name__)



def _build_error_detail(exc: Exception) -> str | None:
    """Return a JSON string of structured failure detail, or None.

    Structured detail lets the UI offer a targeted fix — "add these rows to the
    lookup" — instead of just showing an error string. Each kind carries only
    what its fix needs.
    """
    from app.pipeline.dedupe.stage_3_score import BlockingBudgetError
    from app.rules.engine import UnmappedLookupValuesError

    if isinstance(exc, UnmappedLookupValuesError):
        return json.dumps({
            "kind": "unmapped_lookup_values",
            "table": exc.table,
            "values": exc.values,
        })
    if isinstance(exc, BlockingBudgetError):
        return json.dumps(exc.detail())
    return None


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_run_queue: collections.deque = collections.deque()  # pending run kwargs dicts
_active_run_id: str | None = None
_event_queues: dict[str, list[asyncio.Queue]] = {}  # run_id -> subscriber queues
_run_lock = threading.Lock()

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def subscribe_progress(run_id: str) -> asyncio.Queue:
    """Return a new asyncio.Queue that will receive SSE events for *run_id*."""
    q: asyncio.Queue = asyncio.Queue()
    with _run_lock:
        if run_id not in _event_queues:
            _event_queues[run_id] = []
        _event_queues[run_id].append(q)
    return q


def unsubscribe_progress(run_id: str, queue: asyncio.Queue) -> None:
    """Remove *queue* from the subscriber list for *run_id*."""
    with _run_lock:
        if run_id in _event_queues:
            try:
                _event_queues[run_id].remove(queue)
            except ValueError:
                pass


def _emit_event(run_id: str, event: dict) -> None:
    """Push *event* to every subscriber queue for *run_id* (non-blocking)."""
    with _run_lock:
        queues = list(_event_queues.get(run_id, []))
    for q in queues:
        try:
            q.put_nowait(event)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config writer helpers
# ---------------------------------------------------------------------------

# Second Splink blocking rule: compare same-name records even when their
# jurisdictions differ (or are the UNKNOWN sentinel). Names are the primary
# linkage key in this domain. Guarded against empty name_core. The upload-time
# `cross_jurisdiction_name_matching` toggle strips this rule when disabled.
NAME_CORE_BLOCKING_RULE = "l.name_core = r.name_core AND l.name_core <> ''"


def _write_config_files(
    config_row: dict,
    config_dir: Path,
    threshold_high: float | None = None,
    threshold_review: float | None = None,
    cross_jurisdiction_name_matching: bool = True,
    threshold_high_by_track: dict | None = None,
) -> None:
    """Snapshot a config version into the run's config folder.

    A run must be reproducible from its own folder, so the rules it used are
    copied beside its outputs rather than read back out of the database, which
    the next save moves on.
    """
    config_dir.mkdir(parents=True, exist_ok=True)

    # ruleset.json — already parsed by config_manager.get_version.
    ruleset = config_row.get("ruleset")
    if isinstance(ruleset, str):
        ruleset = json.loads(ruleset)
    (config_dir / "ruleset.json").write_text(
        json.dumps(ruleset or {}, indent=2), encoding="utf-8"
    )

    # linkage_settings.json
    linkage_settings = json.loads(config_row["linkage_settings"] or "{}")
    if threshold_high is not None:
        linkage_settings["match_probability_threshold_high"] = threshold_high
    if threshold_review is not None:
        linkage_settings["match_probability_threshold_review"] = threshold_review
    # A per-track accept line the caller chose replaces the config version's.
    # Passing none leaves the version's own per-track lines alone, so moving
    # only the shared line does not quietly wipe them (docs/LINKAGE.md).
    if threshold_high_by_track is not None:
        linkage_settings[linkage.HIGH_BY_TRACK_KEY] = {
            str(track): float(line) for track, line in threshold_high_by_track.items()
        }
    linkage_settings.setdefault("random_seed", 42)

    # The two-dataset tool this app was copied from kept one flat list of blocking
    # rules and an upload-time toggle over it. Settings in the dedupe shape put the
    # rules under `tracks`, so that toggle has nothing to say about them and is left
    # alone; only a legacy document still gets the old treatment.
    if "tracks" not in linkage_settings:
        blocking_rules = list(linkage_settings.get("blocking_rules", []))
        if cross_jurisdiction_name_matching:
            if NAME_CORE_BLOCKING_RULE not in blocking_rules:
                blocking_rules.append(NAME_CORE_BLOCKING_RULE)
        else:
            blocking_rules = [r for r in blocking_rules if "name_core" not in r]
        linkage_settings["blocking_rules"] = blocking_rules

    (config_dir / "linkage_settings.json").write_text(
        json.dumps(linkage_settings, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Count extraction
# ---------------------------------------------------------------------------


def _run_settings(config_dir: str) -> dict:
    """The linkage settings a run froze into its own config folder."""
    try:
        return json.loads(
            (Path(config_dir) / "linkage_settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_csv_row_count(path: Path) -> int:
    """Return the number of data rows in a CSV file (excluding header)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return max(0, sum(1 for _ in f) - 1)
    except FileNotFoundError:
        return 0


def _dropped_below_review_count(run_dir: Path) -> int:
    """Count scored pairs retained in the parquet but below the review floor.

    Stage 2 now predicts down to the candidate floor, so linkage_scored.parquet keeps
    weak pairs that Stage 3 excludes from review. Measured on the same decision score
    Stage 3 buckets on (gbt_score when enabled, else raw Splink match_probability).
    """
    scored_path = run_dir / "linkage_scored.parquet"
    settings_path = run_dir / "config" / "linkage_settings.json"
    if not scored_path.exists() or not settings_path.is_file():
        return 0
    try:
        import pandas as pd

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        review = float(settings.get("match_probability_threshold_review", 0.0))
        use_gbt = bool(settings.get("gbt_score_column"))
        df = pd.read_parquet(scored_path)
        col = "gbt_score" if (use_gbt and "gbt_score" in df.columns) else "match_probability"
        scores = pd.to_numeric(df[col], errors="coerce")
        return int((scores < review).sum())
    except Exception:
        logger.debug("Could not compute dropped-below-review count")
        return 0


def _collect_counts(run_dir: Path) -> dict:
    """Read output CSVs and produce a summary counts dict."""
    ambiguous_csv = run_dir / "matches_ambiguous.csv"
    ambiguous_count = _read_csv_row_count(ambiguous_csv) if ambiguous_csv.exists() else 0
    roe_parquet = run_dir / "roe_preprocessed.parquet"
    roe_count = 0
    if roe_parquet.exists():
        try:
            import pandas as pd
            # ROE is one row per name variant (current + former); count distinct
            # companies, not rows, so former-name variants don't inflate the total.
            roe_count = int(pd.read_parquet(
                roe_parquet, columns=["roe_company_number"]
            )["roe_company_number"].nunique())
        except Exception:
            pass

    # Count matches from merged_dataset, which is now one row per (title, proprietor):
    # a title with 2+ proprietors has 2+ rows, each independently matched. Title-grain
    # numbers stay per TITLE (a title counts as matched if ANY of its proprietor rows
    # matched); proprietor-grain numbers count the rows themselves. Exact vs fuzzy is
    # split at the title level (a title counts as exact if any proprietor matched exact;
    # fuzzy = matched - exact in the UI), with former-name matches called out.
    matched_titles = 0
    matched_titles_exact = 0
    matched_titles_former = 0
    total_titles = 0
    total_proprietors = 0
    matched_proprietors = 0
    distinct_entities = 0
    distinct_entities_identified = 0
    distinct_entities_unidentified = 0
    merged_csv = run_dir / "merged_dataset.csv"
    if merged_csv.exists():
        try:
            import pandas as pd
            available = set(pd.read_csv(merged_csv, nrows=0, encoding="utf-8-sig").columns)
            wanted = [c for c in ("title_number", "roe_company_number", "match_method",
                                  "matched_name_type", "entity_uid", "entity_identified") if c in available]
            merged_df = pd.read_csv(merged_csv, usecols=wanted,
                                    dtype=str, encoding="utf-8-sig").fillna("")
            has_match = merged_df["roe_company_number"].str.strip() != ""
            is_exact = merged_df["match_method"].str.strip() == "exact"
            is_former = (
                merged_df["matched_name_type"].str.strip() == "former"
                if "matched_name_type" in merged_df.columns
                else pd.Series(False, index=merged_df.index)
            )
            total_proprietors = int(len(merged_df))
            matched_proprietors = int(has_match.sum())
            # Distinct entities, split by whether the identity is a real OE number or a
            # name-based stand-in — a combined figure would hide that difference.
            if "entity_uid" in merged_df.columns:
                uid = merged_df["entity_uid"]
                distinct_entities = int(uid[uid.str.strip() != ""].nunique())
                identified = merged_df.get("entity_identified")
                if identified is not None:
                    is_id = identified.astype(str).str.lower().isin(("true", "1"))
                    distinct_entities_identified = int(uid[is_id].nunique())
                    distinct_entities_unidentified = int(uid[~is_id & (uid.str.strip() != "")].nunique())
            if "title_number" in merged_df.columns:
                titles = merged_df["title_number"]
                total_titles = int(titles.nunique())
                matched_titles = int(titles[has_match].nunique())
                matched_titles_exact = int(titles[has_match & is_exact].nunique())
                matched_titles_former = int(titles[has_match & is_former].nunique())
            else:
                # Legacy runs / fixtures with no title_number: one row per title, so the
                # row grain IS the title grain.
                total_titles = total_proprietors
                matched_titles = matched_proprietors
                matched_titles_exact = int((has_match & is_exact).sum())
                matched_titles_former = int((has_match & is_former).sum())
        except Exception:
            logger.debug("Could not read merged_dataset.csv columns for match counts")

    return {
        "matches_exact": _read_csv_row_count(run_dir / "matches_exact.csv"),
        "matches_high_confidence": _read_csv_row_count(run_dir / "matches_high_confidence.csv"),
        "matches_for_review": _read_csv_row_count(run_dir / "matches_for_review.csv"),
        "matches_ambiguous": ambiguous_count,
        "unmatched_ocod": _read_csv_row_count(run_dir / "unmatched_ocod.csv"),
        "unmatched_roe": _read_csv_row_count(run_dir / "unmatched_roe.csv"),
        "merged_dataset": _read_csv_row_count(run_dir / "merged_dataset.csv"),
        "matched_titles": matched_titles,
        "matched_titles_exact": matched_titles_exact,
        "matched_titles_former": matched_titles_former,
        # Title/proprietor grain both reported: title-grain keeps the land-title panel
        # honest; proprietor-grain reflects the new one-row-per-proprietor export.
        "total_titles": total_titles,
        "total_proprietors": total_proprietors,
        "matched_proprietors": matched_proprietors,
        "dropped_below_review": _dropped_below_review_count(run_dir),
        "roe_preprocessed": roe_count,
        # ROE-side mirror: how many registered entities we could tie to a title. The
        # unmatched remainder is the control on the unmatched-OCOD figure.
        "merged_roe": _read_csv_row_count(run_dir / "merged_roe.csv"),
        "distinct_entities": distinct_entities,
        "distinct_entities_identified": distinct_entities_identified,
        "distinct_entities_unidentified": distinct_entities_unidentified,
    }




# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------

# The dedupe pipeline replaces the old two-dataset linkage stages one slice at a
# time. Six stages run today; export is a request, not a stage.
STAGE_NAMES = {
    0: "load",
    1: "clean",
    2: "exact",
    3: "score",
    4: "cluster",
    5: "entities",
}


@contextlib.contextmanager
def _capture_pipeline_output(run_dir: Path):
    """Capture verbose stage output so broken terminals cannot fail a run."""
    log_path = run_dir / "pipeline.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8", buffering=1)
    except Exception:
        logger.exception("Could not open pipeline output log at %s", log_path)
        yield
        return

    with log_file:
        log_file.write(f"\n--- Pipeline output started {datetime.now(timezone.utc).isoformat()} ---\n")
        with contextlib.redirect_stdout(log_file), contextlib.redirect_stderr(log_file):
            yield


def start_run(
    db_path: str,
    data_dir: str,
    run_id: str,
    input_path: str,
    config_version: int,
    threshold_high: float,
    threshold_review: float,
    render_diagnostics: bool = True,
    quick_mode: bool = False,
    threshold_high_by_track: dict | None = None,
) -> str:
    """Prepare a run directory and launch the pipeline in a background thread.

    Returns the run_id.
    """
    global _active_run_id

    data_dir_p = Path(data_dir)
    run_dir = data_dir_p / "runs" / run_id
    config_dir = run_dir / "config"
    run_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    # Load config from DB and write files to the run's config dir
    config_row = get_version(db_path, config_version)
    if config_row is None:
        raise ValueError(f"Config version {config_version} not found")
    _write_config_files(config_row, config_dir, threshold_high, threshold_review,
                        threshold_high_by_track=threshold_high_by_track)

    # Update run status
    now = datetime.now(timezone.utc).isoformat()
    write_db(
        db_path,
        "UPDATE runs SET status = ?, started_at = ?, current_stage = ? WHERE id = ?",
        ("running", now, "0", run_id),
    )

    # What produced this run, written down before it runs: the input file and
    # its hash, the code version, the config version, the lines in force and
    # the library versions (docs/PROVENANCE.md). It never fails a run.
    _write_start_manifest(db_path, run_id, run_dir, config_row, input_path,
                          threshold_high, threshold_review, now,
                          config_dir=config_dir)

    with _run_lock:
        _active_run_id = run_id

    # Launch background thread
    t = threading.Thread(
        target=_run_pipeline,
        kwargs={
            "db_path": db_path,
            "data_dir": data_dir,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "config_dir": str(config_dir),
            "input_path": input_path,
            "threshold_high": threshold_high,
            "threshold_review": threshold_review,
            "render_diagnostics": render_diagnostics,
            "quick_mode": quick_mode,
        },
        daemon=True,
    )
    t.start()

    return run_id


def _write_start_manifest(db_path, run_id, run_dir, config_row, input_path,
                          threshold_high, threshold_review, started_at,
                          config_dir=None):
    """Build ``run_manifest.json`` and copy its headline fields onto the run row."""
    try:
        row = query_db(db_path, "SELECT * FROM runs WHERE id = ?", (run_id,))
        run = dict(row[0]) if row else {}
        settings = json.loads((config_row or {}).get("linkage_settings") or "{}")
        uploaded = query_db(
            db_path,
            "SELECT completed_at FROM upload_sessions WHERE status = 'complete' "
            "AND (filename = ? OR stored_filename = ?) "
            "ORDER BY completed_at DESC LIMIT 1",
            (run.get("input_filename") or "", Path(input_path).name),
        )
        manifest = run_manifest.build(
            run_id=run_id,
            run_dir=run_dir,
            config_version=run.get("config_version"),
            input_path=input_path,
            thresholds={
                "accept_line": threshold_high,
                # Read back off the snapshot the run will actually score with,
                # not off the config row, so the manifest cannot disagree with
                # the lines stage 3 uses.
                "accept_line_by_track": linkage.high_by_track(
                    _run_settings(config_dir) if config_dir else settings),
                "review_line": threshold_review,
                "lowest_score_kept": settings.get(
                    "match_probability_threshold_candidate"),
            },
            triggered_by=run.get("triggered_by") or "",
            uploaded_at=(uploaded[0]["completed_at"] if uploaded else None),
            started_at=started_at,
        )
        run_manifest.write(run_dir, manifest)
        write_db(
            db_path,
            "UPDATE runs SET code_version = ?, input_sha256 = ?, input_bytes = ?, "
            "input_uploaded_at = ? WHERE id = ?",
            (manifest["code_version"], manifest["input"].get("sha256"),
             manifest["input"].get("size_bytes"), manifest["input"].get("uploaded_at"),
             run_id),
        )
    except Exception:
        logger.exception("Could not write the run manifest for %s", run_id)


def _run_pipeline(
    db_path: str,
    data_dir: str,
    run_id: str,
    run_dir: str,
    config_dir: str,
    input_path: str,
    threshold_high: float,
    threshold_review: float,
    render_diagnostics: bool = True,
    quick_mode: bool = False,
) -> None:
    """Execute the dedupe stages sequentially in a background thread.

    Four stages so far: load the records, assign tracks and clean them, group
    them on the ruleset's match keys, then score the units the keys left with
    Splink. Clustering and export join them later.
    """
    events_path = Path(run_dir) / "events.jsonl"

    def progress_callback(event_type: str, detail: dict) -> None:
        event = {"event": event_type, "timestamp": time.time(), **detail}
        # Update current_stage in DB when a new stage starts
        if event_type == "stage_start" and "stage" in detail:
            write_db(
                db_path,
                "UPDATE runs SET current_stage = ? WHERE id = ?",
                (str(detail["stage"]), run_id),
            )
        # Append to events.jsonl
        try:
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            logger.exception("Failed to write event to %s", events_path)
        # Emit to SSE subscribers
        _emit_event(run_id, event)

    def _on_wait(holder: dict | None) -> None:
        """Another instance is running. Say so on the stream and in the DB (D17)."""
        who = (holder or {}).get("label") or ""
        message = run_lock.QUEUED_MESSAGE + (f" ({who})" if who else "")
        write_db(
            db_path,
            "UPDATE runs SET status = ?, current_stage = ? WHERE id = ?",
            ("queued", None, run_id),
        )
        progress_callback("queued", {"message": message, "holder": holder or {}})

    try:
        # One heavy run at a time across both instances, not just this process.
        # Waiting happens here, on the worker thread start_run created, never on
        # the request thread that asked for the run.
        with run_lock.run_lock(data_dir, label=f"run {run_id}", on_wait=_on_wait):
            write_db(
                db_path,
                "UPDATE runs SET status = ? WHERE id = ?",
                ("running", run_id),
            )
            _run_stages(
                db_path=db_path,
                run_id=run_id,
                run_dir=run_dir,
                config_dir=config_dir,
                input_path=input_path,
                threshold_high=threshold_high,
                threshold_review=threshold_review,
                render_diagnostics=render_diagnostics,
                quick_mode=quick_mode,
                progress_callback=progress_callback,
            )

    except Exception as e:
        logger.exception("Pipeline run %s failed", run_id)
        error_detail_json = _build_error_detail(e)
        write_db(
            db_path,
            "UPDATE runs SET status = ?, error_message = ?, error_detail_json = ? WHERE id = ?",
            ("failed", str(e), error_detail_json, run_id),
        )
        _emit_event(run_id, {"event": "failed", "error": str(e), "timestamp": time.time()})

        log_event(
            db_path,
            user="system",
            kind="run",
            description=f"Pipeline run {run_id} failed: {e}",
            metadata={"run_id": run_id, "error": str(e)},
        )

    finally:
        _on_run_finished(db_path, data_dir)


def _run_stages(
    db_path: str,
    run_id: str,
    run_dir: str,
    config_dir: str,
    input_path: str,
    threshold_high: float,
    threshold_review: float,
    render_diagnostics: bool,
    quick_mode: bool,
    progress_callback,
) -> None:
    """The stages themselves, with the cross-process run lock already held."""
    with _capture_pipeline_output(Path(run_dir)):
        from app.pipeline.dedupe.stage_0_load import run_stage_0_load
        from app.pipeline.dedupe.stage_1_clean import run_stage_1_clean
        from app.pipeline.dedupe.stage_2_exact import run_stage_2_exact
        from app.pipeline.dedupe.stage_3_score import run_stage_3_score
        from app.pipeline.dedupe.stage_4_cluster import run_stage_4_cluster
        from app.pipeline.dedupe.stage_5_entities import run_stage_5_entities

        # Count active labels before running so we know what was available.
        # Nothing consumes them yet — matching arrives in a later slice — but
        # the figure belongs with the run it was measured against.
        label_rows = query_db(
            db_path,
            "SELECT COUNT(*) AS n FROM labels WHERE active = 1",
        )
        labels_in_library = label_rows[0]["n"] if label_rows else 0

        counts = run_stage_0_load(
            run_dir=run_dir,
            input_path=input_path,
            progress_callback=progress_callback,
            options=LoadOptions(quick_mode=quick_mode),
        )
        # Per-track counts are only known after the ruleset has been applied,
        # so they replace whatever stage 0 reported.
        counts.update(run_stage_1_clean(
            run_dir=run_dir,
            config_dir=config_dir,
            progress_callback=progress_callback,
        ))
        from app.services.pair_labels import decisions_by_scope, labels_frame

        labels = labels_frame(db_path)
        counts.update(run_stage_2_exact(
            run_dir=run_dir,
            config_dir=config_dir,
            labels=labels,
            decisions=decisions_by_scope(db_path),
            progress_callback=progress_callback,
        ))
        # A label is a statement about two records, so it outlives the run
        # that made it: stage 3 maps each one onto whichever units hold
        # those two records now.
        counts.update(run_stage_3_score(
            run_dir=run_dir,
            config_dir=config_dir,
            threshold_high=threshold_high,
            threshold_review=threshold_review,
            render_diagnostics=render_diagnostics,
            labels=labels,
            progress_callback=progress_callback,
        ))
        # Which lines put these pairs in their buckets. One entry per change,
        # not three numbers on every one of a hundred million rows.
        bucketing_history.append(
            run_dir, "scored",
            accept_line=threshold_high, review_line=threshold_review,
            accept_line_by_track=linkage.high_by_track(_run_settings(config_dir)),
            lowest_score_kept=_run_settings(config_dir).get(
                "match_probability_threshold_candidate"),
            scorer="splink", counts=counts, who="system",
        )
        counts.update(run_stage_4_cluster(
            run_dir=run_dir,
            config_dir=config_dir,
            labels=labels,
            decisions=decisions_by_scope(db_path),
            progress_callback=progress_callback,
        ))
        counts.update(run_stage_5_entities(
            run_dir=run_dir,
            db_path=db_path,
            progress_callback=progress_callback,
        ))
        counts["labels_in_library"] = labels_in_library

        now = datetime.now(timezone.utc).isoformat()
        started_rows = query_db(
            db_path,
            "SELECT started_at FROM runs WHERE id = ?",
            (run_id,),
        )
        duration = None
        if started_rows and started_rows[0]["started_at"]:
            try:
                started = datetime.fromisoformat(started_rows[0]["started_at"])
                duration = (datetime.now(timezone.utc) - started).total_seconds()
            except Exception:
                pass

        write_db(
            db_path,
            """UPDATE runs
               SET status = ?, finished_at = ?, duration_secs = ?, counts_json = ?,
                   threshold_high = ?, threshold_review = ?, current_stage = NULL
               WHERE id = ?""",
            ("complete", now, duration, json.dumps(counts), threshold_high,
             threshold_review, run_id),
        )

        try:
            run_manifest.finish(run_dir, counts=counts,
                                row_count=counts.get("records_total"))
            write_db(db_path, "UPDATE runs SET input_rows = ? WHERE id = ?",
                     (counts.get("records_total"), run_id))
        except Exception:
            logger.exception("Could not close the run manifest for %s", run_id)

        progress_callback("complete", {"counts": counts})

        log_event(
            db_path,
            user="system",
            kind="run",
            description=f"Pipeline run {run_id} completed",
            metadata={"run_id": run_id, "counts": counts},
        )



def _needs_run_lock(fn):
    """Guard heavy work that an endpoint does inline on its request thread.

    Recluster, re-bucket and apply-model are as heavy as a stage, so D17's "one
    at a time" covers them too. They do not get to *wait*, though: they run on
    the request thread, and holding that open behind the other instance's
    ten-minute run would just move the stall to the browser. They ask once and
    raise :class:`~app.services.run_lock.RunBusy` if the tool is busy.

    Every wrapped function takes ``run_dir`` second, and a run directory is
    ``<data_dir>/runs/<run_id>``, so the data directory is two levels up.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        run_dir = kwargs.get("run_dir") if "run_dir" in kwargs else (
            args[1] if len(args) > 1 else None
        )
        data_dir = Path(run_dir).parent.parent if run_dir else None
        with run_lock.run_lock(data_dir, label=fn.__name__, blocking=False):
            return fn(*args, **kwargs)

    return wrapper



def rebucket_pairs(
    db_path: str,
    run_dir: str,
    config_dir: str,
    run_id: str,
    threshold_high: float | None = None,
    threshold_review: float | None = None,
    who: str = "",
    threshold_high_by_track: dict | None = None,
) -> dict:
    """Move a scored run's bucket lines without re-running Splink.

    Stage 3 keeps every pair down to the lowest score kept, so a new accept or
    review line is a re-read of ``pairs.parquet``, not a rerun. The run's stored counts and thresholds move
    with it, so the screen never shows old numbers over new data.
    """
    from app.pipeline.dedupe.stage_3_score import rebucket
    from app.services.pair_labels import labels_frame

    settings_path = Path(config_dir) / "linkage_settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    rows = query_db(
        db_path,
        "SELECT threshold_high, threshold_review, counts_json FROM runs WHERE id = ?",
        (run_id,),
    )
    current = dict(rows[0]) if rows else {}

    high = threshold_high if threshold_high is not None else current.get("threshold_high")
    review = threshold_review if threshold_review is not None else current.get("threshold_review")
    if high is None:
        high = settings.get("match_probability_threshold_high", 0.92)
    if review is None:
        review = settings.get("match_probability_threshold_review", 0.50)
    high, review = float(high), float(review)
    if review > high:
        raise ValueError("The review threshold must be at or below the accept threshold")

    # A per-track accept line the caller sent replaces what the run had.
    # Sending none leaves the run's own per-track lines where they were, so
    # moving the review line alone does not move any track's accept line.
    if threshold_high_by_track is not None:
        settings[linkage.HIGH_BY_TRACK_KEY] = {
            str(track): float(line) for track, line in threshold_high_by_track.items()
        }
    by_track = linkage.high_by_track(settings)
    for track, line in by_track.items():
        if review > line:
            raise ValueError(
                "The review threshold must be at or below every track's accept line"
            )

    settings["match_probability_threshold_high"] = high
    settings["match_probability_threshold_review"] = review
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # Merged, never replaced: moving a threshold redoes the scoring counts and
    # nothing else, so every other stage's numbers have to survive it
    # (services/run_counts). The thresholds move in the same statement.
    from app.services import run_counts

    counts = run_counts.merge(
        db_path, run_id,
        rebucket(run_dir, high, review, labels_frame(db_path),
                 threshold_high_by_track=by_track),
        extra_sql="threshold_high = ?, threshold_review = ?, ",
        extra_params=(high, review),
    )
    state = _model_state(run_dir)
    bucketing_history.append(
        run_dir, "re-bucketed",
        accept_line=high, review_line=review, accept_line_by_track=by_track,
        lowest_score_kept=settings.get("match_probability_threshold_candidate"),
        scorer="model" if state else "splink",
        model_version=state or None, counts=counts, who=who or "",
    )
    return counts


def _model_state(run_dir: str) -> dict:
    """``{track: version}`` for the models applied to this run, or ``{}``."""
    try:
        state = json.loads(
            (Path(run_dir) / "model_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tracks = state.get("tracks") or {}
    return {track: detail.get("version") for track, detail in tracks.items()
            if isinstance(detail, dict) and detail.get("version") is not None}


def _on_run_finished(db_path: str, data_dir: str) -> None:
    """Clear the active run and start the next queued run if any."""
    global _active_run_id

    with _run_lock:
        _active_run_id = None
        if _run_queue:
            next_kwargs = _run_queue.popleft()
        else:
            next_kwargs = None

    if next_kwargs is not None:
        start_run(**next_kwargs)


# ---------------------------------------------------------------------------
# Public queue interface
# ---------------------------------------------------------------------------


def enqueue_run(
    db_path: str,
    data_dir: str,
    run_id: str,
    input_path: str,
    config_version: int,
    threshold_high: float,
    threshold_review: float,
    render_diagnostics: bool = True,
    quick_mode: bool = False,
    threshold_high_by_track: dict | None = None,
) -> None:
    """If no run is active, start immediately. Otherwise queue for later."""
    kwargs = {
        "db_path": db_path,
        "data_dir": data_dir,
        "run_id": run_id,
        "input_path": input_path,
        "config_version": config_version,
        "threshold_high": threshold_high,
        "threshold_review": threshold_review,
        "render_diagnostics": render_diagnostics,
        "quick_mode": quick_mode,
        "threshold_high_by_track": threshold_high_by_track,
    }

    with _run_lock:
        if _active_run_id is None:
            pass  # will start below
        else:
            _run_queue.append(kwargs)
            return

    start_run(**kwargs)


def cancel_run(db_path: str, run_id: str) -> None:
    """Mark a run as failed/cancelled."""
    write_db(
        db_path,
        "UPDATE runs SET status = ?, error_message = ? WHERE id = ?",
        ("failed", "Cancelled by user", run_id),
    )
    _emit_event(run_id, {"event": "cancelled", "timestamp": time.time()})


@_needs_run_lock
def recluster_run(db_path: str, run_dir: str, run_id: str) -> dict:
    """Redo the grouping and the entities after decisions, without rescoring.

    Splink is the expensive part and nothing a decision changes affects it, so
    the scored pairs are reused. What does change: a split can dissolve an exact
    group, which changes the units, and a unit the scorer never saw has no pairs
    at all. Those units are clustered on their human edges alone and reported,
    because only a full rerun can score them.
    """
    import pandas as pd

    from app.pipeline.dedupe import units as units_module
    from app.pipeline.dedupe.stage_2_exact import EXACT_GROUPS_FILENAME, run_stage_2_exact
    from app.pipeline.dedupe.stage_3_score import (
        PAIRS_FILENAME, never_scored, refresh_after_labels, repoint_pairs,
    )
    from app.pipeline.dedupe.stage_4_cluster import run_stage_4_cluster
    from app.pipeline.dedupe.stage_5_entities import run_stage_5_entities
    from app.pipeline.dedupe.stage_0_load import EVENTS_FILENAME
    from app.services.pair_labels import decisions_by_scope, labels_frame

    started = time.time()
    run_dir_path = Path(run_dir)
    config_dir = run_dir_path / "config"
    if not (run_dir_path / PAIRS_FILENAME).is_file():
        raise FileNotFoundError(str(run_dir_path / PAIRS_FILENAME))

    labels = labels_frame(db_path)
    decisions = decisions_by_scope(db_path)

    before = pd.read_parquet(run_dir_path / EXACT_GROUPS_FILENAME)
    counts: dict = {}
    counts.update(run_stage_2_exact(run_dir=run_dir, config_dir=str(config_dir),
                                    labels=labels, decisions=decisions))
    after = pd.read_parquet(run_dir_path / EXACT_GROUPS_FILENAME)
    groups_rebuilt = not before.equals(after)

    units_rebuilt = False
    unscored = 0
    if groups_rebuilt:
        records = pd.read_parquet(run_dir_path / "records.parquet")
        events_path = run_dir_path / EVENTS_FILENAME
        events = pd.read_parquet(events_path) if events_path.is_file() else None
        units, members = units_module.build_units(records, after, events)
        units.to_parquet(run_dir_path / units_module.UNITS_FILENAME, index=False)
        members.to_parquet(run_dir_path / units_module.UNIT_MEMBERS_FILENAME, index=False)
        units_rebuilt = True

        # The pairs follow their records onto whatever units hold them now, so a
        # merge never loses the candidates its members had.
        pairs = pd.read_parquet(run_dir_path / PAIRS_FILENAME)
        if len(pairs):
            pairs = repoint_pairs(pairs, members)
            known = set(units["unit_id"].astype(str))
            keep = (pairs["unit_id_l"].astype(str).isin(known)
                    & pairs["unit_id_r"].astype(str).isin(known))
            pairs = pairs[keep.to_numpy()]
            pairs.to_parquet(run_dir_path / PAIRS_FILENAME, index=False)
        # Units nothing has ever compared — not units that happen to be in no
        # pair, which is most of them in any run.
        unscored = len(never_scored(run_dir_path, units))

    counts.update(refresh_after_labels(run_dir, labels))
    counts.update(run_stage_4_cluster(run_dir=run_dir, config_dir=str(config_dir),
                                      labels=labels, decisions=decisions))
    counts.update(run_stage_5_entities(run_dir=run_dir, db_path=db_path))

    # Merged, never replaced: a recluster reruns stages 2 to 5 and not stage 0,
    # so the loader's counts have to survive it (services/run_counts).
    from app.services import run_counts

    merged = run_counts.merge(db_path, run_id, counts)

    from app.routers.runs import _normalize_counts

    note = (f"A reviewer's split made {unscored} new unit(s), and no pair has "
            "scored them yet. Start the run again to score them.") if unscored else None
    return {
        "ok": True,
        "exact_groups_rebuilt": groups_rebuilt,
        "units_rebuilt": units_rebuilt,
        "unscored_units": unscored,
        "unscored_note": note,
        "elapsed_seconds": round(time.time() - started, 1),
        "counts": _normalize_counts(merged),
    }

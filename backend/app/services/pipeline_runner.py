# backend/app/services/pipeline_runner.py
"""Pipeline execution service — run queue, background execution, SSE progress."""

import asyncio
import collections
import contextlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import query_db, write_db
from app.profiles.base import LoadOptions
from app.pipeline import gbt_model
from app.services.audit_logger import log_event
from app.services.config_manager import get_version
from app.services.label_applier import apply_labels

logger = logging.getLogger(__name__)

# Fallback bucketing lines for the CALIBRATED GBT score (0..1), used only when a run has
# no held-out eval set to derive them from (gbt_train.derive_gbt_thresholds). Re-exported
# by routers.model so the apply endpoint and the in-band auto-apply agree on one default.
GBT_DEFAULT_HIGH = 0.80
GBT_DEFAULT_REVIEW = 0.10


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


def refresh_counts_after_labels(
    db_path: str, run_dir: str, run_id: str, label_result: dict
) -> dict:
    """Recompute and persist a run's counts after labels were re-applied on their own.

    ``apply_labels`` rewrites merged_dataset.csv in place, but the standalone
    apply-labels endpoint never wrote counts_json back — so the UI kept showing
    pre-label numbers next to post-label data. This closes that gap.

    Keys ``_collect_counts`` cannot derive from the CSVs (decision provenance, the
    pre-label baseline, library size) are carried over from the stored counts.
    """
    from app.services import run_counts

    return run_counts.merge(db_path, run_id, {
        **_collect_counts(Path(run_dir)),
        "labels_applied": label_result.get("applied", 0),
        "labels_unmatched": label_result.get("unmatched", 0),
    })


# ---------------------------------------------------------------------------
# GBT decision: collapse guard + in-band application
# ---------------------------------------------------------------------------


def _distinct_gbt_scores(run_dir: str) -> int:
    """Count distinct CALIBRATED GBT scores across a run's scored pairs. A near-bimodal
    collapse (the R failure) shows up as a tiny count — e.g. 2 for 0.001/0.999. Returns 0
    when the run has not been GBT-scored yet."""
    import pandas as pd

    p = Path(run_dir) / "linkage_scored.parquet"
    if not p.exists():
        return 0
    try:
        df = pd.read_parquet(p, columns=["gbt_score"])
    except Exception:
        return 0
    if "gbt_score" not in df.columns or len(df) == 0:
        return 0
    return int(df["gbt_score"].round(6).nunique())


def _collapse_reason(review_before: int, review_after: int, distinct_scores: int):
    """If applying the GBT looks like the R collapse failure, return a human-readable
    reason; else None. Two blind spots the old 'band emptied to 0' check missed:
      * near-bimodal scores regardless of band counts — catches a first-ever apply where
        the Splink baseline review band was already 0 (0 -> 0 slips past an emptied check);
      * a band that shrinks to a tiny remnant (e.g. 197 -> 1), not just to exactly 0."""
    if 0 < distinct_scores <= 3:
        return (f"the GBT collapsed to a near-bimodal score "
                f"({distinct_scores} distinct value(s) across all scored pairs)")
    if review_before > 0:
        floor = max(1, round(0.05 * review_before))
        if review_after < floor:
            return (f"the review band collapsed from {review_before} to {review_after} "
                    f"(below the {floor}-pair safety floor)")
    return None


def _decision_model_from_settings(settings: dict) -> tuple[str, int | None]:
    """('gbt:<version>' | 'gbt' | 'splink', version) from a run's linkage_settings snapshot."""
    if settings.get("gbt_score_column"):
        v = settings.get("gbt_model_version")
        return (f"gbt:{v}" if v is not None else "gbt"), (int(v) if v is not None else None)
    return "splink", None


def _load_run_settings(config_dir: str) -> tuple[Path, dict]:
    p = Path(config_dir) / "linkage_settings.json"
    return p, json.loads(p.read_text(encoding="utf-8"))


def _enable_gbt_bucketing(config_dir: str, version: int, threshold_high: float, threshold_review: float) -> None:
    """Turn on GBT bucketing in a run's snapshot, preserving the pre-apply Splink
    thresholds so a later revert can restore them."""
    path, s = _load_run_settings(config_dir)
    s.setdefault("splink_threshold_high", s.get("match_probability_threshold_high"))
    s.setdefault("splink_threshold_review", s.get("match_probability_threshold_review"))
    s["gbt_score_column"] = "gbt_score"
    s["gbt_enabled"] = True
    s["gbt_model_version"] = version
    s["match_probability_threshold_high"] = threshold_high
    s["match_probability_threshold_review"] = threshold_review
    path.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _disable_gbt_bucketing(config_dir: str) -> None:
    """Revert a run's snapshot to Splink bucketing, restoring the preserved Splink lines."""
    path, s = _load_run_settings(config_dir)
    if "splink_threshold_high" in s:
        s["match_probability_threshold_high"] = s["splink_threshold_high"]
    if "splink_threshold_review" in s:
        s["match_probability_threshold_review"] = s["splink_threshold_review"]
    s["gbt_score_column"] = ""
    s["gbt_enabled"] = False
    s["gbt_model_version"] = None
    path.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _splink_review_band_count(run_dir: str, config_dir: str) -> int:
    """Pairs whose raw Splink probability sits in the run's current review band — the
    baseline the collapse guard compares the GBT-bucketed review count against. Read
    BEFORE enabling GBT, while the snapshot thresholds are still Splink-scale."""
    import pandas as pd

    p = Path(run_dir) / "linkage_scored.parquet"
    if not p.exists():
        return 0
    try:
        _, s = _load_run_settings(config_dir)
        high = float(s.get("match_probability_threshold_high"))
        review = float(s.get("match_probability_threshold_review"))
        prob = pd.to_numeric(pd.read_parquet(p, columns=["match_probability"])["match_probability"],
                             errors="coerce")
        return int(((prob >= review) & (prob < high)).sum())
    except Exception:
        return 0


def _derive_gbt_thresholds_safe(db_path: str, run_dir: str) -> tuple[float, float]:
    """Held-out-derived GBT bucket lines, or the fixed defaults when no eval set exists."""
    try:
        from app.pipeline.gbt_train import derive_gbt_thresholds

        derived = derive_gbt_thresholds(db_path, run_dir)
    except Exception:
        derived = None
    return derived or (GBT_DEFAULT_HIGH, GBT_DEFAULT_REVIEW)


def apply_active_gbt_bucketing(
    db_path: str,
    run_dir: str,
    config_dir: str,
    run_id: str,
    active_version: int,
    progress_callback=None,
) -> dict:
    """Bucket a fresh run on the active GBT model, in-band, with the same collapse guard
    as the manual /api/model/apply endpoint. Assumes Stage 2.5 already scored the pairs
    with ``active_version``. Enables GBT bucketing, runs Stage 3 on the GBT score, and if
    the guard trips falls back to Splink (re-runs Stage 3) rather than failing the run —
    a collapsed model must never silently empty the review queue. Returns the decision.
    """
    from app.pipeline.stage_3_evaluate import run_stage_3

    review_before = _splink_review_band_count(run_dir, config_dir)
    gbt_high, gbt_review = _derive_gbt_thresholds_safe(db_path, run_dir)
    _enable_gbt_bucketing(config_dir, active_version, gbt_high, gbt_review)
    run_stage_3(run_dir=run_dir, config_dir=config_dir, progress_callback=progress_callback)

    review_after = _read_csv_row_count(Path(run_dir) / "matches_for_review.csv")
    distinct = _distinct_gbt_scores(run_dir)
    reason = _collapse_reason(review_before, review_after, distinct)
    if reason:
        _disable_gbt_bucketing(config_dir)
        run_stage_3(run_dir=run_dir, config_dir=config_dir, progress_callback=progress_callback)
        warning = (f"Active GBT model v{active_version} was NOT applied to this run: {reason}. "
                   "Fell back to Splink bucketing so the review queue is preserved.")
        if progress_callback:
            progress_callback("warning", {"stage": 2.5, "message": warning})
        log_event(
            db_path, user="system", kind="model",
            description=f"Run {run_id}: active GBT v{active_version} collapse-guarded; reverted to Splink",
            metadata={"run_id": run_id, "reason": reason, "review_before": review_before,
                      "review_after": review_after, "distinct_scores": distinct},
        )
        return {"decision_model": "splink", "decision_version": None, "warning": warning,
                "review_before": review_before, "review_after": review_after}

    return {"decision_model": f"gbt:{active_version}", "decision_version": active_version,
            "warning": None, "review_before": review_before, "review_after": review_after}


def revert_run_to_splink(db_path: str, run_dir: str, config_dir: str, run_id: str) -> dict:
    """Un-apply the GBT from a run: re-bucket on the preserved Splink thresholds and clear
    the GBT flags. The inverse of a GBT apply/auto-apply. Returns the refreshed counts."""
    _, s = _load_run_settings(config_dir)
    splink_high = s.get("splink_threshold_high", s.get("match_probability_threshold_high"))
    splink_review = s.get("splink_threshold_review", s.get("match_probability_threshold_review"))
    return rebucket_run(
        db_path, run_dir, config_dir, run_id=run_id,
        threshold_high=splink_high, threshold_review=splink_review,
        gbt_score_column="", score_with_gbt=False,
    )


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
    _write_config_files(config_row, config_dir, threshold_high, threshold_review)

    # Update run status
    now = datetime.now(timezone.utc).isoformat()
    write_db(
        db_path,
        "UPDATE runs SET status = ?, started_at = ?, current_stage = ? WHERE id = ?",
        ("running", now, "0", run_id),
    )

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

    try:
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

            progress_callback("complete", {"counts": counts})

            log_event(
                db_path,
                user="system",
                kind="run",
                description=f"Pipeline run {run_id} completed",
                metadata={"run_id": run_id, "counts": counts},
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


def rebucket_run(
    db_path: str,
    run_dir: str,
    config_dir: str,
    run_id: str,
    threshold_high: float | None = None,
    threshold_review: float | None = None,
    gbt_score_column: str | None = None,
    score_with_gbt: bool = False,
    gbt_model_version: int | None = None,
    preserve_splink_baseline: bool = False,
) -> dict:
    """Re-bucket a completed run without re-running Splink, then re-apply labels.

    Optionally re-scores with the GBT first (``score_with_gbt``) and/or updates
    thresholds, the gbt_score_column flag and the decided model version in the run's
    linkage_settings. ``preserve_splink_baseline`` stashes the pre-apply Splink
    thresholds (once) so a later revert can restore them. Diagnostics are regenerated so
    the histogram tells the truth about the current decision score. Label re-application
    always runs last so human labels stay paramount. Returns the refreshed output counts.
    """
    from app.pipeline.stage_2_5_gbt import run_stage_2_5_gbt
    from app.pipeline.stage_3_evaluate import run_stage_3

    settings_path = Path(config_dir) / "linkage_settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if preserve_splink_baseline:
        settings.setdefault("splink_threshold_high", settings.get("match_probability_threshold_high"))
        settings.setdefault("splink_threshold_review", settings.get("match_probability_threshold_review"))
    if threshold_high is not None:
        settings["match_probability_threshold_high"] = threshold_high
    if threshold_review is not None:
        settings["match_probability_threshold_review"] = threshold_review
    if gbt_score_column is not None:
        settings["gbt_score_column"] = gbt_score_column
        settings["gbt_enabled"] = bool(gbt_score_column)
        if not gbt_score_column:
            settings["gbt_model_version"] = None
    if gbt_model_version is not None:
        settings["gbt_model_version"] = gbt_model_version
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    if score_with_gbt:
        run_stage_2_5_gbt(run_dir=run_dir, version=gbt_model_version)

    # Regenerate diagnostics (Splink unchanged, but the histogram/threshold copy must
    # reflect the current decision score after a GBT apply or a revert to Splink).
    run_stage_3(run_dir=run_dir, config_dir=config_dir, generate_diagnostics=True)

    # Model-only baseline before labels are re-applied (see _run_pipeline).
    pre_label_counts = _collect_counts(Path(run_dir))
    label_result = apply_labels(db_path, run_dir)

    counts = _collect_counts(Path(run_dir))
    counts["pre_labels"] = pre_label_counts
    counts["labels_applied"] = label_result.get("applied", 0)
    counts["labels_unmatched"] = label_result.get("unmatched", 0)
    decision_model, decision_version = _decision_model_from_settings(settings)
    counts["decision_model"] = decision_model
    counts["decision_model_version"] = decision_version

    th = settings.get("match_probability_threshold_high")
    tr = settings.get("match_probability_threshold_review")
    write_db(
        db_path,
        "UPDATE runs SET threshold_high = ?, threshold_review = ?, counts_json = ? WHERE id = ?",
        (th, tr, json.dumps(counts), run_id),
    )
    return counts


def rebucket_pairs(
    db_path: str,
    run_dir: str,
    config_dir: str,
    run_id: str,
    threshold_high: float | None = None,
    threshold_review: float | None = None,
) -> dict:
    """Move a scored run's bucket lines without re-running Splink.

    The dedupe answer to ``rebucket_run``: stage 3 kept every pair down to the
    candidate floor, so a new accept or review line is a re-read of
    ``pairs.parquet``, not a rerun. The run's stored counts and thresholds move
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

    settings["match_probability_threshold_high"] = high
    settings["match_probability_threshold_review"] = review
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # Merged, never replaced: moving a threshold redoes the scoring counts and
    # nothing else, so every other stage's numbers have to survive it
    # (services/run_counts). The thresholds move in the same statement.
    from app.services import run_counts

    return run_counts.merge(
        db_path, run_id,
        rebucket(run_dir, high, review, labels_frame(db_path)),
        extra_sql="threshold_high = ?, threshold_review = ?, ",
        extra_params=(high, review),
    )


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

    note = (f"{unscored} unit(s) were created by a split and have no scored pairs. "
            "Rerun the pipeline to score them.") if unscored else None
    return {
        "ok": True,
        "exact_groups_rebuilt": groups_rebuilt,
        "units_rebuilt": units_rebuilt,
        "unscored_units": unscored,
        "unscored_note": note,
        "elapsed_seconds": round(time.time() - started, 1),
        "counts": _normalize_counts(merged),
    }

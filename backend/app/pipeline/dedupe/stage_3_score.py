# backend/app/pipeline/dedupe/stage_3_score.py
"""Stage 3: score pairs of units with Splink, one track at a time.

Reads ``records.parquet`` and ``exact_groups.parquet``, builds the units stage 2
left behind (``units.py``), and for each track:

  1. counts the pairs every blocking rule would make and stops if the total is
     over the track's ``max_pairs`` — nothing is scored when a rule explodes;
  2. trains Splink on DuckDB, ``dedupe_only``, u by random sampling and m by EM;
  3. predicts down to the candidate threshold.

The scored pairs are bucketed on the run's thresholds and then overlaid with
the imported labels, exactly as ``docs/LINKAGE.md`` sets out. Human labels come
next; ``apply_overlays`` already takes them and is given an empty set for now.

Outputs, all in the run folder:

  ``units.parquet``            one representative row per unit
  ``unit_members.parquet``     unit_id, record_id
  ``pairs.parquet``            one row per scored pair
  ``blocking_report.json``     pairs per blocking rule, per track, and the budget
  ``score_eval.json``          what the accepted pairs do to the existing labels
  ``splink_model_<track>.json``, ``diagnostics/``
"""

import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.pipeline.dedupe import label_overlay
from app.pipeline.dedupe import units as units_module
from app.pipeline.dedupe import score_eval
from app.pipeline.dedupe.stage_0_load import EVENTS_FILENAME
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME
from app.pipeline.dedupe.stage_2_exact import EXACT_GROUPS_FILENAME
from app.profiles import get_profile
from app.rules import linkage

logging.getLogger("splink").setLevel(logging.INFO)

PAIRS_FILENAME = "pairs.parquet"
BLOCKING_REPORT_FILENAME = "blocking_report.json"
SCORE_EVAL_FILENAME = "score_eval.json"
CONTRADICTIONS_FILENAME = "contradictions.json"
MODEL_FILENAME = "splink_model_{track}.json"

STAGE = 3
STAGE_NAME = "score"

# DuckDB assumes most of the machine's RAM unless told otherwise, and a PSC run
# on the server has about 6 GB to play with (DESIGN.md, D17).
DEFAULT_MEMORY_LIMIT = "6GB"

# Pairs sampled to estimate u. Splink's own default is a million; five million
# steadies the estimate without costing much on a dataset this size.
U_SAMPLE_PAIRS = 5e6


class BlockingBudgetError(RuntimeError):
    """A track's blocking rules would make more pairs than its budget allows.

    Carries the per-rule counts so the run's failure can name the rule to fix
    rather than leaving the user to guess, the same way an unmapped lookup
    value does.
    """

    def __init__(self, track: str, budget: int, total: int, rules: list[dict]):
        self.track = track
        self.budget = int(budget)
        self.total = int(total)
        self.rules = rules
        worst = max(rules, key=lambda r: r["pairs"]) if rules else None
        worst_text = (
            f" The biggest is '{worst['id']}' with {worst['pairs']:,}."
            if worst else ""
        )
        super().__init__(
            f"Blocking on the {track} track would make {total:,} pairs, over the "
            f"budget of {budget:,}.{worst_text} Tighten the rule or raise max_pairs."
        )

    def detail(self) -> dict:
        return {
            "kind": "blocking_budget",
            "track": self.track,
            "budget": self.budget,
            "total": self.total,
            "rules": self.rules,
        }


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def memory_limit() -> str:
    return os.environ.get("SPLINK_MEMORY_LIMIT", DEFAULT_MEMORY_LIMIT)


def _db_api(temp_dir: Path | None = None):
    """A DuckDB backend with the run's memory cap already on it."""
    from splink import DuckDBAPI

    api = DuckDBAPI()
    api._con.execute(f"SET memory_limit='{memory_limit()}'")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        api._con.execute(f"SET temp_directory='{temp_dir}'")
    return api


# ---------------------------------------------------------------------------
# The blocking budget — run before Splink, never after
# ---------------------------------------------------------------------------


def count_blocking_pairs(track_units: pd.DataFrame, sql: str, db_api) -> int:
    """How many pairs one blocking rule would put in front of the scorer."""
    from splink.blocking_analysis import count_comparisons_from_blocking_rule

    counts = count_comparisons_from_blocking_rule(
        table_or_tables=[track_units],
        blocking_rule=linkage.build_blocking_rule(sql),
        link_type="dedupe_only",
        db_api=db_api,
        unique_id_column_name="unit_id",
    )
    return int(counts["number_of_comparisons_to_be_scored_post_filter_conditions"])


def blocking_budget_report(
    units: pd.DataFrame,
    settings: dict,
    db_api,
    progress_callback=None,
) -> tuple[dict, "BlockingBudgetError | None"]:
    """``(report, failure)``: pairs per blocking rule, per track, against its budget.

    The failure is the first track over budget. It is returned rather than
    raised so the caller can write the report first — the screen needs to name
    the rule that blew the budget, and a run that dies before writing anything
    cannot.
    """
    report = {"memory_limit": memory_limit(), "tracks": {}}
    failure: BlockingBudgetError | None = None

    for track in linkage.TRACK_KEYS:
        config = linkage.track_settings(settings, track)
        rows = units_module.track_units(units, track)
        rules = linkage.blocking_rules(config)
        budget = linkage.max_pairs(config)

        counted = []
        for rule in rules:
            pairs = count_blocking_pairs(rows, rule["sql"], db_api) if len(rows) > 1 else 0
            counted.append({
                "id": rule["id"],
                "description": rule["description"],
                "sql": rule["sql"],
                "pairs": pairs,
            })
            _step(
                f"  {track}/{rule['id']}: {pairs:,} pairs — {rule['sql']}",
                progress_callback,
            )

        total = sum(r["pairs"] for r in counted)
        over = total > budget
        report["tracks"][track] = {
            "units": int(len(rows)),
            "budget": budget,
            "total": total,
            "over_budget": over,
            "rules": counted,
        }
        if over and failure is None:
            failure = BlockingBudgetError(track, budget, total, counted)

    if failure is not None:
        report["error"] = failure.detail()
    return report, failure


# ---------------------------------------------------------------------------
# Splink
# ---------------------------------------------------------------------------


def _comparison_columns(config: dict) -> list[str]:
    columns = []
    for spec in linkage.comparisons(config):
        column = spec.get("column")
        if isinstance(column, str) and column not in columns:
            columns.append(column)
        for key, value in (spec.get("splink_args") or {}).items():
            if key.endswith("_col_name") and isinstance(value, str) and value not in columns:
                columns.append(value)
    return columns


def _splink_frame(rows: pd.DataFrame, config: dict,
                  extra_sql: list[str] | None = None) -> pd.DataFrame:
    """Only the columns Splink needs: the id, everything blocked on, everything compared.

    *extra_sql* is any further rule text whose columns must travel too — the
    deterministic rules the prior is estimated from name columns that nothing
    else in the settings mentions.
    """
    wanted = ["unit_id"]
    for rule in linkage.blocking_rules(config):
        wanted.extend(sorted(linkage.sql_columns(rule["sql"])))
    for rule in linkage.em_rules(config):
        wanted.extend(sorted(linkage.sql_columns(rule)))
    for rule in extra_sql or []:
        wanted.extend(sorted(linkage.sql_columns(rule)))
    wanted.extend(_comparison_columns(config))
    keep, seen = [], set()
    for column in wanted:
        if column in rows.columns and column not in seen:
            seen.add(column)
            keep.append(column)
    frame = rows[keep].copy()
    frame["unit_id"] = frame["unit_id"].astype(str)
    return frame


def _deterministic_rules(ruleset: dict, track: str) -> list[str]:
    """The track's match keys as SQL, for the prior Splink estimates from them."""
    rules = []
    for key in (ruleset.get("match_keys") or []):
        if not isinstance(key, dict) or key.get("track") != track:
            continue
        columns = [c for c in (key.get("columns") or []) if isinstance(c, str)]
        if columns:
            rules.append(" AND ".join(f"l.{c} = r.{c}" for c in columns))
    return rules


def train_track(
    rows: pd.DataFrame,
    config: dict,
    settings: dict,
    ruleset: dict,
    track: str,
    run_dir: Path,
    progress_callback=None,
):
    """Train a Splink model on one track's units and return ``(linker, pairs)``."""
    from splink import Linker, SettingsCreator

    prior = settings.get("probability_two_random_records_match")
    deterministic = _deterministic_rules(ruleset, track) if prior is None else []
    frame = _splink_frame(rows, config, deterministic)
    candidate, _review, _high = linkage.thresholds(settings)

    kwargs = dict(
        link_type="dedupe_only",
        unique_id_column_name="unit_id",
        comparisons=[linkage.build_comparison(c) for c in linkage.comparisons(config)],
        blocking_rules_to_generate_predictions=[
            linkage.build_blocking_rule(r["sql"]) for r in linkage.blocking_rules(config)
        ],
        max_iterations=int(settings.get("em_iterations", linkage.DEFAULT_EM_ITERATIONS)),
        # Splink only emits the gamma columns — which agreement level each
        # comparison reached — when both retain flags are on. The per-pair
        # explanation reads them, so they have to survive predict. The compared
        # values come back too and are dropped again in ``finalise_pairs``,
        # because the units file already holds them.
        retain_matching_columns=True,
        retain_intermediate_calculation_columns=True,
    )
    if prior is not None:
        kwargs["probability_two_random_records_match"] = float(prior)

    db_api = _db_api(run_dir / "duckdb_tmp")
    linker = Linker(frame, SettingsCreator(**kwargs), db_api=db_api)

    if prior is None:
        recall = float(settings.get("deterministic_recall",
                                    linkage.DEFAULT_DETERMINISTIC_RECALL))
        if deterministic:
            _step(
                f"  Estimating the prior from {len(deterministic)} deterministic "
                f"rule(s) at recall {recall}...",
                progress_callback,
            )
            try:
                linker.training.estimate_probability_two_random_records_match(
                    [linkage.build_blocking_rule(r) for r in deterministic], recall
                )
            except Exception as exc:  # a bad prior must not lose the whole run
                _step(f"  WARNING: could not estimate the prior ({exc}); "
                      "keeping Splink's default.", progress_callback)
        else:
            _step("  No deterministic rules for this track; keeping Splink's "
                  "default prior.", progress_callback)

    _step(f"  Estimating u by random sampling ({U_SAMPLE_PAIRS:,.0f} pairs)...",
          progress_callback)
    t0 = time.time()
    linker.training.estimate_u_using_random_sampling(
        max_pairs=U_SAMPLE_PAIRS, seed=settings.get("random_seed")
    )
    _step(f"  u done ({time.time() - t0:.1f}s)", progress_callback)

    # Labels never train Splink (D9): m comes from EM, u from sampling.
    for rule in linkage.em_rules(config):
        _step(f"  EM on {rule}...", progress_callback)
        t0 = time.time()
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(
                linkage.build_blocking_rule(rule), fix_u_probabilities=True
            )
            _step(f"  EM done ({time.time() - t0:.1f}s)", progress_callback)
        except Exception as exc:
            _step(f"  WARNING: EM on {rule} failed ({exc}); keeping the current m.",
                  progress_callback)

    _step(f"  Predicting down to {candidate}...", progress_callback)
    t0 = time.time()
    predictions = linker.inference.predict(threshold_match_probability=candidate)
    pairs = predictions.as_pandas_dataframe()
    _step(f"  {len(pairs):,} candidate pairs ({time.time() - t0:.1f}s)",
          progress_callback)
    return linker, pairs


# ---------------------------------------------------------------------------
# Bucketing and the overlays
# ---------------------------------------------------------------------------


def _ordered_pairs(pairs: pd.DataFrame) -> pd.DataFrame:
    """``unit_id_l`` < ``unit_id_r`` as strings, so a pair has one identity."""
    left = pairs["unit_id_l"].astype(str)
    right = pairs["unit_id_r"].astype(str)
    swap = left > right
    pairs = pairs.copy()
    pairs["unit_id_l"] = np.where(swap, right, left)
    pairs["unit_id_r"] = np.where(swap, left, right)
    return pairs


def bucket_of(scores, review: float, high: float) -> np.ndarray:
    values = pd.to_numeric(scores, errors="coerce").to_numpy(dtype="float64")
    return np.where(values >= high, "accept",
                    np.where(values >= review, "review", "reject"))


def apply_overlays(
    pairs: pd.DataFrame,
    units: pd.DataFrame,
    review: float,
    high: float,
) -> pd.DataFrame:
    """Bucket the pairs and lay the imported labels on top.

    The score decides first. Then, per LINKAGE.md:

      * two units carrying the same single existing id are accepted, with
        ``decided_by: "import"``;
      * two units carrying different single ids keep their score bucket and are
        flagged ``import_disagrees``, which is a signal and never a decision.

    This is everything ``pairs.parquet`` holds. The human overlay comes last and
    is applied where it is read — ``label_overlay.apply_to_pairs`` in memory and
    the same rule in SQL in ``pairs_reader`` — so recording one decision never
    rewrites a file with millions of rows in it.
    """
    pairs = _ordered_pairs(pairs)
    if not len(pairs):
        for column in ("score_bucket", "bucket", "decided_by", "held_group_id"):
            pairs[column] = pd.Series(dtype="object")
        pairs["import_disagrees"] = pd.Series(dtype="bool")
        return pairs

    lookup = units.set_index(units["unit_id"].astype(str))
    left_id = pairs["unit_id_l"].map(lookup["existing_entity_id"])
    right_id = pairs["unit_id_r"].map(lookup["existing_entity_id"])
    both = left_id.notna() & right_id.notna()
    agrees = (both & (left_id == right_id)).to_numpy()
    disagrees = (both & (left_id != right_id)).to_numpy()

    score_bucket = bucket_of(pairs["match_probability"], review, high)
    pairs["score_bucket"] = score_bucket
    pairs["bucket"] = np.where(agrees, "accept", score_bucket)
    pairs["decided_by"] = np.where(agrees, "import", "score")
    pairs["import_disagrees"] = disagrees

    left_held = pairs["unit_id_l"].map(lookup["held_group_id"])
    right_held = pairs["unit_id_r"].map(lookup["held_group_id"])
    same_held = (left_held.notna() & (left_held == right_held)).to_numpy()
    # np.where keeps None; Series.where would put NaN in an object column.
    pairs["held_group_id"] = pd.Series(
        np.where(same_held, left_held.to_numpy(), None),
        index=pairs.index, dtype="object",
    )
    return pairs


def _priority_totals(pairs: pd.DataFrame, units: pd.DataFrame) -> pd.DataFrame:
    """The pair's summed priority columns — what the review table sorts on."""
    lookup = units.set_index(units["unit_id"].astype(str))
    for column in get_profile().priority_columns:
        if column not in units.columns:
            continue
        values = pd.to_numeric(lookup[column], errors="coerce")
        left = pairs["unit_id_l"].map(values).fillna(0.0)
        right = pairs["unit_id_r"].map(values).fillna(0.0)
        pairs[f"priority_{column}"] = left + right
    return pairs


PAIR_HEAD = ["unit_id_l", "unit_id_r", "track", "match_probability", "match_weight",
             "score_bucket", "bucket", "decided_by", "import_disagrees", "held_group_id"]


def finalise_pairs(pairs: pd.DataFrame, units: pd.DataFrame) -> pd.DataFrame:
    """The pairs file's columns, in the order LINKAGE.md lists them."""
    pairs = _priority_totals(pairs, units)
    gammas = sorted(c for c in pairs.columns if c.startswith("gamma_"))
    priority = sorted(c for c in pairs.columns if c.startswith("priority_"))
    head = [c for c in PAIR_HEAD if c in pairs.columns]
    return pairs[head + gammas + priority].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def render_track_diagnostics(linker, pairs: pd.DataFrame, track: str, diag_dir: Path,
                             review: float, high: float, progress_callback=None) -> None:
    """Splink's match-weight and m/u charts plus a score histogram, per track."""
    diag_dir.mkdir(parents=True, exist_ok=True)
    for name, builder in (
        ("match_weights", lambda l: l.visualisations.match_weights_chart()),
        ("m_u_parameters", lambda l: l.visualisations.m_u_parameters_chart()),
    ):
        try:
            builder(linker).save(str(diag_dir / f"{name}_{track}.html"))
        except Exception as exc:
            _step(f"  WARNING: no {name} chart for {track}: {exc}", progress_callback)
    try:
        _score_histogram(pairs, track, review, high).save(
            str(diag_dir / f"score_distribution_{track}.html")
        )
    except Exception as exc:
        _step(f"  WARNING: no score histogram for {track}: {exc}", progress_callback)


def _score_histogram(pairs: pd.DataFrame, track: str, review: float, high: float):
    import altair as alt

    frame = pairs[["match_probability", "bucket"]].copy()
    frame["match_probability"] = pd.to_numeric(frame["match_probability"], errors="coerce")
    frame = frame.dropna(subset=["match_probability"])
    scale = alt.Scale(domain=["reject", "review", "accept"],
                      range=["#bbbbbb", "#fd7e14", "#28a745"])
    bars = alt.Chart(frame).mark_bar(stroke="white", strokeWidth=0.5).encode(
        x=alt.X("match_probability:Q", bin=alt.Bin(maxbins=50),
                title="Splink match probability"),
        y=alt.Y("count():Q", title="Pairs in this score range"),
        color=alt.Color("bucket:N", scale=scale,
                        legend=alt.Legend(title="Bucket", orient="bottom")),
    ).properties(
        width=820, height=340,
        title={"text": f"{track}: scored pairs by match probability",
               "subtitle": [f"{len(frame):,} pairs down to the candidate floor. "
                            f"Review from {review}, accept from {high}."],
               "anchor": "start"},
    )
    lines = alt.Chart(pd.DataFrame({"x": [review, high]})).mark_rule(
        color="#444", strokeWidth=2, strokeDash=[6, 4]
    ).encode(x="x:Q")
    return bars + lines


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def counts_from(units: pd.DataFrame, pairs: pd.DataFrame, evaluation: dict,
                outcome: dict | None = None) -> dict:
    """The run counts stage 3 contributes, in the pipeline's snake_case.

    The bucket counts are the ones a reviewer sees, so they carry the human
    overlay: ``pairs.parquet`` holds the score and the import overlay, and the
    evaluation has already laid the decisions on top of it.
    """
    with_human = evaluation.get("with_human") or {}
    buckets = with_human.get("by_bucket") or (
        pairs["bucket"].value_counts().to_dict() if len(pairs) else {}
    )
    contradictions = (outcome or {}).get("contradictions") or []
    applied = (outcome or {}).get("applied")
    satisfied = (outcome or {}).get("satisfied")
    counts = {
        **units_module.counts_from(units),
        "pairs_scored": int(len(pairs)),
        "pairs_accept": int(buckets.get("accept", 0)),
        "pairs_review": int(buckets.get("review", 0)),
        "pairs_reject": int(buckets.get("reject", 0)),
        "pairs_decided_by_import": int(
            (pairs["decided_by"] == "import").sum()) if len(pairs) else 0,
        "pairs_import_disagrees": int(
            pairs["import_disagrees"].fillna(False).sum()) if len(pairs) else 0,
        "entities_after_score": evaluation["entities_after"],
        "score_pair_precision": evaluation["pair_precision"],
        "score_pair_recall": evaluation["pair_recall"],
        "labels_applied": int(len(applied)) if applied is not None else 0,
        "labels_satisfied": int(len(satisfied)) if satisfied is not None else 0,
        "labels_forced": int((outcome or {}).get("forced", 0)),
        "labels_true": with_human.get("labels_true", 0),
        "labels_false": with_human.get("labels_false", 0),
        "labels_total": with_human.get("labels_applied", 0),
        "label_contradictions": len(contradictions),
        "entities_after_human": with_human.get("entities_after",
                                               evaluation["entities_after"]),
        "human_pair_precision": with_human.get("pair_precision",
                                               evaluation["pair_precision"]),
        "human_pair_recall": with_human.get("pair_recall", evaluation["pair_recall"]),
    }
    return counts


# ---------------------------------------------------------------------------
# Human labels
# ---------------------------------------------------------------------------


def label_outcomes(labels, members: pd.DataFrame, groups: pd.DataFrame) -> dict:
    """Sort the active labels into applied, satisfied and contradicted."""
    if labels is None or not len(labels):
        empty = pd.DataFrame(columns=["unit_id_l", "unit_id_r", "is_match"])
        return {"applied": empty, "satisfied": empty, "contradictions": []}
    return label_overlay.outcomes(labels, members, groups)


def write_contradictions(run_dir: Path, contradictions: list[dict]) -> None:
    """``contradictions.json``: the FALSE labels an exact key has overruled."""
    (Path(run_dir) / CONTRADICTIONS_FILENAME).write_text(
        json.dumps({"total": len(contradictions), "items": contradictions}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def run_stage_3_score(
    run_dir: str,
    config_dir: str,
    threshold_high: float | None = None,
    threshold_review: float | None = None,
    render_diagnostics: bool = True,
    labels: pd.DataFrame | None = None,
    progress_callback=None,
) -> dict:
    """Score the units of ``<run_dir>`` and write the pairs, the report and the eval.

    Parameters
    ----------
    run_dir : str
        The run's directory, holding stage 1's records and stage 2's groups.
    config_dir : str
        The run's config snapshot, holding ``ruleset.json`` and
        ``linkage_settings.json``.
    threshold_high, threshold_review : float, optional
        The run's own bucket lines. When omitted the settings' lines are used.
    render_diagnostics : bool
        Whether to write Splink's charts. Off makes a re-bucket cheap.
    labels : DataFrame, optional
        The active human labels. A label names two records, so the stage maps
        each to the unit that now holds it: already in one unit and TRUE, the
        label is satisfied; already in one unit and FALSE, it is a
        contradiction; otherwise the pair carries the decision, and is added to
        the file when the scorer never produced it.
    """
    from app.pipeline.dedupe.stage_1_clean import load_ruleset

    t_start = time.time()
    run_dir = Path(run_dir)
    config_dir = Path(config_dir)

    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    settings = json.loads(
        (config_dir / "linkage_settings.json").read_text(encoding="utf-8")
    )
    ruleset = load_ruleset(config_dir)
    candidate, settings_review, settings_high = linkage.thresholds(settings)
    review = float(threshold_review) if threshold_review is not None else settings_review
    high = float(threshold_high) if threshold_high is not None else settings_high

    records = pd.read_parquet(run_dir / RECORDS_FILENAME)
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)
    events_path = run_dir / EVENTS_FILENAME
    events = pd.read_parquet(events_path) if events_path.is_file() else None

    _step(f"Building units from {len(records):,} records...", progress_callback)
    units, members = units_module.build_units(records, groups, events)
    units.to_parquet(run_dir / units_module.UNITS_FILENAME, index=False)
    members.to_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME, index=False)
    _step(f"  {len(units):,} units", progress_callback)

    _step(f"Checking the blocking budget (DuckDB capped at {memory_limit()})...",
          progress_callback)
    budget_api = _db_api(run_dir / "duckdb_tmp")
    report, failure = blocking_budget_report(units, settings, budget_api,
                                             progress_callback)
    (run_dir / BLOCKING_REPORT_FILENAME).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    if failure is not None:
        raise failure

    scored: list[pd.DataFrame] = []
    for track in linkage.TRACK_KEYS:
        config = linkage.track_settings(settings, track)
        rows = units_module.track_units(units, track)
        if len(rows) < 2:
            _step(f"Skipping {track}: {len(rows)} unit(s), nothing to compare.",
                  progress_callback)
            continue
        if not linkage.blocking_rules(config):
            _step(f"Skipping {track}: no blocking rules in linkage_settings.",
                  progress_callback)
            continue

        _step(f"Scoring {track} ({len(rows):,} units)...", progress_callback)
        linker, pairs = train_track(rows, config, settings, ruleset, track,
                                    run_dir, progress_callback)
        linker.misc.save_model_to_json(
            str(run_dir / MODEL_FILENAME.format(track=track)), overwrite=True
        )
        pairs["track"] = track
        pairs = apply_overlays(pairs, units, review, high)
        scored.append(pairs)

        if render_diagnostics:
            render_track_diagnostics(linker, pairs, track, run_dir / "diagnostics",
                                     review, high, progress_callback)

    pairs = (
        pd.concat(scored, ignore_index=True) if scored
        else pd.DataFrame(columns=PAIR_HEAD)
    )
    pairs = finalise_pairs(pairs, units)

    outcome = label_outcomes(labels, members, groups)
    forced = label_overlay.forced_rows(outcome["applied"], pairs, units)
    outcome["forced"] = int(len(forced))
    if len(forced):
        # A human decided these; blocking or the candidate floor never offered
        # them. They join the file with no score rather than being lost.
        forced = finalise_pairs(apply_overlays(forced, units, review, high), units)
        pairs = pd.concat([pairs, forced], ignore_index=True)
        _step(f"  {len(forced):,} labelled pair(s) added that scoring never produced",
              progress_callback)
    pairs.to_parquet(run_dir / PAIRS_FILENAME, index=False)

    write_contradictions(run_dir, outcome["contradictions"])
    if outcome["contradictions"]:
        _step(f"  WARNING: {len(outcome['contradictions'])} FALSE label(s) "
              "contradicted by an exact match key", progress_callback)

    _step("Evaluating the accepted pairs against the existing labels...",
          progress_callback)
    evaluation = score_eval.evaluate(records, groups, units, members, pairs,
                                     thresholds={"candidate": candidate,
                                                 "review": review, "high": high},
                                     applied=outcome["applied"])
    (run_dir / SCORE_EVAL_FILENAME).write_text(
        json.dumps(evaluation, indent=2), encoding="utf-8"
    )

    counts = counts_from(units, pairs, evaluation, outcome)
    elapsed = time.time() - t_start
    precision = evaluation["pair_precision"]
    _step(
        f"Stage 3 complete in {elapsed:.1f}s — {counts['pairs_scored']:,} pairs "
        f"({counts['pairs_accept']:,} accept, {counts['pairs_review']:,} review), "
        f"{counts['entities_after_score']:,} entities left"
        + (f", pair precision {precision:.4f}." if precision is not None else "."),
        progress_callback,
    )

    if progress_callback:
        progress_callback("stage_end", {
            "stage": STAGE, "name": STAGE_NAME,
            "elapsed_seconds": round(elapsed, 1), **counts,
        })
    return counts


def rebucket(
    run_dir: str,
    threshold_high: float,
    threshold_review: float,
    labels: pd.DataFrame | None = None,
) -> dict:
    """Re-bucket an already-scored run on new thresholds, without Splink.

    Moving a threshold must not need a rerun, so this reads the scored pairs
    back, applies the buckets and the overlays again, and rewrites the pairs and
    the evaluation.
    """
    run_dir = Path(run_dir)
    units = pd.read_parquet(run_dir / units_module.UNITS_FILENAME)
    members = pd.read_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME)
    records = pd.read_parquet(run_dir / RECORDS_FILENAME)
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)
    pairs = pd.read_parquet(run_dir / PAIRS_FILENAME)

    keep = [c for c in pairs.columns
            if c not in ("score_bucket", "bucket", "decided_by",
                         "import_disagrees", "held_group_id")]
    pairs = apply_overlays(pairs[keep], units, float(threshold_review),
                           float(threshold_high))
    pairs = finalise_pairs(pairs, units)
    pairs.to_parquet(run_dir / PAIRS_FILENAME, index=False)
    outcome = label_outcomes(labels, members, groups)
    write_contradictions(run_dir, outcome["contradictions"])

    settings_path = run_dir / "config" / "linkage_settings.json"
    candidate = linkage.DEFAULT_CANDIDATE
    if settings_path.is_file():
        candidate = linkage.thresholds(
            json.loads(settings_path.read_text(encoding="utf-8"))
        )[0]

    evaluation = score_eval.evaluate(
        records, groups, units, members, pairs,
        thresholds={"candidate": candidate, "review": float(threshold_review),
                    "high": float(threshold_high)},
        applied=outcome["applied"],
    )
    (run_dir / SCORE_EVAL_FILENAME).write_text(
        json.dumps(evaluation, indent=2), encoding="utf-8"
    )
    return counts_from(units, pairs, evaluation, outcome)


def refresh_after_labels(run_dir: str, labels: pd.DataFrame | None) -> dict:
    """Redo the counts and the evaluation for a run whose labels have changed.

    This is what a label write costs: the parquet is left exactly as scoring
    left it, and only the derived numbers are worked out again. The overlay
    itself is applied where the pairs are read.
    """
    run_dir = Path(run_dir)
    units = pd.read_parquet(run_dir / units_module.UNITS_FILENAME)
    members = pd.read_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME)
    records = pd.read_parquet(run_dir / RECORDS_FILENAME)
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)
    pairs = pd.read_parquet(run_dir / PAIRS_FILENAME)

    settings_path = run_dir / "config" / "linkage_settings.json"
    candidate, review, high = linkage.DEFAULT_CANDIDATE, None, None
    if settings_path.is_file():
        candidate, review, high = linkage.thresholds(
            json.loads(settings_path.read_text(encoding="utf-8"))
        )

    outcome = label_outcomes(labels, members, groups)
    write_contradictions(run_dir, outcome["contradictions"])
    evaluation = score_eval.evaluate(
        records, groups, units, members, pairs,
        thresholds={"candidate": candidate, "review": review, "high": high},
        applied=outcome["applied"],
    )
    (run_dir / SCORE_EVAL_FILENAME).write_text(
        json.dumps(evaluation, indent=2), encoding="utf-8"
    )
    return counts_from(units, pairs, evaluation, outcome)

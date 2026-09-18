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

import contextlib
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app import duckdb_conn
from app.pipeline.dedupe import label_overlay
from app.pipeline.dedupe import units as units_module
from app.pipeline.dedupe import score_eval
from app.pipeline.dedupe import stage_3b_model
from app.pipeline.dedupe.stage_3b_model import MODEL_SCORE_COLUMN
from app.pipeline.dedupe.stage_0_load import EVENTS_FILENAME
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME
from app.pipeline.dedupe.stage_2_exact import EXACT_GROUPS_FILENAME
from app.profiles import get_profile
from app.rules import linkage, vetoes

logging.getLogger("splink").setLevel(logging.INFO)

# `decided_by` when a veto is what put the pair where it is (docs/PAIRS_API.md).
DECIDED_BY_VETO = "veto"

PAIRS_FILENAME = "pairs.parquet"
# The units Splink saw, so a later recluster can say exactly which ones are new.
SCORED_UNITS_FILENAME = "scored_units.parquet"
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

    def __init__(self, track: str, budget: int, total: int, rules: list[dict],
                 phase: str = "prediction"):
        self.track = track
        self.budget = int(budget)
        self.total = int(total)
        self.rules = rules
        self.phase = phase
        worst = max(rules, key=lambda r: r["pairs"]) if rules else None
        worst_text = (
            f" The biggest is '{worst['id']}' with {worst['pairs']:,}."
            if worst else ""
        )
        where = ("Blocking on" if phase == "prediction"
                 else "Training the model on")
        super().__init__(
            f"{where} the {track} track would make {total:,} pairs, over the "
            f"budget of {budget:,}.{worst_text} Tighten the rule or raise max_pairs."
        )

    def detail(self) -> dict:
        return {
            "kind": "blocking_budget",
            "track": self.track,
            "budget": self.budget,
            "total": self.total,
            "rules": self.rules,
            "phase": self.phase,
        }


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


@contextlib.contextmanager
def _phase(label: str, progress_callback=None):
    """Log a timestamped line entering *label* and another on the way out.

    Stage 3 used to report only what it had finished, so a step that never
    finished was invisible: the PSC re-score sat in one DuckDB query for over
    half an hour and the log's last line was about the step before it. Every
    phase now announces itself before it starts, which is what makes a hang
    point at the thing that hung. The closing line carries the elapsed seconds,
    so the same log also says where the time went.
    """
    _step(f"{label}...", progress_callback)
    t0 = time.time()
    try:
        yield
    finally:
        _step(f"  {label} took {time.time() - t0:.1f}s", progress_callback)


def memory_limit() -> str:
    return duckdb_conn.memory_limit()


def _db_api(temp_dir: Path | None = None):
    """A DuckDB backend carrying the run's memory *and* spill caps.

    Splink opens its own connection, so the limits go on after the fact rather
    than at open time. The spill cap matters most here: predict and EM are where
    this pipeline makes enough intermediate data to fill a disk.
    """
    from splink import DuckDBAPI

    api = DuckDBAPI()
    duckdb_conn.configure(api._con, temp_dir)
    return api


#: Kept as a name of its own because the stage calls it before opening anything.
clear_duckdb_tmp = duckdb_conn.clear_spill


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

        # The EM training rules are priced too, and against the same budget.
        # They are not part of `total`: EM runs one rule at a time, after
        # prediction blocking, so the workloads are sequential rather than
        # summed. Pricing them is not optional. An em_blocking_rule is a
        # blocking rule like any other, and a coarse one is far more dangerous
        # than a coarse prediction rule because nothing downstream trims it —
        # PSC's person rule "same birth year AND same birth month" put
        # 170,613,604 pairs through EM on a 449,397-unit sample whose entire
        # prediction workload was 2.4M, spilled 53 GB of DuckDB temp files and
        # ran until a 40-minute timeout killed it. The budget check reported
        # "2.4M of 20M, under budget" and let it through, because it only ever
        # looked at the prediction rules.
        em_counted = []
        for index, sql in enumerate(linkage.em_rules(config), start=1):
            pairs = count_blocking_pairs(rows, sql, db_api) if len(rows) > 1 else 0
            em_counted.append({
                "id": f"em{index}",
                "description": "EM training rule",
                "sql": sql,
                "pairs": pairs,
            })
            _step(f"  {track}/em{index}: {pairs:,} training pairs — {sql}",
                  progress_callback)

        worst_em = max(em_counted, key=lambda r: r["pairs"], default=None)
        em_over = bool(worst_em and worst_em["pairs"] > budget)

        report["tracks"][track] = {
            "units": int(len(rows)),
            "budget": budget,
            "total": total,
            "over_budget": over,
            "rules": counted,
            "em_rules": em_counted,
            "em_over_budget": em_over,
        }
        if over and failure is None:
            failure = BlockingBudgetError(track, budget, total, counted)
        if em_over and failure is None:
            failure = BlockingBudgetError(
                track, budget, worst_em["pairs"], em_counted, phase="training"
            )

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
    # `custom.NumericDifferenceAtThresholds` compares numbers, and the cleaning
    # engine writes text: `dob_year_clean` is a string of digits. Cast here, in
    # the frame Splink sees, so `units.parquet` keeps what was filed and the
    # review screen and the vetoes still read it (docs/LINKAGE.md).
    for column in linkage.numeric_columns(config):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
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


def _model_buckets(pairs: pd.DataFrame, score_bucket: np.ndarray,
                   model_lines: dict | None) -> tuple[np.ndarray, np.ndarray]:
    """Replace the Splink bucket with the model's, for the tracks it decides.

    ``model_lines`` is ``{track: (review, high)}`` for the graded models only
    (``stage_3b_model.deciding_lines``). A track with no entry, a pair the model
    never scored, and every track when no model is active all keep the Splink
    bucket, so this does nothing unless a graded model really is in charge.
    """
    decided = np.full(len(pairs), False)
    if not model_lines or MODEL_SCORE_COLUMN not in pairs.columns:
        return np.asarray(score_bucket, dtype=object), decided
    scores = pd.to_numeric(pairs[MODEL_SCORE_COLUMN], errors="coerce")
    tracks = pairs["track"].to_numpy() if "track" in pairs.columns \
        else np.full(len(pairs), None)
    out = np.asarray(score_bucket, dtype=object)
    for track, (review, high) in model_lines.items():
        mask = (tracks == track) & scores.notna().to_numpy()
        if not mask.any():
            continue
        out[mask] = bucket_of(scores[mask], review, high)
        decided = decided | mask
    return out, decided


def apply_overlays(
    pairs: pd.DataFrame,
    units: pd.DataFrame,
    review: float,
    high: float,
    model_lines: dict | None = None,
    ruleset: dict | None = None,
) -> pd.DataFrame:
    """Bucket the pairs, apply the vetoes, and lay the imported labels on top.

    The score decides first — Splink's, or a graded model's on the tracks
    ``model_lines`` names (`MODEL.md`, stage 3b), which is where ``decided_by``
    reads ``model`` rather than ``score``. Then, per LINKAGE.md and the "Vetoes"
    section of RULESET.md, lowest precedence first:

      * a veto caps the pair at review or puts it in reject, and records
        ``vetoed_by`` and ``veto_reason``. It overrides the scorer and the model;
      * two units carrying the same single existing id are accepted, with
        ``decided_by: "import"``. An imported label beats a veto, and a pair
        where the two disagree is flagged ``veto_conflicts_import``;
      * two units carrying different single ids keep their bucket and are
        flagged ``import_disagrees``, which is a signal and never a decision.

    This is everything ``pairs.parquet`` holds. The human overlay comes last and
    is applied where it is read — ``label_overlay.apply_to_pairs`` in memory and
    the same rule in SQL in ``pairs_reader`` — so recording one decision never
    rewrites a file with millions of rows in it. The vetoes are materialised
    here instead, because a run's ruleset is snapshotted and cannot change under
    it, and every path that moves a bucket calls this function again.
    """
    pairs = _ordered_pairs(pairs)
    if not len(pairs):
        for column in ("score_bucket", "bucket", "decided_by", "held_group_id",
                       "vetoed_by", "veto_reason"):
            pairs[column] = pd.Series(dtype="object")
        pairs["import_disagrees"] = pd.Series(dtype="bool")
        pairs["veto_conflicts_import"] = pd.Series(dtype="bool")
        return pairs

    lookup = units.set_index(units["unit_id"].astype(str))
    left_id = pairs["unit_id_l"].map(lookup["existing_entity_id"])
    right_id = pairs["unit_id_r"].map(lookup["existing_entity_id"])
    both = left_id.notna() & right_id.notna()
    agrees = (both & (left_id == right_id)).to_numpy()
    disagrees = (both & (left_id != right_id)).to_numpy()

    score_bucket, by_model = _model_buckets(
        pairs, bucket_of(pairs["match_probability"], review, high), model_lines
    )
    vetoed = vetoes.apply_to_buckets(pairs, units, ruleset or {}, score_bucket)
    pairs["score_bucket"] = score_bucket
    pairs["bucket"] = np.where(agrees, "accept", vetoed["bucket"])
    pairs["decided_by"] = np.where(
        agrees, "import",
        np.where(vetoed["any"], DECIDED_BY_VETO,
                 np.where(by_model, stage_3b_model.DECIDED_BY_MODEL, "score")),
    )
    pairs["import_disagrees"] = disagrees
    pairs["vetoed_by"] = pd.Series(vetoed["vetoed_by"], index=pairs.index,
                                   dtype="object")
    pairs["veto_reason"] = pd.Series(vetoed["veto_reason"], index=pairs.index,
                                     dtype="object")
    pairs["veto_conflicts_import"] = vetoed["any"] & agrees

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
             MODEL_SCORE_COLUMN, "score_bucket", "bucket", "decided_by",
             "import_disagrees", "vetoed_by", "veto_reason",
             "veto_conflicts_import", "held_group_id"]


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


def events_by_unit(events: pd.DataFrame | None,
                   members: pd.DataFrame | None) -> pd.DataFrame | None:
    """The evidence rows with a ``unit_id``, which is how a feature builder reads them.

    ``events.parquet`` is keyed on ``record_id``, because a donation belongs to a
    donor and not to whatever unit this run happens to pool them into.
    """
    if events is None or not len(events) or members is None or not len(members):
        return None
    joined = events.copy()
    joined["record_id"] = joined["record_id"].astype(str)
    keys = members[["record_id", "unit_id"]].copy()
    keys["record_id"] = keys["record_id"].astype(str)
    return joined.merge(keys, on="record_id", how="inner")


def apply_active_models(
    run_dir,
    pairs: pd.DataFrame,
    units: pd.DataFrame,
    members: pd.DataFrame,
    events: pd.DataFrame | None,
    review: float,
    high: float,
    progress_callback=None,
    ruleset: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Stage 3b: score the pairs with each track's active model and re-bucket.

    Returns the pairs and the state that was written. With no active model
    nothing changes and the run's model state is cleared, so a run that was
    scored by a model and then re-run without one does not keep claiming it.
    """
    models = stage_3b_model.active_models()
    if not models or not len(pairs):
        stage_3b_model.clear_state(run_dir)
        return pairs, stage_3b_model.read_state(run_dir)

    review_before = int((pairs["bucket"] == "review").sum())
    _step(f"Scoring with the active model(s): "
          + ", ".join(f"{t} v{m.version}" for t, m in models.items()) + "...",
          progress_callback)
    pairs, used = stage_3b_model.score_pairs(
        pairs, units, models, events=events_by_unit(events, members),
        profile=get_profile(),
    )
    lines = stage_3b_model.deciding_lines(used)
    warning = None
    if lines:
        rebucketed = finalise_pairs(
            apply_overlays(_strip_overlays(pairs), units, review, high,
                           model_lines=lines, ruleset=ruleset), units
        )
        review_after = int((rebucketed["bucket"] == "review").sum())
        warning = stage_3b_model.collapse_reason(
            review_before, review_after, stage_3b_model.distinct_scores(rebucketed)
        )
        if warning is None:
            pairs = rebucketed
            _step(f"  buckets now follow the model ({review_after:,} in review)",
                  progress_callback)
        else:
            lines = {}
            _step(f"  WARNING: the model was not applied — {warning}. "
                  "Buckets stay on the Splink score.", progress_callback)
            if progress_callback:
                progress_callback("warning", {"stage": 3, "message": warning})
    elif used:
        _step("  the model is not graded, so it re-orders the queue and decides "
              "nothing", progress_callback)

    state = stage_3b_model.write_state(
        run_dir,
        {t: m for t, m in used.items()} if warning is None else
        {t: stage_3b_model.TrackModel(t, m.version, False, None, None)
         for t, m in used.items()},
        warning=warning, applied=bool(used),
    )
    return pairs, state


def _strip_overlays(pairs: pd.DataFrame) -> pd.DataFrame:
    """The pairs without the columns ``apply_overlays`` works out again.

    The veto columns go with them, so a re-bucket, an apply-model and a
    revert-model all run the vetoes again rather than carrying stale ones.
    """
    dropped = ("score_bucket", "bucket", "decided_by", "import_disagrees",
               "held_group_id", *vetoes.VETO_COLUMNS)
    return pairs[[c for c in pairs.columns if c not in dropped]]


def counts_from(units: pd.DataFrame, pairs: pd.DataFrame, evaluation: dict,
                outcome: dict | None = None, run_dir=None,
                untrained: list[dict] | None = None) -> dict:
    """The run counts stage 3 contributes, in the pipeline's snake_case.

    The bucket counts are the ones a reviewer sees, so they carry the human
    overlay: ``pairs.parquet`` holds the score and the import overlay, and the
    evaluation has already laid the decisions on top of it.
    """
    with_human = evaluation.get("with_human") or {}
    mine = (outcome or {}).get("mine")
    verdicts = mine["is_match"].astype(str).str.upper() if mine is not None and len(mine) \
        else pd.Series(dtype="object")
    buckets = with_human.get("by_bucket") or (
        pairs["bucket"].value_counts().to_dict() if len(pairs) else {}
    )
    contradictions = (outcome or {}).get("contradictions") or []
    applied = (outcome or {}).get("applied")
    satisfied = (outcome or {}).get("satisfied")
    counts = {
        **units_module.counts_from(units),
        "untrained_comparisons": len(untrained or []),
        "pairs_scored": int(len(pairs)),
        "pairs_accept": int(buckets.get("accept", 0)),
        "pairs_review": int(buckets.get("review", 0)),
        "pairs_reject": int(buckets.get("reject", 0)),
        "pairs_decided_by_import": int(
            (pairs["decided_by"] == "import").sum()) if len(pairs) else 0,
        "pairs_import_disagrees": int(
            pairs["import_disagrees"].fillna(False).sum()) if len(pairs) else 0,
        **vetoes.counts_from(pairs),
        "entities_after_score": evaluation["entities_after"],
        "score_pair_precision": evaluation["pair_precision"],
        "score_pair_recall": evaluation["pair_recall"],
        "labels_applied": int(len(applied)) if applied is not None else 0,
        "labels_satisfied": int(len(satisfied)) if satisfied is not None else 0,
        "labels_forced": int((outcome or {}).get("forced", 0)),
        # Every active label about this run's records, whatever became of it.
        "labels_true": int((verdicts == "TRUE").sum()),
        "labels_false": int((verdicts == "FALSE").sum()),
        "labels_total": int(len(verdicts)),
        "label_contradictions": len(contradictions),
        "entities_after_human": with_human.get("entities_after",
                                               evaluation["entities_after"]),
        "human_pair_precision": with_human.get("pair_precision",
                                               evaluation["pair_precision"]),
        "human_pair_recall": with_human.get("pair_recall", evaluation["pair_recall"]),
    }
    if run_dir is not None:
        # Which model decided this run, so the summary screen never has to guess
        # whether it is reading Splink's numbers or a model's (MODEL_API.md).
        counts.update(stage_3b_model.counts_from_state(run_dir))
    return counts


# ---------------------------------------------------------------------------
# Human labels
# ---------------------------------------------------------------------------


def unit_fingerprint(units: pd.DataFrame) -> pd.DataFrame:
    """``unit_id`` and ``unit_size`` — enough to tell one membership from another.

    A unit's id is the smallest record in it, so two units with the same id and
    the same size hold the same records: a merge adds members and changes the
    size, a split removes them and changes it too. That makes the pair a
    fingerprint without hashing a member list, which matters when there are 16
    million of them.
    """
    return pd.DataFrame({
        "unit_id": units["unit_id"].astype(str).to_numpy(),
        "unit_size": units["unit_size"].astype("int64").to_numpy()
        if "unit_size" in units.columns else 1,
    })


def write_scored_units(run_dir, units: pd.DataFrame) -> None:
    """Record which units this scoring run compared."""
    unit_fingerprint(units).to_parquet(
        Path(run_dir) / SCORED_UNITS_FILENAME, index=False
    )


def never_scored(run_dir, units: pd.DataFrame) -> list[str]:
    """The units that exist now and were not there when Splink last ran.

    Not "units in no pair" — most units are in no pair in any run, because most
    records have no candidate at all. These are the ones nothing has ever been
    able to compare, so only a full rerun can score them.
    """
    path = Path(run_dir) / SCORED_UNITS_FILENAME
    now = unit_fingerprint(units)
    if not path.is_file():
        return []
    then = pd.read_parquet(path)
    seen = set(zip(then["unit_id"].astype(str), then["unit_size"].astype("int64")))
    fresh = [
        unit_id for unit_id, size in zip(now["unit_id"], now["unit_size"])
        if (unit_id, int(size)) not in seen
    ]
    return sorted(fresh)


def repoint_pairs(pairs: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """Move every pair onto the units that hold its records now.

    A merge replaces several units with one. Their pairs with the outside world
    are still evidence about the same records, so they follow the records rather
    than being thrown away — otherwise merging a group would quietly lose every
    candidate it had. Where two old pairs land on one new pair the better score
    wins, and the row is marked ``rescored: false`` because Splink has not seen
    this pairing.
    """
    if not len(pairs):
        return pairs
    lookup = pd.Series(
        members["unit_id"].astype(str).to_numpy(),
        index=members["record_id"].astype(str).to_numpy(),
    )
    lookup = lookup[~lookup.index.duplicated(keep="first")]
    # A pair's unit id is the smallest record in that unit, so the record of the
    # old id says where the pair belongs now.
    moved = pairs.copy()
    left = moved["unit_id_l"].astype(str)
    right = moved["unit_id_r"].astype(str)
    new_left = left.map(lookup).fillna(left)
    new_right = right.map(lookup).fillna(right)
    changed = (new_left != left) | (new_right != right)
    lower = np.where(new_left <= new_right, new_left, new_right)
    upper = np.where(new_left <= new_right, new_right, new_left)
    moved["unit_id_l"] = lower
    moved["unit_id_r"] = upper
    moved["rescored"] = ~changed.to_numpy()
    # A pair whose two sides are now one unit has been answered by the merge.
    moved = moved[moved["unit_id_l"] != moved["unit_id_r"]]
    if not len(moved):
        return moved
    # A new pair is only a scored one when nothing moved onto it: if any of the
    # rows that landed here came from a unit that has since been merged, Splink
    # has never compared this pairing.
    fresh = moved.groupby(["unit_id_l", "unit_id_r"], sort=False)["rescored"].transform("all")
    moved["rescored"] = fresh
    moved = moved.sort_values("match_probability", ascending=False, kind="mergesort")
    return moved.drop_duplicates(subset=["unit_id_l", "unit_id_r"],
                                 keep="first").reset_index(drop=True)


def label_outcomes(labels, members: pd.DataFrame, groups: pd.DataFrame) -> dict:
    """Sort the active labels into applied, satisfied and contradicted.

    ``mine`` is every active label about two records this run holds, whatever
    became of it. The counts are taken from that, so the library and the run
    always agree: a label the exact keys had already satisfied is still a label
    a reviewer wrote.
    """
    if labels is None or not len(labels):
        empty = pd.DataFrame(columns=["unit_id_l", "unit_id_r", "is_match"])
        return {"applied": empty, "satisfied": empty, "contradictions": [],
                "mine": empty}
    outcome = label_overlay.outcomes(labels, members, groups)
    outcome["mine"] = label_overlay.in_this_run(labels, members)
    return outcome


def write_contradictions(run_dir: Path, contradictions: list[dict]) -> None:
    """``contradictions.json``: the FALSE labels an exact key has overruled."""
    (Path(run_dir) / CONTRADICTIONS_FILENAME).write_text(
        json.dumps({"total": len(contradictions), "items": contradictions}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def untrained_levels(model: dict) -> list[dict]:
    """Comparison levels EM left without an m or a u probability.

    A level with no m contributes nothing to the score, whatever the settings
    say. It happens when every training rule holds that comparison's column
    equal, so there is no disagreement inside the training block to learn from.
    Nothing else reports it: the run succeeds and the comparison is quietly
    worth zero. The PSC person track shipped that way and accepted pairs 37
    birth-years apart.
    """
    found = []
    for comparison in model.get("comparisons") or []:
        name = comparison.get("output_column_name")
        for level in comparison.get("comparison_levels") or []:
            if level.get("is_null_level"):
                continue  # a null level is meant to have neither
            missing = [
                key.split("_")[0] for key in ("m_probability", "u_probability")
                if level.get(key) is None
            ]
            if missing:
                found.append({
                    "comparison": name,
                    "level": level.get("label_for_charts"),
                    "missing": missing,
                })
    return found


def inspect_trained_model(path, track: str, progress_callback=None) -> list[dict]:
    """Read back the model just saved and report anything EM could not learn."""
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = untrained_levels(model)
    for entry in found:
        entry["track"] = track
    if found:
        columns = sorted({entry["comparison"] for entry in found})
        _step(
            f"  WARNING: {len(found)} comparison level(s) on the {track} track were "
            f"not trained ({', '.join(columns)}). They will count for nothing. "
            "Add an em_blocking_rule that does not hold those columns equal.",
            progress_callback,
        )
        if progress_callback:
            progress_callback("warning", {
                "stage": STAGE, "track": track, "kind": "untrained_comparisons",
                "comparisons": columns,
                "message": (
                    f"{len(found)} comparison level(s) on the {track} track were not "
                    f"trained ({', '.join(columns)}) and contribute nothing to the "
                    "score."
                ),
            })
    return found


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

    with _phase(f"Building units from {len(records):,} records", progress_callback):
        units, members = units_module.build_units(
            records, groups, events, temp_dir=run_dir / "duckdb_tmp")
        units.to_parquet(run_dir / units_module.UNITS_FILENAME, index=False)
        members.to_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME, index=False)
        write_scored_units(run_dir, units)
    _step(f"  {len(units):,} units", progress_callback)

    freed = clear_duckdb_tmp(run_dir / "duckdb_tmp")
    if freed:
        _step(f"  Cleared {freed / 2**30:.1f} GB of spill left by an earlier run",
              progress_callback)

    with _phase(f"Checking the blocking budget (DuckDB capped at {memory_limit()})",
                progress_callback):
        budget_api = _db_api(run_dir / "duckdb_tmp")
        report, failure = blocking_budget_report(units, settings, budget_api,
                                                 progress_callback)
    (run_dir / BLOCKING_REPORT_FILENAME).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    if failure is not None:
        raise failure

    scored: list[pd.DataFrame] = []
    untrained: list[dict] = []
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

        with _phase(f"Scoring {track} ({len(rows):,} units)", progress_callback):
            linker, pairs = train_track(rows, config, settings, ruleset, track,
                                        run_dir, progress_callback)
        model_path = run_dir / MODEL_FILENAME.format(track=track)
        linker.misc.save_model_to_json(str(model_path), overwrite=True)
        untrained.extend(inspect_trained_model(model_path, track, progress_callback))
        pairs["track"] = track
        with _phase(f"Applying the {track} overlays to {len(pairs):,} pairs",
                    progress_callback):
            pairs = apply_overlays(pairs, units, review, high, ruleset=ruleset)
        vetoed = int(pairs["vetoed_by"].notna().sum()) if len(pairs) else 0
        if vetoed:
            _step(f"  {vetoed:,} {track} pair(s) vetoed", progress_callback)
        scored.append(pairs)

        if render_diagnostics:
            # Splink's score histogram is drawn from every pair, so this grows
            # with the pair count rather than the unit count and is a plausible
            # suspect whenever a big run stalls after the model is trained.
            with _phase(f"Rendering {track} diagnostics", progress_callback):
                render_track_diagnostics(linker, pairs, track,
                                         run_dir / "diagnostics",
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
        forced = finalise_pairs(
            apply_overlays(forced, units, review, high, ruleset=ruleset), units)
        pairs = pd.concat([pairs, forced], ignore_index=True)
        _step(f"  {len(forced):,} labelled pair(s) added that scoring never produced",
              progress_callback)

    # Stage 3b: the track's own model, when one is active (docs/MODEL.md).
    with _phase("Applying the active track models", progress_callback):
        pairs, model_state = apply_active_models(
            run_dir, pairs, units, members, events, review, high, progress_callback,
            ruleset=ruleset,
        )
    with _phase(f"Writing {len(pairs):,} pairs", progress_callback):
        pairs.to_parquet(run_dir / PAIRS_FILENAME, index=False)

    write_contradictions(run_dir, outcome["contradictions"])
    if outcome["contradictions"]:
        _step(f"  WARNING: {len(outcome['contradictions'])} FALSE label(s) "
              "contradicted by an exact match key", progress_callback)

    with _phase("Evaluating the accepted pairs against the existing labels",
                progress_callback):
        evaluation = score_eval.evaluate(
            records, groups, units, members, pairs,
            thresholds={"candidate": candidate, "review": review, "high": high},
            applied=outcome["applied"],
            model_lines=stage_3b_model.deciding_lines(
                stage_3b_model.models_from_state(run_dir)),
        )
        _write_evaluation(run_dir, evaluation)

    counts = counts_from(units, pairs, evaluation, outcome, run_dir=run_dir,
                         untrained=untrained)
    if untrained:
        # Beside the pairs, so a reader of the report sees it without opening
        # the model, and the UI has something to show on the run.
        report["untrained_comparisons"] = untrained
        (run_dir / BLOCKING_REPORT_FILENAME).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
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


def run_ruleset(run_dir) -> dict:
    """The ruleset this run was started with, from its config snapshot.

    A run's rules are fixed once it starts, which is what makes materialising
    the vetoes into ``pairs.parquet`` safe: every later path that moves a bucket
    reads the same document back and applies the same vetoes.
    """
    from app.pipeline.dedupe.stage_1_clean import load_ruleset

    config_dir = Path(run_dir) / "config"
    if not (config_dir / "ruleset.json").is_file():
        return {}
    try:
        return load_ruleset(config_dir)
    except (OSError, json.JSONDecodeError):
        return {}


def rebucket(
    run_dir: str,
    threshold_high: float,
    threshold_review: float,
    labels: pd.DataFrame | None = None,
) -> dict:
    """Re-bucket an already-scored run on new thresholds, without Splink.

    Moving a threshold must not need a rerun, so this reads the scored pairs
    back, applies the buckets and the overlays again, and rewrites the pairs and
    the evaluation. The vetoes are re-applied with them, from the run's own
    snapshotted ruleset.
    """
    run_dir = Path(run_dir)
    units = pd.read_parquet(run_dir / units_module.UNITS_FILENAME)
    members = pd.read_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME)
    records = pd.read_parquet(run_dir / RECORDS_FILENAME)
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)
    pairs = pd.read_parquet(run_dir / PAIRS_FILENAME)
    ruleset = run_ruleset(run_dir)

    # A threshold move must not un-apply the model: the lines being moved are
    # Splink's, and a graded model keeps deciding whichever tracks it decided.
    lines = stage_3b_model.deciding_lines(stage_3b_model.models_from_state(run_dir))
    pairs = apply_overlays(_strip_overlays(pairs), units, float(threshold_review),
                           float(threshold_high), model_lines=lines, ruleset=ruleset)
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
        applied=outcome["applied"], model_lines=lines,
    )
    _write_evaluation(run_dir, evaluation)
    return counts_from(units, pairs, evaluation, outcome, run_dir=run_dir)


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
        model_lines=stage_3b_model.deciding_lines(
            stage_3b_model.models_from_state(run_dir)),
    )
    _write_evaluation(run_dir, evaluation)
    return counts_from(units, pairs, evaluation, outcome, run_dir=run_dir)


def _write_evaluation(run_dir: Path, evaluation: dict) -> None:
    """Write score_eval.json, keeping the figures stage 5 added to it.

    A label write redoes this file, and stage 5 does not run again — so the
    entity figure set has to be carried over rather than quietly dropped.
    """
    path = Path(run_dir) / SCORE_EVAL_FILENAME
    carried = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            carried = {key: existing[key]
                       for key in ("entities", "versus_existing_entity_id")
                       if key in existing}
        except (OSError, json.JSONDecodeError):
            carried = {}
    path.write_text(json.dumps({**evaluation, **carried}, indent=2), encoding="utf-8")

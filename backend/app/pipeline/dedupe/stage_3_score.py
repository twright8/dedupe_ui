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
import hashlib
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
from app.model import corpus as corpus_lib
from app.profiles import get_profile
from app.rules import linkage, vetoes

logging.getLogger("splink").setLevel(logging.INFO)

# `decided_by` when a veto is what put the pair where it is (docs/PAIRS_API.md).
DECIDED_BY_VETO = "veto"

#: How much generated blocking SQL the blocking report will carry. A hot-key
#: control inlines every oversized key, which is megabytes at PSC scale, and
#: the report is served to a browser.
MAX_REPORTED_SQL = 20_000

PAIRS_FILENAME = "pairs.parquet"
# Splink's raw predictions, one file per track, narrowed to the columns the
# pairs file keeps. Deleted once the overlays have been laid over them.
PREDICTIONS_FILENAME = "predictions_{track}.parquet"
# The units Splink saw, so a later recluster can say exactly which ones are new.
SCORED_UNITS_FILENAME = "scored_units.parquet"
BLOCKING_REPORT_FILENAME = "blocking_report.json"
SCORE_EVAL_FILENAME = "score_eval.json"
CONTRADICTIONS_FILENAME = "contradictions.json"
MODEL_FILENAME = "splink_model_{track}.json"
# Beside the saved model, a fingerprint of everything that shaped it. A run
# that dies after training — which is what a PSC run does, because prediction
# is the expensive half — can then pick the model up instead of spending
# another hour and a half in EM to arrive at the same numbers.
TRAINED_FILENAME = "splink_trained_{track}.json"

STAGE = 3
STAGE_NAME = "score"

# DuckDB assumes most of the machine's RAM unless told otherwise, and a PSC run
# on the server has about 6 GB to play with (DESIGN.md, D17).
DEFAULT_MEMORY_LIMIT = "6GB"

# Pairs sampled to estimate u. Splink's own default is a million; five million
# steadies the estimate without costing much on a dataset this size.
U_SAMPLE_PAIRS = 5e6

# How many pairs a Python step may hold at once. Bucketing, the vetoes and the
# priority totals are all row-independent, so they run over the pairs a batch at
# a time and the memory they need is this number, not the run's pair count.
# PSC's full snapshot makes something over a hundred million pairs; nothing may
# size itself on that.
PAIR_BATCH_ENV = "PAIR_BATCH_ROWS"
DEFAULT_PAIR_BATCH = 500_000

# The same idea for stage 3b. A model's feature build is the most expensive
# per-pair work in the pipeline — DuckDB joins, a TF-IDF lookup, a LightGBM
# predict — so it gets a knob of its own and a larger default: the batch is a
# fixed cost per call, and too small a one pays it too often.
MODEL_BATCH_ENV = "MODEL_BATCH_PAIRS"
DEFAULT_MODEL_BATCH = 2_000_000

# Asking Splink for every blocking rule in one `predict` builds one comparison
# table the size of their sum, and Splink holds both sides' values for every
# comparison in it until `write_predictions` drops them again. On PSC's full
# snapshot that is 94.7 million comparisons at about 370 bytes each — more than
# 35 GB of DuckDB spill, past the cap, and the run dies (`PSC_HANDOVER.md`,
# section 105). Predicting one rule at a time makes exactly the same pairs in
# six passes of about a sixth the width.
#
# It is not free: each pass re-reads the input table and writes its own file,
# and the files are then concatenated. So it turns on only where it is needed —
# when the blocking budget has priced the track above this many pairs.
# Donations, at a few hundred thousand, keeps the single pass.
ROUTE_BY_ROUTE_ENV = "PREDICT_ROUTE_BY_ROUTE_ABOVE"
DEFAULT_ROUTE_BY_ROUTE_ABOVE = 5_000_000

# Set to "0" to train from scratch even when the run folder already holds a
# model that matches. Nothing but a suspicion of the saved file should need it:
# the fingerprint covers everything the training reads.
REUSE_TRAINED_ENV = "REUSE_TRAINED_MODEL"


def _positive_int(name: str, fallback: int) -> int:
    try:
        value = int(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def pair_batch_rows() -> int:
    """Pairs per Python batch, from ``PAIR_BATCH_ROWS``."""
    return _positive_int(PAIR_BATCH_ENV, DEFAULT_PAIR_BATCH)


def model_batch_pairs() -> int:
    """Pairs per model-scoring batch, from ``MODEL_BATCH_PAIRS``."""
    return _positive_int(MODEL_BATCH_ENV, DEFAULT_MODEL_BATCH)


def route_by_route_above() -> int:
    """Priced pairs above which a track predicts one blocking rule at a time."""
    return _positive_int(ROUTE_BY_ROUTE_ENV, DEFAULT_ROUTE_BY_ROUTE_ABOVE)


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
# Reading only what is needed: unit projections and pair batches
# ---------------------------------------------------------------------------


def overlay_columns(ruleset: dict | None = None, profile=None) -> list[str]:
    """Every unit column ``apply_overlays`` and ``finalise_pairs`` read.

    The import overlay needs the single existing id, the held-group flag needs
    ``held_group_id``, the vetoes need whatever columns they name, and the
    priority totals need the profile's priority columns. That is four or five
    columns out of the sixty-odd a PSC unit carries, and reading the other
    fifty-five is the difference between a projection that fits and a frame that
    does not.
    """
    if profile is None:
        profile = get_profile()
    wanted = ["unit_id", "track", "existing_entity_id", "held_group_id"]
    wanted += list(profile.priority_columns or [])
    wanted += vetoes.columns_needed(ruleset or {})
    return list(dict.fromkeys(wanted))


def budget_columns(config: dict) -> list[str]:
    """The unit columns the blocking budget has to count on, for one track."""
    wanted = {"unit_id"}
    for rule in linkage.blocking_rules(config):
        wanted |= set(linkage.sql_columns(rule["sql"]))
        # A hot-key control re-blocks on columns the rule itself never names,
        # so the projection has to carry them or the control cannot run.
        control = linkage.block_control(rule)
        if control:
            wanted |= {c for c in (control.get("refine_with") or [])
                       if isinstance(c, str)}
    for rule in linkage.em_entries(config):
        wanted |= set(linkage.sql_columns(rule["sql"]))
        control = linkage.block_control(rule)
        if control:
            wanted |= {c for c in (control.get("refine_with") or [])
                       if isinstance(c, str)}
    return sorted(wanted)


def splink_columns(config: dict, ruleset: dict, track: str) -> list[str]:
    """The unit columns one track's Splink model touches, and no others.

    ``_splink_frame`` already narrows what Splink is handed. This narrows what
    is read off the disk in the first place, which is the part that used to cost
    a full-width copy of every unit in the track.
    """
    wanted = set(budget_columns(config))
    for rule in _deterministic_rules(ruleset, track):
        wanted |= set(linkage.sql_columns(rule))
    wanted |= set(_comparison_columns(config))
    wanted.add("track")
    return sorted(wanted)


def read_projection(path, columns) -> pd.DataFrame:
    """*columns* of a parquet file, narrowed to the ones the file has.

    A ruleset may legally name a column the data does not carry, and asking
    parquet for a column that is not there is a hard failure — the same trap
    stage 2's projection hit (`PSC_HANDOVER.md`, section 12). A test fixture
    does it too, with a records file that has no ``existing_entity_id``.
    """
    import pyarrow.parquet as pq

    path = Path(path)
    available = set(pq.ParquetFile(path).schema_arrow.names)
    keep = [c for c in dict.fromkeys(columns) if c in available]
    return pd.read_parquet(path, columns=keep)


#: The units projection is the common case and reads better with its own name.
read_unit_projection = read_projection


def iter_pair_batches(path, batch_rows: int | None = None, columns=None):
    """``pairs.parquet`` (or a predictions file) a bounded batch at a time."""
    import pyarrow.parquet as pq

    rows = batch_rows or pair_batch_rows()
    handle = pq.ParquetFile(path)
    for batch in handle.iter_batches(batch_size=rows, columns=columns):
        yield batch.to_pandas()


class PairWriter:
    """One parquet writer for the pairs file, with a schema fixed by batch one.

    Stage 1 learnt this lesson on the records file and it holds here too: a
    parquet written batch by batch must be written against ONE schema, or a
    batch where every veto reason happens to be null writes a different type
    from the batch before it and the file cannot be read back. The schema comes
    from the first finished batch, with any column arrow could only call "null"
    promoted to text, since those are the overlay's own object columns.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._writer = None
        self.schema = None
        self.rows = 0

    def write(self, frame: pd.DataFrame) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        if not len(frame):
            return
        table = pa.Table.from_pandas(frame, preserve_index=False) \
            if self.schema is None else \
            pa.Table.from_pandas(frame, schema=self.schema, preserve_index=False)
        if self.schema is None:
            fields = [
                pa.field(f.name, pa.string()) if pa.types.is_null(f.type) else f
                for f in table.schema
            ]
            self.schema = pa.schema(fields)
            table = table.cast(self.schema)
            self._writer = pq.ParquetWriter(str(self.path), self.schema)
        self._writer.write_table(table)
        self.rows += len(frame)

    def close(self, empty: pd.DataFrame | None = None) -> int:
        """Finish the file. *empty* is written when no batch ever was."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        if self._writer is None:
            frame = empty if empty is not None else pd.DataFrame(columns=PAIR_HEAD)
            table = pa.Table.from_pandas(frame, preserve_index=False)
            pq.write_table(table, str(self.path))
        else:
            self._writer.close()
            self._writer = None
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._writer is not None:
            self._writer.close()
            self._writer = None


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


def count_rule_pairs(rows: pd.DataFrame, rule: dict, controlled_sql: str,
                     track: str, db_api, temp_dir=None) -> int:
    """The pairs one rule makes, counted by whichever counter can count it.

    Splink's own counter is used wherever it can be, because it is the thing
    that will do the blocking. **It cannot count a refined rule.** The control
    generates ``<the rule> AND (<key> NOT IN (...) OR <refine columns agree>)``,
    and an ``OR`` stops Splink's blocking analyser recognising the equi-join at
    all: it falls back to the cartesian bound and refuses. Measured on the full
    snapshot's 593,640 organisation units — ``on_oversize: "drop"`` counts fine
    and agrees with ``price_rule`` to the pair (25,558 both ways), and
    ``on_oversize: "refine"`` raises ``exceeded max_rows_limit`` with a bound of
    3.524e+11, which is 593,640 squared over two.

    ``price_rule`` prices the control natively, in DuckDB group arithmetic. It
    was checked against materialised pairs in sixteen cases on the sample
    (``PSC_HANDOVER.md`` section 19) and against Splink's own count here, so it
    is used for a refined rule and Splink's counter for everything else.
    """
    control = linkage.block_control(rule)
    if control is None or control.get("on_oversize") != "refine":
        try:
            return count_blocking_pairs(rows, controlled_sql, db_api)
        except (TypeError, ValueError):
            # Splink refused. It does that for a rule whose pre-filter count is
            # over its own `max_rows_limit` as well as for a refined one: PSC's
            # pb5 blocks on the forename sound and the date of birth and then
            # filters on `l.surname_metaphone <> r.surname_metaphone`, and at
            # 11.2 million person units the block before that filter is over a
            # billion pairs. Price it here instead of losing the run.
            pass
    # `price_rule` applies the control itself, so it is given the rule as it was
    # written. Handing it the generated SQL would apply the control twice.
    con = duckdb_conn.connect(temp_dir)
    try:
        return int(linkage.price_rule(con, rows, rule, track)["pairs"])
    finally:
        con.close()


def count_uncontrolled_pairs(rows: pd.DataFrame, rule: dict, track: str,
                             temp_dir=None) -> int:
    """What the rule would cost with no control, for the report to show beside.

    Always ``price_rule``: it is exact for every shipped rule but pb6, it never
    refuses, and this figure is shown rather than acted on.
    """
    con = duckdb_conn.connect(temp_dir)
    try:
        return int(linkage.price_rule(
            con, rows, {k: v for k, v in rule.items()
                        if k not in linkage.BLOCK_CONTROL_KEYS},
            track)["pairs_before"])
    finally:
        con.close()


def control_cache_key(rows: pd.DataFrame, rule: dict, track: str) -> tuple:
    """What makes two calls for the same control the same question.

    The track, the rule, the control, how many rows were handed over, and the
    dtype of every column the control reads. The dtypes belong in the key
    because the generated SQL inlines the oversized key VALUES as text, and the
    text a column yields depends on its type — see ``cast_numeric_columns``.
    Two frames that disagree there must not share an answer.
    """
    control = linkage.block_control(rule) or {}
    columns = sorted(set(linkage.sql_columns(rule.get("sql") or ""))
                     | {c for c in (control.get("refine_with") or [])
                        if isinstance(c, str)})
    dtypes = tuple((c, str(rows[c].dtype)) for c in columns if c in rows.columns)
    return (track, rule.get("id") or "", rule.get("sql") or "",
            json.dumps(control, sort_keys=True), int(len(rows)), dtypes)


def controlled_rules(rows: pd.DataFrame, rules: list[dict], track: str,
                     temp_dir=None, progress_callback=None,
                     cache: dict | None = None) -> list[dict]:
    """The blocking rules with their hot-key controls built into the SQL.

    A rule with no ``max_block_size`` comes back exactly as it went in, which is
    what keeps donations and every route that has no control unchanged. A rule
    with one comes back with the control generated into it, so the pairs inside
    a hot block are never made rather than made and then thrown away
    (`docs/LINKAGE.md`).

    The oversized key values are measured from *rows*, so this has to be handed
    the same units Splink is about to score.

    *cache* is a plain dict a caller may keep for the length of one stage. The
    same control is measured twice in a run — once by the blocking budget,
    which has to price what will actually happen, and once by ``train_track``,
    which has to build the SQL Splink will use — and the measurement is a GROUP
    BY over every unit in the track: about 50 seconds a rule on PSC's 11.2
    million person units, or seven minutes of a run spent asking a question
    whose answer cannot have changed.
    """
    if not any(linkage.block_control(rule) for rule in rules):
        return rules
    con = None
    try:
        out = []
        for rule in rules:
            if linkage.block_control(rule) is None:
                out.append(rule)
                continue
            key = control_cache_key(rows, rule, track) if cache is not None else None
            cached = key is not None and key in cache
            if cached:
                sql = cache[key]
            else:
                if con is None:
                    con = duckdb_conn.connect(temp_dir)
                sql = linkage.controlled_sql(con, rows, rule, track)
                if key is not None:
                    cache[key] = sql
            out.append({**rule, "sql": sql})
            # An EM entry written as plain SQL has no id of its own.
            _step(f"  {track}/{rule.get('id') or 'em'}: "
                  f"{linkage.control_description(rule)}"
                  f"{' (already measured)' if cached else ''}", progress_callback)
        return out
    finally:
        if con is not None:
            con.close()


def _budget_rows(units, track: str, config: dict) -> pd.DataFrame:
    """One track's units, narrowed to the columns its rules block on.

    *units* is the frame or the path to ``units.parquet``. A path is read one
    track's projection at a time, so pricing sixteen million units costs the
    half-dozen columns the rules name rather than all sixty.

    The numeric-difference columns are cast here for the same reason
    ``_splink_frame`` casts them: the budget has to price the rule against the
    values the rule will meet, and a control measured on ``1985`` does not
    match a frame that spells it ``1985.0``. Casting both frames the same way
    also lets one measurement serve both (``ControlCache``).
    """
    columns = budget_columns(config)
    if isinstance(units, (str, Path)):
        frame = read_unit_projection(units, columns + ["track"])
        return cast_numeric_columns(units_module.track_units(frame, track), config)
    rows = units_module.track_units(units, track)
    keep = [c for c in columns if c in rows.columns]
    return cast_numeric_columns((rows[keep] if keep else rows).copy(), config)


def blocking_budget_report(
    units,
    settings: dict,
    db_api,
    progress_callback=None,
    cache: dict | None = None,
) -> tuple[dict, "BlockingBudgetError | None"]:
    """``(report, failure)``: pairs per blocking rule, per track, against its budget.

    The failure is the first track over budget. It is returned rather than
    raised so the caller can write the report first — the screen needs to name
    the rule that blew the budget, and a run that dies before writing anything
    cannot.
    """
    report = {"memory_limit": memory_limit(), "tracks": {}}
    failure: BlockingBudgetError | None = None
    temp = Path(units).parent / "duckdb_tmp" if isinstance(units, (str, Path)) else None

    for track in linkage.TRACK_KEYS:
        config = linkage.track_settings(settings, track)
        rows = _budget_rows(units, track, config)
        rules = linkage.blocking_rules(config)
        budget = linkage.max_pairs(config)

        # The budget is priced on what the rules will ACTUALLY do, so a rule
        # with a hot-key control is counted after the control, not before it.
        # A budget that prices the uncontrolled rule is not a budget.
        priced = controlled_rules(rows, rules, track, temp_dir=temp,
                                  progress_callback=progress_callback,
                                  cache=cache) \
            if len(rows) > 1 else rules
        counted = []
        for rule, controlled in zip(rules, priced):
            pairs = count_rule_pairs(rows, rule, controlled["sql"], track,
                                     db_api, temp) if len(rows) > 1 else 0
            entry = {
                "id": rule["id"],
                "description": rule["description"],
                "sql": rule["sql"],
                "pairs": pairs,
            }
            if linkage.block_control(rule) is not None:
                entry["control"] = linkage.block_control(rule)
                entry["control_description"] = linkage.control_description(rule)
                # The generated SQL inlines every hot key, which at PSC scale is
                # megabytes. The report is served to a browser, so it carries
                # the size rather than the text.
                entry["sql_after_control_bytes"] = len(controlled["sql"])
                if len(controlled["sql"]) <= MAX_REPORTED_SQL:
                    entry["sql_after_control"] = controlled["sql"]
                if len(rows) > 1:
                    entry["pairs_before_control"] = count_uncontrolled_pairs(
                        rows, rule, track, temp)
            counted.append(entry)
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
        em_entries = linkage.em_entries(config)
        # An EM rule may carry a hot-key control too, and it is priced after it
        # for the same reason a prediction rule is: the budget has to price
        # what will actually run. PSC's person em1 is 582,535,879 training
        # pairs uncontrolled and 43,054,672 with `drop` over 60.
        em_priced = controlled_rules(rows, em_entries, track, temp_dir=temp,
                                     progress_callback=progress_callback,
                                     cache=cache) \
            if len(rows) > 1 else em_entries
        for index, (entry, controlled) in enumerate(zip(em_entries, em_priced), start=1):
            sql = controlled["sql"]
            pairs = count_rule_pairs(rows, entry, sql, track, db_api,
                                     temp) if len(rows) > 1 else 0
            em_counted.append({
                "id": f"em{index}",
                "description": "EM training rule",
                "sql": entry["sql"],
                "sql_after_control_bytes": len(sql),
                "pairs": pairs,
                **({"control": linkage.block_control(entry),
                    "control_description": linkage.control_description(entry),
                    "sql_before_control": entry["sql"]}
                   if linkage.block_control(entry) is not None else {}),
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
    for rule in linkage.em_entries(config):
        wanted.extend(sorted(linkage.sql_columns(rule["sql"])))
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
    return cast_numeric_columns(frame, config)


def cast_numeric_columns(frame: pd.DataFrame, config: dict) -> pd.DataFrame:
    """The numeric-difference columns as numbers, in place, and the frame back.

    ``custom.NumericDifferenceAtThresholds`` compares numbers and the cleaning
    engine writes text: ``dob_year_clean`` is a string of digits. The cast
    happens in the frame Splink sees, so ``units.parquet`` keeps what was filed
    and the review screen and the vetoes still read it (docs/LINKAGE.md).

    **Every frame a hot-key control is measured on has to have had this done to
    it too**, which is why it is a function rather than four lines inside
    ``_splink_frame``. The control inlines its oversized key values into the
    blocking SQL as text, and the text a column gives depends on its type:
    ``dob_year_clean`` held as a string spells its keys ``1985`` and the same
    column held as a float spells them ``1985.0``. A control measured on one
    and applied to the other matches nothing, so its ``NOT IN`` is always true
    and the control silently does nothing at all. That is not a small mistake —
    on the PSC person track it is the difference between 94.7 million
    comparisons and 1.46 billion (``PSC_HANDOVER.md``, section 106).
    """
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
    priced_pairs: int | None = None,
    control_cache: dict | None = None,
):
    """Train a Splink model on one track's units and predict.

    Returns ``(linker, predictions_path, pairs, routes)``. *priced_pairs* is
    what the blocking budget counted for this track; above
    ``PREDICT_ROUTE_BY_ROUTE_ABOVE`` the prediction runs one blocking rule at a
    time. *routes* describes what each blocking rule produced either way.
    """
    from splink import Linker, SettingsCreator

    prior = settings.get("probability_two_random_records_match")
    deterministic = _deterministic_rules(ruleset, track) if prior is None else []
    frame = _splink_frame(rows, config, deterministic)
    candidate, _review, _high = linkage.thresholds(settings)

    # The control is measured on `frame`, not on `rows`, because `frame` is
    # what Splink will apply the generated SQL to and the two do not spell
    # their key values the same way (`cast_numeric_columns`). Measuring it on
    # `rows` left every PSC route that blocks on `dob_year_clean` — four of the
    # six — with a control that matched nothing and did nothing.
    temp_dir = Path(run_dir) / "duckdb_tmp"
    blocking = controlled_rules(frame, linkage.blocking_rules(config), track,
                                temp_dir=temp_dir,
                                progress_callback=progress_callback,
                                cache=control_cache)
    em_rules = controlled_rules(frame, linkage.em_entries(config), track,
                                temp_dir=temp_dir,
                                progress_callback=progress_callback,
                                cache=control_cache)

    kwargs = dict(
        link_type="dedupe_only",
        unique_id_column_name="unit_id",
        comparisons=[linkage.build_comparison(c) for c in linkage.comparisons(config)],
        blocking_rules_to_generate_predictions=[
            linkage.build_blocking_rule(r["sql"]) for r in blocking
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

    fingerprint = training_fingerprint(config, settings, ruleset, track,
                                       blocking, em_rules, len(frame))
    model_path = Path(run_dir) / MODEL_FILENAME.format(track=track)
    trained_path = Path(run_dir) / TRAINED_FILENAME.format(track=track)
    db_api = _db_api(temp_dir)

    if saved_training_matches(trained_path, model_path, fingerprint):
        _step(f"  Reusing the model this run folder already holds "
              f"({fingerprint[:12]}); nothing about it has changed.",
              progress_callback)
        linker = Linker(frame, str(model_path), db_api=db_api)
        return _predict_track(linker, candidate, settings, config, track, run_dir,
                              kwargs, priced_pairs, progress_callback)

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

    # Labels never train Splink (D9): m comes from EM, u from sampling. A
    # training rule may carry a hot-key control, and it is applied here for the
    # same reason it is applied to a prediction rule: the pairs inside a hot
    # block are never made rather than made and thrown away.
    for rule in em_rules:
        rule = rule["sql"]
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

    # Saved before predicting, not after. Prediction is the expensive half and
    # the half that fails, and a model that has to be trained again from the
    # top on every attempt makes each attempt cost an hour and a half more
    # than it needs to.
    linker.misc.save_model_to_json(str(model_path), overwrite=True)
    trained_path.write_text(json.dumps({
        "fingerprint": fingerprint,
        "track": track,
        "model": model_path.name,
        "units": int(len(frame)),
        "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2), encoding="utf-8")

    return _predict_track(linker, candidate, settings, config, track, run_dir,
                          kwargs, priced_pairs, progress_callback)


def _predict_track(linker, candidate, settings, config, track, run_dir, kwargs,
                   priced_pairs, progress_callback):
    """Predict one track, by whichever path its priced pair total calls for."""
    path = Path(run_dir) / PREDICTIONS_FILENAME.format(track=track)
    n_rules = len(kwargs["blocking_rules_to_generate_predictions"])
    limit = route_by_route_above()
    by_route = priced_pairs is not None and priced_pairs > limit and n_rules > 1
    _step(f"  Predicting down to {candidate}, "
          + (f"one blocking rule at a time ({n_rules} of them)" if by_route
             else f"all {n_rules} blocking rule(s) in one pass")
          + (f" — {priced_pairs:,} priced pairs against a limit of {limit:,}"
             if priced_pairs is not None else ""),
          progress_callback)
    t0 = time.time()
    predict = predict_route_by_route if by_route else predict_one_pass
    n_pairs, routes = predict(linker, candidate, path,
                              temp_dir=Path(run_dir) / "duckdb_tmp",
                              progress_callback=progress_callback)
    _step(f"  {n_pairs:,} candidate pairs ({time.time() - t0:.1f}s)",
          progress_callback)
    return linker, path, n_pairs, routes


def training_fingerprint(config: dict, settings: dict, ruleset: dict, track: str,
                         blocking: list[dict], em_rules: list[dict],
                         n_rows: int) -> str:
    """Everything that decides what a trained model comes out as, as one hash.

    The comparisons, the prior and how it is estimated, the EM settings and the
    seed, how many units there are, and the blocking and training SQL **after**
    their hot-key controls — which is the part that depends on the data, since
    the control inlines the oversized keys it found.
    """
    payload = json.dumps({
        "track": track,
        "comparisons": linkage.comparisons(config),
        "prior": settings.get("probability_two_random_records_match"),
        "deterministic_recall": settings.get("deterministic_recall"),
        "deterministic_rules": _deterministic_rules(ruleset, track),
        "em_iterations": settings.get("em_iterations",
                                      linkage.DEFAULT_EM_ITERATIONS),
        "random_seed": settings.get("random_seed"),
        "u_sample_pairs": U_SAMPLE_PAIRS,
        "units": int(n_rows),
        "blocking": [r["sql"] for r in blocking],
        "em": [r["sql"] for r in em_rules],
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def saved_training_matches(trained_path: Path, model_path: Path,
                           fingerprint: str) -> bool:
    """Whether this run folder already holds the model this training would give."""
    if os.environ.get(REUSE_TRAINED_ENV, "1") == "0":
        return False
    if not (trained_path.is_file() and model_path.is_file()):
        return False
    try:
        saved = json.loads(trained_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved.get("fingerprint") == fingerprint


#: Everything the pairs file keeps out of a prediction, besides the gammas.
PREDICTION_COLUMNS = ("unit_id_l", "unit_id_r", "match_probability", "match_weight")


def prediction_columns(names, extra=()) -> list[str]:
    """The prediction columns worth carrying, out of everything Splink emits.

    Splink is asked to retain the matching columns and the intermediate
    calculations, because the per-pair explanation needs the gammas. That also
    makes it hand back both sides' **values** for every compared column, plus a
    Bayes factor and a term-frequency adjustment each. On the PSC sample that is
    a frame of about 3.2 KB per pair, and pulling 1.07 million of them into
    pandas was the single biggest thing stage 3 did: 3.3 GB of a 7.4 GB peak.
    ``finalise_pairs`` threw every one of those columns away again, because the
    units file already holds the values.

    So they are dropped in SQL, on the way out of DuckDB, and never become
    Python objects at all.
    """
    keep = [c for c in extra if c in names]
    keep += [c for c in PREDICTION_COLUMNS if c in names]
    keep += sorted(c for c in names if c.startswith("gamma_"))
    return keep


def release_linker(linker) -> None:
    """Close a linker's DuckDB connection and let the memory go.

    Splink has no ``close``. The tables it made live in its own in-memory
    database, and that database is only freed when the connection is closed and
    the Python objects holding it are collected — which by default happens some
    time after the next track has already asked for its own gigabyte.
    """
    import gc

    try:
        linker._db_api._con.close()
    except Exception:  # noqa: BLE001 — a failure to tidy up must not fail a run
        pass
    gc.collect()


def write_predictions(linker, predictions, path, extra=()) -> int:
    """Splink's prediction table straight to parquet, without touching pandas.

    Splink predicts through DuckDB and already has the answer in a table, so the
    honest thing to do with it is copy that table to a file. ``as_pandas_dataframe``
    materialises every row in this process instead, which is what stops the
    stage scaling past a few million pairs.
    """
    con = linker._db_api._con
    table = predictions.physical_name
    names = [row[0] for row in con.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
    columns = ", ".join(f'"{c}"' for c in prediction_columns(names, extra))
    con.execute(
        f'COPY (SELECT {columns} FROM "{table}") TO {_path_literal(path)} '
        "(FORMAT PARQUET)"
    )
    return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _path_literal(path) -> str:
    """A filesystem path as a SQL string literal."""
    return "'" + str(path).replace("'", "''") + "'"


def predict_one_pass(linker, candidate: float, path, extra=(), temp_dir=None,
                     progress_callback=None) -> tuple[int, list[dict]]:
    """Every blocking rule in one ``predict``. ``(pairs, routes)``.

    This is the path every run took before PSC's full snapshot, and the one
    every run under ``PREDICT_ROUTE_BY_ROUTE_ABOVE`` still takes.
    """
    predictions = linker.inference.predict(threshold_match_probability=candidate)
    con = linker._db_api._con
    table = predictions.physical_name
    routes = [
        {"match_key": int(key), "pairs": int(pairs)}
        for key, pairs in con.execute(
            f'SELECT match_key, count(*) FROM "{table}" GROUP BY 1 ORDER BY 1'
        ).fetchall()
    ]
    n_pairs = write_predictions(linker, predictions, path, extra)
    return n_pairs, routes


def predict_route_by_route(linker, candidate: float, path, extra=(), temp_dir=None,
                           progress_callback=None) -> tuple[int, list[dict]]:
    """One blocking rule per ``predict``, then the files joined. ``(pairs, routes)``.

    **This makes the same pairs as ``predict_one_pass``, with the same scores,
    the same gammas and the same route attribution**, and the reason is that
    the rule objects are the trained model's own, untouched.

    Splink deduplicates across blocking rules while it blocks: rule *n* carries
    ``AND NOT (rule 0 OR rule 1 OR ... OR rule n-1)``, so a pair is produced by
    the first rule that matches it and by no other. Both that clause and the
    ``match_key`` column come from one place —
    ``BlockingRule.preceding_rules``, which ``match_key`` is simply the length
    of. Handing ``predict`` a list of one *rule object* rather than a list of
    one *rule* therefore changes nothing about what that rule does: its
    preceding rules are still the five in front of it, its SQL is
    byte-for-byte the branch of the ``UNION ALL`` it would have been, and its
    ``match_key`` is still its own index. Splink itself does this, in
    ``estimate_u.py``.

    What does change is how much is alive at once. The comparison table is one
    rule's pairs rather than six rules' pairs, and it is copied out and dropped
    before the next rule starts, so the DuckDB spill is the largest single
    route instead of the sum of them.
    """
    settings = linker._settings_obj
    rules = list(settings._blocking_rules_to_generate_predictions)
    path = Path(path)
    parts: list[Path] = []
    routes: list[dict] = []
    try:
        for rule in rules:
            key = rule.match_key
            part = path.with_name(f"{path.stem}.route{key}{path.suffix}")
            settings._blocking_rules_to_generate_predictions = [rule]
            t0 = time.time()
            predictions = linker.inference.predict(
                threshold_match_probability=candidate)
            pairs = write_predictions(linker, predictions, part, extra)
            # The prediction table is the wide one — both sides' values for
            # every comparison. Dropping it here is what hands the spill back
            # before the next route asks for its own.
            try:
                predictions.drop_table_from_database_and_remove_from_cache()
            except Exception:  # noqa: BLE001 — tidying up must not lose a run
                pass
            parts.append(part)
            routes.append({"match_key": int(key), "pairs": int(pairs),
                           "seconds": round(time.time() - t0, 1)})
            _step(f"    route {key}: {pairs:,} pairs "
                  f"({routes[-1]['seconds']:.1f}s)", progress_callback)
    finally:
        settings._blocking_rules_to_generate_predictions = rules

    total = concat_predictions(parts, path, temp_dir)
    for part in parts:
        part.unlink(missing_ok=True)
    return total, routes


def concat_predictions(parts: list[Path], path: Path, temp_dir=None) -> int:
    """The per-route prediction files as one file, and its row count.

    ``read_parquet`` over a list of files refuses a schema mismatch, which is
    exactly the check worth making here: every route must have produced the
    same columns, or the pairs file would have a shape that depends on which
    route a pair came from.
    """
    if not parts:
        return 0
    con = duckdb_conn.connect(temp_dir)
    try:
        files = ", ".join(_path_literal(part) for part in parts)
        con.execute(f"COPY (SELECT * FROM read_parquet([{files}])) "
                    f"TO {_path_literal(path)} (FORMAT PARQUET)")
        return int(con.execute(
            f"SELECT count(*) FROM read_parquet({_path_literal(path)})"
        ).fetchone()[0])
    finally:
        con.close()


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


def union_prediction_columns(paths) -> list[str]:
    """The union of the tracks' prediction columns, in first-appearance order.

    This is exactly what ``pd.concat`` used to leave behind: every track's
    gammas side by side, with a null wherever a track has no such comparison.
    Working it out up front is what lets the overlays stream — a parquet written
    batch by batch has to know its columns before the first batch, and the
    person track's gammas are not the organisation track's.
    """
    import pyarrow.parquet as pq

    out: list[str] = []
    for path in paths:
        for name in pq.ParquetFile(path).schema_arrow.names:
            if name not in out:
                out.append(name)
    return out


def overlay_predictions(
    prediction_paths: dict,
    units: pd.DataFrame,
    review: float,
    high: float,
    out_path,
    ruleset: dict | None = None,
    model_lines: dict | None = None,
    batch_rows: int | None = None,
    progress_callback=None,
) -> int:
    """Bucket, veto and finalise every prediction, a batch at a time.

    *prediction_paths* is ``{track: parquet path}`` in track order. Each file is
    read in batches, laid out on the union of every track's columns so the
    output has one schema, overlaid, and appended to *out_path*. Nothing here
    holds more than one batch, so the memory this costs is ``PAIR_BATCH_ROWS``
    and not the run's pair count.
    """
    union = union_prediction_columns(prediction_paths.values())
    # A run where every track was skipped still writes a pairs file, and it has
    # to have the columns the readers expect rather than no columns at all.
    for column in (*PREDICTION_COLUMNS, "track"):
        if column not in union:
            union.append(column)
    rows = batch_rows or pair_batch_rows()

    writer = PairWriter(out_path)
    for track, path in prediction_paths.items():
        for batch in iter_pair_batches(path, rows):
            batch = batch.reindex(columns=union)
            batch["track"] = track
            frame = apply_overlays(batch, units, review, high,
                                   model_lines=model_lines, ruleset=ruleset)
            writer.write(finalise_pairs(frame, units))
    empty = finalise_pairs(
        apply_overlays(pd.DataFrame(columns=union), units, review, high,
                       model_lines=model_lines, ruleset=ruleset),
        units,
    )
    return writer.close(empty=empty)


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


#: Bins in the score-distribution chart. The same 50 ``score_eval`` uses.
HISTOGRAM_BINS = 50


def render_model_charts(linker, track: str, diag_dir: Path,
                        progress_callback=None) -> None:
    """Splink's match-weight and m/u charts. They read the model, not the pairs."""
    diag_dir.mkdir(parents=True, exist_ok=True)
    for name, builder in (
        ("match_weights", lambda l: l.visualisations.match_weights_chart()),
        ("m_u_parameters", lambda l: l.visualisations.m_u_parameters_chart()),
    ):
        try:
            builder(linker).save(str(diag_dir / f"{name}_{track}.html"))
        except Exception as exc:
            _step(f"  WARNING: no {name} chart for {track}: {exc}", progress_callback)


def histogram_counts(pairs_path, track: str, temp_dir=None) -> pd.DataFrame:
    """``bucket, bin, pairs`` for one track, counted in DuckDB.

    Fifty rows per bucket, whatever the run's size.
    """
    con = duckdb_conn.connect(temp_dir)
    try:
        return con.execute(f"""
            SELECT bucket,
                   least({HISTOGRAM_BINS - 1},
                         greatest(0, CAST(floor(match_probability * {HISTOGRAM_BINS})
                                          AS INTEGER))) AS bin,
                   count(*) AS pairs
            FROM read_parquet('{pairs_path}')
            WHERE track = ? AND match_probability IS NOT NULL
            GROUP BY ALL
            ORDER BY bucket, bin
        """, [track]).df()
    finally:
        con.close()


def render_score_histogram(pairs_path, track: str, diag_dir: Path, review: float,
                           high: float, temp_dir=None, progress_callback=None) -> None:
    """The score-distribution chart, drawn from counted bins rather than rows.

    This used to hand Altair one row per pair. Altair embeds its data in the
    page, so the PSC sample's person chart came out as a **68 MB HTML file** for
    1.07 million pairs, and the full snapshot would have written gigabytes —
    for a picture with fifty bars in it. The bins are counted in DuckDB and the
    chart is drawn from fifty rows per bucket. It shows the same thing and the
    file is a few kilobytes.
    """
    diag_dir.mkdir(parents=True, exist_ok=True)
    try:
        counts = histogram_counts(pairs_path, track, temp_dir)
        _score_histogram(counts, track, review, high).save(
            str(diag_dir / f"score_distribution_{track}.html")
        )
    except Exception as exc:  # noqa: BLE001 — a chart must not lose a run
        _step(f"  WARNING: no score histogram for {track}: {exc}", progress_callback)


def _score_histogram(counts: pd.DataFrame, track: str, review: float, high: float):
    import altair as alt

    width = 1.0 / HISTOGRAM_BINS
    frame = counts.copy()
    frame["start"] = frame["bin"].astype("float64") * width
    frame["end"] = frame["start"] + width
    total = int(frame["pairs"].sum()) if len(frame) else 0
    scale = alt.Scale(domain=["reject", "review", "accept"],
                      range=["#bbbbbb", "#fd7e14", "#28a745"])
    bars = alt.Chart(frame).mark_bar(stroke="white", strokeWidth=0.5).encode(
        x=alt.X("start:Q", title="Splink match probability",
                scale=alt.Scale(domain=[0, 1])),
        x2=alt.X2("end:Q"),
        y=alt.Y("pairs:Q", title="Pairs in this score range"),
        color=alt.Color("bucket:N", scale=scale,
                        legend=alt.Legend(title="Bucket", orient="bottom")),
    ).properties(
        width=820, height=340,
        title={"text": f"{track}: scored pairs by match probability",
               "subtitle": [f"{total:,} pairs down to the candidate floor. "
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


def label_outcomes_from_files(labels, members_path, groups: pd.DataFrame) -> dict:
    """``label_outcomes``, reading the members only when there are labels.

    A run with no labels — every PSC run, for now — never touches the members
    file, which at sixteen million rows is worth not touching.
    """
    if labels is None or not len(labels):
        return label_outcomes(labels, pd.DataFrame(columns=["record_id", "unit_id"]),
                              groups)
    return label_outcomes(labels, pd.read_parquet(members_path), groups)


def forced_pairs(applied: pd.DataFrame, pairs_path, units_path) -> pd.DataFrame:
    """Labelled pairs the scorer never produced, found without reading the file.

    The labels are few and the pairs file is not, so the question is asked the
    other way round: which of these handful of pairs does the file already hold?
    One DuckDB anti-join over two columns answers it.
    """
    if applied is None or not len(applied):
        return pd.DataFrame(columns=["unit_id_l", "unit_id_r", "match_probability",
                                     "match_weight", "track"])
    wanted = label_overlay.decisions(applied)[["unit_id_l", "unit_id_r"]].copy()
    wanted["unit_id_l"] = wanted["unit_id_l"].astype(str)
    wanted["unit_id_r"] = wanted["unit_id_r"].astype(str)
    con = duckdb_conn.connect(Path(pairs_path).parent / "duckdb_tmp")
    try:
        con.register("wanted", wanted)
        missing = con.execute(f"""
            SELECT w.unit_id_l, w.unit_id_r FROM wanted w
            ANTI JOIN read_parquet('{pairs_path}') p
              ON CAST(p.unit_id_l AS VARCHAR) = w.unit_id_l
             AND CAST(p.unit_id_r AS VARCHAR) = w.unit_id_r
        """).df()
    finally:
        con.close()
    if not len(missing):
        return missing.assign(match_probability=np.nan, match_weight=np.nan,
                              track=None)
    tracks = read_unit_projection(units_path, ("unit_id", "track"))
    lookup = pd.Series(tracks["track"].to_numpy(),
                       index=tracks["unit_id"].astype(str).to_numpy())
    lookup = lookup[~lookup.index.duplicated(keep="first")]
    missing["match_probability"] = np.nan
    missing["match_weight"] = np.nan
    missing["track"] = missing["unit_id_l"].map(lookup)
    return missing.reset_index(drop=True)


def apply_active_models(
    run_dir,
    pairs_path,
    units_path,
    members_path,
    events_path,
    review: float,
    high: float,
    progress_callback=None,
    ruleset: dict | None = None,
    overlay_units: pd.DataFrame | None = None,
) -> dict:
    """Stage 3b: score the pairs with each track's active model and re-bucket.

    The pairs are read, scored and written a batch at a time, so a model run
    costs ``MODEL_BATCH_PAIRS`` rows of features and not the run's pair count.
    Returns the state that was written. With no active model nothing changes and
    the run's model state is cleared, so a run that was scored by a model and
    then re-run without one does not keep claiming it.
    """
    models = stage_3b_model.active_models()
    if not models:
        stage_3b_model.clear_state(run_dir)
        return stage_3b_model.read_state(run_dir)

    _step("Scoring with the active model(s): "
          + ", ".join(f"{t} v{m.version}" for t, m in models.items()) + "...",
          progress_callback)
    # The feature builders read whatever unit columns they please, so this is
    # the one place stage 3 still needs the units frame. It is read once.
    units = pd.read_parquet(units_path)
    if overlay_units is None:
        overlay_units = units
    events = pd.read_parquet(events_path) if Path(events_path).is_file() else None
    members = pd.read_parquet(members_path) if Path(members_path).is_file() else None
    unit_events = events_by_unit(events, members)
    del events, members

    # Fitted once, over every unit, and written into the run folder, so batched
    # scoring, a later apply-model and a one-pair explanation all read the same
    # vocabulary and IDF (`app/model/corpus.py`).
    fitted = {track: corpus_lib.for_run(run_dir, units, track, get_profile())
              for track in models}

    scored_path = Path(pairs_path).with_suffix(".scored.parquet")
    used, review_before, review_after, distinct = _score_pairs_file(
        pairs_path, scored_path, units, overlay_units, models, unit_events,
        review, high, ruleset, fitted,
    )
    warning = None
    lines = stage_3b_model.deciding_lines(used)
    if lines:
        warning = stage_3b_model.collapse_reason(review_before, review_after, distinct)
    if used and (warning is None):
        scored_path.replace(Path(pairs_path))
        if lines:
            _step(f"  buckets now follow the model ({review_after:,} in review)",
                  progress_callback)
        else:
            _step("  the model is not graded, so it re-orders the queue and "
                  "decides nothing", progress_callback)
    else:
        scored_path.unlink(missing_ok=True)
        if warning is not None:
            _step(f"  WARNING: the model was not applied — {warning}. "
                  "Buckets stay on the Splink score.", progress_callback)
            if progress_callback:
                progress_callback("warning", {"stage": 3, "message": warning})

    return stage_3b_model.write_state(
        run_dir,
        {t: m for t, m in used.items()} if warning is None else
        {t: stage_3b_model.TrackModel(t, m.version, False, None, None)
         for t, m in used.items()},
        warning=warning, applied=bool(used),
    )


def _score_pairs_file(pairs_path, out_path, units, overlay_units, models,
                      unit_events, review, high, ruleset, corpus=None):
    """One batched pass: model scores on, buckets redone, written to *out_path*.

    Both the scored-and-rebucketed file and the numbers the collapse guard needs
    come out of the same pass, because reading a hundred million pairs twice to
    answer "did the review band survive" is not a thing worth doing. The guard
    can still refuse the result afterwards — the file is simply not swapped in.
    """
    lines_by_track: dict = {}
    used: dict = {}
    review_before = review_after = 0
    values: set = set()

    writer = PairWriter(out_path)
    empty = None
    for batch in iter_pair_batches(pairs_path, model_batch_pairs()):
        if not len(batch):
            continue
        review_before += int((batch["bucket"] == "review").sum())
        scored, batch_used = stage_3b_model.score_pairs(
            batch, units, models, events=unit_events, profile=get_profile(),
            corpus=corpus,
        )
        used.update(batch_used)
        lines_by_track = stage_3b_model.deciding_lines(used)
        rebucketed = finalise_pairs(
            apply_overlays(_strip_overlays(scored), overlay_units, review, high,
                           model_lines=lines_by_track, ruleset=ruleset),
            overlay_units,
        )
        review_after += int((rebucketed["bucket"] == "review").sum())
        numbers = pd.to_numeric(rebucketed.get(MODEL_SCORE_COLUMN),
                                errors="coerce").dropna()
        if len(numbers) and len(values) < stage_3b_model.MIN_DISTINCT_SCORES * 4:
            values.update(numbers.round(6).unique().tolist())
        writer.write(rebucketed)
        if empty is None:
            empty = rebucketed.iloc[0:0]
    writer.close(empty=empty)
    return used, review_before, review_after, len(values)


def _strip_overlays(pairs: pd.DataFrame) -> pd.DataFrame:
    """The pairs without the columns ``apply_overlays`` works out again.

    The veto columns go with them, so a re-bucket, an apply-model and a
    revert-model all run the vetoes again rather than carrying stale ones.
    """
    dropped = ("score_bucket", "bucket", "decided_by", "import_disagrees",
               "held_group_id", *vetoes.VETO_COLUMNS)
    return pairs[[c for c in pairs.columns if c not in dropped]]


#: What ``pair_counts`` works out, and what each one counts, so a missing
#: column reads as zero rather than failing an old run's file.
_PAIR_COUNT_SQL = {
    "pairs_scored": "count(*)",
    "pairs_decided_by_import": "count(*) FILTER (WHERE decided_by = 'import')",
    "pairs_import_disagrees":
        "count(*) FILTER (WHERE coalesce(import_disagrees, false))",
    "pairs_vetoed": "count(*) FILTER (WHERE vetoed_by IS NOT NULL)",
    "pairs_vetoed_from_accept":
        "count(*) FILTER (WHERE vetoed_by IS NOT NULL AND (CASE WHEN "
        "decided_by = 'import' OR coalesce(veto_conflicts_import, false) "
        "THEN 'accept' ELSE score_bucket END) = 'accept')",
    "veto_conflicts_import":
        "count(*) FILTER (WHERE coalesce(veto_conflicts_import, false))",
}

#: Which columns each count needs. A file without them counts zero.
_PAIR_COUNT_NEEDS = {
    "pairs_scored": (),
    "pairs_decided_by_import": ("decided_by",),
    "pairs_import_disagrees": ("import_disagrees",),
    "pairs_vetoed": ("vetoed_by",),
    "pairs_vetoed_from_accept": ("vetoed_by", "decided_by", "score_bucket",
                                 "veto_conflicts_import"),
    "veto_conflicts_import": ("veto_conflicts_import",),
}


def pair_counts(pairs_path, temp_dir=None) -> dict:
    """The run counts that are sums over the pairs file, counted in DuckDB.

    Every one of these used to be a boolean array the length of the pairs frame.
    They are aggregates, so they belong in SQL, and there they cost one scan and
    no memory at all.
    """
    import pyarrow.parquet as pq

    available = set(pq.ParquetFile(str(pairs_path)).schema_arrow.names)
    parts = []
    for name, expression in _PAIR_COUNT_SQL.items():
        usable = all(c in available for c in _PAIR_COUNT_NEEDS[name])
        parts.append(f"{expression if usable else '0'} AS {name}")
    con = duckdb_conn.connect(temp_dir)
    try:
        row = con.execute(
            f"SELECT {', '.join(parts)} FROM read_parquet('{pairs_path}')"
        ).fetchone()
    finally:
        con.close()
    return {name: int(value) for name, value in zip(_PAIR_COUNT_SQL, row)}


def unit_counts_of(units_path, temp_dir=None) -> dict:
    """``units_total`` and one count per track, straight out of DuckDB."""
    con = duckdb_conn.connect(temp_dir)
    try:
        rows = con.execute(
            f"SELECT track, count(*) FROM read_parquet('{units_path}') GROUP BY track"
        ).fetchall()
    except Exception:  # noqa: BLE001 — a file with no track column still counts
        rows = []
        total = con.execute(
            f"SELECT count(*) FROM read_parquet('{units_path}')").fetchone()[0]
        con.close()
        return {"units_total": int(total)}
    finally:
        con.close()
    counts = {"units_total": int(sum(n for _track, n in rows))}
    for track, n in rows:
        if track is not None:
            counts[f"units_{track}"] = int(n)
    return counts


def rewrite_pairs(pairs_path, units: pd.DataFrame, review: float, high: float,
                  ruleset: dict | None = None, model_lines: dict | None = None,
                  batch_rows: int | None = None) -> int:
    """Apply the buckets and the overlays again, in place, a batch at a time.

    This is what a threshold move costs, and what reverting a model costs. It
    used to read the whole pairs file, rewrite it in memory and write it back,
    which is three copies of a file that will one day hold a hundred million
    rows. Now it streams through one writer and swaps the result in.
    """
    pairs_path = Path(pairs_path)
    temporary = pairs_path.with_suffix(".rewrite.parquet")
    writer = PairWriter(temporary)
    empty = None
    for batch in iter_pair_batches(pairs_path, batch_rows or pair_batch_rows()):
        if not len(batch):
            continue
        frame = finalise_pairs(
            apply_overlays(_strip_overlays(batch), units, review, high,
                           model_lines=model_lines, ruleset=ruleset),
            units,
        )
        writer.write(frame)
        if empty is None:
            empty = frame.iloc[0:0]
    rows = writer.close(empty=empty)
    temporary.replace(pairs_path)
    return rows


def append_pairs(pairs_path, extra: pd.DataFrame, batch_rows: int | None = None) -> None:
    """Add *extra* rows to the pairs file without reading it whole.

    Parquet cannot be appended to in place, so the file is rewritten a batch at
    a time through one writer and swapped in. The forced label rows are a
    handful even on a big run, but the file they join may not be.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    pairs_path = Path(pairs_path)
    if not len(extra):
        return
    temporary = pairs_path.with_suffix(".rewrite.parquet")
    schema = pq.ParquetFile(str(pairs_path)).schema_arrow
    # A pairs file written from an empty frame (nothing was scored, only forced
    # label rows follow) has null-typed columns, and a string cannot be cast to
    # null. Take those columns' types from the rows being added instead.
    if any(pa.types.is_null(f.type) for f in schema):
        inferred = pa.Table.from_pandas(extra.reindex(columns=schema.names),
                                        preserve_index=False).schema
        fields = []
        for f in schema:
            if pa.types.is_null(f.type):
                found = inferred.field(f.name).type
                fields.append(pa.field(f.name, pa.string() if pa.types.is_null(found) else found))
            else:
                fields.append(f)
        schema = pa.schema(fields)
    writer = pq.ParquetWriter(str(temporary), schema)
    try:
        handle = pq.ParquetFile(str(pairs_path))
        for batch in handle.iter_batches(batch_size=batch_rows or pair_batch_rows()):
            writer.write_table(pa.Table.from_batches([batch]).cast(schema))
        writer.write_table(
            pa.Table.from_pandas(extra.reindex(columns=schema.names),
                                 schema=schema, preserve_index=False)
        )
    finally:
        writer.close()
    temporary.replace(pairs_path)


def counts_from(units, pairs, evaluation: dict,
                outcome: dict | None = None, run_dir=None,
                untrained: list[dict] | None = None) -> dict:
    """The run counts stage 3 contributes, in the pipeline's snake_case.

    The bucket counts are the ones a reviewer sees, so they carry the human
    overlay: ``pairs.parquet`` holds the score and the import overlay, and the
    evaluation has already laid the decisions on top of it.

    *units* is either the units frame or the unit counts already worked out from
    it, and *pairs* is either the pairs frame or the pair counts. A run at PSC
    scale has neither frame in memory by the time it gets here, and asking for
    one would undo the whole point of the stage.
    """
    unit_counts = units if isinstance(units, dict) else units_module.counts_from(units)
    if isinstance(pairs, dict):
        pair_totals = dict(pairs)
    else:
        pair_totals = {
            "pairs_scored": int(len(pairs)),
            "pairs_decided_by_import": int(
                (pairs["decided_by"] == "import").sum()) if len(pairs) else 0,
            "pairs_import_disagrees": int(
                pairs["import_disagrees"].fillna(False).sum()) if len(pairs) else 0,
            **vetoes.counts_from(pairs),
        }

    with_human = evaluation.get("with_human") or {}
    mine = (outcome or {}).get("mine")
    verdicts = mine["is_match"].astype(str).str.upper() if mine is not None and len(mine) \
        else pd.Series(dtype="object")
    buckets = with_human.get("by_bucket") or {}
    contradictions = (outcome or {}).get("contradictions") or []
    applied = (outcome or {}).get("applied")
    satisfied = (outcome or {}).get("satisfied")
    counts = {
        **unit_counts,
        "untrained_comparisons": len(untrained or []),
        "pairs_accept": int(buckets.get("accept", 0)),
        "pairs_review": int(buckets.get("review", 0)),
        "pairs_reject": int(buckets.get("reject", 0)),
        **pair_totals,
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

    temp_dir = run_dir / "duckdb_tmp"
    units_path = run_dir / units_module.UNITS_FILENAME
    members_path = run_dir / units_module.UNIT_MEMBERS_FILENAME
    pairs_path = run_dir / PAIRS_FILENAME
    events_path = run_dir / EVENTS_FILENAME

    # The records, the groups and the units are files from here on. The build
    # reads them in DuckDB and writes the two parquets itself; it never holds
    # the units frame and this stage never holds the records one. Reading them
    # into pandas and writing them back was tens of gigabytes at fifteen
    # million records (`PSC_HANDOVER.md`, section 24).
    records_path = run_dir / RECORDS_FILENAME
    n_records = units_module.record_count(records_path)
    with _phase(f"Building units from {n_records:,} records", progress_callback):
        units_module.build_units_files(
            records_path, run_dir / EXACT_GROUPS_FILENAME, units_path, members_path,
            events_path if events_path.is_file() else None, temp_dir=temp_dir,
        )
        units_module.write_fingerprint(
            units_path, run_dir / SCORED_UNITS_FILENAME, temp_dir)
        unit_counts = units_module.counts_from_file(units_path, temp_dir)
    _step(f"  {unit_counts['units_total']:,} units", progress_callback)

    # Everything after this reads projections off the two files just written.
    # Stage 3 needs four unit columns for the overlays and three record columns
    # for the evaluation, out of the sixty-odd each of them carries.
    #
    # The overlay projection and the exact groups are read where they are used,
    # not here. Both are O(units) and O(records), both are needed only after
    # Splink has finished and been released, and holding them across training
    # and prediction adds their whole size to the stage's peak for nothing.
    freed = clear_duckdb_tmp(temp_dir)
    if freed:
        _step(f"  Cleared {freed / 2**30:.1f} GB of spill left by an earlier run",
              progress_callback)

    # Measuring a hot-key control is a GROUP BY over every unit in the track,
    # and the budget and the training both need the answer. One dict, kept for
    # the length of the stage, so it is measured once.
    control_cache: dict = {}
    with _phase(f"Checking the blocking budget (DuckDB capped at {memory_limit()})",
                progress_callback):
        budget_api = _db_api(temp_dir)
        report, failure = blocking_budget_report(units_path, settings, budget_api,
                                                 progress_callback,
                                                 cache=control_cache)

    def _write_report() -> None:
        (run_dir / BLOCKING_REPORT_FILENAME).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    _write_report()
    if failure is not None:
        raise failure

    predictions: dict[str, Path] = {}
    untrained: list[dict] = []
    for track in linkage.TRACK_KEYS:
        config = linkage.track_settings(settings, track)
        rows = read_unit_projection(units_path, splink_columns(config, ruleset, track))
        rows = units_module.track_units(rows, track)
        if len(rows) < 2:
            _step(f"Skipping {track}: {len(rows)} unit(s), nothing to compare.",
                  progress_callback)
            continue
        if not linkage.blocking_rules(config):
            _step(f"Skipping {track}: no blocking rules in linkage_settings.",
                  progress_callback)
            continue

        priced = (report.get("tracks", {}).get(track) or {}).get("total")
        with _phase(f"Scoring {track} ({len(rows):,} units)", progress_callback):
            linker, path, n_pairs, routes = train_track(
                rows, config, settings, ruleset, track, run_dir,
                progress_callback, priced_pairs=priced,
                control_cache=control_cache,
            )
        predictions[track] = path
        # Which route made which pairs, so a reader can see what each blocking
        # rule actually bought rather than only what it was priced at.
        rule_ids = [r["id"] for r in linkage.blocking_rules(config)]
        for route in routes:
            key = route["match_key"]
            if 0 <= key < len(rule_ids):
                route["id"] = rule_ids[key]
        if track in report.get("tracks", {}):
            report["tracks"][track]["prediction"] = {
                "path": ("route_by_route"
                         if any("seconds" in r for r in routes) else "one_pass"),
                "pairs": int(n_pairs),
                "routes": routes,
            }
            _write_report()
        # `train_track` saved the model before it predicted, so it survives a
        # failure in prediction; this reads what it wrote.
        model_path = run_dir / MODEL_FILENAME.format(track=track)
        untrained.extend(inspect_trained_model(model_path, track, progress_callback))
        if render_diagnostics:
            with _phase(f"Rendering the {track} model charts", progress_callback):
                render_model_charts(linker, track, run_dir / "diagnostics",
                                    progress_callback)
        # Splink keeps the units it was handed, and every intermediate table it
        # made, inside its own DuckDB connection. On the person track that is
        # about a gigabyte, and the organisation track is about to ask for its
        # own. Let it go before the next track starts, not after both.
        release_linker(linker)
        del linker, rows

    overlay_units = read_unit_projection(units_path, overlay_columns(ruleset))
    with _phase("Applying the overlays and writing the pairs", progress_callback):
        n_written = overlay_predictions(
            predictions, overlay_units, review, high, pairs_path,
            ruleset=ruleset, progress_callback=progress_callback,
        )
    _step(f"  {n_written:,} pairs written", progress_callback)
    for path in predictions.values():
        path.unlink(missing_ok=True)

    if render_diagnostics:
        with _phase("Rendering the score distributions", progress_callback):
            for track in predictions:
                render_score_histogram(pairs_path, track, run_dir / "diagnostics",
                                       review, high, temp_dir, progress_callback)

    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)
    outcome = label_outcomes_from_files(labels, members_path, groups)
    forced = forced_pairs(outcome["applied"], pairs_path, units_path)
    outcome["forced"] = int(len(forced))
    if len(forced):
        # A human decided these; blocking or the candidate floor never offered
        # them. They join the file with no score rather than being lost.
        append_pairs(pairs_path, finalise_pairs(
            apply_overlays(forced, overlay_units, review, high, ruleset=ruleset),
            overlay_units))
        _step(f"  {len(forced):,} labelled pair(s) added that scoring never produced",
              progress_callback)

    # Stage 3b: the track's own model, when one is active (docs/MODEL.md).
    with _phase("Applying the active track models", progress_callback):
        model_state = apply_active_models(
            run_dir, pairs_path, units_path, members_path, events_path, review, high,
            progress_callback, ruleset=ruleset, overlay_units=overlay_units,
        )

    write_contradictions(run_dir, outcome["contradictions"])
    if outcome["contradictions"]:
        _step(f"  WARNING: {len(outcome['contradictions'])} FALSE label(s) "
              "contradicted by an exact match key", progress_callback)

    with _phase("Evaluating the accepted pairs against the existing labels",
                progress_callback):
        evaluation = score_eval.evaluate(
            read_projection(run_dir / RECORDS_FILENAME, score_eval.RECORD_COLUMNS),
            groups,
            read_unit_projection(units_path, ("unit_id", "track",
                                              "existing_entity_id")),
            pd.read_parquet(members_path),
            pairs_path,
            thresholds={"candidate": candidate, "review": review, "high": high},
            applied=outcome["applied"],
            model_lines=stage_3b_model.deciding_lines(
                stage_3b_model.models_from_state(run_dir)),
        )
        _write_evaluation(run_dir, evaluation)

    counts = counts_from(unit_counts, pair_counts(pairs_path, temp_dir), evaluation,
                         outcome, run_dir=run_dir, untrained=untrained)
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
    units_path = run_dir / units_module.UNITS_FILENAME
    members_path = run_dir / units_module.UNIT_MEMBERS_FILENAME
    pairs_path = run_dir / PAIRS_FILENAME
    temp_dir = run_dir / "duckdb_tmp"
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)
    ruleset = run_ruleset(run_dir)
    overlay_units = read_unit_projection(units_path, overlay_columns(ruleset))

    # A threshold move must not un-apply the model: the lines being moved are
    # Splink's, and a graded model keeps deciding whichever tracks it decided.
    lines = stage_3b_model.deciding_lines(stage_3b_model.models_from_state(run_dir))
    rewrite_pairs(pairs_path, overlay_units, float(threshold_review),
                  float(threshold_high), ruleset=ruleset, model_lines=lines)
    outcome = label_outcomes_from_files(labels, members_path, groups)
    write_contradictions(run_dir, outcome["contradictions"])

    settings_path = run_dir / "config" / "linkage_settings.json"
    candidate = linkage.DEFAULT_CANDIDATE
    if settings_path.is_file():
        candidate = linkage.thresholds(
            json.loads(settings_path.read_text(encoding="utf-8"))
        )[0]

    evaluation = score_eval.evaluate(
        read_projection(run_dir / RECORDS_FILENAME, score_eval.RECORD_COLUMNS),
        groups,
        read_unit_projection(units_path, ("unit_id", "track", "existing_entity_id")),
        pd.read_parquet(members_path),
        pairs_path,
        thresholds={"candidate": candidate, "review": float(threshold_review),
                    "high": float(threshold_high)},
        applied=outcome["applied"], model_lines=lines,
    )
    _write_evaluation(run_dir, evaluation)
    return counts_from(unit_counts_of(units_path, temp_dir),
                       pair_counts(pairs_path, temp_dir), evaluation, outcome,
                       run_dir=run_dir)


def refresh_after_labels(run_dir: str, labels: pd.DataFrame | None) -> dict:
    """Redo the counts and the evaluation for a run whose labels have changed.

    This is what a label write costs: the parquet is left exactly as scoring
    left it, and only the derived numbers are worked out again. The overlay
    itself is applied where the pairs are read.
    """
    run_dir = Path(run_dir)
    units_path = run_dir / units_module.UNITS_FILENAME
    members_path = run_dir / units_module.UNIT_MEMBERS_FILENAME
    pairs_path = run_dir / PAIRS_FILENAME
    temp_dir = run_dir / "duckdb_tmp"
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)

    settings_path = run_dir / "config" / "linkage_settings.json"
    candidate, review, high = linkage.DEFAULT_CANDIDATE, None, None
    if settings_path.is_file():
        candidate, review, high = linkage.thresholds(
            json.loads(settings_path.read_text(encoding="utf-8"))
        )

    outcome = label_outcomes_from_files(labels, members_path, groups)
    write_contradictions(run_dir, outcome["contradictions"])
    evaluation = score_eval.evaluate(
        read_projection(run_dir / RECORDS_FILENAME, score_eval.RECORD_COLUMNS),
        groups,
        read_unit_projection(units_path, ("unit_id", "track", "existing_entity_id")),
        pd.read_parquet(members_path),
        pairs_path,
        thresholds={"candidate": candidate, "review": review, "high": high},
        applied=outcome["applied"],
        model_lines=stage_3b_model.deciding_lines(
            stage_3b_model.models_from_state(run_dir)),
    )
    _write_evaluation(run_dir, evaluation)
    return counts_from(unit_counts_of(units_path, temp_dir),
                       pair_counts(pairs_path, temp_dir), evaluation, outcome,
                       run_dir=run_dir)


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

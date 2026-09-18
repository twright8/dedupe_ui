"""Stage 2 - Phase 2: Splink probabilistic record linkage on the post-exact-match pool.

Reads ocod_phase2.parquet and roe_phase2.parquet (records that did not exact-match
in Stage 1) and runs Splink blocking on jurisdiction OR name_core. The smaller pool
means pair counts stay manageable (~4M vs ~100M+ on the full dataset).

Adapted from ``matching roe ocod/src/stage_2_probabilistic_link.py``.  All
hardcoded paths replaced by ``run_dir`` / ``config_dir`` parameters.
"""

import json
import logging
import os
import re
import time
from pathlib import Path

import pandas as pd
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
from splink.blocking_rule_library import CustomRule

logging.getLogger("splink").setLevel(logging.INFO)


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def _load_settings(config_dir: Path) -> dict:
    with open(config_dir / "linkage_settings.json", encoding="utf-8") as f:
        return json.load(f)


_SIMPLE_EQ_RE = re.compile(r"^l\.(\w+)\s*=\s*r\.(\w+)$")


def _build_blocking_rule(rule_str: str):
    """Turn a config blocking-rule string into a Splink blocking rule.

    A pure ``l.col = r.col`` becomes ``block_on(col)``; any compound or guarded
    rule (e.g. the name_core rule ``l.name_core = r.name_core AND l.name_core <> ''``)
    is passed through verbatim as a raw ``CustomRule`` so SQL beyond simple
    equality is supported.
    """
    m = _SIMPLE_EQ_RE.match(rule_str.strip())
    if m and m.group(1) == m.group(2):
        return block_on(m.group(1))
    return CustomRule(rule_str)


def _equality_cols(rule_str: str) -> set[str]:
    """Columns a rule forces equal across the pair (each ``l.col = r.col``)."""
    return {a for a, b in re.findall(r"l\.(\w+)\s*=\s*r\.(\w+)", rule_str) if a == b}


def _add_jurisdiction_cmp(df: pd.DataFrame) -> pd.DataFrame:
    """Add a comparison-only jurisdiction column, NULL for UNKNOWN / empty values.

    Splink's ExactMatch then treats those as its *null level* (no evidence either
    way) instead of a jurisdiction disagreement — the UNKNOWN sentinel means the
    jurisdiction was not reliably recorded, and the name_core blocking rule exists
    precisely to rescue such records, so they must not be punished for it. The raw
    ``jurisdiction_clean`` is left untouched for blocking.
    """
    jc = df["jurisdiction_clean"].fillna("").astype(str).str.strip().str.upper()
    df["jurisdiction_cmp"] = jc.where(~jc.isin(["", "UNKNOWN"]), other=None)
    return df


def _estimate_m_with_em(linker: Linker, progress_callback=None) -> None:
    _step("Estimating parameters via EM on name_clean (blocked on jurisdiction)...", progress_callback)
    t0 = time.time()
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("jurisdiction_clean"),
        fix_u_probabilities=True,
    )
    _step(f"  EM training done  ({time.time()-t0:.1f}s)", progress_callback)


def run_stage_2(
    run_dir: str,
    config_dir: str,
    progress_callback=None,
    **_kwargs,
):
    """Run Stage 2: Splink probabilistic linkage.

    Parameters
    ----------
    run_dir : str
        Directory containing Stage 1 outputs and where Stage 2 outputs are
        written.
    config_dir : str
        Directory containing ``linkage_settings.json``.
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    """
    t_start = time.time()
    run_dir = Path(run_dir)
    config_dir = Path(config_dir)
    duckdb_tmp_dir = run_dir / "duckdb_tmp"

    if progress_callback:
        progress_callback("stage_start", {"stage": 2, "name": "probabilistic_link"})

    _step("Loading Phase 2 inputs (post-exact-match pool)...", progress_callback)
    config = _load_settings(config_dir)

    ocod_phase2_path = run_dir / "ocod_phase2.parquet"
    roe_phase2_path = run_dir / "roe_phase2.parquet"
    if not ocod_phase2_path.exists() or not roe_phase2_path.exists():
        raise RuntimeError(
            "Phase 2 input parquet files not found. Run stage 1 (exact match) first."
        )

    ocod_p2 = pd.read_parquet(ocod_phase2_path)
    roe_p2 = pd.read_parquet(roe_phase2_path)

    splink_cols = ["unique_id", "name_clean", "jurisdiction_clean", "name_digits_sorted", "name_core", "name_tokens_sorted"]
    ocod_for_splink = _add_jurisdiction_cmp(ocod_p2[splink_cols].copy())
    roe_for_splink = _add_jurisdiction_cmp(roe_p2[splink_cols].copy())

    _step(f"  OCOD records for probabilistic linkage: {len(ocod_for_splink):,}", progress_callback)
    _step(f"  ROE  records for probabilistic linkage: {len(roe_for_splink):,}", progress_callback)

    estimated_pairs = sum(
        ocod_for_splink["jurisdiction_clean"].value_counts().mul(
            roe_for_splink["jurisdiction_clean"].value_counts()
        ).dropna()
    )
    _step(f"  Estimated total pairs after blocking: {int(estimated_pairs):,}", progress_callback)

    blocking_rules = [_build_blocking_rule(r) for r in config["blocking_rules"]]

    # A comparison column adds no signal only when EVERY blocking rule already
    # forces it equal across all candidate pairs. With an OR of rules (jurisdiction
    # OR name_core) neither column is constant, so both stay real comparisons — a
    # jurisdiction mismatch is then weighed as evidence instead of being impossible.
    # When only the jurisdiction rule is present it is constant within blocks and is
    # skipped exactly as before.
    rule_equality_cols = [_equality_cols(r) for r in config["blocking_rules"]]
    always_equal_cols = set.intersection(*rule_equality_cols) if rule_equality_cols else set()

    comparisons = []
    for col_name, col_config in config["comparisons"].items():
        if col_name in always_equal_cols:
            continue
        func_name = col_config["splink_function"]
        args = col_config.get("splink_args", {})
        # Jurisdiction is compared on jurisdiction_cmp (UNKNOWN -> NULL level) so an
        # UNKNOWN jurisdiction reads as no-evidence rather than a disagreement.
        compare_col = "jurisdiction_cmp" if col_name == "jurisdiction_clean" else col_name
        if func_name == "cl.JaroWinklerAtThresholds":
            comparisons.append(cl.JaroWinklerAtThresholds(compare_col, **args))
        elif func_name == "cl.ExactMatch":
            comparisons.append(cl.ExactMatch(compare_col, **args))

    duckdb_tmp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(duckdb_tmp_dir)

    db_api = DuckDBAPI()
    db_api._con.execute(f"PRAGMA temp_directory='{duckdb_tmp_dir}'")
    db_api._con.execute("PRAGMA memory_limit='4GB'")
    db_api._con.execute("PRAGMA threads=4")
    _step(f"  DuckDB temp_directory: {duckdb_tmp_dir}", progress_callback)
    _step("  DuckDB memory_limit: 4GB, threads: 4", progress_callback)

    settings_kwargs = dict(
        link_type="link_only",
        comparisons=comparisons,
        blocking_rules_to_generate_predictions=blocking_rules,
        retain_intermediate_calculation_columns=True,
    )
    if "probability_two_random_records_match" in config:
        settings_kwargs["probability_two_random_records_match"] = config[
            "probability_two_random_records_match"
        ]
        _step(
            f"  Using probability_two_random_records_match = "
            f"{config['probability_two_random_records_match']} (from config)",
            progress_callback,
        )

    settings = SettingsCreator(**settings_kwargs)

    _step("Initialising Splink Linker (DuckDB backend)...", progress_callback)
    linker = Linker(
        [ocod_for_splink, roe_for_splink],
        settings,
        db_api=db_api,
        input_table_aliases=["ocod", "roe"],
    )

    _step("Estimating u probabilities via random sampling (max 5M pairs)...", progress_callback)
    t0 = time.time()
    linker.training.estimate_u_using_random_sampling(
        max_pairs=5e6,
        seed=config.get("random_seed"),
    )
    _step(f"  u estimation done  ({time.time()-t0:.1f}s)", progress_callback)

    # Splink is a purely unsupervised candidate generator: m is always estimated via
    # EM (labels train only the GBT — RECONCILIATION_DECISIONS O1). This keeps Phase-2
    # scores stable run-to-run and stops held-out evaluation labels leaking into the
    # splink_p1 feature the GBT consumes.
    _estimate_m_with_em(linker, progress_callback)

    # Predict down to the candidate floor, not the review floor, so the full candidate
    # set (including weak pairs below review) is retained in linkage_scored.parquet for
    # the GBT to rescore and for below-floor inspection. The review threshold is applied
    # later, purely as a Stage-3 bucketing line. Old configs without the candidate key
    # fall back to the review threshold (reproducing the previous behaviour).
    candidate_floor = config.get(
        "match_probability_threshold_candidate",
        config["match_probability_threshold_review"],
    )
    _step(f"Predicting matches (candidate floor={candidate_floor})...", progress_callback)
    t0 = time.time()
    results = linker.inference.predict(
        threshold_match_probability=candidate_floor
    )
    _step(f"  prediction done  ({time.time()-t0:.1f}s)", progress_callback)

    _step("Converting results to pandas dataframe...", progress_callback)
    df_results = results.as_pandas_dataframe()
    _step(
        f"  Candidate pairs above candidate floor ({candidate_floor}): {len(df_results):,}",
        progress_callback,
    )

    _step("Saving trained Splink model...", progress_callback)
    linker.misc.save_model_to_json(str(run_dir / "splink_model.json"), overwrite=True)

    _step("Writing outputs...", progress_callback)
    df_results.to_parquet(run_dir / "linkage_scored.parquet", index=False)

    elapsed = time.time() - t_start
    print(f"\nStage 2 complete in {elapsed:.1f}s. Outputs:")
    print(f"  {run_dir / 'linkage_scored.parquet'}")
    print(f"  {run_dir / 'splink_model.json'}")

    if progress_callback:
        progress_callback("stage_end", {
            "stage": 2,
            "name": "probabilistic_link",
            "elapsed_seconds": round(elapsed, 1),
            "candidate_pairs": len(df_results),
            "candidate_floor": candidate_floor,
        })

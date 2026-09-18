"""Stage 1 - Phase 1: Deterministic exact match on (name_clean, jurisdiction_clean).

Catches the high-confidence exact-name matches in a single hash join. Records
that match here are removed from both pools before Stage 2's probabilistic
linkage runs, drastically reducing pair counts.

Adapted from ``matching roe ocod/src/stage_1_exact_match.py``.  All hardcoded
paths replaced by the ``run_dir`` parameter.
"""

import time
from pathlib import Path

import pandas as pd


def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def run_stage_1(
    run_dir: str,
    config_dir: str,
    progress_callback=None,
    **_kwargs,
):
    """Run Stage 1: deterministic exact match.

    Parameters
    ----------
    run_dir : str
        Directory containing Stage 0 outputs and where Stage 1 outputs are
        written.
    config_dir : str
        Directory containing config files (not used by this stage, accepted
        for interface consistency).
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    """
    t_start = time.time()
    run_dir = Path(run_dir)

    if progress_callback:
        progress_callback("stage_start", {"stage": 1, "name": "exact_match"})

    _step("Loading preprocessed parquet files...", progress_callback)
    roe = pd.read_parquet(run_dir / "roe_preprocessed.parquet")
    ocod_dedup = pd.read_parquet(run_dir / "ocod_dedup.parquet")

    roe_in = roe[roe["jurisdiction_clean"] != "UNKNOWN"].copy()
    ocod_in = ocod_dedup[ocod_dedup["jurisdiction_clean"] != "UNKNOWN"].copy()

    _step(f"  OCOD pool: {len(ocod_in):,} (deduped, non-UNKNOWN)", progress_callback)
    _step(f"  ROE  pool: {len(roe_in):,} (non-UNKNOWN)", progress_callback)

    _step("Joining on (name_clean, jurisdiction_clean) for exact match...", progress_callback)
    t0 = time.time()
    exact = ocod_in.merge(
        roe_in[
            [
                "unique_id",
                "name_clean",
                "jurisdiction_clean",
                "roe_company_number",
                "roe_name_raw",
                "roe_jurisdiction_raw",
            ]
        ],
        on=["name_clean", "jurisdiction_clean"],
        how="inner",
        suffixes=("_ocod", "_roe"),
    )
    _step(f"  Join done  ({time.time()-t0:.2f}s)", progress_callback)

    n_exact_pairs = len(exact)
    n_ocod_matched = exact["unique_id_ocod"].nunique()
    n_roe_matched = exact["unique_id_roe"].nunique()

    _step(f"  Exact-match pairs:            {n_exact_pairs:,}", progress_callback)
    _step(
        f"  Unique OCOD records matched:  {n_ocod_matched:,}  "
        f"({100*n_ocod_matched/len(ocod_in):.1f}% of OCOD pool)",
        progress_callback,
    )
    _step(
        f"  Unique ROE records matched:   {n_roe_matched:,}  "
        f"({100*n_roe_matched/len(roe_in):.1f}% of ROE pool)",
        progress_callback,
    )

    matched_ocod_ids = set(exact["unique_id_ocod"])
    matched_roe_ids = set(exact["unique_id_roe"])

    # Phase 2 pool = ALL records not consumed by an exact match, including the
    # UNKNOWN-jurisdiction rows excluded from the exact join above. Stage 2 blocks
    # on jurisdiction OR name_core, so UNKNOWN-jurisdiction records can still be
    # compared (and rescued) on name — they must not be dropped here.
    ocod_phase2 = ocod_dedup[~ocod_dedup["unique_id"].isin(matched_ocod_ids)].reset_index(drop=True)
    roe_phase2 = roe[~roe["unique_id"].isin(matched_roe_ids)].reset_index(drop=True)

    _step("Phase 2 remaining pools:", progress_callback)
    _step(f"  OCOD remaining: {len(ocod_phase2):,}", progress_callback)
    _step(f"  ROE  remaining: {len(roe_phase2):,}", progress_callback)

    _step("Writing outputs...", progress_callback)
    exact_out_cols = [
        "unique_id_ocod",
        "unique_id_roe",
        "name_clean",
        "jurisdiction_clean",
        "roe_company_number",
        "roe_name_raw",
        "roe_jurisdiction_raw",
    ]
    exact[exact_out_cols].to_parquet(run_dir / "exact_matches.parquet", index=False)
    ocod_phase2.to_parquet(run_dir / "ocod_phase2.parquet", index=False)
    roe_phase2.to_parquet(run_dir / "roe_phase2.parquet", index=False)

    elapsed = time.time() - t_start
    print(f"\nStage 1 complete in {elapsed:.2f}s. Outputs:")
    print(f"  exact_matches.parquet:  {len(exact):,} pairs")
    print(f"  ocod_phase2.parquet:    {len(ocod_phase2):,} rows (for Stage 2)")
    print(f"  roe_phase2.parquet:     {len(roe_phase2):,} rows (for Stage 2)")

    if progress_callback:
        progress_callback("stage_end", {
            "stage": 1,
            "name": "exact_match",
            "elapsed_seconds": round(elapsed, 2),
            "exact_pairs": n_exact_pairs,
            "ocod_remaining": len(ocod_phase2),
            "roe_remaining": len(roe_phase2),
        })

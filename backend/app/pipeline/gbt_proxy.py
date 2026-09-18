"""Cold-start proxy ground truth for the GBT, generated free from the pipeline.

Positives: Phase-1 exact matches (identical cleaned name + jurisdiction by
construction — confident TRUE). Negatives: random OCOD/ROE pairs that are *not*
the exact match (overwhelmingly non-matches) — mostly same-jurisdiction, plus a
modest share of cross-jurisdiction pairs so ``jurisdiction_match`` has training
support for its genuine-mismatch state before human labels arrive.

These synthetic rows are used for TRAINING only — they are NOT written to the
label store (which stays a record of human/LLM observations). They give the
model continuous, separable scores before any human has labelled anything; the
fuzzy decision boundary is then sharpened by real labels.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.pipeline.gbt_features import FEATURE_COLS, build_features


def _perfect_positive_rows(n: int) -> pd.DataFrame:
    """Feature rows for exact matches: everything agrees, Splink did not score them."""
    return pd.DataFrame(
        {
            "base_name_jw": np.ones(n),
            "base_exact": np.ones(n, dtype=int),
            "base_len_diff": np.zeros(n, dtype=int),
            "name_core_exact": np.ones(n, dtype=int),
            "name_tokens_sorted_exact": np.ones(n, dtype=int),
            "name_digits_sorted_exact": np.ones(n, dtype=int),
            "splink_p1": np.full(n, -1.0),
            "unit_mismatch": np.zeros(n, dtype=int),
            "idf_token_overlap": np.ones(n),   # exact name => all tokens shared
            "jurisdiction_match": np.ones(n),  # exact matches share jurisdiction by construction
        },
        columns=FEATURE_COLS,
    )


def build_proxy_frame(
    run_dir: str,
    max_pos: int = 4000,
    max_neg: int = 8000,
    seed: int = 42,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Return ``(features, y)`` for cold-start proxy training, or empty if inputs missing."""
    run_dir = Path(run_dir)
    rng = np.random.default_rng(seed)

    exact_path = run_dir / "exact_matches.parquet"
    ocod_path = run_dir / "ocod_phase2.parquet"
    roe_path = run_dir / "roe_phase2.parquet"

    frames: list[pd.DataFrame] = []
    labels: list[np.ndarray] = []

    # --- Positives from exact matches ---
    if exact_path.exists():
        exact = pd.read_parquet(exact_path)
        n_pos = min(len(exact), max_pos)
        if n_pos > 0:
            frames.append(_perfect_positive_rows(n_pos))
            labels.append(np.ones(n_pos, dtype=int))

    # --- Negatives from random non-pairs ---
    # Mostly same-jurisdiction (as before), plus a modest share of cross-jurisdiction
    # pairs (a quarter of the budget) so ``jurisdiction_match`` has some training support
    # for the genuine-mismatch (0.0) state before many human labels exist. Without them
    # the proxy would carry zero signal on this feature (all proxy rows would be 1.0).
    if ocod_path.exists() and roe_path.exists():
        ocod = pd.read_parquet(ocod_path)
        roe = pd.read_parquet(roe_path)
        if len(ocod) and len(roe):
            roe_by_jur = {
                jur: grp["unique_id"].to_numpy()
                for jur, grp in roe.groupby("jurisdiction_clean")
            }
            multi_jur = len(roe_by_jur) > 1
            ocod_sample = ocod.sample(n=min(len(ocod), max_neg), random_state=seed)
            n_cross = len(ocod_sample) // 4
            l_ids, r_ids = [], []
            for k, (_, row) in enumerate(ocod_sample.iterrows()):
                jur = row["jurisdiction_clean"]
                if k < n_cross and multi_jur:
                    # Draw the ROE from a DIFFERENT jurisdiction (a genuine mismatch).
                    others = [j for j in roe_by_jur if j != jur]
                    pool = roe_by_jur[others[rng.integers(len(others))]] if others else None
                else:
                    pool = roe_by_jur.get(jur)
                if pool is None or len(pool) == 0:
                    continue
                l_ids.append(row["unique_id"])
                r_ids.append(pool[rng.integers(len(pool))])
            if l_ids:
                neg_pairs = pd.DataFrame({"unique_id_l": l_ids, "unique_id_r": r_ids})
                neg_feat = build_features(neg_pairs, ocod, roe, splink_prob_col=None)
                # Drop accidental exact-name collisions (those are likely true matches).
                keep = neg_feat["base_exact"] == 0
                neg_feat = neg_feat[keep].reset_index(drop=True)
                frames.append(neg_feat)
                labels.append(np.zeros(len(neg_feat), dtype=int))

    if not frames:
        return pd.DataFrame(columns=FEATURE_COLS), np.array([], dtype=int)

    X = pd.concat(frames, ignore_index=True)
    y = np.concatenate(labels)
    return X, y

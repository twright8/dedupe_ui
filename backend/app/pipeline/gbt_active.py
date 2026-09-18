"""Active-learning sampler: pick the highest-value pairs to label next.

Strategy (D8): prioritise the *uncertain* region (decision score near the
boundary) and the *disagreement* region (Splink confident but GBT not, or vice
versa — where the GBT is catching Splink false positives). At cold start there
is no GBT, so uncertainty falls back to the Splink review band. Already-labelled
pairs are excluded so the same pair is never re-queued.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from app.services.label_resolver import lookup_active_label


def _is_labelled(db_path: str, row: pd.Series) -> bool:
    return (
        lookup_active_label(
            db_path,
            row.get("ocod_name_clean"),
            row.get("jurisdiction_clean"),
            row.get("roe_company_number"),
            row.get("ocod_name_raw"),
            row.get("ocod_jurisdiction_raw") if "ocod_jurisdiction_raw" in row else None,
        )
        is not None
    )


def sample_batch(run_dir: str, db_path: str, n: int = 50) -> list[dict]:
    """Return up to ``n`` unlabelled candidate pairs most worth labelling."""
    run_dir = Path(run_dir)
    review_path = run_dir / "matches_for_review.csv"
    if not review_path.exists():
        return []

    df = pd.read_csv(review_path, encoding="utf-8-sig").fillna("")
    if len(df) == 0:
        return []

    score = pd.to_numeric(df.get("match_probability"), errors="coerce").fillna(0.0)
    df = df.assign(_score=score)

    # Disagreement signal where both scores are present.
    if "splink_probability" in df.columns:
        sp = pd.to_numeric(df["splink_probability"], errors="coerce").fillna(0.0)
        df["_disagreement"] = (sp - df["_score"]).abs()
    else:
        df["_disagreement"] = 0.0

    # Uncertainty = closeness to the middle of the review band.
    lo, hi = df["_score"].min(), df["_score"].max()
    mid = (lo + hi) / 2.0
    df["_uncertainty"] = -(df["_score"] - mid).abs()
    df["_priority"] = df["_uncertainty"] + df["_disagreement"]

    df = df.sort_values("_priority", ascending=False)

    out: list[dict] = []
    for _, row in df.iterrows():
        if len(out) >= n:
            break
        if _is_labelled(db_path, row):
            continue
        out.append(
            {
                "ocod_name_raw": row.get("ocod_name_raw", ""),
                "ocod_name_clean": row.get("ocod_name_clean", ""),
                "jurisdiction_clean": row.get("jurisdiction_clean", ""),
                "roe_name_raw": row.get("roe_name_raw", ""),
                "roe_company_number": row.get("roe_company_number", ""),
                "match_probability": float(row.get("_score", 0.0)),
                "splink_probability": float(pd.to_numeric(pd.Series([row.get("splink_probability", "")]), errors="coerce").fillna(0.0).iloc[0]),
            }
        )
    return out

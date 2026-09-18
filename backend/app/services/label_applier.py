# backend/app/services/label_applier.py
"""Label applier: reads active labels from the DB and applies them to a run's
merged dataset CSV, updating match outcomes and producing final output files."""

import logging
import os

import pandas as pd

from app.db import query_db
from app.services.label_resolver import (
    build_frame_key_index,
    entity_key_variants,
    find_indexed_rows,
    norm as _norm,
)

logger = logging.getLogger(__name__)

# Match methods that indicate a confirmed/positive match in the final output
_CONFIRMED_METHODS = {
    "exact",
    "probabilistic",
    "high_confidence",
    "reviewed_true_review",
    "reviewed_true_ambiguous",
}


def apply_labels(db_path: str, run_dir: str) -> dict:
    """Apply reviewer labels from the database to a run's output files.

    1. Reads merged_dataset.csv
    2. Queries all active labels
    3. Matches each label to a row via raw key, falling back to cleaned key
    4. Updates match_method / roe_company_number per TRUE/FALSE verdict
    5. Writes updated merged_dataset.csv, matches_final.csv,
       matches_user_confirmed.csv

    Returns:
        {"applied": N, "unmatched": M}
    """
    merged_path = os.path.join(run_dir, "merged_dataset.csv")
    df = pd.read_csv(merged_path, encoding="utf-8-sig", dtype=str)
    # Fill NaN with empty string for consistent comparison
    df = df.fillna("")

    _ensure_output_columns(df)

    # Fetch all active labels in update order so newer corrections win when
    # analysts intentionally change a decision for the same OCOD entity.
    labels = query_db(
        db_path,
        "SELECT * FROM labels WHERE active = 1 ORDER BY created_at, id",
    )

    if not labels:
        # No labels to apply — still write output files
        _write_output_files(df, run_dir)
        return {"applied": 0, "unmatched": 0}

    applied = 0
    unmatched = 0
    candidates = _load_candidate_lookup(run_dir)
    merged_index = build_frame_key_index(df)

    for label in labels:
        matched_indexes = find_indexed_rows(merged_index, label)
        if not matched_indexes:
            unmatched += 1
            continue

        verdict = str(label["is_true_match"]).upper()
        candidate = _candidate_for_label(candidates, label)

        if verdict == "TRUE":
            _apply_true_label(df, matched_indexes, label, candidate)
        elif verdict == "FALSE":
            _apply_false_label(df, matched_indexes, label)
        # Other verdicts (e.g. "review") are ignored — no row mutation

        applied += 1

    # Write updated merged dataset
    df.to_csv(merged_path, index=False, encoding="utf-8-sig")

    _write_output_files(df, run_dir)

    logger.info(
        "Label applier finished: applied=%d, unmatched=%d", applied, unmatched
    )
    return {"applied": applied, "unmatched": unmatched}


def _first_existing_column(df: pd.DataFrame, names: list[str]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _ensure_output_columns(df: pd.DataFrame) -> None:
    for column in (
        "roe_company_number",
        "roe_name_raw",
        "match_method",
        "match_probability",
        "match_count",
        "is_ambiguous",
    ):
        if column not in df.columns:
            df[column] = ""


def _series_norm(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.upper()


def _label_key_variants(row: dict) -> list[tuple[str, str]]:
    """Shared-resolver key variants (raw first, clean fallback) for a label or
    candidate row dict."""
    return entity_key_variants(
        row.get("ocod_name_clean"),
        row.get("jurisdiction_clean"),
        row.get("ocod_name_raw"),
        row.get("ocod_jurisdiction_raw"),
    )


def _load_candidate_lookup(run_dir: str) -> dict[tuple[str, str, str], dict]:
    """Load candidate rows from run outputs, preserving review/ambiguous source."""
    lookup: dict[tuple[str, str, str], dict] = {}
    files = [
        ("ambiguous", "matches_ambiguous.csv"),
        ("review", "matches_for_review.csv"),
        ("high", "matches_high_confidence.csv"),
        ("exact", "matches_exact.csv"),
    ]
    for source, filename in files:
        path = os.path.join(run_dir, filename)
        if not os.path.isfile(path):
            continue
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
        except Exception:
            logger.exception("Failed to read candidate file %s", path)
            continue
        for row in frame.to_dict(orient="records"):
            row["_source"] = source
            roe = _norm(row.get("roe_company_number"))
            if not roe:
                continue
            for name, jur in _label_key_variants(row):
                lookup.setdefault((name, jur, roe), row)
    return lookup


def _candidate_for_label(candidates: dict[tuple[str, str, str], dict], label: dict) -> dict | None:
    roe = _norm(label.get("roe_company_number"))
    if not roe:
        return None
    for name, jur in _label_key_variants(label):
        hit = candidates.get((name, jur, roe))
        if hit:
            return hit
    return None


def _apply_true_label(df: pd.DataFrame, indexes: list, label: dict, candidate: dict | None) -> None:
    source = (candidate or {}).get("_source")
    method = "reviewed_true_ambiguous" if source == "ambiguous" else "reviewed_true_review"
    df.loc[indexes, "roe_company_number"] = str(label["roe_company_number"])
    df.loc[indexes, "match_method"] = method

    if candidate:
        if candidate.get("roe_name_raw"):
            df.loc[indexes, "roe_name_raw"] = candidate["roe_name_raw"]
        if candidate.get("match_probability") not in (None, ""):
            df.loc[indexes, "match_probability"] = candidate["match_probability"]
        if source == "ambiguous":
            df.loc[indexes, "is_ambiguous"] = "True"
        elif "is_ambiguous" in df.columns:
            existing = _series_norm(df.loc[indexes, "is_ambiguous"])
            if not existing.isin({"TRUE", "1"}).any():
                df.loc[indexes, "is_ambiguous"] = "False"
    elif "roe_name_raw" in df.columns:
        # Keep an existing ROE name if present; otherwise leave it blank for
        # manually added labels where the label only knows the company number.
        pass

    if "match_count" in df.columns:
        blank_count = _series_norm(df.loc[indexes, "match_count"]) == ""
        if blank_count.any():
            df.loc[df.index.isin(indexes) & blank_count.reindex(df.index, fill_value=False), "match_count"] = "1"


def _apply_false_label(df: pd.DataFrame, indexes: list, label: dict) -> None:
    roe_col = _first_existing_column(df, ["roe_company_number"])
    if roe_col is None:
        return
    roe = _norm(label.get("roe_company_number"))
    if not roe:
        return
    mask = df.index.isin(indexes) & (_series_norm(df[roe_col]) == roe)
    if not mask.any():
        return
    df.loc[mask, "roe_company_number"] = ""
    df.loc[mask, "roe_name_raw"] = ""
    df.loc[mask, "match_method"] = ""
    df.loc[mask, "match_probability"] = ""


def _write_output_files(df: pd.DataFrame, run_dir: str) -> None:
    """Write matches_final.csv and matches_user_confirmed.csv.

    matches_final.csv: all confirmed matches — exact + high-confidence +
        user-confirmed TRUE (reviewed_true_review, reviewed_true_ambiguous).
        Also includes rows from matches_exact.csv and
        matches_high_confidence.csv that were already produced by earlier
        pipeline stages.

    matches_user_confirmed.csv: just the subset of pairs marked TRUE by
        reviewers (match_method starts with 'reviewed_true').
    """
    # The updated merged dataset is the source of truth after labels.  Do not
    # union the original exact/high CSVs back in here, or FALSE labels against
    # high-confidence rows would reappear in the final export.
    has_roe = _series_norm(df["roe_company_number"]) != ""
    confirmed_mask = has_roe & df["match_method"].isin(_CONFIRMED_METHODS)
    df_final = df[confirmed_mask].copy()

    user_confirmed_mask = df["match_method"].fillna("").astype(str).str.startswith("reviewed_true")
    df_user_confirmed = df[user_confirmed_mask].copy()

    # Write matches_final.csv
    final_path = os.path.join(run_dir, "matches_final.csv")
    df_final.to_csv(final_path, index=False, encoding="utf-8-sig")

    # Write matches_user_confirmed.csv
    user_confirmed_path = os.path.join(run_dir, "matches_user_confirmed.csv")
    df_user_confirmed.to_csv(user_confirmed_path, index=False, encoding="utf-8-sig")

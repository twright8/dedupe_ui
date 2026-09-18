# backend/app/services/match_reader.py
"""Match reader: paginated CSV reading with label joins and feature mapping."""

import math
import os
import re
from typing import Optional

import pandas as pd

from app.services.feature_mapper import map_features
from app.services.label_resolver import (
    load_active_label_index,
    lookup_active_label,
    lookup_in_index,
)

# Bucket name -> CSV filename
_BUCKET_FILES = {
    "exact": "matches_exact.csv",
    "high": "matches_high_confidence.csv",
    "review": "matches_for_review.csv",
    "ambiguous": "matches_ambiguous.csv",
    "unmatched_ocod": "unmatched_ocod.csv",
    "unmatched_roe": "unmatched_roe.csv",
}

# Buckets that contain scored pairs with Splink features
_SCORED_BUCKETS = {"review", "high", "ambiguous"}

# Columns searched by the free-text search filter
_SEARCH_COLUMNS = [
    "ocod_name_raw",
    "roe_name_raw",
    "ocod_name_clean",
    "roe_company_number",
]

# ---------------------------------------------------------------------------
# Address side-panel (display + human-review signal ONLY)
# ---------------------------------------------------------------------------
# Addresses are DELIBERATELY not used for matching and must never become a score
# input. The two sides record different things by design: OCOD carries the
# proprietor's UK service address (ocod_address_1/2/3, renamed in stage 0), while
# the ROE/Companies House side carries the entity's registered address in its home
# jurisdiction (roe_address_line1 / roe_post_town / roe_postcode). Among confirmed
# true matches only ~27% agree on postcode, so address evidence is one-directional:
# AGREEMENT is a strong confirm, DISAGREEMENT means nothing. We join these columns
# onto review rows at read time purely so a human reviewer can see them; no
# similarity score is computed, and nothing here reaches exports/scores/training.
_OCOD_ADDR_COLS = ["ocod_address_1", "ocod_address_2", "ocod_address_3"]
_ROE_ADDR_COLS = ["roe_address_line1", "roe_post_town", "roe_postcode"]

# UK postcode (outward + inward). OCOD's service address is free text with no
# discrete postcode field, so we extract one to enable the postcode_match flag.
_UK_POSTCODE_RE = re.compile(r"([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})")

# Per-run address lookup cache: run_dir -> {"key": mtimes, "ocod": {...}, "roe": {...}}
_ADDRESS_CACHE: dict = {}


def get_matches(
    run_dir: str,
    db_path: str,
    bucket: str,
    page: int = 1,
    per_page: int = 50,
    jurisdiction: Optional[str] = None,
    search: Optional[str] = None,
    match_method: Optional[str] = None,
) -> dict:
    """Read match CSV, apply filters/pagination, join labels and features.

    Args:
        run_dir: Path to the run output directory.
        db_path: Path to the SQLite database.
        bucket: One of exact, high, review, ambiguous, unmatched_ocod, unmatched_roe.
        page: 1-based page number.
        per_page: Rows per page.
        jurisdiction: Optional jurisdiction filter (case-insensitive).
        search: Optional free-text search across name/number columns.
        match_method: Optional filter by match_method column (e.g. "probabilistic").

    Returns:
        Dict with keys: items, total, page, per_page, total_pages.
    """
    bucket_counts = _bucket_counts(run_dir)

    if bucket == "all":
        frames = []
        for bname, fname in _BUCKET_FILES.items():
            if bname in ("unmatched_ocod", "unmatched_roe", "exact"):
                continue
            csv_p = os.path.join(run_dir, fname)
            if os.path.isfile(csv_p):
                bdf = pd.read_csv(csv_p, encoding="utf-8-sig")
                bdf["_bucket"] = bname
                frames.append(bdf)
        if not frames:
            return {"items": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0, "bucket_counts": bucket_counts}
        df = pd.concat(frames, ignore_index=True)
        bucket_for_features = "high"
    else:
        filename = _BUCKET_FILES.get(bucket)
        if filename is None:
            return {"items": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

        csv_path = os.path.join(run_dir, filename)
        if not os.path.isfile(csv_path):
            return {"items": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0, "bucket_counts": bucket_counts}

        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        df["_bucket"] = bucket
        bucket_for_features = bucket

    # --- Filters ---
    if match_method and "match_method" in df.columns:
        df = df[df["match_method"].astype(str) == match_method]

    if jurisdiction:
        jur_lower = jurisdiction.lower()
        mask = df["jurisdiction_clean"].astype(str).str.lower() == jur_lower
        df = df[mask]

    if search:
        search_lower = search.lower()
        mask = pd.Series(False, index=df.index)
        for col in _SEARCH_COLUMNS:
            if col in df.columns:
                mask = mask | df[col].astype(str).str.lower().str.contains(search_lower, na=False)
        df = df[mask]

    # --- Pagination ---
    total = len(df)
    total_pages = max(1, math.ceil(total / per_page))
    start = (page - 1) * per_page
    end = start + per_page
    page_df = df.iloc[start:end]

    # --- Build items ---
    # Per-run address lookup (cached); missing artifacts yield empty maps so that
    # runs created before this change still return rows with null address fields.
    addr_lookup = _address_lookup(run_dir)

    items = []
    for _, row in page_df.iterrows():
        record = row.to_dict()
        # Convert numpy types to native Python types for JSON serialization
        record = {k: _to_python(v) for k, v in record.items()}
        record_bucket = record.get("_bucket") or bucket
        record["bucket"] = record_bucket
        record["match_id"] = _match_id(record, record_bucket)
        record["id"] = record["match_id"]

        # Side-by-side address block (display + human-review signal only).
        record["address"] = _address_payload(addr_lookup, record)

        # Feature mapping for scored buckets
        effective_bucket = bucket_for_features if bucket == "all" else bucket
        if effective_bucket in _SCORED_BUCKETS or bucket == "all":
            record["features"] = map_features(record)

        # Label lookup
        label_info = _lookup_label(
            db_path,
            record.get("ocod_name_clean"),
            record.get("jurisdiction_clean"),
            record.get("roe_company_number"),
            record.get("ocod_name_raw"),
            record.get("ocod_jurisdiction_raw"),
        )
        record["label"] = label_info["label"]
        record["label_reviewer"] = label_info["reviewer"]

        items.append(record)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "bucket_counts": bucket_counts,
    }


def _load_scored_buckets(run_dir: str) -> pd.DataFrame:
    """Concatenate the buckets that need a human decision (review + ambiguous +
    high-confidence *probabilistic*) into one pair frame.

    Auto-accepted *exact* matches are excluded: they are confident 1:1 links that
    don't need review, and on a full run there are tens of thousands of them —
    loading them all would make the grouped views unusably slow.
    """
    frames = []
    for bname in ("high", "review", "ambiguous"):
        path = os.path.join(run_dir, _BUCKET_FILES[bname])
        if os.path.isfile(path):
            f = pd.read_csv(path, encoding="utf-8-sig")
            f["_bucket"] = bname
            frames.append(f)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "match_method" in df.columns:
        df = df[df["match_method"].astype(str) != "exact"]
    if {"ocod_unique_id", "roe_unique_id"}.issubset(df.columns):
        df = df.drop_duplicates(subset=["ocod_unique_id", "roe_unique_id"], keep="first")
    return df.reset_index(drop=True)


def _apply_text_filters(df: pd.DataFrame, jurisdiction, search) -> pd.DataFrame:
    if jurisdiction and "jurisdiction_clean" in df.columns:
        df = df[df["jurisdiction_clean"].astype(str).str.lower() == jurisdiction.lower()]
    if search:
        s = search.lower()
        mask = pd.Series(False, index=df.index)
        for col in _SEARCH_COLUMNS:
            if col in df.columns:
                mask = mask | df[col].astype(str).str.lower().str.contains(s, na=False)
        df = df[mask]
    return df


def _candidate_record(label_index: dict, row: pd.Series) -> dict:
    rec = {k: _to_python(v) for k, v in row.to_dict().items()}
    hit = lookup_in_index(
        label_index,
        rec.get("ocod_name_clean"),
        rec.get("jurisdiction_clean"),
        rec.get("roe_company_number"),
        rec.get("ocod_name_raw"),
        rec.get("ocod_jurisdiction_raw"),
    )
    if hit:
        verdict = hit.get("is_true_match")
        rec["label"] = str(verdict).strip().upper() if verdict not in (None, "") else None
        rec["label_reviewer"] = hit.get("reviewer")
    else:
        rec["label"] = None
        rec["label_reviewer"] = None
    rec["features"] = map_features(rec)
    return rec


def _paginate(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    total = len(items)
    total_pages = max(1, math.ceil(total / per_page)) if total else 0
    start = (page - 1) * per_page
    return items[start:start + per_page], total, total_pages


def get_matches_by_ocod(
    run_dir: str,
    db_path: str,
    page: int = 1,
    per_page: int = 50,
    jurisdiction: Optional[str] = None,
    search: Optional[str] = None,
) -> dict:
    """Entity-centric view: one OCOD entity per item with its ranked ROE candidates.

    Candidates are sorted by the decision score (``match_probability``, which is
    the GBT score when enabled); ``margin`` is the gap between the top two.
    """
    bucket_counts = _bucket_counts(run_dir)
    df = _load_scored_buckets(run_dir)
    if len(df) == 0:
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0, "bucket_counts": bucket_counts}
    df = _apply_text_filters(df, jurisdiction, search)
    label_index = load_active_label_index(db_path)

    if "ocod_unique_id" in df.columns:
        df["_ekey"] = df["ocod_unique_id"].astype(str)
    else:
        df["_ekey"] = df["ocod_name_clean"].astype(str) + "|" + df["jurisdiction_clean"].astype(str)

    entities = []
    for _, grp in df.groupby("_ekey", sort=False):
        grp = grp.sort_values("match_probability", ascending=False)
        candidates = [_candidate_record(label_index, row) for _, row in grp.iterrows()]
        probs = [float(c.get("match_probability") or 0.0) for c in candidates]
        top = probs[0] if probs else None
        margin = (probs[0] - probs[1]) if len(probs) >= 2 else None
        first = grp.iloc[0]
        entities.append({
            "ocod_unique_id": _to_python(first.get("ocod_unique_id")),
            "ocod_name_raw": first.get("ocod_name_raw"),
            "ocod_name_clean": first.get("ocod_name_clean"),
            "jurisdiction_clean": first.get("jurisdiction_clean"),
            "candidate_count": len(candidates),
            "top_score": top,
            "margin": margin,
            "candidates": candidates,
        })

    entities.sort(key=lambda e: (e["top_score"] or 0.0), reverse=True)
    items, total, total_pages = _paginate(entities, page, per_page)
    return {"items": items, "total": total, "page": page, "per_page": per_page,
            "total_pages": total_pages, "bucket_counts": bucket_counts}


def get_matches_by_roe(
    run_dir: str,
    db_path: str,
    page: int = 1,
    per_page: int = 50,
    jurisdiction: Optional[str] = None,
    search: Optional[str] = None,
) -> dict:
    """ROE-centric view: one ROE company per item with the OCOD entities claiming it.

    Surfaces the OCOD-side de-duplication signal — when several distinct OCOD
    entities all point at the same ROE, they are usually the same entity recorded
    different ways.
    """
    bucket_counts = _bucket_counts(run_dir)
    df = _load_scored_buckets(run_dir)
    if len(df) == 0:
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0, "bucket_counts": bucket_counts}
    df = _apply_text_filters(df, jurisdiction, search)
    df = df[df["roe_company_number"].astype(str).str.strip() != ""]
    label_index = load_active_label_index(db_path)

    groups = []
    for roe_no, grp in df.groupby("roe_company_number", sort=False):
        grp = grp.sort_values("match_probability", ascending=False)
        claimants = [_candidate_record(label_index, row) for _, row in grp.iterrows()]
        first = grp.iloc[0]
        groups.append({
            "roe_company_number": _to_python(roe_no),
            "roe_name_raw": first.get("roe_name_raw"),
            "jurisdiction_clean": first.get("jurisdiction_clean"),
            "claimant_count": len(claimants),
            "top_score": float(claimants[0].get("match_probability") or 0.0) if claimants else None,
            "claimants": claimants,
        })

    # Surface contested ROEs (multiple claimants) first.
    groups.sort(key=lambda g: (g["claimant_count"], g["top_score"] or 0.0), reverse=True)
    items, total, total_pages = _paginate(groups, page, per_page)
    return {"items": items, "total": total, "page": page, "per_page": per_page,
            "total_pages": total_pages, "bucket_counts": bucket_counts}


def _lookup_label(
    db_path: str,
    ocod_name_clean,
    jurisdiction_clean,
    roe_company_number,
    ocod_name_raw=None,
    ocod_jurisdiction_raw=None,
) -> dict:
    """Look up the active label for a match via the shared resolver.

    Uses the same raw-first (clean-fallback) key resolution as the export
    applier, so the review panel shows exactly the label the export will honour.
    """
    row = lookup_active_label(
        db_path,
        ocod_name_clean,
        jurisdiction_clean,
        roe_company_number,
        ocod_name_raw,
        ocod_jurisdiction_raw,
    )
    if row:
        verdict = row.get("is_true_match")
        verdict = str(verdict).strip().upper() if verdict not in (None, "") else None
        return {"label": verdict, "reviewer": row.get("reviewer")}
    return {"label": None, "reviewer": None}


# ---------------------------------------------------------------------------
# Address lookup helpers (read-time join; display + review signal only)
# ---------------------------------------------------------------------------

def _clean_str(val) -> Optional[str]:
    """Trim to a non-empty string, or None (also drops pandas 'nan' strings)."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return None
    return s


def _norm_space(val) -> str:
    """Collapse internal whitespace and trim."""
    return re.sub(r"\s+", " ", str(val)).strip()


def _extract_uk_postcode(text: Optional[str]) -> Optional[str]:
    """Pull the (last) UK-format postcode out of a free-text address, spaced.

    OCOD's service address has no discrete postcode column, so we extract one to
    let the reviewer compare it with the ROE registered postcode. The last match
    is used because a UK postcode conventionally sits at the end of the address.
    """
    if not text:
        return None
    matches = _UK_POSTCODE_RE.findall(text.upper())
    if not matches:
        return None
    outward, inward = matches[-1]
    return f"{outward} {inward}"


def _norm_postcode(val: Optional[str]) -> str:
    """Uppercase and strip all spaces — the normal form for postcode equality."""
    return re.sub(r"\s+", "", val.upper()) if val else ""


def _postcode_match(ocod_postcode: Optional[str], roe_postcode: Optional[str]):
    """True if the two postcodes agree (normalised), False if they differ,
    None when either side has no postcode. NOT a similarity score."""
    a = _norm_postcode(ocod_postcode)
    b = _norm_postcode(roe_postcode)
    if not a or not b:
        return None
    return a == b


def _town_match(ocod_text: Optional[str], roe_town: Optional[str]):
    """True if the ROE registered post-town also appears (case-insensitive) in
    the OCOD service address, False if not, None when either side is absent.

    OCOD has no structured town field, so this is a substring test against the
    free-text service address rather than field-to-field equality. NOT a score.
    """
    if not ocod_text or not roe_town:
        return None
    town = _norm_space(roe_town).upper()
    if not town:
        return None
    return town in _norm_space(ocod_text).upper()


def _parquet_columns(path: str) -> set:
    """Column names of a parquet file, without loading the data."""
    try:
        import pyarrow.parquet as pq
        return set(pq.ParquetFile(path).schema.names)
    except Exception:
        try:
            return set(pd.read_parquet(path).columns)
        except Exception:
            return set()


def _build_address_lookup(run_dir: str) -> dict:
    """Build {ocod: {(name_clean, jurisdiction) -> addr}, roe: {company_no -> addr}}
    from the run's preprocessed parquet artifacts. Tolerant of missing files and
    missing columns (older runs / trimmed fixtures) -> empty maps."""
    ocod_map: dict = {}
    roe_map: dict = {}

    ocod_p = os.path.join(run_dir, "ocod_preprocessed.parquet")
    if os.path.isfile(ocod_p):
        cols = _parquet_columns(ocod_p)
        addr_cols = [c for c in _OCOD_ADDR_COLS if c in cols]
        if {"name_clean", "jurisdiction_clean"}.issubset(cols) and addr_cols:
            try:
                df = pd.read_parquet(
                    ocod_p, columns=["name_clean", "jurisdiction_clean"] + addr_cols
                ).drop_duplicates(subset=["name_clean", "jurisdiction_clean"], keep="first")
                for rec in df.to_dict("records"):
                    key = (
                        str(rec.get("name_clean") or "").strip().upper(),
                        str(rec.get("jurisdiction_clean") or "").strip().upper(),
                    )
                    lines = [
                        _norm_space(rec[c]) for c in addr_cols
                        if _clean_str(rec.get(c)) is not None
                    ]
                    lines = [ln for ln in lines if ln]
                    text = ", ".join(lines) or None
                    ocod_map[key] = {
                        "lines": lines,
                        "text": text,
                        "postcode": _extract_uk_postcode(text),
                    }
            except Exception:
                ocod_map = {}

    roe_p = os.path.join(run_dir, "roe_preprocessed.parquet")
    if os.path.isfile(roe_p):
        cols = _parquet_columns(roe_p)
        addr_cols = [c for c in _ROE_ADDR_COLS if c in cols]
        if "roe_company_number" in cols and addr_cols:
            try:
                df = pd.read_parquet(
                    roe_p, columns=["roe_company_number"] + addr_cols
                ).drop_duplicates(subset=["roe_company_number"], keep="first")
                for rec in df.to_dict("records"):
                    key = str(rec.get("roe_company_number") or "").strip().upper()
                    if not key:
                        continue
                    line1 = _clean_str(rec.get("roe_address_line1"))
                    town = _clean_str(rec.get("roe_post_town"))
                    postcode = _clean_str(rec.get("roe_postcode"))
                    parts = [p for p in (line1, town, postcode) if p]
                    roe_map[key] = {
                        "line1": line1,
                        "post_town": town,
                        "postcode": postcode,
                        "text": ", ".join(parts) or None,
                    }
            except Exception:
                roe_map = {}

    return {"ocod": ocod_map, "roe": roe_map}


def _address_lookup(run_dir: str) -> dict:
    """Return the per-run address lookup, cached and invalidated by parquet mtime."""
    ocod_p = os.path.join(run_dir, "ocod_preprocessed.parquet")
    roe_p = os.path.join(run_dir, "roe_preprocessed.parquet")
    key = (
        os.path.getmtime(ocod_p) if os.path.isfile(ocod_p) else None,
        os.path.getmtime(roe_p) if os.path.isfile(roe_p) else None,
    )
    cached = _ADDRESS_CACHE.get(run_dir)
    if cached is not None and cached.get("key") == key:
        return cached
    lookup = _build_address_lookup(run_dir)
    lookup["key"] = key
    _ADDRESS_CACHE[run_dir] = lookup
    return lookup


def _address_payload(lookup: dict, record: dict) -> dict:
    """Additive per-row address block: both sides' lines plus the postcode_match /
    town_match convenience flags (true / false / null). No similarity score."""
    ocod_name = record.get("ocod_name_clean") or record.get("name_clean")
    ocod_key = (
        str(ocod_name or "").strip().upper(),
        str(record.get("jurisdiction_clean") or "").strip().upper(),
    )
    roe_key = str(record.get("roe_company_number") or "").strip().upper()

    ocod = lookup.get("ocod", {}).get(ocod_key)
    roe = lookup.get("roe", {}).get(roe_key)

    ocod_postcode = ocod["postcode"] if ocod else None
    roe_postcode = roe["postcode"] if roe else None
    ocod_text = ocod["text"] if ocod else None
    roe_town = roe["post_town"] if roe else None

    return {
        "ocod": {
            "lines": ocod["lines"] if ocod else [],
            "text": ocod_text,
            "postcode": ocod_postcode,
        },
        "roe": {
            "line1": roe["line1"] if roe else None,
            "post_town": roe_town,
            "postcode": roe_postcode,
            "text": roe["text"] if roe else None,
        },
        "postcode_match": _postcode_match(ocod_postcode, roe_postcode),
        "town_match": _town_match(ocod_text, roe_town),
    }


def _to_python(val):
    """Convert numpy/pandas types to native Python for JSON serialization."""
    import numpy as np
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if pd.isna(val):
        return None
    return val


def _match_id(record: dict, bucket: str) -> str:
    ocod_id = record.get("ocod_unique_id") or record.get("unique_id") or record.get("ocod_name_clean") or record.get("ocod_name_raw") or "ocod"
    roe_id = record.get("roe_unique_id") or record.get("roe_company_number") or record.get("roe_name_clean") or "roe"
    return f"{bucket}:{ocod_id}:{roe_id}"


def _bucket_counts(run_dir: str) -> dict:
    counts = {}
    for bname, fname in _BUCKET_FILES.items():
        path = os.path.join(run_dir, fname)
        if not os.path.isfile(path):
            counts[bname] = 0
            continue
        try:
            counts[bname] = max(0, sum(1 for _ in open(path, encoding="utf-8-sig")) - 1)
        except OSError:
            counts[bname] = 0
    counts["all"] = counts.get("high", 0) + counts.get("review", 0) + counts.get("ambiguous", 0)
    return counts

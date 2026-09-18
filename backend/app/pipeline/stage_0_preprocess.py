"""Stage 0: Preprocess OCOD and Companies House data for linkage.

Adapted from ``matching roe ocod/src/stage_0_preprocess.py``.  All hardcoded
paths (PROJECT_ROOT, OUTPUT_DIR, find_zip) have been replaced by explicit
parameters passed into :func:`run_stage_0`.
"""

import time
import zipfile
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from app.pipeline.standardise import (
    UnmappedJurisdictionsError,
    apply_name_rules,
    extract_digit_set,
    extract_name_core,
    extract_sorted_tokens,
    load_jurisdiction_map,
    load_legal_entity_tokens,
    load_name_rules,
    standardise_jurisdiction,
    validate_jurisdiction_coverage,
)

tqdm.pandas()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _step(label, progress_callback=None):
    msg = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(msg, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


CH_NEEDED_COLS = [
    "CompanyName",
    "CompanyNumber",
    "RegAddress.AddressLine1",
    "RegAddress.PostCode",
    "RegAddress.PostTown",
    "CompanyCategory",
    "CompanyStatus",
    "CountryOfOrigin",
]

# Companies House records up to 10 previous names per company
# (``PreviousName_1.CompanyName`` .. ``PreviousName_10.CompanyName``).  A property
# is recorded in OCOD under the owner's name *at the time of acquisition*, which
# may since have changed, so the current ROE name can fail to match.  We read
# whichever of these slots are present (they are optional — older test fixtures
# and trimmed CH extracts omit them) and, in :func:`expand_roe_names`, treat each
# former name as an additional name the company may be matched on.  The
# associated change dates are intentionally *not* read: former names are matched
# as if they were separate companies, then re-attributed to the current company
# downstream (see Stage 3).
CH_PREVIOUS_NAME_COLS = [f"PreviousName_{i}.CompanyName" for i in range(1, 11)]


def _read_ch_columns(csv_file_handle) -> list[str]:
    header = pd.read_csv(csv_file_handle, nrows=0, dtype=str)
    return list(header.columns)


class _CsvSource:
    """Yield fresh file handles for CSV content, whether the input is a raw
    ``.csv`` or a ``.zip`` wrapping a single ``.csv``.

    Reviewers may upload either form (the OCOD/CH downloads are available both
    zipped and unzipped), so the loaders accept both rather than failing deep
    in the pipeline with ``BadZipFile``. ``open()`` returns a fresh readable
    each call, since the loaders read the header and body in separate passes.
    """

    def __init__(self, path: Path):
        self.path = path
        self._zip = None
        suffix = path.suffix.lower()
        if suffix == ".zip":
            self._zip = zipfile.ZipFile(path)
            csvs = [n for n in self._zip.namelist() if n.lower().endswith(".csv")]
            if not csvs:
                self._zip.close()
                raise RuntimeError(
                    f"{path.name} is a zip archive but contains no .csv file."
                )
            self._member = csvs[0]
        elif suffix != ".csv":
            raise RuntimeError(
                f"{path.name}: unsupported file type '{suffix or '(none)'}'. "
                "Input must be a .zip or .csv file."
            )

    def open(self):
        if self._zip is not None:
            return self._zip.open(self._member)
        return open(self.path, "rb")

    def close(self):
        if self._zip is not None:
            self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Data loaders  (accept .zip-wrapped or raw .csv inputs)
# ---------------------------------------------------------------------------

def load_roe(ch_path: Path, progress_callback=None) -> pd.DataFrame:
    _step(f"Loading Companies House data from {ch_path.name}...", progress_callback)
    _step(f"  Streaming in chunks; only loading {len(CH_NEEDED_COLS)} of 55 columns; filtering to OE-prefixed rows...", progress_callback)
    t0 = time.time()

    with _CsvSource(ch_path) as src:
        with src.open() as f:
            actual_cols = _read_ch_columns(f)

        col_map = {c.strip(): c for c in actual_cols}
        missing = [c for c in CH_NEEDED_COLS if c not in col_map]
        if missing:
            raise RuntimeError(f"CH CSV missing expected columns: {missing}")
        present_prev = [c for c in CH_PREVIOUS_NAME_COLS if c in col_map]
        usecols_raw = [col_map[c] for c in CH_NEEDED_COLS] + [col_map[c] for c in present_prev]
        if present_prev:
            _step(f"  Including {len(present_prev)} previous-name column(s) for former-name matching.", progress_callback)

        chunks = []
        total_rows = 0
        chunk_iter = pd.read_csv(
            src.open(),
            dtype=str,
            usecols=usecols_raw,
            chunksize=200_000,
        )
        for chunk in tqdm(chunk_iter, desc="  CH chunks", unit=" chunk"):
            chunk.columns = chunk.columns.str.strip()
            total_rows += len(chunk)
            oe_chunk = chunk[chunk["CompanyNumber"].str.startswith("OE", na=False)]
            if len(oe_chunk) > 0:
                chunks.append(oe_chunk)

    roe = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=CH_NEEDED_COLS)
    _step(f"  Total CH rows scanned: {total_rows:,}. ROE entries (OE-prefixed): {len(roe):,}  ({time.time()-t0:.1f}s)", progress_callback)

    rename_map = {
        "CompanyName": "roe_name_raw",
        "CompanyNumber": "roe_company_number",
        "CountryOfOrigin": "roe_jurisdiction_raw",
        "RegAddress.AddressLine1": "roe_address_line1",
        "RegAddress.PostCode": "roe_postcode",
        "RegAddress.PostTown": "roe_post_town",
        "CompanyCategory": "roe_category",
        "CompanyStatus": "roe_status",
    }
    # PreviousName_<n>.CompanyName -> roe_prev_name_<n> (kept by the roe_ filter
    # below so expand_roe_names can fan them out into former-name rows).
    for c in present_prev:
        idx = c.split("_", 1)[1].split(".")[0]
        rename_map[c] = f"roe_prev_name_{idx}"
    roe = roe.rename(columns=rename_map)
    keep_cols = [c for c in roe.columns if c.startswith("roe_")]
    return roe[keep_cols].reset_index(drop=True)


def expand_roe_names(roe: pd.DataFrame, progress_callback=None) -> pd.DataFrame:
    """Fan a one-row-per-company ROE table out to one row per *name variant*.

    A property is recorded in OCOD under the owner's name at acquisition time,
    which may since have changed.  To catch those, every company contributes one
    row for its current name plus one row for each non-empty previous name
    (``roe_prev_name_*``).  Every row keeps the company's ``roe_company_number``
    and a ``roe_current_name_raw`` pointer, and is tagged ``roe_name_type``
    (``"current"`` / ``"former"``).  Downstream the former-name rows behave like
    separate companies for matching, then collapse back onto the company (by
    number) for metrics and the merged dataset — see Stage 3.

    Former names equal to the company's current name (after upper/strip) and
    duplicate former variants within a company are dropped.  When no previous-name
    columns are present this is a no-op apart from adding the two new columns, so
    trimmed CH extracts still run end-to-end.
    """
    roe = roe.copy()
    roe["roe_current_name_raw"] = roe["roe_name_raw"]
    prev_cols = sorted(c for c in roe.columns if c.startswith("roe_prev_name_"))
    if not prev_cols:
        roe["roe_name_type"] = "current"
        return roe

    base_cols = [c for c in roe.columns if not c.startswith("roe_prev_name_")]
    current = roe[base_cols].copy()
    current["roe_name_type"] = "current"

    frames = [current]
    n_former_raw = 0
    for pc in prev_cols:
        prev = roe[base_cols].copy()
        prev["roe_name_raw"] = roe[pc].values
        prev["roe_name_type"] = "former"
        prev = prev[prev["roe_name_raw"].fillna("").astype(str).str.strip() != ""]
        if len(prev):
            n_former_raw += len(prev)
            frames.append(prev)

    out = pd.concat(frames, ignore_index=True)

    name_norm = out["roe_name_raw"].fillna("").astype(str).str.strip().str.upper()
    cur_norm = out["roe_current_name_raw"].fillna("").astype(str).str.strip().str.upper()
    # Drop former names identical to the company's current name (no new signal).
    out = out[~((out["roe_name_type"] == "former") & (name_norm == cur_norm))].copy()
    # Drop duplicate former variants within a company; keep "current" over "former"
    # when both normalise the same (sort puts 'current' first).
    out["_nn"] = out["roe_name_raw"].fillna("").astype(str).str.strip().str.upper()
    out = (
        out.sort_values("roe_name_type")
        .drop_duplicates(subset=["roe_company_number", "_nn"], keep="first")
        .drop(columns="_nn")
        .reset_index(drop=True)
    )
    n_former = int((out["roe_name_type"] == "former").sum())
    _step(
        f"  Expanded ROE names: {len(roe):,} companies -> {len(out):,} name rows "
        f"({n_former:,} usable former-name variants, {n_former_raw:,} before dedupe).",
        progress_callback,
    )
    return out


# OCOD records up to 4 proprietors per title, each with its own name / registration
# no. / category / country-incorporated / address block ("Proprietor Name (2)",
# "Country Incorporated (2)", "Proprietor (2) Address (1)", ...). ~7% of titles have
# a second-or-later proprietor; those co-owners were never matched when only slot 1
# was read. :func:`expand_ocod_proprietors` fans the wide table out to one row per
# (title, proprietor) so each proprietor is standardised and matched like its own
# record — the OCOD analogue of :func:`expand_roe_names`.
OCOD_SHARED_RENAME = {
    "Title Number": "title_number",
    "Property Address": "property_address",
    "Postcode": "property_postcode",
    "District": "district",
    "County": "county",
    "Region": "region",
    "Tenure": "tenure",
    "Price Paid": "price_paid",
    "Date Proprietor Added": "date_proprietor_added",
    "Multiple Address Indicator": "multiple_address_indicator",
    "Additional Proprietor Indicator": "additional_proprietor_indicator",
}

# Canonical per-proprietor field names (proprietor 1 keeps these unsuffixed; slots
# 2-4 are read into "<field>_<n>" and un-suffixed back to these when fanned out).
OCOD_PROPRIETOR_FIELDS = [
    "ocod_name_raw", "ocod_reg_no", "ocod_category", "ocod_jurisdiction_raw",
    "ocod_address_1", "ocod_address_2", "ocod_address_3",
]

MAX_PROPRIETORS = 4


def _proprietor_rename(n: int) -> dict[str, str]:
    """Source-CSV -> destination column map for proprietor slot *n*.

    Slot 1 maps to the canonical unsuffixed names; slots 2-4 map to ``<field>_<n>``
    so :func:`expand_ocod_proprietors` can un-suffix them per slot.
    """
    suffix = "" if n == 1 else f"_{n}"
    return {
        f"Proprietor Name ({n})": f"ocod_name_raw{suffix}",
        f"Company Registration No. ({n})": f"ocod_reg_no{suffix}",
        f"Proprietorship Category ({n})": f"ocod_category{suffix}",
        f"Country Incorporated ({n})": f"ocod_jurisdiction_raw{suffix}",
        f"Proprietor ({n}) Address (1)": f"ocod_address_1{suffix}",
        f"Proprietor ({n}) Address (2)": f"ocod_address_2{suffix}",
        f"Proprietor ({n}) Address (3)": f"ocod_address_3{suffix}",
    }


def load_ocod(ocod_path: Path, progress_callback=None) -> pd.DataFrame:
    _step(f"Loading OCOD data from {ocod_path.name}...", progress_callback)
    t0 = time.time()
    with _CsvSource(ocod_path) as src:
        with src.open() as f:
            df = pd.read_csv(f, dtype=str, low_memory=False)

    _step(f"  Total OCOD rows: {len(df):,}  ({time.time()-t0:.1f}s)", progress_callback)

    rename_map = dict(OCOD_SHARED_RENAME)
    for n in range(1, MAX_PROPRIETORS + 1):
        rename_map.update(_proprietor_rename(n))
    ocod = df.rename(columns=rename_map)

    # Keep the shared columns plus every proprietor slot that is actually present
    # (trimmed test fixtures ship only slot 1). Column order preserved from the map.
    keep_cols = [c for c in rename_map.values() if c in ocod.columns]
    return ocod[keep_cols].reset_index(drop=True)


def expand_ocod_proprietors(ocod: pd.DataFrame, progress_callback=None) -> pd.DataFrame:
    """Fan the wide one-row-per-title OCOD table out to one row per proprietor.

    Each title contributes one row per non-empty proprietor slot (1-4), tagged with a
    1-based ``proprietor_index``. The shared property columns are copied onto every
    row; the slot's own name / registration no. / jurisdiction / address columns are
    un-suffixed onto the canonical ``ocod_*`` names, so downstream cleaning, dedup and
    matching treat each co-owner like its own record. Slots whose proprietor name is
    empty are skipped (most titles only fill slot 1). Mirrors :func:`expand_roe_names`.
    """
    shared_cols = [c for c in OCOD_SHARED_RENAME.values() if c in ocod.columns]
    frames = []
    for n in range(1, MAX_PROPRIETORS + 1):
        suffix = "" if n == 1 else f"_{n}"
        name_col = f"ocod_name_raw{suffix}"
        if name_col not in ocod.columns:
            continue
        src_to_canonical = {
            f"{field}{suffix}": field
            for field in OCOD_PROPRIETOR_FIELDS
            if f"{field}{suffix}" in ocod.columns
        }
        block = ocod[shared_cols + list(src_to_canonical.keys())].rename(columns=src_to_canonical)
        block = block[block["ocod_name_raw"].fillna("").astype(str).str.strip() != ""].copy()
        if len(block):
            block["proprietor_index"] = n
            frames.append(block)

    if not frames:
        cols = shared_cols + OCOD_PROPRIETOR_FIELDS + ["proprietor_index"]
        return pd.DataFrame(columns=cols)

    out = pd.concat(frames, ignore_index=True)
    if "title_number" in out.columns:
        out = out.sort_values(
            ["title_number", "proprietor_index"], kind="stable"
        ).reset_index(drop=True)
    n_extra = int((out["proprietor_index"] > 1).sum())
    _step(
        f"  Expanded OCOD proprietors: {len(ocod):,} titles -> {len(out):,} proprietor rows "
        f"({n_extra:,} co-owner rows beyond proprietor 1).",
        progress_callback,
    )
    return out


def standardise_dataframe(
    df: pd.DataFrame,
    name_col: str,
    jurisdiction_col: str,
    dataset: str,
    jurisdiction_map: dict,
    name_rules: list[dict],
    entity_tokens: set,
    progress_callback=None,
) -> pd.DataFrame:
    _step(f"  Standardising {dataset.upper()} ({len(df):,} rows)...", progress_callback)
    df[name_col] = df[name_col].fillna("").str.strip().str.upper()
    df[jurisdiction_col] = df[jurisdiction_col].fillna("").str.strip().str.upper()

    df["jurisdiction_clean"] = df[jurisdiction_col].apply(
        lambda v: standardise_jurisdiction(v, dataset, jurisdiction_map)
    )

    unique_names = df[name_col].unique()
    name_cache = {}
    for n in tqdm(unique_names, desc=f"  {dataset.upper()} name rules", unit=" names"):
        name_cache[n] = apply_name_rules(n, name_rules)
    df["name_clean"] = df[name_col].map(name_cache)
    df["name_digits_sorted"] = df["name_clean"].apply(extract_digit_set)
    df["name_core"] = df["name_clean"].apply(lambda n: extract_name_core(n, entity_tokens))
    df["name_tokens_sorted"] = df["name_clean"].apply(extract_sorted_tokens)
    return df


def write_standardisation_report(
    ocod: pd.DataFrame, roe: pd.DataFrame, output_path: Path
):
    lines = ["=== Standardisation Report ===\n"]

    lines.append(f"OCOD rows: {len(ocod):,}")
    lines.append(f"ROE rows: {len(roe):,}\n")

    for label, df, raw_col in [
        ("OCOD", ocod, "ocod_name_raw"),
        ("ROE", roe, "roe_name_raw"),
    ]:
        lines.append(f"--- {label} Name Standardisation (first 20 changes) ---")
        changed = df[df[raw_col].str.upper() != df["name_clean"]]
        for _, row in changed.head(20).iterrows():
            lines.append(f"  {row[raw_col]} -> {row['name_clean']}")
        lines.append(f"  Total changed: {len(changed):,} / {len(df):,}\n")

    for label, df, raw_col in [
        ("OCOD", ocod, "ocod_jurisdiction_raw"),
        ("ROE", roe, "roe_jurisdiction_raw"),
    ]:
        lines.append(f"--- {label} Jurisdiction Mapping (changes only, first 20) ---")
        changed = df[df[raw_col].str.upper() != df["jurisdiction_clean"]]
        for _, row in changed.head(20).iterrows():
            lines.append(f"  {row[raw_col]} -> {row['jurisdiction_clean']}")
        lines.append(f"  Total changed: {len(changed):,} / {len(df):,}\n")

    unknowns_ocod = ocod[ocod["jurisdiction_clean"] == "UNKNOWN"]
    unknowns_roe = roe[roe["jurisdiction_clean"] == "UNKNOWN"]
    lines.append(f"OCOD rows mapped to UNKNOWN: {len(unknowns_ocod):,}")
    lines.append(f"ROE rows mapped to UNKNOWN: {len(unknowns_roe):,}\n")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Standardisation report written to {output_path}")


# ---------------------------------------------------------------------------
# Stage entry-point
# ---------------------------------------------------------------------------

def run_stage_0(
    run_dir: str,
    config_dir: str,
    ocod_zip: str = None,
    ch_zip: str = None,
    progress_callback=None,
):
    """Run Stage 0: preprocess OCOD and CH data.

    Parameters
    ----------
    run_dir : str
        Directory for writing output files (parquet, reports).
    config_dir : str
        Directory containing ``name_rules.json``, ``legal_entity_tokens.json``,
        ``jurisdiction_map.csv``.
    ocod_zip : str
        Path to the OCOD zip file.
    ch_zip : str
        Path to the Companies House zip file.
    progress_callback : callable, optional
        ``(event: str, detail: dict) -> None`` called at key milestones.
    """
    t_start = time.time()
    run_dir = Path(run_dir)
    config_dir = Path(config_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if progress_callback:
        progress_callback("stage_start", {"stage": 0, "name": "preprocess"})

    _step("Loading config files...", progress_callback)
    jurisdiction_map = load_jurisdiction_map(config_dir)
    name_rules = load_name_rules(config_dir)
    entity_tokens = load_legal_entity_tokens(config_dir)

    if not ocod_zip or not ch_zip:
        raise RuntimeError("Stage 0 requires both ocod_zip and ch_zip paths.")

    roe = load_roe(Path(ch_zip), progress_callback)
    roe = expand_roe_names(roe, progress_callback)
    ocod = load_ocod(Path(ocod_zip), progress_callback)
    ocod = expand_ocod_proprietors(ocod, progress_callback)

    _step("Validating jurisdiction coverage...", progress_callback)
    ocod_jurisdictions = set(ocod["ocod_jurisdiction_raw"].fillna("").str.strip().str.upper()) - {""}
    roe_jurisdictions = set(roe["roe_jurisdiction_raw"].fillna("").str.strip().str.upper()) - {""}

    unmapped_ocod = validate_jurisdiction_coverage(ocod_jurisdictions, "ocod", jurisdiction_map)
    unmapped_roe = validate_jurisdiction_coverage(roe_jurisdictions, "roe", jurisdiction_map)

    if unmapped_ocod or unmapped_roe:
        raise UnmappedJurisdictionsError(unmapped_ocod, unmapped_roe)

    roe = standardise_dataframe(
        roe, "roe_name_raw", "roe_jurisdiction_raw", "roe",
        jurisdiction_map, name_rules, entity_tokens, progress_callback,
    )
    ocod = standardise_dataframe(
        ocod, "ocod_name_raw", "ocod_jurisdiction_raw", "ocod",
        jurisdiction_map, name_rules, entity_tokens, progress_callback,
    )

    _step("Writing standardisation report...", progress_callback)
    write_standardisation_report(ocod, roe, run_dir / "standardisation_report.txt")

    _step("Deduplicating OCOD by (name, jurisdiction)...", progress_callback)
    # Dedup for linkage is keyed on (name_clean, jurisdiction_clean) across ALL
    # proprietor rows. Carry a representative raw (name, jurisdiction) — the first
    # occurrence per deduped entity — so raw-first label resolution works off the
    # dedup/phase-2 pools too (the GBT-training path reads these), not just the
    # cleaned key. See label_resolver: raw values are the durable identity.
    ocod_dedup = (
        ocod[[
            "name_clean", "jurisdiction_clean", "name_digits_sorted", "name_core",
            "name_tokens_sorted", "ocod_name_raw", "ocod_jurisdiction_raw",
        ]]
        .drop_duplicates(subset=["name_clean", "jurisdiction_clean"])
        .reset_index(drop=True)
    )
    ocod_dedup["unique_id"] = range(len(ocod_dedup))
    _step(f"  OCOD deduplicated: {len(ocod):,} proprietor rows -> {len(ocod_dedup):,} unique (name, jurisdiction) pairs", progress_callback)

    roe["unique_id"] = range(len(roe))

    _step("Writing parquet outputs...", progress_callback)
    roe.to_parquet(run_dir / "roe_preprocessed.parquet", index=False)
    ocod_dedup.to_parquet(run_dir / "ocod_dedup.parquet", index=False)
    ocod.to_parquet(run_dir / "ocod_preprocessed.parquet", index=False)

    elapsed = time.time() - t_start
    _step(f"Stage 0 complete in {elapsed:.1f}s.", progress_callback)
    print(f"  roe_preprocessed.parquet: {len(roe):,} rows")
    print(f"  ocod_dedup.parquet: {len(ocod_dedup):,} rows (for Splink)")
    print(f"  ocod_preprocessed.parquet: {len(ocod):,} rows (full, for join-back)")

    if progress_callback:
        progress_callback("stage_end", {
            "stage": 0,
            "name": "preprocess",
            "elapsed_seconds": round(elapsed, 1),
            "roe_rows": len(roe),
            "ocod_rows": len(ocod),
            "ocod_dedup_rows": len(ocod_dedup),
        })

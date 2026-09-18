# backend/app/services/label_resolver.py
"""Single source of truth for resolving labels <-> entity data.

The review-panel display (``match_reader``), the export applier
(``label_applier``) and GBT training (``gbt_train``) must agree on which
``(name, jurisdiction)`` keys identify the same OCOD entity, and in what order
to try them. Centralising that logic here prevents display/export/training
divergence — e.g. a label that is honoured in the export but not shown in the
review panel, which made a label look "missing" after a cleaning-rule change
and invited an accidental overwrite.

RAW values are the durable identity: cleaning rules are editable config, so a
stored cleaned key can go stale, but the raw source values never change for
the same records. Resolution therefore tries the raw key FIRST and keeps the
stored cleaned key as the fallback for legacy rows missing raw values.
"""

from typing import Optional

import pandas as pd

from app.db import query_db

# Column aliases used by run artifacts (merged_dataset.csv uses ``name_clean``,
# the matches CSVs use ``ocod_name_clean``; only some frames carry raw columns).
_NAME_RAW_COLS = ["ocod_name_raw"]
_NAME_CLEAN_COLS = ["ocod_name_clean", "name_clean"]
_JUR_RAW_COLS = ["ocod_jurisdiction_raw"]
_JUR_CLEAN_COLS = ["jurisdiction_clean"]


def norm(value) -> str:
    return str(value or "").strip().upper()


def _series_norm(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.upper()


def entity_key_variants(name_clean, jur_clean, name_raw, jur_raw) -> list[tuple[str, str]]:
    """Ordered, unique, non-empty ``(name, jurisdiction)`` key variants to try.

    Raw key first (the durable identity), then mixed, then the cleaned key as
    the legacy fallback. Order matters: the first variant that resolves wins.
    """
    nc, jc = norm(name_clean), norm(jur_clean)
    nr, jr = norm(name_raw), norm(jur_raw)
    candidates = [(nr, jr), (nr, jc), (nc, jc), (nc, jr)]
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for name, jur in candidates:
        if name and jur and (name, jur) not in seen:
            seen.add((name, jur))
            out.append((name, jur))
    return out


def lookup_active_label(
    db_path: str,
    name_clean,
    jur_clean,
    roe_company_number,
    name_raw=None,
    jur_raw=None,
) -> Optional[dict]:
    """Return the active label row for an entity/ROE pair, or ``None``.

    Tries each entity-key variant of the *record* (raw first) against BOTH the
    raw and cleaned key columns of the *label* — so a label survives a
    cleaning-rule change as long as either side still lines up.
    """
    roe = norm(roe_company_number)
    if not roe:
        return None
    for name, jur in entity_key_variants(name_clean, jur_clean, name_raw, jur_raw):
        rows = query_db(
            db_path,
            """SELECT * FROM labels
               WHERE active = 1
                 AND upper(trim(roe_company_number)) = ?
                 AND (
                       (upper(trim(ocod_name_raw)) = ?   AND upper(trim(ocod_jurisdiction_raw)) = ?)
                    OR (upper(trim(ocod_name_clean)) = ? AND upper(trim(jurisdiction_clean)) = ?)
                 )
               LIMIT 1""",
            (roe, name, jur, name, jur),
        )
        if rows:
            return rows[0]
    return None


def load_active_label_index(db_path: str) -> dict:
    """Load all active labels once into an in-memory index keyed by every
    (name, jurisdiction, roe) variant (raw and clean). Lets a grouped view
    resolve thousands of candidates without an N+1 query storm."""
    rows = query_db(db_path, "SELECT * FROM labels WHERE active = 1")
    idx: dict = {}
    for r in rows:
        roe = norm(r.get("roe_company_number"))
        if not roe:
            continue
        for nm, jr in (
            (norm(r.get("ocod_name_raw")), norm(r.get("ocod_jurisdiction_raw"))),
            (norm(r.get("ocod_name_clean")), norm(r.get("jurisdiction_clean"))),
        ):
            if nm and jr:
                idx.setdefault((nm, jr, roe), r)
    return idx


def lookup_in_index(idx: dict, name_clean, jur_clean, roe_company_number,
                    name_raw=None, jur_raw=None) -> Optional[dict]:
    """Resolve a label from a prebuilt index (same key precedence as lookup_active_label)."""
    roe = norm(roe_company_number)
    if not roe:
        return None
    for name, jur in entity_key_variants(name_clean, jur_clean, name_raw, jur_raw):
        hit = idx.get((name, jur, roe))
        if hit:
            return hit
    return None


def build_frame_key_index(df: pd.DataFrame) -> dict[tuple[str, str], list]:
    """Index every row of a run frame under each of its available
    ``(name, jurisdiction)`` keys — raw and clean — for bulk label resolution.

    Build once per frame, then resolve each label with ``find_indexed_rows``:
    dict lookups instead of a mask scan per label. Values are lists of
    ``df.index`` labels in frame order.
    """
    name_norms = [_series_norm(df[c]) for c in _NAME_RAW_COLS + _NAME_CLEAN_COLS if c in df.columns]
    jur_norms = [_series_norm(df[c]) for c in _JUR_RAW_COLS + _JUR_CLEAN_COLS if c in df.columns]
    idx: dict[tuple[str, str], list] = {}
    for i in df.index:
        names = {s.at[i] for s in name_norms} - {""}
        jurs = {s.at[i] for s in jur_norms} - {""}
        for nm in names:
            for jr in jurs:
                idx.setdefault((nm, jr), []).append(i)
    return idx


def find_indexed_rows(index: dict[tuple[str, str], list], label: dict) -> list:
    """Find the rows of an indexed run frame for the OCOD entity of a ``label``.

    Raw key first (durable across cleaning-rule changes), stored cleaned key as
    the legacy fallback — the first variant with any rows wins.

    The ROE number is intentionally *not* part of this lookup: a review label
    often points at a candidate not yet in merged_dataset.csv, so we first find
    the OCOD entity, then the caller adds/switches/removes the ROE value.
    """
    for key in entity_key_variants(
        label.get("ocod_name_clean"),
        label.get("jurisdiction_clean"),
        label.get("ocod_name_raw"),
        label.get("ocod_jurisdiction_raw"),
    ):
        rows = index.get(key)
        if rows:
            return list(rows)
    return []


def find_dataframe_rows(df: pd.DataFrame, label: dict) -> list:
    """One-shot ``find_indexed_rows`` for a single label.

    Callers resolving many labels against one frame should build the index once
    with ``build_frame_key_index`` and call ``find_indexed_rows`` per label.
    """
    return find_indexed_rows(build_frame_key_index(df), label)

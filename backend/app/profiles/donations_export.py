# backend/app/profiles/donations_export.py
"""The donations export: the original sheet, plus what this run decided.

The promise (`docs/ENTITIES.md`) is that the file gives back exactly what was
put in — every row, every column, in the original order — with five columns
appended. That includes the rows the loader threw away for having no
``DonorId``: they keep their place with the new cells blank, so a reviewer can
line the export up against their own copy without counting.

Two sheets follow: the aliases, and what the run was.

Speed matters here. The sheet is 94,141 rows; the writer streams them straight
out of the frame's numpy columns rather than building a second copy of the data
as Python objects.
"""

import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app import vocabulary
from app.profiles import export_provenance

# The five columns the export appends, in order.
NEW_COLUMNS = ["RecordID", "EntityID", "EntityBasis", "DonorStatusStandardNew",
               "DonorStatusBasis"]

# The retired-ID sheet. "alias" and "survivor" are retired words
# (docs/GLOSSARY.md); the sheet and its two headline columns say what they mean.
# The three columns after them keep their field names, because nothing on screen
# shows them and a script may already read them.
RETIRED_SHEET = "Retired IDs"
ALIAS_HEADER = ["retired_entity_id", "survivor_entity_id", "track", "retired_run",
                "retired_at"]
ALIAS_LABELS = ["Retired ID", "Now leads to", "track", "retired_run", "retired_at"]

# The sheet that explains every column this export adds.
HOW_TO_READ_SHEET = "How to read this file"

STATUS_COLUMN = "donor_status_std"


def record_ids_for(raw: pd.DataFrame) -> pd.Series:
    """The ``record_id`` each raw row belongs to, blank where the loader dropped it.

    The same rule ``build_records`` uses, so the export and the run always agree
    about which donation belongs to which donor.
    """
    from app.profiles.donations import _column, _id_string

    donor_id = _column(raw, "DonorId").map(_id_string)
    status = _column(raw, "DonorStatus").map(_id_string)
    is_trust = status.str.lower() == "trust"
    return donor_id.where(~is_trust, "TR" + donor_id).where(donor_id != "", "")


def _lookup(entities: pd.DataFrame) -> dict:
    """``{record_id: (entity_id, basis, status, status_basis)}``."""
    if entities is None or not len(entities):
        return {}
    status = f"{STATUS_COLUMN}_entity"
    status_basis = f"{STATUS_COLUMN}_entity_basis"
    frame = entities.copy()
    frame["record_id"] = frame["record_id"].astype(str)
    for column in (status, status_basis):
        if column not in frame.columns:
            frame[column] = None
    return {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in zip(
            frame["record_id"].to_numpy(),
            frame["entity_id"].to_numpy(),
            frame["entity_basis"].to_numpy(),
            frame[status].to_numpy(),
            frame[status_basis].to_numpy(),
        )
    }


def _cell(value):
    """A value Excel and CSV can both take, with nothing silently changed."""
    if value is None:
        return None
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        stamp = pd.Timestamp(value)
        return None if pd.isna(stamp) else stamp.to_pydatetime()
    if value is pd.NaT:
        return None
    # Excel takes text, numbers, booleans and dates. A pandas extension value or
    # a numpy scalar of some other kind is written as its text, which is what a
    # reader would see in the original sheet anyway.
    if isinstance(value, (str, int, float, bool, datetime)):
        return value
    if isinstance(value, date):
        return value
    try:
        return None if pd.isna(value) else str(value)
    except (TypeError, ValueError):
        return str(value)


def _basis_label(value) -> str | None:
    """The raw ``entity_basis`` as the one label the screens use."""
    mapped = vocabulary.provenance_for("entity_basis", value)
    return mapped["label"] if mapped else (None if value is None else _cell(value))


def _value_basis_label(value) -> str | None:
    """The raw attribute basis as the one label the screens use."""
    meta = vocabulary.VALUE_BASIS.get(str(value or ""))
    return meta["label"] if meta else (None if value is None else _cell(value))


def _rows(raw: pd.DataFrame, entities: pd.DataFrame):
    """Yield each original row, in order, with the five new cells on the end.

    ``EntityBasis`` and ``DonorStatusBasis`` keep their column names — the
    owner's spreadsheets read them — and carry the canonical labels as their
    values, so the file says "Earlier grouping" where the screen does, not
    ``import`` (docs/BACKEND_STRINGS.md §6).
    """
    lookup = _lookup(entities)
    record_ids = record_ids_for(raw).to_numpy()
    columns = [raw[column].to_numpy() for column in raw.columns]
    blank = (None, None, None, None)

    for index in range(len(raw)):
        record_id = record_ids[index]
        entity_id, basis, status, status_basis = lookup.get(record_id, blank) \
            if record_id else blank
        yield [_cell(column[index]) for column in columns] + [
            record_id or None,
            _cell(entity_id),
            _basis_label(basis),
            _cell(status),
            _value_basis_label(status_basis),
        ]


def _run_info(context: dict, run_dir=None) -> list[list]:
    """What produced this run, in the words the screens use."""
    return export_provenance.run_rows(context, run_dir)


def write(run_dir, scope: str, fmt: str, context: dict) -> Path:
    """Write the export and return its path."""
    run_dir = Path(run_dir)
    raw = context["raw"]
    # The router hands it as a callable so a profile that never needs the
    # original file does not pay to read it.
    raw = raw() if callable(raw) else raw
    entities = context.get("entities")
    context = {**context, "exported_at": context.get("exported_at")
               or datetime.now(timezone.utc).isoformat()}

    out = run_dir / f"export_{scope}.{'xlsx' if fmt == 'xlsx' else 'csv'}"
    if fmt == "xlsx":
        _write_xlsx(out, raw, entities, context, run_dir)
    else:
        _write_csv(out, raw, entities)
        # A CSV has one sheet, so the other three go beside it. The content is
        # the same; only the container differs.
        _write_sidecars(out, context, run_dir)
    return out


def _write_xlsx(path: Path, raw: pd.DataFrame, entities, context: dict,
                run_dir=None) -> None:
    from openpyxl import Workbook

    # Write-only mode streams each row to the file instead of holding a cell
    # object per value; on 94,141 rows that is the difference between seconds
    # and minutes.
    book = Workbook(write_only=True)
    sheet = book.create_sheet(title="donations")
    sheet.append(list(raw.columns) + NEW_COLUMNS)
    for row in _rows(raw, entities):
        sheet.append(row)

    retired = book.create_sheet(title=RETIRED_SHEET)
    retired.append(ALIAS_LABELS)
    for alias in context.get("aliases") or []:
        retired.append([alias.get(key) for key in ALIAS_HEADER])

    info = book.create_sheet(title="run")
    for row in _run_info(context, run_dir):
        info.append(row)

    guide = book.create_sheet(title=HOW_TO_READ_SHEET)
    for row in export_provenance.how_to_read_rows(
            export_provenance.DONATIONS_ADDED_COLUMNS):
        guide.append(row)

    book.save(path)


def _write_csv(path: Path, raw: pd.DataFrame, entities) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(raw.columns) + NEW_COLUMNS)
        writer.writerows(_rows(raw, entities))


def _write_sidecars(out: Path, context: dict, run_dir=None) -> list[Path]:
    """The three sheets a CSV cannot hold, written beside it as their own files."""
    written = []
    run_sheet = export_provenance.sidecar_path(out, "_run_sheet.txt")
    run_sheet.write_text(
        export_provenance.as_text(_run_info(context, run_dir),
                                  "What produced this file"),
        encoding="utf-8")
    written.append(run_sheet)

    guide = export_provenance.sidecar_path(out, "_how_to_read.txt")
    guide.write_text(
        export_provenance.as_text(
            export_provenance.how_to_read_rows(
                export_provenance.DONATIONS_ADDED_COLUMNS)),
        encoding="utf-8")
    written.append(guide)

    retired = export_provenance.sidecar_path(out, "_retired_ids.csv")
    with open(retired, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(ALIAS_LABELS)
        for alias in context.get("aliases") or []:
            writer.writerow([alias.get(key) for key in ALIAS_HEADER])
    written.append(retired)
    return written

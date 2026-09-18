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

# The five columns the export appends, in order.
NEW_COLUMNS = ["RecordID", "EntityID", "EntityBasis", "DonorStatusStandardNew",
               "DonorStatusBasis"]

ALIAS_HEADER = ["retired_entity_id", "survivor_entity_id", "track", "retired_run",
                "retired_at"]

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


def _rows(raw: pd.DataFrame, entities: pd.DataFrame):
    """Yield each original row, in order, with the five new cells on the end."""
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
            _cell(basis),
            _cell(status),
            _cell(status_basis),
        ]


def _run_info(context: dict) -> list[list]:
    counts = context.get("counts") or {}
    info = [
        ["Run", context.get("run_id")],
        ["Config version", context.get("config_version")],
        ["Scope", context.get("scope")],
        ["Exported at", context.get("exported_at")],
        ["Input file", context.get("input_name")],
        ["", ""],
        ["Count", "Value"],
    ]
    for key in sorted(counts):
        value = counts[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        info.append([key, value])
    return info


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
        _write_xlsx(out, raw, entities, context)
    else:
        _write_csv(out, raw, entities)
    return out


def _write_xlsx(path: Path, raw: pd.DataFrame, entities, context: dict) -> None:
    from openpyxl import Workbook

    # Write-only mode streams each row to the file instead of holding a cell
    # object per value; on 94,141 rows that is the difference between seconds
    # and minutes.
    book = Workbook(write_only=True)
    sheet = book.create_sheet(title="donations")
    sheet.append(list(raw.columns) + NEW_COLUMNS)
    for row in _rows(raw, entities):
        sheet.append(row)

    aliases = book.create_sheet(title="aliases")
    aliases.append(ALIAS_HEADER)
    for alias in context.get("aliases") or []:
        aliases.append([alias.get(key) for key in ALIAS_HEADER])

    info = book.create_sheet(title="run")
    for row in _run_info(context):
        info.append(row)

    book.save(path)


def _write_csv(path: Path, raw: pd.DataFrame, entities) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(raw.columns) + NEW_COLUMNS)
        writer.writerows(_rows(raw, entities))

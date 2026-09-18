# backend/app/profiles/psc_export.py
"""The PSC export: what this run decided, plus the Elasticsearch update file.

Two things come out of a PSC run. A flat table of the decision, one row per PSC
record, which is what a person reads. And a bulk-update file for the
``ch-cred-pscs-v1`` index, which is what a machine reads — but the tool never
writes to Elasticsearch itself (D16). Sending it is a separate step, started by
hand, by someone who has looked at the table first.

The snapshot is not given back the way the donations sheet is: it is 13 GB, the
user already has it, and re-parsing it to add two columns would cost more than
the run did. The export is written from the run's own parquet instead.
"""

import csv
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# The index the PSC ids are keyed on (D16). The record_id IS the document id,
# which is why stable_psc_id may never change.
ES_INDEX = "ch-cred-pscs-v1"
ES_FIELD = "psc_entity_id"

COLUMNS = ["record_id", "entity_id", "entity_basis", "track", "company_number", "name"]

BULK_FILENAME = "elasticsearch_bulk.jsonl"


def _decision_frame(run_dir: Path, entities: pd.DataFrame) -> pd.DataFrame:
    """One row per PSC record: who it is, and what the run decided."""
    records = pd.read_parquet(
        run_dir / "records.parquet",
        columns=[c for c in ("record_id", "track", "company_number", "name")],
    )
    records["record_id"] = records["record_id"].astype(str)

    decided = entities.copy()
    decided["record_id"] = decided["record_id"].astype(str)
    keep = [c for c in ("record_id", "entity_id", "entity_basis") if c in decided.columns]
    merged = records.merge(decided[keep], on="record_id", how="left")

    for column in COLUMNS:
        if column not in merged.columns:
            merged[column] = None
    return merged[COLUMNS].sort_values("record_id", kind="mergesort")


def _write_table(frame: pd.DataFrame, path: Path, fmt: str) -> None:
    if fmt == "parquet":
        frame.to_parquet(path, index=False)
        return
    # Streamed from the numpy columns rather than built as Python objects
    # first: 16 million rows would otherwise be a second copy of the run.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(zip(*(frame[c].to_numpy() for c in COLUMNS)))


def _write_bulk(frame: pd.DataFrame, path: Path) -> int:
    """The Elasticsearch bulk-update body. Returns how many records it updates.

    Two lines per record, the action then the document, which is what the bulk
    API takes. A record the run gave no entity id is left out rather than
    written with a null, because an update that blanks a live field is worse
    than no update.
    """
    written = 0
    with open(path, "w", encoding="utf-8") as handle:
        ids = frame["record_id"].to_numpy()
        entities = frame["entity_id"].to_numpy()
        bases = frame["entity_basis"].to_numpy()
        for record_id, entity_id, basis in zip(ids, entities, bases):
            if not isinstance(entity_id, str) or not entity_id:
                continue
            handle.write(json.dumps(
                {"update": {"_id": str(record_id), "_index": ES_INDEX}}
            ) + "\n")
            doc = {ES_FIELD: entity_id}
            if isinstance(basis, str) and basis:
                doc[f"{ES_FIELD}_basis"] = basis
            handle.write(json.dumps({"doc": doc}) + "\n")
            written += 1
    return written


def _readme(context: dict, frame: pd.DataFrame, updates: int) -> str:
    counts = context.get("counts") or {}
    return "\n".join([
        f"PSC reconciliation export — run {context.get('run_id')}",
        f"Written {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Config version {context.get('config_version')}",
        f"Scope: {context.get('scope')}",
        "",
        f"psc_entities.csv      {len(frame):,} PSC records, with the entity ID each was given",
        f"{BULK_FILENAME}  {updates:,} bulk updates for the {ES_INDEX} index",
        "aliases.csv           entity IDs that retired into another",
        "",
        "The bulk file is NOT sent by this tool. Check the table first, then post",
        f"the file to the {ES_INDEX} _bulk endpoint yourself.",
        "",
        f"Records loaded: {counts.get('records_total', 'n/a')}",
        f"Entities proposed: {counts.get('entities_total', 'n/a')}",
    ]) + "\n"


def export(run_dir: Path, scope: str, fmt: str, context: dict) -> Path:
    """Write the export and return its path.

    CSV and parquet both come back as a zip, because the Elasticsearch file and
    the aliases belong with the table and a reviewer should not have to ask for
    them separately.
    """
    run_dir = Path(run_dir)
    entities = context.get("entities")
    if entities is None:
        raise ValueError("The PSC export needs the run's entities frame")

    frame = _decision_frame(run_dir, entities)
    table_name = "psc_entities.parquet" if fmt == "parquet" else "psc_entities.csv"
    table_path = run_dir / table_name
    _write_table(frame, table_path, fmt)

    bulk_path = run_dir / BULK_FILENAME
    updates = _write_bulk(frame, bulk_path)

    aliases = context.get("aliases") or []
    alias_path = run_dir / "aliases.csv"
    with open(alias_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["retired_entity_id", "survivor_entity_id", "track",
                         "retired_run", "retired_at"])
        for row in aliases:
            writer.writerow([
                row.get("retired_entity_id"), row.get("survivor_entity_id"),
                row.get("track"), row.get("retired_run"), row.get("retired_at"),
            ])

    bundle = run_dir / f"psc_export_{scope}.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(table_path, table_name)
        archive.write(bulk_path, BULK_FILENAME)
        archive.write(alias_path, "aliases.csv")
        archive.writestr("README.txt", _readme(context, frame, updates))
    return bundle

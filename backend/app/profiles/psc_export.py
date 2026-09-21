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

**Out of core (B5).** The decision table is a DuckDB join of ``records.parquet``
to the entity proposal, projected to six columns and sorted in SQL. The parquet
form is written by ``COPY ... TO``; the CSV and the bulk file are written from
**batches** of that join, never from a frame of sixteen million rows. The batch
size comes from ``EXPORT_BATCH_ROWS``.
"""

import csv
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app import duckdb_conn
from app.profiles import export_provenance

# The index the PSC ids are keyed on (D16). The record_id IS the document id,
# which is why stable_psc_id may never change.
ES_INDEX = "ch-cred-pscs-v1"
ES_FIELD = "psc_entity_id"

COLUMNS = ["record_id", "entity_id", "entity_basis", "track", "company_number", "name"]

BULK_FILENAME = "elasticsearch_bulk.jsonl"

# "alias" is a retired word (docs/GLOSSARY.md). The file says what it holds.
RETIRED_FILENAME = "retired_ids.csv"

#: How many rows leave DuckDB at a time when a file has to be written a line at
#: a time. Writing JSON and writing a byte-order mark are the two steps that
#: cannot be SQL, so they are the two that are batched. Small enough that the
#: batch is never the memory ceiling, large enough that the per-batch cost
#: disappears.
BATCH_ROWS_ENV = "EXPORT_BATCH_ROWS"
DEFAULT_BATCH_ROWS = 200_000


def batch_rows() -> int:
    """The export's batch size, from ``EXPORT_BATCH_ROWS``."""
    try:
        value = int(os.environ.get(BATCH_ROWS_ENV, DEFAULT_BATCH_ROWS))
    except ValueError:
        return DEFAULT_BATCH_ROWS
    return value if value > 0 else DEFAULT_BATCH_ROWS


def _literal(path) -> str:
    """A path as a SQL string literal. A view cannot carry a bound parameter."""
    return "'" + str(path).replace("'", "''") + "'"


def _decision_sql(con, run_dir: Path, entities) -> str:
    """One row per PSC record: who it is, and what the run decided.

    *entities* is the proposal — a frame, or the path of ``entities.parquet``.
    """
    if isinstance(entities, pd.DataFrame):
        con.register("psc_entities_in", entities)
        con.execute("CREATE OR REPLACE VIEW psc_decided AS SELECT * FROM psc_entities_in")
    else:
        con.execute(
            "CREATE OR REPLACE VIEW psc_decided AS "
            f"SELECT * FROM read_parquet({_literal(entities)})"
        )
    con.execute(
        "CREATE OR REPLACE VIEW psc_records AS "
        f"SELECT * FROM read_parquet({_literal(run_dir / 'records.parquet')})"
    )
    record_columns = {d[0] for d in
                      con.execute("SELECT * FROM psc_records LIMIT 0").description}
    decided_columns = {d[0] for d in
                       con.execute("SELECT * FROM psc_decided LIMIT 0").description}

    def _from(source: str, available: set, name: str) -> str:
        if name not in available:
            return f'CAST(NULL AS VARCHAR) AS "{name}"'
        return f'{source}."{name}"'

    select = ", ".join([
        "CAST(r.record_id AS VARCHAR) AS record_id",
        _from("e", decided_columns, "entity_id"),
        _from("e", decided_columns, "entity_basis"),
        _from("r", record_columns, "track"),
        _from("r", record_columns, "company_number"),
        _from("r", record_columns, "name"),
    ])
    return f"""
        SELECT {select}
        FROM psc_records r
        LEFT JOIN psc_decided e
               ON CAST(e.record_id AS VARCHAR) = CAST(r.record_id AS VARCHAR)
        ORDER BY CAST(r.record_id AS VARCHAR)
    """


def _batches(con, sql: str):
    """Yield the query's rows as tuples, ``batch_rows()`` at a time."""
    cursor = con.execute(sql)
    size = batch_rows()
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield rows


def _write_table(con, sql: str, path: Path, fmt: str) -> None:
    if fmt == "parquet":
        con.execute(f"COPY ({sql}) TO {_literal(path)} (FORMAT PARQUET)")
        return
    # Streamed out of DuckDB in batches rather than built as one frame first:
    # sixteen million rows would otherwise be a second copy of the run. The
    # file keeps its byte-order mark, which is why it is not a plain COPY.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for rows in _batches(con, sql):
            writer.writerows(rows)


def _write_bulk(con, sql: str, path: Path) -> int:
    """The Elasticsearch bulk-update body. Returns how many records it updates.

    Two lines per record, the action then the document, which is what the bulk
    API takes. A record the run gave no entity id is left out rather than
    written with a null, because an update that blanks a live field is worse
    than no update.
    """
    written = 0
    with open(path, "w", encoding="utf-8") as handle:
        for rows in _batches(con, sql):
            for row in rows:
                record_id, entity_id, basis = row[0], row[1], row[2]
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


def _manifest(context: dict, n_records: int, updates: int, table_name: str,
              run_dir=None) -> str:
    """The bundle's manifest: what is in it, and what produced it.

    The same facts the donations run sheet carries, as plain text, because a zip
    has no sheets (`docs/PROVENANCE.md`).
    """
    header = "\n".join([
        f"PSC reconciliation export — run {context.get('run_id')}",
        f"Written {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "What is in this bundle",
        "----------------------",
        f"{table_name}      {n_records:,} PSC records, with the entity ID each was given",
        f"{BULK_FILENAME}  {updates:,} bulk updates for the {ES_INDEX} index",
        f"{RETIRED_FILENAME}          entity IDs that retired, and what each now leads to",
        "HOW_TO_READ.txt       what every column means",
        "",
        "The bulk file is NOT sent by this tool. Check the table first, then post",
        f"the file to the {ES_INDEX} _bulk endpoint yourself.",
        "",
        "",
    ])
    return header + export_provenance.as_text(
        export_provenance.run_rows(context, run_dir))


def export(run_dir: Path, scope: str, fmt: str, context: dict) -> Path:
    """Write the export and return its path.

    CSV and parquet both come back as a zip, because the Elasticsearch file and
    the aliases belong with the table and a reviewer should not have to ask for
    them separately.
    """
    run_dir = Path(run_dir)
    entities = context.get("entities")
    if entities is None:
        entities = run_dir / "entities.parquet"
        if not entities.is_file():
            raise ValueError("The PSC export needs the run's entities frame")

    temp_dir = run_dir / "duckdb_tmp"
    con = duckdb_conn.connect(temp_dir)
    try:
        sql = _decision_sql(con, run_dir, entities)
        table_name = "psc_entities.parquet" if fmt == "parquet" else "psc_entities.csv"
        table_path = run_dir / table_name
        _write_table(con, sql, table_path, fmt)

        bulk_path = run_dir / BULK_FILENAME
        updates = _write_bulk(con, sql, bulk_path)
        n_records = int(con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0])
    finally:
        con.close()

    aliases = context.get("aliases") or []
    retired_path = run_dir / RETIRED_FILENAME
    with open(retired_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Retired ID", "Now leads to", "track",
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
        archive.write(retired_path, RETIRED_FILENAME)
        archive.writestr("README.txt",
                         _manifest(context, n_records, updates, table_name, run_dir))
        archive.writestr("HOW_TO_READ.txt", export_provenance.as_text(
            export_provenance.how_to_read_rows(
                export_provenance.PSC_ADDED_COLUMNS)))
    return bundle

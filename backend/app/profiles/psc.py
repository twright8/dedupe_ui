# backend/app/profiles/psc.py
"""PSC profile — Companies House people with significant control.

One record is one PSC statement about one company, so the same person appears
once per company they control. The job is to give those statements one entity
ID (D16). The snapshot is 13 GB of JSON lines inside a 2.2 GB zip, about 16
million records, so nothing here may hold the file, or the frame, twice.

How the file is read
--------------------
A thread decompresses the zip member with Python's own ``zipfile`` and writes
the bytes into a FIFO; DuckDB reads that FIFO as newline-delimited JSON and
writes parquet straight back out. Three reasons for a FIFO rather than piping
through ``/dev/stdin``:

* the decompression stays inside this process, so a run does not depend on an
  ``unzip`` binary being present on the server;
* DuckDB opens a FIFO by path exactly as it opens a file, so the projection is
  ordinary SQL and the 13 GB member never touches disk;
* stdin is a single global, and a run shares its process with the web server.

Each side opens the FIFO once and reads to EOF — reopening it per batch
deadlocks, because the writer blocks until a reader arrives and DuckDB has
already finished. DuckDB is given an explicit column schema, so a key missing
from the first rows cannot change how the rest of the file is read.
"""

import hashlib
import os
import re
import tempfile
import threading
import zipfile
from pathlib import Path

import duckdb

from app import duckdb_conn
import pandas as pd

from app.profiles.base import (
    DEFAULT_TRACKS,
    DisplayColumn,
    EvidenceFocus,
    InputSpec,
    LoadOptions,
    Profile,
)

# Kinds that must never reach a screen (D16). Dropped and counted.
DROP_KIND_PREFIX = "super-secure"

# How many records quick mode reads. The Config screen iterates rules against a
# sample; a full pass over 16 million records takes minutes.
DEFAULT_QUICK_ROWS = 200_000
QUICK_ROWS_ENV = "PSC_QUICK_ROWS"

# DuckDB is capped so a run cannot take the machine (deduping's D13: an uncapped
# connection took 80% of RAM and crashed the box twice).
DUCKDB_MEMORY_LIMIT = os.environ.get("PSC_DUCKDB_MEMORY", "2GB")
DUCKDB_THREADS = int(os.environ.get("PSC_DUCKDB_THREADS", "2"))
PARQUET_ROW_GROUP = 100_000

MEMBER_SUFFIXES = (".txt", ".jsonl", ".ndjson", ".json")

# A bare Companies House number is the strongest identifier there is, so it
# becomes the entity id itself (deduping ``registry/edges.py:_weight``).
GB_NUMBER_RE = re.compile(r"^(\d{8}|[A-Z]{2}\d{6})$")

RAW_COLUMNS = [
    "record_id", "name", "review_state", "existing_entity_id",
    "kind", "title", "forename", "middle_name", "surname", "preferred_name",
    "dob_year", "dob_month", "nationality", "country_of_residence",
    "premises", "address_line_1", "address_line_2", "locality", "region",
    "address_country", "postcode", "care_of", "po_box",
    "company_number", "notified_on", "ceased_on", "is_ceased",
    "natures_of_control", "n_companies",
    "registration_number", "legal_form", "legal_authority", "place_registered",
    "country_registered", "is_sanctioned",
]

# The evidence rows behind a record: the company this statement is about. A unit
# pools them, so a person's evidence reads as "the companies this person
# controls" (D13b).
EVENT_COLUMNS = [
    ("company_number", "Company", "text"),
    ("notified_on", "Notified", "text"),
    ("ceased_on", "Ceased", "text"),
    ("natures_of_control", "Control", "list"),
    ("kind", "Kind", "text"),
    ("postcode", "Postcode", "text"),
    ("locality", "Town", "text"),
]

PERSON_KINDS = (
    "individual-person-with-significant-control",
    "individual-beneficial-owner",
)
ORGANISATION_KINDS = (
    "corporate-entity-person-with-significant-control",
    "corporate-entity-beneficial-owner",
    "legal-person-person-with-significant-control",
    "legal-person-beneficial-owner",
)

EVIDENCE_FOCUS = [
    EvidenceFocus(
        "person", "Person",
        [{"column": "kind", "op": "in", "values": list(PERSON_KINDS)}],
        ["name", "dob_year", "nationality", "country_of_residence", "postcode"],
        ["company_number", "notified_on", "natures_of_control"],
    ),
    EvidenceFocus(
        "organisation", "Company or legal person",
        [{"column": "kind", "op": "in", "values": list(ORGANISATION_KINDS)}],
        ["name", "registration_number", "country_registered", "postcode"],
        ["company_number", "notified_on", "natures_of_control"],
    ),
    EvidenceFocus(
        "other", "PSC record", [],
        ["name", "postcode"], ["company_number", "notified_on"],
    ),
]


# ---------------------------------------------------------------------------
# Record identity — reproduces deduping's stable_psc_id exactly
# ---------------------------------------------------------------------------


def stable_psc_id(company_number: str, self_link: str | None, name: str | None = None,
                  kind: str | None = None, notified_on: str | None = None) -> str:
    """The record key, as ``deduping/pipeline/pull.py`` builds it.

    An Elasticsearch index is keyed on this, so it is not ours to improve: the
    last segment of ``links.self``, or a sha256 of the record's own fields when
    the link is missing.
    """
    link = self_link or ""
    if link:
        parts = link.rstrip("/").split("/")
        if len(parts) >= 2:
            return f"{company_number}_{parts[-1]}"
    digest = hashlib.sha256(
        f"{company_number}|{name or ''}|{kind or ''}|{notified_on or ''}".encode()
    ).hexdigest()[:16]
    return f"{company_number}_{digest}"


# The same rule as one SQL expression, so 16 million records never round-trip
# through Python. DuckDB's sha256 is hashlib's.
_RECORD_ID_SQL = """
CASE
  WHEN self_link IS NOT NULL AND self_link <> ''
       AND len(str_split(rtrim(self_link, '/'), '/')) >= 2
  THEN company_number || '_' || list_extract(
         str_split(rtrim(self_link, '/'), '/'),
         len(str_split(rtrim(self_link, '/'), '/')))
  ELSE company_number || '_' || substr(sha256(
         company_number || '|' || coalesce(name, '') || '|' ||
         coalesce(kind, '') || '|' || coalesce(notified_on, '')), 1, 16)
END
"""

_EXTRACT = """
SELECT
  company_number,
  json_extract_string(data, '$.kind')                       AS kind,
  json_extract_string(data, '$.name')                       AS name,
  json_extract_string(data, '$.links.self')                 AS self_link,
  json_extract_string(data, '$.name_elements.title')        AS title,
  json_extract_string(data, '$.name_elements.forename')     AS forename,
  json_extract_string(data, '$.name_elements.middle_name')  AS middle_name,
  json_extract_string(data, '$.name_elements.surname')      AS surname,
  json_extract_string(data, '$.identity_verification_details.preferred_name')
                                                            AS preferred_name,
  TRY_CAST(json_extract_string(data, '$.date_of_birth.year')  AS INTEGER) AS dob_year,
  TRY_CAST(json_extract_string(data, '$.date_of_birth.month') AS INTEGER) AS dob_month,
  json_extract_string(data, '$.nationality')                AS nationality,
  json_extract_string(data, '$.country_of_residence')       AS country_of_residence,
  json_extract_string(data, '$.address.premises')           AS premises,
  json_extract_string(data, '$.address.address_line_1')     AS address_line_1,
  json_extract_string(data, '$.address.address_line_2')     AS address_line_2,
  json_extract_string(data, '$.address.locality')           AS locality,
  json_extract_string(data, '$.address.region')             AS region,
  json_extract_string(data, '$.address.country')            AS address_country,
  json_extract_string(data, '$.address.postal_code')        AS postcode,
  json_extract_string(data, '$.address.care_of')            AS care_of,
  json_extract_string(data, '$.address.po_box')             AS po_box,
  json_extract_string(data, '$.notified_on')                AS notified_on,
  json_extract_string(data, '$.ceased_on')                  AS ceased_on,
  coalesce(TRY_CAST(json_extract_string(data, '$.ceased') AS BOOLEAN),
           json_extract_string(data, '$.ceased_on') IS NOT NULL) AS is_ceased,
  array_to_string(
    coalesce(TRY_CAST(json_extract(data, '$.natures_of_control') AS VARCHAR[]), []), ' | '
  )                                                          AS natures_of_control,
  json_extract_string(data, '$.identification.registration_number') AS registration_number,
  json_extract_string(data, '$.identification.legal_form')          AS legal_form,
  json_extract_string(data, '$.identification.legal_authority')     AS legal_authority,
  json_extract_string(data, '$.identification.place_registered')    AS place_registered,
  json_extract_string(data, '$.identification.country_registered')  AS country_registered,
  coalesce(TRY_CAST(json_extract_string(data, '$.is_sanctioned') AS BOOLEAN), FALSE)
                                                             AS is_sanctioned,
  coalesce(json_extract_string(data, '$.kind'), '') LIKE '{drop_prefix}%' AS dropped_kind,
  coalesce(trim(coalesce(json_extract_string(data, '$.name'), '')), '') = ''
    AND coalesce(trim(coalesce(
          json_extract_string(data, '$.name_elements.surname'), '')), '') = ''
                                                             AS dropped_no_name
FROM read_ndjson(
  ?, columns={{'company_number': 'VARCHAR', 'data': 'JSON'}},
  format='newline_delimited', maximum_object_size=16777216
)
"""

_PROJECTION = f"""
SELECT
  {_RECORD_ID_SQL} AS record_id,
  coalesce(nullif(trim(coalesce(name, '')), ''), surname) AS name,
  'unreviewed' AS review_state,
  CAST(NULL AS VARCHAR) AS existing_entity_id,
  kind, title, forename, middle_name, surname, preferred_name,
  dob_year, dob_month, nationality, country_of_residence,
  premises, address_line_1, address_line_2, locality, region,
  address_country, postcode, care_of, po_box,
  company_number, notified_on, ceased_on, is_ceased,
  natures_of_control,
  CAST(1 AS INTEGER) AS n_companies,
  registration_number, legal_form, legal_authority, place_registered,
  country_registered, is_sanctioned
FROM extracted
WHERE NOT dropped_kind AND NOT dropped_no_name
"""


# ---------------------------------------------------------------------------
# Streaming the snapshot
# ---------------------------------------------------------------------------


def _records_member(book: zipfile.ZipFile) -> str:
    """The JSON-lines member of the snapshot zip."""
    names = [
        i.filename for i in book.infolist()
        if not i.is_dir() and i.filename.lower().endswith(MEMBER_SUFFIXES)
    ]
    if not names:
        found = ", ".join(i.filename for i in book.infolist()) or "nothing"
        raise ValueError(
            f"The snapshot zip has no .txt / .jsonl / .json member. Found: {found}."
        )
    # The snapshot ships exactly one; the largest is the right one regardless.
    return max(names, key=lambda n: book.getinfo(n).file_size)


def _open_stream(path: Path):
    """``(reader, holder)`` over the record lines, whatever wraps them."""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        book = zipfile.ZipFile(path)
        return book.open(_records_member(book)), book
    if suffix == ".gz":
        import gzip

        return gzip.open(path, "rb"), None
    if suffix in MEMBER_SUFFIXES:
        return open(path, "rb"), None
    raise ValueError(
        f"'{path.name}' must be the snapshot .zip, or a .txt / .jsonl / .json.gz of "
        f"the same JSON lines (got '{suffix or 'no extension'}')."
    )


def _pump(path: Path, fifo: str, limit: int | None, box: dict) -> None:
    """Decompress into the FIFO. Runs in a thread; DuckDB is the reader.

    A failure is put in *box* rather than raised: a thread that died before
    opening the FIFO would leave DuckDB blocked on a writer that never comes.
    """
    source = holder = None
    try:
        source, holder = _open_stream(path)
        with open(fifo, "wb") as sink:
            if limit is None:
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    sink.write(chunk)
            else:
                for written, line in enumerate(source, start=1):
                    sink.write(line)
                    if written >= limit:
                        break
    except Exception as exc:  # noqa: BLE001 — handed to the caller through *box*
        box["error"] = exc
        try:  # let the reader see EOF rather than hang
            open(fifo, "wb").close()
        except OSError:
            pass
    finally:
        for handle in (source, holder):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass


def _connect(temp_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """The loader's own connection: a tighter memory cap, and a bounded spill.

    The memory limit here is deliberately lower than the shared default — the
    loader streams a 13 GB JSON member and does not need the scorer's headroom.
    The spill cap and the temp directory come from the shared helper, so this
    connection cannot fill the disk either.
    """
    con = duckdb.connect()
    con.execute(f"SET threads TO {DUCKDB_THREADS}")
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    if temp_dir is not None:
        temp_dir = Path(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{temp_dir}'")
    con.execute(f"SET max_temp_directory_size='{duckdb_conn.max_temp()}'")
    # The order of 16 million records carries no meaning, and preserving it
    # costs the whole frame in memory.
    con.execute("SET preserve_insertion_order=false")
    return con


def quick_rows(options: LoadOptions | None) -> int | None:
    """How many lines to read, or None for the whole file."""
    if options is None or not options.quick_mode:
        return None
    if options.quick_rows:
        return int(options.quick_rows)
    try:
        return int(os.environ.get(QUICK_ROWS_ENV, DEFAULT_QUICK_ROWS))
    except ValueError:
        return DEFAULT_QUICK_ROWS


def extract_to_parquet(input_path: Path, out_path: Path,
                       options: LoadOptions | None = None) -> dict:
    """Stream *input_path* into a records parquet at *out_path*, and count.

    Nothing is held in Python: the bytes go decompressor to FIFO to DuckDB to
    parquet, and this process keeps only the counts.
    """
    limit = quick_rows(options)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="psc-fifo-") as work:
        fifo = os.path.join(work, "records.ndjson")
        os.mkfifo(fifo)
        box: dict = {}
        pump = threading.Thread(
            target=_pump, args=(Path(input_path), fifo, limit, box), daemon=True
        )
        pump.start()

        con = _connect(Path(out_path).parent / "duckdb_tmp")
        try:
            # One scan: a FIFO can only be read once, so the counts and the
            # parquet both come from this table.
            con.execute(
                f"CREATE TEMP TABLE extracted AS "
                f"{_EXTRACT.format(drop_prefix=DROP_KIND_PREFIX)}",
                [fifo],
            )
            pump.join()
            if "error" in box:
                raise box["error"]

            read, dropped_kind, dropped_name = con.execute("""
                SELECT count(*),
                       count(*) FILTER (WHERE dropped_kind),
                       count(*) FILTER (WHERE NOT dropped_kind AND dropped_no_name)
                FROM extracted
            """).fetchone()
            con.execute(
                f"COPY ({_PROJECTION}) TO '{out_path}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {PARQUET_ROW_GROUP})"
            )
            kept = con.execute(f"SELECT count(*) FROM '{out_path}'").fetchone()[0]
        finally:
            con.close()

    return {
        "input_rows": int(read),
        "records_total": int(kept),
        "records_dropped_super_secure": int(dropped_kind),
        "records_dropped_no_name": int(dropped_name),
        "input_rows_dropped": int(dropped_kind) + int(dropped_name),
        "quick_mode": bool(limit),
    }


def events_from_records(records: pd.DataFrame) -> pd.DataFrame:
    """The evidence rows: one per PSC statement, keyed on record_id.

    A statement IS the link between a person and a company, so the evidence is a
    projection of the records and the file is never read twice.
    """
    wanted = ["record_id"] + [key for key, _, _ in EVENT_COLUMNS]
    events = records[[c for c in wanted if c in records.columns]].copy()
    for column in wanted:
        if column not in events.columns:
            events[column] = None
    return events[wanted]


# ---------------------------------------------------------------------------
# Entity IDs (docs/ENTITIES.md)
# ---------------------------------------------------------------------------

# deduping mints a random UUID slice. A zero-padded counter is used here instead
# so ids sort by age, which is what makes "the older entity survives" readable.
MINT_PREFIX = {"person": "PSCP", "organisation": "PSCO"}
MINT_WIDTH = 10


def _single(values) -> str | None:
    """The one non-null value in *values*, or None when they disagree."""
    distinct = {v for v in values if isinstance(v, str) and v}
    return next(iter(distinct)) if len(distinct) == 1 else None


def mint_entity_ids(members: pd.DataFrame, profile: Profile) -> pd.Series:
    """Durable ids for the proposals the registry does not already know.

    An organisation whose records agree on one padded GB company number takes
    that number as its id — the number names the company itself, so nothing
    minted could be more durable. Everything else takes the next counter value
    for its track, and the counter only goes up, so a number is never reused.
    """
    keys = members["entity_key"].drop_duplicates().sort_values()
    if not len(keys):
        return pd.Series(dtype="object")

    by_key = members.groupby("entity_key", sort=False)
    assigned: dict = {}
    if "company_number_padded" in members.columns:
        for key, number in by_key["company_number_padded"].agg(_single).items():
            if isinstance(number, str) and GB_NUMBER_RE.match(number):
                assigned[key] = number

    tracks = by_key["track"].first() if "track" in members.columns else None
    counter = int(getattr(profile, "_entity_counter", 0))
    for key in keys:
        if key in assigned:
            continue
        track = str(tracks.get(key, "person")) if tracks is not None else "person"
        counter += 1
        assigned[key] = f"{MINT_PREFIX.get(track, 'PSCP')}-{counter:0{MINT_WIDTH}d}"
    profile._entity_counter = counter

    return pd.Series([assigned[k] for k in keys], index=keys.to_numpy())


def _id_rank(identifier) -> tuple[int, str]:
    """A company number outranks a minted id; among equals the older wins."""
    text = str(identifier)
    return (0 if GB_NUMBER_RE.match(text) else 1, text)


def choose_survivors(claims: pd.DataFrame) -> pd.Series:
    """Which registry id wins where one proposal spans several of them."""
    return claims.groupby("entity_key", sort=False)["registry_entity"].apply(
        lambda ids: min(ids, key=_id_rank)
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class PscProfile(Profile):
    """People with significant control — persons and businesses across companies."""

    def __init__(self):
        from app.profiles import psc_features

        super().__init__(
            key="psc",
            title="PSC reconciliation",
            subtitle="Give one entity ID to PSC records that are the same person or business",
            input=InputSpec(
                label="PSC snapshot",
                extensions=[".zip", ".txt", ".jsonl", ".gz"],
                help=(
                    "The Companies House bulk PSC snapshot, as downloaded. The zip is "
                    "read without unpacking it."
                ),
            ),
            tracks=list(DEFAULT_TRACKS),
            display_columns=[
                DisplayColumn("name", "Name", "text"),
                DisplayColumn("kind", "Kind", "text"),
                DisplayColumn("dob_month", "Born (month)", "number"),
                DisplayColumn("dob_year", "Born (year)", "year"),
                DisplayColumn("nationality", "Nationality", "text"),
                DisplayColumn("country_of_residence", "Residence", "text"),
                DisplayColumn("postcode", "Postcode", "text"),
                DisplayColumn("locality", "Town", "text"),
                DisplayColumn("company_number", "Company", "text"),
                DisplayColumn("n_companies", "Companies", "number"),
                DisplayColumn("registration_number", "Registration number", "text"),
                DisplayColumn("country_registered", "Registered in", "text"),
                DisplayColumn("notified_on", "Notified", "text"),
                DisplayColumn("is_ceased", "Ceased", "text"),
            ],
            priority_columns=["n_companies"],
            raw_columns=list(RAW_COLUMNS),
            event_columns=[DisplayColumn(*c) for c in EVENT_COLUMNS],
            consensus_columns=["nationality", "country_of_residence"],
            evidence_focus=list(EVIDENCE_FOCUS),
            references=list(psc_features.REFERENCES),
            nouns={
                "record": "PSC record",
                "record_plural": "PSC records",
                "unit_evidence": "companies controlled",
                "evidence_row": "company",
                "evidence_row_plural": "companies",
            },
            pattern_summary=[
                {
                    "template": "{n_companies} companies · born {dob_month}/{dob_year} · "
                                "{nationality} · lives in {country_of_residence}",
                    "when": [{"column": "track", "op": "equals", "value": "person"}],
                },
                {
                    "template": "{n_companies} companies · registered in "
                                "{country_registered} · {registration_number}",
                },
            ],
            export_description=(
                "One row per PSC record with the entity ID it was given and how that was "
                "decided, plus an Elasticsearch bulk-update file for the ch-cred-pscs-v1 "
                "index. The tool never writes to Elasticsearch itself."
            ),
            # PSC has no earlier manual grouping to import.
            existing_label_name=None,
        )

    # -- loading -------------------------------------------------------------

    def load_records(self, input_path: Path, options: LoadOptions | None = None):
        """Stream the snapshot into a parquet and hand back its PATH.

        Not a frame: the rows have just been through DuckDB on their way out,
        and assembling 16 million of them in pandas afterwards would undo the
        whole point of streaming. Stage 0 moves the file into the run.
        """
        work = Path(tempfile.mkdtemp(prefix="psc-load-"))
        out = work / "records.parquet"
        stats = extract_to_parquet(Path(input_path), out, options)
        self._loaded = out
        return out, stats

    def load_events(self, input_path: Path, options: LoadOptions | None = None):
        """The evidence rows, projected out of the records parquet in DuckDB.

        A PSC statement IS the link between a person and a company, so the
        evidence is a projection of the records — the snapshot is never read a
        second time, and neither frame is built in memory.
        """
        source = getattr(self, "_loaded", None)
        if source is None or not Path(source).is_file():
            source, _ = self.load_records(input_path, options)
        out = Path(source).parent / "events.parquet"
        columns = ", ".join(["record_id"] + [key for key, _, _ in EVENT_COLUMNS])
        con = _connect(out.parent / "duckdb_tmp")
        try:
            con.execute(
                f"COPY (SELECT {columns} FROM read_parquet('{source}')) "
                f"TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD, "
                f"ROW_GROUP_SIZE {PARQUET_ROW_GROUP})"
            )
        finally:
            con.close()
        return out

    # -- entity IDs ----------------------------------------------------------

    def mint_entity_ids(self, members: pd.DataFrame) -> pd.Series:
        return mint_entity_ids(members, self)

    def choose_survivors(self, claims: pd.DataFrame) -> pd.Series:
        return choose_survivors(claims)

    # -- per-unit columns ----------------------------------------------------

    def aggregate_unit_columns(self, members, events=None):
        """A unit controls a company once, however many statements name it.

        ``n_companies`` is 1 per record, and summing it would count one company
        twice when two statements about it land in the same unit.
        """
        if members is None or not len(members) or "unit_id" not in members.columns:
            return None
        if "company_number" not in members.columns:
            return None
        return (
            members.groupby("unit_id", sort=False)["company_number"]
            .nunique().rename("n_companies").to_frame()
        )

    # -- the model's features ------------------------------------------------

    def pair_feature_metadata(self, track: str):
        from app.profiles import psc_features

        return psc_features.metadata(track)

    def build_pair_features(self, pairs, units, events=None, references=None,
                            track="person"):
        from app.profiles import psc_features

        return psc_features.build(pairs, units, events, references, track)

    # -- export --------------------------------------------------------------

    def export(self, run_dir: Path, scope: str, fmt: str, context: dict) -> Path:
        from app.profiles import psc_export

        return psc_export.export(run_dir, scope, fmt, context)

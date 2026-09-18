# backend/app/profiles/donations.py
"""Donations profile — Electoral Commission donation records, one record per donor."""

import re
from pathlib import Path

import pandas as pd

from app.profiles.base import DEFAULT_TRACKS, DisplayColumn, InputSpec, Profile

# The donations export ships several pivot sheets alongside the data. Prefer the
# known sheet; fall back to the first sheet whose header carries the two columns
# we cannot work without.
DATA_SHEET_NAME = "results_20260912 x"
REQUIRED_COLUMNS = ("DonorId", "DonorName")

# Read these as text. Excel stores them inconsistently (some numeric, some text,
# some with junk), and a float round-trip turns '12345' into '12345.0'.
TEXT_COLUMNS = ("DonorId", "DonorIDStandardTR", "CompanyRegistrationNumber", "Postcode")

# DonorIDStandardTR values that mean "never reviewed". They carry no label —
# these donors are the main work queue.
UNREVIEWED_ENTITY_IDS = {"", "0", "0.0", "TR0"}

# Track assignment for the two ambiguous donor statuses ("Impermissible Donor"
# and "Other"), decided by name pattern. A later slice moves both lists into
# user-editable rules; until then they live here, as module constants.
PERSON_TITLE_TOKENS = (
    "MR", "MRS", "MS", "MISS", "DR", "SIR", "LORD", "LADY", "DAME",
    "PROF", "REV", "CLLR", "BARONESS",
)
ORGANISATION_TOKENS = (
    "LTD", "LIMITED", "PLC", "LLP", "CLUB", "ASSOCIATION", "UNION", "TRUST",
    "SOCIETY", "COUNCIL", "PARTY", "COMMITTEE", "GROUP", "FUND", "COMPANY",
    "HOLDINGS", "BRANCH",
)

# Statuses that are a person outright, and the two decided by name pattern.
_PERSON_STATUSES = {"individual"}
_PATTERN_STATUSES = {"impermissible donor": "person", "other": "organisation"}

_TITLE_RE = re.compile(
    r"^(?:%s)\b\.?" % "|".join(PERSON_TITLE_TOKENS), re.IGNORECASE
)
_ORG_TOKEN_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(ORGANISATION_TOKENS), re.IGNORECASE
)
_WHITESPACE_RE = re.compile(r"\s+")

# Joined into one cell so a single column can show every spelling / party.
MULTI_VALUE_SEPARATOR = " | "


# ---------------------------------------------------------------------------
# Value cleaning
# ---------------------------------------------------------------------------


def _id_string(value) -> str:
    """Trim an ID and drop the float tail Excel leaves on integers ('12345.0')."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("", "nan", "nat", "none", "<na>"):
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _clean_company_number(value) -> str:
    """Company number with whitespace and the trailing ' ?' junk removed.

    The sheet carries values such as '04250076 ?' where a data entry clerk
    flagged a number they were unsure of.
    """
    text = _WHITESPACE_RE.sub("", _id_string(value))
    return text.rstrip("?")


def _clean_postcode(value) -> str:
    return _id_string(value).upper()


def assign_track(donor_status: str, name: str) -> str:
    """Return 'person' or 'organisation' for one donor.

    "Individual" is a person. "Impermissible Donor" and "Other" are decided by
    name pattern — a personal title prefix means a person, an organisation token
    means an organisation — and fall back to the status default. Every other
    status (company, union, trade union, trust, unincorporated association...)
    is an organisation.
    """
    status = (donor_status or "").strip().lower()
    if status in _PERSON_STATUSES:
        return "person"
    if status not in _PATTERN_STATUSES:
        return "organisation"

    text = (name or "").strip()
    if _TITLE_RE.match(text):
        return "person"
    if _ORG_TOKEN_RE.search(text):
        return "organisation"
    return _PATTERN_STATUSES[status]


# ---------------------------------------------------------------------------
# Reading the input file
# ---------------------------------------------------------------------------


def _text_dtypes(header) -> dict:
    """dtype=str for the columns that are present — pandas rejects unknown keys."""
    present = set(header)
    return {c: str for c in TEXT_COLUMNS if c in present}


def _pick_sheet(book: pd.ExcelFile) -> str:
    if DATA_SHEET_NAME in book.sheet_names:
        return DATA_SHEET_NAME
    for sheet in book.sheet_names:
        header = book.parse(sheet, nrows=0).columns
        if all(c in header for c in REQUIRED_COLUMNS):
            return sheet
    raise ValueError(
        "No sheet in the workbook has both a DonorId and a DonorName column. "
        f"Sheets found: {', '.join(book.sheet_names)}."
    )


def read_input(input_path: Path) -> pd.DataFrame:
    """Read the donations sheet (.xlsx) or export (.csv) into a raw frame."""
    path = Path(input_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        header = pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns
        frame = pd.read_csv(path, dtype=_text_dtypes(header), encoding="utf-8-sig")
    elif suffix in (".xlsx", ".xlsm"):
        with pd.ExcelFile(path) as book:
            sheet = _pick_sheet(book)
            header = book.parse(sheet, nrows=0).columns
            frame = book.parse(sheet, dtype=_text_dtypes(header))
    else:
        raise ValueError(
            f"'{path.name}' must be a .xlsx or .csv file (got '{suffix or 'no extension'}')."
        )

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(
            f"Input file is missing required column(s): {', '.join(missing)}."
        )
    return frame


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    """A column, or an all-null column of the same length when it is absent.

    Only DonorId and DonorName are required; a trimmed export may omit the rest.
    """
    if name in frame.columns:
        return frame[name]
    return pd.Series([None] * len(frame), index=frame.index, dtype="object")


# ---------------------------------------------------------------------------
# Aggregation to one row per donor
# ---------------------------------------------------------------------------


def _modal(frame: pd.DataFrame, column: str) -> pd.Series:
    """Most frequent non-empty value of *column* per record_id.

    Ties break on first appearance (mergesort is stable), so a re-run of the same
    input always produces the same record.
    """
    sub = frame.loc[frame[column] != "", ["record_id", column]]
    if sub.empty:
        return pd.Series(dtype="object")
    counts = (
        sub.groupby(["record_id", column], sort=False)
        .size()
        .rename("n")
        .reset_index()
        .sort_values("n", ascending=False, kind="mergesort")
    )
    return counts.drop_duplicates(subset=["record_id"]).set_index("record_id")[column]


def _joined_distinct(frame: pd.DataFrame, column: str) -> pd.Series:
    """Sorted distinct non-empty values per record_id, joined into one cell."""
    sub = frame.loc[frame[column] != "", ["record_id", column]]
    if sub.empty:
        return pd.Series(dtype="object")
    return (
        sub.drop_duplicates()
        .groupby("record_id", sort=False)[column]
        .apply(lambda values: MULTI_VALUE_SEPARATOR.join(sorted(values)))
    )


def build_records(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Aggregate raw donation rows into one record per donor, plus load stats."""
    stats = {"input_rows": int(len(raw))}

    rows = pd.DataFrame(index=raw.index)
    rows["donor_id"] = _column(raw, "DonorId").map(_id_string)
    keep = rows["donor_id"] != ""
    stats["input_rows_dropped"] = int((~keep).sum())
    raw = raw.loc[keep]
    rows = rows.loc[keep]

    rows["donor_name"] = _column(raw, "DonorName").map(_id_string)
    rows["donor_status"] = _column(raw, "DonorStatus").map(_id_string)
    # Trusts use a separate DonorId number range, so the trust flag is part of
    # the record key (design decision D15).
    rows["is_trust"] = rows["donor_status"].str.lower() == "trust"
    rows["record_id"] = rows["donor_id"].where(~rows["is_trust"], "TR" + rows["donor_id"])

    rows["postcode"] = _column(raw, "Postcode").map(_clean_postcode)
    rows["company_number"] = _column(raw, "CompanyRegistrationNumber").map(_clean_company_number)
    rows["party"] = _column(raw, "RegulatedEntityName").map(_id_string)
    rows["unit"] = _column(raw, "AccountingUnitName").map(_id_string)

    # A blank / zero DonorIDStandardTR means "never reviewed". Blanking those
    # here makes a real value win the modal vote however many zero rows sit
    # beside it.
    entity_id = _column(raw, "DonorIDStandardTR").map(_id_string)
    rows["existing_entity_id"] = entity_id.where(~entity_id.isin(UNREVIEWED_ENTITY_IDS), "")

    accepted = pd.to_datetime(_column(raw, "AcceptedDate"), errors="coerce")
    rows["year"] = accepted.dt.year
    rows["value"] = pd.to_numeric(_column(raw, "Value"), errors="coerce")

    grouped = rows.groupby("record_id", sort=False)
    records = pd.DataFrame(index=grouped.size().index)
    records.index.name = "record_id"

    records["name"] = _modal(rows, "donor_name")
    records["all_names"] = _joined_distinct(rows, "donor_name")
    records["donor_status"] = _modal(rows, "donor_status")
    records["is_trust"] = grouped["is_trust"].max()
    records["postcode"] = _modal(rows, "postcode")
    records["company_number"] = _modal(rows, "company_number")
    records["parties"] = _joined_distinct(rows, "party")
    records["units"] = _joined_distinct(rows, "unit")
    records["existing_entity_id"] = _modal(rows, "existing_entity_id")
    records["first_year"] = grouped["year"].min().astype("Int64")
    records["last_year"] = grouped["year"].max().astype("Int64")
    records["n_donations"] = grouped.size().astype("int64")
    records["total_value"] = grouped["value"].sum(min_count=1).fillna(0.0)

    records = records.reset_index()
    for column in ("name", "all_names", "donor_status"):
        records[column] = records[column].fillna("")
    # A missing postcode or company number stays null, never "". The matching
    # stages treat null as "unknown", and two empty strings would count as equal.
    for column in ("postcode", "company_number", "parties", "units"):
        records[column] = records[column].where(records[column].fillna("") != "", None)
    records["is_trust"] = records["is_trust"].fillna(False).astype(bool)

    records["track"] = [
        assign_track(status, name)
        for status, name in zip(records["donor_status"], records["name"])
    ]
    # A record carries a label only if a previous review gave it an entity ID.
    labelled = records["existing_entity_id"].notna() & (records["existing_entity_id"] != "")
    records["review_state"] = labelled.map({True: "labelled", False: "unreviewed"})
    records["existing_entity_id"] = records["existing_entity_id"].where(labelled, None)

    records = records[[
        "record_id", "track", "name", "review_state", "existing_entity_id",
        "is_trust", "donor_status", "postcode", "company_number", "parties",
        "units", "all_names", "first_year", "last_year", "n_donations", "total_value",
    ]].sort_values("name", kind="mergesort").reset_index(drop=True)

    stats["records_total"] = int(len(records))
    stats["records_person"] = int((records["track"] == "person").sum())
    stats["records_organisation"] = int((records["track"] == "organisation").sum())
    stats["records_labelled"] = int((records["review_state"] == "labelled").sum())
    stats["records_unreviewed"] = int((records["review_state"] == "unreviewed").sum())
    return records, stats


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class DonationsProfile(Profile):
    """Electoral Commission donations — one record per donor."""

    def __init__(self):
        super().__init__(
            key="donations",
            title="Donations reconciliation",
            subtitle="Give one entity ID to donor records that are the same person or organisation",
            input=InputSpec(
                label="Donations spreadsheet",
                extensions=[".xlsx", ".csv"],
                help=(
                    "The Electoral Commission donations export, one row per donation. "
                    "Needs at least DonorId and DonorName."
                ),
            ),
            tracks=list(DEFAULT_TRACKS),
            display_columns=[
                DisplayColumn("name", "Donor", "text"),
                DisplayColumn("donor_status", "Status", "text"),
                DisplayColumn("postcode", "Postcode", "text"),
                DisplayColumn("company_number", "Company number", "text"),
                DisplayColumn("parties", "Recipients", "list"),
                DisplayColumn("first_year", "First", "year"),
                DisplayColumn("last_year", "Last", "year"),
                DisplayColumn("n_donations", "Donations", "number"),
                DisplayColumn("total_value", "Total", "money"),
                DisplayColumn("existing_entity_id", "Entity ID", "text"),
                DisplayColumn("all_names", "All spellings", "list"),
            ],
            priority_columns=["total_value"],
        )

    def load_records(self, input_path: Path) -> tuple[pd.DataFrame, dict]:
        return build_records(read_input(Path(input_path)))

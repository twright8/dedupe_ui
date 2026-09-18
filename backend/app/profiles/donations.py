# backend/app/profiles/donations.py
"""Donations profile — Electoral Commission donation records, one record per donor."""

import re
from pathlib import Path

import numpy as np
import pandas as pd

from app.profiles.base import DEFAULT_TRACKS, DisplayColumn, InputSpec, Profile

# Columns build_records produces, in frame order. The ruleset may read these and
# may not overwrite them.
RAW_COLUMNS = [
    "record_id", "name", "review_state", "existing_entity_id", "is_trust",
    "donor_status", "postcode", "company_number", "parties", "units",
    "all_names", "first_year", "last_year", "n_donations", "total_value",
    "median_value", "modal_value", "n_distinct_values", "share_round_1000",
    "top_values",
]

# The evidence rows behind a donor: one per donation (D13b). The names are the
# profile's, not the spreadsheet's, so no screen ever sees 'RegulatedEntityName'.
EVENT_COLUMNS = [
    ("date", "Date", "text"),
    ("value", "Amount", "money"),
    ("recipient", "Recipient", "text"),
    ("unit", "Local unit", "text"),
    ("donation_type", "Type", "text"),
    ("nature", "Nature", "text"),
    ("is_sponsorship", "Sponsorship", "text"),
    ("reporting_period", "Reporting period", "text"),
    ("ec_ref", "EC reference", "text"),
]

# Reviewers judge a donor partly on the size pattern of their giving (D13a), so
# the amount profile is worked out once per record and again per unit.
AMOUNT_COLUMNS = ("median_value", "modal_value", "n_distinct_values",
                  "share_round_1000", "top_values")

# How many of a donor's most frequent amounts the summary carries.
TOP_VALUES = 5

# A "round" donation — the pattern reviewers look for — is a whole multiple of this.
ROUND_TO = 1000

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


# The workbook takes about ten seconds to parse, and a run needs it twice — once
# for the records and once for the donations behind them. One slot is enough: a
# run reads one file, and the key carries the size and mtime so an edited file is
# never served from the cache.
_LAST_READ: tuple | None = None


def _cache_key(path: Path) -> tuple:
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def read_input(input_path: Path) -> pd.DataFrame:
    """Read the donations sheet (.xlsx) or export (.csv), reusing the last parse."""
    global _LAST_READ

    path = Path(input_path)
    try:
        key = _cache_key(path)
    except OSError:
        key = None
    if key is not None and _LAST_READ is not None and _LAST_READ[0] == key:
        return _LAST_READ[1]

    frame = _read_input_uncached(path)
    if key is not None:
        _LAST_READ = (key, frame)
    return frame


def forget_input_cache() -> None:
    """Drop the cached parse. Tests use it; a run never needs to."""
    global _LAST_READ
    _LAST_READ = None


def _read_input_uncached(input_path: Path) -> pd.DataFrame:
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


def _money_text(values: pd.Series) -> pd.Series:
    """Amounts as text, dropping the decimals a whole pound total does not need."""
    numbers = pd.to_numeric(values, errors="coerce")
    whole = numbers.notna() & (numbers % 1 == 0)
    text = numbers.map(lambda v: "" if pd.isna(v) else f"{v:,.2f}")
    text = text.where(~whole, numbers.map(lambda v: "" if pd.isna(v) else f"{v:,.0f}"))
    return text


def amount_profile(frame: pd.DataFrame, key: str, value: str = "value") -> pd.DataFrame:
    """The donation-size pattern of every group in *frame*, indexed by *key*.

    Reviewers read a donor partly by how they give — always £10,000, or a
    scatter of odd amounts (D13a). This turns that into five columns: the
    median, the single most frequent amount (ties to the larger, because the
    bigger habit is the more telling one), how many distinct amounts there are,
    what share are whole thousands, and the five commonest, largest first within
    a tie.

    One groupby over (group, amount) and one sort. Nothing walks a group.
    """
    columns = list(AMOUNT_COLUMNS)
    sub = frame[[key, value]].copy()
    sub[value] = pd.to_numeric(sub[value], errors="coerce")
    sub = sub.dropna(subset=[value])
    if not len(sub):
        return pd.DataFrame(columns=columns)

    grouped = sub.groupby(key, sort=False)[value]
    result = pd.DataFrame(index=grouped.median().index)
    result.index.name = key
    result["median_value"] = grouped.median()
    result["n_distinct_values"] = grouped.nunique().astype("int64")
    result["share_round_1000"] = sub.assign(
        round_value=(sub[value] > 0) & (sub[value] % ROUND_TO == 0)
    ).groupby(key, sort=False)["round_value"].mean()

    counted = (
        sub.groupby([key, value], sort=False).size().rename("n").reset_index()
        .sort_values([key, "n", value], ascending=[True, False, False],
                     kind="mergesort")
    )
    result["modal_value"] = counted.drop_duplicates(subset=[key]).set_index(key)[value]
    top = counted.groupby(key, sort=False).head(TOP_VALUES).copy()
    top["text"] = _money_text(top[value])
    result["top_values"] = (
        top.groupby(key, sort=False)["text"]
        .apply(lambda values: MULTI_VALUE_SEPARATOR.join(values))
    )
    return result[columns]


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

    amounts = amount_profile(rows, "record_id", "value")
    for column in AMOUNT_COLUMNS:
        records[column] = amounts[column] if column in amounts.columns else None

    records = records.reset_index()
    for column in ("name", "all_names", "donor_status"):
        records[column] = records[column].fillna("")
    # A missing postcode or company number stays null, never "". The matching
    # stages treat null as "unknown", and two empty strings would count as equal.
    for column in ("postcode", "company_number", "parties", "units", "top_values"):
        records[column] = records[column].where(records[column].fillna("") != "", None)
    records["is_trust"] = records["is_trust"].fillna(False).astype(bool)
    records["n_distinct_values"] = records["n_distinct_values"].fillna(0).astype("int64")

    # A record carries a label only if a previous review gave it an entity ID.
    labelled = records["existing_entity_id"].notna() & (records["existing_entity_id"] != "")
    records["review_state"] = labelled.map({True: "labelled", False: "unreviewed"})
    records["existing_entity_id"] = records["existing_entity_id"].where(labelled, None)

    # No track here: the ruleset decides it, and stage 1 adds the column.
    records = records[RAW_COLUMNS].sort_values("name", kind="mergesort").reset_index(drop=True)

    stats["records_total"] = int(len(records))
    stats["records_labelled"] = int((records["review_state"] == "labelled").sum())
    stats["records_unreviewed"] = int((records["review_state"] == "unreviewed").sum())
    return records, stats


# ---------------------------------------------------------------------------
# The donations behind each donor (D13b)
# ---------------------------------------------------------------------------


def build_events(raw: pd.DataFrame) -> pd.DataFrame:
    """One row per donation, keyed on the donor's ``record_id``.

    This is the evidence a reviewer actually reads: who gave what, to whom and
    when. It is the same aggregation key ``build_records`` uses, so the two
    frames always agree about which donations belong to which donor.
    """
    donor_id = _column(raw, "DonorId").map(_id_string)
    keep = donor_id != ""
    raw = raw.loc[keep]
    donor_id = donor_id.loc[keep]
    status = _column(raw, "DonorStatus").map(_id_string)
    is_trust = status.str.lower() == "trust"

    accepted = pd.to_datetime(_column(raw, "AcceptedDate"), errors="coerce")
    events = pd.DataFrame({
        "record_id": donor_id.where(~is_trust, "TR" + donor_id).to_numpy(),
        "date": accepted.dt.strftime("%Y-%m-%d").to_numpy(),
        "value": pd.to_numeric(_column(raw, "Value"), errors="coerce").to_numpy(),
        "recipient": _column(raw, "RegulatedEntityName").map(_id_string).to_numpy(),
        "unit": _column(raw, "AccountingUnitName").map(_id_string).to_numpy(),
        "donation_type": _column(raw, "DonationType").map(_id_string).to_numpy(),
        "nature": _column(raw, "NatureOfDonation").map(_id_string).to_numpy(),
        "is_sponsorship": _column(raw, "IsSponsorship").map(_flag).to_numpy(),
        "reporting_period": _column(raw, "ReportingPeriodName").map(_id_string).to_numpy(),
        "ec_ref": _column(raw, "ECRef").map(_id_string).to_numpy(),
    })
    # A blank stays null, never "": the API has to hand a missing date to the
    # screen as JSON null, and Series.where would leave a NaN behind.
    for column in ("date", "recipient", "unit", "donation_type", "nature",
                   "reporting_period", "ec_ref"):
        values = events[column]
        keep = values.notna() & (values.astype(str) != "")
        events[column] = pd.Series(np.where(keep, values, None),
                                   index=events.index, dtype="object")
    # Newest first is the order a reviewer reads them in, and the order the API
    # serves; sorting once here means nothing has to sort 94,000 rows per request.
    return events.sort_values(["record_id", "date"], ascending=[True, False],
                              kind="mergesort", na_position="last").reset_index(drop=True)


def _flag(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "y")


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
                DisplayColumn("median_value", "Median gift", "money"),
                DisplayColumn("modal_value", "Usual gift", "money"),
                DisplayColumn("existing_entity_id", "Entity ID", "text"),
                DisplayColumn("all_names", "All spellings", "list"),
            ],
            priority_columns=["total_value"],
            raw_columns=list(RAW_COLUMNS),
            event_columns=[DisplayColumn(*c) for c in EVENT_COLUMNS],
        )

    def load_records(self, input_path: Path) -> tuple[pd.DataFrame, dict]:
        return build_records(read_input(Path(input_path)))

    def load_events(self, input_path: Path) -> pd.DataFrame:
        """The individual donations. Reuses the parse ``load_records`` just did."""
        return build_events(read_input(Path(input_path)))

    def aggregate_unit_columns(self, members, events=None):
        """Recompute a unit's giving pattern from its members' donations.

        A unit is several donor records pooled, so the most frequent member's
        median is not the pooled median, and the pooled count is not any one
        member's count. These are recomputed from the donations themselves
        whenever the run has them.
        """
        if events is None or not len(events) or "unit_id" not in events.columns:
            return None
        pooled = amount_profile(events, "unit_id", "value")
        grouped = events.groupby("unit_id", sort=False)
        pooled["n_donations"] = grouped.size().astype("int64")
        years = pd.to_numeric(
            events["date"].astype(str).str.slice(0, 4), errors="coerce"
        )
        pooled["first_year"] = years.groupby(events["unit_id"]).min().astype("Int64")
        pooled["last_year"] = years.groupby(events["unit_id"]).max().astype("Int64")
        return pooled

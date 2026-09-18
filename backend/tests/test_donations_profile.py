"""Tests for the donations profile loader.

Small synthetic fixtures only — the real sheet is 94,141 rows and lives outside
the repo.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.profiles import get_profile
from app.profiles.base import SHARED_COLUMNS, validate_records
from app.profiles.donations import (
    DonationsProfile,
    assign_track,
    build_records,
    read_input,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _row(**overrides):
    """One donation row with sensible defaults; override what the test is about."""
    row = {
        "DonorId": "1001",
        "DonorName": "A Smith",
        "DonorStatus": "Individual",
        "Postcode": None,
        "CompanyRegistrationNumber": None,
        "DonorIDStandardTR": "0",
        "RegulatedEntityName": "Labour Party",
        "AccountingUnitName": "Central Party",
        "AcceptedDate": "2015-06-01",
        "Value": 500,
    }
    row.update(overrides)
    return row


def _frame(rows):
    return pd.DataFrame(rows)


def _by_id(records):
    return {r["record_id"]: r for r in records.to_dict("records")}


# ---------------------------------------------------------------------------
# Record identity
# ---------------------------------------------------------------------------


def test_trusts_use_their_own_id_namespace():
    """A trust and a company may share a DonorId — the TR prefix keeps them apart."""
    records, _ = build_records(_frame([
        _row(DonorId="55", DonorStatus="Trust", DonorName="Smith Family Trust"),
        _row(DonorId="55", DonorStatus="Company", DonorName="Smith Holdings Ltd"),
    ]))
    assert set(records["record_id"]) == {"TR55", "55"}
    assert _by_id(records)["TR55"]["is_trust"] is True
    assert _by_id(records)["55"]["is_trust"] is False


def test_record_id_is_an_integer_string_not_a_float():
    """Excel hands back 12345.0 for an integer column; the record key must not."""
    records, _ = build_records(_frame([_row(DonorId="12345.0")]))
    assert list(records["record_id"]) == ["12345"]


def test_rows_with_no_donor_id_are_dropped_and_counted():
    records, stats = build_records(_frame([
        _row(DonorId="1"),
        _row(DonorId=None),
        _row(DonorId="  "),
    ]))
    assert stats["input_rows"] == 3
    assert stats["input_rows_dropped"] == 2
    assert stats["records_total"] == 1


# ---------------------------------------------------------------------------
# Existing labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", ["", "0", "0.0", "TR0", None])
def test_placeholder_entity_ids_mean_never_reviewed(placeholder):
    records, stats = build_records(_frame([_row(DonorIDStandardTR=placeholder)]))
    record = records.iloc[0]
    assert record["existing_entity_id"] is None
    assert record["review_state"] == "unreviewed"
    assert stats["records_unreviewed"] == 1
    assert stats["records_labelled"] == 0


def test_a_real_entity_id_beats_any_number_of_zero_rows():
    """One reviewed row is enough — the zeros beside it are not votes."""
    records, stats = build_records(_frame([
        _row(DonorId="7", DonorIDStandardTR="0"),
        _row(DonorId="7", DonorIDStandardTR="0"),
        _row(DonorId="7", DonorIDStandardTR="0"),
        _row(DonorId="7", DonorIDStandardTR="4242"),
    ]))
    record = records.iloc[0]
    assert record["existing_entity_id"] == "4242"
    assert record["review_state"] == "labelled"
    assert stats["records_labelled"] == 1


def test_most_frequent_real_entity_id_wins():
    records, _ = build_records(_frame([
        _row(DonorId="7", DonorIDStandardTR="111"),
        _row(DonorId="7", DonorIDStandardTR="222"),
        _row(DonorId="7", DonorIDStandardTR="222"),
    ]))
    assert records.iloc[0]["existing_entity_id"] == "222"


# ---------------------------------------------------------------------------
# Track assignment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,name,expected", [
    ("Individual", "A Smith", "person"),
    ("Individual", "Smith Holdings Ltd", "person"),      # status wins outright
    ("Company", "A Smith", "organisation"),
    ("Trade Union", "Unite", "organisation"),
    ("Trust", "Smith Family Trust", "organisation"),
    # The two ambiguous statuses, decided by name pattern
    ("Impermissible Donor", "Mr A Smith", "person"),
    ("Impermissible Donor", "Dr. J Patel", "person"),
    ("Impermissible Donor", "Baroness Jones", "person"),
    ("Impermissible Donor", "Acme Holdings Ltd", "organisation"),
    ("Impermissible Donor", "Barnet Conservative Association", "organisation"),
    ("Impermissible Donor", "A Smith", "person"),        # neither pattern: status default
    ("Other", "Mrs B Brown", "person"),
    ("Other", "West End Club", "organisation"),
    ("Other", "Something Unclassifiable", "organisation"),  # neither pattern: status default
])
def test_assign_track(status, name, expected):
    assert assign_track(status, name) == expected


def test_title_must_be_a_whole_leading_word():
    """'Drummond' starts with 'Dr' but is not a title."""
    assert assign_track("Impermissible Donor", "Drummond") == "person"  # status default
    assert assign_track("Other", "Drummond") == "organisation"          # status default


def test_track_counts_land_in_stats():
    _, stats = build_records(_frame([
        _row(DonorId="1", DonorStatus="Individual"),
        _row(DonorId="2", DonorStatus="Company", DonorName="Acme Ltd"),
        _row(DonorId="3", DonorStatus="Company", DonorName="Beta Ltd"),
    ]))
    assert stats["records_person"] == 1
    assert stats["records_organisation"] == 2
    assert stats["records_total"] == 3


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_junk_is_stripped_from_the_company_number():
    records, _ = build_records(_frame([
        _row(DonorId="9", DonorStatus="Company", CompanyRegistrationNumber="04250076 ?"),
        _row(DonorId="9", DonorStatus="Company", CompanyRegistrationNumber="04250076 ?"),
    ]))
    assert records.iloc[0]["company_number"] == "04250076"


def test_aggregation_across_a_donor_s_rows():
    records, _ = build_records(_frame([
        _row(DonorId="3", DonorName=" A Smith ", RegulatedEntityName="Labour Party",
             AccountingUnitName="North West", AcceptedDate="2010-01-05",
             Value=100, Postcode=" sw1a 1aa "),
        _row(DonorId="3", DonorName="A Smith", RegulatedEntityName="Green Party",
             AccountingUnitName="Central Party", AcceptedDate="2018-11-20",
             Value=250.5, Postcode="SW1A 1AA"),
        _row(DonorId="3", DonorName="Andrew Smith", RegulatedEntityName="Labour Party",
             AccountingUnitName="North West", AcceptedDate="2014-03-03",
             Value=50, Postcode=None),
    ]))
    record = records.iloc[0]
    assert record["name"] == "A Smith"                     # most frequent spelling
    assert record["all_names"] == "A Smith | Andrew Smith"  # every spelling, sorted
    assert record["postcode"] == "SW1A 1AA"                # upper-cased, trimmed
    assert record["parties"] == "Green Party | Labour Party"
    assert record["units"] == "Central Party | North West"
    assert record["first_year"] == 2010
    assert record["last_year"] == 2018
    assert record["n_donations"] == 3
    assert record["total_value"] == pytest.approx(400.5)


def test_missing_dates_leave_the_years_null():
    records, _ = build_records(_frame([_row(AcceptedDate=None)]))
    record = records.iloc[0]
    assert pd.isna(record["first_year"])
    assert pd.isna(record["last_year"])


def test_optional_columns_may_be_absent():
    """A trimmed export with only the two required columns still loads."""
    records, stats = build_records(_frame([
        {"DonorId": "1", "DonorName": "A Smith"},
        {"DonorId": "2", "DonorName": "Acme Ltd"},
    ]))
    assert stats["records_total"] == 2
    # No status at all: every record falls through to the organisation default.
    assert set(records["track"]) == {"organisation"}
    assert list(records["review_state"]) == ["unreviewed", "unreviewed"]


# ---------------------------------------------------------------------------
# Shared contract
# ---------------------------------------------------------------------------


def test_records_frame_carries_the_shared_columns():
    records, _ = build_records(_frame([_row(DonorId="1"), _row(DonorId="2")]))
    for column in SHARED_COLUMNS:
        assert column in records.columns
    validate_records(records)  # must not raise


def test_record_ids_are_unique():
    records, _ = build_records(_frame([
        _row(DonorId="1"), _row(DonorId="1"), _row(DonorId="2"),
    ]))
    assert records["record_id"].is_unique


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def test_read_csv_input(tmp_path):
    path = tmp_path / "donations.csv"
    _frame([_row(DonorId="1"), _row(DonorId="2")]).to_csv(path, index=False)
    frame = read_input(path)
    assert len(frame) == 2
    assert frame["DonorId"].dtype == object or str(frame["DonorId"].dtype).startswith("str")


def test_read_xlsx_picks_the_sheet_with_the_donor_columns(tmp_path):
    path = tmp_path / "donations.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"anything": [1, 2]}).to_excel(writer, sheet_name="Pivot 1", index=False)
        _frame([_row(DonorId="1")]).to_excel(writer, sheet_name="data", index=False)
    frame = read_input(path)
    assert list(frame["DonorName"]) == ["A Smith"]


def test_read_rejects_an_unsupported_extension(tmp_path):
    path = tmp_path / "donations.txt"
    path.write_text("nope")
    with pytest.raises(ValueError, match=r"\.xlsx or \.csv"):
        read_input(path)


def test_read_rejects_a_file_without_the_required_columns(tmp_path):
    path = tmp_path / "donations.csv"
    pd.DataFrame({"Something": [1]}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="missing required column"):
        read_input(path)


def test_load_records_end_to_end(tmp_path):
    path = tmp_path / "donations.csv"
    _frame([
        _row(DonorId="1", DonorName="A Smith"),
        _row(DonorId="2", DonorName="Acme Ltd", DonorStatus="Company"),
    ]).to_csv(path, index=False)

    records, stats = DonationsProfile().load_records(path)
    assert stats["records_total"] == 2
    assert stats["records_person"] == 1
    assert stats["records_organisation"] == 1


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_get_profile_defaults_to_donations(monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    assert get_profile().key == "donations"


def test_get_profile_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("PROFILE", "psc")
    assert get_profile().key == "psc"


def test_unknown_profile_raises_a_clear_error(monkeypatch):
    monkeypatch.setenv("PROFILE", "nonsense")
    with pytest.raises(RuntimeError, match="Unknown PROFILE 'nonsense'"):
        get_profile()


def test_psc_loader_says_it_is_not_built_yet(tmp_path):
    from app.profiles.psc import PscProfile

    profile = PscProfile()
    assert profile.title == "PSC reconciliation"
    with pytest.raises(NotImplementedError, match="not built yet"):
        profile.load_records(tmp_path / "snapshot.zip")

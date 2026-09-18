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
    RAW_COLUMNS,
    DonationsProfile,
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
# Track assignment belongs to the ruleset, not the loader
# ---------------------------------------------------------------------------


def test_the_loader_does_not_decide_a_track():
    """The ruleset decides the track and stage 1 adds the column. A loader that
    quietly did it again would make the track rules a lie."""
    records, stats = build_records(_frame([
        _row(DonorId="1", DonorStatus="Individual"),
        _row(DonorId="2", DonorStatus="Company", DonorName="Acme Ltd"),
    ]))
    assert "track" not in records.columns
    assert "records_person" not in stats
    assert "records_organisation" not in stats
    assert stats["records_total"] == 2


def test_the_frame_matches_the_declared_raw_columns():
    """The Config screen offers RAW_COLUMNS before any run exists, so it must
    be exactly what the loader produces."""
    records, _ = build_records(_frame([_row(DonorId="1")]))
    assert list(records.columns) == RAW_COLUMNS
    assert DonationsProfile().raw_columns == RAW_COLUMNS


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
    assert list(records["donor_status"]) == ["", ""]
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
    assert set(records["name"]) == {"A Smith", "Acme Ltd"}


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


# ---------------------------------------------------------------------------
# The giving pattern (D13a) and the evidence rows (D13b)
# ---------------------------------------------------------------------------


def test_the_amount_profile_describes_how_a_donor_gives():
    records, _ = build_records(_frame([
        _row(DonorId="7", Value=1000),
        _row(DonorId="7", Value=1000),
        _row(DonorId="7", Value=2000),
        _row(DonorId="7", Value=250),
        _row(DonorId="8", Value=500),
    ]))
    by_id = _by_id(records)

    seven = by_id["7"]
    assert seven["median_value"] == 1000.0       # 250, 1000, 1000, 2000
    assert seven["modal_value"] == 1000.0        # two of them
    assert seven["n_distinct_values"] == 3
    assert seven["share_round_1000"] == 0.75     # three of four are whole thousands
    # Most frequent first, and the larger amount wins a tie.
    assert seven["top_values"] == "1,000 | 2,000 | 250"

    assert by_id["8"]["median_value"] == 500.0
    assert by_id["8"]["n_distinct_values"] == 1


def test_the_modal_amount_breaks_a_tie_towards_the_larger_gift():
    records, _ = build_records(_frame([
        _row(DonorId="9", Value=100),
        _row(DonorId="9", Value=5000),
    ]))
    # One each: the bigger habit is the more telling one.
    assert _by_id(records)["9"]["modal_value"] == 5000.0


def test_a_donor_with_no_usable_amount_still_loads():
    records, _ = build_records(_frame([_row(DonorId="11", Value=None)]))
    row = _by_id(records)["11"]
    assert row["median_value"] is None or pd.isna(row["median_value"])
    assert row["n_distinct_values"] == 0
    assert row["top_values"] is None


def test_the_events_frame_is_one_row_per_donation():
    from app.profiles.donations import build_events

    events = build_events(_frame([
        _row(DonorId="20", Value=100, AcceptedDate="2020-01-05",
             RegulatedEntityName="Party A", ECRef="C1"),
        _row(DonorId="20", Value=200, AcceptedDate="2022-03-09",
             RegulatedEntityName="Party B", ECRef="C2"),
        _row(DonorId="21", DonorStatus="Trust", Value=300, AcceptedDate=None),
    ]))

    assert list(events.columns) == [
        "record_id", "date", "value", "recipient", "unit", "donation_type",
        "nature", "is_sponsorship", "reporting_period", "ec_ref",
    ]
    twenty = events[events["record_id"] == "20"]
    # Newest first, which is the order a reviewer reads them in.
    assert list(twenty["date"]) == ["2022-03-09", "2020-01-05"]
    assert list(twenty["value"]) == [200.0, 100.0]
    assert list(twenty["ec_ref"]) == ["C2", "C1"]
    # A trust keeps its own id namespace here too, so the events join the same
    # record the loader made.
    assert set(events["record_id"]) == {"20", "TR21"}
    assert events[events["record_id"] == "TR21"].iloc[0]["date"] is None


def test_the_workbook_is_parsed_once_for_both_frames(tmp_path, monkeypatch):
    """Reading the real sheet takes about ten seconds, and a run needs it twice."""
    from app.profiles import donations

    path = tmp_path / "donations.csv"
    _frame([_row(DonorId="30"), _row(DonorId="31")]).to_csv(path, index=False)

    donations.forget_input_cache()
    calls = []
    original = donations._read_input_uncached
    monkeypatch.setattr(donations, "_read_input_uncached",
                        lambda p: (calls.append(p), original(p))[1])

    profile = DonationsProfile()
    profile.load_records(path)
    profile.load_events(path)
    assert len(calls) == 1

    # A changed file is never served from the cache.
    _frame([_row(DonorId="32")]).to_csv(path, index=False)
    profile.load_records(path)
    assert len(calls) == 2
    donations.forget_input_cache()


def test_a_units_giving_pattern_is_pooled_not_voted_on():
    """A unit is several donors pooled, so its median is not a member's median."""
    from app.pipeline.dedupe.units import build_units

    records, _ = build_records(_frame([
        _row(DonorId="40", Value=1000),
        _row(DonorId="40", Value=1000),
        _row(DonorId="41", Value=9000),
    ]))
    from app.profiles.donations import build_events

    events = build_events(_frame([
        _row(DonorId="40", Value=1000, AcceptedDate="2020-01-01"),
        _row(DonorId="40", Value=1000, AcceptedDate="2021-01-01"),
        _row(DonorId="41", Value=9000, AcceptedDate="2022-01-01"),
    ]))
    groups = pd.DataFrame([
        {"record_id": r, "group_id": "X-40", "track": "person",
         "status": "merged", "key_ids": "k3", "guard": None} for r in ("40", "41")
    ])
    records["track"] = "person"

    units, members = build_units(records, groups, events)
    unit = units.iloc[0]

    # Pooled: 1000, 1000, 9000.
    assert unit["median_value"] == 1000.0
    assert unit["n_distinct_values"] == 2
    assert unit["n_donations"] == 3
    assert unit["first_year"] == 2020
    assert unit["last_year"] == 2022
    assert unit["total_value"] == 11000.0

    # Without the evidence rows the hook has nothing to work from and the
    # representative's own value stands.
    plain, _ = build_units(records, groups)
    assert plain.iloc[0]["n_donations"] in (1, 2)

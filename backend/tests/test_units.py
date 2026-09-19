"""Unit building (``app/pipeline/dedupe/units.py``).

``docs/LINKAGE.md`` sets the contract these check: a merged exact group is one
unit, the representative takes the modal value with ties going to the smallest
``record_id``, and a record in a held group carries the smallest held group id
it sits in.

The held-group tests are a regression. Taking ``min`` over the whole frame's
``held_group_id`` raised ``TypeError`` on any unit whose members were only
partly in a held group, because pandas has no fast path for ``min`` over an
object column and the Python fallback compares a string to a float sentinel.
The real donations run has 194 such units.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.pipeline.dedupe import units as units_module


def records_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column, default in (
        ("track", "person"), ("name", None), ("existing_entity_id", None),
        ("postcode", None),
    ):
        if column not in frame.columns:
            frame[column] = default
    frame["record_id"] = frame["record_id"].astype(str)
    return frame


def groups_frame(rows: list[dict]) -> pd.DataFrame:
    columns = ["record_id", "group_id", "track", "status", "key_ids", "guard"]
    frame = pd.DataFrame(rows, columns=columns)
    if len(frame):
        frame["record_id"] = frame["record_id"].astype(str)
    return frame


def merged(record_id: str, group_id: str) -> dict:
    return {"record_id": record_id, "group_id": group_id, "track": "person",
            "status": "merged", "key_ids": "k1", "guard": None}


def held(record_id: str, group_id: str) -> dict:
    return {"record_id": record_id, "group_id": group_id, "track": "person",
            "status": "held", "key_ids": "k1", "guard": "max_group_size:9>8"}


def held_of(units: pd.DataFrame) -> dict:
    return dict(zip(units["unit_id"], units["held_group_id"]))


# ---------------------------------------------------------------------------
# held_group_id
# ---------------------------------------------------------------------------


def test_a_unit_whose_members_are_only_partly_held_still_builds():
    """The regression. Records 1 and 2 are one merged unit; only 2 is held."""
    records = records_frame([{"record_id": "1"}, {"record_id": "2"}])
    groups = groups_frame([merged("1", "X-1"), merged("2", "X-1"),
                           held("2", "H-k1-2")])

    units, _ = units_module.build_units(records, groups)

    assert len(units) == 1
    assert held_of(units) == {"1": "H-k1-2"}


def test_a_unit_with_no_held_member_has_a_null_held_group_id():
    records = records_frame([{"record_id": "1"}, {"record_id": "2"}])
    groups = groups_frame([merged("1", "X-1"), merged("2", "X-1")])

    units, _ = units_module.build_units(records, groups)

    assert held_of(units) == {"1": None}


def test_no_held_group_anywhere_leaves_every_unit_null():
    records = records_frame([{"record_id": "1"}, {"record_id": "2"}])

    units, _ = units_module.build_units(records, groups_frame([]))

    assert held_of(units) == {"1": None, "2": None}


def test_a_record_in_several_held_groups_takes_the_smallest_id():
    records = records_frame([{"record_id": "1"}, {"record_id": "2"}])
    groups = groups_frame([held("1", "H-k2-9"), held("1", "H-k1-1"),
                           held("2", "H-k3-4")])

    units, _ = units_module.build_units(records, groups)

    assert held_of(units) == {"1": "H-k1-1", "2": "H-k3-4"}


def test_a_pooled_unit_takes_the_smallest_held_id_among_its_members():
    records = records_frame([{"record_id": f"{n}"} for n in (1, 2, 3)])
    groups = groups_frame([merged("1", "X-1"), merged("2", "X-1"),
                           merged("3", "X-1"),
                           held("3", "H-k1-3"), held("2", "H-k1-0")])

    units, _ = units_module.build_units(records, groups)

    assert held_of(units) == {"1": "H-k1-0"}


def test_a_missing_held_group_id_is_none_and_never_nan():
    """The API strips nulls, and a NaN reads back as a float it cannot strip."""
    records = records_frame([{"record_id": "1"}, {"record_id": "2"}])
    groups = groups_frame([held("1", "H-k1-1")])

    units, _ = units_module.build_units(records, groups)

    missing = units.loc[units["unit_id"] == "2", "held_group_id"].iloc[0]
    assert missing is None
    assert not isinstance(missing, float)


# ---------------------------------------------------------------------------
# The modal vote
# ---------------------------------------------------------------------------


def test_a_unit_of_one_keeps_its_own_values_whatever_the_rest_of_the_frame_says():
    """Only pooled units are voted on, so a lone record is its own answer."""
    records = records_frame([
        {"record_id": "1", "name": "Alpha", "postcode": "AA1 1AA"},
        {"record_id": "2", "name": "Beta", "postcode": "BB2 2BB"},
        {"record_id": "3", "name": "Beta", "postcode": "BB2 2BB"},
    ])

    units, _ = units_module.build_units(records, groups_frame([]))

    rows = units.set_index("unit_id")
    assert rows.loc["1", "name"] == "Alpha"
    assert rows.loc["1", "postcode"] == "AA1 1AA"
    assert list(units["unit_size"]) == [1, 1, 1]


def test_the_representative_is_the_modal_value_with_ties_to_the_smallest_id():
    records = records_frame([
        {"record_id": "1", "name": "Alpha", "postcode": "AA1 1AA"},
        {"record_id": "2", "name": "Beta", "postcode": None},
        {"record_id": "3", "name": "Beta", "postcode": "CC3 3CC"},
    ])
    groups = groups_frame([merged(r, "X-1") for r in ("1", "2", "3")])

    units, _ = units_module.build_units(records, groups)

    row = units.iloc[0]
    assert row["unit_id"] == "1"
    assert row["unit_size"] == 3
    # "Beta" twice beats "Alpha" once; the postcodes tie one-all, so the
    # smallest record_id carrying a value wins.
    assert row["name"] == "Beta"
    assert row["postcode"] == "AA1 1AA"


def test_every_record_column_survives_into_units():
    """The review screen and the exports read columns nothing else touches."""
    records = records_frame([
        {"record_id": "1", "name": "Alpha", "never_read": "kept"},
        {"record_id": "2", "name": "Alpha", "never_read": "kept"},
    ])
    groups = groups_frame([merged("1", "X-1"), merged("2", "X-1")])

    units, _ = units_module.build_units(records, groups)

    assert "never_read" in units.columns
    assert units.iloc[0]["never_read"] == "kept"


@pytest.mark.parametrize("status", ["merged", "held"])
def test_unit_members_lists_every_record_once(status):
    records = records_frame([{"record_id": f"{n}"} for n in (1, 2, 3)])
    rows = [merged(r, "X-1") if status == "merged" else held(r, "H-k1-1")
            for r in ("1", "2", "3")]

    _, members = units_module.build_units(records, groups_frame(rows))

    assert sorted(members["record_id"]) == ["1", "2", "3"]
    assert len(members) == 3

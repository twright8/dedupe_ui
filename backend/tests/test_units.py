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


# ---------------------------------------------------------------------------
# The out-of-core build: the edges the pandas build never had to state
# ---------------------------------------------------------------------------


def test_no_pooled_unit_anywhere_still_writes_every_record(tmp_path):
    """Nothing merged, so the vote, the labels and the totals never run."""
    records = records_frame([
        {"record_id": "r1", "name": "ALICE", "existing_entity_id": "E1"},
        {"record_id": "r2", "name": "BOB", "existing_entity_id": None},
    ])
    units, members = units_module.build_units(
        records, groups_frame([]), temp_dir=tmp_path)
    assert list(units["unit_id"]) == ["r1", "r2"]
    assert list(units["unit_size"]) == [1, 1]
    assert list(units["n_existing_ids"]) == [1, 0]
    assert list(units["existing_entity_id"]) == ["E1", None]
    assert list(units["name"]) == ["ALICE", "BOB"]
    assert len(members) == 2


def test_no_records_at_all_writes_an_empty_units_file(tmp_path):
    """A track a profile declares and the data never fills, in the extreme."""
    records = records_frame([{"record_id": "r1", "name": "ALICE"}]).iloc[:0]
    units, members = units_module.build_units(
        records, groups_frame([]), temp_dir=tmp_path)
    assert len(units) == 0
    assert len(members) == 0
    for column in ("unit_id", "unit_size", "held_group_id", "name", "track"):
        assert column in units.columns


def test_a_column_named_with_a_quote_is_not_a_sql_injection(tmp_path):
    """Every identifier goes through ``_quote``; a hostile name proves it."""
    hostile = 'odd" name, x'
    records = records_frame([
        {"record_id": "r1", "name": "ALICE"},
        {"record_id": "r2", "name": "ALICE"},
    ])
    records[hostile] = ["keep", "keep"]
    units, _ = units_module.build_units(
        records, groups_frame([merged("r1", "G1"), merged("r2", "G1")]),
        temp_dir=tmp_path)
    assert list(units[hostile]) == ["keep"]
    assert list(units["unit_id"]) == ["r1"]


def test_a_group_row_with_no_status_is_held_not_merged(tmp_path):
    """``status == 'merged'`` is the only thing that pools, as in pandas."""
    rows = [merged("r1", "G1"), merged("r2", "G1")]
    rows.append({"record_id": "r3", "group_id": "G2", "track": "person",
                 "status": None, "key_ids": "k1", "guard": None})
    records = records_frame([{"record_id": f"r{n}", "name": "N"} for n in (1, 2, 3)])
    units, _ = units_module.build_units(
        records, groups_frame(rows), temp_dir=tmp_path)
    assert list(units["unit_id"]) == ["r1", "r3"]
    assert held_of(units)["r3"] == "G2"


def test_the_tie_break_compares_record_ids_as_text(tmp_path):
    """"10" is smaller than "9" as text, and the rule says text."""
    records = records_frame([
        {"record_id": "10", "name": "TEN"},
        {"record_id": "9", "name": "NINE"},
    ])
    units, _ = units_module.build_units(
        records, groups_frame([merged("10", "G1"), merged("9", "G1")]),
        temp_dir=tmp_path)
    assert list(units["name"]) == ["TEN"]


def test_a_label_is_trimmed_the_way_python_trims_it(tmp_path):
    """The pandas build used ``str.strip()``, which takes a tab as well."""
    records = records_frame([
        {"record_id": "r1", "name": "A", "existing_entity_id": "\tE1 "},
        {"record_id": "r2", "name": "A", "existing_entity_id": "E1"},
        {"record_id": "r3", "name": "B", "existing_entity_id": " \n "},
    ])
    units, _ = units_module.build_units(
        records, groups_frame([merged("r1", "G1"), merged("r2", "G1")]),
        temp_dir=tmp_path)
    by_id = dict(zip(units["unit_id"], units["existing_entity_ids"]))
    assert by_id["r1"] == "E1"
    assert by_id["r3"] is None
    assert dict(zip(units["unit_id"], units["n_existing_ids"]))["r3"] == 0


def test_an_invented_aggregate_column_fails_loudly(tmp_path, monkeypatch):
    """The hook runs over pooled units only, so it may not invent a column."""
    from app import profiles

    profile = profiles.get_profile()
    monkeypatch.setattr(
        type(profile), "aggregate_unit_columns",
        lambda self, members, events=None: pd.DataFrame(
            {"invented": [1]}, index=pd.Index(["r1"], name="unit_id")),
        raising=False,
    )
    records = records_frame([
        {"record_id": "r1", "name": "A"}, {"record_id": "r2", "name": "A"}])
    with pytest.raises(ValueError, match="invented"):
        units_module.build_units(
            records, groups_frame([merged("r1", "G1"), merged("r2", "G1")]),
            temp_dir=tmp_path)


def test_counts_and_fingerprint_off_the_file_match_the_frame(tmp_path):
    """What stage 3 now writes without ever holding the units frame."""
    records = records_frame([
        {"record_id": "r1", "name": "A", "track": "person"},
        {"record_id": "r2", "name": "A", "track": "person"},
        {"record_id": "r3", "name": "B", "track": "organisation"},
    ])
    groups = groups_frame([merged("r1", "G1"), merged("r2", "G1")])
    units_path = tmp_path / "units.parquet"
    members_path = tmp_path / "unit_members.parquet"
    counts = units_module.build_units_files(
        records, groups, units_path, members_path, temp_dir=tmp_path)
    units, members = units_module.build_units(records, groups, temp_dir=tmp_path)

    assert counts == {"units": len(units), "unit_members": len(members)}
    assert units_module.counts_from_file(units_path) == units_module.counts_from(units)
    fingerprint = units_module.fingerprint_from_file(units_path)
    assert list(fingerprint["unit_id"]) == list(units["unit_id"].astype(str))
    assert list(fingerprint["unit_size"]) == list(units["unit_size"])
    assert units_module.record_count(units_path) == len(units)


def test_parquet_paths_and_frames_build_the_same_units(tmp_path):
    """Stage 3 hands paths now; the tests and the old callers hand frames."""
    records = records_frame([
        {"record_id": "r1", "name": "ALICE", "existing_entity_id": "E1"},
        {"record_id": "r2", "name": "ALICE", "existing_entity_id": "E2"},
        {"record_id": "r3", "name": "BOB", "postcode": "X1 1XX"},
    ])
    groups = groups_frame([merged("r1", "G1"), merged("r2", "G1"), held("r3", "G2")])
    records_path = tmp_path / "records.parquet"
    groups_path = tmp_path / "exact_groups.parquet"
    records.to_parquet(records_path, index=False)
    groups.to_parquet(groups_path, index=False)

    from_frames = tmp_path / "a.parquet"
    from_paths = tmp_path / "b.parquet"
    counts_frames = units_module.build_units_files(
        records, groups, from_frames, tmp_path / "am.parquet", temp_dir=tmp_path)
    counts_paths = units_module.build_units_files(
        records_path, groups_path, from_paths, tmp_path / "bm.parquet",
        temp_dir=tmp_path)

    assert counts_frames == counts_paths
    a = units_module._restore(from_frames)
    b = units_module._restore(from_paths)
    assert list(a.columns) == list(b.columns)
    assert a.astype(object).where(a.notna(), None).to_dict("list") == \
        b.astype(object).where(b.notna(), None).to_dict("list")


def test_the_scored_units_fingerprint_written_in_sql_is_the_pandas_one(tmp_path):
    """``scored_units.parquet`` without the units frame or a copy of it."""
    from app.pipeline.dedupe import stage_3_score

    records = records_frame([
        {"record_id": "r1", "name": "A"}, {"record_id": "r2", "name": "A"},
        {"record_id": "r3", "name": "B"},
    ])
    units, _ = units_module.build_units(
        records, groups_frame([merged("r1", "G1"), merged("r2", "G1")]),
        temp_dir=tmp_path)
    units_path = tmp_path / "units.parquet"
    units.to_parquet(units_path, index=False)

    out = tmp_path / "scored_units.parquet"
    units_module.write_fingerprint(units_path, out, tmp_path)
    written = pd.read_parquet(out)
    expected = stage_3_score.unit_fingerprint(units)

    assert list(written["unit_id"].astype(str)) == list(expected["unit_id"].astype(str))
    assert list(written["unit_size"].astype("int64")) == \
        list(expected["unit_size"].astype("int64"))

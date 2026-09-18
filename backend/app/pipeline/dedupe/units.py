# backend/app/pipeline/dedupe/units.py
"""Turn records and exact groups into the units stage 3 scores.

``LINKAGE.md`` sets the rule: a merged exact group is one unit, and every other
record is a unit of its own — including each member of a held group, because a
guard held that group back precisely so a human could decide it.

A unit gets one representative row. For every column the records carry, the
representative takes the most frequent non-null value among the members, with
ties broken by the smallest ``record_id``. The profile's priority columns are
summed instead, because a reviewer sorting by money wants the pair's total.

Nothing loops over groups. The whole build is a handful of groupbys, so the
16 million PSC records will not need a different implementation.
"""

import duckdb

from app import duckdb_conn
import numpy as np
import pandas as pd

from app.profiles import get_profile
from app.rules import keys

UNITS_FILENAME = "units.parquet"
UNIT_MEMBERS_FILENAME = "unit_members.parquet"

# The joined form of several existing entity ids, matching the donations
# profile's separator for multi-valued cells.
ID_SEPARATOR = " | "

LABEL_COLUMN = "existing_entity_id"

# Columns the representative never carries a modal value for: they are either
# replaced by a unit-level column or meaningless once members are pooled.
_NOT_REPRESENTED = (LABEL_COLUMN,)


def _as_text(series: pd.Series) -> pd.Series:
    return series.astype(str)


def plain_strings(frame: pd.DataFrame) -> pd.DataFrame:
    """Arrow-backed string columns as plain object ones.

    Parquet hands pandas Arrow-backed strings. Anything that groups or compares
    them row by row pays for a pyarrow scalar each time, so the frame is put
    back on plain Python objects before any of that. Note that rebuilding a
    Series from ``to_numpy`` does not do it — pandas re-infers the string dtype;
    ``astype(object)`` is the conversion that sticks.
    """
    converted = {}
    for column in frame.columns:
        dtype = str(frame[column].dtype)
        if dtype in ("str", "string", "large_string") or dtype.startswith("string["):
            converted[column] = frame[column].astype(object)
    return frame.assign(**converted) if converted else frame


def _quote(name: str) -> str:
    """An identifier DuckDB will read as one name, whatever is in it."""
    return '"' + str(name).replace('"', '""') + '"'


def representatives(joined: pd.DataFrame, columns: list[str],
                    temp_dir=None) -> pd.DataFrame:
    """The representative value of every column, for every unit, in one pass.

    The rule (`docs/LINKAGE.md`) is the most frequent non-null value, ties going
    to the smallest ``record_id``. Pandas does that with a group-by per column,
    and on Arrow-backed strings it falls back to a Python loop per group — ten
    seconds for 52,000 records, and the best part of an hour at PSC scale.

    DuckDB does the same work set-based. ``mode()`` is no use because its
    tie-break is not the one the contract names, so each column is counted and
    ranked: ``count(*) DESC`` then ``min(record_id) ASC``, which is the rule
    exactly. One query per column keeps every column's own type, which an
    unpivot into a single VARCHAR would throw away.
    """
    result = pd.DataFrame(index=pd.Index([], name="unit_id"))
    if not len(joined) or not columns:
        return result

    # The per-column modal vote is the heaviest thing this module does and
    # it grows with the record count, so it gets the shared caps too.
    con = duckdb_conn.connect(temp_dir)
    try:
        con.register("j", joined)
        frames = []
        for column in columns:
            quoted = _quote(column)
            frames.append(con.execute(f"""
                SELECT unit_id, {quoted} FROM (
                    SELECT unit_id, {quoted},
                           row_number() OVER (
                               PARTITION BY unit_id
                               ORDER BY count(*) DESC, min(record_id) ASC
                           ) AS rank
                    FROM j
                    WHERE {quoted} IS NOT NULL
                    GROUP BY unit_id, {quoted}
                ) WHERE rank = 1
            """).df().set_index("unit_id"))
    finally:
        con.close()
    return pd.concat(frames, axis=1) if frames else result


def _nullable(values, keep, index) -> pd.Series:
    """An object column whose missing entries are ``None``, never ``NaN``.

    ``Series.where(cond, None)`` fills with NaN, which then reads back as a
    float the API has to strip out again. numpy keeps the None.
    """
    return pd.Series(np.where(keep, values, None), index=index, dtype="object")


def _blank_to_null(series: pd.Series) -> pd.Series:
    """A blank existing id means "never reviewed", not an id of empty string."""
    text = series.astype("object").where(series.notna(), None)
    trimmed = pd.Series(text, index=series.index).map(
        lambda v: None if v is None or str(v).strip() == "" else str(v).strip()
    )
    return trimmed


def unit_membership(records: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    """``record_id``, ``unit_id`` and ``held_group_id`` for every record.

    A record in a merged group joins that group's unit. Everything else is its
    own unit, so a held group's members stay separate and can still be told
    apart on the review screen by the ``held_group_id`` they share.
    """
    record_ids = _as_text(records["record_id"])
    members = pd.DataFrame({"record_id": record_ids.to_numpy()})
    members["unit_id"] = members["record_id"]

    if len(groups):
        group_records = _as_text(groups["record_id"])
        merged = groups["status"].to_numpy() == keys.MERGED

        if merged.any():
            # The unit id is the smallest member record id, which is exactly what
            # the group id spells, but recomputing it keeps the two independent.
            merged_frame = pd.DataFrame({
                "record_id": group_records[merged].to_numpy(),
                "group_id": groups.loc[merged, "group_id"].to_numpy(),
            })
            smallest = merged_frame.groupby("group_id")["record_id"].min()
            merged_frame["unit_id"] = merged_frame["group_id"].map(smallest)
            mapping = merged_frame.set_index("record_id")["unit_id"]
            mapped = members["record_id"].map(mapping)
            members["unit_id"] = mapped.where(mapped.notna(), members["unit_id"])

        held = ~merged
        if held.any():
            held_frame = pd.DataFrame({
                "record_id": group_records[held].to_numpy(),
                "held_group_id": groups.loc[held, "group_id"].to_numpy(),
            })
            # A record may sit in several held groups; the smallest id is the
            # stable one to show.
            first = held_frame.groupby("record_id")["held_group_id"].min()
            mapped = members["record_id"].map(first)
            members["held_group_id"] = _nullable(mapped, mapped.notna(), members.index)
        else:
            members["held_group_id"] = None
    else:
        members["held_group_id"] = None

    return members


def _apply_profile_aggregates(units, joined, members, events) -> None:
    """Let the profile replace the columns a modal vote cannot get right.

    Anything the hook returns overwrites the representative's value for the
    units it names; a unit it leaves out keeps what the modal vote found, which
    is the correct answer for a unit of one.
    """
    profile = get_profile()
    with_units = None
    if events is not None and len(events):
        with_units = events.copy()
        with_units["record_id"] = _as_text(with_units["record_id"])
        with_units = with_units.merge(members, on="record_id", how="inner")

    replacements = profile.aggregate_unit_columns(joined, with_units)
    if replacements is None or not len(replacements):
        return
    for column in replacements.columns:
        values = replacements[column].reindex(units.index)
        if column in units.columns:
            units[column] = values.where(values.notna(), units[column])
        else:
            units[column] = values


def _representative_columns(records: pd.DataFrame, priority: list[str]) -> list[str]:
    skip = {"record_id", *priority, *_NOT_REPRESENTED}
    return [c for c in records.columns if c not in skip]


def _modal(frame: pd.DataFrame, column: str) -> pd.Series:
    """Most frequent non-null value of *column* per unit, ties to the smallest id.

    One groupby over (unit, value) pairs, then one sort. No loop over units.
    """
    sub = frame.loc[frame[column].notna(), ["unit_id", "record_id", column]]
    if not len(sub):
        return pd.Series(dtype="object")
    counted = (
        sub.groupby(["unit_id", column], sort=False, dropna=False)
        .agg(n=("record_id", "size"), first_id=("record_id", "min"))
        .reset_index()
        .sort_values(["unit_id", "n", "first_id"], ascending=[True, False, True],
                     kind="mergesort")
    )
    return counted.drop_duplicates(subset=["unit_id"]).set_index("unit_id")[column]


def build_units(
    records: pd.DataFrame, groups: pd.DataFrame, events: pd.DataFrame | None = None,
    temp_dir=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(units, unit_members)`` for *records* grouped by *groups*.

    ``units`` is one representative row per unit: every record column plus
    ``unit_id``, ``unit_size``, ``held_group_id``, the summed priority columns,
    ``existing_entity_id`` (the members' single id, null when they carry none or
    disagree), ``existing_entity_ids`` and ``n_existing_ids``.

    A modal vote is right for a name and wrong for a median, so a profile may
    then correct its own columns through ``Profile.aggregate_unit_columns``.
    *events* is the run's evidence rows, which that hook reads when there are any.
    """
    records = plain_strings(records.copy())
    records["record_id"] = _as_text(records["record_id"])
    groups = plain_strings(groups)

    members = unit_membership(records, groups)
    joined = records.merge(members, on="record_id", how="left")

    priority = [c for c in get_profile().priority_columns if c in records.columns]
    represented = _representative_columns(records, priority)

    sizes = joined.groupby("unit_id", sort=True).size().rename("unit_size")
    singles = set(sizes.index[sizes == 1])

    # Only pooled units need a modal vote. A unit of one is its own
    # representative, which is most of a donations run and nearly all of PSC.
    lone = joined[joined["unit_id"].isin(singles)]
    many = joined[~joined["unit_id"].isin(singles)]

    units = pd.DataFrame(index=sizes.index)
    units.index.name = "unit_id"
    # A unit of one is its own representative — most of a donations run and
    # nearly all of a PSC one — so only the pooled units are voted on.
    modal = representatives(many, represented, temp_dir) if len(many) else None
    for column in represented:
        column_values = pd.Series(index=sizes.index, dtype="object")
        if len(lone):
            column_values.loc[lone["unit_id"].to_numpy()] = lone[column].to_numpy()
        if modal is not None and column in modal.columns:
            found = modal[column].dropna()
            if len(found):
                column_values.loc[found.index] = found.to_numpy()
        units[column] = column_values.astype(records[column].dtype, errors="ignore")

    for column in priority:
        numbers = pd.to_numeric(joined[column], errors="coerce")
        units[column] = numbers.groupby(joined["unit_id"]).sum(min_count=1)

    units["unit_size"] = sizes.astype("int64")
    held = joined.groupby("unit_id")["held_group_id"].min()
    units["held_group_id"] = _nullable(held, held.notna(), held.index)

    labels = _blank_to_null(joined[LABEL_COLUMN]) if LABEL_COLUMN in joined.columns \
        else pd.Series([None] * len(joined), index=joined.index)
    labelled = pd.DataFrame({
        "unit_id": joined["unit_id"].to_numpy(),
        "label": labels.to_numpy(),
    }).dropna(subset=["label"]).drop_duplicates()
    if len(labelled):
        ids = (
            labelled.sort_values(["unit_id", "label"], kind="mergesort")
            .groupby("unit_id")["label"]
            .apply(lambda values: ID_SEPARATOR.join(values))
        )
        counts = labelled.groupby("unit_id")["label"].nunique()
    else:
        ids = pd.Series(dtype="object")
        counts = pd.Series(dtype="int64")
    joined_ids = units.index.map(ids)
    units["existing_entity_ids"] = _nullable(joined_ids, pd.notna(joined_ids),
                                             units.index)
    units["n_existing_ids"] = units.index.map(counts).fillna(0).astype("int64")
    # A unit with two old ids has no single id, so the import overlay cannot read
    # one off it. Storing that here means the overlay never has to split a string.
    units[LABEL_COLUMN] = _nullable(
        units["existing_entity_ids"], units["n_existing_ids"] == 1, units.index
    )

    _apply_profile_aggregates(units, joined, members, events)

    units = units.reset_index()
    ordered = ["unit_id", "unit_size", "held_group_id", LABEL_COLUMN,
               "existing_entity_ids", "n_existing_ids"]
    rest = [c for c in units.columns if c not in ordered]
    units = units[ordered + rest]

    members = members[["unit_id", "record_id"]].sort_values(
        ["unit_id", "record_id"], kind="mergesort"
    ).reset_index(drop=True)
    return units.sort_values("unit_id", kind="mergesort").reset_index(drop=True), members


def counts_from(units: pd.DataFrame) -> dict:
    """The run counts the unit build contributes, in the pipeline's snake_case."""
    by_track = units["track"].value_counts() if "track" in units.columns else pd.Series(dtype=int)
    counts = {"units_total": int(len(units))}
    for track, n in by_track.items():
        counts[f"units_{track}"] = int(n)
    return counts


def track_units(units: pd.DataFrame, track: str) -> pd.DataFrame:
    """The units of one track, ready for Splink (``unit_id`` is the unique id)."""
    if "track" not in units.columns:
        return units
    return units[units["track"] == track].reset_index(drop=True)

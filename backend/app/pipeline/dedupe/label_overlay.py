# backend/app/pipeline/dedupe/label_overlay.py
"""Turn human labels about records into decisions about this run's unit pairs.

A label names two records (`DESIGN.md` D10). A run names units. So every use of
a label starts by asking which unit holds each of its two records, and the
answer changes as the match keys change. There are three outcomes, and
`docs/LINKAGE.md` names them:

  satisfied      TRUE, and both records are already in one unit
  contradiction  FALSE, and both records are already in one unit — an exact key
                 has merged what a human kept apart
  applied        the records are in two different units, so the pair carries
                 the decision

An applied label whose pair the scorer never produced is **forced** into the
pairs file with no score, because a human decision must not be lost behind a
blocking rule.

The overlay itself is deliberately tiny: TRUE accepts, FALSE rejects, and
``decided_by`` becomes ``human``. ``pairs_reader`` does the same thing in SQL at
read time, and a test holds the two to the same answer.
"""

import numpy as np
import pandas as pd

from app.rules import keys

# What a label carries onto the pair it decides.
CARRIED = ("is_match", "reviewer", "created_at", "notes", "evidence_url",
           "provenance", "held_out")


def _empty(columns) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})


def to_unit_pairs(labels: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """Each label with the units that now hold its two records.

    Adds ``unit_id_l`` / ``unit_id_r`` in string order and ``same_unit``. A
    label naming a record this run does not have is dropped: it belongs to
    another dataset, or to a record the loader no longer keeps.
    """
    columns = list(labels.columns) + ["unit_id_l", "unit_id_r", "same_unit"]
    if not len(labels):
        return _empty(columns)

    lookup = pd.Series(
        members["unit_id"].astype(str).to_numpy(),
        index=members["record_id"].astype(str).to_numpy(),
    )
    # A record may appear once; duplicated ids would make the map ambiguous.
    lookup = lookup[~lookup.index.duplicated(keep="first")]

    frame = labels.copy()
    left = frame["record_id_a"].astype(str).map(lookup)
    right = frame["record_id_b"].astype(str).map(lookup)
    known = left.notna() & right.notna()
    frame = frame[known.to_numpy()].copy()
    if not len(frame):
        return _empty(columns)
    left, right = left[known], right[known]

    lower = np.where(left.to_numpy() <= right.to_numpy(), left.to_numpy(), right.to_numpy())
    upper = np.where(left.to_numpy() <= right.to_numpy(), right.to_numpy(), left.to_numpy())
    frame["unit_id_l"] = lower
    frame["unit_id_r"] = upper
    frame["same_unit"] = lower == upper
    return frame.reset_index(drop=True)


def in_this_run(labels: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """The labels whose two records this run actually loaded.

    A label about records from another dataset, or from a record the loader no
    longer keeps, is not this run's business. Everything else is — whether the
    exact keys have already satisfied it, whether a pair carries it, or neither.
    """
    if not len(labels):
        return labels
    known = set(members["record_id"].astype(str))
    keep = (labels["record_id_a"].astype(str).isin(known)
            & labels["record_id_b"].astype(str).isin(known))
    return labels[keep.to_numpy()]


def outcomes(
    labels: pd.DataFrame, members: pd.DataFrame, groups: pd.DataFrame
) -> dict:
    """``{applied, satisfied, contradictions}`` for this run's units.

    ``contradictions`` carries what the screen needs to act: the two records,
    the unit and merged group that now hold them, and the match keys that did it.
    """
    mapped = to_unit_pairs(labels, members)
    if not len(mapped):
        return {"applied": mapped, "satisfied": mapped, "contradictions": []}

    is_true = mapped["is_match"].astype(str).str.upper().to_numpy() == "TRUE"
    same = mapped["same_unit"].to_numpy()

    applied = mapped[~same].reset_index(drop=True)
    satisfied = mapped[same & is_true].reset_index(drop=True)
    clashing = mapped[same & ~is_true].reset_index(drop=True)

    merged = groups[groups["status"] == keys.MERGED] if len(groups) else groups
    by_record = (
        merged.set_index(merged["record_id"].astype(str))[["group_id", "key_ids"]]
        if len(merged) else None
    )

    contradictions = []
    for row in clashing.to_dict("records"):
        group_id, key_ids = None, []
        if by_record is not None:
            found = by_record.reindex([str(row["record_id_a"])]).iloc[0]
            group_id = None if pd.isna(found.get("group_id")) else found["group_id"]
            raw = found.get("key_ids")
            key_ids = [k for k in str(raw or "").split("|") if k] if not pd.isna(raw) else []
        contradictions.append({
            "record_id_a": str(row["record_id_a"]),
            "record_id_b": str(row["record_id_b"]),
            "unit_id": str(row["unit_id_l"]),
            "group_id": group_id,
            "key_ids": key_ids,
            "track": row.get("track"),
            "name_a": row.get("name_a"),
            "name_b": row.get("name_b"),
            "label": {field: row.get(field) for field in CARRIED},
        })
    return {"applied": applied, "satisfied": satisfied, "contradictions": contradictions}


def decisions(applied: pd.DataFrame) -> pd.DataFrame:
    """``unit_id_l``, ``unit_id_r``, ``is_match`` — one row per decided pair."""
    if not len(applied):
        return _empty(["unit_id_l", "unit_id_r", "is_match"])
    frame = applied[["unit_id_l", "unit_id_r", "is_match"]].copy()
    frame["is_match"] = frame["is_match"].astype(str).str.upper()
    return frame.drop_duplicates(subset=["unit_id_l", "unit_id_r"], keep="last")


def apply_to_pairs(pairs: pd.DataFrame, applied: pd.DataFrame) -> pd.DataFrame:
    """The pairs with the human overlay on top of the score and the import one.

    ``pairs.parquet`` never holds this: the overlay is cheap to redo and a label
    write must not rewrite a file with millions of rows in it. The readers join
    the same thing in SQL.
    """
    result = pairs.copy()
    if not len(result):
        return result
    if not len(applied):
        return result

    keyed = decisions(applied).set_index(["unit_id_l", "unit_id_r"])["is_match"]
    index = pd.MultiIndex.from_arrays([
        result["unit_id_l"].astype(str), result["unit_id_r"].astype(str),
    ])
    verdict = pd.Series(index.map(keyed), index=result.index)
    decided = verdict.notna().to_numpy()
    true = (verdict.astype(str).str.upper() == "TRUE").to_numpy()

    result["bucket"] = np.where(decided, np.where(true, "accept", "reject"),
                                result["bucket"])
    result["decided_by"] = np.where(decided, "human", result["decided_by"])
    return result


def forced_rows(applied: pd.DataFrame, pairs: pd.DataFrame, units: pd.DataFrame) -> pd.DataFrame:
    """Labelled pairs the scorer never produced, shaped like scored ones.

    Blocking may never have put the two units together, or the score may have
    fallen under the candidate floor. Either way the decision exists, so the
    pair has to exist too — with no score, which is the honest thing to show.
    """
    if not len(applied):
        return pairs.iloc[0:0]

    wanted = decisions(applied)
    if len(pairs):
        existing = set(zip(pairs["unit_id_l"].astype(str), pairs["unit_id_r"].astype(str)))
    else:
        existing = set()
    missing = wanted[[
        (left, right) not in existing
        for left, right in zip(wanted["unit_id_l"], wanted["unit_id_r"])
    ]]
    if not len(missing):
        return pairs.iloc[0:0]

    tracks = units.set_index(units["unit_id"].astype(str))["track"] \
        if "track" in units.columns else None
    rows = pd.DataFrame({
        "unit_id_l": missing["unit_id_l"].astype(str).to_numpy(),
        "unit_id_r": missing["unit_id_r"].astype(str).to_numpy(),
        "match_probability": np.nan,
        "match_weight": np.nan,
    })
    rows["track"] = rows["unit_id_l"].map(tracks) if tracks is not None else None
    return rows.reset_index(drop=True)

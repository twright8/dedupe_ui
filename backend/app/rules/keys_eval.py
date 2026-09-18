# backend/app/rules/keys_eval.py
"""Score the match keys against the labels a previous review already made.

Every record carries ``existing_entity_id``, which is null when nobody has ever
reviewed it. Those old decisions are the only ground truth there is, so a key
change is judged by what it does to them:

  precision  of the labelled pairs a key merges, how many the reviewers had
             already put together
  recall     of the pairs the reviewers put together, how many a key finds

Both are counted from group sizes with n·(n−1)/2, never by walking pairs — a
single donations group has over a thousand members, and a PSC group is worse.

The donations labels have a known bias (DESIGN.md D13): reviewers merged 99.6%
of identical-name pairs of individuals, so they cannot show when two people
with the same name are different. Precision here is therefore an agreement
figure, not an accuracy figure.
"""

import numpy as np
import pandas as pd

from app.rules import keys

AGREEMENTS = ("consistent", "conflict", "extends", "new")

LABEL_COLUMN = "existing_entity_id"


def _pair_count(sizes) -> int:
    """n·(n−1)/2, summed. The whole reason this module has no pairwise loop."""
    values = np.asarray(sizes, dtype=np.int64)
    if values.size == 0:
        return 0
    return int((values * (values - 1) // 2).sum())


def _labels(records: pd.DataFrame) -> pd.DataFrame:
    """``record_id``, ``track`` and the existing entity id, blanks made null."""
    frame = pd.DataFrame({"record_id": records["record_id"].astype(str)})
    frame["track"] = (
        records["track"].astype("object").to_numpy() if "track" in records.columns else None
    )
    if LABEL_COLUMN in records.columns:
        label = records[LABEL_COLUMN].astype("object")
        label = label.where(label.notna(), None)
        blank = pd.Series(label).map(lambda v: v is None or str(v).strip() == "")
        frame[LABEL_COLUMN] = label.where(~blank.to_numpy(), None).to_numpy()
    else:
        frame[LABEL_COLUMN] = None
    return frame


def group_agreement(records: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    """One row per merged group: size, labelled members, distinct ids, agreement.

    ``conflict`` when two or more distinct existing ids meet in one group;
    otherwise ``new`` when no member is labelled; otherwise ``extends`` when the
    group brings an unlabelled member to a settled id; otherwise ``consistent``.
    """
    labels = _labels(records)
    merged = groups[groups["status"] == keys.MERGED] if len(groups) else groups
    if not len(merged):
        return pd.DataFrame(
            columns=["group_id", "track", "size", "n_labelled", "n_ids",
                     "n_unlabelled", "agreement"]
        )

    joined = merged[["record_id", "group_id", "track"]].merge(
        labels[["record_id", LABEL_COLUMN]], on="record_id", how="left"
    )
    agg = joined.groupby("group_id", sort=True).agg(
        track=("track", "first"),
        size=("record_id", "size"),
        n_labelled=(LABEL_COLUMN, "count"),
        n_ids=(LABEL_COLUMN, "nunique"),
    ).reset_index()
    agg["n_unlabelled"] = agg["size"] - agg["n_labelled"]

    agreement = np.where(
        agg["n_ids"] >= 2, "conflict",
        np.where(agg["n_labelled"] == 0, "new",
                 np.where(agg["n_unlabelled"] > 0, "extends", "consistent")),
    )
    agg["agreement"] = agreement
    return agg


def _precision(joined: pd.DataFrame) -> tuple[int, int]:
    """``(pairs the labels agree on, labelled pairs inside merged groups)``."""
    labelled = joined[joined[LABEL_COLUMN].notna()]
    if not len(labelled):
        return 0, 0
    per_group = labelled.groupby("group_id", sort=False).size()
    per_cell = labelled.groupby(["group_id", LABEL_COLUMN], sort=False).size()
    return _pair_count(per_cell), _pair_count(per_group)


def _recall(labels: pd.DataFrame, merged: pd.DataFrame) -> tuple[int, int]:
    """``(manual pairs a merged group also holds, manual pairs)``."""
    labelled = labels[labels[LABEL_COLUMN].notna()]
    if not len(labelled):
        return 0, 0
    manual = _pair_count(labelled.groupby(LABEL_COLUMN, sort=False).size())
    if not len(merged):
        return 0, manual
    joined = labelled.merge(
        merged[["record_id", "group_id"]], on="record_id", how="inner"
    )
    if not len(joined):
        return 0, manual
    found = _pair_count(joined.groupby([LABEL_COLUMN, "group_id"], sort=False).size())
    return found, manual


def _ratio(numerator: int, denominator: int):
    """A share, or None when there is nothing to divide — never a silent zero."""
    return round(numerator / denominator, 6) if denominator else None


def _scores(labels: pd.DataFrame, merged: pd.DataFrame) -> dict:
    joined = merged[["record_id", "group_id"]].merge(
        labels[["record_id", LABEL_COLUMN]], on="record_id", how="left"
    ) if len(merged) else pd.DataFrame(columns=["record_id", "group_id", LABEL_COLUMN])
    same, labelled_pairs = _precision(joined)
    found, manual_pairs = _recall(labels, merged)
    return {
        "pair_precision": _ratio(same, labelled_pairs),
        "pair_recall": _ratio(found, manual_pairs),
        "labelled_pairs": labelled_pairs,
        "labelled_pairs_agreeing": same,
        "manual_pairs": manual_pairs,
        "manual_pairs_found": found,
    }


def evaluate(records: pd.DataFrame, groups: pd.DataFrame) -> dict:
    """What the merged groups do to the labels a previous review already made."""
    labels = _labels(records)
    merged = (
        groups[groups["status"] == keys.MERGED][["record_id", "group_id"]]
        if len(groups) else pd.DataFrame(columns=["record_id", "group_id"])
    )

    agg = group_agreement(records, groups)
    by_agreement = {name: 0 for name in AGREEMENTS}
    if len(agg):
        for name, count in agg["agreement"].value_counts().items():
            by_agreement[str(name)] = int(count)

    extends = agg[agg["agreement"] == "extends"] if len(agg) else agg
    attached = int(extends["n_unlabelled"].sum()) if len(extends) else 0

    result = {
        **_scores(labels, merged),
        "by_agreement": by_agreement,
        "conflicts": by_agreement["conflict"],
        "records_attached": attached,
        "labelled_records": int(labels[LABEL_COLUMN].notna().sum()),
        "by_track": {},
    }

    for track in sorted({t for t in labels["track"].dropna().unique()}):
        track_ids = set(labels.loc[labels["track"] == track, "record_id"])
        result["by_track"][str(track)] = _scores(
            labels[labels["track"] == track],
            merged[merged["record_id"].isin(track_ids)] if len(merged) else merged,
        )
    return result

# backend/app/pipeline/dedupe/exact_overlay.py
"""Let a human's decisions change what the exact keys grouped.

Stage 2 groups records that agree on a match key. A reviewer may disagree, and
`docs/ENTITIES.md` says what happens then:

* a merged group holding a human FALSE pair is **dissolved** into the parts its
  human TRUE labels connect, and a record no TRUE label reaches becomes a unit
  of its own;
* a held group a reviewer has merged becomes a **merged** group, its ``key_ids``
  carrying ``human`` beside the key that first proposed it;
* a FALSE pair inside a group nobody has otherwise decided is not a split. It is
  the contradiction slice 3 already reported: a rule now says "same" where a
  human said "not the same", and the rule is what needs fixing.

Nothing here walks a pair of records. The parts come from SciPy over the label
graph, and everything else is a merge or a groupby.
"""

import numpy as np
import pandas as pd

from app.rules import keys

MERGED_PREFIX = keys.MERGED_PREFIX
HUMAN_KEY = "human"


def _components(nodes: np.ndarray, edges: list[tuple[str, str]]) -> pd.Series:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    position = pd.Series(np.arange(len(nodes)), index=nodes)
    if not len(nodes):
        return pd.Series(dtype="int64")
    rows, cols = [], []
    for left, right in edges:
        if left in position.index and right in position.index:
            rows.append(position[left])
            cols.append(position[right])
    graph = coo_matrix(
        (np.ones(len(rows), dtype="int8"), (rows, cols)),
        shape=(len(nodes), len(nodes)),
    )
    _count, labels = connected_components(graph, directed=False)
    return pd.Series(labels, index=nodes)


def _verdict_pairs(labels: pd.DataFrame, verdict: str) -> list[tuple[str, str]]:
    if labels is None or not len(labels):
        return []
    wanted = labels[labels["is_match"].astype(str).str.upper() == verdict]
    return list(zip(wanted["record_id_a"].astype(str), wanted["record_id_b"].astype(str)))


def apply_human_overlay(
    groups: pd.DataFrame,
    labels: pd.DataFrame | None,
    decisions: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """``(groups, report)`` with the reviewers' decisions applied.

    *decisions* is ``{scope: decision}`` from ``pair_labels.decisions_by_scope``;
    a held group is merged when its own id carries a ``merge`` decision.
    """
    report = {"split_groups": [], "merged_held_groups": [], "contradictions": [],
              "records_freed": 0}
    if not len(groups):
        return groups, report

    frame = groups.copy()
    frame["record_id"] = frame["record_id"].astype(str)
    for column in ("split_by_human", "merged_by_human"):
        if column not in frame.columns:
            frame[column] = False

    true_pairs = _verdict_pairs(labels, "TRUE")
    false_pairs = _verdict_pairs(labels, "FALSE")
    merged = frame[frame["status"] == keys.MERGED]
    group_of = pd.Series(merged["group_id"].to_numpy(),
                         index=merged["record_id"].to_numpy())
    group_of = group_of[~group_of.index.duplicated(keep="first")]

    def _inside(pairs):
        found = {}
        for left, right in pairs:
            gl, gr = group_of.get(left), group_of.get(right)
            if gl is not None and gl == gr:
                found.setdefault(gl, []).append((left, right))
        return found

    false_inside = _inside(false_pairs)
    true_inside = _inside(true_pairs)

    # A group with a FALSE pair and no TRUE label has not been decided — that is
    # the contradiction, not a split.
    to_split = {g: p for g, p in false_inside.items() if g in true_inside}
    for group_id, pairs in false_inside.items():
        if group_id in to_split:
            continue
        for left, right in pairs:
            report["contradictions"].append({
                "group_id": group_id, "record_id_a": left, "record_id_b": right,
            })

    changed = False
    if to_split:
        frame, freed = _split_groups(frame, to_split, true_inside)
        report["split_groups"] = sorted(to_split)
        report["records_freed"] = freed
        changed = True

    merged_held = _held_to_merge(frame, decisions or {})
    if merged_held:
        frame = _merge_held(frame, merged_held)
        report["merged_held_groups"] = sorted(merged_held)
        changed = True

    if changed:
        frame = _renumber(frame)
    return frame.reset_index(drop=True), report


def _split_groups(frame, to_split, true_inside):
    """Dissolve each decided group into the parts its TRUE labels connect."""
    freed = 0
    keep_mask = np.ones(len(frame), dtype=bool)
    new_ids = frame["group_id"].to_numpy().copy()
    split_flag = frame["split_by_human"].to_numpy().copy()
    positions = {gid: np.flatnonzero(
        (frame["group_id"].to_numpy() == gid)
        & (frame["status"].to_numpy() == keys.MERGED)
    ) for gid in to_split}

    for group_id, rows in positions.items():
        members = frame["record_id"].to_numpy()[rows]
        parts = _components(members, true_inside.get(group_id, []))
        sizes = parts.value_counts()
        for index, row in enumerate(rows):
            part = parts.iloc[index]
            if sizes[part] < 2:
                # No TRUE label reaches this record: it stands alone now.
                keep_mask[row] = False
                freed += 1
                continue
            smallest = min(members[parts.to_numpy() == part])
            new_ids[row] = f"{MERGED_PREFIX}{smallest}"
            split_flag[row] = True

    frame = frame.copy()
    frame["group_id"] = new_ids
    frame["split_by_human"] = split_flag
    return frame[keep_mask], freed


def _held_to_merge(frame, decisions: dict) -> set:
    """The held groups a reviewer has asked to merge."""
    held_ids = set(frame.loc[frame["status"] == keys.HELD, "group_id"])
    return {
        scope for scope, decision in decisions.items()
        if scope in held_ids and decision.get("kind") == "merge"
    }


def _merge_held(frame, merged_held: set):
    """Turn a decided held group into a merged one, crediting the human."""
    frame = frame.copy()
    mask = (frame["status"] == keys.HELD) & frame["group_id"].isin(merged_held)
    frame.loc[mask, "status"] = keys.MERGED
    frame.loc[mask, "merged_by_human"] = True
    frame.loc[mask, "guard"] = None
    frame.loc[mask, "key_ids"] = (
        frame.loc[mask, "key_ids"].astype(str) + "|" + HUMAN_KEY
    )
    return frame


def _renumber(frame):
    """Re-unite the merged rows and give every group its smallest member's id.

    A held group a reviewer merged may share a record with a group a key made,
    and two dissolved parts may have to be joined again. Re-running the union
    over the record/group graph settles both at once.
    """
    merged = frame[frame["status"] == keys.MERGED]
    if not len(merged):
        return frame
    records = merged["record_id"].astype(str).to_numpy()
    group_ids = merged["group_id"].astype(str).to_numpy()
    nodes = np.array(sorted(set(records) | {f"g::{g}" for g in group_ids}))
    edges = list(zip(records, [f"g::{g}" for g in group_ids]))
    component = _components(nodes, edges)

    part = pd.Series(component.reindex(records).to_numpy(), index=merged.index)
    smallest = pd.Series(records, index=merged.index).groupby(part).min()
    frame = frame.copy()
    frame.loc[merged.index, "group_id"] = (
        MERGED_PREFIX + part.map(smallest).astype(str)
    )
    # One record may now sit in one merged group only; a re-union can leave two
    # rows for the same record and group, so the duplicates go.
    merged_rows = frame["status"] == keys.MERGED
    deduped = frame[merged_rows].drop_duplicates(subset=["record_id", "group_id"])
    keyed = (
        deduped.groupby(["group_id", "record_id"], sort=False)["key_ids"]
        .apply(lambda values: "|".join(sorted({
            key for value in values for key in str(value).split("|") if key
        })))
    )
    deduped = deduped.set_index(["group_id", "record_id"])
    deduped["key_ids"] = keyed
    deduped = deduped.reset_index()
    return pd.concat([deduped, frame[~merged_rows]], ignore_index=True)

# backend/app/model/dataset.py
"""Turn a run's pairs and the label store into training rows, weights and folds.

`MODEL.md` sets out the table this builds:

| source | use |
|---|---|
| human labels, `held_out = 0` | training, full weight |
| human labels, `held_out = 1` | the frozen test set, never trained on |
| group decisions (`cluster_merge`, `cluster_split`) | as human labels |
| imported labels that agree | positives, reduced weight, sampled |
| imported labels that disagree | weak negatives, lower weight still |

An **imported label** is not a row in `pair_labels`. It is what the import
overlay already worked out in stage 3 and wrote onto the pair: `decided_by` is
`import` when both units carry the same single old entity id, and
`import_disagrees` is true when they carry different ones (`LINKAGE.md`).
Reading it back off the pairs file means training and the review screen can
never disagree about which pairs the old work joined up.

Folds split on **connected components over units**, never on pairs. Two pairs
that share a unit are two views of the same donor, so putting one in the
training half and one in the validation half would let the model learn the
donor rather than the evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# The provenances that count as a human decision about this pair.
HUMAN_PROVENANCES = ("manual", "bulk_range", "llm")
DECISION_PROVENANCES = ("cluster_merge", "cluster_split")

SOURCES = ("human", "decision", "import_agree", "import_disagree")

# The column `pairs.parquet` carries the import overlay in.
DECIDED_BY = "decided_by"
IMPORT_DISAGREES = "import_disagrees"


@dataclass
class TrainingSet:
    """Everything the trainer needs, already lined up row for row."""

    index: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    y: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    weight: np.ndarray = field(default_factory=lambda: np.array([], dtype=float))
    source: np.ndarray = field(default_factory=lambda: np.array([], dtype=object))
    fold: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    # The connected component of units each row belongs to. Kept beside the
    # folds so the ablation can re-split at a different fold count and still
    # never put one donor on both sides.
    group: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    # The frozen test set, as positions into the same feature frame.
    test_index: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    test_y: np.ndarray = field(default_factory=lambda: np.array([], dtype=int))
    n_human: int = 0
    n_groups: int = 0
    counts: list[dict] = field(default_factory=list)
    sampling: dict = field(default_factory=dict)

    @property
    def n_rows(self) -> int:
        return int(len(self.index))

    @property
    def is_human(self) -> np.ndarray:
        return np.isin(self.source, ("human", "decision"))


# ---------------------------------------------------------------------------
# Resolving human labels onto this run's pairs
# ---------------------------------------------------------------------------


def _unit_of(members: pd.DataFrame) -> pd.Series:
    if members is None or not len(members):
        return pd.Series(dtype="object")
    return pd.Series(
        members["unit_id"].astype(str).to_numpy(),
        index=members["record_id"].astype(str).to_numpy(),
    )


def _ordered(left: np.ndarray, right: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The two ids the way `pairs.parquet` holds them: smaller first, as text."""
    swap = left > right
    return np.where(swap, right, left), np.where(swap, left, right)


def resolve_labels(labels: pd.DataFrame, members: pd.DataFrame,
                   track: str) -> pd.DataFrame:
    """The active labels as unit pairs, with their source and their verdict.

    A label names two **records**. In this run those records sit in whichever
    units stage 2 left them in, so the label is re-pointed onto that unit pair.
    A label whose two records now sit in one unit is already satisfied and
    carries no information about a pair; a label naming a record this run never
    loaded belongs to another dataset. Both drop out here (`LINKAGE.md`).
    """
    columns = ["unit_id_l", "unit_id_r", "y", "source", "held_out"]
    if labels is None or not len(labels):
        return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})

    frame = labels.copy()
    if "track" in frame.columns:
        frame = frame[frame["track"].isna() | (frame["track"] == track)]
    if not len(frame):
        return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})

    lookup = _unit_of(members)
    left = frame["record_id_a"].astype(str).map(lookup)
    right = frame["record_id_b"].astype(str).map(lookup)
    keep = left.notna() & right.notna() & (left != right)
    if not keep.any():
        return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})

    frame = frame[keep]
    ordered_l, ordered_r = _ordered(left[keep].to_numpy().astype(str),
                                    right[keep].to_numpy().astype(str))
    provenance = frame["provenance"].fillna("manual").astype(str).to_numpy()
    verdict = frame["is_match"].astype(str).str.upper().to_numpy()
    y = (verdict == "TRUE").astype(int)

    source = np.where(np.isin(provenance, DECISION_PROVENANCES), "decision", "human")
    # A row someone loaded with provenance 'import' is an imported label, not a
    # reviewer's decision, and is weighted like one.
    imported = provenance == "import"
    source = np.where(imported & (y == 1), "import_agree", source)
    source = np.where(imported & (y == 0), "import_disagree", source)

    out = pd.DataFrame({
        "unit_id_l": ordered_l,
        "unit_id_r": ordered_r,
        "y": y,
        "source": source,
        "held_out": pd.to_numeric(frame["held_out"], errors="coerce")
                      .fillna(0).astype(int).to_numpy(),
    })
    # A pair labelled twice cannot happen — the store keeps one active row per
    # pair — but a re-pointing can land two labels on one unit pair. The frozen
    # one wins, so designating a test set is never undone by a later merge.
    return out.sort_values("held_out", ascending=False, kind="mergesort") \
              .drop_duplicates(subset=["unit_id_l", "unit_id_r"], keep="first") \
              .reset_index(drop=True)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _stable_order(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """A deterministic shuffle of pair ids, the same on every machine and run.

    A hash of the pair id rather than a random number generator: the same run
    trained twice gives the same rows, and adding a pair elsewhere in the file
    does not re-shuffle everything.
    """
    digests = [
        int.from_bytes(hashlib.blake2b(f"{a}|{b}".encode(), digest_size=8).digest(),
                       "big")
        for a, b in zip(left, right)
    ]
    return np.argsort(np.asarray(digests, dtype="uint64"), kind="stable")


def _cap(n_available: int, n_human: int, settings: dict) -> tuple[int, str | None]:
    """How many imported positives to keep, and which limit bound."""
    absolute = int(settings["import_agree_max_rows"])
    per_human = int(settings["import_agree_per_human_label"]) * int(n_human)
    limit, which = absolute, "import_agree_max_rows"
    if n_human > 0 and per_human < absolute:
        limit, which = per_human, "import_agree_per_human_label"
    if n_available <= limit:
        return n_available, None
    return limit, which


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------


def components(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """A group id per pair: the connected component of units it belongs to."""
    if not len(left):
        return np.array([], dtype=int)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    units = pd.unique(np.concatenate([left, right]))
    position = pd.Series(np.arange(len(units)), index=units)
    li = position.reindex(left).to_numpy()
    ri = position.reindex(right).to_numpy()
    graph = coo_matrix(
        (np.ones(len(li), dtype=np.int8), (li, ri)), shape=(len(units), len(units))
    )
    _n, labels = connected_components(graph, directed=False)
    return labels[li]


def assign_folds(group: np.ndarray, n_folds: int) -> np.ndarray:
    """Spread whole groups over *n_folds*, keeping the folds a similar size.

    Biggest group first into the emptiest fold. Greedy, deterministic, and it
    copes with the shape this data actually has: a few enormous components of
    one common surname beside thousands of pairs on their own.
    """
    if not len(group):
        return np.array([], dtype=int)
    n_folds = max(1, int(n_folds))
    sizes = pd.Series(group).value_counts()
    sizes = sizes.sort_values(ascending=False, kind="mergesort")
    load = np.zeros(n_folds, dtype="int64")
    fold_of: dict = {}
    for key, size in sizes.items():
        chosen = int(np.argmin(load))
        fold_of[key] = chosen
        load[chosen] += int(size)
    return pd.Series(group).map(fold_of).to_numpy().astype(int)


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def build(pairs: pd.DataFrame, labels: pd.DataFrame, members: pd.DataFrame,
          track: str, settings: dict) -> TrainingSet:
    """The training set for one track's *pairs*, as positions into that frame.

    *pairs* must be the same frame the features were built from, in the same
    order, because everything here is positional.
    """
    pairs = pairs.reset_index(drop=True)
    if not len(pairs):
        return TrainingSet(counts=_empty_counts(settings),
                           sampling=_empty_sampling(settings))

    left = pairs["unit_id_l"].astype(str).to_numpy()
    right = pairs["unit_id_r"].astype(str).to_numpy()
    position = pd.Series(np.arange(len(pairs)),
                         index=pd.MultiIndex.from_arrays([left, right]))
    position = position[~position.index.duplicated()]

    resolved = resolve_labels(labels, members, track)
    row_of = position.reindex(
        pd.MultiIndex.from_arrays([resolved["unit_id_l"].to_numpy(),
                                   resolved["unit_id_r"].to_numpy()])
    ).to_numpy() if len(resolved) else np.array([])
    found = pd.notna(row_of)
    resolved = resolved[found].assign(row=row_of[found].astype(int))

    human = resolved[resolved["source"].isin(("human", "decision"))]
    train_human = human[human["held_out"] == 0]
    test_human = human[human["held_out"] == 1]
    # An imported label loaded through the label store joins the imported rows
    # from the pairs file below, and never the test set.
    loaded_import = resolved[resolved["source"].isin(("import_agree", "import_disagree"))]

    decided = set(human["row"].tolist())

    decided_by = pairs[DECIDED_BY].astype(str).to_numpy() if DECIDED_BY in pairs.columns \
        else np.full(len(pairs), "score")
    disagrees = pairs[IMPORT_DISAGREES].fillna(False).to_numpy().astype(bool) \
        if IMPORT_DISAGREES in pairs.columns else np.zeros(len(pairs), dtype=bool)

    free = ~np.isin(np.arange(len(pairs)), list(decided))
    agree_rows = np.flatnonzero((decided_by == "import") & free)
    disagree_rows = np.flatnonzero(disagrees & free & (decided_by != "import"))
    if len(loaded_import):
        extra_agree = loaded_import.loc[loaded_import["source"] == "import_agree", "row"]
        extra_disagree = loaded_import.loc[
            loaded_import["source"] == "import_disagree", "row"]
        agree_rows = np.union1d(agree_rows, extra_agree.to_numpy().astype(int))
        disagree_rows = np.union1d(disagree_rows, extra_disagree.to_numpy().astype(int))
        disagree_rows = np.setdiff1d(disagree_rows, agree_rows)

    n_human = int(len(human))
    n_agree_available = int(len(agree_rows))
    keep, which = _cap(len(agree_rows), n_human, settings)
    if keep < len(agree_rows):
        order = _stable_order(left[agree_rows], right[agree_rows])
        agree_rows = np.sort(agree_rows[order[:keep]])

    # A reviewer's own decision and a whole-group decision train the same way but
    # are counted apart, because "50 human labels" and "one merge of 50 records"
    # are very different amounts of looking.
    is_decision = (train_human["source"] == "decision").to_numpy()
    human_rows = train_human["row"].to_numpy().astype(int)
    human_y = train_human["y"].to_numpy().astype(int)
    blocks = [
        ("human", human_rows[~is_decision], human_y[~is_decision],
         float(settings["human_weight"])),
        ("decision", human_rows[is_decision], human_y[is_decision],
         float(settings["decision_weight"])),
        ("import_agree", agree_rows, np.ones(len(agree_rows), dtype=int),
         float(settings["import_agree_weight"])),
        ("import_disagree", disagree_rows, np.zeros(len(disagree_rows), dtype=int),
         float(settings["import_disagree_weight"])),
    ]

    index = np.concatenate([b[1] for b in blocks]).astype(int)
    y = np.concatenate([b[2] for b in blocks]).astype(int)
    weight = np.concatenate([np.full(len(b[1]), b[3]) for b in blocks]).astype(float)
    source = np.concatenate([np.full(len(b[1]), b[0], dtype=object) for b in blocks])

    order = np.argsort(index, kind="stable")
    index, y, weight, source = index[order], y[order], weight[order], source[order]

    group = components(left[index], right[index])
    fold = assign_folds(group, int(settings["n_folds"]))

    counts = [
        {"source": name, "held_out": 0, "rows": int(len(rows)),
         "positives": int(ys.sum()), "negatives": int(len(ys) - ys.sum()),
         "weight": w,
         "capped": bool(name == "import_agree" and which is not None)}
        for name, rows, ys, w in blocks
    ]
    test_y = test_human["y"].to_numpy().astype(int)
    counts.append({
        "source": "human", "held_out": 1, "rows": int(len(test_human)),
        "positives": int(test_y.sum()), "negatives": int(len(test_y) - test_y.sum()),
        "weight": None, "capped": False,
    })

    return TrainingSet(
        index=index, y=y, weight=weight, source=source, fold=fold, group=group,
        test_index=test_human["row"].to_numpy().astype(int), test_y=test_y,
        n_human=n_human, n_groups=int(len(np.unique(group))) if len(group) else 0,
        counts=counts,
        sampling={
            "import_agree_available": n_agree_available,
            "import_agree_kept": int(len(agree_rows)),
            "cap": int(settings["import_agree_max_rows"]),
            "per_human_label": int(settings["import_agree_per_human_label"]),
            "cap_applied": which,
        },
    )


def _empty_counts(settings: dict) -> list[dict]:
    weights = {"human": settings["human_weight"], "decision": settings["decision_weight"],
               "import_agree": settings["import_agree_weight"],
               "import_disagree": settings["import_disagree_weight"]}
    rows = [{"source": s, "held_out": 0, "rows": 0, "positives": 0, "negatives": 0,
             "weight": float(weights[s]), "capped": False} for s in SOURCES]
    rows.append({"source": "human", "held_out": 1, "rows": 0, "positives": 0,
                 "negatives": 0, "weight": None, "capped": False})
    return rows


def _empty_sampling(settings: dict) -> dict:
    return {"import_agree_available": 0, "import_agree_kept": 0,
            "cap": int(settings["import_agree_max_rows"]),
            "per_human_label": int(settings["import_agree_per_human_label"]),
            "cap_applied": None}

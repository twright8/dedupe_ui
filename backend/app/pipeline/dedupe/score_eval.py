# backend/app/pipeline/dedupe/score_eval.py
"""Score the exact groups plus the accepted pairs against the existing labels.

``exact_eval.json`` asks what the match keys alone do to the decisions a
previous review made. This asks the same of the whole chain: the exact groups,
joined up by every pair the scorer accepted. It is what the owner tunes rules
against, so the arithmetic here has to be right.

Entities come from connected components over the units — the accepted pairs are
edges, and SciPy labels the components, so nothing walks a pair of records.
Precision and recall then reuse ``rules/keys_eval``, which counts from group
sizes with n·(n−1)/2, so the figures here and in ``exact_eval.json`` mean the
same thing and can be put side by side.
"""

import numpy as np
import pandas as pd

from app.pipeline.dedupe import label_overlay
from app.rules import keys, keys_eval

HISTOGRAM_BINS = 50

AGREEMENTS = ("agrees", "disagrees", "unknown")


def components(units: pd.DataFrame, accepted: pd.DataFrame) -> pd.Series:
    """A component id per unit: the units the accepted pairs join together."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    ids = units["unit_id"].astype(str).to_numpy()
    position = pd.Series(np.arange(len(ids)), index=ids)
    n = len(ids)
    if n == 0:
        return pd.Series(dtype="int64")

    if len(accepted):
        left = accepted["unit_id_l"].astype(str).map(position)
        right = accepted["unit_id_r"].astype(str).map(position)
        keep = left.notna() & right.notna()
        rows = left[keep].to_numpy(dtype="int64")
        cols = right[keep].to_numpy(dtype="int64")
    else:
        rows = np.empty(0, dtype="int64")
        cols = np.empty(0, dtype="int64")

    graph = coo_matrix(
        (np.ones(len(rows), dtype="int8"), (rows, cols)), shape=(n, n)
    )
    _count, labels = connected_components(graph, directed=False)
    return pd.Series(labels, index=ids, name="component")


def import_agreement(pairs: pd.DataFrame, units: pd.DataFrame) -> pd.Series:
    """Per pair: do the two units' imported entity ids agree, disagree, or say nothing?

    A unit with more than one old id has no single id, so it says nothing — the
    same rule the overlay applies when it decides a pair.
    """
    if not len(pairs):
        return pd.Series(dtype="object")
    lookup = units.set_index(units["unit_id"].astype(str))["existing_entity_id"]
    left = pairs["unit_id_l"].astype(str).map(lookup)
    right = pairs["unit_id_r"].astype(str).map(lookup)
    both = left.notna() & right.notna()
    return pd.Series(
        np.where(both & (left == right), "agrees",
                 np.where(both, "disagrees", "unknown")),
        index=pairs.index,
    )


def _as_groups(records: pd.DataFrame, members: pd.DataFrame,
               component: pd.Series) -> pd.DataFrame:
    """Records labelled by their component, shaped like a groups frame.

    ``keys_eval`` reads ``status == 'merged'``, so every record joins in; a
    component of one contributes no pairs to either score, which is exactly what
    an unmerged record should contribute.
    """
    frame = members[["record_id", "unit_id"]].copy()
    frame["record_id"] = frame["record_id"].astype(str)
    frame["group_id"] = "C" + frame["unit_id"].astype(str).map(component).astype(str)
    tracks = records.set_index(records["record_id"].astype(str))["track"] \
        if "track" in records.columns else None
    frame["track"] = frame["record_id"].map(tracks) if tracks is not None else None
    frame["status"] = keys.MERGED
    return frame[["record_id", "group_id", "track", "status"]]


def _histogram(scores: pd.Series, agreement: pd.Series) -> dict:
    """A 50-bin score histogram split by what the imported labels say."""
    edges = np.linspace(0.0, 1.0, HISTOGRAM_BINS + 1)
    values = pd.to_numeric(scores, errors="coerce")
    result = {"bins": HISTOGRAM_BINS, "edges": [round(float(e), 4) for e in edges]}
    for name in AGREEMENTS:
        selected = values[(agreement == name).to_numpy()].dropna()
        counts, _ = np.histogram(selected.to_numpy(dtype="float64"), bins=edges)
        result[name] = [int(c) for c in counts]
    return result


def _bucket_counts(frame: pd.DataFrame, column: str) -> dict:
    counts = frame[column].value_counts() if len(frame) else pd.Series(dtype=int)
    return {name: int(counts.get(name, 0)) for name in ("accept", "review", "reject")}


def _review_block(pairs: pd.DataFrame, agreement: pd.Series) -> dict:
    """The review queue, and what the imported labels say about it.

    Counted on the SCORE bucket, before the import overlay moves agreeing pairs
    to accept — otherwise "how many review pairs do the labels agree with" is
    zero by construction and tells the owner nothing.
    """
    if not len(pairs):
        return {"pairs": 0, "score_bucket_pairs": 0,
                **{f"import_{name}": 0 for name in AGREEMENTS}}
    in_review = (pairs["score_bucket"] == "review").to_numpy()
    block = {
        "pairs": int((pairs["bucket"] == "review").sum()),
        "score_bucket_pairs": int(in_review.sum()),
    }
    for name in AGREEMENTS:
        block[f"import_{name}"] = int((in_review & (agreement == name).to_numpy()).sum())
    return block


def _figures(records, members, units, accepted) -> tuple[dict, int]:
    """``(what keys_eval makes of these accepted pairs, entities left)``."""
    component = components(units, accepted)
    entities = int(component.nunique()) if len(component) else 0
    return keys_eval.evaluate(records, _as_groups(records, members, component)), entities


def _human_figures(records, members, units, pairs, applied) -> dict:
    """The ``with_human`` set: the run with the reviewers' decisions on top.

    A TRUE label joins its two units even when the scorer never offered the
    pair, and a FALSE label pulls them apart even when the score or the import
    overlay had accepted it. With no labels this is the top-level set exactly,
    which is what makes the two comparable while a review is under way.
    """
    overlaid = label_overlay.apply_to_pairs(pairs, applied) if applied is not None \
        else pairs
    accepted = overlaid[overlaid["bucket"] == "accept"] if len(overlaid) else overlaid

    verdicts = label_overlay.decisions(applied) if applied is not None else None
    n_true = n_false = 0
    if verdicts is not None and len(verdicts):
        is_true = verdicts["is_match"].astype(str).str.upper() == "TRUE"
        n_true, n_false = int(is_true.sum()), int((~is_true).sum())
        # A TRUE label on a pair the file does not hold still joins the units.
        extra = verdicts[is_true.to_numpy()][["unit_id_l", "unit_id_r"]]
        accepted = pd.concat(
            [accepted[["unit_id_l", "unit_id_r"]], extra], ignore_index=True
        ).drop_duplicates()

    figures, entities = _figures(records, members, units, accepted)
    return {
        "entities_after": entities,
        "pair_precision": figures["pair_precision"],
        "pair_recall": figures["pair_recall"],
        "labels_applied": n_true + n_false,
        "labels_true": n_true,
        "labels_false": n_false,
        "by_bucket": _bucket_counts(overlaid, "bucket") if len(overlaid) else
                     {"accept": 0, "review": 0, "reject": 0},
        "by_track": figures["by_track"],
    }


def evaluate(
    records: pd.DataFrame,
    groups: pd.DataFrame,
    units: pd.DataFrame,
    members: pd.DataFrame,
    pairs: pd.DataFrame,
    thresholds: dict | None = None,
    applied: pd.DataFrame | None = None,
) -> dict:
    """What the exact groups plus the accepted pairs do to the existing labels.

    *applied* is the active human labels mapped onto this run's unit pairs
    (``label_overlay.outcomes``). It adds the ``with_human`` figure set and
    changes nothing else, so the sets beside it stay comparable.
    """
    accepted = pairs[pairs["bucket"] == "accept"] if len(pairs) else pairs
    combined, entities_after = _figures(records, members, units, accepted)
    exact_only = keys_eval.evaluate(records, groups)

    # The import overlay accepts a pair because the two units already carry the
    # same old entity id, so those accepts cannot be evidence that the scorer
    # found anything. The score-only figures leave them out, and they are the
    # ones to tune blocking rules and comparisons against.
    score_accepted = pairs[pairs["score_bucket"] == "accept"] if len(pairs) else pairs
    score_only, score_only_entities = _figures(records, members, units, score_accepted)

    with_human = _human_figures(records, members, units, pairs, applied)

    agreement = import_agreement(pairs, units)
    tracks = sorted(pairs["track"].dropna().unique()) if len(pairs) else []

    by_track = {}
    for track in tracks:
        mask = (pairs["track"] == track).to_numpy()
        subset = pairs[mask]
        by_track[str(track)] = {
            "pairs": int(len(subset)),
            "by_bucket": _bucket_counts(subset, "bucket"),
            "by_score_bucket": _bucket_counts(subset, "score_bucket"),
            "decided_by_import": int((subset["decided_by"] == "import").sum()),
            "import_disagrees": int(subset["import_disagrees"].fillna(False).sum()),
            "review": _review_block(subset, agreement[mask]),
            "histogram": _histogram(subset["match_probability"], agreement[mask]),
            **{k: v for k, v in (combined["by_track"].get(str(track)) or {}).items()},
            "score_only": score_only["by_track"].get(str(track)),
            "with_human": with_human["by_track"].get(str(track)),
            "exact_only": exact_only["by_track"].get(str(track)),
            "units": int((units["track"] == track).sum())
            if "track" in units.columns else None,
        }

    return {
        "thresholds": thresholds or {},
        "units_total": int(len(units)),
        "pairs_total": int(len(pairs)),
        "by_bucket": _bucket_counts(pairs, "bucket") if len(pairs) else
                     {"accept": 0, "review": 0, "reject": 0},
        "by_score_bucket": _bucket_counts(pairs, "score_bucket") if len(pairs) else
                           {"accept": 0, "review": 0, "reject": 0},
        "decided_by_import": int((pairs["decided_by"] == "import").sum()) if len(pairs) else 0,
        "import_disagrees": int(pairs["import_disagrees"].fillna(False).sum()) if len(pairs) else 0,
        "entities_after": entities_after,
        "pair_precision": combined["pair_precision"],
        "pair_recall": combined["pair_recall"],
        "labelled_pairs": combined["labelled_pairs"],
        "labelled_pairs_agreeing": combined["labelled_pairs_agreeing"],
        "manual_pairs": combined["manual_pairs"],
        "manual_pairs_found": combined["manual_pairs_found"],
        "conflicts": combined["conflicts"],
        "review": _review_block(pairs, agreement),
        "histogram": _histogram(pairs["match_probability"], agreement) if len(pairs)
                     else _histogram(pd.Series(dtype=float), pd.Series(dtype=object)),
        "by_track": by_track,
        "score_only": {
            "entities_after": score_only_entities,
            "pair_precision": score_only["pair_precision"],
            "pair_recall": score_only["pair_recall"],
            "by_track": score_only["by_track"],
        },
        "with_human": with_human,
        "exact_only": {
            "entities_after": int(len(units)),
            "pair_precision": exact_only["pair_precision"],
            "pair_recall": exact_only["pair_recall"],
            "by_track": exact_only["by_track"],
        },
    }

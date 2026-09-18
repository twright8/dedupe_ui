# backend/app/pipeline/dedupe/stage_4_cluster.py
"""Stage 4: group the accepted pairs into clusters, and gate the doubtful ones.

A cluster is a connected component over the pairs that ended up in the
``accept`` bucket once the score, the import overlay and the human labels have
all had their say. That is the easy half. The half that matters is the **gate**
(`docs/ENTITIES.md`): a cluster that looks like a chain, or is enormous, or
would merge groups the earlier manual work kept apart, is not proposed as one
entity. It is **withheld** — rebuilt from the trusted edges alone, which are the
import and human ones — and sent to a review queue.

Nothing here walks a pair of records. The components come from SciPy, and the
gate's tests are groupbys over ``pairs.parquet``.

Output ``clusters.parquet``: one row per unit.
"""

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.pipeline.dedupe import label_overlay
from app.pipeline.dedupe import units as units_module
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME
from app.pipeline.dedupe.stage_2_exact import EXACT_GROUPS_FILENAME
from app.pipeline.dedupe.stage_3_score import PAIRS_FILENAME
from app.rules import keys, linkage

CLUSTERS_FILENAME = "clusters.parquet"

STAGE = 4
STAGE_NAME = "cluster"

# The five statuses, in the order that decides which one is the main one.
STATUS_ORDER = ("conflict", "too_large", "weak_link", "mixed_ids", "cross_track_ids")
OK = "ok"
CROSS_TRACK = "cross_track_ids"
HELD_KEY = "held_key"
ATTRIBUTE_TIE = "attribute_tie"

TRUSTED_SOURCES = ("human", "import")


def _step(label, progress_callback=None):
    message = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(message, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def gate_settings(settings: dict) -> dict:
    """The three gate limits, with the defaults `docs/ENTITIES.md` names."""
    return {
        "cluster_floor": float(settings.get("cluster_floor", linkage.DEFAULT_CLUSTER_FLOOR)),
        "max_cluster_units": int(
            settings.get("max_cluster_units", linkage.DEFAULT_MAX_CLUSTER_UNITS)
        ),
        "max_existing_ids": int(
            settings.get("max_existing_ids", linkage.DEFAULT_MAX_EXISTING_IDS)
        ),
    }


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def accepted_edges(pairs: pd.DataFrame, applied: pd.DataFrame | None) -> pd.DataFrame:
    """``unit_id_l``, ``unit_id_r``, ``source`` for every pair that joins two units.

    The human overlay is applied here rather than read off the file, because
    ``pairs.parquet`` holds the score and the import overlay only (slice 3b).
    """
    if not len(pairs):
        return pd.DataFrame(columns=["unit_id_l", "unit_id_r", "source"])
    overlaid = label_overlay.apply_to_pairs(pairs, applied) if applied is not None \
        else pairs
    accepted = overlaid[overlaid["bucket"] == "accept"]
    edges = pd.DataFrame({
        "unit_id_l": accepted["unit_id_l"].astype(str).to_numpy(),
        "unit_id_r": accepted["unit_id_r"].astype(str).to_numpy(),
        "source": accepted["decided_by"].astype(str).to_numpy(),
    })
    if applied is None or not len(applied):
        return edges

    # A whole-cluster decision writes a star, and the scorer may never have made
    # some of those pairs. The decision is still an accept, so the edge exists
    # whatever pairs.parquet holds — otherwise a merge would not take effect
    # until the next full run.
    verdicts = label_overlay.decisions(applied)
    true_pairs = verdicts[verdicts["is_match"].astype(str).str.upper() == "TRUE"]
    if not len(true_pairs):
        return edges
    have = set(zip(edges["unit_id_l"], edges["unit_id_r"])) if len(edges) else set()
    extra = true_pairs[[
        (left, right) not in have
        for left, right in zip(true_pairs["unit_id_l"].astype(str),
                               true_pairs["unit_id_r"].astype(str))
    ]]
    if not len(extra):
        return edges
    return pd.concat([edges, pd.DataFrame({
        "unit_id_l": extra["unit_id_l"].astype(str).to_numpy(),
        "unit_id_r": extra["unit_id_r"].astype(str).to_numpy(),
        "source": "human",
    })], ignore_index=True)


def import_edges(units: pd.DataFrame) -> pd.DataFrame:
    """Join the units an earlier review already gave one entity id (D11).

    A real group in ``DonorIDStandardTR`` is a trusted merge — a human decision,
    only an older one — so it joins its units whether or not blocking happened
    to produce the pair. Without this a run "keeps apart" thousands of records
    the earlier work had settled, and then mints new ids for them.

    A unit whose members carry **two or more** distinct earlier ids says nothing
    here: it is ambiguous, and its cluster is flagged ``mixed_ids`` for a human.

    A star from the smallest unit id, never a clique: some of these groups hold
    over a thousand units, and n-1 edges connect them just as well as n(n-1)/2.
    """
    label = units_module.LABEL_COLUMN
    if label not in units.columns:
        return pd.DataFrame(columns=["unit_id_l", "unit_id_r", "source"])
    frame = pd.DataFrame({
        "unit_id": units["unit_id"].astype(str).to_numpy(),
        "label": units[label].to_numpy(),
        "track": units["track"].to_numpy() if "track" in units.columns else "",
    }).dropna(subset=["label"])
    frame = frame[frame["label"].astype(str).str.strip() != ""]
    if not len(frame):
        return pd.DataFrame(columns=["unit_id_l", "unit_id_r", "source"])

    # Within a track. The tool never merges a person into an organisation (D5),
    # and an entity id that spanned both would be ambiguous in the registry,
    # which keys on the id alone. An earlier group that crosses the two stays
    # split, and the two halves collide on the id, which is counted and shown.
    frame = frame.sort_values(["track", "label", "unit_id"], kind="mergesort")
    head = frame.groupby(["track", "label"], sort=False)["unit_id"].transform("first")
    star = frame[head.to_numpy() != frame["unit_id"].to_numpy()]
    if not len(star):
        return pd.DataFrame(columns=["unit_id_l", "unit_id_r", "source"])
    left = head[star.index].to_numpy()
    right = star["unit_id"].to_numpy()
    return pd.DataFrame({
        "unit_id_l": np.where(left <= right, left, right),
        "unit_id_r": np.where(left <= right, right, left),
        "source": "import",
    })


def drop_human_false(edges: pd.DataFrame, applied: pd.DataFrame | None) -> pd.DataFrame:
    """Take out every edge a human has said is not a match.

    A new decision from the UI beats an old imported one, so this runs after the
    import edges are added and before anything is clustered.
    """
    if applied is None or not len(applied) or not len(edges):
        return edges
    verdicts = label_overlay.decisions(applied)
    refused = {
        (str(left), str(right)) for left, right, verdict
        in zip(verdicts["unit_id_l"], verdicts["unit_id_r"], verdicts["is_match"])
        if str(verdict).upper() == "FALSE"
    }
    if not refused:
        return edges
    keep = [
        (left, right) not in refused
        for left, right in zip(edges["unit_id_l"], edges["unit_id_r"])
    ]
    return edges[keep].reset_index(drop=True)


def components(unit_ids: np.ndarray, edges: pd.DataFrame) -> pd.Series:
    """A component label per unit. SciPy does the walking, not Python."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    position = pd.Series(np.arange(len(unit_ids)), index=unit_ids)
    n = len(unit_ids)
    if n == 0:
        return pd.Series(dtype="int64")
    if len(edges):
        left = edges["unit_id_l"].map(position)
        right = edges["unit_id_r"].map(position)
        keep = left.notna() & right.notna()
        rows = left[keep].to_numpy(dtype="int64")
        cols = right[keep].to_numpy(dtype="int64")
    else:
        rows = cols = np.empty(0, dtype="int64")
    graph = coo_matrix((np.ones(len(rows), dtype="int8"), (rows, cols)), shape=(n, n))
    _count, labels = connected_components(graph, directed=False)
    return pd.Series(labels, index=unit_ids, name="component")


def _smallest_per_group(frame: pd.DataFrame, group: str, value: str) -> pd.Series:
    """The smallest *value* in each group, compared as text — the id convention."""
    return frame.groupby(group, sort=False)[value].min()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def gate(
    units: pd.DataFrame,
    members: pd.DataFrame,
    pairs: pd.DataFrame,
    cluster_of_unit: pd.Series,
    labels_by_unit: pd.DataFrame,
    limits: dict,
    merged_scopes: set | None = None,
) -> pd.DataFrame:
    """One row per cluster: ``n_units``, ``n_records``, ``n_existing_ids``, statuses.

    Every test is a groupby. ``weak_link`` reads the pairs whose two units are
    both inside one cluster, which is what makes "the cluster may be a chain"
    measurable without enumerating pairs that were never scored.
    """
    frame = pd.DataFrame({
        "unit_id": cluster_of_unit.index.to_numpy(),
        "cluster_id": cluster_of_unit.to_numpy(),
    })
    sizes = units.set_index(units["unit_id"].astype(str))["unit_size"]
    frame["unit_size"] = frame["unit_id"].map(sizes).fillna(1).astype("int64")
    tracks = units.set_index(units["unit_id"].astype(str))["track"] \
        if "track" in units.columns else None
    frame["track"] = frame["unit_id"].map(tracks) if tracks is not None else None

    summary = frame.groupby("cluster_id", sort=True).agg(
        n_units=("unit_id", "size"),
        n_records=("unit_size", "sum"),
        track=("track", "first"),
    )

    # mixed_ids — distinct earlier entity ids among the cluster's records.
    label_column = units_module.LABEL_COLUMN
    if label_column in members.columns:
        joined = members[["unit_id", label_column]].copy()
        joined["cluster_id"] = joined["unit_id"].astype(str).map(cluster_of_unit)
        distinct = (
            joined.dropna(subset=[label_column])
            .groupby("cluster_id", sort=False)[label_column].nunique()
        )
    else:
        distinct = pd.Series(dtype="int64")
    summary["n_existing_ids"] = summary.index.map(distinct).fillna(0).astype("int64")

    inside = _inside_pairs(pairs, cluster_of_unit)
    # weak_link — a scored pair inside the cluster below the floor. A pair a
    # human has already looked at is not weak evidence, whatever it scored.
    weak = pd.Series(dtype=bool)
    if len(inside):
        scored = inside[inside["decided_by"] != "human"]
        scored = scored[scored["match_probability"].notna()]
        if len(scored):
            weak = (
                scored.groupby("cluster_id", sort=False)["match_probability"].min()
                < limits["cluster_floor"]
            )
    summary["weak_link"] = summary.index.map(weak).fillna(False).astype(bool)

    # conflict — a human FALSE label joining two units of one cluster.
    conflict = pd.Series(dtype=bool)
    if len(labels_by_unit):
        false_labels = labels_by_unit[
            labels_by_unit["is_match"].astype(str).str.upper() == "FALSE"
        ].copy()
        if len(false_labels):
            left = false_labels["unit_id_l"].astype(str).map(cluster_of_unit)
            right = false_labels["unit_id_r"].astype(str).map(cluster_of_unit)
            same = left.notna() & (left == right)
            conflict = pd.Series(True, index=left[same].unique())
    summary["conflict"] = summary.index.map(conflict).fillna(False).astype(bool)

    # cross_track_ids — this cluster carries an earlier id that also belongs to a
    # cluster of the other track, so one manual group spans a person and an
    # organisation (a man and his own company, say). The tool never suggests a
    # merge across tracks (D5), so both halves stand; this makes them findable.
    summary[CROSS_TRACK] = _cross_track(members, cluster_of_unit, summary)

    summary["too_large"] = summary["n_units"] > limits["max_cluster_units"]
    summary["mixed_ids"] = summary["n_existing_ids"] > limits["max_existing_ids"]
    # A cluster of one unit is nothing to gate: there is no merge to doubt.
    alone = summary["n_units"] < 2
    for status in STATUS_ORDER:
        summary.loc[alone, status] = False

    # A human who has merged the whole cluster has answered every question the
    # gate asks. A decision always wins, so it is not withheld again — the same
    # rule weak_link already follows for a pair a human has decided.
    summary["decided"] = summary.index.isin(merged_scopes or set())
    for status in ("too_large", "weak_link", "mixed_ids"):
        summary.loc[summary["decided"], status] = False

    statuses = [
        [status for status in STATUS_ORDER if row[status]]
        for _, row in summary[list(STATUS_ORDER)].iterrows()
    ] if len(summary) else []
    summary["statuses"] = ["|".join(s) for s in statuses] if len(summary) else []
    summary["status"] = [s[0] if s else OK for s in statuses] if len(summary) else []
    summary["withheld"] = summary["status"] != OK
    return summary.reset_index()


def _cross_track(members: pd.DataFrame, cluster_of_unit: pd.Series,
                 summary: pd.DataFrame) -> pd.Series:
    """Clusters holding an earlier id that the other track also claims."""
    label = units_module.LABEL_COLUMN
    if label not in members.columns or "track" not in members.columns:
        return pd.Series(False, index=summary.index)
    frame = members[["unit_id", label, "track"]].dropna(subset=[label]).copy()
    if not len(frame):
        return pd.Series(False, index=summary.index)
    frame["cluster_id"] = frame["unit_id"].astype(str).map(cluster_of_unit)
    tracks_per_id = frame.groupby(label, sort=False)["track"].nunique()
    shared = set(tracks_per_id.index[tracks_per_id > 1])
    if not shared:
        return pd.Series(False, index=summary.index)
    flagged = set(frame.loc[frame[label].isin(shared), "cluster_id"].dropna())
    return pd.Series(summary.index.isin(flagged), index=summary.index)


def _inside_pairs(pairs: pd.DataFrame, cluster_of_unit: pd.Series) -> pd.DataFrame:
    """The pairs whose two units landed in one cluster, with that cluster's id."""
    if not len(pairs):
        return pd.DataFrame(columns=["cluster_id", "match_probability", "decided_by"])
    left = pairs["unit_id_l"].astype(str).map(cluster_of_unit)
    right = pairs["unit_id_r"].astype(str).map(cluster_of_unit)
    same = (left.notna() & (left == right)).to_numpy()
    inside = pairs[same].copy()
    inside["cluster_id"] = left[same].to_numpy()
    return inside


# ---------------------------------------------------------------------------
# Withholding
# ---------------------------------------------------------------------------


def proposed_parts(
    cluster_of_unit: pd.Series,
    withheld_clusters: set,
    edges: pd.DataFrame,
) -> pd.Series:
    """``{unit_id: proposed_entity_key}``.

    A cluster that passed the gate is proposed whole. A withheld one is rebuilt
    from the trusted edges alone — the import and human ones — so a human
    decision is never withheld, and each part it leaves becomes an entity.
    """
    unit_ids = cluster_of_unit.index.to_numpy()
    if not withheld_clusters:
        return pd.Series(cluster_of_unit.to_numpy(), index=unit_ids).map(
            lambda value: str(value)[2:] if str(value).startswith("C-") else str(value)
        )

    in_withheld = pd.Series(cluster_of_unit.isin(withheld_clusters).to_numpy(),
                            index=unit_ids)
    trusted = edges[edges["source"].isin(TRUSTED_SOURCES)] if len(edges) else edges
    if len(trusted):
        keep = (
            trusted["unit_id_l"].map(in_withheld).fillna(False)
            & trusted["unit_id_r"].map(in_withheld).fillna(False)
        ).to_numpy()
        trusted = trusted[keep]

    # Units outside a withheld cluster keep their cluster; units inside are
    # re-clustered on the trusted edges only.
    rebuilt = components(unit_ids, trusted)
    part_frame = pd.DataFrame({"unit_id": unit_ids, "part": rebuilt.to_numpy()})
    smallest = _smallest_per_group(part_frame, "part", "unit_id")
    rebuilt_key = pd.Series(part_frame["part"].map(smallest).to_numpy(), index=unit_ids)

    whole_key = pd.Series(
        [str(value)[2:] if str(value).startswith("C-") else str(value)
         for value in cluster_of_unit.to_numpy()],
        index=unit_ids,
    )
    return pd.Series(
        np.where(in_withheld.to_numpy(), rebuilt_key.to_numpy(), whole_key.to_numpy()),
        index=unit_ids,
    )


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def build_clusters(
    units: pd.DataFrame,
    members: pd.DataFrame,
    pairs: pd.DataFrame,
    settings: dict,
    applied: pd.DataFrame | None = None,
    decisions: dict | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(clusters, cluster_summary)`` — one row per unit, and one per cluster."""
    limits = gate_settings(settings)
    unit_ids = units["unit_id"].astype(str).to_numpy()
    # The score and the human overlay, then the trusted merges an earlier round
    # already made, then anything a human has since refused.
    edges = pd.concat([accepted_edges(pairs, applied), import_edges(units)],
                      ignore_index=True)
    edges = edges.drop_duplicates(subset=["unit_id_l", "unit_id_r"], keep="first")
    edges = drop_human_false(edges, applied)

    component = components(unit_ids, edges)
    frame = pd.DataFrame({"unit_id": unit_ids, "component": component.to_numpy()})
    smallest = _smallest_per_group(frame, "component", "unit_id")
    cluster_of_unit = pd.Series(
        ("C-" + frame["component"].map(smallest)).to_numpy(), index=unit_ids
    )

    wanted = ["unit_id"]
    for column in (units_module.LABEL_COLUMN, "track"):
        if column in units.columns:
            wanted.append(column)
    member_ids = members.merge(units[wanted], on="unit_id", how="left")
    labels_by_unit = applied if applied is not None else pd.DataFrame(
        columns=["unit_id_l", "unit_id_r", "is_match"]
    )
    merged_scopes = {
        scope for scope, decision in (decisions or {}).items()
        if decision.get("kind") == "merge"
    }
    summary = gate(units, member_ids, pairs, cluster_of_unit, labels_by_unit, limits,
                   merged_scopes)

    withheld = set(summary.loc[summary["withheld"], "cluster_id"])
    parts = proposed_parts(cluster_of_unit, withheld, edges)

    by_cluster = summary.set_index("cluster_id")
    clusters = pd.DataFrame({
        "unit_id": unit_ids,
        "cluster_id": cluster_of_unit.to_numpy(),
        "track": units["track"].to_numpy() if "track" in units.columns else None,
        "proposed_entity_key": parts.to_numpy(),
    })
    clusters["status"] = clusters["cluster_id"].map(by_cluster["status"])
    clusters["statuses"] = clusters["cluster_id"].map(by_cluster["statuses"])
    clusters["withheld"] = clusters["cluster_id"].map(by_cluster["withheld"]).astype(bool)
    # The strongest edge inside each proposed part, for entity_basis in stage 5.
    clusters["edge_source"] = clusters["proposed_entity_key"].map(
        _strongest_source(parts, edges)
    )
    return clusters.sort_values("unit_id", kind="mergesort").reset_index(drop=True), summary


BASIS_ORDER = {"single": 0, "exact_key": 1, "import": 2, "score": 3, "human": 4}


def _strongest_source(parts: pd.Series, edges: pd.DataFrame) -> pd.Series:
    """The strongest accepted edge inside each proposed part."""
    if not len(edges):
        return pd.Series(dtype="object")
    left = edges["unit_id_l"].map(parts)
    right = edges["unit_id_r"].map(parts)
    inside = edges[(left.notna() & (left == right)).to_numpy()].copy()
    if not len(inside):
        return pd.Series(dtype="object")
    inside["part"] = left[left.notna() & (left == right)].to_numpy()
    inside["rank"] = inside["source"].map(BASIS_ORDER).fillna(0)
    best = inside.sort_values("rank", kind="mergesort").groupby("part").last()
    return best["source"]


def held_groups(groups: pd.DataFrame) -> pd.DataFrame:
    """The held exact groups, which wait for a human whatever the scorer said."""
    if not len(groups):
        return pd.DataFrame(columns=["group_id", "track", "n_records"])
    held = groups[groups["status"] == keys.HELD]
    if not len(held):
        return pd.DataFrame(columns=["group_id", "track", "n_records"])
    return held.groupby("group_id", sort=True).agg(
        track=("track", "first"), n_records=("record_id", "size")
    ).reset_index()


def counts_from(clusters: pd.DataFrame, summary: pd.DataFrame,
                held: pd.DataFrame, decisions: dict | None = None) -> dict:
    """The run counts stage 4 contributes, in the pipeline's snake_case.

    The queue counts what is still **open**. A cluster or held group a human has
    decided is done with, and leaving it in the total would mean the number
    never fell however much work was done.
    """
    decisions = decisions or {}
    by_status = summary["status"].value_counts() if len(summary) else pd.Series(dtype=int)
    statuses = {name: int(by_status.get(name, 0)) for name in (OK, *STATUS_ORDER)}
    withheld = int(summary["withheld"].sum()) if len(summary) else 0
    open_withheld = int((summary["withheld"]
                         & ~summary["cluster_id"].isin(decisions)).sum()) \
        if len(summary) else 0
    open_held = int((~held["group_id"].isin(decisions)).sum()) if len(held) else 0
    return {
        "clusters_total": int(len(summary)),
        "clusters_withheld": withheld,
        "clusters_by_status": statuses,
        "held_groups_open": open_held,
        "review_queue": open_withheld + open_held,
        "decisions_total": len(decisions),
        "cross_track_ids": int(statuses.get(CROSS_TRACK, 0)),
    }


def run_stage_4_cluster(
    run_dir: str,
    config_dir: str,
    labels: pd.DataFrame | None = None,
    decisions: dict | None = None,
    progress_callback=None,
) -> dict:
    """Cluster the units of ``<run_dir>`` and write ``clusters.parquet``."""
    t_start = time.time()
    run_dir = Path(run_dir)
    config_dir = Path(config_dir)

    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    settings = json.loads(
        (config_dir / "linkage_settings.json").read_text(encoding="utf-8")
    )
    units = pd.read_parquet(run_dir / units_module.UNITS_FILENAME)
    members = pd.read_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME)
    pairs = pd.read_parquet(run_dir / PAIRS_FILENAME)
    groups = pd.read_parquet(run_dir / EXACT_GROUPS_FILENAME)

    applied = None
    if labels is not None and len(labels):
        applied = label_overlay.outcomes(labels, members, groups)["applied"]

    _step(f"Clustering {len(units):,} units...", progress_callback)
    clusters, summary = build_clusters(units, members, pairs, settings, applied,
                                       decisions)
    clusters.to_parquet(run_dir / CLUSTERS_FILENAME, index=False)

    held = held_groups(groups)
    counts = counts_from(clusters, summary, held, decisions)
    elapsed = time.time() - t_start
    _step(
        f"Stage 4 complete in {elapsed:.1f}s — {counts['clusters_total']:,} clusters, "
        f"{counts['clusters_withheld']:,} withheld, "
        f"{counts['held_groups_open']:,} held group(s) waiting.",
        progress_callback,
    )
    if progress_callback:
        progress_callback("stage_end", {
            "stage": STAGE, "name": STAGE_NAME,
            "elapsed_seconds": round(elapsed, 1), **counts,
        })
    return counts

# backend/app/pipeline/dedupe/stage_5_entities.py
"""Stage 5: turn the proposed clusters into entity IDs, and settle the attributes.

A run never writes the registry. It writes a **proposal**, ``entities.parquet``,
one row per record: which entity it belongs to, how it got there, and what the
entity's consensus columns came out as. Publishing the run is what moves the
registry (`docs/ENTITIES.md`).

Two things here are worth being careful about.

**ID resolution.** In order: records already in exactly one live registry entity
keep that ID; records spread over several give the survivor the profile picks
and the rest become aliases; records in none get a minted ID. The registry is
read as it is *now*, so the answer does not depend on the order runs happened in.

**Uniqueness.** Two proposed entities must never claim one ID. They can: a
published entity that this run splits leaves two parts both entitled to it, and
a profile that mints from the earlier manual ID will mint the same one twice
when the run keeps apart what that manual work merged. Both are resolved here,
by different rules, and both are counted and listed.
"""

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.pipeline.dedupe import units as units_module
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME
from app.pipeline.dedupe.stage_4_cluster import BASIS_ORDER, CLUSTERS_FILENAME
from app.profiles import get_profile

ENTITIES_FILENAME = "entities.parquet"
ENTITY_REPORT_FILENAME = "entity_report.json"

STAGE = 5
STAGE_NAME = "entities"

BASES = ("single", "exact_key", "import", "score", "human")
ATTRIBUTE_BASES = ("human", "rule", "majority", "raw", "tie")

# How a proposed entity came by its ID.
ID_STATUSES = ("new", "kept", "survivor", "minted_after_collision")

MAX_EXAMPLES = 20


def _step(label, progress_callback=None):
    message = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(message, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


# ---------------------------------------------------------------------------
# Proposed entities: which records, and how they got there
# ---------------------------------------------------------------------------


def proposed_members(clusters: pd.DataFrame, members: pd.DataFrame) -> pd.DataFrame:
    """``record_id``, ``unit_id``, ``entity_key``, ``cluster_id``, ``track``, ``basis``."""
    unit_index = clusters.set_index(clusters["unit_id"].astype(str))
    frame = members[["unit_id", "record_id"]].copy()
    frame["unit_id"] = frame["unit_id"].astype(str)
    frame["record_id"] = frame["record_id"].astype(str)
    frame["entity_key"] = frame["unit_id"].map(unit_index["proposed_entity_key"])
    # Every record must reach an entity. A unit the cluster file does not name —
    # which should not happen, but a missing row must not cost a record its ID —
    # stands as an entity of its own.
    frame["entity_key"] = frame["entity_key"].where(
        frame["entity_key"].notna(), frame["unit_id"]
    )
    frame["cluster_id"] = frame["unit_id"].map(unit_index["cluster_id"])
    frame["track"] = frame["unit_id"].map(unit_index["track"])

    unit_sizes = frame.groupby("unit_id", sort=False)["record_id"].transform("size")
    units_per_entity = (
        frame.groupby("entity_key", sort=False)["unit_id"].transform("nunique")
    )
    edge_source = frame["unit_id"].map(unit_index["edge_source"]) \
        if "edge_source" in unit_index.columns else pd.Series(index=frame.index, dtype=object)

    # A record alone in its unit and its unit alone in its entity was never
    # merged with anything. Inside a merged exact group it is at least
    # exact_key. Joined to another unit, the strongest edge on its path wins.
    basis = np.where(unit_sizes > 1, "exact_key", "single")
    joined = (units_per_entity > 1).to_numpy()
    edge = edge_source.fillna("score").to_numpy()
    stronger = np.array([
        BASIS_ORDER.get(str(e), 0) > BASIS_ORDER.get(str(b), 0)
        for e, b in zip(edge, basis)
    ])
    frame["basis"] = np.where(joined & stronger, edge, basis)
    return frame


# ---------------------------------------------------------------------------
# ID resolution
# ---------------------------------------------------------------------------


def _registry_entity_of(record_ids: pd.Series, registry_members: dict) -> pd.Series:
    return record_ids.map(registry_members)


def resolve_ids(
    proposed: pd.DataFrame,
    records: pd.DataFrame,
    registry_members: dict,
    profile,
) -> tuple[pd.DataFrame, list[dict]]:
    """``(one row per proposed entity, the collisions that had to be broken)``.

    Each row carries ``entity_key``, ``entity_id``, ``id_status``, ``absorbs``
    (the registry entities it takes over) and ``smallest_record``.

    Everything here is a group-by or a merge. There are 19,000 entities in a
    donations run and there will be millions in a PSC one, so nothing may look
    at an entity one at a time — including the profile's own hooks, which take
    whole frames.
    """
    summary = proposed.groupby("entity_key", sort=True).agg(
        n_records=("record_id", "size"),
        smallest_record=("record_id", "min"),
        track=("track", "first"),
        cluster_id=("cluster_id", "first"),
    ).reset_index()

    # Which live registry entities each proposal's records already belong to.
    proposed = proposed.copy()
    proposed["registry_entity"] = proposed["record_id"].map(registry_members)
    claimed = proposed.dropna(subset=["registry_entity"])[
        ["entity_key", "registry_entity"]
    ].drop_duplicates()
    n_claims = claimed.groupby("entity_key", sort=False)["registry_entity"].size()
    summary["n_claims"] = summary["entity_key"].map(n_claims).fillna(0).astype("int64")

    # Rule 1: exactly one registry entity, so keep its id.
    single = claimed[claimed["entity_key"].map(n_claims) == 1]
    kept = single.set_index("entity_key")["registry_entity"]

    # Rule 2: several, so the profile picks the survivor and the rest are aliases.
    several = claimed[claimed["entity_key"].map(n_claims) > 1]
    survivors = profile.choose_survivors(several) if len(several) else pd.Series(dtype=object)
    absorbed = pd.Series(dtype=object)
    if len(several):
        joined = several.copy()
        joined["survivor"] = joined["entity_key"].map(survivors)
        losers = joined[joined["registry_entity"].astype(str)
                        != joined["survivor"].astype(str)]
        absorbed = losers.groupby("entity_key", sort=False)["registry_entity"].apply(list)

    # Rule 3: none, so the profile mints one from the members themselves.
    minting = summary.loc[summary["n_claims"] == 0, "entity_key"]
    members = proposed[proposed["entity_key"].isin(set(minting))]
    with_records = members.merge(records, on="record_id", how="left") if len(members) \
        else members
    minted = profile.mint_entity_ids(with_records) if len(with_records) \
        else pd.Series(dtype=object)

    entity_id = summary["entity_key"].map(kept)
    entity_id = entity_id.where(entity_id.notna(), summary["entity_key"].map(survivors))
    entity_id = entity_id.where(entity_id.notna(), summary["entity_key"].map(minted))
    # A proposal whose records give the profile nothing to mint from still needs
    # an id; its own key is the one thing it certainly has.
    summary["entity_id"] = entity_id.where(entity_id.notna(),
                                           summary["entity_key"]).astype(str)
    summary["id_status"] = np.where(
        summary["n_claims"] == 1, "kept",
        np.where(summary["n_claims"] > 1, "survivor", "new"),
    )
    summary["absorbs"] = summary["entity_key"].map(absorbed).map(
        lambda value: value if isinstance(value, list) else []
    )
    summary["from_registry"] = summary["n_claims"] > 0

    summary, collisions = _break_collisions(summary, proposed, records, profile)
    return summary.drop(columns=["n_claims"]), collisions


def _break_collisions(summary, proposed, records, profile):
    """Make sure no two proposed entities claim one ID.

    Three ways it happens, and they are settled differently:

    * a claim the **registry** has already granted always beats one the profile
      has just minted — an id a published entity holds is not available;
    * two registry claims mean this run splits a published entity, and
      `docs/ENTITIES.md` gives it to the part holding the smallest ``record_id``;
    * two minted claims mean the profile minted the same id twice. The larger
      part keeps it, ties going to the smaller record id.

    The set of taken ids starts as **every** id anything claims, not just the
    ones seen so far. Seeding it group by group was the bug behind the entity
    that went missing at publish: a re-mint could take an id that a group
    processed later then also claimed, and the two entities ended up sharing it.
    """
    collisions: list[dict] = []
    duplicated = summary["entity_id"].duplicated(keep=False)
    if not duplicated.any():
        return summary, collisions

    taken = set(summary["entity_id"])
    entity_ids = summary["entity_id"].tolist()
    statuses = summary["id_status"].tolist()

    for value, group in summary[duplicated].groupby("entity_id", sort=True):
        from_registry = bool(group["from_registry"].any())
        if from_registry:
            contenders = group[group["from_registry"]]
            ordered = pd.concat([
                contenders.sort_values("smallest_record", kind="mergesort"),
                group[~group["from_registry"]].sort_values(
                    ["n_records", "smallest_record"], ascending=[False, True],
                    kind="mergesort"),
            ])
        else:
            ordered = group.sort_values(
                ["n_records", "smallest_record"], ascending=[False, True],
                kind="mergesort",
            )
        winner = ordered.index[0]
        for loser in ordered.index[1:]:
            position = summary.index.get_loc(loser)
            minted = _unique_id(str(summary.at[loser, "smallest_record"]), taken)
            entity_ids[position] = minted
            statuses[position] = "minted_after_collision"
            taken.add(minted)
            collisions.append({
                "entity_id": value,
                "kept_by_key": str(summary.at[winner, "entity_key"]),
                "n_records_kept": int(summary.at[winner, "n_records"]),
                "minted": minted,
                "minted_for_key": str(summary.at[loser, "entity_key"]),
                "n_records_minted": int(summary.at[loser, "n_records"]),
                "from_registry": from_registry,
            })

    summary = summary.copy()
    summary["entity_id"] = entity_ids
    summary["id_status"] = statuses
    return summary, collisions


def _unique_id(fallback: str, taken: set) -> str:
    """An id nothing else has taken, built from the entity's smallest record id."""
    if fallback and fallback not in taken:
        return fallback
    suffix = 2
    while f"{fallback}-{suffix}" in taken:
        suffix += 1
    return f"{fallback}-{suffix}"


# ---------------------------------------------------------------------------
# Consensus attributes (D8a, entity level)
# ---------------------------------------------------------------------------


def consensus(proposed: pd.DataFrame, records: pd.DataFrame, column: str,
              overrides: pd.DataFrame | None = None) -> pd.DataFrame:
    """``entity_key``, ``value``, ``basis`` for one consensus column.

    In order: a value a **human** settled beats everything; then one a rule set;
    then the most frequent. A tie is left alone and flagged, because guessing
    between two equally supported statuses is exactly the decision a human
    should make — and once they have, this is where their answer comes back in.
    """
    rule_column = f"{column}_rule"
    if column not in records.columns:
        return pd.DataFrame(columns=["entity_key", "value", "basis"])

    wanted = ["record_id", column] + ([rule_column] if rule_column in records.columns else [])
    joined = proposed[["record_id", "entity_key"]].merge(
        records[wanted].assign(record_id=records["record_id"].astype(str)),
        on="record_id", how="left",
    )
    if rule_column not in joined.columns:
        joined[rule_column] = None
    joined["by_rule"] = joined[rule_column].notna()

    sizes = joined.groupby("entity_key", sort=False)["record_id"].size()
    has_rule = joined.groupby("entity_key", sort=False)["by_rule"].any()

    def _winner(frame: pd.DataFrame) -> pd.DataFrame:
        counted = (
            frame.dropna(subset=[column])
            .groupby(["entity_key", column], sort=False).size().rename("n").reset_index()
        )
        if not len(counted):
            return pd.DataFrame(columns=["entity_key", "value", "n", "tied"])
        counted = counted.sort_values(
            ["entity_key", "n", column], ascending=[True, False, True], kind="mergesort"
        )
        best = counted.drop_duplicates(subset=["entity_key"])
        top = best.set_index("entity_key")["n"]
        counted["is_top"] = counted["n"] == counted["entity_key"].map(top)
        tied = counted.groupby("entity_key", sort=False)["is_top"].sum() > 1
        return best.assign(
            value=best[column], tied=best["entity_key"].map(tied).fillna(False)
        )[["entity_key", "value", "n", "tied"]]

    ruled = _winner(joined[joined["by_rule"]])
    plain = _winner(joined)

    result = plain.set_index("entity_key")
    ruled = ruled.set_index("entity_key")
    entities = sizes.index
    value = pd.Series(index=entities, dtype="object")
    basis = pd.Series(index=entities, dtype="object")

    rule_entities = entities[entities.isin(ruled.index) & has_rule.reindex(entities).fillna(False)]
    value.loc[rule_entities] = ruled.loc[rule_entities, "value"]
    basis.loc[rule_entities] = np.where(
        ruled.loc[rule_entities, "tied"].to_numpy(), "tie", "rule"
    )

    rest = entities.difference(rule_entities)
    known = rest.intersection(result.index)
    value.loc[known] = result.loc[known, "value"]
    single = sizes.reindex(known).fillna(0) <= 1
    basis.loc[known] = np.where(
        result.loc[known, "tied"].to_numpy(), "tie",
        np.where(single.to_numpy(), "raw", "majority"),
    )

    settled = pd.DataFrame({
        "entity_key": entities, "value": value.to_numpy(), "basis": basis.to_numpy(),
    })
    return _apply_overrides(settled, proposed, column, overrides)


def _apply_overrides(settled: pd.DataFrame, proposed: pd.DataFrame, column: str,
                     overrides: pd.DataFrame | None) -> pd.DataFrame:
    """A human's answer, which outranks the rule and the majority alike."""
    if overrides is None or not len(overrides):
        return settled
    mine = overrides[overrides["column_name"] == column]
    if not len(mine):
        return settled
    joined = proposed[["record_id", "entity_key"]].merge(
        mine[["record_id", "value"]], on="record_id", how="inner"
    )
    if not len(joined):
        return settled
    # One override per record, and a reviewer settles a whole entity at once, so
    # the first is the answer; a later rerun that splits them keeps each part's.
    chosen = joined.drop_duplicates(subset=["entity_key"]).set_index("entity_key")["value"]
    settled = settled.set_index("entity_key")
    settled.loc[settled.index.isin(chosen.index), "value"] = chosen
    settled.loc[settled.index.isin(chosen.index), "basis"] = "human"
    return settled.reset_index()


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def build_entities(
    clusters: pd.DataFrame,
    members: pd.DataFrame,
    records: pd.DataFrame,
    registry_members: dict,
    profile=None,
    overrides: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    """``(entities.parquet, the report)`` — the proposal and what it did."""
    profile = profile or get_profile()
    proposed = proposed_members(clusters, members)
    summary, collisions = resolve_ids(proposed, records, registry_members, profile)

    lookup = summary.set_index("entity_key")
    frame = proposed.copy()
    frame["entity_id"] = frame["entity_key"].map(lookup["entity_id"])
    frame["entity_basis"] = frame["basis"]
    # How the ID was come by travels with the proposal, so the Entities tab and
    # the publish preview never have to work it out again.
    frame["id_status"] = frame["entity_key"].map(lookup["id_status"])
    frame = frame[["record_id", "entity_id", "entity_basis", "id_status",
                   "cluster_id", "track", "unit_id", "entity_key"]]

    attributes: dict[str, pd.DataFrame] = {}
    for column in getattr(profile, "consensus_columns", []):
        settled = consensus(proposed, records, column, overrides)
        attributes[column] = settled
        settled_index = settled.set_index("entity_key")
        frame[f"{column}_entity"] = frame["entity_key"].map(settled_index["value"])
        frame[f"{column}_entity_basis"] = frame["entity_key"].map(settled_index["basis"])

    check_invariants(frame)

    ties = sorted({
        str(key)
        for settled in attributes.values()
        for key in settled.loc[settled["basis"] == "tie", "entity_key"]
    })
    report = {
        "entities_proposed": int(len(summary)),
        "by_id_status": {
            status: int((summary["id_status"] == status).sum())
            for status in ID_STATUSES
        },
        "id_collisions": len(collisions),
        "id_collision_examples": collisions[:MAX_EXAMPLES],
        "attribute_ties": len(ties),
        "attribute_tie_keys": ties[:200],
        "entities_absorbing": int(summary["absorbs"].map(len).gt(0).sum()),
        "registry_entities_absorbed": int(summary["absorbs"].map(len).sum()),
    }
    summary_out = summary[[
        "entity_key", "entity_id", "id_status", "n_records", "smallest_record",
        "track", "cluster_id",
    ]].copy()
    summary_out["absorbs"] = summary["absorbs"].map(lambda v: "|".join(str(x) for x in v))
    return frame, {"report": report, "summary": summary_out, "attributes": attributes}


def entity_figures(records: pd.DataFrame, frame: pd.DataFrame) -> dict:
    """What the proposal does to the existing labels, measured like every other set.

    The same counting as ``score_eval``: the entity takes the place of the
    group, so a withheld cluster visibly costs recall rather than quietly
    disappearing.
    """
    from app.rules import keys, keys_eval

    groups = pd.DataFrame({
        "record_id": frame["record_id"].astype(str).to_numpy(),
        "group_id": "E" + frame["entity_id"].astype(str).to_numpy(),
        "track": frame["track"].to_numpy(),
        "status": keys.MERGED,
    })
    figures = keys_eval.evaluate(records, groups)
    return {
        "circular": (
            "The imported labels are both an input and the yardstick here: every "
            "earlier group joins its units as trusted import edges (D11), so this "
            "set and with_human score near 1.0 recall by construction. Tune on "
            "score_only, which leaves the import overlay out."
        ),
        "entities_after": int(frame["entity_id"].nunique()),
        "pair_precision": figures["pair_precision"],
        "pair_recall": figures["pair_recall"],
        "conflicts": figures["conflicts"],
        "by_track": figures["by_track"],
    }


def compare_with_existing(records: pd.DataFrame, frame: pd.DataFrame) -> dict:
    """How the proposed ``entity_id`` lines up with the earlier manual ID.

    Only the records the earlier work actually reviewed can say anything, so
    the shares are over those. Where the two differ, the reason is what the
    owner needs: a merge of two earlier groups, an ID re-minted after a
    collision, or a split.
    """
    label = "existing_entity_id"
    if label not in records.columns:
        return {"labelled_records": 0, "identical": None, "different": None,
                "reasons": {}}
    joined = frame[["record_id", "entity_id", "id_status"]].merge(
        records[["record_id", label]].assign(
            record_id=records["record_id"].astype(str)
        ),
        on="record_id", how="left",
    )
    joined[label] = joined[label].astype("object").where(joined[label].notna(), None)
    labelled = joined[joined[label].map(
        lambda v: v is not None and str(v).strip() != ""
    )]
    if not len(labelled):
        return {"labelled_records": 0, "identical": None, "different": None,
                "reasons": {}}
    same = labelled["entity_id"].astype(str) == labelled[label].astype(str)
    # Why the two differ, counted per record.
    ids_per_entity = labelled.groupby("entity_id")[label].nunique()
    entities_per_id = labelled.groupby(label)["entity_id"].nunique()
    differing = labelled[~same.to_numpy()]
    reasons = {
        "merged_two_earlier_groups": int(
            (differing["entity_id"].map(ids_per_entity) > 1).sum()),
        "collision_re_mint": int(
            (differing["id_status"] == "minted_after_collision").sum()),
        "split": int((differing[label].map(entities_per_id) > 1).sum()),
    }
    total = int(len(labelled))
    return {
        "labelled_records": total,
        "identical": round(float(same.mean()), 6),
        "different": round(float((~same).mean()), 6),
        "identical_records": int(same.sum()),
        "different_records": int((~same).sum()),
        "reasons": reasons,
        "reason_shares": {
            name: round(count / total, 6) if total else None
            for name, count in reasons.items()
        },
        "circular": (
            "The earlier ids are an input to the run as well as the yardstick "
            "here (D11), so a high share identical is agreement, not accuracy."
        ),
    }


class EntityInvariantError(RuntimeError):
    """The proposal breaks a rule the registry depends on. The run stops here."""


def check_invariants(frame: pd.DataFrame) -> None:
    """Every record has exactly one entity, and no entity spans two tracks.

    The registry keys on ``entity_id`` alone, so an id shared by a person and an
    organisation would silently merge them at publish. A record with no id would
    be dropped by the publish group-by and quietly left out. Both are worth
    stopping a run for rather than discovering later.
    """
    missing = frame["entity_id"].isna() | (
        frame["entity_id"].astype(str).str.strip().isin(("", "nan", "None"))
    )
    if missing.any():
        examples = frame.loc[missing, "record_id"].head(5).tolist()
        raise EntityInvariantError(
            f"{int(missing.sum())} record(s) have no entity id, for example {examples}"
        )
    duplicated = frame["record_id"].duplicated()
    if duplicated.any():
        examples = frame.loc[duplicated, "record_id"].head(5).tolist()
        raise EntityInvariantError(
            f"{int(duplicated.sum())} record(s) appear twice, for example {examples}"
        )
    if "track" in frame.columns:
        spans = frame.groupby("entity_id")["track"].nunique(dropna=True)
        crossing = spans[spans > 1]
        if len(crossing):
            raise EntityInvariantError(
                f"{len(crossing)} entity id(s) span two tracks, "
                f"for example {list(crossing.index[:5])}"
            )


def counts_from(frame: pd.DataFrame, extra: dict, registry_members: dict) -> dict:
    """The run counts stage 5 contributes, in the pipeline's snake_case."""
    report = extra["report"]
    return {
        "entities_proposed": report["entities_proposed"],
        "entities_new": report["by_id_status"]["new"],
        "entities_kept": report["by_id_status"]["kept"],
        "entities_merged": report["by_id_status"]["survivor"],
        "id_collisions": report["id_collisions"],
        "attribute_ties": report["attribute_ties"],
        "records_with_entity": int(len(frame)),
    }


def _merge_into_score_eval(run_dir: Path, report: dict) -> None:
    """Put the entity figures beside the other sets, so they can be read together."""
    path = run_dir / "score_eval.json"
    if not path.is_file():
        return
    try:
        evaluation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    evaluation["entities"] = report["entities"]
    evaluation["versus_existing_entity_id"] = report["versus_existing_entity_id"]
    path.write_text(json.dumps(evaluation, indent=2), encoding="utf-8")


def run_stage_5_entities(
    run_dir: str,
    db_path: str | None = None,
    progress_callback=None,
) -> dict:
    """Propose an entity ID for every record and write ``entities.parquet``."""
    from app.registry import store

    t_start = time.time()
    run_dir = Path(run_dir)
    if progress_callback:
        progress_callback("stage_start", {"stage": STAGE, "name": STAGE_NAME})

    clusters = pd.read_parquet(run_dir / CLUSTERS_FILENAME)
    members = pd.read_parquet(run_dir / units_module.UNIT_MEMBERS_FILENAME)
    records = pd.read_parquet(run_dir / RECORDS_FILENAME)
    registry_members = store.current_members(db_path) if db_path else {}
    overrides = None
    if db_path:
        from app.services.attribute_overrides import active_overrides

        overrides = active_overrides(db_path)

    _step(f"Proposing entities for {len(records):,} records...", progress_callback)
    frame, extra = build_entities(clusters, members, records, registry_members,
                                  overrides=overrides)
    frame.to_parquet(run_dir / ENTITIES_FILENAME, index=False)
    report = extra["report"]
    report["entities"] = entity_figures(records, frame)
    report["versus_existing_entity_id"] = compare_with_existing(records, frame)
    if extra["attributes"]:
        report["attribute_basis"] = {
            column: {name: int((settled["basis"] == name).sum())
                     for name in ATTRIBUTE_BASES}
            for column, settled in extra["attributes"].items()
        }
    (run_dir / ENTITY_REPORT_FILENAME).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    _merge_into_score_eval(run_dir, report)

    counts = counts_from(frame, extra, registry_members)
    elapsed = time.time() - t_start
    _step(
        f"Stage 5 complete in {elapsed:.1f}s — {counts['entities_proposed']:,} entities "
        f"({counts['entities_kept']:,} kept, {counts['entities_merged']:,} absorbing "
        f"another, {counts['id_collisions']:,} id collision(s) broken).",
        progress_callback,
    )
    if progress_callback:
        progress_callback("stage_end", {
            "stage": STAGE, "name": STAGE_NAME,
            "elapsed_seconds": round(elapsed, 1), **counts,
        })
    return counts

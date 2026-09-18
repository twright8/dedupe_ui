# backend/app/registry/plan.py
"""Work out what publishing a run would do to the registry.

The preview and the publish read the same plan, so what a user is shown is
exactly what is written. Nothing here touches the database beyond reading it.
"""

import pandas as pd

from app.registry import store

MAX_EXAMPLES = 20


def _entity_frame(entities: pd.DataFrame) -> pd.DataFrame:
    frame = entities.copy()
    frame["record_id"] = frame["record_id"].astype(str)
    frame["entity_id"] = frame["entity_id"].astype(str)
    return frame


def build_plan(
    db_path: str,
    run_id: str,
    entities: pd.DataFrame,
    names: dict | None = None,
    attributes_columns: list[str] | None = None,
) -> dict:
    """``{entities, retire, summary, examples}`` for publishing *entities*.

    ``entities`` is the run's proposal, one row per record. Everything the
    registry needs is derived from it and from the registry as it stands.
    """
    frame = _entity_frame(entities)
    # A record with no entity id would be silently dropped by the groupby below
    # and then left out of the registry, so it is refused loudly instead.
    missing = frame["entity_id"].isin(("", "nan", "None")) | frame["entity_id"].isna()
    if missing.any():
        raise ValueError(
            f"{int(missing.sum())} record(s) in the proposal have no entity id"
        )
    names = names or {}
    attributes_columns = attributes_columns or []

    current = store.current_members(db_path)
    live = store.active_entities(db_path)

    # Group once, in pandas, rather than iterating 19,000 groups in Python —
    # PSC will have millions, and this is the same lesson stage 5 learnt.
    ordered = frame.sort_values(["entity_id", "record_id"], kind="mergesort")
    records_by_entity = ordered.groupby("entity_id", sort=True)["record_id"].apply(list)
    tracks = ordered.groupby("entity_id", sort=True)["track"].first()
    attribute_columns = {}
    for column in attributes_columns:
        value_column, basis_column = f"{column}_entity", f"{column}_entity_basis"
        if value_column in ordered.columns:
            attribute_columns[column] = (
                ordered.groupby("entity_id", sort=True)[value_column].first(),
                ordered.groupby("entity_id", sort=True)[basis_column].first()
                if basis_column in ordered.columns else None,
            )
    plan_entities = [
        {
            "entity_id": entity_id,
            "track": None if pd.isna(tracks[entity_id]) else tracks[entity_id],
            "records": records,
            "attributes": {
                column: {
                    "value": None if pd.isna(values[entity_id]) else values[entity_id],
                    "basis": None if bases is None or pd.isna(bases[entity_id])
                    else bases[entity_id],
                }
                for column, (values, bases) in attribute_columns.items()
            },
        }
        for entity_id, records in records_by_entity.items()
    ]

    # Which registry entities each proposed entity takes over, and which it
    # merely keeps. A registry entity this run does not name at all is left
    # exactly as it is.
    frame["was"] = frame["record_id"].map(current)
    claims = (
        frame.dropna(subset=["was"]).groupby("entity_id", sort=True)["was"]
        .apply(lambda values: sorted(set(values)))
    )

    retire = []
    merged_examples, split_examples, new_examples = [], [], []
    kept_examples, alias_examples = [], []
    new = kept = merged = 0
    for entity in plan_entities:
        entity_id = entity["entity_id"]
        previous = claims.get(entity_id, [])
        if not previous:
            new += 1
            if len(new_examples) < MAX_EXAMPLES:
                new_examples.append({
                    "entity_id": entity_id, "n_records": len(entity["records"]),
                    "names": _names(entity["records"], names),
                })
            continue
        absorbed = [value for value in previous if value != entity_id]
        if absorbed:
            merged += 1
            retire.extend({"entity_id": value, "alias_of": entity_id}
                          for value in absorbed)
            for value in absorbed:
                if len(alias_examples) < MAX_EXAMPLES:
                    alias_examples.append({
                        "retired_entity_id": value, "survivor_entity_id": entity_id,
                        "names": _names(entity["records"], names),
                    })
            if len(merged_examples) < MAX_EXAMPLES:
                merged_examples.append({
                    "entity_id": entity_id, "absorbs": absorbed,
                    "n_records": len(entity["records"]),
                    "names": _names(entity["records"], names),
                })
        else:
            kept += 1
            if len(kept_examples) < MAX_EXAMPLES:
                kept_examples.append({
                    "entity_id": entity_id, "n_records": len(entity["records"]),
                    "names": _names(entity["records"], names),
                })

    # A registry entity whose records this run spreads over several entities is
    # being split. The part that keeps the ID is already decided in stage 5.
    by_previous = (
        frame.dropna(subset=["was"]).groupby("was", sort=True)["entity_id"]
        .apply(lambda values: sorted(set(values)))
    )
    split = 0
    for previous, now in by_previous.items():
        if len(now) > 1:
            split += 1
            if len(split_examples) < MAX_EXAMPLES:
                keeps = [value for value in now if value == previous]
                split_examples.append({
                    "entity_id": previous,
                    "keeps_records": int(
                        (frame["entity_id"] == (keeps[0] if keeps else previous)).sum()
                    ),
                    "new_entities": [value for value in now if value != previous],
                    "names": _names(
                        frame.loc[frame["was"] == previous, "record_id"].tolist()[:5],
                        names,
                    ),
                })

    moved = frame[frame["was"].notna() & (frame["was"] != frame["entity_id"])]
    moved_examples = [
        {"record_id": row["record_id"], "from_entity_id": row["was"],
         "to_entity_id": row["entity_id"], "name": names.get(row["record_id"])}
        for row in moved.head(MAX_EXAMPLES).to_dict("records")
    ]

    summary = {
        "new": new, "kept": kept, "merged": merged, "split": split,
        "aliases": len(retire), "retired": len(retire),
        "records_moved": int(len(moved)), "records_total": int(len(frame)),
    }
    return {
        "entities": plan_entities,
        "retire": retire,
        "summary": summary,
        "examples": {
            "new": new_examples, "kept": kept_examples, "merged": merged_examples,
            "split": split_examples, "moved": moved_examples,
            "aliases": alias_examples,
        },
        "registry_entities": len(live),
    }


def _names(record_ids, names: dict) -> list[str]:
    found = []
    for record_id in record_ids:
        name = names.get(str(record_id))
        if name and name not in found:
            found.append(name)
        if len(found) >= 3:
            break
    return found


def newer_publication(db_path: str, run_id: str, started_at: str | None) -> dict | None:
    """A published run that began after this one, if there is one.

    Publishing an older run over a newer one would undo decisions nobody asked
    to undo, so it is refused unless the caller insists.
    """
    for row in store.publications(db_path):
        if row["run_id"] == run_id:
            continue
        if started_at and row["published_at"] > started_at:
            return row
    return None

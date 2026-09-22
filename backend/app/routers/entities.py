# backend/app/routers/entities.py
"""Clusters, group decisions, entities, publishing, the registry and the export.

The contract is `docs/ENTITIES_API.md`. Everything run-scoped keeps the
``/api/runs/{run_id}/...`` shape the pairs API already uses, so one screen can
move between a pair, a cluster and an entity without changing its base URL.
"""

import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import duckdb_conn, vocabulary
from app.auth import current_user
from app.db import query_db, write_db
from app.profiles import get_profile
from app.registry import plan as registry_plan
from app.registry import provenance as registry_provenance
from app.registry import store as registry_store
from app.services import bucketing_history, clusters_reader, entities_reader
from app.services import run_counts
from app.services import pair_labels, run_manifest
from app.services.audit_logger import log_event

router = APIRouter(tags=["entities"])


def _db_path() -> str:
    from app.main import DB_PATH
    return DB_PATH


def _data_dir() -> Path:
    from app.main import DATA_DIR
    return Path(DATA_DIR)


def _run_or_404(run_id: str) -> dict:
    rows = query_db(_db_path(), "SELECT * FROM runs WHERE id = ?", (run_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")
    return dict(rows[0])


def _run_dir(run_id: str) -> str:
    return str(_data_dir() / "runs" / run_id)


def _decisions() -> dict:
    return pair_labels.decisions_by_scope(_db_path())


def _id_statuses(run_dir: str) -> dict:
    """The fallback ``{entity_id: id_status}`` map, which is now always empty.

    It used to read every row of ``entities.parquet`` on every entities request
    — eleven million of them at the full PSC snapshot — and then hand the map
    to a reader that ignores it, because ``id_status`` is a column of the file
    and ``_base_sql`` already selects it as ``stored_id_status``. A file
    written before that column existed has nothing to build the map from, so
    the old code returned ``{}`` for it too. Kept as a function because the
    reader still takes a map from callers who have one.
    """
    return {}


def _cluster_floor(run_id: str) -> float:
    from app.rules import linkage

    path = _data_dir() / "runs" / run_id / "config" / "linkage_settings.json"
    if not path.is_file():
        return linkage.DEFAULT_CLUSTER_FLOOR
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return linkage.DEFAULT_CLUSTER_FLOOR
    return float(settings.get("cluster_floor", linkage.DEFAULT_CLUSTER_FLOOR))


def _gate_limits(run_id: str) -> dict:
    """The run's own stage 4 gate, so the cluster screen can say what held it."""
    from app.rules import linkage

    path = _data_dir() / "runs" / run_id / "config" / "linkage_settings.json"
    if not path.is_file():
        return {}
    try:
        return linkage.max_distinct_values(
            json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


@router.get("/api/runs/{run_id}/clusters")
def get_clusters(
    run_id: str,
    track: str | None = Query(None, description="person | organisation"),
    status: str | None = Query(None),
    withheld: str | None = Query(None, description="yes | no"),
    decided: str | None = Query(None, description="yes | no"),
    min_units: int = Query(2, ge=1),
    q: str | None = Query(None),
    sort: str = Query(clusters_reader.DEFAULT_SORT),
    order: str = Query("desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(clusters_reader.DEFAULT_LIMIT, ge=1,
                       le=clusters_reader.MAX_LIMIT),
):
    """The cluster review queue, held exact groups included."""
    _run_or_404(run_id)
    try:
        return clusters_reader.get_clusters(
            _run_dir(run_id), track=track, status=status, withheld=withheld,
            decided=decided, min_units=min_units, q=q, sort=sort, order=order,
            offset=offset, limit=limit, decisions=_decisions(),
        )
    except clusters_reader.ClustersNotFound:
        raise HTTPException(status_code=404, detail="Run has no clusters yet")
    except clusters_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/runs/{run_id}/clusters/{cluster_id}")
def get_cluster(
    run_id: str,
    cluster_id: str,
    events: int = Query(0, ge=0, le=1, description="1 adds each unit's evidence rows"),
):
    """One cluster: its units, the pairs between them, and the proposed parts."""
    _run_or_404(run_id)
    try:
        cluster = clusters_reader.get_cluster(
            _run_dir(run_id), cluster_id, decisions=_decisions(),
            with_events=bool(events), cluster_floor=_cluster_floor(run_id),
            gate=_gate_limits(run_id),
        )
    except clusters_reader.ClustersNotFound:
        raise HTTPException(status_code=404, detail="Run has no clusters yet")
    if cluster is None:
        raise HTTPException(status_code=404,
                            detail=f"No cluster '{cluster_id}' in this run")
    return cluster


class DecisionBody(BaseModel):
    kind: str
    parts: list[list[str]] | None = None
    notes: str | None = None
    evidence_url: str | None = None


@router.post("/api/runs/{run_id}/clusters/{cluster_id}/decision")
def save_decision(run_id: str, cluster_id: str, body: DecisionBody,
                  user_name: str = Depends(current_user)):
    """Merge a whole cluster, or split it into parts. Works on a held group too."""
    run = _run_or_404(run_id)
    run_dir = _run_dir(run_id)
    try:
        members = clusters_reader.cluster_members(run_dir, cluster_id)
    except clusters_reader.ClustersNotFound:
        raise HTTPException(status_code=404, detail="Run has no clusters yet")
    if not members:
        raise HTTPException(status_code=404,
                            detail=f"No cluster '{cluster_id}' in this run")

    kind = (body.kind or "").strip().lower()
    if kind not in pair_labels.DECISION_KINDS:
        raise HTTPException(
            status_code=400,
            detail=vocabulary.choice_error("kind", body.kind,
                                           pair_labels.DECISION_KINDS,
                                           "whether to merge or to split"),
        )
    if kind == "merge":
        # One representative per unit: the records inside a unit are already
        # one thing, so labelling them again would say nothing (ENTITIES.md).
        parts = [clusters_reader.cluster_representatives(run_dir, cluster_id)]
    else:
        parts = [[str(r) for r in part] for part in (body.parts or [])]
        if len(parts) < 2:
            raise HTTPException(status_code=400, detail="A split needs at least two parts")
        known = set(members)
        seen: set[str] = set()
        for part in parts:
            for record_id in part:
                if record_id not in known:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Record '{record_id}' is not in cluster {cluster_id}",
                    )
                if record_id in seen:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Record '{record_id}' is in two parts",
                    )
                seen.add(record_id)

    names, track = _member_names(run_dir, members)
    try:
        result = pair_labels.save_decision(
            _db_path(), scope=cluster_id, kind=kind, parts=parts,
            reviewer=user_name, names=names, track=track,
            notes=body.notes, evidence_url=body.evidence_url,
            run_id=run_id, config_version=run.get("config_version"),
        )
    except pair_labels.LabelError as exc:
        status = 422 if "at most" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc))

    log_event(
        _db_path(), user=user_name or "unknown", kind="label",
        description=f"Decided {kind} on {cluster_id} ({result['labels_written']} labels)",
        metadata={"run_id": run_id, "cluster_id": cluster_id, **result},
    )
    from app.routers.runs import _refresh_after_labels, _normalize_counts

    counts = _refresh_after_labels(_db_path(), run_dir, run_id)
    return {**result, "cluster_id": cluster_id, "needs_recluster": True,
            "counts": _normalize_counts(counts)}


class AttributeBody(BaseModel):
    column: str
    value: str | None = None
    notes: str | None = None


@router.post("/api/runs/{run_id}/clusters/{cluster_id}/attribute")
def save_attribute(run_id: str, cluster_id: str, body: AttributeBody,
                   user_name: str = Depends(current_user)):
    """Settle a consensus column for every record of a cluster.

    Stored per record, so it survives a rerun that groups them differently. The
    next recluster reads it back and the column's basis becomes ``human``.
    """
    from app.services import attribute_overrides

    _run_or_404(run_id)
    run_dir = _run_dir(run_id)
    columns = list(getattr(get_profile(), "consensus_columns", []))
    if body.column not in columns:
        raise HTTPException(
            status_code=400,
            detail=vocabulary.choice_error(
                "column", body.column, columns,
                "which value this tool settles for a whole entity"),
        )
    try:
        members = clusters_reader.cluster_members(run_dir, cluster_id)
    except clusters_reader.ClustersNotFound:
        raise HTTPException(status_code=404, detail="Run has no clusters yet")
    if not members:
        raise HTTPException(status_code=404,
                            detail=f"No cluster '{cluster_id}' in this run")

    decision_id = pair_labels.new_decision_id()
    written = attribute_overrides.save_overrides(
        _db_path(), members, body.column, body.value, reviewer=user_name,
        notes=body.notes, decision_id=decision_id,
    )
    log_event(
        _db_path(), user=user_name or "unknown", kind="label",
        description=(f"Set {body.column} to {body.value!r} on {cluster_id} "
                     f"({written} records)"),
        metadata={"run_id": run_id, "cluster_id": cluster_id,
                  "column": body.column, "value": body.value,
                  "records": written, "decision_id": decision_id},
    )
    return {
        "decision_id": decision_id, "column": body.column, "value": body.value,
        "records_set": written, "needs_recluster": True,
    }


@router.delete("/api/runs/{run_id}/clusters/{cluster_id}/decision")
def delete_decision(run_id: str, cluster_id: str,
                    user_name: str = Depends(current_user)):
    """Undo the latest decision on a cluster, every one of its labels at once."""
    _run_or_404(run_id)
    run_dir = _run_dir(run_id)
    from app.services import attribute_overrides

    withdrawn = pair_labels.withdraw_decision(_db_path(), cluster_id)
    if withdrawn is None:
        # An attribute decision writes no labels, so it is undone on its own.
        undone = _withdraw_attribute_decision(_db_path(), run_dir, cluster_id)
        if undone is None:
            raise HTTPException(status_code=404,
                                detail=f"No decision on cluster '{cluster_id}'")
        from app.routers.runs import _normalize_counts, _refresh_after_labels

        return {
            "decision_id": undone["decision_id"],
            "labels_withdrawn": 0,
            "attributes_withdrawn": undone["records"],
            "needs_recluster": True,
            "counts": _normalize_counts(
                _refresh_after_labels(_db_path(), run_dir, run_id)
            ),
        }
    attribute_overrides.withdraw_decision(_db_path(), withdrawn["decision_id"])
    log_event(
        _db_path(), user=user_name or "unknown", kind="label",
        description=(f"Undid the {withdrawn['kind']} decision on {cluster_id} "
                     f"({withdrawn['labels_withdrawn']} labels)"),
        metadata={"run_id": run_id, "cluster_id": cluster_id,
                  "decision_id": withdrawn["decision_id"]},
    )
    from app.routers.runs import _refresh_after_labels, _normalize_counts

    counts = _refresh_after_labels(_db_path(), run_dir, run_id)
    return {
        "decision_id": withdrawn["decision_id"],
        "labels_withdrawn": withdrawn["labels_withdrawn"],
        "attributes_withdrawn": 0,
        "needs_recluster": True,
        "counts": _normalize_counts(counts),
    }


def _withdraw_attribute_decision(db_path: str, run_dir: str, cluster_id: str):
    """Undo the newest attribute decision on a cluster, if it has one."""
    from app.services import attribute_overrides

    try:
        members = clusters_reader.cluster_members(run_dir, cluster_id)
    except clusters_reader.ClustersNotFound:
        return None
    if not members:
        return None
    rows = attribute_overrides.active_overrides(db_path)
    mine = rows[rows["record_id"].isin(set(members))]
    mine = mine[mine["decision_id"].notna()]
    if not len(mine):
        return None
    decision_id = mine.sort_values("id").iloc[-1]["decision_id"]
    count = attribute_overrides.withdraw_decision(db_path, decision_id)
    return {"decision_id": decision_id, "records": count}


def _member_names(run_dir: str, record_ids: list[str]) -> tuple[dict, str | None]:
    """``({record_id: name}, track)`` so a label carries what a human will read."""
    path = Path(run_dir) / "records.parquet"
    if not path.is_file() or not record_ids:
        return {}, None
    con = duckdb_conn.connect(Path(run_dir) / "duckdb_tmp")
    try:
        columns = [d[0] for d in con.execute(
            "SELECT * FROM read_parquet(?) LIMIT 0", [str(path)]
        ).description]
        name = "CAST(name AS VARCHAR)" if "name" in columns else "CAST(NULL AS VARCHAR)"
        track = "CAST(track AS VARCHAR)" if "track" in columns else "CAST(NULL AS VARCHAR)"
        marks = ", ".join("?" * len(record_ids))
        rows = con.execute(
            f"""SELECT CAST(record_id AS VARCHAR), {name}, {track}
                FROM read_parquet(?)
                WHERE CAST(record_id AS VARCHAR) IN ({marks})""",
            [str(path), *record_ids],
        ).fetchall()
    finally:
        con.close()
    names = {row[0]: row[1] for row in rows}
    tracks = {row[2] for row in rows if row[2]}
    return names, (sorted(tracks)[0] if len(tracks) == 1 else None)


# ---------------------------------------------------------------------------
# Recluster
# ---------------------------------------------------------------------------


@router.post("/api/runs/{run_id}/recluster")
def recluster(run_id: str, user_name: str = Depends(current_user)):
    """Redo stages 4 and 5 after decisions, without rerunning Splink."""
    from app.services.pipeline_runner import recluster_run

    _run_or_404(run_id)
    try:
        result = recluster_run(_db_path(), _run_dir(run_id), run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run has no scored pairs yet")
    log_event(
        _db_path(), user=user_name or "unknown", kind="run",
        description=f"Reclustered run {run_id}",
        metadata={"run_id": run_id, **{k: v for k, v in result.items()
                                       if k != "counts"}},
    )
    return result


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@router.get("/api/runs/{run_id}/entities")
def get_entities(
    run_id: str,
    track: str | None = Query(None),
    basis: str | None = Query(None),
    id_status: str | None = Query(None),
    min_size: int = Query(1, ge=1),
    q: str | None = Query(None),
    sort: str = Query(entities_reader.DEFAULT_SORT),
    order: str = Query("desc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(entities_reader.DEFAULT_LIMIT, ge=1,
                       le=entities_reader.MAX_LIMIT),
):
    """The entities this run proposes."""
    _run_or_404(run_id)
    run_dir = _run_dir(run_id)
    try:
        return entities_reader.get_entities(
            run_dir, track=track, basis=basis, id_status=id_status,
            min_size=min_size, q=q, sort=sort, order=order, offset=offset,
            limit=limit, statuses=_id_statuses(run_dir),
        )
    except entities_reader.EntitiesNotFound:
        raise HTTPException(status_code=404, detail="Run has no entities yet")
    except entities_reader.InvalidQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/runs/{run_id}/entities/{entity_id}")
def get_entity(run_id: str, entity_id: str,
               events: int = Query(0, ge=0, le=1)):
    """One entity with its member records."""
    _run_or_404(run_id)
    run_dir = _run_dir(run_id)
    try:
        entity = entities_reader.get_entity(
            run_dir, entity_id, statuses=_id_statuses(run_dir),
            with_events=bool(events),
        )
    except entities_reader.EntitiesNotFound:
        raise HTTPException(status_code=404, detail="Run has no entities yet")
    if entity is None:
        raise HTTPException(status_code=404,
                            detail=f"No entity '{entity_id}' in this run")
    return entity


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


# The preview reads the whole proposal and the whole registry, so a screen that
# polls it would pay for that every time. One slot per run is enough: the key
# carries the proposal's mtime and how many times the registry has been written,
# so a recluster or a publish invalidates it without anyone remembering to.
_PLAN_CACHE: dict[str, tuple] = {}


def _plan_key(run_id: str, path: Path) -> tuple:
    published = query_db(_db_path(), "SELECT COUNT(*) AS n FROM entity_publications")
    retired = query_db(_db_path(),
                       "SELECT COUNT(*) AS n FROM entities WHERE status = 'retired'")
    return (run_id, path.stat().st_mtime_ns, path.stat().st_size,
            published[0]["n"], retired[0]["n"])


def _plan_for(run_id: str) -> dict:
    run_dir = _run_dir(run_id)
    path = entities_reader.entities_path(run_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Run has no entities yet")

    key = _plan_key(run_id, path)
    cached = _PLAN_CACHE.get(run_id)
    if cached and cached[0] == key:
        return cached[1]

    entities = pd.read_parquet(path)
    names = {}
    records_path = Path(run_dir) / "records.parquet"
    if records_path.is_file():
        con = duckdb_conn.connect(Path(run_dir) / "duckdb_tmp")
        try:
            columns = [d[0] for d in con.execute(
                "SELECT * FROM read_parquet(?) LIMIT 0", [str(records_path)]
            ).description]
            if "name" in columns:
                rows = con.execute(
                    "SELECT CAST(record_id AS VARCHAR), CAST(name AS VARCHAR) "
                    "FROM read_parquet(?)", [str(records_path)]
                ).fetchall()
                names = {row[0]: row[1] for row in rows}
        finally:
            con.close()

    plan = registry_plan.build_plan(
        _db_path(), run_id, entities, names,
        list(getattr(get_profile(), "consensus_columns", [])),
    )
    _PLAN_CACHE[run_id] = (key, plan)
    return plan


def _collisions(run_dir: str) -> dict:
    """The run's ID-collision list, or an empty answer when it has none."""
    report_path = entities_reader.report_path(run_dir)
    if not report_path.is_file():
        return {}
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@router.get("/api/runs/{run_id}/publish-preview")
def publish_preview(run_id: str):
    """What publishing this run would change in the registry."""
    run = _run_or_404(run_id)
    plan = _plan_for(run_id)
    newer = registry_plan.newer_publication(_db_path(), run_id, run.get("started_at"))
    collisions = _collisions(_run_dir(run_id))
    latest = registry_store.latest_publication(_db_path())
    return {
        "run_id": run_id,
        "registry_entities": plan["registry_entities"],
        "published_at": (latest or {}).get("published_at"),
        "published_by": (latest or {}).get("published_by"),
        "can_publish": newer is None,
        "blocked_by": ({"kind": "newer_run_published", "run_id": newer["run_id"],
                        "published_at": newer["published_at"]} if newer else None),
        "summary": {**plan["summary"],
                    "id_collisions": collisions.get("id_collisions", 0)},
        "new_examples": plan["examples"]["new"],
        "kept_examples": plan["examples"]["kept"],
        "alias_examples": plan["examples"]["aliases"],
        "merged_examples": plan["examples"]["merged"],
        "split_examples": plan["examples"]["split"],
        "moved_examples": plan["examples"]["moved"],
        "id_collision_examples": collisions.get("id_collision_examples", []),
    }


class PublishBody(BaseModel):
    force: bool = False


@router.post("/api/runs/{run_id}/publish")
def publish(run_id: str, body: PublishBody | None = None,
            user_name: str = Depends(current_user)):
    """Write the proposal into the registry, in one transaction."""
    body = body or PublishBody()
    run = _run_or_404(run_id)
    already = registry_store.publication(_db_path(), run_id)
    if already:
        return {"ok": True, "run_id": run_id, "already": True,
                "published_at": already["published_at"],
                "published_by": already["published_by"],
                "summary": json.loads(already["summary_json"] or "{}")}

    newer = registry_plan.newer_publication(_db_path(), run_id, run.get("started_at"))
    if newer and not body.force:
        raise HTTPException(status_code=409, detail={
            "kind": "newer_run_published", "run_id": newer["run_id"],
            "published_at": newer["published_at"],
        })

    plan = _plan_for(run_id)
    run_dir = _run_dir(run_id)
    entities = pd.read_parquet(
        entities_reader.entities_path(run_dir), columns=["record_id", "entity_id"]
    )
    # The join log and the collision list are written inside the same
    # transaction as the entities, so the registry is never half-explained.
    edges = registry_provenance.build_edges(
        run_dir, Path(run_dir) / "config", _db_path(), run_id, entities,
        config_version=run.get("config_version"),
    )
    result = registry_store.publish(
        _db_path(), run_id, plan, published_by=user_name or "",
        edges=edges, collisions=_collisions(run_dir).get("id_collision_examples") or [],
    )
    # A run's label is its title on screen (the input file's name when unset), so
    # publishing must not write one. The run's counts carry when and by whom, so the
    # runs list can show a "Published" chip without asking the registry per row.
    run_counts.merge(_db_path(), run_id, {
        "published_at": result["published_at"], "published_by": user_name or "",
    })
    log_event(
        _db_path(), user=user_name or "unknown", kind="publish",
        description=(f"Published run {run_id}: {plan['summary']['new']} new, "
                     f"{plan['summary']['kept']} kept, {plan['summary']['merged']} merged"),
        metadata={"run_id": run_id, **result["summary"]},
    )
    return {"ok": True, "run_id": run_id, "already": False,
            "published_at": result["published_at"],
            "published_by": user_name or "", "summary": result["summary"]}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@router.get("/api/registry/aliases.csv")
def aliases_csv():
    """Every retired ID with the entity it finally resolves to."""
    rows = registry_store.alias_rows(_db_path())
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[
        "retired_entity_id", "survivor_entity_id", "track", "retired_run", "retired_at",
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return Response(content=buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=aliases.csv"})


@router.get("/api/runs/{run_id}/entities/{entity_id}/provenance")
def run_entity_provenance(run_id: str, entity_id: str,
                          limit: int = Query(2000, ge=1, le=20000)):
    """Why these records are one entity, read from this run's own files.

    Two shapes of the same answer: ``steps`` is an ordered list a person can
    read, weakest evidence first; ``edges`` is the structured join log the
    steps were written from. `docs/ENTITIES_API.md` has an example.
    """
    run = _run_or_404(run_id)
    run_dir = _run_dir(run_id)
    path = entities_reader.entities_path(run_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Run has no entities yet")

    con = duckdb_conn.connect(Path(run_dir) / "duckdb_tmp")
    try:
        columns = {d[0] for d in con.execute(
            "SELECT * FROM read_parquet(?) LIMIT 0", [str(path)]
        ).description}
        extra = ", ".join(
            f'CAST("{name}" AS VARCHAR) AS "{name}"'
            for name in ("entity_basis", "id_status") if name in columns
        )
        mine = con.execute(
            f"SELECT CAST(record_id AS VARCHAR) AS record_id, "
            f"CAST(entity_id AS VARCHAR) AS entity_id"
            f"{', ' + extra if extra else ''} "
            f"FROM read_parquet(?) WHERE CAST(entity_id AS VARCHAR) = ? "
            f"ORDER BY record_id",
            [str(path), str(entity_id)],
        ).df()
    finally:
        con.close()

    if not len(mine):
        raise HTTPException(
            status_code=404,
            detail=f"Run {run_id} proposes no entity '{entity_id}'",
        )

    built = [
        dict(zip(registry_provenance.EDGE_COLUMNS, row))
        for row in registry_provenance.build_edges(
            run_dir, Path(run_dir) / "config", _db_path(), run_id, mine,
            config_version=run.get("config_version"),
        )
    ]
    # `n_edges` is the true total. `edges` is one page of it, so a reader is
    # never left summing `steps[].n_links` to guess how much was left out.
    n_edges = len(built)
    edges = built[:limit]
    members = [
        {"record_id": row["record_id"],
         "entity_basis": row.get("entity_basis"),
         "entity_basis_label": _basis_label(row.get("entity_basis"))}
        for row in mine.to_dict("records")
    ]
    id_status = next(
        (row.get("id_status") for row in mine.to_dict("records") if row.get("id_status")),
        None,
    )
    return {
        "entity_id": str(entity_id),
        "source": "run",
        "run_id": run_id,
        "config_version": run.get("config_version"),
        "n_records": len(members),
        "id_status": id_status,
        "id_status_label": (vocabulary.ID_ORIGIN.get(str(id_status or "")) or {}).get("label"),
        "question": vocabulary.PROVENANCE_QUESTION,
        "precedence": vocabulary.PROVENANCE_PRECEDENCE,
        "steps": registry_provenance.chain(built, members, id_status, str(entity_id)),
        "n_edges": n_edges,
        "edges": edges,
        "edges_truncated": n_edges > len(edges),
        "members": members,
    }


@router.get("/api/registry/entities/{entity_id}/provenance")
def registry_entity_provenance(entity_id: str,
                               limit: int = Query(2000, ge=1, le=20000)):
    """Why these records are one entity, read from the registry alone.

    This answer survives the deletion of the run folder, because publishing
    wrote it down (`docs/PROVENANCE.md`). The shape is the same as the
    run-scoped answer, with the publication that wrote it named.
    """
    try:
        found = registry_store.entity_detail(_db_path(), entity_id)
    except registry_store.RegistryError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if found is None:
        raise HTTPException(status_code=404,
                            detail=f"No entity '{entity_id}' in the registry")

    resolved = found["entity_id"]
    edges = registry_provenance.edges_of(_db_path(), resolved, limit=limit)
    totals = registry_provenance.counts_by_source(_db_path(), resolved)
    n_edges = sum(totals.values())
    members = found["members"]
    id_status = next((row.get("id_status") for row in members if row.get("id_status")),
                     None)
    runs = sorted({str(edge["run_id"]) for edge in edges if edge.get("run_id")})
    return {
        "entity_id": resolved,
        "requested": found["requested"],
        "redirected": found["redirected"],
        "chain": found["chain"],
        "source": "registry",
        "created_run": found["created_run"],
        "runs": runs,
        "n_records": found["n_records"],
        "id_status": id_status,
        "id_status_label": (vocabulary.ID_ORIGIN.get(str(id_status or "")) or {}).get("label"),
        "question": vocabulary.PROVENANCE_QUESTION,
        "precedence": vocabulary.PROVENANCE_PRECEDENCE,
        "steps": registry_provenance.chain(edges, members, id_status, resolved,
                                           totals=totals),
        "n_edges": n_edges,
        "edges": edges,
        "edges_truncated": n_edges > len(edges),
        "members": members,
        "attributes": found["attributes"],
        "attribute_history": registry_store.attribute_history(_db_path(), resolved),
        "id_collisions": registry_store.collisions_of(_db_path(), resolved),
    }


def _basis_label(value) -> str | None:
    mapped = vocabulary.provenance_for("entity_basis", value)
    return mapped["label"] if mapped else None


@router.get("/api/registry/entities/{entity_id}")
def registry_entity(entity_id: str):
    """Follow the alias chain to the entity that is live now."""
    try:
        found = registry_store.entity_detail(_db_path(), entity_id)
    except registry_store.RegistryError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if found is None:
        raise HTTPException(status_code=404,
                            detail=f"No entity '{entity_id}' in the registry")
    return found


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.get("/api/runs/{run_id}/export")
def export(run_id: str, format: str = Query("xlsx"), scope: str = Query("proposal"),
           user_name: str = Depends(current_user)):
    """The profile's export file, streamed."""
    run = _run_or_404(run_id)
    if format not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail="format must be xlsx or csv")
    if scope not in ("proposal", "published"):
        raise HTTPException(status_code=400, detail="scope must be proposal or published")
    run_dir = Path(_run_dir(run_id))
    entities_path = entities_reader.entities_path(str(run_dir))
    if not entities_path.is_file():
        raise HTTPException(status_code=404, detail="Run has no entities yet")
    if scope == "published" and not registry_store.publication(_db_path(), run_id):
        raise HTTPException(status_code=409, detail="This run has not been published")

    entities = pd.read_parquet(entities_path)
    if scope == "published":
        current = registry_store.current_members(_db_path())
        entities = entities.copy()
        entities["record_id"] = entities["record_id"].astype(str)
        entities["entity_id"] = entities["record_id"].map(current).fillna(
            entities["entity_id"]
        )

    counts = {}
    if run.get("counts_json"):
        try:
            counts = json.loads(run["counts_json"])
        except (ValueError, TypeError):
            counts = {}

    profile = get_profile()
    started = time.time()
    publication = registry_store.publication(_db_path(), run_id)
    try:
        path = profile.export(run_dir, scope, format, {
            "raw": lambda: _raw_input(run),
            "entities": entities,
            "aliases": registry_store.alias_rows(_db_path()),
            "run_id": run_id,
            "config_version": run.get("config_version"),
            "counts": counts,
            "scope": scope,
            "input_name": run.get("input_filename"),
            # What produced the run, and who is taking it away. Read once here
            # so both profiles' exports say the same thing (docs/PROVENANCE.md).
            "manifest": run_manifest.read(run_dir),
            "bucketing": bucketing_history.current(run_dir),
            "triggered_by": run.get("triggered_by"),
            "exported_by": user_name or "unknown",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "published_at": (publication or {}).get("published_at"),
        })
    except NotImplementedError:
        path = _default_export(run_dir, entities, format)
    log_event(
        _db_path(), user=user_name or "unknown", kind="export",
        description=(f"Downloaded the export of run {run_id} — "
                     f"{'the published entity IDs' if scope == 'published' else 'this run\'s proposal'}"
                     f", as {format}"),
        metadata={"run_id": run_id, "scope": scope, "format": format,
                  "filename": path.name,
                  "size_bytes": path.stat().st_size if path.is_file() else None,
                  "published": bool(publication),
                  "seconds": round(time.time() - started, 1)},
    )
    return FileResponse(path=str(path), filename=path.name)


def _raw_input(run: dict) -> pd.DataFrame | None:
    """The original input file, as the profile reads it.

    The profile decides — an export that gives the user's own file back needs
    it, one writing from the run's parquet does not, and re-parsing a 13 GB
    snapshot to add two columns would be absurd.
    """
    profile = get_profile()
    name = run.get("input_filename")
    if not name:
        raise HTTPException(status_code=400, detail="This run has no input file recorded")
    path = _data_dir() / "uploads" / name
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"The input file '{name}' is no longer in the uploads folder",
        )
    return profile.read_input_frame(path)


def _default_export(run_dir: Path, entities: pd.DataFrame, fmt: str) -> Path:
    """A profile with no export of its own gets its records plus the new columns."""
    records = pd.read_parquet(run_dir / "records.parquet")
    records["record_id"] = records["record_id"].astype(str)
    merged = records.merge(
        entities[["record_id", "entity_id", "entity_basis"]], on="record_id", how="left"
    )
    path = run_dir / f"export.{'csv' if fmt != 'xlsx' else 'xlsx'}"
    if fmt == "xlsx":
        merged.to_excel(path, index=False)
    else:
        merged.to_csv(path, index=False, encoding="utf-8-sig")
    return path

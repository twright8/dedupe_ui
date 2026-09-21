# backend/app/registry/store.py
"""Read and write the durable entity registry.

Three tables (`docs/ENTITIES.md`): ``entities``, ``entity_members`` and
``entity_attributes``, plus ``entity_publications`` so publishing twice is a
no-op. A run writes none of them — only ``publish`` does, and it does the whole
thing inside one SQLite transaction.

A retired ID is never deleted. It stays as a redirect to the entity that
absorbed it, so a link printed in a report last year still resolves. Chains are
followed to the end, because an entity that absorbed another may later be
absorbed itself.
"""

import json
import sqlite3
from datetime import datetime, timezone

from app import vocabulary
from app.db import get_db, query_db

# An alias chain longer than this is a bug, not data. Cutting it is better than
# looping forever inside a web request.
MAX_ALIAS_HOPS = 50


class RegistryError(RuntimeError):
    """The registry cannot answer, and the caller has to be told why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def active_entities(db_path: str) -> dict[str, dict]:
    """``{entity_id: row}`` for every live entity."""
    return {
        row["entity_id"]: dict(row)
        for row in query_db(db_path, "SELECT * FROM entities WHERE status = 'active'")
    }


def current_members(db_path: str) -> dict[str, str]:
    """``{record_id: entity_id}`` for every record the registry currently holds."""
    return {
        row["record_id"]: row["entity_id"]
        for row in query_db(
            db_path,
            "SELECT record_id, entity_id FROM entity_members WHERE until_run IS NULL",
        )
    }


def members_of(db_path: str, entity_id: str) -> list[str]:
    return [
        row["record_id"]
        for row in query_db(
            db_path,
            "SELECT record_id FROM entity_members "
            "WHERE entity_id = ? AND until_run IS NULL ORDER BY record_id",
            (entity_id,),
        )
    ]


def attributes_of(db_path: str, entity_id: str) -> dict:
    """The values in force now. Superseded rows stay in the table, unread."""
    return {
        row["column_name"]: {
            "value": row["value"],
            "basis": row["basis"],
            "basis_label": vocabulary.VALUE_BASIS.get(
                str(row["basis"] or ""), {}).get("label"),
            "rule_id": row["rule_id"],
            "tally": _loads(row["tally_json"]),
            "since_run": row["since_run"] or row["run_id"],
        }
        for row in query_db(
            db_path,
            "SELECT column_name, value, basis, rule_id, tally_json, since_run, run_id "
            "FROM entity_attributes WHERE entity_id = ? AND until_run IS NULL",
            (entity_id,),
        )
    }


def attribute_history(db_path: str, entity_id: str) -> list[dict]:
    """Every value this entity has held, oldest first."""
    return [
        {**dict(row), "tally": _loads(row["tally_json"])}
        for row in query_db(
            db_path,
            "SELECT column_name, value, basis, rule_id, tally_json, since_run, "
            "until_run, run_id FROM entity_attributes WHERE entity_id = ? "
            "ORDER BY id",
            (entity_id,),
        )
    ]


def member_rows(db_path: str, entity_id: str) -> list[dict]:
    """Each current member with why it is here and where the ID came from."""
    rows = query_db(
        db_path,
        "SELECT record_id, entity_basis, id_status, since_run FROM entity_members "
        "WHERE entity_id = ? AND until_run IS NULL ORDER BY record_id",
        (entity_id,),
    )
    out = []
    for row in rows:
        mapped = vocabulary.provenance_for("entity_basis", row["entity_basis"])
        origin = vocabulary.ID_ORIGIN.get(str(row["id_status"] or ""))
        out.append({
            **dict(row),
            "entity_basis_label": mapped["label"] if mapped else None,
            "id_status_label": origin["label"] if origin else None,
        })
    return out


def collisions_of(db_path: str, entity_id: str) -> list[dict]:
    """Times this entity's ID was claimed twice and one side gave way."""
    return [dict(row) for row in query_db(
        db_path,
        "SELECT * FROM entity_id_collisions WHERE entity_id = ? OR minted = ? "
        "ORDER BY id",
        (str(entity_id), str(entity_id)),
    )]


def _loads(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def resolve(db_path: str, entity_id: str) -> dict | None:
    """Follow the alias chain from *entity_id* to the entity that is live now.

    Returns ``{requested, entity_id, chain, redirected, row}`` or None when the
    id has never existed. A chain that loops raises rather than hanging.
    """
    wanted = str(entity_id)
    rows = query_db(db_path, "SELECT * FROM entities WHERE entity_id = ?", (wanted,))
    if not rows:
        return None

    chain = [wanted]
    row = dict(rows[0])
    seen = {wanted}
    hops = 0
    while row["status"] != "active" and row.get("alias_of"):
        hops += 1
        target = row["alias_of"]
        if target in seen or hops > MAX_ALIAS_HOPS:
            raise RegistryError(f"Alias chain for '{wanted}' does not end")
        seen.add(target)
        chain.append(target)
        found = query_db(db_path, "SELECT * FROM entities WHERE entity_id = ?", (target,))
        if not found:
            raise RegistryError(
                f"Alias chain for '{wanted}' points at '{target}', which is not in the registry"
            )
        row = dict(found[0])

    return {
        "requested": wanted,
        "entity_id": row["entity_id"],
        "chain": chain,
        "redirected": len(chain) > 1,
        "row": row,
    }


def entity_detail(db_path: str, entity_id: str) -> dict | None:
    """One registry entity, aliases followed, with its records and attributes."""
    resolved = resolve(db_path, entity_id)
    if resolved is None:
        return None
    row = resolved["row"]
    records = members_of(db_path, row["entity_id"])
    return {
        "requested": resolved["requested"],
        "entity_id": row["entity_id"],
        "track": row["track"],
        "status": row["status"],
        "redirected": resolved["redirected"],
        "chain": resolved["chain"],
        "created_run": row["created_run"],
        "created_at": row["created_at"],
        "n_records": len(records),
        "records": records,
        "members": member_rows(db_path, row["entity_id"]),
        "attributes": attributes_of(db_path, row["entity_id"]),
    }


def alias_rows(db_path: str) -> list[dict]:
    """Every retired id with the entity it finally resolves to.

    The chain is walked here so nothing downstream has to: a client reading
    ``aliases.csv`` gets the answer, not a puzzle.
    """
    retired = query_db(
        db_path,
        "SELECT * FROM entities WHERE status = 'retired' ORDER BY entity_id",
    )
    rows = []
    for entry in retired:
        try:
            resolved = resolve(db_path, entry["entity_id"])
            survivor = resolved["entity_id"] if resolved else None
        except RegistryError:
            survivor = None
        rows.append({
            "retired_entity_id": entry["entity_id"],
            "survivor_entity_id": survivor,
            "track": entry["track"],
            "retired_run": entry["retired_run"],
            "retired_at": entry["created_at"],
        })
    return rows


def publication(db_path: str, run_id: str) -> dict | None:
    rows = query_db(
        db_path, "SELECT * FROM entity_publications WHERE run_id = ?", (run_id,)
    )
    return dict(rows[0]) if rows else None


def latest_publication(db_path: str) -> dict | None:
    rows = query_db(
        db_path,
        "SELECT * FROM entity_publications ORDER BY published_at DESC, run_id DESC LIMIT 1",
    )
    return dict(rows[0]) if rows else None


def publications(db_path: str) -> list[dict]:
    return [dict(row) for row in query_db(
        db_path, "SELECT * FROM entity_publications ORDER BY published_at"
    )]


def is_empty(db_path: str) -> bool:
    return not query_db(db_path, "SELECT 1 FROM entities LIMIT 1")


# ---------------------------------------------------------------------------
# Writing — only publish() does
# ---------------------------------------------------------------------------


def publish(
    db_path: str,
    run_id: str,
    plan: dict,
    published_by: str = "",
    published_at: str | None = None,
    edges=None,
    collisions: list[dict] | None = None,
) -> dict:
    """Write *plan* into the registry in one transaction.

    *plan* is what ``registry.plan.build_plan`` worked out:

      ``entities``   [{entity_id, track, records, bases, id_status, attributes}]
      ``retire``     [{entity_id, alias_of}]
      ``summary``    the numbers the preview showed

    *edges* is the run's join log — every accepted link inside a published
    entity, from ``registry.provenance.build_edges``. *collisions* is the run's
    ID-collision list. Both are written here so that deleting the run folder
    does not take the answer to "why are these two records one entity?" with it
    (`docs/PROVENANCE.md`).

    Every statement runs inside one ``BEGIN``/``COMMIT``. A failure half way
    leaves the registry exactly as it was, which matters more here than
    anywhere else in the app: a half-published registry would hand out entity
    IDs that no later run could reproduce.
    """
    from app.registry import provenance
    connection = get_db(db_path)
    now = published_at or _now()

    try:
        connection.execute("BEGIN IMMEDIATE")

        known = {
            row["entity_id"]
            for row in connection.execute("SELECT entity_id FROM entities")
        }
        # Every record this run places. Their old rows close first, so the
        # one-live-row-per-record index is never broken mid-write.
        placing = [str(r) for entity in plan["entities"] for r in entity["records"]]
        for chunk in _chunks(placing, 500):
            marks = ",".join("?" * len(chunk))
            connection.execute(
                f"UPDATE entity_members SET until_run = ? "
                f"WHERE until_run IS NULL AND record_id IN ({marks})",
                (run_id, *chunk),
            )

        for entity in plan["entities"]:
            entity_id = str(entity["entity_id"])
            if entity_id in known:
                connection.execute(
                    "UPDATE entities SET status = 'active', alias_of = NULL, "
                    "retired_run = NULL, track = ? WHERE entity_id = ?",
                    (entity.get("track"), entity_id),
                )
            else:
                connection.execute(
                    "INSERT INTO entities (entity_id, track, created_run, created_at, "
                    "status) VALUES (?, ?, ?, ?, 'active')",
                    (entity_id, entity.get("track"), run_id, now),
                )
                known.add(entity_id)
            bases = entity.get("bases") or []
            id_status = entity.get("id_status")
            connection.executemany(
                "INSERT INTO entity_members (record_id, entity_id, since_run, "
                "entity_basis, id_status) VALUES (?, ?, ?, ?, ?)",
                [(str(record), entity_id, run_id,
                  _clean(bases[index]) if index < len(bases) else None,
                  _clean(id_status))
                 for index, record in enumerate(entity["records"])],
            )
            # Attributes keep history now, like members: close the live row and
            # open a new one, rather than deleting what the last run said.
            attributes = entity.get("attributes") or {}
            if attributes:
                marks = ",".join("?" * len(attributes))
                connection.execute(
                    f"UPDATE entity_attributes SET until_run = ? WHERE entity_id = ? "
                    f"AND until_run IS NULL AND column_name IN ({marks})",
                    (run_id, entity_id, *attributes),
                )
            connection.executemany(
                "INSERT INTO entity_attributes (entity_id, column_name, value, basis, "
                "run_id, since_run, rule_id, tally_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(entity_id, column,
                  str(a.get("value")) if a.get("value") is not None else None,
                  _clean(a.get("basis")), run_id, run_id, _clean(a.get("rule_id")),
                  _tally(a.get("tally")))
                 for column, a in attributes.items()],
            )

        for retirement in plan["retire"]:
            connection.execute(
                "UPDATE entities SET status = 'retired', alias_of = ?, retired_run = ? "
                "WHERE entity_id = ?",
                (str(retirement["alias_of"]), run_id, str(retirement["entity_id"])),
            )

        n_edges = provenance.write_edges(connection, run_id, edges or ())
        n_collisions = provenance.write_collisions(connection, run_id, collisions or [])

        summary = {**plan["summary"], "edges": n_edges, "id_collisions": n_collisions}
        connection.execute(
            "INSERT OR REPLACE INTO entity_publications "
            "(run_id, published_at, published_by, summary_json) VALUES (?, ?, ?, ?)",
            (run_id, now, published_by, json.dumps(summary)),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise

    return {"run_id": run_id, "published_at": now, "summary": summary}


def _clean(value):
    """A plain string, or None. Keeps NaN and empty text out of the registry."""
    if value is None:
        return None
    text = str(value)
    if text in ("", "nan", "None", "<NA>"):
        return None
    return text


def _tally(value) -> str | None:
    """The majority vote as JSON, whether it arrives as a dict or as text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def _chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]

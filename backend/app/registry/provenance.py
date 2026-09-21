# backend/app/registry/provenance.py
"""Why records are together — stored at publish, never rebuilt.

A live run can show the links behind a merge because ``pairs.parquet`` is still
on disk. Delete the run folder and the answer goes with it. This module writes
the answer down instead, at publish, into ``entity_edges``: one row for every
accepted link inside every published entity, with the score and the scorer, the
match key, the veto a reviewer overrode, the label and who saved it, and the
earlier ID two records shared.

**Exact groups are logged as a star, not as every pair.** A match key that puts
1,000 records together makes 499,500 pairs and 999 links. The 999 are the
smallest record id joined to each of the others, which is enough to say that all
1,000 are one group, and which is what a reader wants to see.

**Sized for PSC.** The edges are built in DuckDB from the run's parquet files,
never in pandas, and loaded into SQLite in one transaction with the indexes
dropped first and rebuilt after. Tens of millions of rows load in minutes that
way and in hours the other way.

The words are ``app/vocabulary.py``'s: ``source`` is always one of the six
labels on the ordered provenance list, so the chain reads the same here, on the
screen and in an export.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from app import duckdb_conn, vocabulary
from app.db import query_db

logger = logging.getLogger(__name__)

#: The columns of one ``entity_edges`` row, in the order the insert binds them.
EDGE_COLUMNS = (
    "entity_id", "run_id", "source",
    "record_id_a", "record_id_b", "unit_id_a", "unit_id_b",
    "match_key", "match_key_id", "group_id",
    "score", "scorer", "model_version",
    "veto_overridden", "veto_reason",
    "label_id", "reviewer", "decided_at", "note", "evidence_url",
    "earlier_entity_id", "config_version",
)

#: How many rows go to SQLite at a time. Large enough that the round trips do
#: not dominate, small enough that the batch itself is never the memory problem.
BATCH = 20_000

#: The indexes the join log carries. They are dropped before a bulk load and
#: made again after it, because building a b-tree once over a finished table is
#: much cheaper than maintaining it row by row.
EDGE_INDEXES = {
    "idx_entity_edges_entity": "CREATE INDEX idx_entity_edges_entity "
                               "ON entity_edges (entity_id)",
    "idx_entity_edges_run": "CREATE INDEX idx_entity_edges_run "
                            "ON entity_edges (run_id)",
}

#: What ``decided_by`` on a pair means as a link. Both scorers are Score on the
#: ordered list; ``scorer`` is what says which one.
_SOURCE_FROM_DECIDED_BY = {
    "score": "score",
    "model": "model",
    "import": "import",
    "human": "human",
    "veto": "veto",
}


# ---------------------------------------------------------------------------
# What the run folder can tell us
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def match_key_names(config_dir: Path) -> dict[str, str]:
    """``{key id: name}`` from the run's own frozen ruleset."""
    ruleset = _read_json(Path(config_dir) / "ruleset.json")
    names = {}
    for key in ruleset.get("match_keys") or []:
        key_id = str(key.get("id") or "")
        if key_id:
            names[key_id] = str(key.get("name") or key_id)
    return names


def model_versions(run_dir: Path) -> dict[str, str]:
    """``{track: model version}`` from the run's ``model_state.json``.

    Empty when no model was applied, which is the usual case for donations.
    """
    state = _read_json(Path(run_dir) / "model_state.json")
    tracks = state.get("tracks") if isinstance(state.get("tracks"), dict) else state
    versions = {}
    for track, detail in (tracks or {}).items():
        if isinstance(detail, dict) and detail.get("version") is not None:
            versions[str(track)] = str(detail["version"])
    return versions


def active_labels(db_path: str) -> list[dict]:
    """Every live reviewer answer, with what it says and who said it."""
    return query_db(
        db_path,
        """SELECT id, record_id_a, record_id_b, is_match, reviewer, notes,
                  evidence_url, created_at, provenance
           FROM pair_labels WHERE active = 1""",
    )


# ---------------------------------------------------------------------------
# Building the edges
# ---------------------------------------------------------------------------


def _file(run_dir: Path, name: str) -> Path | None:
    path = Path(run_dir) / name
    return path if path.is_file() else None


def build_edges(
    run_dir: str | Path,
    config_dir: str | Path,
    db_path: str,
    run_id: str,
    entities,
    config_version: int | None = None,
) -> Iterator[tuple]:
    """Yield one ``entity_edges`` row per accepted link inside a published entity.

    *entities* is the run's proposal — a frame with ``record_id`` and
    ``entity_id``. Everything else is read from the run folder and the label
    table. Nothing is held whole in memory: DuckDB does the joins over the
    parquet files and the rows come back in batches.
    """
    import pandas as pd

    run_dir = Path(run_dir)
    config_dir = Path(config_dir)
    key_names = match_key_names(config_dir)
    versions = model_versions(run_dir)

    lookup = pd.DataFrame({
        "record_id": entities["record_id"].astype(str),
        "entity_id": entities["entity_id"].astype(str),
    })

    con = duckdb_conn.connect(run_dir / "duckdb_tmp")
    try:
        con.register("prov_entities", lookup)
        members_file = _file(run_dir, "unit_members.parquet")
        if members_file is not None:
            con.execute(
                "CREATE OR REPLACE TABLE prov_rep AS "
                "SELECT CAST(unit_id AS VARCHAR) AS unit_id, "
                "       min(CAST(record_id AS VARCHAR)) AS record_id "
                f"FROM read_parquet('{members_file.as_posix()}') GROUP BY unit_id"
            )

        yield from _exact_group_edges(con, run_dir, run_id, key_names, config_version)
        yield from _pair_edges(
            con, run_dir, db_path, run_id, versions, config_version,
            have_units=members_file is not None,
        )
    finally:
        con.close()


def _exact_group_edges(con, run_dir: Path, run_id: str, key_names: dict,
                       config_version: int | None) -> Iterator[tuple]:
    """Star edges: the group's smallest record id joined to each other member."""
    groups_file = _file(run_dir, "exact_groups.parquet")
    if groups_file is None:
        return
    merged = vocabulary.EXACT_GROUP_STATUSES[0]  # "merged"
    con.execute(f"""
        CREATE OR REPLACE TABLE prov_exact AS
        WITH g AS (
            SELECT CAST(record_id AS VARCHAR) AS record_id,
                   CAST(group_id AS VARCHAR) AS group_id,
                   CAST(key_ids AS VARCHAR) AS key_ids
            FROM read_parquet('{groups_file.as_posix()}')
            WHERE status = '{merged}'
        ),
        anchor AS (SELECT group_id, min(record_id) AS record_id FROM g GROUP BY group_id)
        SELECT e.entity_id, a.record_id AS record_id_a, g.record_id AS record_id_b,
               g.group_id, g.key_ids
        FROM g
        JOIN anchor a ON a.group_id = g.group_id AND a.record_id <> g.record_id
        JOIN prov_entities e ON e.record_id = g.record_id
        ORDER BY e.entity_id, g.group_id, g.record_id
    """)
    for row in _batches(con, "SELECT * FROM prov_exact"):
        entity_id, record_a, record_b, group_id, key_ids = row
        key_id = (key_ids or "").split("|")[0] or None
        yield (
            entity_id, run_id, "exact_key",
            record_a, record_b, None, None,
            key_names.get(key_id or "", key_id), key_id, group_id,
            None, None, None,
            None, None,
            None, None, None, None, None,
            None, config_version,
        )


def _pair_edges(con, run_dir: Path, db_path: str, run_id: str,
                versions: dict, config_version: int | None,
                have_units: bool) -> Iterator[tuple]:
    """One edge per accepted pair whose two sides ended in the same entity.

    A pair the gate then broke apart — the two units are in different entities —
    is not a link inside an entity, so it is not logged as one.
    """
    pairs_file = _file(run_dir, "pairs.parquet")
    if pairs_file is None or not have_units:
        return

    columns = {c.lower() for c in _parquet_columns(con, pairs_file)}
    score_column = "gbt_score" if "gbt_score" in columns else "NULL"
    has_veto = "vetoed_by" in columns
    units_file = _file(run_dir, "units.parquet")
    earlier = "NULL"
    units_join = ""
    if units_file is not None and "existing_entity_id" in {
        c.lower() for c in _parquet_columns(con, units_file)
    }:
        con.execute(
            "CREATE OR REPLACE TABLE prov_units AS SELECT CAST(unit_id AS VARCHAR) "
            "AS unit_id, CAST(existing_entity_id AS VARCHAR) AS existing_entity_id "
            f"FROM read_parquet('{units_file.as_posix()}')"
        )
        units_join = "LEFT JOIN prov_units u ON u.unit_id = CAST(p.unit_id_l AS VARCHAR)"
        earlier = "u.existing_entity_id"

    labels = _labels_by_unit_pair(con, db_path, run_dir)
    label_join = ""
    label_select = "NULL AS label_id, NULL AS reviewer, NULL AS decided_at, " \
                   "NULL AS note, NULL AS evidence_url"
    if labels is not None:
        con.register("prov_labels", labels)
        label_join = ("LEFT JOIN prov_labels l "
                      "ON l.unit_id_l = CAST(p.unit_id_l AS VARCHAR) "
                      "AND l.unit_id_r = CAST(p.unit_id_r AS VARCHAR)")
        label_select = ("l.label_id, l.reviewer, l.decided_at, l.note, "
                        "l.evidence_url, l.record_id_a AS label_record_a, "
                        "l.record_id_b AS label_record_b")

    con.execute(f"""
        CREATE OR REPLACE TABLE prov_pairs AS
        SELECT el.entity_id AS entity_id,
               rl.record_id AS record_id_a, rr.record_id AS record_id_b,
               CAST(p.unit_id_l AS VARCHAR) AS unit_id_a,
               CAST(p.unit_id_r AS VARCHAR) AS unit_id_b,
               p.decided_by AS decided_by, p.track AS track,
               p.match_probability AS match_probability,
               {score_column} AS model_score,
               {"p.vetoed_by" if has_veto else "NULL"} AS vetoed_by,
               {"p.veto_reason" if has_veto else "NULL"} AS veto_reason,
               {earlier} AS earlier_entity_id,
               {label_select}
        FROM read_parquet('{pairs_file.as_posix()}') p
        JOIN prov_rep rl ON rl.unit_id = CAST(p.unit_id_l AS VARCHAR)
        JOIN prov_rep rr ON rr.unit_id = CAST(p.unit_id_r AS VARCHAR)
        JOIN prov_entities el ON el.record_id = rl.record_id
        JOIN prov_entities er ON er.record_id = rr.record_id
        {units_join}
        {label_join}
        WHERE p.bucket = 'accept' AND el.entity_id = er.entity_id
        ORDER BY el.entity_id, rl.record_id, rr.record_id
    """)

    names = [d[0] for d in con.execute("SELECT * FROM prov_pairs LIMIT 0").description]
    for row in _batches(con, "SELECT * FROM prov_pairs"):
        item = dict(zip(names, row))
        decided_by = str(item.get("decided_by") or "score").lower()
        source = _SOURCE_FROM_DECIDED_BY.get(decided_by, "score")
        # The model is a scorer, so it reads as Score on the ordered list.
        # `scorer` is the line that says which one, and which version.
        scored_by_model = source == "model"
        model_score = item.get("model_score")
        score = model_score if scored_by_model else item.get("match_probability")
        record_a = item.get("label_record_a") or item["record_id_a"]
        record_b = item.get("label_record_b") or item["record_id_b"]
        if source != "human":
            record_a, record_b = item["record_id_a"], item["record_id_b"]
        yield (
            item["entity_id"], run_id, source,
            str(record_a), str(record_b), item["unit_id_a"], item["unit_id_b"],
            None, None, None,
            None if score is None else float(score),
            ("model" if scored_by_model else "splink") if source in ("score", "model")
            else None,
            versions.get(str(item.get("track") or "")) if scored_by_model else None,
            item.get("vetoed_by"), item.get("veto_reason"),
            item.get("label_id"), item.get("reviewer"), item.get("decided_at"),
            item.get("note"), item.get("evidence_url"),
            item.get("earlier_entity_id") if source == "import" else None,
            config_version,
        )


def _labels_by_unit_pair(con, db_path: str, run_dir: Path):
    """Every live Match answer, with the two units that now hold its records."""
    import pandas as pd

    rows = [row for row in active_labels(db_path)
            if str(row.get("is_match") or "").upper() == "TRUE"]
    if not rows:
        return None
    members_file = _file(run_dir, "unit_members.parquet")
    if members_file is None:
        return None
    frame = pd.DataFrame(rows)
    members = con.execute(
        "SELECT CAST(record_id AS VARCHAR) AS record_id, "
        f"CAST(unit_id AS VARCHAR) AS unit_id FROM read_parquet('{members_file.as_posix()}')"
    ).df()
    lookup = members.drop_duplicates("record_id").set_index("record_id")["unit_id"]
    left = frame["record_id_a"].astype(str).map(lookup)
    right = frame["record_id_b"].astype(str).map(lookup)
    keep = left.notna() & right.notna()
    frame = frame[keep.to_numpy()].copy()
    if not len(frame):
        return None
    left, right = left[keep], right[keep]
    lower = left.where(left <= right, right)
    upper = right.where(left <= right, left)
    return pd.DataFrame({
        "unit_id_l": lower.to_numpy(),
        "unit_id_r": upper.to_numpy(),
        "label_id": frame["id"].to_numpy(),
        "reviewer": frame["reviewer"].to_numpy(),
        "decided_at": frame["created_at"].to_numpy(),
        "note": frame["notes"].to_numpy(),
        "evidence_url": frame["evidence_url"].to_numpy(),
        "record_id_a": frame["record_id_a"].astype(str).to_numpy(),
        "record_id_b": frame["record_id_b"].astype(str).to_numpy(),
    }).drop_duplicates(subset=["unit_id_l", "unit_id_r"])


def _parquet_columns(con, path: Path) -> list[str]:
    return [
        row[0] for row in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}') LIMIT 0"
        ).fetchall()
    ]


def _batches(con, sql: str):
    cursor = con.execute(sql)
    while True:
        rows = cursor.fetchmany(BATCH)
        if not rows:
            return
        yield from rows


# ---------------------------------------------------------------------------
# Writing, inside the publish transaction
# ---------------------------------------------------------------------------


def write_edges(connection, run_id: str, rows: Iterator[tuple]) -> int:
    """Replace this run's join log. Returns how many links were written.

    Idempotent: publishing the same run twice deletes what the first publish
    wrote and writes the same rows again. The indexes come off before the load
    and go back on after it.
    """
    connection.execute("DELETE FROM entity_edges WHERE run_id = ?", (run_id,))
    for name in EDGE_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {name}")

    marks = ",".join("?" * len(EDGE_COLUMNS))
    sql = (f"INSERT INTO entity_edges ({', '.join(EDGE_COLUMNS)}) VALUES ({marks})")
    written = 0
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH:
            connection.executemany(sql, batch)
            written += len(batch)
            batch = []
    if batch:
        connection.executemany(sql, batch)
        written += len(batch)

    for statement in EDGE_INDEXES.values():
        connection.execute(statement)
    return written


def write_collisions(connection, run_id: str, collisions: list[dict]) -> int:
    """Copy the run's ID-collision list into the registry. Idempotent."""
    connection.execute("DELETE FROM entity_id_collisions WHERE run_id = ?", (run_id,))
    if not collisions:
        return 0
    connection.executemany(
        """INSERT INTO entity_id_collisions
           (run_id, entity_id, kept_by_key, n_records_kept, minted,
            minted_for_key, n_records_minted, from_registry)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(run_id, str(c.get("entity_id") or ""), c.get("kept_by_key"),
          c.get("n_records_kept"), c.get("minted"), c.get("minted_for_key"),
          c.get("n_records_minted"), 1 if c.get("from_registry") else 0)
         for c in collisions],
    )
    return len(collisions)


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


def edges_of(db_path: str, entity_id: str, run_id: str | None = None,
             limit: int = 2000) -> list[dict]:
    """The stored links inside one entity, strongest kind last."""
    sql = "SELECT * FROM entity_edges WHERE entity_id = ?"
    params: list = [str(entity_id)]
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY id LIMIT ?"
    params.append(int(limit) + 1)
    rows = query_db(db_path, sql, tuple(params))
    return rows[:limit]


def _rank(edge: dict) -> int:
    mapped = vocabulary.provenance_for("edge_source", edge.get("source"))
    return vocabulary.provenance_rank(mapped["key"]) if mapped else 0


def chain(edges: list[dict], members: list[dict] | None = None,
          id_status: str | None = None, entity_id: str | None = None) -> list[dict]:
    """The same links as an ordered list of steps a person can read.

    Weakest first, so the list reads as the story of the merge: a match key put
    some records together, the score joined more, the earlier grouping joined
    more again, and a reviewer had the last word. Each step names its own
    evidence.
    """
    steps: list[dict] = []
    n_records = len(members or [])
    if n_records:
        steps.append({
            "order": 0,
            "source": None,
            "label": "This entity",
            "text": f"This entity holds {n_records:,} record"
                    f"{'' if n_records == 1 else 's'}.",
            "n_links": 0,
        })

    by_source: dict[str, list[dict]] = {}
    for edge in edges:
        by_source.setdefault(str(edge.get("source") or "score"), []).append(edge)

    for source in sorted(by_source, key=lambda s: _rank({"source": s})):
        group = by_source[source]
        mapped = vocabulary.provenance_for("edge_source", source) or {}
        steps.append({
            "order": len(steps),
            "source": source,
            "label": mapped.get("label") or source,
            "definition": mapped.get("definition"),
            "text": _sentence(source, group),
            "n_links": len(group),
            "examples": [_example(edge) for edge in group[:5]],
        })

    if id_status:
        origin = vocabulary.ID_ORIGIN.get(str(id_status))
        if origin:
            steps.append({
                "order": len(steps),
                "source": None,
                "label": vocabulary.ID_ORIGIN_QUESTION,
                "text": f"{origin['label']}. {origin['definition']}",
                "n_links": 0,
            })
    return steps


def _sentence(source: str, edges: list[dict]) -> str:
    n = len(edges)
    links = f"{n:,} link{'' if n == 1 else 's'}"
    if source == "exact_key":
        names = sorted({str(e.get("match_key") or e.get("match_key_id") or "")
                        for e in edges if e.get("match_key") or e.get("match_key_id")})
        named = ", ".join(names[:3]) if names else "a match key"
        return (f"A match key put these records together: {named}. "
                f"That is {links}.")
    if source in ("score", "model"):
        scores = [e["score"] for e in edges if e.get("score") is not None]
        scorer = "the model score" if source == "model" else "the Splink score"
        if scores:
            return (f"{links} were accepted on {scorer}. The scores ran from "
                    f"{min(scores):.2f} to {max(scores):.2f}.")
        return f"{links} were accepted on {scorer}."
    if source == "import":
        ids = sorted({str(e["earlier_entity_id"]) for e in edges
                      if e.get("earlier_entity_id")})
        if ids:
            named = ", ".join(ids[:3])
            return (f"{links} were accepted because both sides already carried "
                    f"the same earlier ID: {named}.")
        return f"{links} were accepted because both sides carried the same earlier ID."
    if source == "human":
        reviewers = sorted({str(e["reviewer"]) for e in edges if e.get("reviewer")})
        who = ", ".join(reviewers[:3]) if reviewers else "a reviewer"
        return f"A reviewer said Match on {links}. Saved by {who}."
    if source == "veto":
        return f"A veto rule moved {links}."
    return f"{links} of this kind."


def _example(edge: dict) -> dict:
    """One link, with only the fields that kind of link fills in."""
    out = {
        "record_id_a": edge.get("record_id_a"),
        "record_id_b": edge.get("record_id_b"),
        "source": edge.get("source"),
    }
    for field in ("match_key", "match_key_id", "group_id", "score", "scorer",
                  "model_version", "veto_overridden", "veto_reason", "label_id",
                  "reviewer", "decided_at", "note", "evidence_url",
                  "earlier_entity_id"):
        if edge.get(field) not in (None, ""):
            out[field] = edge[field]
    return out

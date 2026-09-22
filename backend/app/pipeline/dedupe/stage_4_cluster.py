# backend/app/pipeline/dedupe/stage_4_cluster.py
"""Stage 4: group the accepted pairs into clusters, and gate the doubtful ones.

A cluster is a connected component over the pairs that ended up in the
``accept`` bucket once the score, the import overlay and the human labels have
all had their say. That is the easy half. The half that matters is the **gate**
(`docs/ENTITIES.md`): a cluster that looks like a chain, or is enormous, or
would merge groups the earlier manual work kept apart, is not proposed as one
entity. It is **withheld** — rebuilt from the trusted edges alone, which are the
import and human ones — and sent to a review queue.

**Out of core (B5).** Nothing here reads a whole parquet file. The units, the
unit members and the pairs are named to DuckDB and every edge, every group-by
and the output file itself are SQL. The full PSC snapshot is about 15 million
units and 100 million pairs, and neither fits in the server's RAM as a pandas
frame.

The one step that cannot be SQL is the connected components, because SciPy owns
that walk. It is given **integer unit codes**, never strings: the unit ids are
dense-coded in DuckDB (``row_number() OVER (ORDER BY unit_id)``) and SciPy gets
two int32 arrays. A Python dict of 15 million strings, which is what the old
``pd.Series(..., index=unit_ids).map()`` built, is the thing that had to go.

``build_clusters`` takes frames and ``run_stage_4_cluster`` takes files, but
both run the same SQL against the same view names, so there is one
implementation and the two cannot drift apart.

Output ``clusters.parquet``: one row per unit.
"""

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app import duckdb_conn, vocabulary
from app.pipeline.dedupe import label_overlay
from app.pipeline.dedupe import units as units_module
from app.pipeline.dedupe.stage_1_clean import RECORDS_FILENAME
from app.pipeline.dedupe.stage_2_exact import EXACT_GROUPS_FILENAME
from app.pipeline.dedupe.stage_3_score import PAIRS_FILENAME
from app.rules import keys, linkage

CLUSTERS_FILENAME = "clusters.parquet"

STAGE = 4
STAGE_NAME = "cluster"

# The six statuses, in the order that decides which one is the main one.
# `mixed_names` sits just under `too_large` because it asks the same question —
# is this one entity at all? — of a cluster that is small enough to pass the
# size cap and still holds people who are plainly not the same person.
STATUS_ORDER = ("conflict", "too_large", "mixed_names", "weak_link", "mixed_ids",
                "cross_track_ids")
OK = "ok"
CROSS_TRACK = "cross_track_ids"
MIXED_NAMES = "mixed_names"
HELD_KEY = "held_key"
ATTRIBUTE_TIE = "attribute_tie"

TRUSTED_SOURCES = ("human", "import")

# How strongly each kind of link decides, weakest first. One definition, in
# ``app/vocabulary.py``, derived from the ordered provenance list
# (`docs/DESIGN.md` D22): Earlier grouping beats Score. This list used to
# rank Score above Earlier grouping, which contradicted `docs/RULESET.md`,
# so a record whose path held both kinds of edge reported the weaker one.
BASIS_ORDER = vocabulary.BASIS_ORDER

# The view names the SQL uses. Both entry points bind the same three names —
# to registered frames, or to read_parquet over the run's files.
UNITS_VIEW = "s4_units"
MEMBERS_VIEW = "s4_members"
PAIRS_VIEW = "s4_pairs"


def _step(label, progress_callback=None):
    message = f"[{time.strftime('%H:%M:%S')}] {label}"
    print(message, flush=True)
    if progress_callback:
        progress_callback("log", {"message": label})


def gate_settings(settings: dict) -> dict:
    """The four gate limits, with the defaults `docs/ENTITIES.md` names."""
    return {
        "cluster_floor": float(settings.get("cluster_floor", linkage.DEFAULT_CLUSTER_FLOOR)),
        "max_cluster_units": int(
            settings.get("max_cluster_units", linkage.DEFAULT_MAX_CLUSTER_UNITS)
        ),
        "max_existing_ids": int(
            settings.get("max_existing_ids", linkage.DEFAULT_MAX_EXISTING_IDS)
        ),
        # {track: {"column", "count"}}; an empty dict turns the name gate off,
        # which is what every profile that names no column gets.
        "max_distinct_values": linkage.max_distinct_values(settings),
    }


# ---------------------------------------------------------------------------
# Naming the inputs to DuckDB
# ---------------------------------------------------------------------------


def _literal(path) -> str:
    """A path as a SQL string literal. Views cannot carry bound parameters."""
    return "'" + str(path).replace("'", "''") + "'"


def _bind(con, name: str, source) -> None:
    """Name *source* on the connection — a frame is registered, a path is a view."""
    if isinstance(source, pd.DataFrame):
        con.register(f"{name}_frame", source)
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {name}_frame")
        return
    con.execute(
        f"CREATE OR REPLACE VIEW {name} AS "
        f"SELECT * FROM read_parquet({_literal(source)})"
    )


def _columns(con, name: str) -> set:
    return {d[0] for d in con.execute(f"SELECT * FROM {name} LIMIT 0").description}


# ---------------------------------------------------------------------------
# Components — the one step SciPy owns, and it gets integers
# ---------------------------------------------------------------------------


def components(n_units: int, rows, cols) -> np.ndarray:
    """A component label per unit **code**. SciPy does the walking, not Python.

    *rows* and *cols* are integer codes into ``0 .. n_units - 1``, which is what
    the dense-coding in DuckDB produces. Strings are refused outright: at PSC
    scale a string graph means a Python dict of 15 million keys, which is the
    memory this stage exists to stop spending.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if n_units == 0:
        return np.empty(0, dtype="int32")
    rows = np.asarray(rows)
    cols = np.asarray(cols)
    for name, array in (("rows", rows), ("cols", cols)):
        if array.dtype.kind not in "iu":
            raise TypeError(
                f"components() needs integer unit codes; {name} is {array.dtype}. "
                "Dense-code the unit ids in DuckDB first."
            )
    graph = coo_matrix(
        (np.ones(rows.size, dtype="int8"), (rows, cols)),
        shape=(n_units, n_units),
    )
    _count, labels = connected_components(graph, directed=False)
    return labels


def _component_codes(con, sql: str, n_units: int) -> pd.DataFrame:
    """``(code, component)`` for every unit, from an edge query in integer codes."""
    arrays = con.execute(sql).fetchnumpy()
    rows = np.asarray(arrays.get("l", np.empty(0, dtype="int32")))
    cols = np.asarray(arrays.get("r", np.empty(0, dtype="int32")))
    labels = components(n_units, rows, cols)
    return pd.DataFrame({
        "code": np.arange(n_units, dtype="int32"),
        "component": np.asarray(labels, dtype="int32"),
    })


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def _verdict_table(con, applied: pd.DataFrame | None) -> None:
    """``s4_verdicts``: one row per unit pair a human has decided.

    Always a real table with declared types, because an empty frame gives DuckDB
    nothing to infer from.
    """
    con.execute(
        "CREATE OR REPLACE TABLE s4_verdicts"
        "(unit_id_l VARCHAR, unit_id_r VARCHAR, is_match VARCHAR)"
    )
    con.execute(
        "CREATE OR REPLACE TABLE s4_applied"
        "(unit_id_l VARCHAR, unit_id_r VARCHAR, is_match VARCHAR)"
    )
    if applied is None or not len(applied):
        return
    con.register("s4_applied_in", applied[["unit_id_l", "unit_id_r", "is_match"]])
    con.execute(
        "INSERT INTO s4_applied SELECT CAST(unit_id_l AS VARCHAR), "
        "CAST(unit_id_r AS VARCHAR), upper(CAST(is_match AS VARCHAR)) FROM s4_applied_in"
    )
    # The overlay is one verdict per pair, the last label written winning.
    verdicts = label_overlay.decisions(applied)
    if not len(verdicts):
        return
    con.register("s4_verdicts_in", verdicts[["unit_id_l", "unit_id_r", "is_match"]])
    con.execute(
        "INSERT INTO s4_verdicts SELECT CAST(unit_id_l AS VARCHAR), "
        "CAST(unit_id_r AS VARCHAR), upper(CAST(is_match AS VARCHAR)) FROM s4_verdicts_in"
    )


def _import_edges_sql(unit_columns: set) -> str:
    """The star that joins the units an earlier review already gave one id (D11).

    A real group in ``existing_entity_id`` is a trusted merge — a human
    decision, only an older one — so it joins its units whether or not blocking
    happened to produce the pair. A unit whose members carry two or more
    distinct ids says nothing here; its cluster is flagged ``mixed_ids``.

    A star from the smallest unit id, never a clique: some of these groups hold
    over a thousand units, and n-1 edges connect them just as well as n(n-1)/2.
    Within a track, because the tool never merges a person into an organisation.
    """
    label = units_module.LABEL_COLUMN
    if label not in unit_columns:
        return "SELECT NULL AS l, NULL AS r, 'import' AS source, 2 AS ord WHERE FALSE"
    track = "CAST(track AS VARCHAR)" if "track" in unit_columns else "''"
    return f"""
        SELECT l, r, 'import' AS source, 2 AS ord FROM (
            SELECT min(unit_id) OVER (PARTITION BY trk, lbl) AS l, unit_id AS r
            FROM (
                SELECT CAST(unit_id AS VARCHAR) AS unit_id,
                       {track} AS trk,
                       CAST("{label}" AS VARCHAR) AS lbl
                FROM {UNITS_VIEW}
            )
            WHERE lbl IS NOT NULL AND trim(lbl) <> ''
        ) WHERE l <> r
    """


def _scored_edges_sql(pair_columns: set) -> str:
    """The pairs the score, the import overlay and the human labels accepted.

    The human overlay is applied here rather than read off the file, because
    ``pairs.parquet`` holds the score and the import overlay only (slice 3b).
    """
    if not {"bucket", "decided_by"} <= pair_columns:
        return ("SELECT CAST(NULL AS VARCHAR) AS l, CAST(NULL AS VARCHAR) AS r, "
                "CAST(NULL AS VARCHAR) AS source, 0 AS ord WHERE FALSE")
    return f"""
        SELECT l, r, source, 0 AS ord FROM (
            SELECT CAST(p.unit_id_l AS VARCHAR) AS l,
                   CAST(p.unit_id_r AS VARCHAR) AS r,
                   CASE WHEN v.is_match = 'TRUE' THEN 'accept'
                        WHEN v.is_match = 'FALSE' THEN 'reject'
                        ELSE CAST(p.bucket AS VARCHAR) END AS bucket,
                   CASE WHEN v.is_match IS NOT NULL THEN 'human'
                        ELSE CAST(p.decided_by AS VARCHAR) END AS source
            FROM {PAIRS_VIEW} p
            LEFT JOIN s4_verdicts v
                   ON v.unit_id_l = CAST(p.unit_id_l AS VARCHAR)
                  AND v.unit_id_r = CAST(p.unit_id_r AS VARCHAR)
        ) WHERE bucket = 'accept'
    """


# A whole-cluster decision writes a star, and the scorer may never have made
# some of those pairs. The decision is still an accept, so the edge exists
# whatever pairs.parquet holds — otherwise a merge would not take effect until
# the next full run.
_EXTRA_HUMAN_SQL = """
    SELECT v.unit_id_l AS l, v.unit_id_r AS r, 'human' AS source, 1 AS ord
    FROM s4_verdicts v
    WHERE v.is_match = 'TRUE'
      AND NOT EXISTS (SELECT 1 FROM scored s
                      WHERE s.l = v.unit_id_l AND s.r = v.unit_id_r)
"""


def accepted_edges(pairs: pd.DataFrame, applied: pd.DataFrame | None) -> pd.DataFrame:
    """``unit_id_l``, ``unit_id_r``, ``source`` for every pair that joins two units.

    The same SQL the stage runs, over a frame instead of a file. The import
    stars and the human FALSE deletions are not here; they join in
    ``_edge_table``, which is what ``build_clusters`` uses.
    """
    con = duckdb_conn.connect()
    try:
        _bind(con, PAIRS_VIEW, pairs)
        _verdict_table(con, applied)
        return con.execute(f"""
            WITH scored AS ({_scored_edges_sql(_columns(con, PAIRS_VIEW))}),
                 extra AS ({_EXTRA_HUMAN_SQL})
            SELECT l AS unit_id_l, r AS unit_id_r, source FROM (
                SELECT * FROM scored UNION ALL SELECT * FROM extra
            ) ORDER BY ord, l, r
        """).df()
    finally:
        con.close()


def _edge_table(con, unit_columns: set, pair_columns: set) -> None:
    """``s4_edge``: every edge that joins two units, with the source that made it.

    Three sources, in the order that decides a duplicate:

    1. a pair the score, the import overlay and then the human labels accepted;
    2. a human TRUE label the scorer never made a pair for;
    3. an earlier manual group.

    Then every edge a human has said is not a match is deleted, whatever made
    it: a new decision beats an older imported one.
    """
    con.execute(f"""
        CREATE OR REPLACE TABLE s4_edge AS
        WITH scored AS ({_scored_edges_sql(pair_columns)}),
             extra AS ({_EXTRA_HUMAN_SQL}),
             imported AS ({_import_edges_sql(unit_columns)}),
             every_edge AS (
                SELECT * FROM scored
                UNION ALL SELECT * FROM extra
                UNION ALL SELECT * FROM imported
             )
        -- One row per unit pair. The tie-break on `source` never fires in
        -- practice: pairs.parquet holds one row per unit pair, so a repeat can
        -- only come from a different `ord`.
        SELECT l AS unit_id_l, r AS unit_id_r, source FROM (
            SELECT l, r, source,
                   row_number() OVER (PARTITION BY l, r ORDER BY ord, source) AS rn
            FROM every_edge
        ) WHERE rn = 1
    """)
    con.execute("""
        DELETE FROM s4_edge WHERE EXISTS (
            SELECT 1 FROM s4_verdicts v
            WHERE v.is_match = 'FALSE'
              AND v.unit_id_l = s4_edge.unit_id_l
              AND v.unit_id_r = s4_edge.unit_id_r
        )
    """)


def import_edges(units: pd.DataFrame) -> pd.DataFrame:
    """``unit_id_l``, ``unit_id_r``, ``source`` for the earlier manual groups.

    The same SQL the stage runs, over a frame instead of a file.
    """
    con = duckdb_conn.connect()
    try:
        _bind(con, UNITS_VIEW, units)
        frame = con.execute(
            f"SELECT l AS unit_id_l, r AS unit_id_r, source "
            f"FROM ({_import_edges_sql(_columns(con, UNITS_VIEW))})"
        ).df()
    finally:
        con.close()
    return frame


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def gate_limits(per_track: dict) -> list[dict]:
    """Every limit the gate counts, in a stable order, no repeats.

    A track names several — the PSC person track counts cleaned surnames AND
    canonical forenames — and the same limit may be named by more than one
    track and by more than one clause, so it is measured once and everything
    that named it reads the same number.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for groups in per_track.values():
        for group in groups:
            for limit in group:
                if limit["key"] not in seen:
                    seen.add(limit["key"])
                    out.append(limit)
    return out


def gate_columns(per_track: dict) -> list[str]:
    """The name each limit's count comes back under, as ``n_distinct_<key>``.

    One column keeps its own name. Several columns counted as one value join
    with ``+`` — ``dob_year_clean+dob_month_clean`` is one full birth date.
    """
    return [limit["key"] for limit in gate_limits(per_track)]


def _gate_value_sql(unit_columns: set, limit: dict) -> str:
    """One limit's value for a unit, blank read as nothing.

    Several columns are counted as one value, joined on a separator that cannot
    occur in the data. A unit missing any one of them has no such value at all,
    so a filing carrying a birth year but no month is not a second birth date —
    it is a unit this limit cannot judge, and ``count(DISTINCT ...)`` skips it.
    """
    parts = []
    for column in limit["columns"]:
        if column not in unit_columns:
            return "CAST(NULL AS VARCHAR)"
        parts.append(f"""NULLIF(trim(CAST("{column}" AS VARCHAR)), '')""")
    if len(parts) == 1:
        return parts[0]
    missing = " OR ".join(f"({part}) IS NULL" for part in parts)
    joined = " || chr(31) || ".join(f"({part})" for part in parts)
    return f"CASE WHEN {missing} THEN NULL ELSE {joined} END"


def _unit_info(con, unit_columns: set, limits: dict | None = None) -> None:
    """``s4_unit_info``: the five unit columns the stage actually reads.

    The PSC units file has sixty-odd columns and 15 million rows. This is the
    only pass over it, and it takes five — the fifth only when a track names a
    column for the name gate.
    """
    label = units_module.LABEL_COLUMN
    size = "CAST(COALESCE(unit_size, 1) AS BIGINT)" if "unit_size" in unit_columns \
        else "CAST(1 AS BIGINT)"
    track = "CAST(track AS VARCHAR)" if "track" in unit_columns else "CAST(NULL AS VARCHAR)"
    labelled = f'CAST("{label}" AS VARCHAR)' if label in unit_columns \
        else "CAST(NULL AS VARCHAR)"
    gates = gate_limits((limits or {}).get("max_distinct_values") or {})
    gate_select = "".join(
        f", {_gate_value_sql(unit_columns, limit)} AS gate_{index}"
        for index, limit in enumerate(gates)
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE s4_unit_info AS
        SELECT CAST(unit_id AS VARCHAR) AS unit_id, {size} AS unit_size,
               {track} AS track, {labelled} AS label{gate_select}
        FROM {UNITS_VIEW}
    """)
    con.execute("""
        CREATE OR REPLACE TABLE s4_unit_code AS
        SELECT unit_id, CAST(row_number() OVER (ORDER BY unit_id) - 1 AS INTEGER) AS code
        FROM (SELECT DISTINCT unit_id FROM s4_unit_info)
    """)


def _summary(con, limits: dict, pair_columns: set, decisions: dict | None) -> pd.DataFrame:
    """One row per cluster: ``n_units``, ``n_records``, ``n_existing_ids``, statuses.

    Every test is a group-by in SQL. ``weak_link`` reads the pairs whose two
    units are both inside one cluster, which is what makes "the cluster may be a
    chain" measurable without enumerating pairs that were never scored.
    """
    if {"match_probability", "decided_by"} <= pair_columns:
        weak = f"""
            SELECT a.cluster_id AS cluster_id, min(p.match_probability) AS lo
            FROM {PAIRS_VIEW} p
            JOIN s4_unit_cluster a ON a.unit_id = CAST(p.unit_id_l AS VARCHAR)
            JOIN s4_unit_cluster b ON b.unit_id = CAST(p.unit_id_r AS VARCHAR)
            WHERE a.cluster_id = b.cluster_id
              AND p.match_probability IS NOT NULL
              AND (p.decided_by IS NULL OR CAST(p.decided_by AS VARCHAR) <> 'human')
            GROUP BY a.cluster_id
        """
    else:
        weak = "SELECT NULL AS cluster_id, NULL AS lo WHERE FALSE"

    gates = gate_columns(limits.get("max_distinct_values") or {})
    distinct_select = "".join(
        f", count(DISTINCT u.gate_{index}) AS n_{index}" for index in range(len(gates))
    )
    distinct_columns = "".join(
        f"CAST(COALESCE(dv.n_{index}, 0) AS BIGINT) AS \"n_distinct_{gates[index]}\", "
        for index in range(len(gates))
    )
    summary = con.execute(f"""
        WITH per_cluster AS (
            SELECT c.cluster_id AS cluster_id, count(*) AS n_units,
                   CAST(sum(COALESCE(u.unit_size, 1)) AS BIGINT) AS n_records,
                   arg_min(u.track, c.unit_id) AS track
            FROM s4_unit_cluster c
            LEFT JOIN s4_unit_info u ON u.unit_id = c.unit_id
            GROUP BY c.cluster_id
        ),
        mem AS (
            SELECT c.cluster_id AS cluster_id, u.label AS label, u.track AS trk
            FROM {MEMBERS_VIEW} m
            JOIN s4_unit_info u ON u.unit_id = CAST(m.unit_id AS VARCHAR)
            JOIN s4_unit_cluster c ON c.unit_id = CAST(m.unit_id AS VARCHAR)
            WHERE u.label IS NOT NULL
        ),
        ids AS (SELECT cluster_id, count(DISTINCT label) AS n FROM mem GROUP BY cluster_id),
        shared AS (SELECT label FROM mem GROUP BY label HAVING count(DISTINCT trk) > 1),
        crossed AS (
            SELECT DISTINCT cluster_id FROM mem
            WHERE label IN (SELECT label FROM shared)
        ),
        distinct_values AS (
            SELECT c.cluster_id AS cluster_id{distinct_select}
            FROM s4_unit_cluster c
            JOIN s4_unit_info u ON u.unit_id = c.unit_id
            GROUP BY c.cluster_id
        ),
        weak AS ({weak}),
        conflicted AS (
            SELECT DISTINCT a.cluster_id AS cluster_id
            FROM s4_applied x
            JOIN s4_unit_cluster a ON a.unit_id = x.unit_id_l
            JOIN s4_unit_cluster b ON b.unit_id = x.unit_id_r
            WHERE x.is_match = 'FALSE' AND a.cluster_id = b.cluster_id
        )
        SELECT pc.cluster_id, pc.n_units, pc.n_records, pc.track,
               CAST(COALESCE(ids.n, 0) AS BIGINT) AS n_existing_ids,
               {distinct_columns}
               COALESCE(weak.lo < {float(limits['cluster_floor'])}, FALSE) AS weak_link,
               (conflicted.cluster_id IS NOT NULL) AS conflict,
               (crossed.cluster_id IS NOT NULL) AS {CROSS_TRACK}
        FROM per_cluster pc
        LEFT JOIN ids ON ids.cluster_id = pc.cluster_id
        LEFT JOIN distinct_values dv ON dv.cluster_id = pc.cluster_id
        LEFT JOIN weak ON weak.cluster_id = pc.cluster_id
        LEFT JOIN conflicted ON conflicted.cluster_id = pc.cluster_id
        LEFT JOIN crossed ON crossed.cluster_id = pc.cluster_id
        ORDER BY pc.cluster_id
    """).df()

    summary["too_large"] = summary["n_units"] > limits["max_cluster_units"]
    summary["mixed_ids"] = summary["n_existing_ids"] > limits["max_existing_ids"]
    # The gate. A track's entry is a list of clauses; the cluster is held when
    # ANY clause holds, and a clause holds when EVERY limit in it is over its
    # count. A plain single-column clause is a clause of one, so the any-of
    # behaviour this setting has always had is unchanged. A track with no entry
    # is not gated this way at all, and neither is a profile that names none.
    per_track = limits.get("max_distinct_values") or {}
    over = np.zeros(len(summary), dtype=bool)
    tracks = summary["track"].to_numpy()
    for track, groups in per_track.items():
        in_track = tracks == track
        if not in_track.any():
            continue
        for group in groups:
            holds = in_track.copy()
            for limit in group:
                counted = summary[f"n_distinct_{limit['key']}"].to_numpy()
                holds = holds & (counted > limit["count"])
                if not holds.any():
                    break
            over = over | holds
    summary[MIXED_NAMES] = over
    # A cluster of one unit is nothing to gate: there is no merge to doubt.
    alone = (summary["n_units"] < 2).to_numpy()
    for status in STATUS_ORDER:
        summary.loc[alone, status] = False

    # A human who has merged the whole cluster has answered every question the
    # gate asks. A decision always wins, so it is not withheld again — the same
    # rule weak_link already follows for a pair a human has decided.
    merged_scopes = {
        scope for scope, decision in (decisions or {}).items()
        if decision.get("kind") == "merge"
    }
    summary["decided"] = summary["cluster_id"].isin(merged_scopes)
    decided = summary["decided"].to_numpy()
    for status in ("too_large", MIXED_NAMES, "weak_link", "mixed_ids"):
        summary.loc[decided, status] = False

    # The statuses of every cluster at once. The old row-by-row build cost
    # 458,000 Python iterations on the PSC sample alone.
    flags = {name: summary[name].to_numpy(dtype=bool) for name in STATUS_ORDER}
    n = len(summary)
    statuses = np.full(n, "", dtype=object)
    for name in STATUS_ORDER:
        hit = flags[name]
        statuses[hit] = np.where(statuses[hit] == "", name, statuses[hit] + "|" + name)
    status = np.full(n, OK, dtype=object)
    for name in reversed(STATUS_ORDER):       # the first in STATUS_ORDER wins
        status[flags[name]] = name
    summary["statuses"] = statuses
    summary["status"] = status
    summary["withheld"] = summary["status"] != OK
    return summary[[
        "cluster_id", "n_units", "n_records", "track", "n_existing_ids",
        *[f"n_distinct_{column}" for column in gates],
        "weak_link", "conflict", CROSS_TRACK, "too_large",
        MIXED_NAMES, "mixed_ids", "decided", "statuses", "status", "withheld",
    ]]


# ---------------------------------------------------------------------------
# Withholding
# ---------------------------------------------------------------------------


def _parts(con, summary: pd.DataFrame, n_units: int) -> None:
    """``s4_unit_part``: the proposed entity key of every unit.

    A cluster that passed the gate is proposed whole. A withheld one is rebuilt
    from the trusted edges alone — the import and human ones — so a human
    decision is never withheld, and each part it leaves becomes an entity.
    """
    withheld = summary.loc[summary["withheld"], "cluster_id"]
    if not len(withheld):
        con.execute(
            "CREATE OR REPLACE TABLE s4_unit_part AS "
            "SELECT unit_id, whole_key AS part FROM s4_unit_cluster"
        )
        return

    con.register("s4_withheld_in", pd.DataFrame({"cluster_id": withheld.to_numpy()}))
    con.execute("CREATE OR REPLACE TABLE s4_withheld AS "
                "SELECT CAST(cluster_id AS VARCHAR) AS cluster_id FROM s4_withheld_in")
    trusted = ", ".join(f"'{source}'" for source in TRUSTED_SOURCES)
    codes = _component_codes(con, f"""
        SELECT a.code AS l, b.code AS r
        FROM s4_edge e
        JOIN s4_unit_cluster a ON a.unit_id = e.unit_id_l
        JOIN s4_unit_cluster b ON b.unit_id = e.unit_id_r
        WHERE e.source IN ({trusted})
          AND a.cluster_id IN (SELECT cluster_id FROM s4_withheld)
          AND b.cluster_id IN (SELECT cluster_id FROM s4_withheld)
    """, n_units)
    con.register("s4_rebuilt_in", codes)
    con.execute("""
        CREATE OR REPLACE TABLE s4_unit_part AS
        WITH j AS (
            SELECT u.unit_id, r.component
            FROM s4_unit_code u JOIN s4_rebuilt_in r ON r.code = u.code
        ),
        smallest AS (SELECT component, min(unit_id) AS part FROM j GROUP BY component)
        SELECT c.unit_id,
               CASE WHEN c.cluster_id IN (SELECT cluster_id FROM s4_withheld)
                    THEN s.part ELSE c.whole_key END AS part
        FROM s4_unit_cluster c
        JOIN j ON j.unit_id = c.unit_id
        JOIN smallest s ON s.component = j.component
    """)


def _part_source(con) -> None:
    """``s4_part_source``: the strongest accepted edge inside each proposed part.

    The ranks come from ``app/vocabulary.BASIS_ORDER`` and are unique over the
    sources an accepted edge can carry — ``score``, ``import`` and ``human`` —
    so the ``source DESC`` tie-break never decides anything; it is there to keep
    the answer deterministic. ``import`` outranks ``score`` (`docs/DESIGN.md`
    D22): a merge the earlier grouping already made is stronger evidence than a
    score, so a part joined by both reports Earlier grouping.
    """
    ranks = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in BASIS_ORDER.items())
    con.execute(f"""
        CREATE OR REPLACE TABLE s4_part_source AS
        SELECT part, source FROM (
            SELECT a.part AS part, e.source AS source,
                   row_number() OVER (
                       PARTITION BY a.part
                       ORDER BY CASE e.source {ranks} ELSE 0 END DESC, e.source DESC
                   ) AS rn
            FROM s4_edge e
            JOIN s4_unit_part a ON a.unit_id = e.unit_id_l
            JOIN s4_unit_part b ON b.unit_id = e.unit_id_r
            WHERE a.part = b.part
        ) WHERE rn = 1
    """)


CLUSTERS_SELECT = """
    SELECT c.unit_id, c.cluster_id, u.track AS track, p.part AS proposed_entity_key,
           s.status, s.statuses, s.withheld, ps.source AS edge_source
    FROM s4_unit_cluster c
    JOIN s4_unit_part p ON p.unit_id = c.unit_id
    LEFT JOIN s4_unit_info u ON u.unit_id = c.unit_id
    LEFT JOIN s4_summary s ON s.cluster_id = c.cluster_id
    LEFT JOIN s4_part_source ps ON ps.part = p.part
    ORDER BY c.unit_id
"""


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def _cluster(con, settings: dict, applied: pd.DataFrame | None,
             decisions: dict | None) -> pd.DataFrame:
    """Everything from the bound views to ``s4_summary``, in SQL. Returns it."""
    limits = gate_settings(settings)
    unit_columns = _columns(con, UNITS_VIEW)
    pair_columns = _columns(con, PAIRS_VIEW)

    _verdict_table(con, applied)
    _unit_info(con, unit_columns, limits)
    _edge_table(con, unit_columns, pair_columns)

    n_units = int(con.execute("SELECT count(*) FROM s4_unit_code").fetchone()[0])
    codes = _component_codes(con, """
        SELECT a.code AS l, b.code AS r
        FROM s4_edge e
        JOIN s4_unit_code a ON a.unit_id = e.unit_id_l
        JOIN s4_unit_code b ON b.unit_id = e.unit_id_r
    """, n_units)
    con.register("s4_comp_in", codes)
    con.execute("""
        CREATE OR REPLACE TABLE s4_unit_cluster AS
        WITH j AS (
            SELECT u.unit_id, u.code, c.component
            FROM s4_unit_code u JOIN s4_comp_in c ON c.code = u.code
        ),
        smallest AS (SELECT component, min(unit_id) AS whole_key FROM j GROUP BY component)
        SELECT j.unit_id, j.code, 'C-' || s.whole_key AS cluster_id, s.whole_key
        FROM j JOIN smallest s ON s.component = j.component
    """)

    summary = _summary(con, limits, pair_columns, decisions)
    con.register("s4_summary_in", summary[["cluster_id", "status", "statuses", "withheld"]])
    con.execute("CREATE OR REPLACE VIEW s4_summary AS SELECT * FROM s4_summary_in")
    _parts(con, summary, n_units)
    _part_source(con)
    return summary


def build_clusters(
    units: pd.DataFrame,
    members: pd.DataFrame,
    pairs: pd.DataFrame,
    settings: dict,
    applied: pd.DataFrame | None = None,
    decisions: dict | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(clusters, cluster_summary)`` — one row per unit, and one per cluster.

    The in-memory entry point: the same SQL as the stage, over frames. Used by
    the tests and by anything that already holds the three frames.
    """
    con = duckdb_conn.connect()
    try:
        _bind(con, UNITS_VIEW, units)
        _bind(con, MEMBERS_VIEW, members)
        _bind(con, PAIRS_VIEW, pairs)
        summary = _cluster(con, settings, applied, decisions)
        clusters = con.execute(CLUSTERS_SELECT).df()
    finally:
        con.close()
    return clusters, summary


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


def _held_groups_from_file(con, path: Path) -> pd.DataFrame:
    """The same, as one aggregate over the parquet. One row per held group."""
    if not Path(path).is_file():
        return pd.DataFrame(columns=["group_id", "track", "n_records"])
    return con.execute(f"""
        SELECT group_id, min(CAST(track AS VARCHAR)) AS track,
               CAST(count(*) AS BIGINT) AS n_records
        FROM read_parquet({_literal(path)})
        WHERE status = '{keys.HELD}'
        GROUP BY group_id ORDER BY group_id
    """).df()


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


def _applied_labels(con, run_dir: Path, labels: pd.DataFrame) -> pd.DataFrame | None:
    """The human labels as decisions about this run's unit pairs.

    Only the members and groups of the records the labels actually name are read
    — a label set is thousands of rows against sixteen million members, and the
    overlay needs no more than the units those records now sit in.
    """
    if labels is None or not len(labels):
        return None
    named = pd.unique(pd.concat([
        labels["record_id_a"].astype(str), labels["record_id_b"].astype(str),
    ], ignore_index=True))
    wanted = list(named)
    members = con.execute(
        f"SELECT CAST(unit_id AS VARCHAR) AS unit_id, "
        f"CAST(record_id AS VARCHAR) AS record_id FROM {MEMBERS_VIEW} "
        f"WHERE CAST(record_id AS VARCHAR) IN (SELECT UNNEST(?))", [wanted],
    ).df()
    groups_path = run_dir / EXACT_GROUPS_FILENAME
    if groups_path.is_file():
        groups = con.execute(
            f"SELECT * FROM read_parquet({_literal(groups_path)}) "
            f"WHERE CAST(record_id AS VARCHAR) IN (SELECT UNNEST(?))", [wanted],
        ).df()
    else:
        groups = pd.DataFrame(columns=["record_id", "group_id", "status", "key_ids"])
    return label_overlay.outcomes(labels, members, groups)["applied"]


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
    temp_dir = run_dir / "duckdb_tmp"
    # A killed run leaves its spill behind; clear it before adding to it.
    duckdb_conn.clear_spill(temp_dir)
    con = duckdb_conn.connect(temp_dir)
    try:
        _bind(con, UNITS_VIEW, run_dir / units_module.UNITS_FILENAME)
        _bind(con, MEMBERS_VIEW, run_dir / units_module.UNIT_MEMBERS_FILENAME)
        _bind(con, PAIRS_VIEW, run_dir / PAIRS_FILENAME)

        applied = _applied_labels(con, run_dir, labels)
        n_units = int(con.execute(f"SELECT count(*) FROM {UNITS_VIEW}").fetchone()[0])
        _step(f"Clustering {n_units:,} units...", progress_callback)

        summary = _cluster(con, settings, applied, decisions)
        out = run_dir / CLUSTERS_FILENAME
        con.execute(
            f"COPY ({CLUSTERS_SELECT}) TO {_literal(out)} (FORMAT PARQUET)"
        )
        held = _held_groups_from_file(con, run_dir / EXACT_GROUPS_FILENAME)
    finally:
        con.close()
        duckdb_conn.clear_spill(temp_dir)

    # The queue's index. Every list reader used to rebuild its whole-run
    # aggregate on every request, which is what ran out of memory at PSC scale
    # (`docs/PSC_HANDOVER.md` section 107, item 6). The queue's own SQL writes
    # it, so there is still one definition of a queue row.
    with_index = time.time()
    from app.services import clusters_reader

    clusters_reader.write_index(run_dir)
    _step(f"  Cluster index written in {time.time() - with_index:.1f}s",
          progress_callback)

    counts = counts_from(None, summary, held, decisions)
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

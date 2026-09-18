/* ============================================================
   EntitiesTable — the entity IDs this run proposes
   ------------------------------------------------------------
   One row per proposed entity. Nothing here is written to the
   durable register until the run is published, so every ID on this
   tab is a proposal. The ID status says where the ID came from, and
   the basis says what joined the records in the first place.
   ============================================================ */

import { useState, useEffect, Fragment } from "react";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber } from "./ProbBar";
import { Empty } from "./Empty";
import { Cell, NUMERIC_TYPES } from "./cells";

const PER_PAGE = 50;

// How the records of an entity came to be together.
const BASES = [
  { key: "single", label: "Single", help: "One record on its own. Nothing was merged." },
  { key: "exact_key", label: "Exact key", help: "A match key merged the records." },
  {
    key: "import",
    label: "Imported label",
    help: "The records already carried the same ID from an earlier grouping.",
  },
  { key: "score", label: "Score", help: "The scorer accepted a pair and joined them." },
  { key: "human", label: "Human", help: "A reviewer said these records are the same." },
];

// Where the entity's ID came from.
const ID_STATUSES = [
  { key: "new", label: "New", tag: "", help: "Nothing in the register claimed these records." },
  { key: "kept", label: "Kept", tag: "green", help: "One register entity already held them, so its ID stands." },
  {
    key: "survivor",
    label: "Survivor",
    tag: "blue",
    help: "Several register entities held them. This ID survives and the others become aliases.",
  },
  {
    key: "minted_after_collision",
    label: "Re-minted",
    tag: "amber",
    help: "Two proposed entities claimed the same earlier ID. This one gave way and took a new ID.",
  },
];

const ATTRIBUTE_BASIS = {
  rule: { label: "Rule", help: "A derived-column rule set this value, which beats a raw value." },
  majority: { label: "Majority", help: "The most common value among the records." },
  raw: { label: "Raw", help: "One record, so its own value stands." },
  tie: { label: "Tie", help: "Two values were equally common, so each record keeps its own." },
};

export default function EntitiesTable({ runId, profile }) {
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [track, setTrack] = useState("all");
  const [basis, setBasis] = useState("all");
  const [idStatus, setIdStatus] = useState("all");
  const [minSize, setMinSize] = useState(1);
  const [sort, setSort] = useState("size");
  const [order, setOrder] = useState("desc");
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState(null);

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);

  const tracks = profile.tracks || [];
  const displayColumns = profile.display_columns || [];
  const priorityKey = (profile.priority_columns || [])[0];
  const priorityColumn = displayColumns.find((c) => c.key === priorityKey);

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(query.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const params = { offset: page * PER_PAGE, limit: PER_PAGE, sort, order };
    if (q) params.q = q;
    if (track !== "all") params.track = track;
    if (basis !== "all") params.basis = basis;
    if (idStatus !== "all") params.id_status = idStatus;
    if (minSize > 1) params.min_size = minSize;
    api
      .getRunEntities(runId, params)
      .then((res) => {
        if (!alive) return;
        setData(res);
        setError(null);
      })
      .catch((err) => {
        if (!alive) return;
        setData(null);
        setError(err.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId, q, track, basis, idStatus, minSize, sort, order, page, attempt]);

  const items = data?.items || [];
  const counts = data?.counts || {};
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const attributeKeys = [
    ...new Set(items.flatMap((e) => Object.keys(e.attributes || {}))),
  ];

  function pick(setter, value) {
    setter(value);
    setPage(0);
    setOpen(null);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="search" style={{ width: 250 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search names, record or entity IDs..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        {tracks.length > 1 && (
          <div className="seg">
            <button className={track === "all" ? "on" : ""} onClick={() => pick(setTrack, "all")}>
              All tracks
            </button>
            {tracks.map((t) => (
              <button
                key={t.key}
                className={track === t.key ? "on" : ""}
                onClick={() => pick(setTrack, t.key)}
              >
                {t.label}
                {counts[t.key] != null && (
                  <span className="muted" style={{ fontSize: 11 }}>
                    &middot; {fmtNumber(counts[t.key])}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}

        <div className="seg" title="What joined the records">
          <button className={basis === "all" ? "on" : ""} onClick={() => pick(setBasis, "all")}>
            Any basis
          </button>
          {BASES.map((b) => (
            <button
              key={b.key}
              className={basis === b.key ? "on" : ""}
              title={b.help}
              onClick={() => pick(setBasis, b.key)}
            >
              {b.label}
              {counts[b.key] != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts[b.key])}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="seg" title="Where the ID came from">
          <button className={idStatus === "all" ? "on" : ""} onClick={() => pick(setIdStatus, "all")}>
            Any ID
          </button>
          {ID_STATUSES.map((s) => (
            <button
              key={s.key}
              className={idStatus === s.key ? "on" : ""}
              title={s.help}
              onClick={() => pick(setIdStatus, s.key)}
            >
              {s.label}
              {counts[s.key] != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts[s.key])}
                </span>
              )}
            </button>
          ))}
        </div>

        <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5 }}>
          <span className="muted">at least</span>
          <input
            className="input mono"
            type="number"
            min="1"
            style={{ width: 66 }}
            value={minSize}
            onChange={(e) => pick(setMinSize, Math.max(1, +e.target.value || 1))}
          />
          <span className="muted">records</span>
        </label>

        <select
          className="select"
          style={{ width: 190 }}
          value={`${sort}:${order}`}
          onChange={(e) => {
            const [s, o] = e.target.value.split(":");
            setSort(s);
            setOrder(o);
            setPage(0);
          }}
        >
          <option value="size:desc">Most records first</option>
          {priorityColumn && (
            <option value="priority:desc">{priorityColumn.label}, highest first</option>
          )}
          <option value="name:asc">Name, A to Z</option>
          <option value="entity_id:asc">Entity ID</option>
        </select>

        <div className="spacer" />
        <button className="btn" onClick={() => setAttempt((n) => n + 1)}>
          <Icons.refresh size={14} />
          Refresh
        </button>
      </div>

      <p className="muted" style={{ fontSize: 12, margin: 0 }}>
        These IDs are a proposal. Nothing is written to the durable register until you publish the
        run on the <strong>Publish &amp; export</strong> tab.
      </p>

      {loading && !data ? (
        <p className="muted pulse" style={{ padding: 40 }}>
          Loading entities...
        </p>
      ) : error ? (
        <Empty
          title="Failed to load entities"
          sub={error}
          action={
            <button className="btn primary" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </button>
          }
        />
      ) : items.length === 0 ? (
        <Empty title="No entities match" sub="Clear the filters or lower the record count." />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th style={{ width: 130 }}>Entity ID</th>
                  <th style={{ minWidth: 220 }}>Names</th>
                  <th style={{ width: 80, textAlign: "right" }}>Records</th>
                  <th style={{ width: 110 }}>Track</th>
                  <th style={{ minWidth: 150 }}>Joined by</th>
                  {attributeKeys.map((k) => (
                    <th key={k} style={{ minWidth: 150 }}>
                      {k}
                    </th>
                  ))}
                  {priorityColumn && (
                    <th style={{ width: 120, textAlign: "right" }}>{priorityColumn.label}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {items.map((e) => {
                  const isOpen = open === e.entity_id;
                  const idMeta = ID_STATUSES.find((s) => s.key === e.id_status);
                  const bases = Object.entries(e.bases || {});
                  return (
                    <Fragment key={e.entity_id}>
                      <tr
                        className={"sortable" + (isOpen ? " selected" : "")}
                        style={{ cursor: "pointer" }}
                        onClick={() => setOpen(isOpen ? null : e.entity_id)}
                      >
                        <td style={{ verticalAlign: "top" }}>
                          <span className="mono" style={{ fontWeight: 600 }}>
                            {e.entity_id}
                          </span>
                          {idMeta && (
                            <div style={{ marginTop: 2 }}>
                              <span className={"tag " + idMeta.tag} title={idMeta.help}>
                                {idMeta.label}
                              </span>
                            </div>
                          )}
                        </td>
                        <td style={{ whiteSpace: "normal", overflowWrap: "anywhere", verticalAlign: "top" }}>
                          {(e.names || []).join(" · ")}
                          {(e.existing_entity_ids || []).length > 0 && (
                            <div className="mono muted" style={{ fontSize: 11 }}>
                              earlier: {e.existing_entity_ids.join(", ")}
                            </div>
                          )}
                        </td>
                        <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                          {fmtNumber(e.n_records)}
                          <div className="muted" style={{ fontSize: 11 }}>
                            {fmtNumber(e.n_units)} unit{e.n_units === 1 ? "" : "s"}
                          </div>
                        </td>
                        <td style={{ verticalAlign: "top" }}>
                          <span className="tag">
                            {tracks.find((t) => t.key === e.track)?.label || e.track}
                          </span>
                        </td>
                        <td style={{ whiteSpace: "normal", verticalAlign: "top" }}>
                          {bases.length === 0 ? (
                            <span className="muted">—</span>
                          ) : (
                            bases.map(([k, n]) => {
                              const meta = BASES.find((b) => b.key === k);
                              return (
                                <span
                                  key={k}
                                  className="tag"
                                  title={meta?.help}
                                  style={{ marginRight: 4 }}
                                >
                                  {meta?.label || k}
                                  <span className="muted" style={{ marginLeft: 4 }}>
                                    {fmtNumber(n)}
                                  </span>
                                </span>
                              );
                            })
                          )}
                        </td>
                        {attributeKeys.map((k) => {
                          const a = (e.attributes || {})[k];
                          const meta = a && ATTRIBUTE_BASIS[a.basis];
                          return (
                            <td key={k} style={{ whiteSpace: "normal", verticalAlign: "top" }}>
                              {a ? (
                                <>
                                  {a.value ?? <span className="muted">—</span>}
                                  {meta && (
                                    <div style={{ marginTop: 2 }}>
                                      <span
                                        className={"tag" + (a.basis === "tie" ? " amber" : "")}
                                        title={meta.help}
                                      >
                                        {meta.label}
                                      </span>
                                    </div>
                                  )}
                                </>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                          );
                        })}
                        {priorityColumn && (
                          <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                            <Cell
                              value={e.priority ? e.priority[priorityColumn.key] : null}
                              type={priorityColumn.type}
                            />
                          </td>
                        )}
                      </tr>
                      {isOpen && (
                        <tr>
                          <td
                            colSpan={5 + attributeKeys.length + (priorityColumn ? 1 : 0)}
                            style={{ whiteSpace: "normal" }}
                          >
                            <EntityMembers
                              runId={runId}
                              entityId={e.entity_id}
                              columns={displayColumns}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              Showing {fmtNumber(page * PER_PAGE + 1)}&ndash;{fmtNumber(page * PER_PAGE + items.length)}{" "}
              of {fmtNumber(total)}
            </span>
            {totalPages > 1 && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  Page {page + 1} of {fmtNumber(totalPages)}
                </span>
                <button className="btn sm" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
                  &larr; Prev
                </button>
                <button
                  className="btn sm"
                  disabled={page + 1 >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next &rarr;
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// The records inside one entity, with the earlier ID beside the new one.
function EntityMembers({ runId, entityId, columns }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    api
      .getRunEntity(runId, entityId)
      .then((res) => {
        if (alive) {
          setDetail(res);
          setError(null);
        }
      })
      .catch((err) => {
        if (alive) setError(err.message);
      });
    return () => {
      alive = false;
    };
  }, [runId, entityId]);

  if (error) return <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error}</p>;
  if (!detail)
    return (
      <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
        Loading the records...
      </p>
    );

  const members = detail.members || [];
  const dataColumns = columns.filter((c) => c.key !== "existing_entity_id");

  return (
    <div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              {dataColumns.map((c) => (
                <th
                  key={c.key}
                  style={{ textAlign: NUMERIC_TYPES.has(c.type) ? "right" : "left", whiteSpace: "nowrap" }}
                >
                  {c.label}
                </th>
              ))}
              <th style={{ width: 130 }}>Earlier ID</th>
              <th style={{ width: 130 }}>New ID</th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const earlier = m.existing_entity_id;
              const changed = earlier && String(earlier) !== String(detail.entity_id);
              return (
                <tr key={m.record_id}>
                  {dataColumns.map((c, i) => {
                    const numeric = NUMERIC_TYPES.has(c.type);
                    return (
                      <td
                        key={c.key}
                        className={numeric ? "mono tnum" : ""}
                        style={{ textAlign: numeric ? "right" : "left", whiteSpace: "normal" }}
                      >
                        <Cell value={m[c.key]} type={c.type} />
                        {i === 0 && (
                          <div className="mono muted" style={{ fontSize: 11 }}>
                            {m.record_id}
                          </div>
                        )}
                      </td>
                    );
                  })}
                  <td>
                    {earlier ? (
                      <span
                        className={"tag" + (changed ? " amber" : "")}
                        style={{ fontFamily: "var(--font-mono)", textTransform: "none" }}
                        title={changed ? "This record moves to a different ID" : undefined}
                      >
                        {earlier}
                      </span>
                    ) : (
                      <span className="muted" style={{ fontSize: 12 }}>
                        never reviewed
                      </span>
                    )}
                  </td>
                  <td>
                    <span
                      className="tag green"
                      style={{ fontFamily: "var(--font-mono)", textTransform: "none" }}
                    >
                      {detail.entity_id}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {detail.members_truncated && (
        <p className="muted" style={{ fontSize: 11.5, margin: "6px 0 0" }}>
          Only the first {members.length} records are shown.
        </p>
      )}
    </div>
  );
}

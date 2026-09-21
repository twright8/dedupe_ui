/* ============================================================
   ExactGroupsTable — what the match keys made, one row each
   ------------------------------------------------------------
   An EXACT GROUP is a set of records a match key put together. A
   HELD GROUP is a set of records a match key would have put
   together, stopped by a guard, waiting for a person. Agreement
   says how the group sits against the earlier grouping, in the
   glossary's own words. Filtering, sorting and paging happen on the
   server; this component holds the controls' state.
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber } from "./ProbBar";
import { Empty } from "./Empty";
import { Cell, NUMERIC_TYPES } from "./cells";
import { Term, TermHint, Provenance, provenanceLabel } from "./Term";
import { noun, existingLabelName } from "../profileText";

const PER_PAGE = 50;

// The API values the agreement filter offers. Every word beside them comes
// from the glossary.
const AGREEMENT_VALUES = ["consistent", "conflict", "extends", "new"];

export function AgreementTag({ value }) {
  if (!value) return <span className="muted">—</span>;
  return <Provenance kind="agreement" value={value} size="sm" />;
}

/* A guard string in plain words, for an answer that carries no `guard_text`.
   The API now builds that sentence itself and it is always preferred; this is
   the fallback, and it is what an older answer gets. */
export function guardReason(guard) {
  const raw = String(guard || "").trim();
  if (!raw) return "";

  let m = raw.match(/^max_group_size=(\d+)>(\d+)$/);
  if (m) return `${fmtNumber(+m[1])} records, limit ${fmtNumber(+m[2])}`;

  m = raw.match(/^max_distinct:([^=]+)=(\d+)>(\d+)$/);
  if (m) return `${fmtNumber(+m[2])} different values in ${m[1]}, limit ${fmtNumber(+m[3])}`;

  m = raw.match(/^require_any_equal[:=](.+)$/);
  if (m) return `no shared value in ${m[1].split(",").join(", ")}`;

  m = raw.match(/^blocklist[:=](.+)$/);
  if (m) return `the key value is listed in ${m[1]}`;

  return raw;
}

// ---------- expanded members ----------
function GroupMembers({ runId, groupId, columns, existingIds, recordPlural }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    api
      .getRunExactGroup(runId, groupId)
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
  }, [runId, groupId]);

  if (error) {
    return (
      <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error}</p>
    );
  }
  if (!detail) {
    return (
      <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
        Loading the {recordPlural} in this group...
      </p>
    );
  }

  const members = Array.isArray(detail.members) ? detail.members : [];
  // More than one earlier ID in a group is exactly what a conflict looks like,
  // so that column is the thing to make obvious. It gets its own last column,
  // which means dropping the profile's own copy of it.
  const distinctIds = new Set(
    members.map((r) => r.existing_entity_id).filter(Boolean)
  );
  const conflicting = distinctIds.size > 1;
  const dataColumns = columns.filter((c) => c.key !== "existing_entity_id");

  return (
    <div>
      <div className="tbl-wrap" style={{ marginBottom: 8 }}>
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              {dataColumns.map((col) => (
                <th
                  key={col.key}
                  style={{
                    textAlign: NUMERIC_TYPES.has(col.type) ? "right" : "left",
                    whiteSpace: "nowrap",
                  }}
                >
                  {col.label}
                </th>
              ))}
              <th style={{ width: 170 }}>Earlier ID</th>
            </tr>
          </thead>
          <tbody>
            {members.map((r) => (
              <tr key={r.record_id}>
                {dataColumns.map((col, i) => {
                  const numeric = NUMERIC_TYPES.has(col.type);
                  return (
                    <td
                      key={col.key}
                      className={numeric ? "mono tnum" : ""}
                      style={{ textAlign: numeric ? "right" : "left" }}
                    >
                      <Cell value={r[col.key]} type={col.type} />
                      {i === 0 && (
                        <div className="mono muted" style={{ fontSize: 11 }}>
                          {r.record_id}
                        </div>
                      )}
                    </td>
                  );
                })}
                <td>
                  {r.existing_entity_id ? (
                    <span
                      className={"tag" + (conflicting ? " red" : " green")}
                      style={{ fontFamily: "var(--font-mono)", textTransform: "none" }}
                    >
                      {r.existing_entity_id}
                    </span>
                  ) : (
                    <span className="muted" style={{ fontSize: 12 }}>
                      no earlier ID
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail.members_truncated && (
        <p className="muted" style={{ fontSize: 11.5, margin: 0 }}>
          Only the first {members.length} {recordPlural} of this group are shown.
        </p>
      )}
      {conflicting && (
        <p style={{ fontSize: 12, color: "var(--ti-red)", margin: "4px 0 0" }}>
          This group joins {distinctIds.size} different earlier IDs
          {existingIds && existingIds.length ? `: ${existingIds.join(", ")}` : ""}.
        </p>
      )}
    </div>
  );
}

// ---------- main table ----------
export default function ExactGroupsTable({ runId, profile, initialAgreement }) {
  const tracks = profile.tracks || [];
  const recordPlural = noun(profile, "record_plural");
  const recordPluralCap = recordPlural.charAt(0).toUpperCase() + recordPlural.slice(1);
  const earlier = existingLabelName(profile) || "earlier grouping";
  const displayColumns =
    profile.display_columns && profile.display_columns.length
      ? profile.display_columns
      : [{ key: "name", label: "Name", type: "text" }];
  const priorityKey = (profile.priority_columns || [])[0];
  const priorityColumn = displayColumns.find((c) => c.key === priorityKey);

  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [track, setTrack] = useState("all");
  const [status, setStatus] = useState("all");
  const [agreement, setAgreement] = useState(initialAgreement || "all");
  const [keyId, setKeyId] = useState("all");
  const [sort, setSort] = useState("size");
  const [order, setOrder] = useState("desc");
  const [page, setPage] = useState(0);
  const [openGroup, setOpenGroup] = useState(null);

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);

  // The keys this run used, for the filter and the Keys column. The same
  // response carries per-track figures, which give the track chips their counts
  // when the groups endpoint does not report them.
  const [runKeys, setRunKeys] = useState([]);
  const [evalTracks, setEvalTracks] = useState({});
  useEffect(() => {
    let alive = true;
    api
      .getRunExactEval(runId)
      .then((d) => {
        if (!alive) return;
        setRunKeys((d?.keys || []).map((k) => ({ id: k.id, name: k.name || k.id })));
        setEvalTracks(d?.by_track && typeof d.by_track === "object" ? d.by_track : {});
      })
      .catch(() => {
        if (!alive) return;
        setRunKeys([]);
        setEvalTracks({});
      });
    return () => {
      alive = false;
    };
  }, [runId]);

  // Per-key group counts cannot simply be summed — keys that share a record are
  // united into one group — so the count only appears when the evaluation
  // reports it for the track outright.
  function trackCount(key) {
    const t = evalTracks[key];
    if (!t || typeof t !== "object") return null;
    const n = t.merged_groups ?? t.groups ?? null;
    return typeof n === "number" ? n : null;
  }

  // A deep link from the summary lands here already filtered.
  useEffect(() => {
    if (initialAgreement) {
      setAgreement(initialAgreement);
      setPage(0);
    }
  }, [initialAgreement]);

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
    if (status !== "all") params.status = status;
    if (agreement !== "all") params.agreement = agreement;
    if (keyId !== "all") params.key = keyId;
    api
      .getRunExactGroups(runId, params)
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
  }, [runId, q, track, status, agreement, keyId, sort, order, page, attempt]);

  function choose(setter, value) {
    setter(value);
    setPage(0);
    setOpenGroup(null);
  }

  const items = data?.items || [];
  const counts = data?.counts || {};
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const firstShown = total === 0 ? 0 : page * PER_PAGE + 1;
  const lastShown = page * PER_PAGE + items.length;

  // The key filter lists every key the run used, by name. Taking them from the
  // rows on screen would drop a key that only appears on a later page.
  const keyOptions = runKeys.length
    ? runKeys
    : Array.from(
        new Set(items.flatMap((g) => (Array.isArray(g.key_ids) ? g.key_ids : [])))
      )
        .sort()
        .map((id) => ({ id, name: id }));
  const keyName = (id) => keyOptions.find((k) => k.id === id)?.name || id;

  function trackLabel(key) {
    const t = tracks.find((x) => x.key === key);
    return t ? t.label : key || "—";
  }

  const filtersOn =
    !!q || track !== "all" || status !== "all" || agreement !== "all" || keyId !== "all";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div className="search" style={{ width: 260 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search names..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <div className="seg" title="Filter by track">
          <button className={track === "all" ? "on" : ""} onClick={() => choose(setTrack, "all")}>
            All
          </button>
          {tracks.map((t) => (
            <button
              key={t.key}
              className={track === t.key ? "on" : ""}
              onClick={() => choose(setTrack, t.key)}
            >
              {t.label}
              {(counts[t.key] ?? trackCount(t.key)) != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts[t.key] ?? trackCount(t.key))}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="seg" title="Merged by a match key, or held back by a guard">
          {[
            ["all", "All", null],
            ["merged", "Exact groups", counts.merged],
            ["held", "Held groups", counts.held],
          ].map(([id, label, n]) => (
            <button
              key={id}
              className={status === id ? "on" : ""}
              onClick={() => choose(setStatus, id)}
            >
              {label}
              {n != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(n)}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="seg" title={`Against the ${earlier}`}>
          <button
            className={agreement === "all" ? "on" : ""}
            onClick={() => choose(setAgreement, "all")}
          >
            All
          </button>
          {AGREEMENT_VALUES.map((value) => (
            <button
              key={value}
              className={agreement === value ? "on" : ""}
              onClick={() => choose(setAgreement, value)}
            >
              <span
                style={value === "conflict" && counts.conflict > 0 ? { color: "var(--ti-red)" } : undefined}
              >
                {provenanceLabel("agreement", value)}
              </span>
              {counts[value] != null && (
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts[value])}
                </span>
              )}
            </button>
          ))}
        </div>

        {keyOptions.length > 1 && (
          <select
            className="select"
            style={{ width: 190 }}
            value={keyId}
            onChange={(e) => choose(setKeyId, e.target.value)}
            title="Filter by the match key that made the group"
          >
            <option value="all">Any match key</option>
            {keyOptions.map((k) => (
              <option key={k.id} value={k.id}>
                {k.name}
              </option>
            ))}
          </select>
        )}

        <select
          className="select"
          style={{ width: 190 }}
          value={sort}
          onChange={(e) => {
            setSort(e.target.value);
            setOrder(e.target.value === "name" ? "asc" : "desc");
            setPage(0);
          }}
        >
          <option value="size">Largest first</option>
          {priorityColumn && <option value="priority">By {priorityColumn.label}</option>}
          <option value="name">By name</option>
        </select>

        <div className="spacer" />
        <button className="btn" onClick={() => setAttempt((n) => n + 1)}>
          <Icons.refresh size={14} />
          Refresh
        </button>
      </div>

      <p className="muted" style={{ fontSize: 12, margin: 0 }}>
        An <Term name="exactGroup" /> is a set of {recordPlural} a <Term name="matchKey" /> put
        together. A <Term name="heldGroup" /> is a set a match key would have put together, stopped
        by a <Term name="guard" />, waiting for a person. Nothing here is scored.
      </p>

      {loading ? (
        <p className="muted pulse" style={{ fontSize: 13.5, padding: 40 }}>
          Loading exact groups...
        </p>
      ) : error ? (
        <Empty
          title="Could not load the exact groups"
          sub={error}
          action={
            <button className="btn primary" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </button>
          }
        />
      ) : items.length === 0 ? (
        <Empty
          title="No exact groups found"
          sub={
            filtersOn
              ? "No exact group matches these filters. Clear the search or pick another track."
              : "The match keys merged nothing in this run. Check the match keys on the Config & rules screen."
          }
        />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="t" style={{ tableLayout: "fixed" }}>
              <thead>
                <tr>
                  <th>
                    {recordPluralCap} in the group <TermHint name="record" />
                  </th>
                  <th style={{ width: 60, textAlign: "right" }}>Size</th>
                  <th style={{ width: 110 }}>
                    Track <TermHint name="track" />
                  </th>
                  <th style={{ width: 140 }}>
                    Match keys <TermHint name="matchKey" />
                  </th>
                  <th style={{ width: 140 }}>
                    Against the {earlier} <TermHint name="earlierGrouping" />
                  </th>
                  <th style={{ width: 160 }}>
                    Earlier IDs <TermHint name="earlierId" />
                  </th>
                  {priorityColumn && (
                    <th style={{ width: 110, textAlign: "right" }}>{priorityColumn.label}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {items.map((g) => {
                  const open = openGroup === g.group_id;
                  const names = Array.isArray(g.names) ? g.names : [];
                  const extra = Math.max(0, (g.size || names.length) - names.length);
                  const ids = Array.isArray(g.existing_ids) ? g.existing_ids : [];
                  return [
                    <tr
                      key={g.group_id}
                      className={"sortable" + (open ? " selected" : "")}
                      style={{ cursor: "pointer" }}
                      onClick={() => setOpenGroup(open ? null : g.group_id)}
                    >
                      <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
                        {names.join(" | ") || <span className="muted">—</span>}
                        {extra > 0 && (
                          <span className="tag" style={{ marginLeft: 6 }}>
                            +{extra}
                          </span>
                        )}
                        <div className="mono muted" style={{ fontSize: 11 }}>
                          {g.group_id}
                          {g.n_labelled ? ` · ${g.n_labelled} already labelled` : ""}
                        </div>
                        {g.status === "held" && (
                          <div style={{ fontSize: 12, color: "var(--amber)" }}>
                            Held group: {g.guard_text || guardReason(g.guard) || "a guard stopped this merge"}
                          </div>
                        )}
                      </td>
                      <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                        {fmtNumber(g.size)}
                      </td>
                      <td style={{ verticalAlign: "top" }}>
                        <span className="tag">{trackLabel(g.track)}</span>
                      </td>
                      <td
                        style={{ fontSize: 12, whiteSpace: "normal", verticalAlign: "top" }}
                      >
                        {(g.key_ids || []).map((id) => (
                          <div key={id}>
                            {keyName(id)}
                            {keyName(id) !== id && (
                              <span className="muted mono" style={{ fontSize: 11, marginLeft: 5 }}>
                                {id}
                              </span>
                            )}
                          </div>
                        ))}
                      </td>
                      <td style={{ verticalAlign: "top" }}>
                        {g.status === "held" ? (
                          <span className="tag amber" title="A guard stopped a match key merging these records">
                            Held group
                          </span>
                        ) : (
                          <AgreementTag value={g.agreement} />
                        )}
                      </td>
                      <td style={{ whiteSpace: "normal", verticalAlign: "top" }}>
                        {ids.length === 0 ? (
                          <span className="muted" style={{ fontSize: 12 }}>
                            no earlier ID
                          </span>
                        ) : (
                          ids.map((id) => (
                            <span
                              key={id}
                              className={"tag" + (ids.length > 1 ? " red" : "")}
                              style={{
                                fontFamily: "var(--font-mono)",
                                textTransform: "none",
                                marginRight: 4,
                              }}
                            >
                              {id}
                            </span>
                          ))
                        )}
                      </td>
                      {priorityColumn && (
                        <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                          <Cell
                            value={g.priority ? g.priority[priorityColumn.key] : null}
                            type={priorityColumn.type}
                          />
                        </td>
                      )}
                    </tr>,
                    open ? (
                      <tr key={g.group_id + "_members"}>
                        <td colSpan={priorityColumn ? 7 : 6} style={{ whiteSpace: "normal" }}>
                          <GroupMembers
                            runId={runId}
                            groupId={g.group_id}
                            columns={displayColumns}
                            existingIds={ids}
                            recordPlural={recordPlural}
                          />
                        </td>
                      </tr>
                    ) : null,
                  ];
                })}
              </tbody>
            </table>
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
            }}
          >
            <span className="muted" style={{ fontSize: 12 }}>
              Showing {fmtNumber(firstShown)}&ndash;{fmtNumber(lastShown)} of {fmtNumber(total)}{" "}
              group{total === 1 ? "" : "s"}
            </span>
            {totalPages > 1 && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  Page {page + 1} of {totalPages}
                </span>
                <button className="btn sm" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
                  <Icons.arrowR size={12} style={{ transform: "rotate(180deg)" }} />
                  Prev
                </button>
                <button
                  className="btn sm"
                  disabled={page + 1 >= totalPages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next
                  <Icons.arrowR size={12} />
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

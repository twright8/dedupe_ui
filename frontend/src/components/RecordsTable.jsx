/* ============================================================
   RecordsTable — the records one run loaded, one row per record
   ------------------------------------------------------------
   Every data column comes from the profile (key, label, type), and
   the word for a record comes from the profile too, so nothing here
   knows about donations or PSC. Filtering, sorting and paging all
   happen on the server; this component only holds the controls'
   state. Later slices reuse it.
   ============================================================ */

import { useState, useEffect, Fragment } from "react";
import { api } from "../api";
import { Icons } from "./Icons";
import { fmtNumber } from "./ProbBar";
import { Empty } from "./Empty";
import { Cell, NUMERIC_TYPES } from "./cells";
import { CleaningTrace } from "./CleaningTrace";
import { Term, TermHint } from "./Term";
import { RULES_REPLAYED, valueMeta } from "../glossary";
import { noun } from "../profileText";

const PER_PAGE = 100;

// ---------- Main table ----------
export default function RecordsTable({ runId, profile }) {
  // A profile always names its columns; the fallback keeps the table readable
  // if one ever arrives empty.
  const profileFallback =
    profile.display_columns && profile.display_columns.length
      ? profile.display_columns
      : [{ key: "name", label: "Name", type: "text" }];
  const tracks = profile.tracks || [];
  const recordWord = noun(profile, "record");
  const recordPlural = noun(profile, "record_plural");

  const [query, setQuery] = useState("");   // what is typed
  const [q, setQ] = useState("");           // what is sent, debounced
  const [track, setTrack] = useState("all");
  const [state, setState] = useState("all");
  const [sort, setSort] = useState("");
  const [order, setOrder] = useState("asc");
  const [page, setPage] = useState(0);
  const [showCleaned, setShowCleaned] = useState(false);
  const [open, setOpen] = useState(null); // the record whose drawer is open

  // The run itself says which columns it holds, and whether each came from the
  // profile or from a cleaning step. Kept in state so the controls stay put
  // while the next page loads, and so an older backend that sends no columns
  // still gets the profile's list.
  const [columnDefs, setColumnDefs] = useState(null);

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);

  // Debounce the search box so typing doesn't fire a request per keystroke.
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
    const params = { offset: page * PER_PAGE, limit: PER_PAGE };
    if (q) params.q = q;
    if (track !== "all") params.track = track;
    if (state !== "all") params.state = state;
    if (sort) {
      params.sort = sort;
      params.order = order;
    }
    api
      .getRunRecords(runId, params)
      .then((res) => {
        if (!alive) return;
        setData(res);
        if (Array.isArray(res?.columns) && res.columns.length) setColumnDefs(res.columns);
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
  }, [runId, q, track, state, sort, order, page, attempt]);

  function chooseTrack(value) {
    setTrack(value);
    setPage(0);
  }

  function chooseState(value) {
    setState(value);
    setPage(0);
  }

  function toggleSort(key) {
    if (sort === key) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setOrder("asc");
    }
    setPage(0);
  }

  function sortDescBy(key) {
    setSort(key);
    setOrder("desc");
    setPage(0);
  }

  // Cleaning columns are the ones a cleaning step wrote. They stay hidden until
  // asked for, because most of the time the profile's own columns are the point.
  const allColumns = columnDefs || profileFallback;
  const profileColumns = allColumns.filter((c) => c.source !== "cleaning");
  const cleaningColumns = allColumns.filter((c) => c.source === "cleaning");
  const columns = showCleaned ? [...profileColumns, ...cleaningColumns] : profileColumns;
  const isCleaned = (col) => col.source === "cleaning";
  const priorityColumns = (profile.priority_columns || [])
    .map((key) => allColumns.find((c) => c.key === key))
    .filter(Boolean);

  // counts cover the whole run; total reflects the filters in force.
  const counts = data?.counts || {};
  const items = data?.items || [];
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));
  const firstShown = total === 0 ? 0 : page * PER_PAGE + 1;
  const lastShown = page * PER_PAGE + items.length;

  function trackLabel(key) {
    const t = tracks.find((x) => x.key === key);
    return t ? t.label : key || "—";
  }

  const filtersOn = !!q || track !== "all" || state !== "all";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Controls */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <div className="search" style={{ width: 280 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder={`Search ${recordPlural}...`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <div className="seg" title="Filter by track">
          <button className={track === "all" ? "on" : ""} onClick={() => chooseTrack("all")}>
            All
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {fmtNumber(counts.all)}
            </span>
          </button>
          {tracks.map((t) => (
            <button
              key={t.key}
              className={track === t.key ? "on" : ""}
              onClick={() => chooseTrack(t.key)}
            >
              {t.label}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {fmtNumber(counts[t.key])}
              </span>
            </button>
          ))}
        </div>

        <div className="seg" title="Filter by whether a reviewer has answered">
          {[
            ["all", "All", counts.all],
            ["labelled", "Labelled", counts.labelled],
            ["unreviewed", "Unreviewed", counts.unreviewed],
          ].map(([id, label, n]) => (
            <button key={id} className={state === id ? "on" : ""} onClick={() => chooseState(id)}>
              {label}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {fmtNumber(n)}
              </span>
            </button>
          ))}
        </div>

        {priorityColumns.map((col) => (
          <button
            key={col.key}
            className="btn sm"
            onClick={() => sortDescBy(col.key)}
            title={`Sort by ${col.label}, largest first`}
          >
            Sort by {col.label} &darr;
          </button>
        ))}

        {cleaningColumns.length > 0 && (
          <button
            className={"btn" + (showCleaned ? " primary" : "")}
            onClick={() => setShowCleaned((v) => !v)}
            title="Show the columns the cleaning steps wrote, beside the ones the file came with"
          >
            <Icons.table size={14} stroke={showCleaned ? "#fff" : undefined} />
            Cleaned and derived columns
            <span className="muted" style={{ fontSize: 11 }}>
              &middot; {cleaningColumns.length}
            </span>
          </button>
        )}

        <div className="spacer" />
        <button className="btn" onClick={() => setAttempt((n) => n + 1)}>
          <Icons.refresh size={14} />
          Refresh
        </button>
      </div>

      {loading ? (
        <p className="muted pulse" style={{ fontSize: 13.5, padding: 40 }}>
          Loading {recordPlural}...
        </p>
      ) : error ? (
        <Empty
          title={`Could not load the ${recordPlural}`}
          sub={error}
          action={
            <button className="btn primary" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </button>
          }
        />
      ) : items.length === 0 ? (
        <Empty
          title={`No ${recordPlural} found`}
          sub={
            filtersOn
              ? `No ${recordWord} matches these filters. Clear the search or pick another track.`
              : `This run loaded no ${recordPlural}.`
          }
        />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>
                    Track <TermHint name="track" />
                  </th>
                  {columns.map((col) => {
                    const numeric = NUMERIC_TYPES.has(col.type);
                    return (
                      <th
                        key={col.key}
                        className="sortable"
                        style={{
                          textAlign: numeric ? "right" : "left",
                          whiteSpace: "nowrap",
                          // Cleaning columns read as secondary, so their header
                          // stays muted and keeps the mono face of a column name.
                          color: isCleaned(col) ? "var(--muted-2)" : undefined,
                          fontFamily: isCleaned(col) ? "var(--font-mono)" : undefined,
                        }}
                        onClick={() => toggleSort(col.key)}
                        title={isCleaned(col) ? "Written by a cleaning step. Click to sort." : "Click to sort"}
                      >
                        {col.label}
                        {sort === col.key ? (order === "asc" ? " ▲" : " ▼") : ""}
                      </th>
                    );
                  })}
                  <th style={{ width: 150 }}>
                    Reviewed? <TermHint name="label" />
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((r) => {
                  const isOpen = open === r.record_id;
                  return (
                    <Fragment key={r.record_id}>
                      <tr
                        className={isOpen ? "selected" : ""}
                        style={{ cursor: "pointer" }}
                        onClick={() => setOpen(isOpen ? null : r.record_id)}
                        title="Open this record to see how it was cleaned"
                      >
                        <td>
                          <span className="tag">{trackLabel(r.track)}</span>
                        </td>
                        {columns.map((col, i) => {
                          const numeric = NUMERIC_TYPES.has(col.type);
                          return (
                            <td
                              key={col.key}
                              className={numeric ? "mono tnum" : ""}
                              style={{ textAlign: numeric ? "right" : "left" }}
                            >
                              <Cell value={r[col.key]} type={col.type} />
                              {/* The record id rides under the first column, as run
                                  ids do on the runs list. */}
                              {i === 0 && (
                                <div className="mono muted" style={{ fontSize: 11 }}>
                                  {r.record_id}
                                </div>
                              )}
                            </td>
                          );
                        })}
                        <td>
                          {r.review_state === "labelled" ? (
                            <span className="tag green">
                              <span className="dot" />
                              labelled
                            </span>
                          ) : (
                            <span className="tag">
                              <span className="dot" />
                              unreviewed
                            </span>
                          )}
                          {r.existing_entity_id && (
                            <div className="muted" style={{ fontSize: 11 }}>
                              earlier ID <span className="mono">{r.existing_entity_id}</span>
                            </div>
                          )}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr>
                          <td colSpan={columns.length + 2} style={{ whiteSpace: "normal" }}>
                            <RecordCleaning runId={runId} recordId={r.record_id} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Paging */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
            }}
          >
            <span className="muted" style={{ fontSize: 12 }}>
              Showing {fmtNumber(firstShown)}&ndash;{fmtNumber(lastShown)} of{" "}
              {fmtNumber(total)} {total === 1 ? recordWord : recordPlural}
            </span>
            {totalPages > 1 && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  Page {page + 1} of {totalPages}
                </span>
                <button
                  className="btn sm"
                  disabled={page <= 0}
                  onClick={() => setPage((p) => p - 1)}
                >
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

/* ---------- one record, cleaned step by step ----------
   POST /api/config/preview-cleaning with a record id and a run id
   replays THAT RUN'S OWN frozen rules on that record. It does not use
   today's rules, because today's rules did not make the value on screen.
   The answer says which rules it replayed and that is printed here.
   ---------------------------------------------------------- */
function RecordCleaning({ runId, recordId }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setResult(null);
    setError(null);
    api
      .previewCleaning({ record_id: String(recordId), run_id: runId })
      .then((res) => alive && setResult(res))
      .catch((err) => alive && setError(err.message));
    return () => {
      alive = false;
    };
  }, [runId, recordId]);

  const samples = Array.isArray(result?.samples) ? result.samples : [];
  const rules = valueMeta(RULES_REPLAYED, result?.source);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "4px 0 8px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span className="eyebrow">How this record was cleaned</span>
        <span className="mono muted" style={{ fontSize: 11.5 }}>
          {recordId}
        </span>
        {rules && <span className={"tag " + (rules.tag || "")}>{rules.label}</span>}
      </div>
      {rules && (
        <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
          {rules.definition}
        </p>
      )}
      {error ? (
        <p style={{ fontSize: 12.5, color: "var(--ti-red)", margin: 0 }}>{error}</p>
      ) : !result ? (
        <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
          Replaying this run's <Term name="cleaningStep" plural />...
        </p>
      ) : samples.length === 0 ? (
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          This track has no <Term name="cleaningStep" plural />, so nothing changed.
        </p>
      ) : (
        samples.map((sample, i) => <CleaningTrace key={i} sample={sample} />)
      )}
    </div>
  );
}

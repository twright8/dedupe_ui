/* ============================================================
   Screen: Audit log
   Filterable, paginated event log — every action attributed to a user.
   ============================================================ */

import { Fragment, useState, useEffect, useCallback } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { fmtDateTime, timeAgo } from "../components/ProbBar";
import { Empty } from "../components/Empty";

const PER_PAGE = 25;

function parseMetadata(value) {
  if (!value) return null;
  if (typeof value === "object") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

// ---------- Kind colour tag ----------
function KindTag({ kind }) {
  const map = {
    run:    { cls: "blue",   lab: "run" },
    label:  { cls: "green",  lab: "label" },
    config: { cls: "violet", lab: "config" },
    thresh: { cls: "amber",  lab: "threshold" },
    threshold: { cls: "amber", lab: "threshold" },
    upload: { cls: "",       lab: "upload" },
    export: { cls: "",       lab: "export" },
    model:  { cls: "violet", lab: "model" },
  };
  const m = map[kind] || { cls: "", lab: kind };
  return (
    <span className={"tag " + m.cls}>
      <span className="dot" />
      {m.lab}
    </span>
  );
}

// ---------- Main screen ----------
export default function AuditScreen() {
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [events, setEvents] = useState([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [openMeta, setOpenMeta] = useState({});

  const kinds = [
    { id: "all",    lab: "All",       icon: Icons.audit },
    { id: "run",    lab: "Runs",      icon: Icons.runs },
    { id: "label",  lab: "Labels",    icon: Icons.review },
    { id: "config", lab: "Config",    icon: Icons.config },
    { id: "threshold", lab: "Threshold", icon: Icons.bolt },
    { id: "model", lab: "Model", icon: Icons.bolt },
    { id: "upload", lab: "Uploads",   icon: Icons.upload },
    { id: "export", lab: "Exports",   icon: Icons.download },
  ];

  // Fetch events from API
  const fetchEvents = useCallback(() => {
    setLoading(true);
    const params = { page, per_page: PER_PAGE };
    if (filter !== "all") params.kind = filter;

    api
      .listAudit(params)
      .then((data) => {
        const rows = Array.isArray(data) ? data : data.items || data.events || [];
        const tot  = typeof data?.total === "number" ? data.total : rows.length;
        setEvents(rows);
        setTotal(tot);

        return api.auditCounts();
      })
      .then((c) => {
        setCounts(c || {});
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [filter, page, search]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setPage(1);
  }, [filter, search]);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  // Export handler
  function handleExport() {
    const params = new URLSearchParams();
    if (filter !== "all") params.set("kind", filter);
    window.open(api.auditExportUrl(params), "_blank");
  }

  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));

  // Client-side search filter on fetched events (supplements API-side)
  const displayed = search
    ? events.filter((e) => {
        const q = search.toLowerCase();
        return (
          (e.who  && e.who.toLowerCase().includes(q))  ||
          (e.user_name && e.user_name.toLowerCase().includes(q)) ||
          (e.desc && e.desc.toLowerCase().includes(q)) ||
          (e.description && e.description.toLowerCase().includes(q)) ||
          (e.kind && e.kind.toLowerCase().includes(q))
        );
      })
    : events;

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1 className="page-title">Audit log</h1>
          <p className="page-sub">
            Every action — runs, labels, config saves, threshold changes, uploads, exports — attributed
            to a user. Append-only.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={handleExport}>
            <Icons.download size={14} />
            Export JSON-L
          </button>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "200px 1fr",
          gap: 16,
          alignItems: "flex-start",
        }}
      >
        {/* ---- Kind filter sidebar ---- */}
        <div className="card" style={{ position: "sticky", top: 70 }}>
          <div className="card-h" style={{ padding: "10px 12px" }}>
            <span className="eyebrow">Type</span>
          </div>
          <div style={{ padding: 4 }}>
            {kinds.map((k) => {
              const n = counts[k.id] ?? 0;
              return (
                <div
                  key={k.id}
                  className={"nav-item " + (filter === k.id ? "active" : "")}
                  onClick={() => setFilter(k.id)}
                  style={{ borderRadius: 4 }}
                >
                  <k.icon size={14} />
                  <span>{k.lab}</span>
                  <span className="count">{n}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* ---- Event table ---- */}
        <div className="card">
          <div className="card-h">
            <Icons.history size={16} />
            <h3>Recent events</h3>
            <div className="actions" style={{ gap: 6 }}>
              <div className="search" style={{ width: 240 }}>
                <Icons.search size={14} />
                <input
                  className="input"
                  placeholder="Search events…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
            </div>
          </div>

          {loading ? (
            <p className="muted pulse" style={{ padding: 40, textAlign: "center" }}>
              Loading audit log…
            </p>
          ) : error ? (
            <Empty
              title="Failed to load audit log"
              sub={error}
              action={
                <button className="btn primary" onClick={fetchEvents}>
                  <Icons.refresh size={14} />
                  Retry
                </button>
              }
            />
          ) : displayed.length === 0 ? (
            <Empty
              title="No events"
              sub={filter !== "all" ? `No ${filter} events found.` : "No audit events yet."}
            />
          ) : (
            <>
              <table className="t">
                <thead>
                  <tr>
                    <th style={{ width: 130 }}>When</th>
                    <th style={{ width: 160 }}>Who</th>
                    <th style={{ width: 100 }}>Kind</th>
                    <th>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {displayed.map((e, i) => {
                    const key = e.id || i;
                    const meta = parseMetadata(e.metadata_json || e.metadata);
                    return (
                      <Fragment key={key}>
                        <tr>
                          <td>
                            <div className="mono">{fmtDateTime(e.timestamp || e.at)}</div>
                            <div className="muted" style={{ fontSize: 11 }}>{timeAgo(e.timestamp || e.at)}</div>
                          </td>
                          <td>
                            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                              <div
                                className="avatar"
                                style={{ width: 22, height: 22, fontSize: 10 }}
                              >
                                {e.init || initials(e.user_name || e.who)}
                              </div>
                              <span style={{ fontSize: 13 }}>{e.user_name || e.who}</span>
                            </div>
                          </td>
                          <td>
                            <KindTag kind={e.kind} />
                          </td>
                          <td style={{ whiteSpace: "normal" }}>
                            <div style={{ fontSize: 13, display: "flex", gap: 8, alignItems: "center" }}>
                              <span>{e.description || e.desc}</span>
                              {meta && (
                                <button
                                  className="btn sm"
                                  style={{ marginLeft: "auto" }}
                                  onClick={() => setOpenMeta((m) => ({ ...m, [key]: !m[key] }))}
                                >
                                  {openMeta[key] ? "Hide details" : "Details"}
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                        {meta && openMeta[key] && (
                        <tr>
                            <td colSpan={4} style={{ background: "var(--bg)", whiteSpace: "normal" }}>
                              <pre
                                className="mono"
                                style={{
                                  margin: 0,
                                  fontSize: 11.5,
                                  whiteSpace: "pre-wrap",
                                  wordBreak: "break-word",
                                }}
                              >
                                {typeof meta === "string" ? meta : JSON.stringify(meta, null, 2)}
                              </pre>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>

              {/* ---- Pagination ---- */}
              {totalPages > 1 && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "10px 14px",
                    borderTop: "1px solid var(--line)",
                  }}
                >
                  <span className="muted" style={{ fontSize: 12 }}>
                    Page {page} of {totalPages} &middot; {total} event{total !== 1 ? "s" : ""}
                  </span>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button
                      className="btn sm"
                      disabled={page <= 1}
                      onClick={() => setPage((p) => p - 1)}
                    >
                      <Icons.arrowR size={12} style={{ transform: "rotate(180deg)" }} />
                      Prev
                    </button>
                    <button
                      className="btn sm"
                      disabled={page >= totalPages}
                      onClick={() => setPage((p) => p + 1)}
                    >
                      Next
                      <Icons.arrowR size={12} />
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- helper ----------
function initials(name) {
  if (!name) return "?";
  return name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

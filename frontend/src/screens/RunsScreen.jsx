/* ============================================================
   Screen: Runs list
   ============================================================ */

import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { fmtNumber, fmtPct, fmtDateTime, timeAgo } from "../components/ProbBar";
import { Empty } from "../components/Empty";
import { useProfile } from "../profile";
import { hasEntityCounts, hasExactCounts, hasPairCounts, hasRecordCounts, trackCountKey } from "../counts";
import { decidedBy } from "./RunDetailScreen";

// Old linkage runs report pair buckets; a run that only loaded records does not.
// The list shows whichever set of numbers the runs actually carry.

function normalizeRun(r) {
  const dur = r.duration_secs;
  let duration = "—";
  if (dur != null) {
    const m = Math.floor(dur / 60);
    const s = Math.round(dur % 60);
    duration = m > 0 ? `${m}m ${s}s` : `${s}s`;
  }
  return {
    ...r,
    started: r.started_at || r.started,
    finished: r.finished_at || r.finished,
    duration,
    config: r.config_version != null ? `v${r.config_version}` : r.config || "",
    by: r.triggered_by || r.by || "",
    byInit: r.triggered_by ? r.triggered_by.split(" ").map(w => w[0] || "").join("").toUpperCase().slice(0, 2) : r.byInit || "",
    label: r.label || r.input_filename || "",
    counts: r.counts || null,
  };
}

export default function RunsScreen() {
  const navigate = useNavigate();
  const profile = useProfile();
  const tracks = profile.tracks || [];
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");

  const fetchRuns = useCallback(() => {
    setLoading(true);
    api
      .listRuns()
      .then((data) => {
        const raw = Array.isArray(data) ? data : data.runs || [];
        setRuns(raw.map(normalizeRun));
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  // Filter + search
  const filtered = runs
    .filter((r) => (filter === "all" ? true : r.status === filter))
    .filter((r) => {
      if (!search) return true;
      const q = search.toLowerCase();
      return (
        (r.id && r.id.toLowerCase().includes(q)) ||
        (r.label && r.label.toLowerCase().includes(q)) ||
        (r.by && r.by.toLowerCase().includes(q)) ||
        (r.config && r.config.toLowerCase().includes(q))
      );
    });

  // Which numbers this list can show at all
  const anyPairs = runs.some((r) => hasPairCounts(r.counts));

  // Sparkline: how many entities each recent run ended with, oldest to newest.
  // Fewer entities over the same records means more records were joined up.
  const recent = [...runs]
    .filter((r) => hasEntityCounts(r.counts))
    .reverse()
    .slice(-8);
  const maxEntities = recent.length
    ? Math.max(...recent.map((r) => r.counts.entitiesProposed || 0), 1)
    : 1;

  // KPI values from latest run (if exists)
  const latest = runs.length ? runs[0] : null;
  const prior = runs.length > 1 ? runs[1] : null;

  if (loading) {
    return (
      <div className="content">
        <p className="muted pulse" style={{ fontSize: 13.5, padding: 40 }}>
          Loading runs...
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="content">
        <Empty
          title="Failed to load runs"
          sub={error}
          action={
            <button className="btn primary" onClick={fetchRuns}>
              Retry
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1 className="page-title">Runs</h1>
          <p className="page-sub">
            Each run reads one {profile.input?.label || "input file"} and reconciles
            the records inside it. Labels persist across runs.
          </p>
        </div>
        <button
          className="btn primary lg"
          onClick={() => navigate("/runs/new")}
        >
          <Icons.plus size={14} stroke="#fff" />
          New run
        </button>
      </div>

      {/* KPI strip — record counts when the latest run has no pair buckets */}
      {latest && !hasPairCounts(latest.counts) && hasRecordCounts(latest.counts) && (
        <div className="kpi-grid" style={{ marginBottom: 20 }}>
          <div className="kpi">
            <div className="label">Records (latest)</div>
            <div className="value">{fmtNumber(latest.counts.recordsTotal)}</div>
            <div className="delta muted">
              {fmtNumber(latest.counts.inputRows)} rows in the file
            </div>
          </div>
          {tracks.map((t) => (
            <div className="kpi" key={t.key}>
              <div className="label">{t.label}</div>
              <div className="value">{fmtNumber(latest.counts[trackCountKey(t.key)])}</div>
              <div className="delta muted">
                {fmtPct(
                  latest.counts.recordsTotal > 0
                    ? (latest.counts[trackCountKey(t.key)] || 0) / latest.counts.recordsTotal
                    : 0,
                  1
                )}{" "}
                of records
              </div>
            </div>
          ))}
          {hasExactCounts(latest.counts) && (
            <div className="kpi">
              <div className="label">Entities after exact keys</div>
              <div className="value">{fmtNumber(latest.counts.exactEntitiesAfter)}</div>
              <div className="delta muted">
                {fmtNumber(latest.counts.exactMergedRecords)} records merged
                {latest.counts.exactHeldGroups
                  ? `, ${fmtNumber(latest.counts.exactHeldGroups)} held`
                  : ""}
              </div>
            </div>
          )}
          <div className="kpi">
            <div className="label">Unreviewed</div>
            <div className="value" style={{ color: "var(--amber)" }}>
              {fmtNumber(latest.counts.recordsUnreviewed)}
            </div>
            <div className="delta muted">
              {fmtNumber(latest.counts.recordsLabelled)} already labelled
            </div>
          </div>
        </div>
      )}

      {latest && hasPairCounts(latest.counts) && (
        <div className="kpi-grid" style={{ marginBottom: 20 }}>
          <div className="kpi">
            <div className="label">Pairs to review</div>
            <div className="value" style={{ color: "var(--amber)" }}>
              {fmtNumber(latest.counts.pairsReview)}
            </div>
            <div className="delta muted">
              {fmtNumber(latest.counts.pairsAccept)} accepted of{" "}
              {fmtNumber(latest.counts.pairsScored)} scored
            </div>
          </div>
          {hasEntityCounts(latest.counts) && (
            <>
              <div className="kpi">
                <div className="label">Entities proposed</div>
                <div className="value">{fmtNumber(latest.counts.entitiesProposed)}</div>
                <div className="delta muted">
                  from {fmtNumber(latest.counts.recordsTotal)} records and{" "}
                  {fmtNumber(latest.counts.unitsTotal)} units
                </div>
              </div>
              <div className="kpi">
                <div className="label">Groups to decide</div>
                <div className="value" style={{ color: "var(--amber)" }}>
                  {fmtNumber(latest.counts.reviewQueue)}
                </div>
                <div className="delta muted">
                  {fmtNumber(latest.counts.decisionsTotal)} already decided
                </div>
              </div>
              <div className="kpi">
                <div className="label">Entities &middot; last 8 runs</div>
                <div style={{ marginTop: 6 }}>
                  <div className="barchart">
                    {recent.map((r, i) => (
                      <div
                        key={i}
                        className="b"
                        style={{
                          height: `${((r.counts.entitiesProposed || 0) / maxEntities) * 100}%`,
                        }}
                        title={`${r.id}: ${fmtNumber(r.counts.entitiesProposed)} entities`}
                      />
                    ))}
                  </div>
                  {recent.length >= 2 && (
                    <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                      {fmtNumber(recent[0].counts.entitiesProposed)} &rarr;{" "}
                      {fmtNumber(recent[recent.length - 1].counts.entitiesProposed)}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* Filters */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 12,
        }}
      >
        <div className="seg">
          {["all", "complete", "failed"].map((s) => (
            <button
              key={s}
              className={filter === s ? "on" : ""}
              onClick={() => setFilter(s)}
            >
              {s[0].toUpperCase() + s.slice(1)}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot;{" "}
                {
                  runs.filter((r) =>
                    s === "all" ? true : r.status === s
                  ).length
                }
              </span>
            </button>
          ))}
        </div>
        <div className="search" style={{ width: 280 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Filter by run id, uploader, config..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="spacer" />
        <button className="btn" onClick={fetchRuns}>
          <Icons.refresh size={14} />
          Refresh
        </button>
      </div>

      {/* Table */}
      {filtered.length === 0 ? (
        <Empty
          title="No runs found"
          sub={
            search
              ? "Try a different search term."
              : "Start a new run to see results here."
          }
          action={
            <button
              className="btn primary"
              onClick={() => navigate("/runs/new")}
            >
              <Icons.plus size={14} stroke="#fff" /> New run
            </button>
          }
        />
      ) : (
        <div className="tbl-wrap">
          <table className="t">
            <thead>
              <tr>
                <th style={{ width: 50 }}></th>
                <th>Run</th>
                <th>Started</th>
                <th>Duration</th>
                {anyPairs ? (
                  <>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      Records
                    </th>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      Units
                    </th>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      Accepted
                    </th>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      Review
                    </th>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      Entities
                    </th>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      To decide
                    </th>
                    <th>Decided by</th>
                  </>
                ) : (
                  <>
                    <th className="tnum" style={{ textAlign: "right" }}>
                      Records
                    </th>
                    {tracks.map((t) => (
                      <th
                        key={t.key}
                        className="tnum"
                        style={{ textAlign: "right" }}
                      >
                        {t.label}
                      </th>
                    ))}
                  </>
                )}
                <th>Config</th>
                <th>By</th>
                <th style={{ width: 60 }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => navigate(`/runs/${r.id}`)}
                  style={{ cursor: "pointer" }}
                >
                  <td>
                    {r.status === "complete" ? (
                      <span className="dot green" />
                    ) : r.status === "failed" ? (
                      <span className="dot red" />
                    ) : (
                      <span className="dot amber" />
                    )}
                  </td>
                  <td>
                    <div style={{ fontWeight: 500 }}>{r.label}</div>
                    <div
                      className="mono muted"
                      style={{ fontSize: 11 }}
                    >
                      {r.id}
                    </div>
                  </td>
                  <td>
                    <div>{fmtDateTime(r.started)}</div>
                    <div
                      className="muted"
                      style={{ fontSize: 11 }}
                    >
                      {timeAgo(r.started)}
                    </div>
                  </td>
                  <td className="mono">{r.duration}</td>
                  {anyPairs ? (
                    <>
                      <td className="mono" style={{ textAlign: "right" }}>
                        {fmtNumber(r.counts?.recordsTotal)}
                      </td>
                      <td className="mono" style={{ textAlign: "right" }}>
                        {fmtNumber(r.counts?.unitsTotal)}
                      </td>
                      <td className="mono" style={{ textAlign: "right" }}>
                        {hasPairCounts(r.counts) ? fmtNumber(r.counts.pairsAccept) : "—"}
                      </td>
                      <td
                        className="mono"
                        style={{
                          textAlign: "right",
                          color: r.counts?.pairsReview ? "var(--amber)" : "var(--muted)",
                        }}
                      >
                        {fmtNumber(r.counts?.pairsReview)}
                      </td>
                      <td className="mono" style={{ textAlign: "right" }}>
                        {hasEntityCounts(r.counts) ? fmtNumber(r.counts.entitiesProposed) : "—"}
                      </td>
                      <td
                        className="mono"
                        style={{
                          textAlign: "right",
                          color: r.counts?.reviewQueue ? "var(--amber)" : "var(--muted)",
                        }}
                      >
                        {hasEntityCounts(r.counts) ? fmtNumber(r.counts.reviewQueue) : "—"}
                      </td>
                    </>
                  ) : (
                    <>
                      <td
                        className="mono"
                        style={{ textAlign: "right" }}
                      >
                        {fmtNumber(r.counts?.recordsTotal)}
                      </td>
                      {tracks.map((t) => (
                        <td
                          key={t.key}
                          className="mono"
                          style={{ textAlign: "right" }}
                        >
                          {fmtNumber(r.counts?.[trackCountKey(t.key)])}
                        </td>
                      ))}
                    </>
                  )}
                  <td>
                    <span className="tag">{r.config}</span>
                  </td>
                  <td>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                      }}
                    >
                      <div
                        className="avatar"
                        style={{
                          width: 22,
                          height: 22,
                          fontSize: 10,
                        }}
                      >
                        {r.byInit}
                      </div>
                      <span style={{ fontSize: 12.5 }}>
                        {r.by ? r.by.split(" ")[0] : ""}
                      </span>
                    </div>
                  </td>
                  <td>
                    <Icons.more size={14} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

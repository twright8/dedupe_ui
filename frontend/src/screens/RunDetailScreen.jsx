/* ============================================================
   Screen: Run detail — summary, records, diagnostics, files, history
   ============================================================ */

import { Component, useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, apiUrl } from "../api";
import { Icons } from "../components/Icons";
import { fmtNumber, fmtPct, fmtDateTime, timeAgo } from "../components/ProbBar";
import { Empty } from "../components/Empty";
import ModelPanel from "../components/ModelPanel";
import RecordsTable from "../components/RecordsTable";
import ExactGroupsTable from "../components/ExactGroupsTable";
import { useProfile } from "../profile";
import { hasExactCounts, hasPairCounts, hasRecordCounts, trackCountKey } from "../counts";
import { useRunProgress } from "../hooks/useRunProgress";

// ---------- Unmapped-lookup-values self-serve fix ----------
// Shown when a run failed because a lookup whose fallback is "error" met values
// it does not hold. Lets the user give the canonical value for each (or keep it
// as it is), add the rows to that lookup as a new config version, and re-run —
// without leaving the screen or editing the Config tab by hand.
function UnmappedLookupValuesPanel({ run, runId, navigate }) {
  const detail = run.error_detail || {};
  const table = detail.table || "";
  // Values may arrive as plain strings or as small objects; both read the same.
  const unmapped = (detail.values || []).map((v) =>
    typeof v === "string" ? v : String(v?.value ?? v?.raw ?? "")
  );
  const [rows, setRows] = useState(
    unmapped.map((raw) => ({ raw, canonical: raw, keep: true }))
  );
  const [saving, setSaving] = useState(false);
  const [savedVersion, setSavedVersion] = useState(null);
  const [err, setErr] = useState(null);

  function setRow(i, patch) {
    setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  function handleSave() {
    if (rows.some((r) => !r.keep && !r.canonical.trim())) {
      setErr("Every value needs a canonical value, or tick keep as is.");
      return;
    }
    setSaving(true);
    setErr(null);
    api
      .addLookupRows(table, {
        rows: rows.map((r) => ({ raw: r.raw, canonical: r.keep ? r.raw : r.canonical.trim() })),
        note: `Added ${rows.length} row(s) to ${table} after run ${runId} hit unmapped values`,
      })
      .then((res) => setSavedVersion(res.version))
      .catch((e) => setErr(e.message || "Failed to save"))
      .finally(() => setSaving(false));
  }

  return (
    <div
      className="card"
      style={{ borderColor: "var(--ti-red-200, #f3c2c2)", marginBottom: 16 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <Icons.alert size={16} />
        <h3 style={{ margin: 0 }}>Unmapped lookup values blocked this run</h3>
      </div>
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
        The data contained {unmapped.length} value{unmapped.length === 1 ? "" : "s"} the lookup{" "}
        <span className="mono">{table}</span> does not hold, and that lookup is set to stop the
        run rather than guess. Give the canonical value for each, or keep it as it is, then save
        a new config version and re-run.
      </p>

      {savedVersion == null ? (
        <>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--muted, #777)" }}>
                <th style={{ padding: "4px 8px" }}>Raw value (from data)</th>
                <th style={{ padding: "4px 8px" }}>Canonical value</th>
                <th style={{ padding: "4px 8px", width: 110 }}>Keep as is</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.raw}>
                  <td style={{ padding: "4px 8px" }} className="mono">
                    {r.raw}
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    <input
                      className="input"
                      style={{ width: "100%" }}
                      value={r.keep ? r.raw : r.canonical}
                      disabled={r.keep}
                      onChange={(e) => setRow(i, { canonical: e.target.value })}
                    />
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    <input
                      type="checkbox"
                      checked={r.keep}
                      onChange={(e) => setRow(i, { keep: e.target.checked })}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {err && (
            <p style={{ color: "var(--ti-red, #c0392b)", fontSize: 13 }}>{err}</p>
          )}
          <div style={{ marginTop: 10 }}>
            <button className="btn primary" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : `Add ${rows.length} row${rows.length === 1 ? "" : "s"} to ${table} & save new config version`}
            </button>
          </div>
        </>
      ) : (
        <div
          style={{
            background: "var(--green-50, #eaf7ee)",
            borderRadius: 6,
            padding: 12,
            display: "flex",
            alignItems: "center",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontSize: 13 }}>
            Saved as config <strong>v{savedVersion}</strong>. Re-run with the same
            file to continue.
          </span>
          <button
            className="btn primary"
            onClick={() =>
              navigate(`/runs/new?from=${encodeURIComponent(runId)}&config=${savedVersion}`)
            }
          >
            <Icons.refresh size={14} stroke="#fff" /> Re-run with v{savedVersion}
          </button>
        </div>
      )}
    </div>
  );
}

// ---------- Confusion-matrix cell ----------
function ConfCell({ n, good, warn, note }) {
  const bg = good
    ? "var(--green-50)"
    : warn
      ? "var(--amber-50)"
      : "var(--ti-red-50)";
  const fg = good
    ? "var(--green)"
    : warn
      ? "var(--amber)"
      : "var(--ti-red)";
  return (
    <div
      style={{
        padding: 12,
        background: bg,
        border: `1px solid ${bg}`,
        borderRadius: 5,
        textAlign: "center",
      }}
    >
      <div
        className="mono"
        style={{ fontSize: 22, fontWeight: 600, color: fg }}
      >
        {n}
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        {note}
      </div>
    </div>
  );
}

// ---------- Record KPI strip ----------
// Track cards come from the profile, so a profile with different tracks gets
// its own cards without a change here. Count keys come from trackCountKey().
function RecordKpis({ c }) {
  const tracks = useProfile().tracks || [];
  const share = (n) =>
    fmtPct(c.recordsTotal > 0 ? (n || 0) / c.recordsTotal : 0, 1) + " of records";

  return (
    <div className="kpi-grid">
      <div className="kpi">
        <div className="label">Records</div>
        <div className="value">{fmtNumber(c.recordsTotal)}</div>
        <div className="delta muted">{fmtNumber(c.inputRows)} rows in the file</div>
      </div>
      {tracks.map((t) => (
        <div className="kpi" key={t.key}>
          <div className="label">{t.label}</div>
          <div className="value">{fmtNumber(c[trackCountKey(t.key)])}</div>
          <div className="delta muted">{share(c[trackCountKey(t.key)])}</div>
        </div>
      ))}
      <div className="kpi">
        <div className="label">Already labelled</div>
        <div className="value">{fmtNumber(c.recordsLabelled)}</div>
        <div className="delta muted">decided in an earlier round</div>
      </div>
      <div className="kpi">
        <div className="label">Unreviewed</div>
        <div className="value" style={{ color: "var(--amber)" }}>
          {fmtNumber(c.recordsUnreviewed)}
        </div>
        <div className="delta muted">no decision yet</div>
      </div>
      <div className="kpi">
        <div className="label">Input rows dropped</div>
        <div
          className="value"
          style={c.inputRowsDropped ? { color: "var(--ti-red)" } : undefined}
        >
          {fmtNumber(c.inputRowsDropped)}
        </div>
        <div className="delta muted">unusable rows in the upload</div>
      </div>
    </div>
  );
}

// ---------- Exact-key KPI strip ----------
// The second row on the summary once the exact keys have run: what merged, what
// is waiting for a human, and how the result sits against the earlier labels.
function ExactKpis({ c, onConflicts }) {
  const conflicts = c.exactConflicts || 0;
  return (
    <div className="kpi-grid">
      <div className="kpi">
        <div className="label">Entities after exact keys</div>
        <div className="value">{fmtNumber(c.exactEntitiesAfter)}</div>
        <div className="delta muted">was {fmtNumber(c.recordsTotal)} records</div>
      </div>
      <div className="kpi">
        <div className="label">Records merged</div>
        <div className="value">{fmtNumber(c.exactMergedRecords)}</div>
        <div className="delta muted">
          into {fmtNumber(c.exactMergedGroups)} group{c.exactMergedGroups === 1 ? "" : "s"}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Held for review</div>
        <div className="value" style={{ color: "var(--amber)" }}>
          {fmtNumber(c.exactHeldGroups)}
        </div>
        <div className="delta muted">
          {fmtNumber(c.exactHeldRecords)} records a guard stopped
        </div>
      </div>
      <div
        className="kpi"
        onClick={conflicts > 0 ? onConflicts : undefined}
        style={conflicts > 0 ? { cursor: "pointer" } : undefined}
        title={conflicts > 0 ? "Open the Exact groups tab, filtered to conflicts" : undefined}
      >
        <div className="label">Conflicts with earlier labels</div>
        <div className="value" style={conflicts > 0 ? { color: "var(--ti-red)" } : undefined}>
          {fmtNumber(conflicts)}
        </div>
        <div className="delta muted">
          {conflicts > 0 ? "groups joining different entity IDs — open them" : "no group joins two entity IDs"}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Pair precision</div>
        <div className="value">{fmtPct(c.exactPairPrecision, 1)}</div>
        <div className="delta muted">of pairs these keys join, the manual work agreed</div>
      </div>
      <div className="kpi">
        <div className="label">Pair recall</div>
        <div className="value">{fmtPct(c.exactPairRecall, 1)}</div>
        <div className="delta muted">of pairs the manual work joined, these keys find</div>
      </div>
    </div>
  );
}

function normalizeRun(r) {
  if (!r) return r;
  const dur = r.duration_secs;
  let duration = r.duration || "—";
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
    label: r.label || r.input_filename || "Run",
    counts: r.counts || null,
  };
}

// A run that only loaded records has no pairs yet, so everything the linkage
// pipeline produced — review queue, ambiguous cases, match exports — is absent.
// hasPairCounts / hasRecordCounts (../counts) read the flags the API returns.

function formatTimelineEvent(e) {
  const event = e.event || e.type || "";
  const stage = e.stage_name || e.stage || "";
  const message = e.message || e.msg || "";
  if (message) return message;
  if (event === "stage_start") return `Started ${stage || "pipeline stage"}`;
  if (event === "stage_end") return `Completed ${stage || "pipeline stage"}`;
  if (event === "complete") return "Pipeline completed";
  if (event === "failed") return e.error || "Pipeline failed";
  return event.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

// ---------- Summary tab ----------
class PanelErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidUpdate(prevProps) {
    if (prevProps.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  render() {
    if (this.state.error) {
      return (
        <Empty
          title="This panel could not render"
          sub={this.state.error.message || String(this.state.error)}
        />
      );
    }
    return this.props.children;
  }
}

function RunSummary({ run, onReview, onConflicts }) {
  const c = run.counts;
  const [diagData, setDiagData] = useState(null);
  const [labelStats, setLabelStats] = useState(null);
  const [modelStatus, setModelStatus] = useState(null);

  useEffect(() => {
    if (run && run.id) {
      api.getRunDiagnostics(run.id)
        .then((data) => setDiagData(data))
        .catch(() => setDiagData(null));
      api.modelStatus().then(setModelStatus).catch(() => setModelStatus(null));
      api.listLabels({ active: 1, run_id: run.id, per_page: 1 })
        .then((data) => {
          const forRun = data.total || 0;
          return api.listLabels({ active: 1, per_page: 1 }).then((all) => {
            setLabelStats({ forRun, total: all.total || 0 });
          });
        })
        .catch(() => setLabelStats(null));
    }
  }, [run?.id]);

  const pairs = hasPairCounts(c);
  const records = hasRecordCounts(c);

  if (!c || (!pairs && !records)) {
    return (
      <Empty
        title="No match data"
        sub={run.error || "This run did not produce match results."}
      />
    );
  }

  // A run that only loaded records: no pair panels to draw, so say what there
  // is and point at the Records tab.
  if (!pairs) {
    const exact = hasExactCounts(c);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <RecordKpis c={c} />
        {exact && <ExactKpis c={c} onConflicts={onConflicts} />}
        <div className="card">
          <div className="card-h">
            <Icons.table size={16} />
            <h3>{exact ? "What this run did" : "Records loaded"}</h3>
          </div>
          <div className="card-b">
            <p className="muted" style={{ fontSize: 13, margin: 0, lineHeight: 1.6 }}>
              {exact ? (
                <>
                  This run read the input file, sorted every record into a track, cleaned it with
                  the config's rules, and merged records that share a match key. Open the{" "}
                  <strong>Records</strong> tab to read the records, or{" "}
                  <strong>Exact groups</strong> to see what merged and what a guard held back.
                  Scoring, the review queue and durable entity IDs are not built yet.
                </>
              ) : (
                <>
                  This run read the input file and sorted every record into a track. Open the{" "}
                  <strong>Records</strong> tab to read them. Matching, the review queue and
                  entity IDs are not built yet.
                </>
              )}
            </p>
          </div>
        </div>
      </div>
    );
  }

  const total = c.exact + c.probAccept + c.review + c.ambiguous;
  const segs = [
    { label: "Exact", value: c.exact, color: "var(--blue)", key: "exact" },
    {
      label: "Auto-accept",
      value: c.probAccept,
      color: "var(--green)",
      key: "auto",
    },
    {
      label: "Review",
      value: c.review,
      color: "var(--amber)",
      key: "review",
    },
    {
      label: "Ambiguous",
      value: c.ambiguous,
      color: "var(--violet)",
      key: "amb",
    },
  ];

  // Which model actually DECIDED this run. Prefer the authoritative per-run field
  // (decision_model, recorded in run metadata), falling back to the diagnostics
  // score_column for legacy runs recorded before that field existed.
  const decisionModel = c.decisionModel; // 'splink' | 'gbt:<version>' | null
  const gbtDecides = decisionModel
    ? decisionModel.startsWith("gbt")
    : diagData?.score_column === "gbt_score";
  const decisionVersion =
    c.decisionModelVersion ??
    (decisionModel && decisionModel.includes(":") ? decisionModel.split(":")[1] : null);
  const gbtTrained = !!modelStatus?.exists;
  const gm = modelStatus?.metrics;
  const gbtUnvalidated =
    gm && (gm.auc === 1 || gm.brier_calibrated === 0 ||
           (gm.eval_source !== "held_out" && gm.eval_source !== "oof_train"));
  const decision = gbtDecides
    ? {
        label: `Decision score: GBT (calibrated)${decisionVersion ? ` · model v${decisionVersion}` : ""}`,
        cls: gbtUnvalidated ? "amber" : "green",
        note: gbtUnvalidated
          ? "⚠ unvalidated — too small or separable"
          : gm?.eval_source === "oof_train"
            ? "validated out-of-fold (no separate held-out set)"
            : null,
      }
    : {
        label: "Decision score: Splink (clumpy)",
        cls: c.gbtWarning ? "amber" : "",
        note: c.gbtWarning
          ? c.gbtWarning
          : gbtTrained
            ? "a GBT is trained but not applied to this run"
            : "no GBT trained",
      };

  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 20 }}
    >
      {/* Record counts, when this run also recorded them */}
      {records && <RecordKpis c={c} />}

      {/* KPI strip */}
      <div className="kpi-grid">
        <div className="kpi">
          <div className="label">OCOD rows in</div>
          <div className="value">{fmtNumber(c.ocod)}</div>
          <div className="delta muted">{fmtNumber(c.roe)} ROE rows</div>
        </div>
        <div className="kpi">
          <div className="label">Total matched (titles)</div>
          <div className="value">
            {fmtNumber(c.matchedTitles || c.exact + c.probAccept)}
          </div>
          <div className="delta up">
            {fmtPct(c.matchRate, 2)} of OCOD
          </div>
        </div>
        <div className="kpi">
          <div className="label">Pending review</div>
          <div className="value" style={{ color: "var(--amber)" }}>
            {fmtNumber(c.review)}
          </div>
          <div className="delta muted">
            {fmtNumber(c.ambiguous)} ambiguous
          </div>
        </div>
        <div className="kpi">
          <div className="label">Phase split</div>
          <div className="value">
            <span style={{ fontSize: 22, color: "var(--blue)" }}>
              {c.exact + c.probAccept > 0
                ? Math.round(
                    (c.exact / (c.exact + c.probAccept)) * 100
                  )
                : 0}
              %
            </span>
            <span
              className="muted"
              style={{ fontSize: 14, marginLeft: 6 }}
            >
              exact
            </span>
          </div>
          <div className="delta muted">
            Phase 2 added {fmtNumber(c.probAccept)}
          </div>
        </div>
      </div>

      {/* Stages strip */}
      <div className="card">
        <div className="card-h">
          <Icons.bolt size={16} />
          <h3>Pipeline stages</h3>
          <div className="actions">
            <span className={`tag ${decision.cls}`}>
              <span className="dot" />{decision.label}
            </span>
            {decision.note && (
              <span className="muted" style={{ fontSize: 11 }}>{decision.note}</span>
            )}
          </div>
        </div>
        <div className="card-b">
          <div className="stages">
            {(run.stages || [
              {
                num: 0,
                name: "Preprocess",
                status: "done",
                meta: "",
              },
              {
                num: 1,
                name: "Phase 1 — Exact",
                status: "done",
                meta: `${fmtNumber(c.exact)} matches`,
              },
              {
                num: 2,
                name: "Phase 2 — Splink",
                status: "done",
                meta: `${fmtNumber(c.probAccept + c.review + c.ambiguous)} candidates scored (unsupervised)`
                  + (c.droppedBelowReview > 0 ? ` · ${fmtNumber(c.droppedBelowReview)} below review floor` : ""),
              },
              {
                num: "2.5",
                name: "GBT re-score",
                status: gbtDecides ? "done" : "skip",
                meta: gbtDecides
                  ? "applied · decides this run (calibrated)"
                  : gbtTrained
                    ? "trained but NOT applied — this run decided on Splink"
                    : "not trained — pipeline ran on Splink",
              },
              {
                num: 3,
                name: "Evaluate & Export",
                status: "done",
                meta: "",
              },
              {
                num: null,
                name: "Apply labels",
                status: "done",
                meta: labelStats
                  ? `${fmtNumber(c.labelsApplied || 0)} of your saved answers applied`
                    + (c.labelsUnmatched > 0 ? ` · ${fmtNumber(c.labelsUnmatched)} didn't match this run's data` : "")
                    + ` · ${fmtNumber(labelStats.total)} saved in total`
                  : c.labelsApplied > 0
                    ? `${fmtNumber(c.labelsApplied)} of your saved answers applied`
                      + (c.labelsUnmatched > 0 ? ` · ${fmtNumber(c.labelsUnmatched)} didn't match this run's data` : "")
                    : "",
              },
            ]).map((s) => (
              <div
                key={s.num ?? s.name}
                className={`stage ${s.status || "done"}`}
              >
                <div className="st-num">
                  {s.num != null ? `${s.num} · ` : "POST · "}{(s.status || "done").toUpperCase()}
                </div>
                <div className="st-name">{s.name}</div>
                {s.meta && (
                  <div className="st-meta">{s.meta}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Land-title outcome — counted per land title (OCOD rows), not per owner */}
      <div className="card">
        <div className="card-h">
          <Icons.spark size={16} />
          <h3>Land-title outcome</h3>
          <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
            counted per land title, not per owner
          </span>
        </div>
        <div className="card-b">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 28 }}>
            {(() => {
              const matched = c.matchedTitles || 0;
              const unmatchedT = c.unmatchedTitles ?? Math.max(0, c.ocod - matched);
              const exactT = c.matchedTitlesExact ?? 0;
              const fuzzyT = Math.max(0, matched - exactT);
              const formerT = c.matchedTitlesFormer ?? 0;
              const roeNoMatch = c.unmatchedRoe ?? 0;
              return [
                { label: "Total land titles", value: c.ocod, sub: null },
                { label: "Titles with a matched owner", value: matched, color: "var(--green)",
                  sub: `${fmtPct(c.matchRate, 1)} of titles`
                    + (c.matchedTitlesExact != null ? ` · ${fmtNumber(exactT)} exact, ${fmtNumber(fuzzyT)} fuzzy/confirmed` : "")
                    + (formerT > 0 ? ` · incl. ${fmtNumber(formerT)} via former name` : "") },
                { label: "Titles with no confirmed owner", value: unmatchedT, color: "var(--ti-red)",
                  sub: `${fmtPct(c.ocod > 0 ? unmatchedT / c.ocod : 0, 1)} of titles · incl. any in review` },
                { label: "Total ROE companies", value: c.roe, sub: null },
                { label: "ROE companies with no match", value: roeNoMatch,
                  sub: `${fmtPct(c.roe > 0 ? roeNoMatch / c.roe : 0, 1)} of ROE · sold up, owns outside E&W, or the link failed` },
              ].map((s) => (
                <div key={s.label} style={{ minWidth: 150 }}>
                  <div className="muted" style={{ fontSize: 12 }}>{s.label}</div>
                  <div className="mono" style={{ fontSize: 22, fontWeight: 650, color: s.color }}>
                    {fmtNumber(s.value)}
                  </div>
                  {s.sub && <div className="muted" style={{ fontSize: 11 }}>{s.sub}</div>}
                </div>
              ));
            })()}
          </div>
          <p className="muted" style={{ fontSize: 11.5, marginTop: 14, lineHeight: 1.5 }}>
            Title-level: <strong>matched + unmatched = total titles</strong> (a company owning 5
            properties counts as 5). The “Outcome composition” bar below counts <strong>per owner</strong>
            (de-duplicated proprietors), so its total differs from the title counts here — and the
            downloadable <code>unmatched_ocod.csv</code> is per-owner too.
          </p>
        </div>
      </div>

      {/* Before vs after human labels */}
      {c.preLabels && (
        <div className="card">
          <div className="card-h">
            <h3>Before and after your labels</h3>
            <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
              what the matcher decided vs what the export says
            </span>
          </div>
          <div className="card-b">
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr className="muted" style={{ fontSize: 12, textAlign: "left" }}>
                  <th style={{ padding: "4px 0" }}>Measure</th>
                  <th style={{ padding: "4px 0", textAlign: "right" }}>Model only</th>
                  <th style={{ padding: "4px 0", textAlign: "right" }}>After labels</th>
                  <th style={{ padding: "4px 0", textAlign: "right" }}>Change</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Titles with a matched owner", c.preLabels.matchedTitles, c.matchedTitles],
                  ["Titles with no confirmed owner", c.preLabels.unmatchedTitles, c.unmatchedTitles],
                  ["Distinct entities identified", c.preLabels.distinctEntitiesIdentified, c.distinctEntitiesIdentified],
                ].map(([label, before, after]) => {
                  const delta = (after ?? 0) - (before ?? 0);
                  return (
                    <tr key={label} style={{ borderTop: "1px solid var(--border)" }}>
                      <td style={{ padding: "7px 0" }}>{label}</td>
                      <td className="mono" style={{ padding: "7px 0", textAlign: "right" }}>{fmtNumber(before ?? 0)}</td>
                      <td className="mono" style={{ padding: "7px 0", textAlign: "right", fontWeight: 650 }}>{fmtNumber(after ?? 0)}</td>
                      <td className="mono" style={{ padding: "7px 0", textAlign: "right",
                        color: delta === 0 ? "var(--muted)" : delta > 0 ? "var(--green)" : "var(--ti-red)" }}>
                        {delta > 0 ? "+" : ""}{fmtNumber(delta)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 11.5, marginTop: 12, lineHeight: 1.5 }}>
              “Model only” is what Stage 3 produced before any human label touched the export.
              “After labels” is what <code>merged_dataset.csv</code> now contains — {fmtNumber(c.labelsApplied || 0)} label
              {(c.labelsApplied || 0) === 1 ? "" : "s"} applied
              {(c.labelsUnmatched || 0) > 0 ? `, ${fmtNumber(c.labelsUnmatched)} that did not resolve onto this run` : ""}.
            </p>
          </div>
        </div>
      )}

      {/* Outcome composition bar */}
      <div className="card">
        <div className="card-h">
          <Icons.spark size={16} />
          <h3>Outcome composition</h3>
          <div className="actions">
            <span
              className="muted"
              style={{ fontSize: 12 }}
            >
              Candidate pairs scored (per owner):
            </span>{" "}
            <span className="mono" style={{ fontSize: 13 }}>
              {fmtNumber(total)}
            </span>
          </div>
        </div>
        <div className="card-b">
          <div
            style={{
              display: "flex",
              height: 32,
              borderRadius: 6,
              overflow: "hidden",
              border: "1px solid var(--line)",
            }}
          >
            {segs.map((s) => (
              <div
                key={s.key}
                style={{
                  width: `${total > 0 ? (s.value / total) * 100 : 0}%`,
                  background: s.color,
                  color: "#fff",
                  fontSize: 12,
                  fontWeight: 600,
                  display: "grid",
                  placeItems: "center",
                  minWidth: s.value > 0 ? 24 : 0,
                }}
              >
                {total > 0 && s.value / total > 0.04
                  ? fmtNumber(s.value)
                  : ""}
              </div>
            ))}
          </div>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 16,
              marginTop: 12,
            }}
          >
            {segs.map((s) => (
              <div
                key={s.key}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span
                  style={{
                    width: 10,
                    height: 10,
                    background: s.color,
                    borderRadius: 2,
                  }}
                />
                <span style={{ fontSize: 12.5 }}>{s.label}</span>
                <span
                  className="mono muted"
                  style={{ fontSize: 12 }}
                >
                  {fmtNumber(s.value)} &middot;{" "}
                  {fmtPct(total > 0 ? s.value / total : 0, 1)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Side-by-side: Top jurisdictions + Top name rules */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 16,
        }}
      >
        <div className="card">
          <div className="card-h">
            <h3>Top jurisdictions by matches</h3>
          </div>
          <div className="card-b">
            {diagData && diagData.top_jurisdictions && diagData.top_jurisdictions.length > 0 ? (
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th>Jurisdiction</th>
                    <th style={{ textAlign: "right" }}>Matches</th>
                  </tr>
                </thead>
                <tbody>
                  {diagData.top_jurisdictions.map((j, i) => (
                    <tr key={i}>
                      <td style={{ fontSize: 12.5 }}>{j.jurisdiction}</td>
                      <td className="mono" style={{ textAlign: "right", fontSize: 12.5 }}>
                        {fmtNumber(j.count)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted" style={{ fontSize: 13, padding: "12px 0" }}>
                No data available.
              </p>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-h">
            <h3>Most-applied name rules</h3>
          </div>
          <div className="card-b">
            {diagData && diagData.top_rules && diagData.top_rules.length > 0 ? (
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th style={{ textAlign: "right" }}>Hits</th>
                    <th>Effect</th>
                  </tr>
                </thead>
                <tbody>
                  {diagData.top_rules.map((r, i) => (
                    <tr key={i}>
                      <td style={{ fontSize: 12.5 }}>{r.rule}</td>
                      <td className="mono" style={{ textAlign: "right", fontSize: 12.5 }}>
                        {fmtNumber(r.hits)}
                      </td>
                      <td className="muted" style={{ fontSize: 12 }}>{r.effect}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted" style={{ fontSize: 13, padding: "12px 0" }}>
                No data available.
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Call out: needs attention */}
      {c.review > 0 && (
        <div
          className="card"
          style={{
            borderColor: "var(--amber)",
            background: "var(--amber-50)",
          }}
        >
          <div
            style={{
              padding: "14px 16px",
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            <Icons.alert size={20} stroke="var(--amber)" />
            <div style={{ flex: 1 }}>
              <div
                style={{ fontWeight: 600, color: "var(--amber)" }}
              >
                {c.review} pairs in the review band
                {c.ambiguous > 0 &&
                  ` — and ${c.ambiguous} ambiguous cases to disambiguate.`}
              </div>
              <div
                style={{
                  color: "var(--ink-2)",
                  fontSize: 12.5,
                }}
              >
                {labelStats && labelStats.forRun > 0
                  ? `${fmtNumber(labelStats.forRun)} labelled so far. Apply labels to update exports, or continue reviewing.`
                  : "Open the review queue to label matches as TRUE or FALSE."}
              </div>
            </div>
            <button className="btn primary" onClick={onReview}>
              Start reviewing
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Diagnostics tab ----------
function RunDiagnostics({ runId }) {
  const [diag, setDiag] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .getRunDiagnostics(runId)
      .then((data) => setDiag(data))
      .catch(() => setDiag(null))
      .finally(() => setLoading(false));
  }, [runId]);

  if (loading) {
    return (
      <p
        className="muted pulse"
        style={{ fontSize: 13.5, padding: 40 }}
      >
        Loading diagnostics...
      </p>
    );
  }
  if (!diag) {
    return (
      <Empty
        title="No diagnostics available"
        sub="Diagnostics were not generated for this run."
      />
    );
  }

  const hist = diag.histogram || diag.hist || [];
  const maxBin = Math.max(...hist, 1);
  const thresholdHigh = +(diag.thresholds?.threshold_high ?? 0.92);
  const thresholdReview = +(diag.thresholds?.threshold_review ?? 0.5);
  const scoreColumn = diag.score_column === "gbt_score" ? "GBT score" : "Splink probability";
  const pct = (v) => `${Math.max(0, Math.min(1, v)) * 100}%`;
  const features = (diag.features || [
    {
      lab: "name_jw",
      v: 0.92,
      info: "Jaro-Winkler on cleaned name",
    },
    { lab: "name_core", v: 0.88, info: "Entity-suffix stripped" },
    {
      lab: "tokens_sorted",
      v: 0.81,
      info: "Word-order invariant",
    },
    { lab: "digits", v: 0.74, info: "Set equality of digits" },
    {
      lab: "jurisdiction",
      v: 1.0,
      info: "Hard-equal after canonicalisation",
    },
  ]).filter((f) => f.lab !== "suffix_norm");

  const confusion = diag.confusion || null;
  const examples = diag.cleaning_examples || diag.examples || [];

  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 16 }}
    >
      {/* Probability histogram */}
      <div className="card">
        <div className="card-h">
          <Icons.spark size={16} />
          <h3>Probability distribution</h3>
          <div
            className="actions muted"
            style={{ fontSize: 12 }}
          >
            {scoreColumn} &middot; {hist.length} bins,
            log-scaled height
          </div>
        </div>
        <div className="card-b">
          <div style={{ position: "relative" }}>
            <div className="histo">
              {hist.map((v, i) => {
                const h =
                  (Math.log(v + 1) / Math.log(maxBin + 1)) * 100;
                let cls = "b";
                const binStart = i / hist.length;
                const binEnd = (i + 1) / hist.length;
                const binMid = (i + 0.5) / hist.length;
                if (binMid >= thresholdHigh) cls += " exact";
                else if (binMid >= thresholdReview) cls += " review";
                else cls += " auto-no";
                return (
                  <div
                    key={i}
                    className={cls}
                    style={{ height: `${h}%` }}
                    title={`bin ${binStart.toFixed(2)}-${binEnd.toFixed(2)}: ${fmtNumber(v)}`}
                  />
                );
              })}
            </div>
            <div
              className="threshold-band"
              style={{ marginTop: 16 }}
            >
              <span
                style={{
                  position: "absolute",
                  left: 4,
                  top: "50%",
                  transform: "translateY(-50%)",
                  fontSize: 11,
                }}
              >
                below scored floor (&lt;{thresholdReview.toFixed(2)})
              </span>
              <span
                style={{
                  position: "absolute",
                  left: pct((thresholdReview + thresholdHigh) / 2),
                  top: "50%",
                  transform: "translateY(-50%)",
                  fontSize: 11,
                }}
              >
                review band
              </span>
              <span
                style={{
                  position: "absolute",
                  right: 6,
                  top: "50%",
                  transform: "translateY(-50%)",
                  fontSize: 11,
                }}
              >
                {`auto-accept (>=${thresholdHigh.toFixed(2)})`}
              </span>
              <div
                className="pin"
                style={{ left: pct(thresholdReview) }}
                data-label={thresholdReview.toFixed(2)}
              />
              <div
                className="pin"
                style={{ left: pct(thresholdHigh) }}
                data-label={thresholdHigh.toFixed(2)}
              />
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 11,
                color: "var(--muted)",
                fontFamily: "var(--font-mono)",
                marginTop: 22,
              }}
            >
              <span>0.00</span>
              <span>0.25</span>
              <span>0.50</span>
              <span>0.75</span>
              <span>1.00</span>
            </div>
            <p className="muted" style={{ fontSize: 12, margin: "10px 0 0" }}>
              Stage 2 only emits candidate pairs at or above the review floor. To inspect
              weaker possible matches, lower the review floor and run the pipeline again.
            </p>
          </div>
        </div>
      </div>

      {/* GBT model: train / apply / active learning.
          (Threshold tuning + mark-by-range now live inline on the Review queue.) */}
      <ModelPanel runId={runId} />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 16,
        }}
      >
        {/* Feature contribution */}
        <div className="card">
          <div className="card-h">
            <h3>Feature contribution (Splink m-values)</h3>
          </div>
          <div className="card-b">
            <div className="features">
              {features.map((f) => (
                <div className="ft" key={f.lab}>
                  <div className="lab">
                    <div
                      className="mono"
                      style={{ fontSize: 12.5 }}
                    >
                      {f.lab}
                    </div>
                    <div
                      className="muted"
                      style={{ fontSize: 11 }}
                    >
                      {f.info}
                    </div>
                  </div>
                  <div className="bar">
                    <i
                      style={{
                        width: `${f.v * 100}%`,
                      }}
                    />
                  </div>
                  <div className="val">{f.v.toFixed(2)}</div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Confusion matrix — requires dedicated endpoint */}
        <div className="card">
          <div className="card-h">
            <h3>Confusion (vs prior labels re-applied)</h3>
          </div>
          <div className="card-b">
            {confusion ? (
              <>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "auto 1fr 1fr",
                    gap: 4,
                    fontSize: 12.5,
                  }}
                >
                  <div></div>
                  <div
                    style={{
                      textAlign: "center",
                      color: "var(--muted)",
                      fontWeight: 600,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Prior TRUE
                  </div>
                  <div
                    style={{
                      textAlign: "center",
                      color: "var(--muted)",
                      fontWeight: 600,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Prior FALSE
                  </div>

                  <div
                    style={{
                      alignSelf: "center",
                      color: "var(--muted)",
                      fontWeight: 600,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Now &gt;= {thresholdHigh.toFixed(2)}
                  </div>
                  <ConfCell
                    n={confusion.tp}
                    good
                    note="re-confirmed"
                  />
                  <ConfCell
                    n={confusion.fp}
                    warn
                    note="model says TRUE; prior FALSE"
                  />

                  <div
                    style={{
                      alignSelf: "center",
                      color: "var(--muted)",
                      fontWeight: 600,
                      fontSize: 11,
                      textTransform: "uppercase",
                      letterSpacing: "0.06em",
                    }}
                  >
                    Now &lt; {thresholdHigh.toFixed(2)}
                  </div>
                  <ConfCell
                    n={confusion.fn}
                    warn
                    note="prior TRUE; below threshold now"
                  />
                  <ConfCell
                    n={confusion.tn}
                    good
                    note="re-confirmed FALSE"
                  />
                </div>
                {confusion.fp > 0 && (
                  <div
                    className="muted"
                    style={{ fontSize: 11.5, marginTop: 10 }}
                  >
                    {confusion.fp} disagreements where the new
                    model now says TRUE for previously-rejected pairs.
                    Worth a look.
                  </div>
                )}
              </>
            ) : (
              <p className="muted" style={{ fontSize: 13, padding: "12px 0" }}>
                Confusion matrix requires comparing against prior labels and will be available in a future update.
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Cleaning examples */}
      {examples.length > 0 && (
        <div className="card">
          <div className="card-h">
            <h3>Cleaning pipeline &middot; examples</h3>
          </div>
          <div className="card-b" style={{ padding: 0 }}>
            <table className="t" style={{ borderRadius: 0 }}>
              <thead>
                <tr>
                  <th>Raw</th>
                  <th>After whitespace</th>
                  <th>After punctuation</th>
                  <th>After suffix family</th>
                  <th>Final clean_name</th>
                </tr>
              </thead>
              <tbody>
                {examples.map((row, i) => {
                  const cells = Array.isArray(row)
                    ? row
                    : [
                        row.raw,
                        row.whitespace,
                        row.punctuation,
                        row.suffix,
                        row.final,
                      ];
                  return (
                    <tr key={i}>
                      {cells.map((cell, j) => (
                        <td
                          key={j}
                          className="mono"
                          style={{ fontSize: 11.5 }}
                        >
                          {cell}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Files tab ----------
function RunFiles({ runId }) {
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .getRunFiles(runId)
      .then((data) => setFiles(Array.isArray(data) ? data : data.files || []))
      .catch(() => setFiles([]))
      .finally(() => setLoading(false));
  }, [runId]);

  if (loading) {
    return (
      <p
        className="muted pulse"
        style={{ fontSize: 13.5, padding: 40 }}
      >
        Loading files...
      </p>
    );
  }

  if (files.length === 0) {
    return (
      <Empty
        title="No output files"
        sub="This run has not produced any output files yet."
      />
    );
  }

  return (
    <div className="card">
      <div className="card-h">
        <Icons.file size={16} />
        <h3>Output files</h3>
        <div className="actions">
          <a className="btn" href={api.runAllFilesUrl(runId)} download>
            <Icons.export size={14} />
            Download all (.zip)
          </a>
        </div>
      </div>
      <table className="t">
        <thead>
          <tr>
            <th>File</th>
            <th>Description</th>
            <th style={{ textAlign: "right" }}>Rows</th>
            <th style={{ textAlign: "right" }}>Size</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.name}>
              <td className="mono">
                <Icons.file size={13} />
                &nbsp;{f.name}
              </td>
              <td className="muted">{f.desc || f.description || ""}</td>
              <td
                className="mono"
                style={{ textAlign: "right" }}
              >
                {f.rows == null ? "—" : fmtNumber(f.rows)}
              </td>
              <td
                className="mono"
                style={{ textAlign: "right" }}
              >
                {f.size}
              </td>
              <td>
                <a
                  href={f.url ? apiUrl(f.url) : api.runFileUrl(runId, f.name)}
                  download
                  className="btn sm"
                >
                  <Icons.download size={12} />
                  Download
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------- History tab ----------
function RunHistory({ runId }) {
  const [timeline, setTimeline] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .getRunTimeline(runId)
      .then((data) =>
        setTimeline(Array.isArray(data) ? data : data.events || [])
      )
      .catch(() => setTimeline([]))
      .finally(() => setLoading(false));
  }, [runId]);

  if (loading) {
    return (
      <p
        className="muted pulse"
        style={{ fontSize: 13.5, padding: 40 }}
      >
        Loading timeline...
      </p>
    );
  }

  if (timeline.length === 0) {
    return (
      <Empty
        title="No timeline events"
        sub="No events have been recorded for this run."
      />
    );
  }

  return (
    <div className="card">
      <div className="card-h">
        <Icons.clock size={16} />
        <h3>Run timeline</h3>
      </div>
      <div className="card-b">
        <div className="timeline">
          {timeline.map((e, i) => (
            <div className="ev" key={i}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 12,
                }}
              >
                <span className="desc">{formatTimelineEvent(e)}</span>
                <span className="when">{e.at || e.timestamp || ""}</span>
              </div>
              {e.who && (
                <div
                  className="who muted"
                  style={{ fontSize: 11 }}
                >
                  {e.who}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------- Live progress panel (for in-progress runs) ----------
function RunProgress({ runId }) {
  const { events, currentStage, status, isConnected } =
    useRunProgress(runId);

  if (!isConnected && events.length === 0) return null;

  return (
    <div className="card" style={{ borderColor: "var(--blue)" }}>
      <div className="card-h">
        <Icons.bolt size={16} />
        <h3>Live progress</h3>
        <div className="actions">
          {isConnected ? (
            <span className="tag green">
              <span className="dot" />
              connected
            </span>
          ) : (
            <span className="tag muted">
              <span className="dot" />
              {status}
            </span>
          )}
        </div>
      </div>
      <div className="card-b">
        {currentStage !== null && (
          <div style={{ marginBottom: 8, fontWeight: 500 }}>
            Stage {currentStage}
          </div>
        )}
        <div
          className="timeline"
          style={{ maxHeight: 240, overflowY: "auto" }}
        >
          {events.map((e, i) => (
            <div className="ev" key={i}>
              <span className="desc">
                {e.message || e.event || e.raw || JSON.stringify(e)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------- Main screen ----------
export default function RunDetailScreen() {
  const { id } = useParams();
  const navigate = useNavigate();
  const profile = useProfile();
  const [run, setRun] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("summary");
  // Set when the summary's Conflicts card is clicked, so the Exact groups tab
  // opens already filtered.
  const [exactAgreement, setExactAgreement] = useState(null);

  useEffect(() => {
    setLoading(true);
    api
      .getRun(id)
      .then((data) => {
        setRun(normalizeRun(data));
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [id]);

  // Poll for status updates while run is in progress
  useEffect(() => {
    if (!run || (run.status !== "running" && run.status !== "pending" && run.status !== "queued")) return;
    const interval = setInterval(() => {
      api.getRun(id).then((data) => setRun(normalizeRun(data))).catch(() => {});
    }, 3000);
    return () => clearInterval(interval);
  }, [id, run?.status]);

  if (loading) {
    return (
      <div className="content">
        <p
          className="muted pulse"
          style={{ fontSize: 13.5, padding: 40 }}
        >
          Loading run...
        </p>
      </div>
    );
  }

  if (error || !run) {
    return (
      <div className="content">
        <Empty
          title="Run not found"
          sub={error || `Could not load run ${id}.`}
          action={
            <button
              className="btn primary"
              onClick={() => navigate("/runs")}
            >
              Back to runs
            </button>
          }
        />
      </div>
    );
  }

  const isRunning =
    run.status === "running" || run.status === "queued";
  const buckets = run.counts;
  // Match exports and the review queue only exist once the run has produced
  // pairs. Hide them otherwise rather than send the user to a 404.
  const pairs = hasPairCounts(run.counts);
  const exact = hasExactCounts(run.counts);

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <div
            className="muted"
            style={{ fontSize: 12, marginBottom: 6 }}
          >
            <a
              className="link"
              onClick={() => navigate("/runs")}
              style={{ cursor: "pointer" }}
            >
              &larr; All runs
            </a>
            <span style={{ margin: "0 6px" }}>&middot;</span>
            <span className="mono">{run.id}</span>
            <span style={{ margin: "0 6px" }}>&middot;</span>
            config{" "}
            <span
              className="tag"
              style={{ verticalAlign: "middle" }}
            >
              {run.config}
            </span>
          </div>
          <h1 className="page-title">{run.label}</h1>
          <p className="page-sub">
            {run.status === "complete" && run.finished && (
              <>
                Finished {timeAgo(run.finished)} &middot; ran for{" "}
                {run.duration} &middot; by {run.by} &middot;
                <span
                  className="tag green"
                  style={{ marginLeft: 8 }}
                >
                  <span className="dot" />
                  complete
                </span>
              </>
            )}
            {run.status === "failed" && (
              <>
                Failed &middot; by {run.by} &middot;
                <span
                  className="tag red"
                  style={{ marginLeft: 8 }}
                >
                  <span className="dot" />
                  failed
                </span>
              </>
            )}
            {isRunning && (
              <>
                Started {timeAgo(run.started)} &middot; by{" "}
                {run.by} &middot;
                <span
                  className="tag amber"
                  style={{ marginLeft: 8 }}
                >
                  <span className="dot" />
                  running
                </span>
              </>
            )}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={() => navigate(`/runs/new?from=${encodeURIComponent(id)}`)}>
            <Icons.refresh size={14} /> Re-run with{" "}
            {run.config}
          </button>
          {pairs && (
            <a
              className="btn"
              href={api.runFileUrl(id, "matches_final.csv")}
              download
              title="Only the rows that got a match — exact + high-confidence + the ones you confirmed — without the blanks."
            >
              <Icons.export size={14} /> Download matches only
            </a>
          )}
          {pairs && (
            <a
              className="btn"
              href={api.runFileUrl(id, "merged_dataset.csv")}
              download
              title="Every OCOD row, with its match where there is one and a blank where there isn't."
            >
              <Icons.export size={14} /> Export all rows (full)
            </a>
          )}
          {pairs && buckets.review > 0 && (
            <button
              className="btn primary"
              onClick={() => navigate(`/runs/${id}/review`)}
            >
              <Icons.review size={14} stroke="#fff" /> Open review
              queue ({buckets.review})
            </button>
          )}
          <button
            className="btn danger"
            onClick={() => {
              if (confirm(`Delete run ${run.id}? This removes all output files and cannot be undone.`)) {
                api.deleteRun(id).then(() => navigate("/runs")).catch(err => alert(err.message));
              }
            }}
          >
            <Icons.x size={14} /> Delete
          </button>
        </div>
      </div>

      {/* Self-serve fix for runs that failed on unmapped lookup values */}
      {run.status === "failed" &&
        run.error_detail?.kind === "unmapped_lookup_values" &&
        (run.error_detail.values?.length || 0) > 0 && (
          <UnmappedLookupValuesPanel run={run} runId={id} navigate={navigate} />
        )}

      {/* Tabs. Exact groups only appears once the key stage has run. */}
      <div className="tabs">
        {[
          { id: "summary", lab: "Summary" },
          { id: "records", lab: "Records" },
          ...(exact ? [{ id: "exact", lab: "Exact groups" }] : []),
          { id: "diagnostics", lab: "Diagnostics" },
          { id: "files", lab: "Files" },
          { id: "history", lab: "History" },
        ].map((t) => (
          <div
            key={t.id}
            className={"tab " + (tab === t.id ? "on" : "")}
            onClick={() => setTab(t.id)}
          >
            {t.lab}
          </div>
        ))}
      </div>

      {/* Live progress for in-progress runs */}
      {isRunning && <RunProgress runId={id} />}

      {tab === "summary" && (
        <RunSummary
          run={run}
          onReview={() => navigate(`/runs/${id}/review`)}
          onConflicts={() => {
            setExactAgreement("conflict");
            setTab("exact");
          }}
        />
      )}
      <PanelErrorBoundary resetKey={`${id}:${tab}`}>
        {tab === "records" && <RecordsTable runId={id} profile={profile} />}
        {tab === "exact" && (
          <ExactGroupsTable
            runId={id}
            profile={profile}
            initialAgreement={exactAgreement}
          />
        )}
        {tab === "diagnostics" && <RunDiagnostics runId={id} />}
        {tab === "files" && <RunFiles runId={id} />}
        {tab === "history" && <RunHistory runId={id} />}
      </PanelErrorBoundary>
    </div>
  );
}

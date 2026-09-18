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
import EntitiesTable from "../components/EntitiesTable";
import PublishPanel from "../components/PublishPanel";
import { useProfile } from "../profile";
import {
  hasEntityCounts,
  hasExactCounts,
  hasPairCounts,
  hasRecordCounts,
  trackCountKey,
} from "../counts";
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

// ---------- Blocking-budget failure panel ----------
// A run whose blocking rules would have made more pairs than the budget stops
// before Splink. The fix lives on the Thresholds & Splink tab, so the panel
// names the worst rule and takes the user there.
function BlockingBudgetPanel({ run, navigate }) {
  const detail = run.error_detail || {};
  const rules = Array.isArray(detail.rules) ? detail.rules : [];

  return (
    <div className="card" style={{ borderColor: "var(--ti-red-200, #f3c2c2)", marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <Icons.alert size={16} />
        <h3 style={{ margin: 0 }}>The blocking rules would have made too many pairs</h3>
      </div>
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
        On the <strong>{detail.track}</strong> track the rules would compare{" "}
        <span className="mono">{fmtNumber(detail.total)}</span> pairs against a budget of{" "}
        <span className="mono">{fmtNumber(detail.budget)}</span>, so the run stopped before scoring
        anything. Tighten the worst rule, or raise that track's pair budget.
      </p>
      {rules.length > 0 && (
        <div className="tbl-wrap" style={{ marginBottom: 10 }}>
          <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
            <thead>
              <tr>
                <th style={{ width: 70 }}>Rule</th>
                <th>What it blocks on</th>
                <th style={{ width: 140, textAlign: "right" }}>Pairs</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r, i) => (
                <tr key={r.id || i}>
                  <td className="mono">{r.id}</td>
                  <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
                    {r.description || <span className="muted">(no description)</span>}
                    {r.sql && (
                      <div className="mono muted" style={{ fontSize: 11 }}>
                        {r.sql}
                      </div>
                    )}
                  </td>
                  <td
                    className="mono tnum"
                    style={{
                      textAlign: "right",
                      color:
                        detail.budget != null && r.pairs > detail.budget ? "var(--ti-red)" : undefined,
                    }}
                  >
                    {fmtNumber(r.pairs)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <button className="btn primary" onClick={() => navigate("/config")}>
        <Icons.config size={14} stroke="#fff" /> Open Thresholds &amp; Splink
      </button>
    </div>
  );
}

// ---------- Contradictions callout ----------
// A reviewer said two records are not the same and an exact key merged them
// anyway. That is a rule bug, not a review task, so it gets said loudly.
function ContradictionsPanel({ runId, count }) {
  const [rows, setRows] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open || rows) return;
    api
      .getRunContradictions(runId)
      .then((d) => setRows(Array.isArray(d) ? d : d?.items || []))
      .catch(() => setRows([]));
  }, [open, rows, runId]);

  return (
    <div
      style={{
        background: "var(--ti-red-50)",
        border: "1px solid var(--ti-red)",
        borderRadius: 5,
        padding: "10px 14px",
        marginBottom: 16,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <Icons.alert size={15} />
        <span style={{ fontSize: 13 }}>
          <strong>{fmtNumber(count)}</strong> pair{count === 1 ? "" : "s"} an exact key merged
          although a reviewer had said they are not the same. A match key is too loose.
        </span>
        <button className="btn sm" style={{ marginLeft: "auto" }} onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Show them"}
        </button>
      </div>
      {open && (
        <div style={{ marginTop: 10 }}>
          {rows === null ? (
            <p className="muted pulse" style={{ fontSize: 12.5, margin: 0 }}>
              Loading…
            </p>
          ) : rows.length === 0 ? (
            <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
              Nothing to list.
            </p>
          ) : (
            <div className="tbl-wrap">
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th>Records a reviewer kept apart</th>
                    <th style={{ width: 150 }}>Merged by</th>
                    <th style={{ width: 160 }}>Group</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i}>
                      <td style={{ whiteSpace: "normal" }}>
                        {r.name_a || r.record_id_a} ↔ {r.name_b || r.record_id_b}
                        <div className="mono muted" style={{ fontSize: 11 }}>
                          {r.record_id_a} ↔ {r.record_id_b}
                        </div>
                      </td>
                      <td className="mono" style={{ fontSize: 12 }}>
                        {(r.key_ids || []).join(", ") || r.key_id || "—"}
                      </td>
                      <td className="mono" style={{ fontSize: 12 }}>
                        {r.group_id || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* How the proposed IDs line up with the earlier manual grouping. The backend
   adds this to score-eval, so the card only appears once it is there. */
const REASON_LABELS = {
  merged_two_earlier_groups: "Joined two earlier groups",
  split: "Split an earlier group",
  collision_re_mint: "Given a new ID after a collision",
};

function VersusEarlierIds({ scoreEval }) {
  const v = scoreEval?.versus_existing_entity_id;
  const entities = scoreEval?.entities;
  if (!v && !entities) return null;
  const reasons = Object.entries(v?.reasons || {});

  return (
    <div className="card">
      <div className="card-h">
        <Icons.branch size={16} />
        <h3>The proposed IDs against the earlier ones</h3>
        {entities && (
          <span className="muted" style={{ fontSize: 12 }}>
            pair precision {entities.pair_precision == null ? "—" : fmtPct(entities.pair_precision, 1)}{" "}
            · pair recall {entities.pair_recall == null ? "—" : fmtPct(entities.pair_recall, 1)}
          </span>
        )}
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {v && (
          <div style={{ fontSize: 13.5 }}>
            <strong>{fmtNumber(v.identical_records)}</strong> of{" "}
            {fmtNumber(v.labelled_records)} records that already had an ID keep exactly the same
            grouping. <strong>{fmtNumber(v.different_records)}</strong> are grouped differently.
          </div>
        )}
        {reasons.length > 0 && (
          <div className="tbl-wrap">
            <table className="t" style={{ borderRadius: 0 }}>
              <thead>
                <tr>
                  <th style={{ minWidth: 220 }}>Why it differs</th>
                  <th style={{ width: 110, textAlign: "right" }}>Records</th>
                </tr>
              </thead>
              <tbody>
                {reasons.map(([key, n]) => (
                  <tr key={key}>
                    <td>{REASON_LABELS[key] || key}</td>
                    <td className="mono tnum" style={{ textAlign: "right" }}>
                      {fmtNumber(n)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted" style={{ fontSize: 11.5, margin: 0, lineHeight: 1.5 }}>
          A record can appear under more than one reason, so these numbers do not add up to the
          records that differ. {v?.circular}
        </p>
      </div>
    </div>
  );
}

// ---------- Entity KPI strip ----------
// The last row on the summary: what stage 5 proposed, and what is still
// waiting for a person before the run can be published.
function EntityKpis({ c, onQueue }) {
  const collisions = c.idCollisions || 0;
  const ties = c.attributeTies || 0;
  return (
    <div className="kpi-grid">
      <div className="kpi">
        <div className="label">Entities proposed</div>
        <div className="value">{fmtNumber(c.entitiesProposed)}</div>
        <div className="delta muted">
          {fmtNumber(c.entitiesNew)} new · {fmtNumber(c.entitiesKept)} kept ·{" "}
          {fmtNumber(c.entitiesMerged)} merged
        </div>
      </div>
      <div
        className="kpi"
        onClick={onQueue}
        style={{ cursor: "pointer" }}
        title="Open the cluster review queue"
      >
        <div className="label">Withheld groups</div>
        <div className="value" style={{ color: "var(--amber)" }}>
          {fmtNumber(c.clustersWithheld)}
        </div>
        <div className="delta muted">the gate did not settle these</div>
      </div>
      <div className="kpi" onClick={onQueue} style={{ cursor: "pointer" }}>
        <div className="label">Held groups open</div>
        <div className="value" style={{ color: "var(--amber)" }}>
          {fmtNumber(c.heldGroupsOpen)}
        </div>
        <div className="delta muted">a match key guard stopped these</div>
      </div>
      <div className="kpi">
        <div className="label">Attribute ties</div>
        <div className="value" style={ties > 0 ? { color: "var(--violet)" } : undefined}>
          {fmtNumber(ties)}
        </div>
        <div className="delta muted">no single value was the most common</div>
      </div>
      <div className="kpi">
        <div className="label">ID collisions</div>
        <div className="value" style={collisions > 0 ? { color: "var(--ti-red)" } : undefined}>
          {fmtNumber(collisions)}
        </div>
        <div className="delta muted">
          {collisions > 0
            ? "two entities claimed one earlier ID, so one took a new one"
            : "no earlier ID was claimed twice"}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Decisions made</div>
        <div className="value">{fmtNumber(c.decisionsTotal)}</div>
        <div className="delta muted">groups a person has settled</div>
      </div>
    </div>
  );
}

// One line saying which score decided this run's pairs.
export function decidedBy(counts) {
  if (!counts?.modelActive) return "Splink score";
  const versions = counts.modelVersion || {};
  const list = Object.values(versions);
  const v = list.length === 1 ? `v${list[0]}` : list.map((n) => `v${n}`).join(" / ");
  return `Model ${v} (${counts.modelGraded ? "graded" : "cold start"})`;
}

// ---------- Scoring KPI strip ----------
// The third row on the summary, once stage 3 has scored the pairs.
function ScoreKpis({ c, onReview }) {
  const disagrees = c.pairsImportDisagrees || 0;
  return (
    <div className="kpi-grid">
      <div className="kpi">
        <div className="label">Decided by</div>
        <div className="value" style={{ fontSize: 17 }}>
          {decidedBy(c)}
        </div>
        <div className="delta muted">
          {c.modelActive
            ? c.modelGraded
              ? "the model sets the buckets"
              : "the model only re-orders the queue"
            : "no model applied to this run"}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Units compared</div>
        <div className="value">{fmtNumber(c.unitsTotal)}</div>
        <div className="delta muted">{fmtNumber(c.pairsScored)} pairs scored</div>
      </div>
      <div className="kpi">
        <div className="label">Auto-accepted</div>
        <div className="value" style={{ color: "var(--green)" }}>
          {fmtNumber(c.pairsAccept)}
        </div>
        <div className="delta muted">
          {fmtNumber(c.pairsDecidedByImport)} of them on an earlier entity ID
        </div>
      </div>
      <div className="kpi" onClick={onReview} style={{ cursor: "pointer" }} title="Open the review queue">
        <div className="label">To review</div>
        <div className="value" style={{ color: "var(--amber)" }}>
          {fmtNumber(c.pairsReview)}
        </div>
        <div className="delta muted">open the review queue</div>
      </div>
      <div className="kpi">
        <div className="label">Rejected</div>
        <div className="value">{fmtNumber(c.pairsReject)}</div>
        <div className="delta muted">below the review floor</div>
      </div>
      <div className="kpi">
        <div className="label">Entities after scoring</div>
        <div className="value">{fmtNumber(c.entitiesAfterScore)}</div>
        <div className="delta muted">was {fmtNumber(c.unitsTotal)} units</div>
      </div>
      <div className="kpi">
        <div className="label">Earlier labels disagree</div>
        <div className="value" style={disagrees > 0 ? { color: "var(--amber)" } : undefined}>
          {fmtNumber(disagrees)}
        </div>
        <div className="delta muted">a flag for sorting, never a decision</div>
      </div>
      <div className="kpi">
        <div className="label">Pair precision</div>
        <div className="value">
          {c.scorePairPrecision == null ? "—" : fmtPct(c.scorePairPrecision, 1)}
        </div>
        <div className="delta muted">of pairs this run joins, the manual work agreed</div>
      </div>
      <div className="kpi">
        <div className="label">Pair recall</div>
        <div className="value">
          {c.scorePairRecall == null ? "—" : fmtPct(c.scorePairRecall, 1)}
        </div>
        <div className="delta muted">of pairs the manual work joined, this run finds</div>
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

function RunSummary({ run, onReview, onConflicts, onQueue }) {
  const c = run.counts;
  const [diagData, setDiagData] = useState(null);
  const [labelStats, setLabelStats] = useState(null);
  const [scoreEval, setScoreEval] = useState(null);

  useEffect(() => {
    if (run && run.id) {
      api.getRunDiagnostics(run.id)
        .then((data) => setDiagData(data))
        .catch(() => setDiagData(null));
      api.getRunScoreEval(run.id).then(setScoreEval).catch(() => setScoreEval(null));
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

  // One summary for every stage a run has reached: records, then the exact
  // groups, then the scored pairs. Each strip appears once its stage has run.
  const exact = hasExactCounts(c);
  const entities = hasEntityCounts(c);
  const contradictions = c.labelContradictions || 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {contradictions > 0 && <ContradictionsPanel runId={run.id} count={contradictions} />}
      <RecordKpis c={c} />
      {exact && <ExactKpis c={c} onConflicts={onConflicts} />}
      {pairs && <ScoreKpis c={c} onReview={onReview} />}
      {entities && <EntityKpis c={c} onQueue={onQueue} />}
      {entities && <VersusEarlierIds scoreEval={scoreEval} />}
      <div className="card">
        <div className="card-h">
          <Icons.table size={16} />
          <h3>{exact || pairs ? "What this run did" : "Records loaded"}</h3>
          {pairs && (
            <div className="actions">
              <button className="btn primary" onClick={onReview}>
                <Icons.review size={14} stroke="#fff" /> Open the review queue
              </button>
            </div>
          )}
        </div>
        <div className="card-b">
          <p className="muted" style={{ fontSize: 13, margin: 0, lineHeight: 1.6 }}>
            {pairs ? (
              <>
                This run read the input file, sorted every record into a track, cleaned it, merged
                records that share a match key, and scored every pair the blocking rules let
                through. Open <strong>Review</strong> to answer the uncertain ones. Clustering and
                durable entity IDs are not built yet, so nothing is published from here.
              </>
            ) : exact ? (
              <>
                This run read the input file, sorted every record into a track, cleaned it with the
                config's rules, and merged records that share a match key. Open the{" "}
                <strong>Records</strong> tab to read the records, or <strong>Exact groups</strong>{" "}
                to see what merged and what a guard held back. Scoring, the review queue and
                durable entity IDs are not built yet.
              </>
            ) : (
              <>
                This run read the input file and sorted every record into a track. Open the{" "}
                <strong>Records</strong> tab to read them. Matching, the review queue and entity IDs
                are not built yet.
              </>
            )}
          </p>
        </div>
      </div>
    </div>
  );
}


/* The charts Splink writes for each track, under diagnostics/. They are whole
   HTML documents, so each one is opened in its own frame rather than injected
   into this page, and there is always a link to open it full size. */
const SPLINK_CHARTS = [
  {
    file: "match_weights",
    label: "Match weights",
    help: "How much each comparison level pushes a pair towards a match or away from one.",
  },
  {
    file: "m_u_parameters",
    label: "m and u values",
    help: "How often each level happens among real matches, and among pairs picked at random.",
  },
  {
    file: "score_distribution",
    label: "Score distribution",
    help: "Splink's own histogram of the scores it produced for this track.",
  },
];

function SplinkCharts({ runId, tracks }) {
  if (!tracks || tracks.length === 0) return null;

  return (
    <div className="card">
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Splink's own charts</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          written by the run, one set per track
        </span>
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th style={{ minWidth: 180 }}>Chart</th>
              <th>What it shows</th>
              {tracks.map((t) => (
                <th key={t.key} style={{ width: 130 }}>
                  {t.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {SPLINK_CHARTS.map((c) => (
              <tr key={c.file}>
                <td>{c.label}</td>
                <td className="muted" style={{ whiteSpace: "normal", fontSize: 12.5 }}>
                  {c.help}
                </td>
                {tracks.map((t) => (
                  <td key={t.key}>
                    <a
                      className="btn sm"
                      href={api.runFileUrl(runId, `diagnostics/${c.file}_${t.key}.html`)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <Icons.link size={12} /> Open
                    </a>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card-b">
        <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.55 }}>
          Each chart opens in a new tab. They are drawn by a charting library the page fetches from
          the internet, so a machine with no connection shows an empty chart. The same figures are
          in <span className="mono">splink_model_&lt;track&gt;.json</span> on the Files tab.
        </p>
      </div>
    </div>
  );
}

// ---------- Diagnostics tab ----------
function RunDiagnostics({ runId }) {
  const navigate = useNavigate();
  const profile = useProfile();
  const tracks = profile.tracks || [];
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
  // Whatever comparisons the run actually used. There is no stand-in list: made-up
  // weights from the ROE tool were worse than showing nothing.
  const features = (diag.features || []).filter((f) => f.lab !== "suffix_norm");

  const confusion = diag.confusion || null;
  const examples = diag.cleaning_examples || diag.examples || [];

  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 16 }}
    >
      {/* The review screen draws this distribution properly: every scored pair by
          bucket, the lines, the import split, the model score and a brush. One
          chart, in one place, rather than a second empty one here. */}
      <div className="card">
        <div className="card-h">
          <Icons.spark size={16} />
          <h3>Score distribution</h3>
        </div>
        <div className="card-b" style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <p className="muted" style={{ fontSize: 13, margin: 0, lineHeight: 1.55, flex: "1 1 340px" }}>
            The review queue draws the whole distribution: every scored pair by bucket, the accept
            and review lines, whether the earlier labels agree, and the model's score once one has
            been applied. You can drag a band there to label it.
          </p>
          <button className="btn" onClick={() => navigate(`/runs/${runId}/review`)}>
            <Icons.review size={14} /> Open the review queue
          </button>
        </div>
      </div>

      {/* GBT model: train / apply / active learning.
          (Threshold tuning + mark-by-range now live inline on the Review queue.) */}
      <ModelPanel runId={runId} />

      <SplinkCharts runId={runId} tracks={tracks} />

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
  const entities = hasEntityCounts(run.counts);

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

      {/* A run that never scored because its blocking rules were too loose */}
      {run.status === "failed" && run.error_detail?.kind === "blocking_budget" && (
        <BlockingBudgetPanel run={run} navigate={navigate} />
      )}

      {/* Tabs. Exact groups only appears once the key stage has run. */}
      <div className="tabs">
        {[
          { id: "summary", lab: "Summary" },
          { id: "records", lab: "Records" },
          ...(exact ? [{ id: "exact", lab: "Exact groups" }] : []),
          ...(entities ? [{ id: "entities", lab: "Entities" }] : []),
          ...(entities ? [{ id: "publish", lab: "Publish & export" }] : []),
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
          onQueue={() => navigate(`/runs/${id}/clusters`)}
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
        {tab === "entities" && <EntitiesTable runId={id} profile={profile} />}
        {tab === "publish" && <PublishPanel runId={id} run={run} profile={profile} />}
        {tab === "diagnostics" && <RunDiagnostics runId={id} />}
        {tab === "files" && <RunFiles runId={id} />}
        {tab === "history" && <RunHistory runId={id} />}
      </PanelErrorBoundary>
    </div>
  );
}

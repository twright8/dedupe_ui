/* ============================================================
   Screen: Label library — the answers that teach and test the matcher
   ----------------------------------------------------------------
   Every label carries three SEPARATE facts (kept distinct on purpose):
     • Verdict  — Match / Not a match
     • Source   — who said it: You / Bulk / Imported / Suggested
     • Role     — what the model does with it: TEACHES (trains) or TESTS (held-out)
   A label is now a statement about two records of the same dataset
   (DESIGN.md D10): the pair is stored against the two record ids,
   smaller first, and a later label supersedes an earlier one rather
   than overwriting it.
   ============================================================ */

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { Empty } from "../components/Empty";
import { fmtDateTime } from "../components/ProbBar";

// --- Source: who produced the label (independent of its Role) ---
function sourceKind(label) {
  const p = String(label.provenance || "").toLowerCase();
  if (p === "llm") return "suggested";
  if (p === "import") return "imported";
  if (p.startsWith("bulk")) return "bulk";
  return "you"; // manual, or legacy/null
}

const SOURCE_META = {
  you: { text: "You", title: "Hand-made by a reviewer in the review screen" },
  bulk: { text: "Bulk", title: "Created by marking a score band in one go" },
  imported: { text: "Imported", title: "Came from the earlier manual grouping, not from this tool" },
  suggested: { text: "Suggested", title: "Machine-suggested — review before trusting" },
};

export default function LabelsScreen() {
  const [labels, setLabels] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [search, setSearch] = useState("");
  const [verdict, setVerdict] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [trackFilter, setTrackFilter] = useState("all");
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [sortKey, setSortKey] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");

  const [evalSet, setEvalSet] = useState(null);
  const [designating, setDesignating] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [openHistory, setOpenHistory] = useState(null);

  function loadEval() {
    api.evalSetStatus().then(setEvalSet).catch(() => setEvalSet(null));
  }
  useEffect(loadEval, []);

  function loadLabels() {
    setLoading(true);
    setError(null);
    api
      .listLabels({ active: showSuperseded ? 0 : 1, per_page: 500 })
      .then((data) => {
        setLabels(data.items || []);
        setTotal(data.total || 0);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || "Failed to load labels");
        setLoading(false);
      });
  }
  useEffect(loadLabels, [showSuperseded]); // eslint-disable-line react-hooks/exhaustive-deps

  async function autoPickTests() {
    if (
      !window.confirm(
        "Auto-pick a balanced Test set?\n\n" +
          "This takes up to 200 of your NEWEST hand-made Match/Not-match labels, an equal " +
          "number of each, and moves them to TESTS (the model stops learning from them and is " +
          "graded on them instead). It never takes more than half of either, so there's always " +
          "enough left to teach the model."
      )
    )
      return;
    setDesignating(true);
    try {
      await api.designateEvalSet(200);
      loadEval();
      loadLabels();
    } finally {
      setDesignating(false);
    }
  }

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const fromMs = dateFrom ? new Date(dateFrom + "T00:00:00").getTime() : null;
    const toMs = dateTo ? new Date(dateTo + "T23:59:59.999").getTime() : null;
    const rows = labels.filter((label) => {
      const truth = String(label.is_match || label.is_true_match || "").toUpperCase();
      if (verdict !== "all" && truth !== verdict) return false;
      if (roleFilter === "teaches" && label.held_out) return false;
      if (roleFilter === "tests" && !label.held_out) return false;
      if (sourceFilter !== "all" && sourceKind(label) !== sourceFilter) return false;
      if (trackFilter !== "all" && label.track !== trackFilter) return false;
      if (fromMs != null || toMs != null) {
        const t = label.created_at ? new Date(label.created_at).getTime() : NaN;
        if (Number.isNaN(t)) return false;
        if (fromMs != null && t < fromMs) return false;
        if (toMs != null && t > toMs) return false;
      }
      if (!q) return true;
      return [
        label.name_a,
        label.name_b,
        label.record_id_a,
        label.record_id_b,
        label.reviewer,
        label.notes,
        label.run_id,
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q));
    });

    const dir = sortDir === "asc" ? 1 : -1;
    const sortVal = (l) => {
      if (sortKey === "verdict") return String(l.is_match || l.is_true_match || "").toUpperCase();
      if (sortKey === "name") return String(l.name_a || "").toLowerCase();
      const t = l.created_at ? new Date(l.created_at).getTime() : NaN;
      return Number.isNaN(t) ? -Infinity : t;
    };
    return [...rows].sort((a, b) => {
      const av = sortVal(a);
      const bv = sortVal(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [labels, search, verdict, roleFilter, sourceFilter, trackFilter, dateFrom, dateTo, sortKey, sortDir]);

  function toggleSort(key) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir(key === "created_at" ? "desc" : "asc");
    }
  }

  function SortHead({ k, children, style }) {
    const active = sortKey === k;
    return (
      <th
        style={{ cursor: "pointer", userSelect: "none", whiteSpace: "nowrap", ...style }}
        onClick={() => toggleSort(k)}
        title="Click to sort"
      >
        {children}
        {active ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
      </th>
    );
  }

  async function setRole(ids, heldOut) {
    if (!ids.length) return;
    setBulkBusy(true);
    try {
      await api.setLabelsRole(ids, heldOut);
      loadLabels();
      loadEval();
    } catch (err) {
      alert(err.message || "Could not change role");
    } finally {
      setBulkBusy(false);
    }
  }

  const fileRef = useRef(null);
  async function onImportFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (
      !window.confirm(
        "Import adds these labels to the library. A label on a pair that already has one " +
          "supersedes it, and the old row stays on record. Export first if you want a backup. Continue?"
      )
    )
      return;
    setBulkBusy(true);
    try {
      const r = await api.importLabels(await file.text());
      alert(`Imported ${r.inserted ?? r.saved ?? 0} labels (${r.skipped ?? 0} skipped).`);
      loadLabels();
      loadEval();
    } catch (err) {
      alert(err.message || "Import failed");
    } finally {
      setBulkBusy(false);
    }
  }

  async function deleteLabel(label) {
    if (!window.confirm(`Remove the active label for ${label.name_a} ↔ ${label.name_b}?`)) return;
    try {
      await api.deleteLabel(label.id);
      loadLabels();
    } catch (err) {
      alert(err.message || "Could not delete label");
    }
  }

  const shownIds = filtered.map((l) => l.id);
  const shownTeaches = filtered.filter((l) => !l.held_out).length;
  const shownTests = filtered.length - shownTeaches;
  const tracksSeen = [...new Set(labels.map((l) => l.track).filter(Boolean))].sort();

  return (
    <div className="content" style={{ maxWidth: "none", paddingRight: 28 }}>
      <div className="page-head">
        <div>
          <h1 className="page-title">Label library</h1>
          <p className="page-sub">
            Your Match / Not-a-match answers, one per pair of records. Each one either{" "}
            <strong>teaches</strong> the matcher (it learns from it) or <strong>tests</strong> it
            (held aside to grade it honestly). They re-apply to every later run.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <a className="btn" href={api.labelsExportUrl()} title="Download every answer as a CSV">
            <Icons.download size={14} /> Export CSV
          </a>
          <button className="btn" onClick={() => fileRef.current?.click()} disabled={bulkBusy}>
            Import CSV
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            style={{ display: "none" }}
            onChange={onImportFile}
          />
        </div>
      </div>

      {/* Test set */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-b" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <span className="eyebrow">Test set</span>
            <span style={{ fontSize: 13 }}>
              {evalSet ? (
                <>
                  <strong>{evalSet.total}</strong> labels held aside for testing
                  {evalSet.total > 0 && (
                    <span className="muted">
                      {" "}
                      · {evalSet.by_verdict?.TRUE || 0} Match / {evalSet.by_verdict?.FALSE || 0} No
                    </span>
                  )}
                </>
              ) : (
                "—"
              )}
            </span>
            <button
              className="btn"
              style={{ marginLeft: "auto" }}
              onClick={autoPickTests}
              disabled={designating}
            >
              {designating ? "Picking..." : "Auto-pick a balanced Test set"}
            </button>
          </div>
          <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
            <strong>Test</strong> labels are hidden from the model so you can grade it on cases it
            never studied. A model graded on the answers it learned from always looks too good.
            Everything else <strong>teaches</strong> the model.
          </p>
        </div>
      </div>

      {/* Filters */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
        <div className="search" style={{ width: 260 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search names, record ids, notes..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="seg" title="Filter by verdict">
          {[
            ["all", "All"],
            ["TRUE", "Match"],
            ["FALSE", "No"],
          ].map(([id, label]) => (
            <button key={id} className={verdict === id ? "on" : ""} onClick={() => setVerdict(id)}>
              {label}
            </button>
          ))}
        </div>
        <div className="seg" title="Does it teach or test the model?">
          {[
            ["all", "Any role"],
            ["teaches", "Teaches"],
            ["tests", "Tests"],
          ].map(([id, label]) => (
            <button key={id} className={roleFilter === id ? "on" : ""} onClick={() => setRoleFilter(id)}>
              {label}
            </button>
          ))}
        </div>
        <select
          className="select"
          style={{ width: 150 }}
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          title="Filter by source"
        >
          <option value="all">Any source</option>
          <option value="you">You</option>
          <option value="bulk">Bulk</option>
          <option value="imported">Imported</option>
          <option value="suggested">Suggested</option>
        </select>
        {tracksSeen.length > 1 && (
          <select
            className="select"
            style={{ width: 140 }}
            value={trackFilter}
            onChange={(e) => setTrackFilter(e.target.value)}
          >
            <option value="all">Any track</option>
            {tracksSeen.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        )}
        <div
          style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12 }}
          title="Show only labels created within this date window"
        >
          <span className="muted">Labelled</span>
          <input
            type="date"
            className="input"
            style={{ width: 140 }}
            value={dateFrom}
            max={dateTo || undefined}
            onChange={(e) => setDateFrom(e.target.value)}
          />
          <span className="muted">–</span>
          <input
            type="date"
            className="input"
            style={{ width: 140 }}
            value={dateTo}
            min={dateFrom || undefined}
            onChange={(e) => setDateTo(e.target.value)}
          />
          {(dateFrom || dateTo) && (
            <button
              className="btn sm"
              onClick={() => {
                setDateFrom("");
                setDateTo("");
              }}
            >
              Clear
            </button>
          )}
        </div>
        <label className="muted" style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5 }}>
          <input
            type="checkbox"
            checked={showSuperseded}
            onChange={(e) => setShowSuperseded(e.target.checked)}
          />
          include superseded
        </label>
        <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
          Showing {filtered.length} of {total}
        </span>
      </div>

      {/* Bulk role bar */}
      {filtered.length > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginBottom: 12,
            padding: "7px 12px",
            background: "var(--surface-sub)",
            border: "1px solid var(--line)",
            borderRadius: 6,
            fontSize: 12.5,
          }}
        >
          <span className="muted">
            The <strong>{filtered.length}</strong> shown ({shownTeaches} teach · {shownTests} test) →
          </span>
          <button className="btn sm" disabled={bulkBusy} onClick={() => setRole(shownIds, 0)}>
            Set shown → Teaches
          </button>
          <button className="btn sm" disabled={bulkBusy} onClick={() => setRole(shownIds, 1)}>
            Set shown → Tests
          </button>
          {bulkBusy && <span className="muted pulse">updating…</span>}
        </div>
      )}

      {loading ? (
        <p className="muted pulse" style={{ padding: 24 }}>
          Loading labels...
        </p>
      ) : error ? (
        <Empty
          title="Failed to load labels"
          sub={error}
          action={
            <button className="btn primary" onClick={loadLabels}>
              <Icons.refresh size={14} stroke="#fff" />
              Retry
            </button>
          }
        />
      ) : filtered.length === 0 ? (
        <Empty
          title="No labels match"
          sub="Clear the filters, or answer some pairs in the review queue."
        />
      ) : (
        <div className="tbl-wrap">
          <table className="t">
            <thead>
              <tr>
                <SortHead k="verdict" style={{ width: 90 }}>
                  Verdict
                </SortHead>
                <SortHead k="name" style={{ minWidth: 230 }}>The pair</SortHead>
                <th style={{ width: 110 }}>Track</th>
                <th style={{ width: 110 }}>Reviewer</th>
                <SortHead k="created_at" style={{ width: 150 }}>
                  Labelled
                </SortHead>
                <th style={{ width: 100 }}>Source</th>
                <th style={{ width: 160 }}>Role</th>
                <th style={{ minWidth: 220 }}>Notes</th>
                <th style={{ width: 110 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((label) => {
                const truth = String(label.is_match || label.is_true_match || "").toUpperCase();
                const src = SOURCE_META[sourceKind(label)] || SOURCE_META.you;
                const tests = !!label.held_out;
                const superseded = label.active === 0 || label.superseded_by;
                return (
                  <tr key={label.id} style={superseded ? { opacity: 0.55 } : undefined}>
                    <td>
                      <span className={`tag ${truth === "TRUE" ? "green" : "red"}`}>
                        <span className="dot" />
                        {truth === "TRUE" ? "Match" : "No"}
                      </span>
                      {superseded && (
                        <div className="muted" style={{ fontSize: 11 }}>
                          superseded
                        </div>
                      )}
                    </td>
                    <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
                      <div style={{ fontWeight: 600 }}>{label.name_a || label.record_id_a}</div>
                      <div style={{ fontWeight: 600 }}>{label.name_b || label.record_id_b}</div>
                      <div className="mono muted" style={{ fontSize: 11 }}>
                        {label.record_id_a} ↔ {label.record_id_b}
                        {label.run_id ? ` · ${label.run_id}` : ""}
                      </div>
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {label.track || "—"}
                    </td>
                    <td>{label.reviewer || "user"}</td>
                    <td className="muted" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>
                      {fmtDateTime(label.created_at) || "—"}
                    </td>
                    <td>
                      <span className="tag" title={src.title}>
                        {src.text}
                      </span>
                    </td>
                    <td>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        {tests ? (
                          <span className="tag violet" title="Held aside to grade the model">
                            <span className="dot" />
                            Tests
                          </span>
                        ) : (
                          <span className="tag" title="The model trains on this label">
                            <span className="dot" />
                            Teaches
                          </span>
                        )}
                        <button
                          className="btn sm"
                          disabled={bulkBusy}
                          onClick={() => setRole([label.id], tests ? 0 : 1)}
                        >
                          {tests ? "→ Teaches" : "→ Tests"}
                        </button>
                      </div>
                    </td>
                    <td style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
                      <span className="muted">{label.notes || label.reviewer_notes || ""}</span>
                      {label.evidence_url && (
                        <div>
                          <a
                            className="link"
                            href={label.evidence_url}
                            target="_blank"
                            rel="noreferrer"
                            style={{ fontSize: 11.5 }}
                          >
                            <Icons.link size={11} /> source
                          </a>
                        </div>
                      )}
                    </td>
                    <td>
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        <button
                          className="btn sm"
                          onClick={() => setOpenHistory(openHistory === label.id ? null : label.id)}
                          title="Earlier decisions on this pair"
                        >
                          History
                        </button>
                        {!superseded && (
                          <button className="btn sm danger" onClick={() => deleteLabel(label)}>
                            Remove
                          </button>
                        )}
                      </div>
                      {openHistory === label.id && (
                        <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                          {label.superseded_by
                            ? `Superseded by label ${label.superseded_by}.`
                            : "This is the decision that stands. Tick “include superseded” to see the ones it replaced."}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

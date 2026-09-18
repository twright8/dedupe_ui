/* ============================================================
   Screen: Label library — the answers that teach and test the matcher
   ----------------------------------------------------------------
   Every label carries three SEPARATE facts (kept distinct on purpose):
     • Verdict  — Match / Not a match
     • Source   — who said it: You / Suggested (LLM) / Auto / Bulk
     • Role     — what the model does with it: TEACHES (trains) or TESTS (held-out)
   "Gold" / "freeze" / "held-out" were three names for the same Role=Tests state;
   we use Teaches / Tests everywhere now.
   ============================================================ */

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { Empty } from "../components/Empty";

const EMPTY_FORM = {
  ocod_name_clean: "",
  jurisdiction_clean: "",
  roe_company_number: "",
  roe_name: "",
  ocod_name_raw: "",
  ocod_jurisdiction_raw: "",
  is_true_match: "TRUE",
  reviewer_notes: "",
  run_id: "",
};

function labelPayload(form) {
  return {
    ocod_name_clean: form.ocod_name_clean.trim(),
    jurisdiction_clean: form.jurisdiction_clean.trim(),
    roe_company_number: form.roe_company_number.trim(),
    roe_name: (form.roe_name || "").trim() || null,
    ocod_name_raw: form.ocod_name_raw.trim() || null,
    ocod_jurisdiction_raw: form.ocod_jurisdiction_raw.trim() || null,
    is_true_match: form.is_true_match,
    reviewer_notes: form.reviewer_notes.trim() || null,
    run_id: form.run_id.trim() || null,
  };
}

function formFromLabel(label) {
  return {
    ocod_name_clean: label.ocod_name_clean || "",
    jurisdiction_clean: label.jurisdiction_clean || "",
    roe_company_number: label.roe_company_number || "",
    roe_name: label.roe_name || "",
    ocod_name_raw: label.ocod_name_raw || "",
    ocod_jurisdiction_raw: label.ocod_jurisdiction_raw || "",
    is_true_match: String(label.is_true_match || "TRUE").toUpperCase(),
    reviewer_notes: label.reviewer_notes || "",
    run_id: label.run_id || "",
  };
}

function fmtDate(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

// --- Source: who produced the label (independent of its Role) ---
function sourceKind(label) {
  const p = String(label.provenance || "").toLowerCase();
  if (p === "llm") return "suggested";
  if (p === "implied_negative") return "auto";
  if (p.startsWith("bulk")) return "bulk";
  return "you"; // manual, or legacy/null
}
const SOURCE_META = {
  you: { text: "You", title: "Hand-made by a reviewer" },
  suggested: { text: "Suggested", title: "Machine-suggested (LLM import) — review before trusting" },
  auto: { text: "Auto", title: "Auto-created (the losing candidate when you picked another match)" },
  bulk: { text: "Bulk", title: "Created by a bulk range/review action" },
};

export default function LabelsScreen() {
  const [labels, setLabels] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [verdict, setVerdict] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");     // all | teaches | tests
  const [sourceFilter, setSourceFilter] = useState("all");  // all | you | suggested | auto | bulk
  const [dateFrom, setDateFrom] = useState("");             // labelled-on/after (YYYY-MM-DD)
  const [dateTo, setDateTo] = useState("");                 // labelled-on/before
  const [sortKey, setSortKey] = useState("created_at");     // created_at | verdict | ocod
  const [sortDir, setSortDir] = useState("desc");           // newest labelled first
  const [sheet, setSheet] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [evalSet, setEvalSet] = useState(null);
  const [designating, setDesignating] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);

  function loadEval() {
    api.evalSetStatus().then(setEvalSet).catch(() => setEvalSet(null));
  }
  useEffect(loadEval, []);

  async function autoPickTests() {
    if (!window.confirm(
      "Auto-pick a balanced Test set?\n\n" +
      "This takes up to 200 of your NEWEST hand-made Match/Not-match labels, an equal " +
      "number of each, and moves them to TESTS (the model stops learning from them and is " +
      "graded on them instead). It never takes more than half of either, so there's always " +
      "enough left to teach the model. It does NOT touch Suggested/Auto labels — to use those, " +
      "filter below and click “Set shown → Tests”."
    )) return;
    setDesignating(true);
    try {
      await api.designateEvalSet(200);
      loadEval();
      loadLabels();
    } finally {
      setDesignating(false);
    }
  }

  function loadLabels() {
    setLoading(true);
    setError(null);
    api.listLabels({ active: 1, per_page: 500 })
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
  useEffect(() => { loadLabels(); }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const fromMs = dateFrom ? new Date(dateFrom + "T00:00:00").getTime() : null;
    const toMs = dateTo ? new Date(dateTo + "T23:59:59.999").getTime() : null;
    const rows = labels.filter((label) => {
      const truth = String(label.is_true_match || "").toUpperCase();
      if (verdict !== "all" && truth !== verdict) return false;
      if (roleFilter === "teaches" && label.held_out) return false;
      if (roleFilter === "tests" && !label.held_out) return false;
      if (sourceFilter !== "all" && sourceKind(label) !== sourceFilter) return false;
      if (fromMs != null || toMs != null) {
        const t = label.created_at ? new Date(label.created_at).getTime() : NaN;
        if (Number.isNaN(t)) return false;            // undated rows fall outside any date window
        if (fromMs != null && t < fromMs) return false;
        if (toMs != null && t > toMs) return false;
      }
      if (!q) return true;
      return [
        label.ocod_name_clean, label.ocod_name_raw, label.jurisdiction_clean,
        label.roe_company_number, label.roe_name, label.reviewer,
        label.reviewer_notes, label.run_id,
      ].filter(Boolean).some((value) => String(value).toLowerCase().includes(q));
    });

    const dir = sortDir === "asc" ? 1 : -1;
    const sortVal = (l) => {
      if (sortKey === "verdict") return String(l.is_true_match || "").toUpperCase();
      if (sortKey === "ocod") return String(l.ocod_name_clean || "").toLowerCase();
      const t = l.created_at ? new Date(l.created_at).getTime() : NaN;
      return Number.isNaN(t) ? -Infinity : t;        // undated rows sort to the bottom (desc)
    };
    return [...rows].sort((a, b) => {
      const av = sortVal(a), bv = sortVal(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
  }, [labels, search, verdict, roleFilter, sourceFilter, dateFrom, dateTo, sortKey, sortDir]);

  function toggleSort(key) {
    if (sortKey === key) { setSortDir((d) => (d === "asc" ? "desc" : "asc")); }
    else { setSortKey(key); setSortDir(key === "created_at" ? "desc" : "asc"); }
  }
  function SortHead({ k, children, style }) {
    const active = sortKey === k;
    return (
      <th style={{ cursor: "pointer", userSelect: "none", whiteSpace: "nowrap", ...style }}
        onClick={() => toggleSort(k)} title="Click to sort">
        {children}{active ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
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
    if (!window.confirm(
      "Import REPLACES your entire label library with this CSV (overwrite). " +
      "Export first if you want a backup. Continue?"
    )) return;
    setBulkBusy(true);
    try {
      const r = await api.importLabelsCsv(await file.text());
      alert(`Imported ${r.inserted} labels (${r.skipped} skipped).`);
      loadLabels();
      loadEval();
    } catch (err) {
      alert(err.message || "Import failed");
    } finally {
      setBulkBusy(false);
    }
  }

  function openAdd() { setForm(EMPTY_FORM); setSheet({ mode: "add", label: null }); }
  function openEdit(label) { setForm(formFromLabel(label)); setSheet({ mode: "edit", label }); }
  function closeSheet() { if (!saving) setSheet(null); }
  function updateField(field, value) { setForm((prev) => ({ ...prev, [field]: value })); }

  async function saveLabel(event) {
    event.preventDefault();
    setSaving(true);
    try {
      const payload = labelPayload(form);
      if (sheet?.mode === "edit" && sheet.label) await api.updateLabel(sheet.label.id, payload);
      else await api.createLabel(payload);
      setSheet(null);
      loadLabels();
    } catch (err) {
      alert(err.message || "Could not save label");
    } finally {
      setSaving(false);
    }
  }

  async function deleteLabel(label) {
    if (!window.confirm(`Remove the active label for ${label.ocod_name_clean} ↔ ${label.roe_company_number}?`)) return;
    try { await api.deleteLabel(label.id); loadLabels(); }
    catch (err) { alert(err.message || "Could not delete label"); }
  }

  const shownIds = filtered.map((l) => l.id);
  const shownTeaches = filtered.filter((l) => !l.held_out).length;
  const shownTests = filtered.length - shownTeaches;

  return (
    <div className="content" style={{ maxWidth: "none", paddingRight: 28 }}>
      <div className="page-head">
        <div>
          <h1 className="page-title">Label library</h1>
          <p className="page-sub">
            Your Match / Not-a-match answers. Each one either <strong>teaches</strong> the matcher
            (it learns from it) or <strong>tests</strong> it (held aside to grade it honestly). They
            re-apply to matching OCOD entities on every run.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <a className="btn" href={api.labelsExportUrl()}
            title="Download every answer as a CSV — a backup, or to edit in a spreadsheet and re-import">
            <Icons.download size={14} /> Export CSV
          </a>
          <button className="btn" onClick={() => fileRef.current?.click()} disabled={bulkBusy}
            title="Replace the whole library from a CSV (overwrites — export first to back up)">
            Import CSV
          </button>
          <input ref={fileRef} type="file" accept=".csv,text/csv" style={{ display: "none" }} onChange={onImportFile} />
          <button className="btn primary" onClick={openAdd}>
            <Icons.plus size={14} stroke="#fff" />
            Add label
          </button>
        </div>
      </div>

      {/* Test set: labels held aside to grade the model honestly (never trained on). */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-b" style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <span className="eyebrow">Test set</span>
            <span style={{ fontSize: 13 }}>
              {evalSet ? (
                <><strong>{evalSet.total}</strong> labels held aside for testing
                  {evalSet.total > 0 && <span className="muted"> · {evalSet.by_verdict?.TRUE || 0} Match / {evalSet.by_verdict?.FALSE || 0} No</span>}</>
              ) : "—"}
            </span>
            <button
              className="btn"
              style={{ marginLeft: "auto" }}
              onClick={autoPickTests}
              disabled={designating}
              title="Move a balanced sample of your newest hand-made labels into the Test set"
            >
              {designating ? "Picking..." : "Auto-pick a balanced Test set"}
            </button>
          </div>
          <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
            <strong>Test</strong> labels are hidden from the model so you can grade it on cases it never
            studied — like marking an exam on questions the student didn't revise. A model graded on the
            answers it learned from always looks too good. Everything else <strong>teaches</strong> the
            model. Pick your own by filtering below and using <em>Set shown → Tests</em>, or let
            <em> Auto-pick</em> grab a balanced sample of your newest hand-made labels.
          </p>
        </div>
      </div>

      {/* Filters */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
        <div className="search" style={{ width: 280 }}>
          <Icons.search size={14} />
          <input className="input" placeholder="Search OCOD, ROE, jurisdiction, notes..."
            value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <div className="seg" title="Filter by verdict">
          {[["all", "All"], ["TRUE", "Match"], ["FALSE", "No"]].map(([id, label]) => (
            <button key={id} className={verdict === id ? "on" : ""} onClick={() => setVerdict(id)}>{label}</button>
          ))}
        </div>
        <div className="seg" title="Filter by role: does it teach or test the model?">
          {[["all", "Any role"], ["teaches", "Teaches"], ["tests", "Tests"]].map(([id, label]) => (
            <button key={id} className={roleFilter === id ? "on" : ""} onClick={() => setRoleFilter(id)}>{label}</button>
          ))}
        </div>
        <select className="select" style={{ width: 150 }} value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)} title="Filter by source">
          <option value="all">Any source</option>
          <option value="you">You</option>
          <option value="suggested">Suggested (LLM)</option>
          <option value="auto">Auto</option>
          <option value="bulk">Bulk</option>
        </select>
        <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12 }}
          title="Show only labels created within this date window">
          <span className="muted">Labelled</span>
          <input type="date" className="input" style={{ width: 150 }} value={dateFrom}
            max={dateTo || undefined} onChange={(e) => setDateFrom(e.target.value)} />
          <span className="muted">–</span>
          <input type="date" className="input" style={{ width: 150 }} value={dateTo}
            min={dateFrom || undefined} onChange={(e) => setDateTo(e.target.value)} />
          {(dateFrom || dateTo) && (
            <button className="btn sm" onClick={() => { setDateFrom(""); setDateTo(""); }}
              title="Clear the date filter">Clear</button>
          )}
        </div>
        <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
          Showing {filtered.length} of {total}
        </span>
      </div>

      {/* Bulk role bar — acts on exactly the shown (filtered) labels */}
      {filtered.length > 0 && (
        <div style={{
          display: "flex", alignItems: "center", gap: 10, marginBottom: 12, padding: "7px 12px",
          background: "var(--paper-2, #f7f7f8)", border: "1px solid var(--line)", borderRadius: 6, fontSize: 12.5,
        }}>
          <span className="muted">
            The <strong>{filtered.length}</strong> shown ({shownTeaches} teach · {shownTests} test) →
          </span>
          <button className="btn sm" disabled={bulkBusy} onClick={() => setRole(shownIds, 0)}
            title="Move all shown labels into Teaches (the model will train on them)">
            Set shown → Teaches
          </button>
          <button className="btn sm" disabled={bulkBusy} onClick={() => setRole(shownIds, 1)}
            title="Move all shown labels into Tests (held aside, graded only)">
            Set shown → Tests
          </button>
          {bulkBusy && <span className="muted pulse">updating…</span>}
        </div>
      )}

      {loading ? (
        <p className="muted pulse" style={{ padding: 24 }}>Loading labels...</p>
      ) : error ? (
        <Empty title="Failed to load labels" sub={error}
          action={<button className="btn primary" onClick={loadLabels}><Icons.refresh size={14} stroke="#fff" />Retry</button>} />
      ) : filtered.length === 0 ? (
        <Empty title="No labels match" sub="Clear the filters, or create a label / save decisions from Review."
          action={<button className="btn primary" onClick={openAdd}><Icons.plus size={14} stroke="#fff" />Add label</button>} />
      ) : (
        <div className="tbl-wrap">
          <table className="t">
            <thead>
              <tr>
                <SortHead k="verdict">Verdict</SortHead>
                <SortHead k="ocod">OCOD entity (clean)</SortHead>
                <th>ROE company</th>
                <th>Jurisdiction</th>
                <th>Reviewer</th>
                <SortHead k="created_at">Labelled</SortHead>
                <th>Source</th>
                <th>Role</th>
                <th>Notes</th>
                <th style={{ width: 132 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((label) => {
                const truth = String(label.is_true_match || "").toUpperCase();
                const src = SOURCE_META[sourceKind(label)] || SOURCE_META.you;
                const tests = !!label.held_out;
                return (
                  <tr key={label.id}>
                    <td>
                      <span className={`tag ${truth === "TRUE" ? "green" : "red"}`}>
                        <span className="dot" />{truth === "TRUE" ? "Match" : "No"}
                      </span>
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{label.ocod_name_clean}</div>
                      {label.ocod_name_raw && label.ocod_name_raw !== label.ocod_name_clean && (
                        <div className="muted" style={{ fontSize: 11.5 }}>raw: {label.ocod_name_raw}</div>
                      )}
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>
                        {label.roe_name || <span className="muted" style={{ fontWeight: 400 }}>— name not recorded —</span>}
                      </div>
                      <div className="mono muted" style={{ fontSize: 11 }}>
                        {label.roe_company_number}{label.run_id ? ` · ${label.run_id}` : ""}
                      </div>
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>{label.jurisdiction_clean}</td>
                    <td>{label.reviewer || "user"}</td>
                    <td className="muted" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>
                      {fmtDate(label.created_at) || "—"}
                    </td>
                    <td><span className="tag" title={src.title}>{src.text}</span></td>
                    <td>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        {tests ? (
                          <span className="tag violet" title="Held aside to grade the model — not trained on">
                            <span className="dot" />Tests
                          </span>
                        ) : (
                          <span className="tag" title="The model trains on this label">
                            <span className="dot" />Teaches
                          </span>
                        )}
                        <button className="btn sm" disabled={bulkBusy}
                          onClick={() => setRole([label.id], tests ? 0 : 1)}
                          title={tests ? "Move to Teaches (train on it)" : "Move to Tests (hold aside, grade only)"}>
                          {tests ? "→ Teaches" : "→ Tests"}
                        </button>
                      </div>
                    </td>
                    <td style={{ maxWidth: 320 }}><span className="muted">{label.reviewer_notes || ""}</span></td>
                    <td>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button className="btn sm" onClick={() => openEdit(label)}>Edit</button>
                        <button className="btn sm danger" onClick={() => deleteLabel(label)}>Remove</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {sheet && (
        <>
          <div className="scrim" onClick={closeSheet} />
          <aside className="sheet" role="dialog" aria-modal="true" aria-label="Label editor">
            <form onSubmit={saveLabel} style={{ display: "contents" }}>
              <div className="sheet-h">
                <div>
                  <div className="eyebrow">{sheet.mode === "edit" ? "Edit label" : "Add label"}</div>
                  <div style={{ fontSize: 18, fontWeight: 650 }}>
                    {sheet.mode === "edit" ? form.roe_company_number : "New decision"}
                  </div>
                </div>
                <button type="button" className="btn" style={{ marginLeft: "auto" }}
                  onClick={closeSheet} aria-label="Close label editor">
                  <Icons.x size={14} />
                </button>
              </div>
              <div className="sheet-b" style={{ display: "grid", gap: 12 }}>
                <div className="field">
                  <label>Verdict</label>
                  <select className="select" value={form.is_true_match}
                    onChange={(e) => updateField("is_true_match", e.target.value)}>
                    <option value="TRUE">Match (TRUE)</option>
                    <option value="FALSE">Not a match (FALSE)</option>
                  </select>
                </div>
                <div className="field">
                  <label>OCOD clean name</label>
                  <input className="input" value={form.ocod_name_clean} required
                    onChange={(e) => updateField("ocod_name_clean", e.target.value)} />
                </div>
                <div className="field">
                  <label>Jurisdiction</label>
                  <input className="input" value={form.jurisdiction_clean} required
                    onChange={(e) => updateField("jurisdiction_clean", e.target.value)} />
                </div>
                <div className="field">
                  <label>ROE company number</label>
                  <input className="input" value={form.roe_company_number} required
                    onChange={(e) => updateField("roe_company_number", e.target.value)} />
                </div>
                <div className="field">
                  <label>OCOD raw name</label>
                  <input className="input" value={form.ocod_name_raw}
                    onChange={(e) => updateField("ocod_name_raw", e.target.value)} />
                </div>
                <div className="field">
                  <label>Raw jurisdiction</label>
                  <input className="input" value={form.ocod_jurisdiction_raw}
                    onChange={(e) => updateField("ocod_jurisdiction_raw", e.target.value)} />
                </div>
                <div className="field">
                  <label>Run id</label>
                  <input className="input" value={form.run_id}
                    onChange={(e) => updateField("run_id", e.target.value)} />
                </div>
                <div className="field">
                  <label>Notes</label>
                  <textarea className="textarea" value={form.reviewer_notes}
                    onChange={(e) => updateField("reviewer_notes", e.target.value)} />
                </div>
              </div>
              <div className="sheet-f">
                <button className="btn primary" type="submit" disabled={saving}>
                  <Icons.check size={14} stroke="#fff" />
                  {saving ? "Saving..." : "Save label"}
                </button>
                <button className="btn" type="button" onClick={closeSheet} disabled={saving}>Cancel</button>
              </div>
            </form>
          </aside>
        </>
      )}
    </div>
  );
}

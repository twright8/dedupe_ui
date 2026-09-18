/* ============================================================
   Screen: Ambiguous cases — 1 OCOD x N ROE candidate picker
   with multi-label saves (TRUE for chosen, FALSE for others)
   ============================================================ */

import { useState, useEffect, useMemo, useCallback } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { ProbBar, fmtProb, fmtNumber } from "../components/ProbBar";
import { AddressPanel } from "../components/AddressPanel";
import { Empty } from "../components/Empty";

// ---------- Group flat match rows into ambiguous cases ----------
// API returns flat rows from matches_ambiguous.csv. Multiple rows
// can share the same OCOD entity (ocod_name_clean + jurisdiction_clean).
// Group them: each group becomes one "case" with N candidates.
function groupIntoCases(items) {
  const map = new Map();
  for (const m of items) {
    const key = `${m.ocod_name_clean || m.ocod_name_raw}||${m.jurisdiction_clean}`;
    if (!map.has(key)) {
      map.set(key, {
        id: key,
        ocod: {
          raw: m.ocod_name_raw || m.ocod_name_clean || "",
          clean: m.ocod_name_clean || m.ocod_name_raw || "",
          id: m.ocod_id || "",
          jur: m.jurisdiction_clean || m.ocod_jurisdiction_raw || "",
          titles: m.title_count || m.titles || 0,
          registry_date: m.registry_date || "",
        },
        candidates: [],
      });
    }
      map.get(key).candidates.push({
        matchId: m.match_id || m.id,
        roe: {
          raw: m.roe_name_raw || "",
          clean: m.roe_name_clean || m.roe_name_raw || "",
          id: m.roe_company_number || "",
        jur: m.roe_jurisdiction || m.jurisdiction_clean || "",
        incorp: m.roe_incorporation_date || m.incorporation_date || "",
        agent: m.roe_registered_agent || m.registered_agent || "",
        address: m.roe_address || "",
      },
      prob: m.match_probability ?? m.prob ?? 0,
      label: m.label || null,
      // Side-by-side service-vs-registered address block for this candidate pair.
      addressPanel: m.address || null,
      _raw: m,
    });
  }
  return Array.from(map.values());
}

function companiesHouseUrl(companyNumber) {
  if (!companyNumber) return null;
  return `https://find-and-update.company-information.service.gov.uk/company/${encodeURIComponent(companyNumber)}`;
}

// ---------- Main screen component ----------
export default function AmbiguousScreen() {
  const { id: runId } = useParams();

  // Data
  const [allItems, setAllItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Local decision state: caseId -> roeId (or "_all_false_")
  const [picks, setPicks] = useState({});

  // Saving state for feedback
  const [saving, setSaving] = useState({});  // caseId -> "saving" | "saved" | "error"

  // Selected case
  const [caseId, setCaseId] = useState(null);
  const [inspectCandidate, setInspectCandidate] = useState(null);
  const [search, setSearch] = useState("");
  const [caseFilter, setCaseFilter] = useState("all");

  // ---------- Load data ----------
  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    setError(null);

    api
      .getRunMatches(runId, { bucket: "ambiguous", per_page: 5000 })
      .then((data) => {
        const items = data.items || [];
        setAllItems(items);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || "Failed to load ambiguous cases");
        setLoading(false);
      });
  }, [runId]);

  // ---------- Group into cases ----------
  const cases = useMemo(() => groupIntoCases(allItems), [allItems]);
  const filteredCases = useMemo(() => {
    const q = search.trim().toLowerCase();
    return cases.filter((c) => {
      if (caseFilter === "undecided" && picks[c.id]) return false;
      if (caseFilter === "close") {
        const probs = [...c.candidates].map((cand) => cand.prob || 0).sort((a, b) => b - a);
        const margin = probs.length >= 2 ? probs[0] - probs[1] : null;
        if (margin == null || margin >= 0.05) return false;
      }
      if (!q) return true;
      return [
        c.ocod.raw,
        c.ocod.clean,
        c.ocod.id,
        c.ocod.jur,
        ...c.candidates.flatMap((cand) => [cand.roe.raw, cand.roe.clean, cand.roe.id, cand.roe.jur]),
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q));
    });
  }, [cases, caseFilter, picks, search]);

  // Seed picks from pre-existing labels
  useEffect(() => {
    if (cases.length === 0) return;
    const seeded = {};
    for (const c of cases) {
      const trueCandidate = c.candidates.find((cand) => cand.label === "TRUE");
      if (trueCandidate) {
        seeded[c.id] = trueCandidate.roe.id;
      } else {
        const allFalse =
          c.candidates.length > 0 &&
          c.candidates.every((cand) => cand.label === "FALSE");
        if (allFalse) {
          seeded[c.id] = "_all_false_";
        }
      }
    }
    if (Object.keys(seeded).length > 0) {
      setPicks((prev) => ({ ...seeded, ...prev }));
    }
    // Auto-select first case
    if (!caseId || !cases.find((c) => c.id === caseId)) {
      setCaseId(cases[0].id);
    }
  }, [cases]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- Current case ----------
  const cur = useMemo(
    () => filteredCases.find((c) => c.id === caseId) || filteredCases[0] || null,
    [filteredCases, caseId]
  );

  // Margin between the top two candidates — a thin gap means a genuinely close call.
  const curMargin = useMemo(() => {
    if (!cur || cur.candidates.length < 2) return null;
    const sorted = [...cur.candidates].sort((a, b) => (b.prob || 0) - (a.prob || 0));
    return (sorted[0].prob || 0) - (sorted[1].prob || 0);
  }, [cur]);

  // ---------- Pick a candidate as TRUE ----------
  const handlePick = useCallback(
    (roeId) => {
      setPicks((p) => ({
        ...p,
        [caseId]: p[caseId] === roeId ? null : roeId,
      }));
    },
    [caseId]
  );

  // ---------- Mark all FALSE ----------
  const handleAllFalse = useCallback(() => {
    setPicks((p) => ({
      ...p,
      [caseId]: p[caseId] === "_all_false_" ? null : "_all_false_",
    }));
  }, [caseId]);

  // ---------- Skip to next case ----------
  const handleSkip = useCallback(() => {
    const idx = filteredCases.findIndex((c) => c.id === caseId);
    if (idx >= 0 && idx < filteredCases.length - 1) {
      setCaseId(filteredCases[idx + 1].id);
    }
  }, [filteredCases, caseId]);

  // ---------- Save labels for current case ----------
  // Persist the FULL decision: TRUE for the chosen candidate and FALSE for the
  // rejected ones (and FALSE for every candidate when "all false"). The rejected
  // candidates are tagged provenance=implied_negative — they are valuable hard
  // negatives for the GBT (same OCOD, similar names, but wrong), and "all false"
  // is now persisted instead of silently dropped.
  const handleSaveCase = useCallback(
    async (targetCaseId) => {
      const theCase = cases.find((c) => c.id === targetCaseId);
      if (!theCase) return;
      const pick = picks[targetCaseId];
      if (!pick) return;

      setSaving((s) => ({ ...s, [targetCaseId]: "saving" }));
      try {
        for (const cand of theCase.candidates) {
          const isTrue = pick !== "_all_false_" && cand.roe.id === pick;
          await api.createLabel({
            ocod_name_clean: theCase.ocod.clean || theCase.ocod.raw,
            jurisdiction_clean: theCase.ocod.jur,
            roe_company_number: cand.roe.id,
            roe_name: cand.roe.raw,
            ocod_name_raw: theCase.ocod.raw,
            ocod_jurisdiction_raw: theCase.ocod.jur,
            is_true_match: isTrue ? "TRUE" : "FALSE",
            reviewer_notes: isTrue ? "Chosen from ambiguous candidates" : "Rejected ambiguous candidate",
            provenance: isTrue ? "manual" : "implied_negative",
            run_id: runId,
          });
        }
        setSaving((s) => ({ ...s, [targetCaseId]: "saved" }));
      } catch {
        setSaving((s) => ({ ...s, [targetCaseId]: "error" }));
      }
    },
    [cases, picks, runId]
  );

  // ---------- Apply all decisions ----------
  const handleApplyAll = useCallback(async () => {
    const decided = cases.filter((c) => picks[c.id]);
    for (const c of decided) {
      await handleSaveCase(c.id);
    }
    if (runId && decided.length > 0) {
      await api.applyLabels(runId);
    }
  }, [cases, picks, handleSaveCase, runId]);

  // ---------- Loading / error states ----------
  if (loading) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Ambiguous cases</h1>
            <p className="page-sub muted pulse">Loading ambiguous cases...</p>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Ambiguous cases</h1>
          </div>
        </div>
        <Empty
          title="Failed to load ambiguous cases"
          sub={error}
          action={
            <button
              className="btn primary"
              onClick={() => window.location.reload()}
            >
              <Icons.refresh size={14} /> Retry
            </button>
          }
        />
      </div>
    );
  }

  if (cases.length === 0) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Ambiguous cases</h1>
            <p className="page-sub">
              When several ROE entries plausibly match one OCOD record, pick the
              correct entity.
            </p>
          </div>
        </div>
        <Empty
          title="No ambiguous cases"
          sub="All matches in this run have a single best candidate. Nothing to disambiguate."
        />
      </div>
    );
  }

  const decidedCount = cases.filter((c) => picks[c.id]).length;
  const candidateRowCount = allItems.length;
  const inspectUrl = inspectCandidate ? companiesHouseUrl(inspectCandidate.roe.id) : null;

  return (
    <div className="content">
      {/* Page header */}
      <div className="page-head">
        <div>
          {runId && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              <span className="mono">{runId}</span>
            </div>
          )}
          <h1 className="page-title">Ambiguous cases</h1>
          <p className="page-sub">
            When several ROE entries plausibly match one OCOD record, pick the
            correct entity. Marking one as TRUE drops the others; you can also
            mark all as FALSE. Showing {fmtNumber(cases.length)} grouped cases from{" "}
            {fmtNumber(candidateRowCount)} candidate rows.
          </p>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
            <strong style={{ color: cases.length - decidedCount > 0 ? "var(--amber)" : "var(--green)" }}>
              {fmtNumber(cases.length - decidedCount)}
            </strong>{" "}
            {cases.length - decidedCount === 1 ? "case" : "cases"} still need a pick ·{" "}
            {fmtNumber(decidedCount)} decided
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <a className="btn" href={api.runFileUrl(runId, "matches_ambiguous.csv")} download>
            <Icons.download size={14} />
            Export
          </a>
          <button
            className="btn primary"
            onClick={handleApplyAll}
            disabled={decidedCount === 0}
          >
            <Icons.check size={14} stroke="#fff" />
            Save & apply decisions
            {decidedCount > 0 && (
              <span
                className="muted"
                style={{ fontSize: 11, marginLeft: 4, color: "rgba(255,255,255,.7)" }}
              >
                ({decidedCount})
              </span>
            )}
          </button>
        </div>
      </div>

      {/* Two-column layout */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "260px 1fr",
          gap: 16,
          alignItems: "flex-start",
        }}
      >
        {/* Left sidebar: case list */}
        <div className="card" style={{ position: "sticky", top: 70 }}>
          <div className="card-h" style={{ padding: "10px 12px" }}>
            <span className="eyebrow">Cases</span>
            <span
              className="muted"
              style={{ fontSize: 11, marginLeft: "auto" }}
            >
              {fmtNumber(filteredCases.length)} / {fmtNumber(cases.length)}
            </span>
          </div>
          <div style={{ padding: 8, borderBottom: "1px solid var(--line)", display: "grid", gap: 8 }}>
            <div className="search" style={{ width: "100%" }}>
              <Icons.search size={14} />
              <input
                className="input"
                placeholder="Search names, number, jurisdiction..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <select
              className="select"
              value={caseFilter}
              onChange={(e) => setCaseFilter(e.target.value)}
            >
              <option value="all">All cases</option>
              <option value="undecided">Undecided only</option>
              <option value="close">Close margin only</option>
            </select>
          </div>
          <div
            style={{
              maxHeight: "calc(100vh - 220px)",
              overflow: "auto",
            }}
          >
            {filteredCases.map((c) => {
              const sel = c.id === caseId;
              const picked = picks[c.id];
              const saveState = saving[c.id];
              return (
                <div
                  key={c.id}
                  onClick={() => setCaseId(c.id)}
                  style={{
                    padding: "10px 12px",
                    borderBottom: "1px solid var(--line)",
                    background: sel ? "var(--ti-red-50)" : "transparent",
                    cursor: "pointer",
                  }}
                >
                  <div style={{ fontWeight: 500, fontSize: 13 }}>
                    {c.ocod.raw}
                  </div>
                  <div className="mono muted" style={{ fontSize: 11 }}>
                    {c.ocod.id} · {c.ocod.jur}
                  </div>
                  <div
                    style={{
                      marginTop: 6,
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                    }}
                  >
                    <span className="tag violet">
                      <span className="dot" />
                      {c.candidates.length} candidate
                      {c.candidates.length !== 1 ? "s" : ""}
                    </span>
                    {picked && (
                      <span className="tag green">
                        <span className="dot" />
                        chosen
                      </span>
                    )}
                    {saveState === "saved" && (
                      <span className="tag green">
                        <span className="dot" />
                        saved
                      </span>
                    )}
                    {saveState === "error" && (
                      <span className="tag" style={{ color: "var(--ti-red)" }}>
                        error
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Right panel: OCOD card + candidate grid */}
        {!cur && (
          <Empty
            title="No cases in this filter"
            sub="Clear the search or switch back to all cases."
          />
        )}
        {cur && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {/* Pinned OCOD card */}
            <div className="card" style={{ borderColor: "var(--ink)" }}>
              <div
                style={{
                  padding: "14px 18px",
                  display: "grid",
                  gridTemplateColumns: "auto 1fr",
                  gap: 16,
                  alignItems: "center",
                }}
              >
                <div
                  className="brand-mark"
                  style={{ background: "var(--ink)", borderRadius: 4 }}
                >
                  OC
                </div>
                <div>
                  <div className="eyebrow">
                    OCOD record (the one we are matching)
                  </div>
                  <div
                    style={{
                      fontSize: 18,
                      fontWeight: 600,
                      letterSpacing: "-0.01em",
                    }}
                  >
                    {cur.ocod.raw}
                  </div>
                  <div className="mono muted" style={{ fontSize: 12 }}>
                    {cur.ocod.id}
                    {cur.ocod.jur && <> · {cur.ocod.jur}</>}
                    {cur.ocod.clean && cur.ocod.clean !== cur.ocod.raw && (
                      <> · clean <span className="mono">{cur.ocod.clean}</span></>
                    )}
                    {cur.ocod.titles > 0 && (
                      <> · {cur.ocod.titles} title{cur.ocod.titles !== 1 ? "s" : ""}</>
                    )}
                    {cur.ocod.registry_date && (
                      <> · OCOD registry date {cur.ocod.registry_date}</>
                    )}
                    {curMargin != null && (
                      <> · margin <span style={{ color: curMargin < 0.05 ? "var(--ti-red)" : "inherit" }}>
                        {curMargin.toFixed(2)}{curMargin < 0.05 ? " (close)" : ""}</span></>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* Candidate cards grid */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: `repeat(${Math.max(
                  2,
                  Math.min(3, cur.candidates.length)
                )}, 1fr)`,
                gap: 14,
              }}
            >
              {cur.candidates.map((c, i) => {
                const isPick =
                  picks[caseId] &&
                  picks[caseId] !== "_all_false_" &&
                  picks[caseId] === c.roe.id;
                const isAllFalse = picks[caseId] === "_all_false_";
                const chUrl = companiesHouseUrl(c.roe.id);
                return (
                  <div
                    key={c.roe.id + "_" + i}
                    className="card"
                    style={{
                      borderColor: isPick
                        ? "var(--green)"
                        : isAllFalse
                          ? "var(--ti-red)"
                          : "var(--line)",
                      background: isPick
                        ? "var(--green-50)"
                        : "var(--surface)",
                      position: "relative",
                      overflow: "hidden",
                    }}
                  >
                    {isPick && (
                      <div
                        style={{
                          position: "absolute",
                          top: 0,
                          left: 0,
                          right: 0,
                          height: 3,
                          background: "var(--green)",
                        }}
                      />
                    )}
                    <div
                      className="card-h"
                      style={{
                        padding: "12px 16px",
                        borderColor: isPick
                          ? "var(--green)"
                          : "var(--line)",
                      }}
                    >
                      <span className="eyebrow">
                        Candidate {String.fromCharCode(65 + i)}
                      </span>
                      <span
                        className="tag"
                        style={{ marginLeft: "auto" }}
                      >
                        {c.roe.id}
                      </span>
                    </div>
                    <div
                      className="card-b"
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 10,
                      }}
                    >
                      <div
                        style={{
                          fontWeight: 600,
                          fontSize: 15,
                          letterSpacing: "-0.005em",
                        }}
                      >
                        {c.roe.raw}
                      </div>
                      {c.roe.clean && c.roe.clean !== c.roe.raw && (
                        <div className="mono muted" style={{ fontSize: 11.5 }}>
                          clean: {c.roe.clean}
                        </div>
                      )}
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 10,
                        }}
                      >
                        <ProbBar p={c.prob} w={70} />
                        <span className="mono" style={{ fontWeight: 600 }}>
                          {fmtProb(c.prob)}
                        </span>
                        <span className="muted" style={{ fontSize: 11 }}>
                          probability
                        </span>
                      </div>
                      <dl
                        className="diff-meta"
                        style={{ gridTemplateColumns: "max-content 1fr" }}
                      >
                        {c.roe.incorp && (
                          <>
                            <dt>incorporated</dt>
                            <dd className="mono">{c.roe.incorp}</dd>
                          </>
                        )}
                        {c.roe.jur && (
                          <>
                            <dt>jurisdiction</dt>
                            <dd className="mono">{c.roe.jur}</dd>
                          </>
                        )}
                        {c.roe.agent && (
                          <>
                            <dt>agent</dt>
                            <dd
                              className="mono"
                              style={{ fontSize: 11.5 }}
                            >
                              {c.roe.agent}
                            </dd>
                          </>
                        )}
                        {c.roe.address && (
                          <>
                            <dt>address</dt>
                            <dd
                              className="mono"
                              style={{
                                fontSize: 11.5,
                                whiteSpace: "normal",
                              }}
                            >
                              {c.roe.address}
                            </dd>
                          </>
                        )}
                      </dl>
                      {c.addressPanel && <AddressPanel address={c.addressPanel} />}
                      <div
                        style={{ display: "flex", gap: 6, marginTop: 4 }}
                      >
                        <button
                          className="btn"
                          style={
                            isPick
                              ? {
                                  background: "var(--green)",
                                  color: "#fff",
                                  borderColor: "var(--green)",
                                  flex: 1,
                                }
                              : { flex: 1 }
                          }
                          onClick={() => handlePick(c.roe.id)}
                        >
                          <Icons.check
                            size={13}
                            stroke={isPick ? "#fff" : "currentColor"}
                          />
                          {isPick ? "Chosen" : "Pick as TRUE"}
                        </button>
                        <button
                          className="btn"
                          title="Inspect at Companies House"
                          aria-label={`Inspect ${c.roe.id} at Companies House`}
                          disabled={!chUrl}
                          onClick={() => setInspectCandidate(c)}
                        >
                          <Icons.link size={13} />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Decision summary */}
            <div className="card">
              <div
                className="card-b"
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr auto",
                  gap: 16,
                  alignItems: "center",
                }}
              >
                <div style={{ fontSize: 13 }}>
                  {picks[caseId] && picks[caseId] !== "_all_false_" ? (
                    <span>
                      <strong style={{ color: "var(--green)" }}>
                        Chosen:
                      </strong>{" "}
                      <span className="mono">{picks[caseId]}</span>. Other
                      candidates will be marked FALSE.
                    </span>
                  ) : picks[caseId] === "_all_false_" ? (
                    <span>
                      <strong style={{ color: "var(--ti-red)" }}>
                        All marked FALSE.
                      </strong>{" "}
                      No candidate matches the OCOD record.
                    </span>
                  ) : (
                    <span className="muted">
                      No candidate chosen yet. Pick one above, or mark all as
                      FALSE if none match.
                    </span>
                  )}
                  {saving[caseId] === "saved" && (
                    <span
                      className="tag green"
                      style={{ marginLeft: 10, verticalAlign: "middle" }}
                    >
                      <span className="dot" />
                      Saved
                    </span>
                  )}
                  {saving[caseId] === "error" && (
                    <span
                      style={{
                        marginLeft: 10,
                        color: "var(--ti-red)",
                        fontSize: 12,
                      }}
                    >
                      Save failed — try again
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {picks[caseId] && (
                    <button
                      className="btn primary"
                      onClick={() => handleSaveCase(caseId)}
                      disabled={saving[caseId] === "saving"}
                    >
                      {saving[caseId] === "saving" ? (
                        <>Saving...</>
                      ) : (
                        <>
                          <Icons.check size={13} stroke="#fff" />
                          Save this case
                        </>
                      )}
                    </button>
                  )}
                  <button className="btn danger" onClick={handleAllFalse}>
                    <Icons.x size={13} />
                    Mark all FALSE
                  </button>
                  <button className="btn" onClick={handleSkip}>
                    Skip <Icons.arrowR size={13} />
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {inspectCandidate && (
        <>
          <div className="scrim" onClick={() => setInspectCandidate(null)} />
          <aside className="sheet" role="dialog" aria-modal="true" aria-label="Companies House inspection">
            <div className="sheet-h">
              <div>
                <div className="eyebrow">Companies House</div>
                <div style={{ fontSize: 18, fontWeight: 650 }}>
                  {inspectCandidate.roe.id}
                </div>
              </div>
              <button
                className="btn"
                style={{ marginLeft: "auto" }}
                onClick={() => setInspectCandidate(null)}
                aria-label="Close inspection"
              >
                <Icons.x size={14} />
              </button>
            </div>
            <div className="sheet-b">
              <div style={{ fontWeight: 650, fontSize: 18, marginBottom: 8 }}>
                {inspectCandidate.roe.raw}
              </div>
              <dl className="diff-meta">
                <dt>company number</dt>
                <dd className="mono">{inspectCandidate.roe.id}</dd>
                {inspectCandidate.roe.jur && (
                  <>
                    <dt>jurisdiction</dt>
                    <dd className="mono">{inspectCandidate.roe.jur}</dd>
                  </>
                )}
                {inspectCandidate.roe.incorp && (
                  <>
                    <dt>incorporated</dt>
                    <dd className="mono">{inspectCandidate.roe.incorp}</dd>
                  </>
                )}
                <dt>probability</dt>
                <dd className="mono">{fmtProb(inspectCandidate.prob)}</dd>
                {inspectCandidate.roe.agent && (
                  <>
                    <dt>agent</dt>
                    <dd className="mono">{inspectCandidate.roe.agent}</dd>
                  </>
                )}
                {inspectCandidate.roe.address && (
                  <>
                    <dt>address</dt>
                    <dd className="mono" style={{ whiteSpace: "normal" }}>
                      {inspectCandidate.roe.address}
                    </dd>
                  </>
                )}
              </dl>
            </div>
            <div className="sheet-f">
              <a
                className="btn primary"
                href={inspectUrl || undefined}
                target="_blank"
                rel="noreferrer"
              >
                <Icons.link size={14} stroke="#fff" />
                Open Companies House
              </a>
              <button className="btn" onClick={() => setInspectCandidate(null)}>
                Close
              </button>
            </div>
          </aside>
        </>
      )}
    </div>
  );
}

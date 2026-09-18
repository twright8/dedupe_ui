/* ============================================================
   Screen: Review queue — table + diff view, threshold control,
   keyboard nav, live re-bucketing, feature breakdown
   ============================================================ */

import { useState, useEffect, useMemo, useCallback, useRef, Fragment } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { ProbBar, BandTag, fmtProb, fmtNumber } from "../components/ProbBar";
import { Empty } from "../components/Empty";
import { DiffHero } from "../components/DiffHero";
import { AddressPanel } from "../components/AddressPanel";
import { FeatureBreakdown, FeaturePopover } from "../components/FeatureBreakdown";
import { GbtExplain } from "../components/GbtExplain";
import { ThresholdPanel } from "../components/ThresholdPanel";
import MethodologyNotes from "../components/MethodologyNotes";
import { useLabels } from "../hooks/useLabels";
import { useKeyboardNav } from "../hooks/useKeyboardNav";

// ---------- Normalise API match item to a consistent shape ----------
function normaliseItem(m) {
  const bucket = m.bucket || m._bucket || null;
  const band =
    m.band ||
    (bucket === "high" || bucket === "exact"
      ? "auto-accept"
      : bucket === "review"
        ? "review"
        : bucket === "ambiguous"
          ? "ambiguous"
          : null);
  return {
    id: m.match_id || m.id,
    ocod_name_raw: m.ocod_name_raw || "",
    ocod_name_clean: m.ocod_name_clean || m.ocod_name_raw || "",
    roe_name_raw: m.roe_name_raw || "",
    roe_name_clean: m.roe_name_clean || m.roe_name_raw || "",
    roe_company_number: m.roe_company_number || "",
    matched_name_type: m.matched_name_type || "current",
    roe_name_matched_raw: m.roe_name_matched_raw || "",
    ocod_id: m.ocod_id || "",
    jurisdiction_clean: m.jurisdiction_clean || "",
    ocod_jurisdiction_raw: m.ocod_jurisdiction_raw || m.jurisdiction_clean || "",
    match_probability: m.match_probability ?? m.prob ?? 0,
    match_method: m.match_method || m.method || "probabilistic",
    features: m.features || {},
    label: m.label || null,
    label_reviewer: m.label_reviewer || null,
    // Side-by-side address block (display + review signal only; never a score input).
    address: m.address || null,
    band,
    bucket,
    run_id: m.run_id || "",
    // Keep raw for API body
    _raw: m,
  };
}

function companiesHouseUrl(companyNumber) {
  if (!companyNumber) return null;
  return `https://find-and-update.company-information.service.gov.uk/company/${encodeURIComponent(companyNumber)}`;
}

// ---------- Main screen component ----------
export default function ReviewScreen() {
  const { id: runId } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  // View mode
  const [mode, setMode] = useState("table");
  const [groupByEntity, setGroupByEntity] = useState(false);

  // Thresholds (initialised from diagnostics on load)
  const [threshold, setThreshold] = useState(0.92);
  const [reviewLow, setReviewLow] = useState(0.50);
  const [pipelineThreshold, setPipelineThreshold] = useState(null);
  const [committedThreshold, setCommittedThreshold] = useState(null);

  // Filters & sorting
  const [filter, setFilter] = useState("review");
  const [jurFilter, setJurFilter] = useState("all");
  const [search, setSearch] = useState(() => searchParams.get("search") || "");
  const [sortCol, setSortCol] = useState("prob");
  const [sortAsc, setSortAsc] = useState(false);
  const [hideLabelled, setHideLabelled] = useState(false);
  // Former-name matches are shown inline (badged) by default; this isolates them.
  const [formerOnly, setFormerOnly] = useState(false);

  // Selection
  const [selectedId, setSelectedId] = useState(null);

  // Probabilistic items (loaded on mount — the 726 Phase 2 scored pairs)
  const [probItems, setProbItems] = useState([]);
  const [histogram, setHistogram] = useState([]);
  const [scoreSource, setScoreSource] = useState("simple matcher");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [committing, setCommitting] = useState(false);

  // Cockpit: brushed score window + bulk-on-slice staging
  const [brushLo, setBrushLo] = useState(null);
  const [brushHi, setBrushHi] = useState(null);
  const [staged, setStaged] = useState(() => ({})); // id -> provisional 'TRUE' | 'FALSE'
  const [stageOnlyUnlabelled, setStageOnlyUnlabelled] = useState(true);
  const [committingBulk, setCommittingBulk] = useState(false);

  // Exact items (lazy-loaded when Exact tab selected)
  const [exactItems, setExactItems] = useState([]);
  const [exactPage, setExactPage] = useState(1);
  const [exactTotal, setExactTotal] = useState(0);
  const [exactLoading, setExactLoading] = useState(false);

  // Ambiguous count for the link button
  const [ambiguousCount, setAmbiguousCount] = useState(0);

  // Labels hook
  const { labels, notes, setLabel, setNote, pending } = useLabels([]);

  // ---------- Load probabilistic data + diagnostics ----------
  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    setError(null);

    Promise.all([
      api.getRunMatches(runId, { bucket: "review", per_page: 10000 }),
      api.getRunMatches(runId, { bucket: "high", per_page: 10000, match_method: "probabilistic" }),
      api.getRunDiagnostics(runId),
    ])
      .then(([reviewData, highProbData, diagData]) => {
        const reviewItems = (reviewData.items || []).map(normaliseItem);
        const highItems = (highProbData.items || []).map(normaliseItem);
        const items = [...highItems, ...reviewItems];
        setProbItems(items);

        const bc = reviewData.bucket_counts || highProbData.bucket_counts || {};
        setAmbiguousCount(bc.ambiguous || 0);
        setExactTotal(bc.exact || 0);

        if (diagData && diagData.histogram) {
          setHistogram(diagData.histogram);
        } else if (diagData && diagData.probability_histogram) {
          setHistogram(diagData.probability_histogram);
        }
        setScoreSource(diagData?.score_column === "gbt_score" ? "trained model" : "simple matcher");
        if (diagData?.thresholds) {
          if (diagData.thresholds.threshold_high != null) {
            setThreshold(+diagData.thresholds.threshold_high);
            setCommittedThreshold(+diagData.thresholds.threshold_high);
          }
          if (diagData.thresholds.threshold_review != null) {
            setReviewLow(+diagData.thresholds.threshold_review);
            setPipelineThreshold(+diagData.thresholds.threshold_review);
          }
        }

        for (const item of items) {
          if (item.label) {
            setLabel(item.id, item.label, "", null);
          }
        }

        const reviewItem = items.find((m) => m.band === "review");
        if (reviewItem) setSelectedId(reviewItem.id);
        else if (items.length > 0) setSelectedId(items[0].id);

        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || "Failed to load review data");
        setLoading(false);
      });
  }, [runId, reloadToken]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------- Lazy-load exact matches when tab selected ----------
  useEffect(() => {
    if (filter !== "exact" || !runId) return;
    setExactLoading(true);
    api
      .getRunMatches(runId, { bucket: "exact", page: exactPage, per_page: 50 })
      .then((data) => {
        setExactItems((data.items || []).map(normaliseItem));
        setExactTotal(data.total || 0);
      })
      .catch(() => setExactItems([]))
      .finally(() => setExactLoading(false));
  }, [filter, exactPage, runId]);

  // ---------- Re-bucket probabilistic items per current thresholds ----------
  const bucketed = useMemo(
    () =>
      probItems.map((m) => {
        const band = +m.match_probability >= threshold ? "auto-accept" : "review";
        return { ...m, band };
      }),
    [probItems, threshold]
  );

  // Entities that already have a chosen match: once one candidate of an OCOD entity is
  // marked TRUE, the siblings are "resolved" — they can't also be the match (one per
  // entity), so we grey them out rather than auto-writing them as FALSE (a losing
  // candidate isn't a real hard-negative; see the model's unit_mismatch handling).
  const entitiesWithTrue = useMemo(() => {
    const s = new Set();
    const scan = (arr) => arr.forEach((m) => { if (labels[m.id] === "TRUE") s.add(entityKeyOf(m)); });
    scan(bucketed); scan(exactItems);
    return s;
  }, [bucketed, exactItems, labels]);

  // ---------- Filter & sort ----------
  const filtered = useMemo(() => {
    const source = filter === "exact" ? exactItems : bucketed;
    const out = source.filter((m) => {
      if (filter === "review" && m.band !== "review") return false;
      if (filter === "accepted" && m.band !== "auto-accept") return false;
      if (jurFilter !== "all" && m.jurisdiction_clean !== jurFilter) return false;
      if (brushLo != null && brushHi != null) {
        const p = +m.match_probability;
        if (!(p >= brushLo && p <= brushHi)) return false;
      }
      if (
        search &&
        !(
          m.ocod_name_raw.toLowerCase().includes(search.toLowerCase()) ||
          m.roe_name_raw.toLowerCase().includes(search.toLowerCase())
        )
      )
        return false;
      if (hideLabelled && (labels[m.id] || entitiesWithTrue.has(entityKeyOf(m)))) return false;
      if (formerOnly && m.matched_name_type !== "former") return false;
      return true;
    });

    const dir = sortAsc ? 1 : -1;
    out.sort((a, b) => {
      let av, bv;
      if (sortCol === "prob") {
        av = +a.match_probability || 0;
        bv = +b.match_probability || 0;
      } else if (sortCol === "ocod") {
        av = (a.ocod_name_raw || "").toLowerCase();
        bv = (b.ocod_name_raw || "").toLowerCase();
      } else if (sortCol === "roe") {
        av = (a.roe_name_raw || "").toLowerCase();
        bv = (b.roe_name_raw || "").toLowerCase();
      } else if (sortCol === "jur") {
        av = (a.jurisdiction_clean || "").toLowerCase();
        bv = (b.jurisdiction_clean || "").toLowerCase();
      } else {
        return 0;
      }
      if (av < bv) return -dir;
      if (av > bv) return dir;
      return 0;
    });
    return out;
  }, [bucketed, exactItems, filter, jurFilter, search, sortCol, sortAsc, brushLo, brushHi, hideLabelled, formerOnly, labels, entitiesWithTrue]);

  // ---------- Counts (live from slider re-bucketing) ----------
  const counts = useMemo(() => {
    const review = bucketed.filter((m) => m.band === "review").length;
    const accepted = bucketed.filter((m) => m.band === "auto-accept").length;
    return {
      review,
      accepted,
      all: probItems.length,
      exact: exactTotal,
      ambiguous: ambiguousCount,
      pending: bucketed.filter((m) => m.band === "review" && !labels[m.id]).length,
      labelled: Object.keys(labels).length,
      former: (filter === "exact" ? exactItems : bucketed).filter((m) => m.matched_name_type === "former").length,
    };
  }, [bucketed, probItems, exactTotal, ambiguousCount, labels, filter, exactItems]);

  // ---------- Unique jurisdictions for filter dropdown ----------
  const jurisdictions = useMemo(() => {
    const set = new Set();
    const source = filter === "exact" ? exactItems : probItems;
    for (const m of source) {
      if (m.jurisdiction_clean) set.add(m.jurisdiction_clean);
    }
    return [...set].sort();
  }, [probItems, exactItems, filter]);

  // ---------- Currently selected item ----------
  const current = useMemo(() => {
    return (
      filtered.find((m) => m.id === selectedId) ||
      filtered[0] ||
      null
    );
  }, [filtered, selectedId]);

  // ---------- Label handler (wraps hook with raw data) ----------
  const handleLabel = useCallback(
    (matchId, value) => {
      const all = [...probItems, ...exactItems];
      const item = all.find((m) => m.id === matchId);
      const noteText = notes[matchId] || "";
      setLabel(matchId, value, noteText, item ? { ...item._raw, run_id: runId } : null);
    },
    [probItems, exactItems, notes, setLabel, runId]
  );

  const handleCommitThresholds = useCallback(() => {
    setCommitting(true);
    api
      .reBucketRun(runId, { threshold_high: threshold, threshold_review: reviewLow })
      .then(() => {
        setPipelineThreshold(reviewLow);
        setCommittedThreshold(threshold);
        setReloadToken((token) => token + 1);
      })
      .catch((err) => alert(err.message))
      .finally(() => setCommitting(false));
  }, [runId, threshold, reviewLow]);

  // ---------- Brush (labelling window) ----------
  const onBrush = useCallback((lo, hi) => {
    setBrushLo(lo); setBrushHi(hi);
    // Surface the most-likely-wrong first: lowest scores in the band (the ones to
    // flip FALSE when you've bulk-marked a high band TRUE).
    setSortCol("prob"); setSortAsc(true);
  }, []);
  const clearBrush = useCallback(() => { setBrushLo(null); setBrushHi(null); }, []);

  // ---------- Bulk-on-slice staging ----------
  const stagedCount = Object.keys(staged).length;
  const previewDelta = useMemo(() => {
    if (committedThreshold == null) return null;
    const committedAccepted = probItems.filter((m) => +m.match_probability >= committedThreshold).length;
    const committedReview = probItems.length - committedAccepted;
    return {
      threshold,
      committed: committedThreshold,
      acceptedDelta: counts.accepted - committedAccepted,
      reviewDelta: counts.review - committedReview,
      changed: bucketed.filter((m) => {
        const now = +m.match_probability >= threshold;
        const before = +m.match_probability >= committedThreshold;
        return now !== before;
      }).length,
    };
  }, [bucketed, committedThreshold, counts.accepted, counts.review, probItems, threshold]);

  const sliceParts = useMemo(() => {
    const parts = [];
    const bucketLabel = {
      review: "bucket: Review",
      accepted: "bucket: Auto-accepted",
      all: "bucket: All probabilistic",
      exact: "bucket: Exact",
    }[filter];
    if (bucketLabel) parts.push(bucketLabel);
    if (brushLo != null && brushHi != null) {
      parts.push(`score: ${brushLo.toFixed(2)}-${brushHi.toFixed(2)}`);
    }
    if (jurFilter !== "all") parts.push(`jurisdiction: ${jurFilter}`);
    if (search.trim()) parts.push(`search: "${search.trim()}"`);
    return parts;
  }, [filter, brushLo, brushHi, jurFilter, search]);

  const stageSlice = useCallback((verdict) => {
    setStaged((prev) => {
      const next = { ...prev };
      for (const m of filtered) {
        if (stageOnlyUnlabelled && labels[m.id]) continue;
        next[m.id] = verdict;
      }
      return next;
    });
  }, [filtered, stageOnlyUnlabelled, labels]);
  const toggleStaged = useCallback((id, verdict) => {
    setStaged((prev) => ({ ...prev, [id]: verdict }));
  }, []);
  const cancelStaging = useCallback(() => setStaged({}), []);
  const commitStaged = useCallback(async () => {
    const entries = Object.entries(staged);
    if (entries.length === 0) return;
    const byId = new Map([...probItems, ...exactItems].map((m) => [m.id, m]));
    const lbls = [];
    for (const [id, verdict] of entries) {
      const m = byId.get(id);
      const r = (m && m._raw) || {};
      if (!r.roe_company_number) continue;
      lbls.push({
        _ocodKey: `${(r.ocod_name_clean || r.ocod_name_raw || "").toUpperCase()}|${(r.jurisdiction_clean || "").toUpperCase()}`,
        _score: +(m.match_probability) || 0,
        ocod_name_clean: r.ocod_name_clean || r.ocod_name_raw,
        jurisdiction_clean: r.jurisdiction_clean,
        roe_company_number: r.roe_company_number,
        roe_name: r.roe_name_raw || r.roe_name,
        ocod_name_raw: r.ocod_name_raw,
        ocod_jurisdiction_raw: r.ocod_jurisdiction_raw || r.jurisdiction_clean,
        is_true_match: verdict,
        reviewer_notes: "Bulk-labelled by score range",
        provenance: "bulk_range",
        run_id: runId,
      });
    }
    // One TRUE per OCOD: if a band marked several candidates of the same entity TRUE,
    // keep only the HIGHEST-scored as TRUE and demote the rest to FALSE (a useful hard
    // negative). The store enforces this anyway; doing it here makes the *kept* one the
    // best candidate rather than whatever the batch happened to process last.
    const bestTrue = {};
    for (const l of lbls) {
      if (l.is_true_match !== "TRUE") continue;
      if (!bestTrue[l._ocodKey] || l._score > bestTrue[l._ocodKey]._score) bestTrue[l._ocodKey] = l;
    }
    let demoted = 0;
    for (const l of lbls) {
      if (l.is_true_match === "TRUE" && bestTrue[l._ocodKey] !== l) {
        l.is_true_match = "FALSE";
        l.reviewer_notes = "Auto-FALSE: a higher-scored candidate was chosen for this entity";
        l.provenance = "implied_negative";
        demoted += 1;
      }
    }
    const payload = lbls.map(({ _ocodKey, _score, ...rest }) => rest);
    setCommittingBulk(true);
    try {
      for (let i = 0; i < payload.length; i += 500) {
        await api.createLabelsBatch({ labels: payload.slice(i, i + 500) });
      }
      setStaged({});
      setReloadToken((t) => t + 1);
    } catch (err) {
      alert(err.message);
    } finally {
      setCommittingBulk(false);
    }
  }, [staged, probItems, exactItems, runId]);

  // ---------- Keyboard navigation ----------
  useKeyboardNav({
    items: filtered,
    selectedId: current?.id,
    setSelectedId,
    onLabel: handleLabel,
    enabled: mode === "diff",
  });

  // ---------- Loading / error states ----------
  if (loading) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Review queue</h1>
            <p className="page-sub muted pulse">Loading match data...</p>
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
            <h1 className="page-title">Review queue</h1>
          </div>
        </div>
        <Empty
          title="Failed to load review data"
          sub={error}
          action={
            <button className="btn primary" onClick={() => window.location.reload()}>
              <Icons.refresh size={14} /> Retry
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="content" style={{ maxWidth: "none", paddingRight: 28 }}>
      {/* Page header */}
      <div className="page-head">
        <div>
          {runId && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              <span className="mono">{runId}</span>
            </div>
          )}
          <h1 className="page-title">Review queue</h1>
          <p className="page-sub">
            {counts.pending} pairs the computer wasn't sure about. <strong>Drag a band on the
            chart</strong> to grab a group of similar scores, <strong>mark them all</strong> TRUE or
            FALSE, fix the exceptions in the list, then save. Scores come from the{" "}
            <strong>{scoreSource}</strong> (0 = probably not a match, 1 = probably a match).
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div className="seg">
            <button
              className={mode === "table" ? "on" : ""}
              onClick={() => setMode("table")}
            >
              <Icons.table size={13} />Table
            </button>
            <button
              className={mode === "diff" ? "on" : ""}
              onClick={() => setMode("diff")}
            >
              <Icons.diff size={13} />Diff
            </button>
          </div>
          <a className="btn" href={api.runFileUrl(runId, "matches_for_review.csv")} download>
            <Icons.download size={14} />Export queue
          </a>
          <button
            className="btn primary"
            onClick={() => api.applyLabels(runId).then(() => alert("Library labels applied to this run.")).catch((err) => alert(err.message))}
            title="Re-apply the durable label library to this run's outputs; this does not commit staged review labels."
          >
            <Icons.check size={14} stroke="#fff" />Apply library labels to run
          </button>
        </div>
      </div>

      {/* Shared methodology notes — persistent across users */}
      <MethodologyNotes />

      {/* Threshold panel — only shown for probabilistic tabs */}
      {filter !== "exact" && (
        <>
          <ThresholdPanel
            threshold={threshold}
            setThreshold={setThreshold}
            reviewLow={reviewLow}
            setReviewLow={setReviewLow}
            counts={counts}
            histogram={histogram}
            pipelineThreshold={pipelineThreshold}
            previewDelta={previewDelta}
            brushLo={brushLo}
            brushHi={brushHi}
            onBrush={onBrush}
            isGbt={scoreSource === "trained model"}
            scoreLabel={scoreSource === "trained model" ? "calibrated model score" : "simple-matcher score"}
          />
          {/* SECONDARY · output cutoff (changes the download; not labelling) */}
          <div
            style={{
              display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
              margin: "-2px 0 16px", padding: "8px 12px",
              background: "var(--surface-sub)", border: "1px solid var(--line)",
              borderRadius: "var(--r-md)", fontSize: 12,
            }}
          >
            <span className="eyebrow" style={{ margin: 0 }}>Output cutoff</span>
            <span className="muted">
              Ships <span className="mono">&ge; {threshold.toFixed(2)}</span>, drops{" "}
              <span className="mono">&le; {reviewLow.toFixed(2)}</span> — to the downloaded file, a
              provisional rule re-checked every run. It{" "}
              <strong>saves no answers and teaches nothing</strong>.
              {previewDelta && previewDelta.acceptedDelta !== 0 && (
                <> Moving the line would shift{" "}
                  <span className="mono">{previewDelta.acceptedDelta > 0 ? "+" : ""}{fmtNumber(previewDelta.acceptedDelta)}</span>{" "}
                  pairs into auto-accept.</>
              )}
            </span>
            <span style={{ marginLeft: "auto" }} />
            <button className="btn sm" onClick={handleCommitThresholds} disabled={committing}>
              {committing ? "Applying…" : "Apply cutoffs to the run"}
            </button>
          </div>

          {/* PRIMARY · label these pairs (your saved answers) */}
          <div className="card" style={{ marginBottom: 12, borderColor: "var(--line-strong)" }}>
            <div className="card-h" style={{ paddingBottom: 6 }}>
              <Icons.check size={15} />
              <h3 style={{ margin: 0 }}>Label these pairs</h3>
              <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                your answers — saved for good, and what teaches the model
              </span>
            </div>
            <div className="card-b" style={{ paddingTop: 10 }}>
              <div style={{ fontSize: 13, marginBottom: 10 }}>
                {brushLo != null ? (
                  <>Band <span className="mono">{brushLo.toFixed(2)}–{brushHi.toFixed(2)}</span> selected on the chart — </>
                ) : (
                  <>Whole <strong>{filter}</strong> view (drag a band on the chart to narrow) — </>
                )}
                <strong>{stageOnlyUnlabelled ? filtered.filter((m) => !labels[m.id]).length : filtered.length}</strong> pairs will be marked
                {stageOnlyUnlabelled && filtered.filter((m) => labels[m.id]).length > 0 && (
                  <span className="muted"> ({filtered.filter((m) => labels[m.id]).length} already answered, skipped)</span>
                )}.
                {brushLo != null && (
                  <> <button className="btn sm" onClick={clearBrush}>clear band</button></>
                )}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <button
                  className="btn lg"
                  style={{ borderColor: "var(--green)", color: "var(--green)", fontWeight: 600 }}
                  onClick={() => stageSlice("TRUE")}
                  disabled={filtered.length === 0}
                >
                  <Icons.check size={14} /> Mark all TRUE
                </button>
                <button
                  className="btn lg"
                  style={{ borderColor: "var(--ti-red)", color: "var(--ti-red)", fontWeight: 600 }}
                  onClick={() => stageSlice("FALSE")}
                  disabled={filtered.length === 0}
                >
                  Mark all FALSE
                </button>
                <label className="muted" style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5, marginLeft: 6 }}>
                  <input type="checkbox" checked={stageOnlyUnlabelled} onChange={(e) => setStageOnlyUnlabelled(e.target.checked)} />
                  skip ones I've already answered
                </label>
              </div>
              <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
                Nothing saves yet — you'll review and flip the wrong ones first.{" "}
                {brushLo != null && brushHi <= 0.7
                  ? <><strong>Low-scoring pairs are usually FALSE</strong> — recording NOs teaches the model the most.</>
                  : <>The model learns most from the <strong>uncertain middle</strong>, not the confident top — the run's “Next label batch” (Diagnostics tab) picks the pairs worth your time.</>}
              </div>
            </div>
          </div>

          {stagedCount > 0 && (
            <div className="card" style={{ marginBottom: 14, borderColor: "var(--ti-red)", borderWidth: 2, background: "var(--ti-red-50)" }}>
              <div className="card-b" style={{ display: "flex", alignItems: "center", gap: 14, padding: "12px 16px", flexWrap: "wrap" }}>
                <span style={{ fontSize: 13.5 }}>
                  <strong>{stagedCount} answer{stagedCount === 1 ? "" : "s"} staged</strong>
                  {" ("}<span style={{ color: "var(--green)", fontWeight: 600 }}>{Object.values(staged).filter((v) => v === "TRUE").length} TRUE</span>
                  {" / "}<span style={{ color: "var(--ti-red)", fontWeight: 600 }}>{Object.values(staged).filter((v) => v === "FALSE").length} FALSE</span>
                  {") — "}<strong>not saved yet.</strong> Flip any wrong ones in the list below, then save.
                </span>
                <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
                  <span className="muted" style={{ fontSize: 11 }}>saved for good · becomes train/test data</span>
                  <button className="btn primary" onClick={commitStaged} disabled={committingBulk}>
                    {committingBulk ? "Saving…" : `Save ${stagedCount} answer${stagedCount === 1 ? "" : "s"}`}
                  </button>
                  <button className="btn" onClick={cancelStaging}>Discard</button>
                </span>
              </div>
            </div>
          )}
        </>
      )}

      {/* Toolbar: bucket tabs, search, jurisdiction filter */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginTop: 16,
          marginBottom: 10,
          flexWrap: "wrap",
        }}
      >
        <div className="seg">
          {[
            { id: "review", lab: "Review", n: counts.review },
            { id: "accepted", lab: "Auto-accepted", n: counts.accepted },
            { id: "all", lab: "All probabilistic", n: counts.all },
            { id: "exact", lab: "Exact", n: counts.exact },
          ].map((f) => (
            <button
              key={f.id}
              className={filter === f.id ? "on" : ""}
              onClick={() => { setFilter(f.id); setSelectedId(null); }}
            >
              {f.lab} <span className="muted">&middot; {fmtNumber(f.n)}</span>
            </button>
          ))}
        </div>
        <div className="search" style={{ width: 280 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search name or jurisdiction..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <select
          className="select"
          style={{ width: 220 }}
          value={jurFilter}
          onChange={(e) => setJurFilter(e.target.value)}
        >
          <option value="all">All jurisdictions</option>
          {jurisdictions.map((j) => (
            <option key={j} value={j}>
              {j}
            </option>
          ))}
        </select>
        {mode === "table" && filter !== "exact" && (
          <button
            className="btn sm"
            style={groupByEntity ? { background: "var(--ink)", color: "#fff", borderColor: "var(--ink)" } : {}}
            onClick={() => setGroupByEntity((v) => !v)}
            title="Collapse one OCOD entity's multiple ROE candidates into a single group"
          >
            <Icons.ambiguous size={13} /> Group by entity
          </button>
        )}
        {filter !== "exact" && (
          <button
            className="btn sm"
            style={hideLabelled ? { background: "var(--ink)", color: "#fff", borderColor: "var(--ink)" } : {}}
            onClick={() => setHideLabelled((v) => !v)}
            title="Hide pairs you've already marked — show only what's left to review"
          >
            {hideLabelled ? "Showing unlabelled only" : "Hide labelled"}
          </button>
        )}
        <button
          className="btn sm"
          style={formerOnly ? { background: "var(--amber, #b7791f)", color: "#fff", borderColor: "var(--amber, #b7791f)" } : {}}
          onClick={() => setFormerOnly((v) => !v)}
          title="Show only matches made via a company's former (previous) name"
        >
          {formerOnly ? "Showing former-name only" : "Former-name matches"}
          <span className="muted" style={{ marginLeft: 4 }}>&middot; {fmtNumber(counts.former)}</span>
        </button>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 12 }}>
          {filter === "exact"
            ? `Page ${exactPage} · ${fmtNumber(exactTotal)} exact matches`
            : `Showing ${filtered.length} of ${fmtNumber(probItems.length)} probabilistic`}
        </span>
        {counts.ambiguous > 0 && (
          <button
            className="btn sm"
            onClick={() => navigate(runId ? `/runs/${runId}/ambiguous` : "/ambiguous")}
          >
            <Icons.ambiguous size={13} /> {counts.ambiguous} ambiguous &rarr;
          </button>
        )}
      </div>

      {/* Main view area */}
      {filter === "exact" && exactLoading ? (
        <p className="muted pulse" style={{ padding: 20 }}>Loading exact matches...</p>
      ) : mode === "table" ? (
        <>
          <ReviewTable
            items={filtered}
            labels={labels}
            entitiesWithTrue={entitiesWithTrue}
            runId={runId}
            onLabel={handleLabel}
            staged={staged}
            onStage={toggleStaged}
            threshold={threshold}
            reviewLow={reviewLow}
            groupBy={groupByEntity}
            selectedId={selectedId}
            setSelectedId={setSelectedId}
            sortCol={sortCol}
            sortAsc={sortAsc}
            onSort={(col) => {
              if (col === sortCol) setSortAsc(!sortAsc);
              else { setSortCol(col); setSortAsc(col === "ocod" || col === "roe" || col === "jur"); }
            }}
          />
          {filter === "exact" && exactTotal > 50 && (
            <div style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 12 }}>
              <button
                className="btn sm"
                disabled={exactPage <= 1}
                onClick={() => setExactPage((p) => Math.max(1, p - 1))}
              >
                &larr; Prev
              </button>
              <span className="muted" style={{ fontSize: 12, lineHeight: "28px" }}>
                Page {exactPage} of {Math.ceil(exactTotal / 50)}
              </span>
              <button
                className="btn sm"
                disabled={exactPage >= Math.ceil(exactTotal / 50)}
                onClick={() => setExactPage((p) => p + 1)}
              >
                Next &rarr;
              </button>
            </div>
          )}
        </>
      ) : (
          <ReviewDiff
            items={filtered}
            current={current}
            labels={labels}
            onLabel={handleLabel}
            staged={staged}
            onStage={toggleStaged}
            threshold={threshold}
            reviewLow={reviewLow}
            notes={notes}
            setNote={setNote}
            setSelectedId={setSelectedId}
            runId={runId}
            isGbt={scoreSource === "trained model"}
        />
      )}
    </div>
  );
}

// ============================================================
// ReviewTable — table view with feature popover
// ============================================================

const entityKeyOf = (m) => `${m.ocod_name_clean || m.ocod_name_raw}||${m.jurisdiction_clean}`;

function ReviewTable({ items, labels, entitiesWithTrue, onLabel, staged, onStage, threshold, reviewLow, groupBy, selectedId, setSelectedId, sortCol, sortAsc, onSort, runId }) {
  const [popup, setPopup] = useState(null);
  const explainCache = useRef({});   // m.id -> contributions | false (tried, none)
  const showPopup = (event, m, pinned = false) => {
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    // Toggle off if re-clicking the already-pinned one.
    if (popup?.id === m.id && popup.pinned && pinned) { setPopup(null); return; }
    // Load the model's per-feature contributions on the SAME hover/click as the Splink
    // features (cached per pair, so it's fetched once). null = loading, false = no model.
    const canExplain = !!(runId && m.ocod_name_clean && m.roe_company_number);
    const cached = explainCache.current[m.id];
    setPopup({
      id: m.id, rect, features: m.features, pinned,
      explain: cached !== undefined ? cached : (canExplain ? null : false),
    });
    if (cached === undefined && canExplain) {
      api.explainPair(runId, {
        ocod_name_clean: m.ocod_name_clean,
        jurisdiction_clean: m.jurisdiction_clean,
        roe_company_number: m.roe_company_number,
      })
        .then((d) => { explainCache.current[m.id] = d; setPopup((cur) => (cur && cur.id === m.id ? { ...cur, explain: d } : cur)); })
        .catch(() => { explainCache.current[m.id] = false; setPopup((cur) => (cur && cur.id === m.id ? { ...cur, explain: false } : cur)); });
    }
  };

  // Entity aggregates (count + margin) for the group headers, and an entity-ordered
  // list so a single OCOD's candidates sit together under one header.
  const { displayItems, entityInfo } = useMemo(() => {
    if (!groupBy) return { displayItems: items, entityInfo: {} };
    const info = {};
    for (const m of items) {
      const k = entityKeyOf(m);
      (info[k] ||= { probs: [], ocod: m.ocod_name_raw || m.ocod_name_clean, jur: m.jurisdiction_clean });
      info[k].probs.push(+m.match_probability || 0);
    }
    for (const k of Object.keys(info)) {
      const ps = info[k].probs.sort((a, b) => b - a);
      info[k].count = ps.length;
      info[k].top = ps[0] ?? 0;
      info[k].margin = ps.length >= 2 ? ps[0] - ps[1] : null;
    }
    const ordered = [...items].sort((a, b) => {
      const ia = info[entityKeyOf(a)], ib = info[entityKeyOf(b)];
      if (ib.top !== ia.top) return ib.top - ia.top;              // best entities first
      const ka = entityKeyOf(a), kb = entityKeyOf(b);
      if (ka !== kb) return ka < kb ? -1 : 1;                     // keep an entity together
      return (+b.match_probability || 0) - (+a.match_probability || 0); // best candidate first
    });
    return { displayItems: ordered, entityInfo: info };
  }, [items, groupBy]);

  const SortTh = ({ col, children, style }) => {
    const active = sortCol === col;
    return (
      <th
        style={{ ...style, cursor: "pointer", userSelect: "none" }}
        onClick={() => onSort?.(col)}
      >
        {children} {active ? (sortAsc ? "▲" : "▼") : ""}
      </th>
    );
  };

  if (items.length === 0) {
    return (
      <Empty
        title="No matches in this filter"
        sub="Try widening the threshold range or clearing the search."
      />
    );
  }

  return (
    <div className="tbl-wrap" style={{ position: "relative" }}>
      <table className="t">
        <thead>
          <tr>
            <th style={{ width: 28 }}></th>
            <SortTh col="ocod">OCOD name (raw)</SortTh>
            <SortTh col="roe">ROE name (raw)</SortTh>
            <SortTh col="jur">Jurisdiction</SortTh>
            <SortTh col="prob" style={{ width: 90 }}>Prob.</SortTh>
            <th>Method</th>
            <th>Features</th>
            <th>Label</th>
          </tr>
        </thead>
        <tbody>
          {(() => { let lastKey = null; return displayItems.map((m) => {
            const lab = labels[m.id];
            const sel = selectedId === m.id;
            const gkey = entityKeyOf(m);
            const showHead = groupBy && gkey !== lastKey;
            lastKey = gkey;
            const ginfo = showHead ? entityInfo[gkey] : null;
            const stagedVal = staged ? staged[m.id] : undefined;
            const isStaged = stagedVal !== undefined;
            const effTrue = isStaged ? stagedVal === "TRUE" : lab === "TRUE";
            const effFalse = isStaged ? stagedVal === "FALSE" : lab === "FALSE";
            const overrides =
              lab &&
              ((lab === "TRUE" && +m.match_probability < threshold) ||
                (lab === "FALSE" && +m.match_probability >= threshold));
            const clickTrue = () => (isStaged ? onStage(m.id, "TRUE") : onLabel(m.id, lab === "TRUE" ? null : "TRUE"));
            const clickFalse = () => (isStaged ? onStage(m.id, "FALSE") : onLabel(m.id, lab === "FALSE" ? null : "FALSE"));
            // Another candidate of this entity is already the match -> this one is resolved.
            const resolvedByOther = !effTrue && !!entitiesWithTrue && entitiesWithTrue.has(gkey);
            return (
              <Fragment key={m.id}>
              {showHead && ginfo && (
                <tr>
                  <td colSpan={8} style={{ background: "var(--ti-red-50)", borderTop: "2px solid var(--line)", padding: "7px 10px" }}>
                    <span style={{ fontWeight: 600 }}>{ginfo.ocod}</span>{" "}
                    <span className="mono muted" style={{ fontSize: 11 }}>{ginfo.jur}</span>{" "}
                    <span className="tag violet"><span className="dot" />{ginfo.count} candidate{ginfo.count !== 1 ? "s" : ""}</span>
                    {ginfo.margin != null && (
                      <span style={{ fontSize: 11, marginLeft: 6, color: ginfo.margin < 0.05 ? "var(--ti-red)" : "var(--muted)" }}>
                        margin {ginfo.margin.toFixed(2)}{ginfo.margin < 0.05 ? " · close" : ""}
                      </span>
                    )}
                  </td>
                </tr>
              )}
              <tr
                className={sel ? "selected" : ""}
                style={resolvedByOther ? { opacity: 0.5 } : undefined}
                onClick={() => setSelectedId(m.id)}
              >
                <td>
                  <span
                    className={
                      "dot " +
                      (m.band === "auto-accept"
                        ? "green"
                        : m.band === "review"
                          ? "amber"
                          : m.band === "ambiguous"
                            ? "blue"
                            : "red")
                    }
                  />
                </td>
                <td>
                  <div
                    title={m.ocod_name_raw}
                    style={{
                      fontWeight: 500,
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      cursor: "default",
                    }}
                  >
                    {m.ocod_name_raw}
                  </div>
                  {m.ocod_id && (
                    <div className="mono muted" style={{ fontSize: 11 }}>
                      {m.ocod_id}
                    </div>
                  )}
                </td>
                <td>
                  <div
                    title={m.roe_name_raw || ""}
                    style={{
                      fontWeight: 500,
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      cursor: "default",
                    }}
                  >
                    {m.roe_name_raw || (
                      <span className="muted">(multiple candidates)</span>
                    )}
                  </div>
                  {m.roe_company_number && (
                    <div className="mono muted" style={{ fontSize: 11 }}>
                      {m.roe_company_number}
                    </div>
                  )}
                  {m.matched_name_type === "former" && (
                    <div style={{ marginTop: 2, fontSize: 11 }}>
                      <span className="tag amber" title={`Matched on this company's former name${m.roe_name_matched_raw ? `: ${m.roe_name_matched_raw}` : ""}`}>
                        <span className="dot" />former name
                      </span>
                      {m.roe_name_matched_raw && (
                        <span className="muted" style={{ marginLeft: 4 }}>
                          via “{m.roe_name_matched_raw}”
                        </span>
                      )}
                    </div>
                  )}
                </td>
                <td>
                  <span className="mono" style={{ fontSize: 12 }}>
                    {m.jurisdiction_clean}
                  </span>
                </td>
                <td>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <ProbBar p={m.match_probability} w={50} high={threshold} review={reviewLow} />
                    <span className="mono" style={{ fontWeight: 600 }}>
                      {fmtProb(m.match_probability)}
                    </span>
                  </div>
                </td>
                <td>
                  <BandTag band={m.band} method={m.match_method} />
                </td>
                <td>
                  <button
                    className="btn sm"
                    aria-label={`Show feature breakdown for ${m.ocod_name_raw || m.id}`}
                    title="Show feature breakdown"
                    onClick={(e) => showPopup(e, m, true)}
                    onFocus={(e) => showPopup(e, m, false)}
                    onBlur={() => setPopup((cur) => (cur?.pinned ? cur : null))}
                    onMouseEnter={(e) => showPopup(e, m, false)}
                    onMouseLeave={() => setPopup((cur) => (cur?.pinned ? cur : null))}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setPopup(null);
                    }}
                  >
                    <Icons.spark size={12} /> breakdown
                  </button>
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                    <button
                      className="btn sm"
                      disabled={resolvedByOther}
                      title={resolvedByOther
                        ? "Another candidate is already the match for this entity (one per entity)"
                        : (isStaged ? "Staged — click to flip" : undefined)}
                      style={{
                        ...(effTrue ? { background: "var(--green)", color: "#fff", borderColor: "var(--green)" } : {}),
                        ...(isStaged ? { borderStyle: "dashed", opacity: effTrue ? 0.85 : 1 } : {}),
                      }}
                      onClick={clickTrue}
                    >
                      <Icons.check size={12} stroke={effTrue ? "#fff" : "currentColor"} />
                      TRUE
                    </button>
                    <button
                      className="btn sm"
                      title={isStaged ? "Staged — click to flip" : undefined}
                      style={{
                        ...(effFalse ? { background: "var(--ti-red)", color: "#fff", borderColor: "var(--ti-red)" } : {}),
                        ...(isStaged ? { borderStyle: "dashed", opacity: effFalse ? 0.85 : 1 } : {}),
                      }}
                      onClick={clickFalse}
                    >
                      <Icons.x size={12} stroke={effFalse ? "#fff" : "currentColor"} />
                      FALSE
                    </button>
                    {isStaged && (
                      <span className="tag" style={{ borderColor: "#4C78A8", color: "#4C78A8" }}>staged</span>
                    )}
                    {resolvedByOther && (
                      <span className="tag" title="Another candidate is the chosen match for this entity — left unlabelled, not auto-FALSE">
                        resolved
                      </span>
                    )}
                    {overrides && !isStaged && (
                      <span className="tag" style={{ color: "var(--ti-red)" }} title="Your label overrides the score band — labels are paramount">
                        overrides
                      </span>
                    )}
                  </div>
                </td>
              </tr>
              </Fragment>
            );
          }); })()}
        </tbody>
      </table>
      {popup && <FeaturePopover popup={popup} />}
    </div>
  );
}

// ============================================================
// ReviewDiff — diff view with queue sidebar
// ============================================================

function ReviewDiff({
  items,
  current,
  labels,
  onLabel,
  staged,
  onStage,
  threshold,
  reviewLow,
  notes,
  setNote,
  setSelectedId,
  runId,
  isGbt,
}) {
  const [inspectOpen, setInspectOpen] = useState(false);

  useEffect(() => {
    setInspectOpen(false);
  }, [current?.id]);

  if (!current) {
    return (
      <Empty
        title="Nothing in this filter"
        sub="Try widening the threshold or clearing the search."
      />
    );
  }

  const lab = labels[current.id];
  const note = notes[current.id] || "";
  const pos = items.findIndex((m) => m.id === current.id);
  const inspectUrl = companiesHouseUrl(current.roe_company_number);

  return (
    <>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "260px 1fr",
          gap: 16,
          alignItems: "flex-start",
        }}
      >
        {/* Queue sidebar */}
        <div
          className="card"
          style={{
            maxHeight: "calc(100vh - 320px)",
            overflow: "auto",
            position: "sticky",
            top: 70,
          }}
        >
          <div className="card-h" style={{ padding: "10px 12px" }}>
            <span className="eyebrow">Queue</span>
            <span className="muted" style={{ fontSize: 11, marginLeft: "auto" }}>
              {pos + 1} / {items.length}
            </span>
          </div>
          <div>
            {items.map((m) => {
              const sel = m.id === current.id;
              const ml = labels[m.id];
              const stagedVal = staged?.[m.id];
              return (
                <div
                  key={m.id}
                  onClick={() => setSelectedId(m.id)}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "16px 1fr auto",
                    gap: 8,
                    padding: "8px 12px",
                    borderBottom: "1px solid var(--line)",
                    background: sel ? "var(--ti-red-50)" : "transparent",
                    cursor: "pointer",
                    alignItems: "center",
                  }}
                >
                  <span
                    className={
                      "dot " +
                      (m.band === "auto-accept"
                        ? "green"
                        : m.band === "review"
                          ? "amber"
                          : "red")
                    }
                  />
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: 12.5,
                        fontWeight: 500,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {m.ocod_name_raw}
                    </div>
                    <div className="mono muted" style={{ fontSize: 11 }}>
                      {fmtProb(m.match_probability)} &middot; {m.jurisdiction_clean}
                    </div>
                  </div>
                  {stagedVal === "TRUE" && <span className="tag green">staged</span>}
                  {stagedVal === "FALSE" && <span className="tag" style={{ color: "var(--ti-red)" }}>staged</span>}
                  {!stagedVal && ml === "TRUE" && <Icons.check size={12} stroke="var(--green)" />}
                  {!stagedVal && ml === "FALSE" && <Icons.x size={12} stroke="var(--ti-red)" />}
                </div>
              );
            })}
          </div>
        </div>

        {/* Main diff area */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <DiffHero match={current} high={threshold} review={reviewLow} />
          <DiffMeta match={current} onInspect={() => setInspectOpen(true)} />
          <AddressPanel address={current.address} />
          <FeatureBreakdown
            features={current.features}
            probability={current.match_probability}
          />
          <GbtExplain runId={runId} pair={current} />{/* self-hides when no model */}
          <DiffControls
            match={current}
            label={lab}
            onLabel={onLabel}
            staged={staged?.[current.id]}
            onStage={onStage}
            note={note}
            setNote={(v) => setNote(current.id, v)}
          />
        </div>
      </div>

      {inspectOpen && current.roe_company_number && (
        <>
          <div className="scrim" onClick={() => setInspectOpen(false)} />
          <aside className="sheet" role="dialog" aria-modal="true" aria-label="Companies House inspection">
            <div className="sheet-h">
              <div>
                <div className="eyebrow">Companies House</div>
                <div style={{ fontSize: 18, fontWeight: 650 }}>
                  {current.roe_company_number}
                </div>
              </div>
              <button
                className="btn"
                style={{ marginLeft: "auto" }}
                onClick={() => setInspectOpen(false)}
                aria-label="Close inspection"
              >
                <Icons.x size={14} />
              </button>
            </div>
            <div className="sheet-b">
              <div style={{ fontWeight: 650, fontSize: 18, marginBottom: 8 }}>
                {current.roe_name_raw}
              </div>
              <dl className="diff-meta">
                <dt>company number</dt>
                <dd className="mono">{current.roe_company_number}</dd>
                <dt>jurisdiction</dt>
                <dd className="mono">{current.jurisdiction_clean}</dd>
                <dt>probability</dt>
                <dd className="mono">{fmtProb(current.match_probability)}</dd>
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
              <button className="btn" onClick={() => setInspectOpen(false)}>
                Close
              </button>
            </div>
          </aside>
        </>
      )}
    </>
  );
}

// ============================================================
// DiffMeta — side-by-side OCOD and ROE record cards
// ============================================================

function DiffMeta({ match, onInspect }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <div className="card">
        <div className="card-h" style={{ padding: "10px 14px" }}>
          <span className="eyebrow">OCOD record</span>
        </div>
        <div className="card-b" style={{ padding: 14 }}>
          <dl className="diff-meta">
            <dt>raw name</dt>
            <dd className="mono">{match.ocod_name_raw}</dd>
            <dt>clean name</dt>
            <dd className="mono">{match.ocod_name_clean}</dd>
            <dt>jurisdiction</dt>
            <dd className="mono">{match.jurisdiction_clean}</dd>
            {match.ocod_id && (
              <>
                <dt>OCOD id</dt>
                <dd className="mono">{match.ocod_id}</dd>
              </>
            )}
          </dl>
        </div>
      </div>
      <div className="card">
        <div className="card-h" style={{ padding: "10px 14px" }}>
          <span className="eyebrow">ROE record</span>
          {match.roe_company_number && (
            <button
              className="btn sm"
              style={{ marginLeft: "auto" }}
              onClick={onInspect}
              aria-label={`Inspect ${match.roe_company_number} at Companies House`}
              title="Inspect at Companies House"
            >
              <Icons.link size={13} />
              Inspect CH
            </button>
          )}
        </div>
        <div className="card-b" style={{ padding: 14 }}>
          {match.roe_name_raw ? (
            <dl className="diff-meta">
              <dt>raw name</dt>
              <dd className="mono">{match.roe_name_raw}</dd>
              <dt>clean name</dt>
              <dd className="mono">{match.roe_name_clean || match.roe_name_raw}</dd>
              <dt>company number</dt>
              <dd className="mono">{match.roe_company_number}</dd>
              <dt>jurisdiction</dt>
              <dd className="mono">{match.jurisdiction_clean}</dd>
              {match.matched_name_type === "former" && (
                <>
                  <dt>matched on</dt>
                  <dd className="mono">
                    {match.roe_name_matched_raw || "(former name)"}{" "}
                    <span className="tag amber"><span className="dot" />former name</span>
                  </dd>
                </>
              )}
            </dl>
          ) : (
            <div className="muted" style={{ fontSize: 12.5 }}>
              Multiple ROE candidates &mdash; open Ambiguous to disambiguate.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================
// DiffControls — notes + TRUE/FALSE/Clear buttons with keys
// ============================================================

function DiffControls({ match, label, onLabel, staged, onStage, note, setNote }) {
  const effectiveLabel = staged || label;
  const handleTrue = () => {
    if (staged) onStage(match.id, "TRUE");
    else onLabel(match.id, label === "TRUE" ? null : "TRUE");
  };
  const handleFalse = () => {
    if (staged) onStage(match.id, "FALSE");
    else onLabel(match.id, label === "FALSE" ? null : "FALSE");
  };
  const handleClear = () => {
    if (staged) onStage(match.id, staged === "TRUE" ? "FALSE" : "TRUE");
    else onLabel(match.id, null);
  };
  return (
    <div className="card" style={{ position: "sticky", bottom: 16 }}>
      <div
        className="card-b"
        style={{
          display: "grid",
          gridTemplateColumns: "1fr auto",
          gap: 16,
          alignItems: "flex-start",
        }}
      >
        <div className="field">
          <label>Notes (visible in audit log)</label>
          <textarea
            className="textarea"
            placeholder='e.g. "Same UBO, parent vs subsidiary — checked CH filing history"'
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </div>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            alignItems: "stretch",
            minWidth: 220,
          }}
        >
          {staged && (
            <div className="tag" style={{ justifyContent: "center", borderStyle: "dashed" }}>
              Staged {staged}; commit or cancel in the banner above
            </div>
          )}
          <button
            className="btn lg"
            onClick={handleTrue}
            style={
              effectiveLabel === "TRUE"
                ? {
                    background: "var(--green)",
                    color: "#fff",
                    borderColor: "var(--green)",
                  }
                : {}
            }
          >
            <Icons.check
              size={14}
              stroke={effectiveLabel === "TRUE" ? "#fff" : "currentColor"}
            />{" "}
            {staged ? "Stage TRUE" : "Mark TRUE"}
            <span
              className="kh"
              style={{
                marginLeft: "auto",
                color:
                  effectiveLabel === "TRUE" ? "rgba(255,255,255,.75)" : undefined,
              }}
            >
              <span
                className="kbd"
                style={
                  effectiveLabel === "TRUE"
                    ? {
                        background: "rgba(255,255,255,.18)",
                        color: "#fff",
                        borderColor: "transparent",
                      }
                    : {}
                }
              >
                T
              </span>
            </span>
          </button>
          <button
            className="btn lg"
            onClick={handleFalse}
            style={
              effectiveLabel === "FALSE"
                ? {
                    background: "var(--ti-red)",
                    color: "#fff",
                    borderColor: "var(--ti-red)",
                  }
                : {}
            }
          >
            <Icons.x
              size={14}
              stroke={effectiveLabel === "FALSE" ? "#fff" : "currentColor"}
            />{" "}
            {staged ? "Stage FALSE" : "Mark FALSE"}
            <span
              className="kh"
              style={{
                marginLeft: "auto",
                color:
                  effectiveLabel === "FALSE" ? "rgba(255,255,255,.75)" : undefined,
              }}
            >
              <span
                className="kbd"
                style={
                  effectiveLabel === "FALSE"
                    ? {
                        background: "rgba(255,255,255,.18)",
                        color: "#fff",
                        borderColor: "transparent",
                      }
                    : {}
                }
              >
                F
              </span>
            </span>
          </button>
          <button className="btn ghost" onClick={handleClear}>
            <Icons.refresh size={13} />
            {staged ? "Flip staged verdict" : "Clear label"}{" "}
            <span className="kh" style={{ marginLeft: "auto" }}>
              <span className="kbd">U</span>
            </span>
          </button>
          <div
            className="muted"
            style={{ fontSize: 11, textAlign: "center", marginTop: 4 }}
          >
            <span className="kbd">J</span> / <span className="kbd">K</span> to
            navigate
          </div>
        </div>
      </div>
    </div>
  );
}

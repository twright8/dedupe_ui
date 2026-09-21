/* ============================================================
   Screen: Review queue — table + diff view, threshold control,
   keyboard nav, live re-bucketing, per-comparison explanation
   ------------------------------------------------------------
   The shape of the work: brush a band on the histogram, mark the
   band Match or Not a match, fix the exceptions in the list, then
   save. Both sides of a pair are units of the same dataset,
   filtering and paging happen on the server, and the evidence a
   reviewer judges on (DESIGN.md D13a) sits in the diff view under
   the explanation.

   The two answers are Match and Not a match. The API still takes
   TRUE and FALSE, so those values live in the request bodies and
   in the state keys, and ANSWER turns them into words at the last
   moment. Neither value is ever shown.
   ============================================================ */

import { useState, useEffect, useMemo, useCallback, useRef, Fragment } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Icons } from "../components/Icons";
import { ProbBar, fmtProb, fmtNumber } from "../components/ProbBar";
import { Empty } from "../components/Empty";
import {
  DiffHero,
  BucketTag,
  BUCKET_LABELS,
  VetoTag,
  VetoBanner,
  entityIds,
} from "../components/DiffHero";
import { Term, TermHint, Provenance, provenanceLabel } from "../components/Term";
import { PairExplain } from "../components/PairExplain";
import { PairEvidence } from "../components/PairEvidence";
import { ModelExplain } from "../components/ModelExplain";
import { FocusStrip } from "../components/FocusStrip";
import { ThresholdPanel } from "../components/ThresholdPanel";
import { Cell, NUMERIC_TYPES, SYSTEM_COLUMNS, PATTERN_COLUMNS } from "../components/cells";
import MethodologyNotes from "../components/MethodologyNotes";
import { useKeyboardNav } from "../hooks/useKeyboardNav";
import { useProfile } from "../profile";
import { ANSWER_LABEL as ANSWER, SCORER, valueLabel } from "../glossary";
import { existingLabelName, hasExistingLabels, noun } from "../profileText";

const PER_PAGE = 50;
const BULK_LIMIT = 500; // the API's own cap, and the batch size for saving

// The bucket tabs, in the order a reviewer works through them.
const BUCKETS = [
  { id: "review", lab: BUCKET_LABELS.review, count: "review" },
  { id: "accept", lab: BUCKET_LABELS.accept, count: "accept" },
  { id: "reject", lab: BUCKET_LABELS.reject, count: "reject" },
  { id: "all", lab: "All", count: "all" },
];

/* The "How it was decided" filter. Every name comes from the one provenance
   vocabulary, so a value reads the same here as on the chip beside the pair.
   The two scorers are named apart because both are on screen at once. A pair
   a veto rule decided is covered by the wider "stopped by a veto rule"
   button below, which also catches a vetoed pair the earlier grouping
   accepted anyway, so it is not repeated here. `optional` marks a filter the
   run may carry no count for. */
const DECIDED_BY = [
  { id: "score", lab: "Splink score", count: "score", help: "The Splink score alone put this pair where it is." },
  {
    id: "model",
    lab: "Model score",
    count: "model",
    optional: true,
    help: "The model score alone put this pair where it is.",
  },
  {
    id: "import",
    lab: provenanceLabel("decided_by", "import"),
    count: "import",
    help: "Accepted because both sides already carry the same earlier ID.",
  },
  {
    id: "human",
    lab: provenanceLabel("decided_by", "human"),
    count: "human",
    help: "A person decided this pair.",
  },
];

function isHttpUrl(value) {
  const text = String(value || "").trim();
  if (!text) return true; // empty is fine
  return /^https?:\/\/\S+$/i.test(text);
}

// ---------- main screen ----------
export default function ReviewScreen() {
  const { id: runId } = useParams();
  const navigate = useNavigate();
  const profile = useProfile();
  const [searchParams] = useSearchParams();

  const [mode, setMode] = useState("table");

  // Thresholds, seeded from the run and applied by the re-bucket call.
  const [threshold, setThreshold] = useState(0.92);
  const [reviewLow, setReviewLow] = useState(0.5);
  const [committed, setCommitted] = useState(null);
  const [runInfo, setRunInfo] = useState(null);
  const [committing, setCommitting] = useState(false);

  // Filters — every one of these is a query parameter.
  // A link that asks for the vetoed pairs wants all of them, not only the ones
  // already sitting in the review bucket.
  const [bucket, setBucket] = useState(() =>
    searchParams.get("vetoed") === "yes" ? "all" : "review"
  );
  const [track, setTrack] = useState("all");
  const [decidedBy, setDecidedBy] = useState("all");
  const [importFilter, setImportFilter] = useState("all");
  const [labelled, setLabelled] = useState("all");
  // Every pair a veto rule hit. Wider than decided_by=veto: a vetoed pair the
  // earlier grouping accepted reads "import" and is still vetoed.
  const [vetoed, setVetoed] = useState(() =>
    searchParams.get("vetoed") === "yes" ? "yes" : "all"
  );
  const [held, setHeld] = useState("hide");
  const [query, setQuery] = useState(() => searchParams.get("search") || "");
  const [q, setQ] = useState(() => searchParams.get("search") || "");
  const [sort, setSort] = useState("score");
  const [order, setOrder] = useState("desc");
  const [page, setPage] = useState(0);
  const [brushLo, setBrushLo] = useState(null);
  const [brushHi, setBrushHi] = useState(null);
  const [brushOnModel, setBrushOnModel] = useState(false);

  const [data, setData] = useState(null);
  const [histogram, setHistogram] = useState(null);
  const [scoreEval, setScoreEval] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);

  const [selectedId, setSelectedId] = useState(null);

  // Labels the user has set this session, over whatever the API returned.
  const [labels, setLabels] = useState({}); // pair_id -> {is_match, notes, evidence_url} | null
  const [saving, setSaving] = useState(new Set());
  const [liveCounts, setLiveCounts] = useState(null);

  // Bulk staging: nothing saves until the banner's Save is pressed.
  const [staged, setStaged] = useState({});
  const [stageOnlyUnlabelled, setStageOnlyUnlabelled] = useState(true);
  const [stagingBusy, setStagingBusy] = useState(false);
  const [committingBulk, setCommittingBulk] = useState(false);

  const tracks = profile.tracks || [];
  const eventColumns = profile.event_columns || [];
  // A profile with nothing to compare against hides every chip, column and
  // figure about an earlier grouping.
  const earlier = existingLabelName(profile);

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(query.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  // The run's own thresholds seed the sliders.
  useEffect(() => {
    if (!runId) return;
    api
      .getRun(runId)
      .then((run) => {
        setRunInfo(run);
        const high = run?.threshold_high;
        const low = run?.threshold_review;
        if (high != null) setThreshold(+high);
        if (low != null) setReviewLow(+low);
        setCommitted({ high: +(high ?? 0.92), review: +(low ?? 0.5) });
      })
      .catch(() => {});
  }, [runId]);

  // Histogram and score-eval follow the track filter, not the rest.
  useEffect(() => {
    if (!runId) return;
    const params = track === "all" ? undefined : { track };
    api.getRunPairsHistogram(runId, params).then(setHistogram).catch(() => setHistogram(null));
    api.getRunScoreEval(runId).then(setScoreEval).catch(() => setScoreEval(null));
  }, [runId, track, attempt]);

  const listParams = useMemo(() => {
    const p = { offset: page * PER_PAGE, limit: PER_PAGE, sort, order };
    if (bucket !== "all") p.bucket = bucket;
    if (track !== "all") p.track = track;
    if (decidedBy !== "all") p.decided_by = decidedBy;
    if (importFilter !== "all") p.import = importFilter;
    if (labelled !== "all") p.labelled = labelled;
    if (vetoed !== "all") p.vetoed = vetoed;
    if (held !== "both") p.held = held;
    if (q) p.q = q;
    if (brushLo != null) p[brushOnModel ? "min_gbt" : "min_score"] = brushLo;
    if (brushHi != null) p[brushOnModel ? "max_gbt" : "max_score"] = brushHi;
    return p;
  }, [page, sort, order, bucket, track, decidedBy, importFilter, labelled, vetoed, held, q, brushLo, brushHi, brushOnModel]);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    setLoading(true);
    api
      .getRunPairs(runId, listParams)
      .then((res) => {
        if (!alive) return;
        setData(res);
        setError(null);
        if (res?.items?.length && !res.items.some((it) => it.pair_id === selectedId)) {
          setSelectedId(res.items[0].pair_id);
        }
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
    // selectedId is deliberately out: picking a row must not refetch the page.
  }, [runId, listParams, attempt]); // eslint-disable-line react-hooks/exhaustive-deps

  /* The read-only lines belong to the run, not to whatever version happens to be
     active now, and each track has its own. The histogram answers for the track
     it was asked about; the run's counts answer for any track. */
  const modelTrack = track === "all" ? (tracks[0]?.key ?? null) : track;
  const modelForTrack = useMemo(() => {
    const src = histogram?.modelActive != null ? histogram : runInfo?.counts || {};
    const per = (value) =>
      value && typeof value === "object" && !Array.isArray(value)
        ? modelTrack
          ? value[modelTrack]
          : Object.values(value)[0]
        : value;
    return {
      active: !!src.modelActive,
      graded: !!per(src.modelGraded),
      version: per(src.modelVersion),
      warning: per(src.modelWarning),
      accept: per(src.modelAcceptLine),
      reject: per(src.modelRejectLine),
    };
  }, [histogram, runInfo, modelTrack]);

  const items = data?.items || [];
  const counts = liveCounts || data?.counts || {};
  const columns = data?.columns || [];
  const priorityKey = (data?.priority_columns || profile.priority_columns || [])[0];
  const priorityColumn =
    (columns.find((c) => c.key === priorityKey)) ||
    (profile.display_columns || []).find((c) => c.key === priorityKey);
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));

  // What the row shows: the session's label if there is one, else the API's.
  const labelOf = useCallback(
    (item) => (item.pair_id in labels ? labels[item.pair_id] : item.label || null),
    [labels]
  );

  const current = useMemo(
    () => items.find((m) => m.pair_id === selectedId) || items[0] || null,
    [items, selectedId]
  );

  // ---------- labelling ----------
  const applyLabel = useCallback(
    (pairId, verdict, extra) => {
      const before = labels[pairId];
      const optimistic =
        verdict == null ? null : { is_match: verdict, reviewer: "you", ...(extra || {}) };
      setLabels((prev) => ({ ...prev, [pairId]: optimistic }));
      setSaving((prev) => new Set(prev).add(pairId));

      const done = () =>
        setSaving((prev) => {
          const next = new Set(prev);
          next.delete(pairId);
          return next;
        });

      const request =
        verdict == null
          ? api.deleteRunLabel(runId, pairId)
          : api.saveRunLabels(runId, {
              labels: [{ pair_id: pairId, is_match: verdict, ...(extra || {}) }],
              provenance: "manual",
            });

      request
        .then((res) => {
          if (res?.counts) setLiveCounts(res.counts);
        })
        .catch((err) => {
          // Put the row back the way it was rather than lie about what is saved.
          setLabels((prev) => ({ ...prev, [pairId]: before ?? undefined }));
          alert("Could not save your answer. " + err.message);
        })
        .finally(done);
    },
    [labels, runId]
  );

  const handleLabel = useCallback(
    (pairId, verdict) => {
      const item = items.find((m) => m.pair_id === pairId);
      const existing = item ? labelOf(item) : null;
      // Pressing the same verdict again clears it, as the old screen does.
      const next = existing && existing.is_match === verdict ? null : verdict;
      applyLabel(pairId, next, existing ? { notes: existing.notes, evidence_url: existing.evidence_url } : null);
    },
    [items, labelOf, applyLabel]
  );

  // ---------- thresholds ----------
  const handleCommitThresholds = useCallback(() => {
    setCommitting(true);
    api
      .reBucketRun(runId, { threshold_high: threshold, threshold_review: reviewLow })
      .then((res) => {
        setCommitted({ high: threshold, review: reviewLow });
        if (res?.counts) setLiveCounts(res.counts);
        setAttempt((n) => n + 1);
      })
      .catch((err) => alert(err.message))
      .finally(() => setCommitting(false));
  }, [runId, threshold, reviewLow]);

  // ---------- brush ----------
  const onBrush = useCallback((lo, hi, onModelScore) => {
    setBrushLo(lo);
    setBrushHi(hi);
    setBrushOnModel(!!onModelScore);
    setPage(0);
    // Surface the most-likely-wrong first.
    setSort("score");
    setOrder("asc");
  }, []);
  const clearBrush = useCallback(() => {
    setBrushLo(null);
    setBrushHi(null);
    setPage(0);
    setOrder("desc");
  }, []);

  // ---------- bulk on the slice ----------
  const sliceParts = useMemo(() => {
    const parts = [`bucket: ${BUCKETS.find((b) => b.id === bucket)?.lab || bucket}`];
    if (brushLo != null)
      parts.push(
        `${brushOnModel ? "model" : "Splink"} score: ${brushLo.toFixed(2)}–${brushHi.toFixed(2)}`
      );
    if (track !== "all") parts.push(`track: ${tracks.find((t) => t.key === track)?.label || track}`);
    if (decidedBy !== "all")
      parts.push(
        `how it was decided: ${DECIDED_BY.find((d) => d.id === decidedBy)?.lab || decidedBy}`
      );
    if (importFilter === "disagrees") parts.push("the two earlier IDs differ");
    if (vetoed === "yes") parts.push("stopped by a veto rule");
    if (held !== "both") parts.push(held === "hide" ? "held groups hidden" : "held groups only");
    if (q) parts.push(`search: "${q}"`);
    return parts;
  }, [bucket, brushLo, brushHi, brushOnModel, track, decidedBy, importFilter, vetoed, held, q, tracks]);

  const stageSlice = useCallback(
    (verdict) => {
      setStagingBusy(true);
      // Stage the whole slice, not just the page on screen — up to the API's cap.
      api
        .getRunPairs(runId, { ...listParams, offset: 0, limit: BULK_LIMIT })
        .then((res) => {
          const next = {};
          for (const it of res.items || []) {
            if (stageOnlyUnlabelled && (it.pair_id in labels ? labels[it.pair_id] : it.label)) continue;
            next[it.pair_id] = verdict;
          }
          setStaged((prev) => ({ ...prev, ...next }));
        })
        .catch((err) => alert(err.message))
        .finally(() => setStagingBusy(false));
    },
    [runId, listParams, stageOnlyUnlabelled, labels]
  );

  const stagedCount = Object.keys(staged).length;
  const toggleStaged = useCallback((pairId, verdict) => {
    setStaged((prev) => ({ ...prev, [pairId]: verdict }));
  }, []);
  const cancelStaging = useCallback(() => setStaged({}), []);

  const commitStaged = useCallback(async () => {
    const entries = Object.entries(staged);
    if (!entries.length) return;
    setCommittingBulk(true);
    try {
      for (let i = 0; i < entries.length; i += BULK_LIMIT) {
        const slice = entries.slice(i, i + BULK_LIMIT);
        const res = await api.saveRunLabels(runId, {
          labels: slice.map(([pair_id, is_match]) => ({
            pair_id,
            is_match,
            notes: "Marked in bulk by score band",
          })),
          provenance: "bulk_range",
        });
        if (res?.counts) setLiveCounts(res.counts);
      }
      setLabels((prev) => {
        const next = { ...prev };
        for (const [pairId, verdict] of entries) {
          next[pairId] = { is_match: verdict, reviewer: "you", provenance: "bulk_range" };
        }
        return next;
      });
      setStaged({});
    } catch (err) {
      alert(err.message);
    } finally {
      setCommittingBulk(false);
    }
  }, [staged, runId]);

  useKeyboardNav({
    items,
    selectedId: current?.pair_id,
    setSelectedId,
    onLabel: handleLabel,
    enabled: mode === "diff",
  });

  function resetPage(setter, value) {
    setter(value);
    setPage(0);
  }

  if (error && !data) {
    return (
      <div className="content">
        <div className="page-head">
          <div>
            <h1 className="page-title">Review queue</h1>
          </div>
        </div>
        <Empty
          title="No pairs for this run"
          sub={error}
          action={
            <button className="btn primary" onClick={() => navigate(`/runs/${runId}`)}>
              Back to the run
            </button>
          }
        />
      </div>
    );
  }

  const pendingReview = counts.pairsReview ?? counts.review ?? 0;
  const showEarlier = hasExistingLabels(profile, counts);

  return (
    <div className="content" style={{ maxWidth: "none", paddingRight: 28 }}>
      <div className="page-head">
        <div>
          {runId && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              <span className="mono">{runId}</span>
            </div>
          )}
          <h1 className="page-title">Review queue</h1>
          <p className="page-sub">
            {fmtNumber(pendingReview)} <Term name="pair" plural /> the <Term name="scorer" /> was
            not sure about. <strong>Drag a band on the chart</strong> to grab a set of similar{" "}
            <Term name="score" plural />, <strong>mark them all</strong> Match or Not a match, fix
            the exceptions in the list, then save. A score of 0 means probably not the same thing,
            1 means probably the same thing.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div className="seg">
            <button className={mode === "table" ? "on" : ""} onClick={() => setMode("table")}>
              <Icons.table size={13} />
              Table
            </button>
            <button className={mode === "diff" ? "on" : ""} onClick={() => setMode("diff")}>
              <Icons.diff size={13} />
              Diff
            </button>
          </div>
          <a className="btn" href={api.labelsExportUrl({ run_id: runId })} download>
            <Icons.download size={14} />
            Export labels
          </a>
        </div>
      </div>

      <MethodologyNotes />

      <ThresholdPanel
        threshold={threshold}
        setThreshold={setThreshold}
        reviewLow={reviewLow}
        setReviewLow={setReviewLow}
        histogram={histogram}
        counts={counts}
        committed={committed}
        onCommit={handleCommitThresholds}
        committing={committing}
        brushLo={brushLo}
        brushHi={brushHi}
        onBrush={onBrush}
        scoreEval={scoreEval}
        model={modelForTrack}
        earlier={earlier}
        showEarlier={showEarlier}
      />

      {/* PRIMARY · label these pairs */}
      <div className="card" style={{ margin: "12px 0", borderColor: "var(--line-strong)" }}>
        <div className="card-h" style={{ paddingBottom: 6 }}>
          <Icons.check size={15} />
          <h3 style={{ margin: 0 }}>Label these pairs</h3>
          <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
            your answers — saved for good, and what the model learns from
          </span>
        </div>
        <div className="card-b" style={{ paddingTop: 10 }}>
          <div style={{ fontSize: 13, marginBottom: 10 }}>
            {brushLo != null ? (
              <>
                Band{" "}
                <span className="mono">
                  {brushLo.toFixed(2)}–{brushHi.toFixed(2)}
                </span>{" "}
                of the {brushOnModel ? "model" : "Splink"} score selected on the chart —{" "}
              </>
            ) : (
              <>Whole current view (drag a band on the chart to narrow) — </>
            )}
            <strong>{fmtNumber(Math.min(total, BULK_LIMIT))}</strong> pairs will be marked
            {total > BULK_LIMIT && (
              <span className="muted"> (the first {BULK_LIMIT} of {fmtNumber(total)}; repeat to go further)</span>
            )}
            .{" "}
            {brushLo != null && (
              <button className="btn sm" onClick={clearBrush}>
                clear band
              </button>
            )}
            <div className="muted mono" style={{ fontSize: 11, marginTop: 4 }}>
              {sliceParts.join(" · ")}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <button
              className="btn lg"
              style={{ borderColor: "var(--green)", color: "var(--green)", fontWeight: 600 }}
              onClick={() => stageSlice("TRUE")}
              disabled={total === 0 || stagingBusy}
            >
              <Icons.check size={14} /> Mark all as {ANSWER.TRUE}
            </button>
            <button
              className="btn lg"
              style={{ borderColor: "var(--ti-red)", color: "var(--ti-red)", fontWeight: 600 }}
              onClick={() => stageSlice("FALSE")}
              disabled={total === 0 || stagingBusy}
            >
              Mark all as {ANSWER.FALSE}
            </button>
            <label
              className="muted"
              style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5, marginLeft: 6 }}
            >
              <input
                type="checkbox"
                checked={stageOnlyUnlabelled}
                onChange={(e) => setStageOnlyUnlabelled(e.target.checked)}
              />
              skip ones I've already answered
            </label>
            {stagingBusy && <span className="muted pulse">collecting the band…</span>}
          </div>
          <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
            Nothing saves yet — you'll review and flip the wrong ones first.{" "}
            {brushHi != null && brushHi <= 0.7 ? (
              <>
                <strong>Low-scoring pairs are usually not a match</strong> — the model learns most
                from the pairs you rule out.
              </>
            ) : (
              <>The model learns most from the uncertain middle, not the confident top.</>
            )}
          </div>
        </div>
      </div>

      {stagedCount > 0 && (
        <div
          className="card"
          style={{
            marginBottom: 14,
            borderColor: "var(--ti-red)",
            borderWidth: 2,
            background: "var(--ti-red-50)",
          }}
        >
          <div
            className="card-b"
            style={{ display: "flex", alignItems: "center", gap: 14, padding: "12px 16px", flexWrap: "wrap" }}
          >
            <span style={{ fontSize: 13.5 }}>
              <strong>
                {fmtNumber(stagedCount)} answer{stagedCount === 1 ? "" : "s"} staged
              </strong>
              {" ("}
              <span style={{ color: "var(--green)", fontWeight: 600 }}>
                {Object.values(staged).filter((v) => v === "TRUE").length} {ANSWER.TRUE}
              </span>
              {" / "}
              <span style={{ color: "var(--ti-red)", fontWeight: 600 }}>
                {Object.values(staged).filter((v) => v === "FALSE").length} {ANSWER.FALSE}
              </span>
              {") — "}
              <strong>not saved yet.</strong> Flip any wrong ones in the list below, then save.
            </span>
            <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
              <span className="muted" style={{ fontSize: 11 }}>
                saved for good · joins the training set or the test set
              </span>
              <button className="btn primary" onClick={commitStaged} disabled={committingBulk}>
                {committingBulk ? "Saving…" : `Save ${fmtNumber(stagedCount)} answers`}
              </button>
              <button className="btn" onClick={cancelStaging}>
                Discard
              </button>
            </span>
          </div>
        </div>
      )}

      {/* Toolbar */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6, flexWrap: "wrap" }}>
        <span className="muted" style={{ fontSize: 12 }}>
          Bucket
          <TermHint name="bucket" />
        </span>
        <div className="seg">
          {BUCKETS.map((b) => (
            <button
              key={b.id}
              className={bucket === b.id ? "on" : ""}
              onClick={() => {
                resetPage(setBucket, b.id);
                setSelectedId(null);
              }}
            >
              {b.lab}{" "}
              <span className="muted">&middot; {fmtNumber(counts[b.count])}</span>
            </button>
          ))}
        </div>
        <div className="search" style={{ width: 260 }}>
          <Icons.search size={14} />
          <input
            className="input"
            placeholder="Search either side's name..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <select
          className="select"
          style={{ width: 210 }}
          value={`${sort}:${order}`}
          onChange={(e) => {
            const [s, o] = e.target.value.split(":");
            setSort(s);
            setOrder(o);
            setPage(0);
          }}
          title="Sorting changes the order of review and nothing else"
        >
          <option value="score:desc">Score, highest first</option>
          <option value="score:asc">Score, lowest first</option>
          {priorityColumn && (
            <option value="priority:desc">{priorityColumn.label}, highest first</option>
          )}
          <option value="name:asc">Name, A to Z</option>
          <option value="useful:desc">Most useful to label</option>
        </select>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 12 }}>
          {fmtNumber(total)} pair{total === 1 ? "" : "s"} in this view
        </span>
      </div>

      {sort === "useful" && (
        <p className="muted" style={{ fontSize: 11.5, margin: "0 0 10px" }}>
          First the pairs the model is least sure about, then the pairs where the model and Splink
          disagree most. A pair worth more money is nudged up the order, never far enough to bury an
          uncertain one.
        </p>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
        {tracks.length > 1 && (
          <div className="seg" title="Filter by track">
            <button className={track === "all" ? "on" : ""} onClick={() => resetPage(setTrack, "all")}>
              All tracks
            </button>
            {tracks.map((t) => (
              <button
                key={t.key}
                className={track === t.key ? "on" : ""}
                onClick={() => resetPage(setTrack, t.key)}
              >
                {t.label}
                <span className="muted" style={{ fontSize: 11 }}>
                  &middot; {fmtNumber(counts[t.key])}
                </span>
              </button>
            ))}
          </div>
        )}

        <span className="muted" style={{ fontSize: 12 }}>
          How it was decided
        </span>
        <div className="seg" title="What put a pair in its bucket">
          <button
            className={decidedBy === "all" && vetoed === "all" ? "on" : ""}
            onClick={() => {
              resetPage(setDecidedBy, "all");
              setVetoed("all");
            }}
          >
            Any decision
          </button>
          {DECIDED_BY.filter(
            (d) =>
              (d.id !== "import" || showEarlier) && (!d.optional || counts[d.count] != null)
          ).map((d) => (
            <button
              key={d.id}
              className={decidedBy === d.id && vetoed === "all" ? "on" : ""}
              onClick={() => {
                resetPage(setDecidedBy, d.id);
                setVetoed("all");
              }}
              title={d.help}
            >
              {d.lab}
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {fmtNumber(counts[d.count])}
              </span>
            </button>
          ))}
          {/* Every pair a veto rule hit, not only the ones a veto rule decided.
              A pair the earlier grouping accepted anyway is still stopped. */}
          {counts.vetoed > 0 && (
            <button
              className={vetoed === "yes" ? "on" : ""}
              onClick={() => {
                resetPage(setDecidedBy, "all");
                setVetoed(vetoed === "yes" ? "all" : "yes");
              }}
              title="A veto rule hit this pair, so the run will not accept it on the score alone."
            >
              Stopped by a veto rule
              <span className="muted" style={{ fontSize: 11 }}>
                &middot; {fmtNumber(counts.vetoed)}
              </span>
            </button>
          )}
        </div>
        {counts.vetoed > 0 && <TermHint name="veto" />}

        {showEarlier && (
        <button
          className="btn sm"
          style={
            importFilter === "disagrees"
              ? { background: "var(--amber)", color: "#fff", borderColor: "var(--amber)" }
              : {}
          }
          onClick={() => resetPage(setImportFilter, importFilter === "disagrees" ? "all" : "disagrees")}
          title="Both sides carry an earlier ID and the two differ. A flag, never a decision."
        >
          Earlier IDs differ
          <span className="muted" style={{ marginLeft: 4 }}>
            &middot; {fmtNumber(counts.import_disagrees)}
          </span>
        </button>
        )}

        <div className="seg" title="Filter by whether you have answered">
          {[
            ["all", "All"],
            ["no", "Unlabelled"],
            ["yes", "Labelled"],
          ].map(([id, lab]) => (
            <button key={id} className={labelled === id ? "on" : ""} onClick={() => resetPage(setLabelled, id)}>
              {lab}
            </button>
          ))}
        </div>

        <span className="muted" style={{ fontSize: 12 }}>
          Held groups
          <TermHint name="heldGroup" />
        </span>
        <div
          className="seg"
          title="A held group is a set of records a match key would have put together, stopped by a guard. These are the pairs whose two sides sit inside one of them."
        >
          {[
            ["hide", "Hide them"],
            ["only", "Only them"],
            ["both", "Show both"],
          ].map(([id, lab]) => (
            <button key={id} className={held === id ? "on" : ""} onClick={() => resetPage(setHeld, id)}>
              {lab}
            </button>
          ))}
        </div>
      </div>

      {vetoed === "yes" && counts.veto_conflicts_import > 0 && showEarlier && (
        <p style={{ fontSize: 11.5, margin: "0 0 12px", color: "var(--amber)" }}>
          {fmtNumber(counts.veto_conflicts_import)} of these were put together by the earlier
          grouping anyway. A veto rule and the earlier grouping disagree about them.
        </p>
      )}

      {held === "hide" && (
        <p className="muted" style={{ fontSize: 11.5, margin: "0 0 12px" }}>
          A <Term name="heldGroup" /> is decided as one group on the cluster screen, not pair by
          pair here, so its pairs are hidden.
        </p>
      )}

      {loading && !data ? (
        <p className="muted pulse" style={{ padding: 40 }}>
          Loading pairs...
        </p>
      ) : items.length === 0 ? (
        <Empty
          title="Nothing in this view"
          sub="Clear the band, widen the bucket, or clear the search."
        />
      ) : mode === "table" ? (
        <>
          <ReviewTable
            items={items}
            columns={columns}
            priorityColumn={priorityColumn}
            tracks={tracks}
            labelOf={labelOf}
            onLabel={handleLabel}
            onLabelWithExtra={applyLabel}
            saving={saving}
            staged={staged}
            onStage={toggleStaged}
            threshold={threshold}
            reviewLow={reviewLow}
            selectedId={current?.pair_id}
            setSelectedId={setSelectedId}
          />
          <Pager page={page} totalPages={totalPages} total={total} setPage={setPage} shown={items.length} />
        </>
      ) : (
        <ReviewDiff
          runId={runId}
          items={items}
          current={current}
          columns={columns}
          displayColumns={profile.display_columns || []}
          eventColumns={eventColumns}
          profile={profile}
          labelOf={labelOf}
          onLabel={handleLabel}
          onLabelWithExtra={applyLabel}
          staged={staged}
          onStage={toggleStaged}
          threshold={threshold}
          reviewLow={reviewLow}
          setSelectedId={setSelectedId}
        />
      )}
    </div>
  );
}

function Pager({ page, totalPages, total, setPage, shown }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        marginTop: 12,
      }}
    >
      <span className="muted" style={{ fontSize: 12 }}>
        Showing {fmtNumber(page * 50 + 1)}&ndash;{fmtNumber(page * 50 + shown)} of {fmtNumber(total)}
      </span>
      {totalPages > 1 && (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className="muted" style={{ fontSize: 12 }}>
            Page {page + 1} of {fmtNumber(totalPages)}
          </span>
          <button className="btn sm" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
            &larr; Prev
          </button>
          <button
            className="btn sm"
            disabled={page + 1 >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next &rarr;
          </button>
        </div>
      )}
    </div>
  );
}

// ============================================================
// ReviewTable — one row per pair, both sides side by side
// ============================================================

function ReviewTable({
  items,
  columns,
  priorityColumn,
  tracks,
  labelOf,
  onLabel,
  onLabelWithExtra,
  saving,
  staged,
  onStage,
  threshold,
  reviewLow,
  selectedId,
  setSelectedId,
}) {
  const [noteFor, setNoteFor] = useState(null);

  function trackLabel(key) {
    const t = tracks.find((x) => x.key === key);
    return t ? t.label : key;
  }

  return (
    <div className="tbl-wrap" style={{ position: "relative" }}>
      <table className="t">
        <thead>
          <tr>
            <th style={{ width: 26 }}></th>
            <th style={{ minWidth: 230 }}>
              First unit
              <TermHint name="unit" />
            </th>
            <th style={{ minWidth: 230 }}>Second unit</th>
            <th style={{ width: 120 }}>
              Score
              <TermHint name="score" />
            </th>
            <th style={{ width: 150 }}>
              Bucket
              <TermHint name="bucket" />
            </th>
            {priorityColumn && (
              <th style={{ width: 110, textAlign: "right" }}>{priorityColumn.label}</th>
            )}
            <th style={{ width: 190 }}>
              Your answer
              <TermHint name="label" />
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((m) => {
            const lab = labelOf(m);
            const sel = selectedId === m.pair_id;
            const stagedVal = staged[m.pair_id];
            const isStaged = stagedVal !== undefined;
            const effTrue = isStaged ? stagedVal === "TRUE" : lab?.is_match === "TRUE";
            const effFalse = isStaged ? stagedVal === "FALSE" : lab?.is_match === "FALSE";
            const busy = saving.has(m.pair_id);
            return (
              <Fragment key={m.pair_id}>
                <tr className={sel ? "selected" : ""} onClick={() => setSelectedId(m.pair_id)}>
                  <td style={{ verticalAlign: "top" }}>
                    <span
                      className={
                        "dot " +
                        (m.bucket === "accept" ? "green" : m.bucket === "review" ? "amber" : "red")
                      }
                    />
                  </td>
                  <UnitCell unit={m.left} />
                  <UnitCell unit={m.right} />
                  <td style={{ verticalAlign: "top" }}>
                    {/* When a model has scored the run its score leads and the
                        Splink score stays readable underneath. */}
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <ProbBar
                        p={m.gbt_score ?? m.match_probability}
                        w={44}
                        high={threshold}
                        review={reviewLow}
                      />
                      <span className="mono" style={{ fontWeight: 600 }}>
                        {fmtProb(m.gbt_score ?? m.match_probability)}
                      </span>
                    </div>
                    {m.gbt_score != null && (
                      <div className="mono muted" style={{ fontSize: 11 }}>
                        Splink {fmtProb(m.match_probability)}
                      </div>
                    )}
                    <div className="mono muted" style={{ fontSize: 11 }}>
                      {trackLabel(m.track)}
                    </div>
                    {m.usefulness && (
                      <div
                        className="muted"
                        style={{ fontSize: 11 }}
                        title="How much labelling this pair would teach the model, on a scale of 0 to 1."
                      >
                        worth labelling {Number(m.usefulness.score).toFixed(2)} of 1
                      </div>
                    )}
                  </td>
                  <td style={{ verticalAlign: "top", whiteSpace: "normal" }}>
                    {/* Where the pair landed, then how it got there. Two
                        questions, two chips, never one word doing both. */}
                    <BucketTag bucket={m.bucket} />
                    <Provenance kind="decided_by" value={m.decided_by} size="sm" />
                    <VetoTag pair={m} />
                    {m.import_disagrees && (
                      <div style={{ marginTop: 3 }}>
                        <span className="tag amber" title="The two sides carry different earlier IDs">
                          earlier IDs differ
                        </span>
                      </div>
                    )}
                    {m.held_group_id && (
                      <div className="muted" style={{ fontSize: 11 }}>
                        held group <span className="mono">{m.held_group_id}</span>
                      </div>
                    )}
                  </td>
                  {priorityColumn && (
                    <td className="mono tnum" style={{ textAlign: "right", verticalAlign: "top" }}>
                      <Cell
                        value={m.priority ? m.priority[priorityColumn.key] : null}
                        type={priorityColumn.type}
                      />
                    </td>
                  )}
                  <td onClick={(e) => e.stopPropagation()} style={{ verticalAlign: "top" }}>
                    <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
                      <button
                        className="btn sm"
                        disabled={busy}
                        title={isStaged ? "Staged — click to flip" : undefined}
                        style={{
                          ...(effTrue
                            ? { background: "var(--green)", color: "#fff", borderColor: "var(--green)" }
                            : {}),
                          ...(isStaged ? { borderStyle: "dashed" } : {}),
                        }}
                        onClick={() =>
                          isStaged ? onStage(m.pair_id, "TRUE") : onLabel(m.pair_id, "TRUE")
                        }
                      >
                        <Icons.check size={12} stroke={effTrue ? "#fff" : "currentColor"} />
                        {ANSWER.TRUE}
                      </button>
                      <button
                        className="btn sm"
                        disabled={busy}
                        title={isStaged ? "Staged — click to flip" : undefined}
                        style={{
                          ...(effFalse
                            ? { background: "var(--ti-red)", color: "#fff", borderColor: "var(--ti-red)" }
                            : {}),
                          ...(isStaged ? { borderStyle: "dashed" } : {}),
                        }}
                        onClick={() =>
                          isStaged ? onStage(m.pair_id, "FALSE") : onLabel(m.pair_id, "FALSE")
                        }
                      >
                        <Icons.x size={12} stroke={effFalse ? "#fff" : "currentColor"} />
                        {ANSWER.FALSE}
                      </button>
                      <button
                        className="btn sm ghost"
                        title="Notes and a source link for your answer"
                        onClick={() => setNoteFor(noteFor === m.pair_id ? null : m.pair_id)}
                      >
                        <Icons.doc size={12} />
                      </button>
                      {lab?.evidence_url && (
                        <a
                          className="btn sm ghost"
                          href={lab.evidence_url}
                          target="_blank"
                          rel="noreferrer"
                          title={lab.evidence_url}
                        >
                          <Icons.link size={12} />
                        </a>
                      )}
                      {isStaged && (
                        <span className="tag" style={{ borderColor: "#4C78A8", color: "#4C78A8" }}>
                          staged
                        </span>
                      )}
                      {busy && <span className="muted pulse" style={{ fontSize: 11 }}>saving…</span>}
                    </div>
                  </td>
                </tr>
                {noteFor === m.pair_id && (
                  <tr>
                    <td colSpan={priorityColumn ? 7 : 6} style={{ whiteSpace: "normal" }}>
                      <LabelNotes
                        label={lab}
                        onSave={(extra) =>
                          onLabelWithExtra(m.pair_id, lab?.is_match || "TRUE", extra)
                        }
                        onClose={() => setNoteFor(null)}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function UnitCell({ unit }) {
  const profile = useProfile();
  const ids = entityIds(unit);
  return (
    <td style={{ verticalAlign: "top", whiteSpace: "normal", overflowWrap: "anywhere" }}>
      <div style={{ fontWeight: 500 }}>{unit?.name || <span className="muted">(no name)</span>}</div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 2, alignItems: "center" }}>
        <span className="mono muted" style={{ fontSize: 11 }}>
          {unit?.unit_id}
        </span>
        {unit?.unit_size > 1 && (
          <span className="tag">
            ×{unit.unit_size} {noun(profile, "record_plural")}
          </span>
        )}
        {ids.map((id) => (
          <span
            key={id}
            className={"tag" + (ids.length > 1 ? " amber" : "")}
            style={{ fontFamily: "var(--font-mono)", textTransform: "none" }}
          >
            {id}
          </span>
        ))}
      </div>
    </td>
  );
}

// Notes and source link, shared by the table popover and the diff controls.
function LabelNotes({ label, onSave, onClose, compact }) {
  const [notes, setNotes] = useState(label?.notes || "");
  const [url, setUrl] = useState(label?.evidence_url || "");
  const urlOk = isHttpUrl(url);

  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
      <div className="field" style={{ flex: "1 1 260px", minWidth: 0 }}>
        <label>Notes {compact ? "" : "(saved with your answer)"}</label>
        <textarea
          className="textarea"
          style={{ minHeight: 52 }}
          placeholder="e.g. same address and the same employer on both records"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </div>
      <div className="field" style={{ flex: "1 1 260px", minWidth: 0 }}>
        <label>Source link</label>
        <input
          className="input"
          placeholder="https://..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          style={urlOk ? undefined : { borderColor: "var(--ti-red)" }}
        />
        <div className="muted" style={{ fontSize: 11.5, color: urlOk ? undefined : "var(--ti-red)" }}>
          {urlOk
            ? "Where the evidence for your answer lives — a news story, a register entry."
            : "That is not an http or https link."}
        </div>
      </div>
      <div style={{ display: "flex", gap: 6, alignItems: "center", paddingTop: 18 }}>
        <button
          className="btn sm primary"
          disabled={!urlOk}
          onClick={() => {
            onSave({ notes: notes.trim() || undefined, evidence_url: url.trim() || undefined });
            onClose && onClose();
          }}
        >
          Save
        </button>
        {onClose && (
          <button className="btn sm" onClick={onClose}>
            Close
          </button>
        )}
      </div>
    </div>
  );
}

// ============================================================
// ReviewDiff — one pair in full, with the queue beside it
// ============================================================

function ReviewDiff({
  runId,
  items,
  current,
  columns,
  displayColumns,
  eventColumns,
  profile,
  labelOf,
  onLabel,
  onLabelWithExtra,
  staged,
  onStage,
  threshold,
  reviewLow,
  setSelectedId,
}) {
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const cache = useRef({});
  const hasFocus = (profile.evidence_focus || []).length > 0;

  useEffect(() => {
    if (!current) return;
    const pairId = current.pair_id;
    if (cache.current[pairId]) {
      setDetail(cache.current[pairId]);
      setDetailError(null);
      return;
    }
    setDetail(null);
    let alive = true;
    api
      .getRunPair(runId, pairId)
      .then((res) => {
        if (!alive) return;
        cache.current[pairId] = res;
        setDetail(res);
        setDetailError(null);
      })
      .catch((err) => {
        if (alive) setDetailError(err.message);
      });
    return () => {
      alive = false;
    };
  }, [runId, current?.pair_id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!current) {
    return <Empty title="Nothing in this view" sub="Clear the band or widen the bucket." />;
  }

  const pair = detail || current;
  const lab = labelOf(current);
  const pos = items.findIndex((m) => m.pair_id === current.pair_id);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "260px minmax(0, 1fr)", gap: 16, alignItems: "start" }}>
      {/* Queue */}
      <div
        className="card"
        style={{ maxHeight: "calc(100vh - 220px)", overflow: "auto", position: "sticky", top: 70 }}
      >
        <div className="card-h" style={{ padding: "10px 12px" }}>
          <span className="eyebrow">Queue</span>
          <span className="muted" style={{ fontSize: 11, marginLeft: "auto" }}>
            {pos + 1} / {items.length}
          </span>
        </div>
        <div>
          {items.map((m) => {
            const sel = m.pair_id === current.pair_id;
            const ml = labelOf(m);
            const stagedVal = staged[m.pair_id];
            return (
              <div
                key={m.pair_id}
                onClick={() => setSelectedId(m.pair_id)}
                style={{
                  display: "grid",
                  gridTemplateColumns: "16px minmax(0, 1fr) auto",
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
                    (m.bucket === "accept" ? "green" : m.bucket === "review" ? "amber" : "red")
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
                    {m.left?.name}
                  </div>
                  <div
                    className="muted"
                    style={{
                      fontSize: 12,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {m.right?.name}
                  </div>
                  <div className="mono muted" style={{ fontSize: 11 }}>
                    {fmtProb(m.match_probability)}
                  </div>
                </div>
                {stagedVal === "TRUE" && (
                  <span className="tag green" title={`Staged as ${ANSWER[stagedVal]}`}>
                    staged
                  </span>
                )}
                {stagedVal === "FALSE" && (
                  <span
                    className="tag"
                    style={{ color: "var(--ti-red)" }}
                    title={`Staged as ${ANSWER[stagedVal]}`}
                  >
                    staged
                  </span>
                )}
                {!stagedVal && ml?.is_match === "TRUE" && <Icons.check size={12} stroke="var(--green)" />}
                {!stagedVal && ml?.is_match === "FALSE" && <Icons.x size={12} stroke="var(--ti-red)" />}
              </div>
            );
          })}
        </div>
      </div>

      {/* The pair */}
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <DiffHero pair={pair} high={threshold} review={reviewLow} />
        <BucketingLine bucketing={pair.bucketing} />
        {/* The hero's tag says where the pair landed. This says how it got
            there — the same chip, in the same words, as in the table. */}
        {pair.decided_by && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="muted" style={{ fontSize: 12 }}>
              How it was decided
            </span>
            <Provenance kind="decided_by" value={pair.decided_by} />
          </div>
        )}
        <VetoBanner pair={pair} />
        <FocusStrip
          pair={pair}
          profile={profile}
          showAll={showAll}
          onToggle={() => setShowAll((v) => !v)}
        />
        {(showAll || !hasFocus) && (
          <UnitCompare pair={pair} columns={columns} displayColumns={displayColumns} />
        )}
        {detailError ? (
          <p style={{ fontSize: 12.5, color: "var(--ti-red)" }}>{detailError}</p>
        ) : (
          <>
            <ModelExplain explanation={pair.model_explanation} />
            <PairExplain
              explanation={pair.explanation}
              matchWeight={pair.match_weight}
              matchProbability={pair.match_probability}
            />
            {(showAll || !hasFocus) && (
              <PairEvidence pair={pair} eventColumns={eventColumns} profile={profile} />
            )}
          </>
        )}
        <DiffControls
          pair={current}
          label={lab}
          onLabel={onLabel}
          onLabelWithExtra={onLabelWithExtra}
          staged={staged[current.pair_id]}
          onStage={onStage}
        />
      </div>
    </div>
  );
}

const CLEANED_OPEN_KEY = "review.cleanedColumnsOpen";

/* Which lines put this pair in its bucket, when they were set and who set
   them. The lines are not stamped on every pair row — the change is written
   down instead — so this is the entry in force when the pair was bucketed. */
function BucketingLine({ bucketing }) {
  if (!bucketing) return null;
  const at = bucketing.at ? new Date(bucketing.at) : null;
  const day =
    at && !Number.isNaN(at.getTime())
      ? at.toLocaleDateString("en-GB", { day: "numeric", month: "short" })
      : null;
  const who = bucketing.who && bucketing.who !== "system" ? bucketing.who : null;
  const scorer = valueLabel(SCORER, bucketing.scorer);
  const parts = [];
  if (bucketing.accept_line != null)
    parts.push(`accept at ${Number(bucketing.accept_line).toFixed(2)}`);
  if (bucketing.review_line != null)
    parts.push(`review at ${Number(bucketing.review_line).toFixed(2)}`);
  if (scorer) parts.push(`scorer ${scorer}`);
  if (parts.length === 0) return null;

  return (
    <p className="muted" style={{ fontSize: 12, margin: 0, lineHeight: 1.5 }}>
      Placed by the lines set{day ? ` on ${day}` : ""}
      {who ? ` by ${who}` : ""}: {parts.join(", ")}.
    </p>
  );
}

function readOpen() {
  try {
    return window.localStorage.getItem(CLEANED_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

// One row of the comparison, or nothing when neither side has a value.
function CompareRow({ col, left, right, mono }) {
  const a = left[col.key];
  const b = right[col.key];
  if ((a == null || a === "") && (b == null || b === "")) return null;
  const same = String(a ?? "") === String(b ?? "");
  const numeric = NUMERIC_TYPES.has(col.type);
  // A share arrives as a fraction; nobody reads 0.6 as "60% of them".
  const type = col.key === "share_round_1000" ? "percent" : col.type;
  const render = (v) =>
    type === "percent" && Number.isFinite(Number(v)) ? (
      `${Math.round(Number(v) * 100)}%`
    ) : (
      <Cell value={v} type={col.type} />
    );

  return (
    <tr>
      <td className="muted" style={{ whiteSpace: "normal" }}>
        {mono ? <span className="mono">{col.key}</span> : col.label}
      </td>
      {[a, b].map((v, i) => (
        <td
          key={i}
          className={numeric ? "mono tnum" : ""}
          style={{
            textAlign: numeric ? "right" : "left",
            whiteSpace: "normal",
            overflowWrap: "anywhere",
            background: same ? undefined : "var(--ti-red-50)",
          }}
        >
          {render(v)}
        </td>
      ))}
    </tr>
  );
}

// The same fields for both sides, aligned, with the differences marked. The
// profile's own columns lead, in the profile's order; everything cleaning and
// the derived columns wrote sit in a section that stays shut until asked for.
function UnitCompare({ pair, columns, displayColumns }) {
  const profile = useProfile();
  const left = pair?.left || {};
  const right = pair?.right || {};
  const [cleanedOpen, setCleanedOpen] = useState(readOpen);

  function toggleCleaned() {
    setCleanedOpen((open) => {
      try {
        window.localStorage.setItem(CLEANED_OPEN_KEY, open ? "0" : "1");
      } catch {
        // A browser with storage blocked simply forgets the choice.
      }
      return !open;
    });
  }

  const usable = (columns || []).filter(
    (c) => !SYSTEM_COLUMNS.has(c.key) && !PATTERN_COLUMNS.has(c.key)
  );
  const order = new Map((displayColumns || []).map((c, i) => [c.key, i]));
  const profileCols = usable
    .filter((c) => c.source !== "cleaning")
    .sort((a, b) => (order.get(a.key) ?? 999) - (order.get(b.key) ?? 999));
  const cleanedCols = usable.filter((c) => c.source === "cleaning");

  const hasRow = (c) => {
    const a = left[c.key];
    const b = right[c.key];
    return !((a == null || a === "") && (b == null || b === ""));
  };
  const cleanedShown = cleanedCols.filter(hasRow);

  if (profileCols.length === 0 && cleanedShown.length === 0) return null;

  return (
    <div className="card">
      <div className="card-h">
        <Icons.diff size={16} />
        <h3>The two {noun(profile, "record_plural")}</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          differences are marked
        </span>
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
          <thead>
            <tr>
              <th style={{ width: 180 }}>Field</th>
              <th>{left.name || "First unit"}</th>
              <th>{right.name || "Second unit"}</th>
            </tr>
          </thead>
          <tbody>
            {profileCols.map((c) => (
              <CompareRow key={c.key} col={c} left={left} right={right} />
            ))}

            {cleanedShown.length > 0 && (
              <tr>
                <td colSpan={3} style={{ background: "var(--surface-sub)", padding: 0 }}>
                  <button
                    className="btn sm ghost"
                    style={{ width: "100%", justifyContent: "flex-start", height: 30 }}
                    onClick={toggleCleaned}
                  >
                    {cleanedOpen ? <Icons.arrowD size={12} /> : <Icons.arrowR size={12} />}
                    Cleaned and derived columns
                    <span className="muted" style={{ fontSize: 11, marginLeft: 4 }}>
                      &middot; {cleanedShown.length}
                    </span>
                  </button>
                </td>
              </tr>
            )}
            {cleanedOpen &&
              cleanedShown.map((c) => (
                <CompareRow key={c.key} col={c} left={left} right={right} mono />
              ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ============================================================
// DiffControls — notes, source link, Match / Not a match / clear, with keys
// ============================================================

function DiffControls({ pair, label, onLabel, onLabelWithExtra, staged, onStage }) {
  const effective = staged || label?.is_match;
  const pairId = pair.pair_id;

  // One short bar rather than a tall card: it floats over the evidence tables
  // while the reviewer scrolls them, so it has to cover as little as possible.
  const [notesOpen, setNotesOpen] = useState(false);

  return (
    <div className="card" style={{ position: "sticky", bottom: 16 }}>
      <div className="card-b" style={{ padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            className="btn lg"
            onClick={() => (staged ? onStage(pairId, "TRUE") : onLabel(pairId, "TRUE"))}
            style={
              effective === "TRUE"
                ? { background: "var(--green)", color: "#fff", borderColor: "var(--green)" }
                : {}
            }
          >
            <Icons.check size={14} stroke={effective === "TRUE" ? "#fff" : "currentColor"} />{" "}
            {staged ? `Stage as ${ANSWER.TRUE}` : `Mark as ${ANSWER.TRUE}`}
            <span className="kh" style={{ marginLeft: 6 }}>
              <span className="kbd">T</span>
            </span>
          </button>
          <button
            className="btn lg"
            onClick={() => (staged ? onStage(pairId, "FALSE") : onLabel(pairId, "FALSE"))}
            style={
              effective === "FALSE"
                ? { background: "var(--ti-red)", color: "#fff", borderColor: "var(--ti-red)" }
                : {}
            }
          >
            <Icons.x size={14} stroke={effective === "FALSE" ? "#fff" : "currentColor"} />{" "}
            {staged ? `Stage as ${ANSWER.FALSE}` : `Mark as ${ANSWER.FALSE}`}
            <span className="kh" style={{ marginLeft: 6 }}>
              <span className="kbd">F</span>
            </span>
          </button>
          <button
            className="btn"
            onClick={() =>
              staged ? onStage(pairId, staged === "TRUE" ? "FALSE" : "TRUE") : onLabel(pairId, null)
            }
          >
            <Icons.refresh size={13} />
            {staged ? "Flip" : "Clear"}
            <span className="kh" style={{ marginLeft: 6 }}>
              <span className="kbd">U</span>
            </span>
          </button>
          <button className="btn" onClick={() => setNotesOpen((v) => !v)}>
            <Icons.doc size={13} />
            Notes &amp; source
            {(label?.notes || label?.evidence_url) && (
              <span className="dot green" style={{ marginLeft: 4 }} />
            )}
          </button>

          {staged && (
            <span className="tag" style={{ borderStyle: "dashed" }}>
              Staged as {ANSWER[staged] || "—"}; save or discard in the banner above
            </span>
          )}
          {label?.reviewer && !staged && (
            <span className="muted" style={{ fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 6 }}>
              {ANSWER[label.is_match] || "—"} by {label.reviewer}
              {label.provenance && label.provenance !== "manual" && (
                <Provenance kind="provenance" value={label.provenance} size="sm" />
              )}
            </span>
          )}
          <span className="spacer" />
          <span className="muted" style={{ fontSize: 11 }}>
            <span className="kbd">J</span> / <span className="kbd">K</span> to navigate
          </span>
        </div>

        {notesOpen && (
          <LabelNotes
            label={label}
            compact
            onSave={(extra) => onLabelWithExtra(pairId, label?.is_match || "TRUE", extra)}
            onClose={() => setNotesOpen(false)}
          />
        )}
      </div>
    </div>
  );
}

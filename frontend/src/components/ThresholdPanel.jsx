/* ============================================================
   ThresholdPanel — the score distribution, brushable
   ------------------------------------------------------------
   Two distinct controls on one distribution:
   - the two SLIDERS set the accept line and the review line, applied
     to the run by the re-bucket call
   - dragging across the HISTOGRAM brushes a band of scores for bulk
     labelling (a labelling slice, not a rule)

   The series come from GET /pairs/histogram. Bars can be stacked by
   final bucket, or by whether the earlier grouping agrees, which is
   how the owner picks a line: a band where the earlier grouping
   disagrees is a band worth reading rather than accepting.
   ============================================================ */

import { useRef, useState } from "react";
import { Icons } from "./Icons";
import { fmtNumber, fmtPct } from "./ProbBar";
import { Term, TermHint } from "./Term";
import { BUCKET_LABELS } from "./DiffHero";
import { FIGURE_SET_BY_KEY } from "../glossary";

/* The three buckets, in the one vocabulary the whole tool uses. */
const BUCKET_SERIES = [
  { key: "accept", label: BUCKET_LABELS.accept, colour: "var(--green)" },
  { key: "review", label: BUCKET_LABELS.review, colour: "var(--amber)" },
  { key: "reject", label: BUCKET_LABELS.reject, colour: "var(--ti-red)" },
];

function importSeries(earlier) {
  const name = earlier || "the earlier grouping";
  return [
    { key: "agrees", label: `${name} agrees`, colour: "var(--green)" },
    { key: "disagrees", label: `${name} disagrees`, colour: "var(--amber)" },
    { key: "unknown", label: "no earlier decision", colour: "var(--muted-2)" },
  ];
}

export function ThresholdPanel({
  threshold,
  setThreshold,
  reviewLow,
  setReviewLow,
  histogram,
  counts,
  committed,
  onCommit,
  committing,
  brushLo = null,
  brushHi = null,
  onBrush,
  scoreEval,
  model,
  earlier,
  showEarlier = true,
}) {
  const [stack, setStack] = useState("bucket"); // bucket | import
  const histoRef = useRef(null);
  const [drag, setDrag] = useState(null);

  // A run scored by a model is read on the model's score. Both sets of series
  // share one shape: edges of length bins+1, and every series a flat array of
  // length bins, so only the source object changes.
  const onModel = !!model?.active && histogram?.score_column === "gbt_score";
  const [showModelScore, setShowModelScore] = useState(true);
  const useModel = onModel && showModelScore;
  const series = useModel ? histogram?.by_score_column || {} : histogram || {};
  const edges = Array.isArray(histogram?.edges) ? histogram.edges : [];
  const nBins = Math.max(0, edges.length - 1);
  const stackSeries = stack === "bucket" ? BUCKET_SERIES : importSeries(earlier);
  const totals = Array.isArray(series.total) ? series.total : [];
  const maxBin = totals.length ? Math.max(...totals, 1) : 1;

  const rangeMin = nBins ? edges[0] : 0;
  const rangeMax = nBins ? edges[edges.length - 1] : 1;
  const rangeSpan = rangeMax - rangeMin || 1;
  const pct = (v) => `${((v - rangeMin) / rangeSpan) * 100}%`;

  const scoreAtClientX = (clientX) => {
    const el = histoRef.current;
    if (!el) return rangeMin;
    const r = el.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    return rangeMin + f * rangeSpan;
  };
  const commitDrag = (d) => {
    if (!d || !onBrush) return setDrag(null);
    const lo = Math.min(d.a, d.b);
    const hi = Math.max(d.a, d.b);
    if (hi - lo > 0.005) onBrush(+lo.toFixed(2), +hi.toFixed(2), useModel);
    setDrag(null);
  };
  const selLo = drag ? Math.min(drag.a, drag.b) : brushLo;
  const selHi = drag ? Math.max(drag.a, drag.b) : brushHi;
  const hasSel = selLo != null && selHi != null;

  const moved =
    committed &&
    (Math.abs(committed.high - threshold) >= 0.005 ||
      Math.abs(committed.review - reviewLow) >= 0.005);

  const nLabels = 6;
  const scaleLabels = [];
  for (let i = 0; i < nLabels; i++) scaleLabels.push(rangeMin + (i / (nLabels - 1)) * rangeSpan);

  return (
    <div className="card">
      <div className="card-h" style={{ paddingBottom: 8 }}>
        <Icons.bolt size={16} />
        <h3>
          Score distribution <TermHint name="score" />
        </h3>
        <span className="muted" style={{ fontSize: 12 }}>
          · every pair this run scored · drag the chart to label a band
        </span>
        <div className="actions">
          <div className="seg">
            <button className={stack === "bucket" ? "on" : ""} onClick={() => setStack("bucket")}>
              By bucket
            </button>
            {showEarlier && (
              <button className={stack === "import" ? "on" : ""} onClick={() => setStack("import")}>
                By {earlier || "earlier decisions"}
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="card-b" style={{ padding: "14px 18px 18px" }}>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
          {stackSeries.map((s) => (
            <span key={s.key} className="muted" style={{ fontSize: 11.5 }}>
              <span
                style={{
                  display: "inline-block",
                  width: 9,
                  height: 9,
                  borderRadius: 2,
                  background: s.colour,
                  marginRight: 5,
                }}
              />
              {s.label}
            </span>
          ))}
        </div>

        {onModel && (
          <div
            style={{
              border: "1px solid var(--line)",
              background: "var(--surface-sub)",
              borderRadius: 5,
              padding: "8px 10px",
              fontSize: 12,
              lineHeight: 1.5,
              marginBottom: 10,
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
            }}
          >
            <span>
              This run is read on the <Term name="modelScore" />.
              {model.accept != null && (
                <>
                  {" "}
                  It accepts at <span className="mono">{model.accept.toFixed(2)}</span> and above.
                </>
              )}
              {model.reject != null && (
                <>
                  {" "}
                  It rejects below <span className="mono">{model.reject.toFixed(2)}</span>.
                </>
              )}{" "}
              Those two lines are set from the <Term name="testSet" />, so they are not sliders.
              {!model.graded &&
                " This is a new model: it has not been measured against the test set, so it only" +
                  " re-orders the review queue and cannot move a pair into a different bucket."}
            </span>
            {model.warning && <span style={{ width: "100%" }}>{model.warning}</span>}
            <div className="seg" style={{ marginLeft: "auto" }}>
              <button className={showModelScore ? "on" : ""} onClick={() => setShowModelScore(true)}>
                Model score
              </button>
              <button className={!showModelScore ? "on" : ""} onClick={() => setShowModelScore(false)}>
                Splink score
              </button>
            </div>
          </div>
        )}

        <div style={{ position: "relative", padding: "0 4px" }}>
          {nBins > 0 ? (
            <div
              ref={histoRef}
              onMouseDown={
                onBrush
                  ? (e) => {
                      e.preventDefault();
                      const s = scoreAtClientX(e.clientX);
                      setDrag({ a: s, b: s });
                    }
                  : undefined
              }
              onMouseMove={
                onBrush ? (e) => drag && setDrag((d) => ({ ...d, b: scoreAtClientX(e.clientX) })) : undefined
              }
              onMouseUp={onBrush ? () => commitDrag(drag) : undefined}
              onMouseLeave={onBrush ? () => commitDrag(drag) : undefined}
              style={{
                position: "relative",
                display: "grid",
                gridTemplateColumns: `repeat(${nBins}, 1fr)`,
                gap: 1,
                alignItems: "flex-end",
                height: 72,
                marginBottom: 4,
                cursor: onBrush ? "crosshair" : "default",
              }}
            >
              {Array.from({ length: nBins }, (_, i) => {
                const total = totals[i] || 0;
                // Log height, as roe_ui does: one dominant bin must not flatten
                // everything else into nothing.
                const h = total > 0 ? Math.max(4, (Math.log(total + 1) / Math.log(maxBin + 1)) * 100) : 0;
                const lo = edges[i];
                const hi = edges[i + 1];
                const parts = stackSeries.map((s) => ({
                  ...s,
                  n: (Array.isArray(series[s.key]) ? series[s.key][i] : 0) || 0,
                }));
                const title =
                  `${lo.toFixed(2)}–${hi.toFixed(2)}: ${fmtNumber(total)}\n` +
                  parts.map((p) => `${p.label}: ${fmtNumber(p.n)}`).join("\n");
                return (
                  <div
                    key={i}
                    title={title}
                    style={{ height: `${h}%`, display: "flex", flexDirection: "column-reverse" }}
                  >
                    {parts.map((p) => (
                      <div
                        key={p.key}
                        style={{
                          height: total > 0 ? `${(p.n / total) * 100}%` : 0,
                          background: p.colour,
                          opacity: 0.85,
                        }}
                      />
                    ))}
                  </div>
                );
              })}

              {hasSel && (
                <div
                  style={{
                    position: "absolute",
                    top: 0,
                    bottom: 0,
                    left: pct(selLo),
                    width: `calc(${pct(selHi)} - ${pct(selLo)})`,
                    background: "rgba(76,120,168,.18)",
                    border: "1px solid #4C78A8",
                    borderRadius: 2,
                    pointerEvents: "none",
                  }}
                />
              )}
              <div
                style={{
                  position: "absolute",
                  top: -3,
                  bottom: 0,
                  left: pct(Math.max(useModel && model?.accept != null ? model.accept : threshold, rangeMin)),
                  borderLeft: "2px solid var(--green)",
                  pointerEvents: "none",
                }}
              />
              <div
                style={{
                  position: "absolute",
                  top: -3,
                  bottom: 0,
                  left: pct(Math.max(useModel && model?.reject != null ? model.reject : reviewLow, rangeMin)),
                  borderLeft: "2px solid var(--ti-red)",
                  pointerEvents: "none",
                }}
              />
            </div>
          ) : (
            <p className="muted" style={{ fontSize: 12.5, margin: "12px 0" }}>
              No histogram for this run yet.
            </p>
          )}

          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: 10.5,
              color: "var(--muted)",
              fontFamily: "var(--font-mono)",
              marginBottom: 10,
            }}
          >
            {scaleLabels.map((v, i) => (
              <span key={i}>{v.toFixed(2)}</span>
            ))}
          </div>

          <div style={{ marginTop: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
              <span className="eyebrow" style={{ color: "var(--green)" }}>
                Accept line <TermHint name="acceptLine" />
              </span>
              <span className="mono" style={{ fontSize: 12 }}>
                {threshold.toFixed(2)}
              </span>
            </div>
            <input
              type="range"
              min={rangeMin}
              max={1}
              step="0.01"
              value={threshold}
              onChange={(e) => setThreshold(Math.min(1, Math.max(+e.target.value, reviewLow)))}
              className="slider"
              style={{ width: "100%", display: "block" }}
            />
          </div>
          <div style={{ marginTop: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
              <span className="eyebrow" style={{ color: "var(--ti-red)" }}>
                Review line <TermHint name="reviewLine" />
              </span>
              <span className="mono" style={{ fontSize: 12 }}>
                {reviewLow.toFixed(2)}
              </span>
            </div>
            <input
              type="range"
              min={rangeMin}
              max={1}
              step="0.01"
              value={reviewLow}
              onChange={(e) => setReviewLow(Math.max(rangeMin, Math.min(+e.target.value, threshold)))}
              className="slider"
              style={{ width: "100%", display: "block" }}
            />
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
              marginTop: 10,
              fontSize: 11.5,
            }}
          >
            <span className="muted">
              <span style={{ color: "var(--ti-red)" }}>
                Rejected below {reviewLow.toFixed(2)}
              </span>
              {" · "}
              <span style={{ color: "var(--amber)" }}>
                For review {reviewLow.toFixed(2)}–{threshold.toFixed(2)} (
                {fmtNumber(counts?.review)})
              </span>
              {" · "}
              <span style={{ color: "var(--green)" }}>
                Accepted at {threshold.toFixed(2)} and above ({fmtNumber(counts?.accept)})
              </span>
            </span>
            <span className="spacer" />
            {moved && (
              <span className="tag amber">
                <span className="dot" />
                lines moved from {committed.review.toFixed(2)} / {committed.high.toFixed(2)}
              </span>
            )}
            <button
              className="btn sm"
              onClick={onCommit}
              disabled={committing || !moved || (onModel && model.graded)}
              title={
                onModel && model.graded
                  ? "A graded model has been measured against the test set, and sets the two lines from it"
                  : undefined
              }
            >
              {committing ? "Applying…" : "Apply these lines to the run"}
            </button>
          </div>

          {onBrush && (
            <div className="muted" style={{ fontSize: 11, marginTop: 8, textAlign: "center" }}>
              {hasSel ? (
                <>
                  Band <span className="mono">{selLo.toFixed(2)}–{selHi.toFixed(2)}</span> selected —
                  label it below.
                </>
              ) : (
                <>
                  Drag across the chart to grab a band to label. Moving a line re-buckets the run.
                  It saves no answers.
                </>
              )}
            </div>
          )}
        </div>

        {scoreEval && showEarlier && <ScoreEvalStrip scoreEval={scoreEval} earlier={earlier} />}
      </div>
    </div>
  );
}

/* How the run's grouping compares with the earlier grouping. Every row is one
   way of measuring the same run, and each says in one line what it leaves in
   and what it leaves out. */
function ScoreEvalStrip({ scoreEval, earlier }) {
  const name = earlier || "earlier grouping";
  const rows = [
    {
      key: "all",
      label: FIGURE_SET_BY_KEY.all.label,
      value: scoreEval,
      help: "Every exact group, plus every pair the run accepted, however it was accepted.",
    },
    {
      key: "score_only",
      label: FIGURE_SET_BY_KEY.score_only.label,
      value: scoreEval.score_only,
      help: `Only the pairs the score accepted. Pairs accepted because both sides already carried the same earlier ID are left out, so the ${name} is not marking its own work.`,
    },
    {
      // Only worth a row once a veto rule has actually stopped something. With
      // no veto rules in force this line repeats the one above it exactly.
      key: "without_vetoes",
      label: FIGURE_SET_BY_KEY.without_vetoes.label,
      value: scoreEval.vetoes?.vetoed > 0 ? scoreEval.without_vetoes?.score_only : null,
      help: "The same again, with every veto rule taken out. The gap between these two rows is what the veto rules cost in recall and bought in precision.",
    },
    {
      key: "splink_only",
      label: FIGURE_SET_BY_KEY.splink_only.label,
      value: scoreEval.splink_only,
      help: "What the Splink score would have decided on its own, with no model in force.",
    },
    {
      key: "model_only",
      label: FIGURE_SET_BY_KEY.model_only.label,
      value: scoreEval.model_only,
      help: `What the model score would have decided on its own, with nothing from the ${name} laid over it.`,
    },
    {
      key: "with_human",
      label: FIGURE_SET_BY_KEY.with_human.label,
      value: scoreEval.with_human,
      help: "The same, with every pair a reviewer answered Match joined up and every pair answered Not a match pulled apart. This is what the run would publish today.",
    },
    {
      key: "exact_only",
      label: FIGURE_SET_BY_KEY.exact_only.label,
      value: scoreEval.exact_only,
      help: "Only the match keys, with no scoring at all.",
    },
  ].filter((r) => r.value);

  return (
    <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 12 }}>
      <div className="eyebrow" style={{ marginBottom: 6 }}>
        Agreement with the {name} <TermHint name="earlierGrouping" />
      </div>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              <th>Measured on</th>
              <th style={{ width: 110, textAlign: "right" }}>
                Pair precision <TermHint name="pairPrecision" />
              </th>
              <th style={{ width: 110, textAlign: "right" }}>
                Pair recall <TermHint name="pairRecall" />
              </th>
              <th style={{ width: 120, textAlign: "right" }}>
                Entities after <TermHint name="entity" />
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td style={{ whiteSpace: "normal" }}>
                  {r.label}
                  <div className="muted" style={{ fontSize: 11.5 }}>
                    {r.help}
                  </div>
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {r.value.pair_precision == null ? "—" : fmtPct(r.value.pair_precision, 1)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {r.value.pair_recall == null ? "—" : fmtPct(r.value.pair_recall, 1)}
                </td>
                <td className="mono tnum" style={{ textAlign: "right" }}>
                  {r.value.entities_after == null ? "—" : fmtNumber(r.value.entities_after)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="muted" style={{ fontSize: 11.5, marginTop: 6, lineHeight: 1.5 }}>
        Pair precision is the share of the pairs this run joins that the {name} had already joined.
        Pair recall is the share of the pairs the {name} joined that this run joins too. Both count
        only pairs the {name} has an answer for.
        {scoreEval.conflicts > 0 && (
          <>
            {" "}
            <span style={{ color: "var(--ti-red)" }}>
              {fmtNumber(scoreEval.conflicts)} pairs conflict with an earlier decision.
            </span>
          </>
        )}
      </div>
    </div>
  );
}

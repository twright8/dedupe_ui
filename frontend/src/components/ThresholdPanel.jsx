/* ============================================================
   ThresholdPanel — auto-accept slider + brushable histogram
   ------------------------------------------------------------
   Two distinct controls on one distribution:
   - the SLIDER sets the auto-accept threshold (the provisional rule)
   - dragging across the HISTOGRAM brushes a score window for bulk
     labelling (a labelling slice, not the rule)
   ============================================================ */

import { useRef, useState } from "react";
import { Icons } from "./Icons";
import { fmtNumber } from "./ProbBar";

/**
 * @param {number}      props.threshold         — auto-accept threshold
 * @param {Function}    props.setThreshold      — setter
 * @param {number}      props.reviewLow         — pipeline review threshold (lower bound of the view)
 * @param {Object}      props.counts            — { accepted, review }
 * @param {Array}       props.histogram         — 20-bin counts across 0.0–1.0
 * @param {number|null} props.pipelineThreshold — the run's configured review threshold
 * @param {Object|null} props.previewDelta      — before/after movement versus committed run threshold
 * @param {number|null} props.brushLo           — committed brush lower bound (or null)
 * @param {number|null} props.brushHi           — committed brush upper bound (or null)
 * @param {Function}    [props.onBrush]         — (lo, hi) => void when a brush is drawn; null clears
 */
export function ThresholdPanel({
  threshold, setThreshold, reviewLow, setReviewLow, counts, histogram, pipelineThreshold,
  previewDelta = null,
  brushLo = null, brushHi = null, onBrush,
  isGbt = false, scoreLabel = null,
}) {
  // Score-aware wording: the GBT's calibrated 0-1 dial has a real auto-reject zone;
  // Splink's lower line is just its candidate-emit floor (nothing exists below it).
  const lowerName = isGbt ? "Auto-reject ≤" : "Review floor ≥";
  const dropWord = isGbt ? "auto-reject" : "drop";
  const which = scoreLabel || (isGbt ? "calibrated model score" : "simple-matcher score");
  const allBins = histogram && histogram.length >= 20 ? histogram : [];
  const binWidth = 1.0 / (allBins.length || 20);

  // Show the full retained range — from the lowest scored bin up to 1.0 — so the
  // auto-reject zone (below the bottom line) is visible, not sliced off.
  const firstNonEmpty = allBins.findIndex((v) => v > 0);
  const startBin = firstNonEmpty >= 0 ? firstNonEmpty : Math.floor(reviewLow / binWidth);
  const bins = allBins.slice(startBin);
  const rangeMin = startBin * binWidth;
  const rangeSpan = 1.0 - rangeMin;
  const maxBin = bins.length > 0 ? Math.max(...bins) : 1;

  // Position within the visible range (0% = rangeMin, 100% = 1.0)
  const pct = (v) => `${((v - rangeMin) / rangeSpan) * 100}%`;
  const pctInv = (v) => `${((1.0 - v) / rangeSpan) * 100}%`;

  // ----- Brush interaction -----
  const histoRef = useRef(null);
  const [drag, setDrag] = useState(null); // { a, b } in score units while dragging

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
    if (hi - lo > 0.005) onBrush(+lo.toFixed(2), +hi.toFixed(2));
    setDrag(null);
  };
  const selLo = drag ? Math.min(drag.a, drag.b) : brushLo;
  const selHi = drag ? Math.max(drag.a, drag.b) : brushHi;
  const hasSel = selLo != null && selHi != null;
  const noMovement =
    previewDelta &&
    previewDelta.changed === 0 &&
    Math.abs((previewDelta.threshold ?? threshold) - (previewDelta.committed ?? threshold)) >= 0.005;

  // Scale labels — evenly spaced across the visible range
  const nLabels = 6;
  const scaleLabels = [];
  for (let i = 0; i < nLabels; i++) {
    scaleLabels.push(rangeMin + (i / (nLabels - 1)) * rangeSpan);
  }

  return (
    <div className="card">
      <div className="card-h" style={{ paddingBottom: 8 }}>
        <Icons.bolt size={16} />
        <h3>Score distribution</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          · {which} · {isGbt
            ? "green = auto-accept, red = auto-reject (started from your Test set)"
            : "green = auto-accept, red = the emit floor"} · drag the chart to label a band</span>
        <div className="actions">
          <span className="tag green"><span className="dot" />auto-accept {fmtNumber(counts.accepted)}</span>
          <span className="tag amber"><span className="dot" />review {fmtNumber(counts.review)}</span>
          <span className="tag"><span className="dot" />scored {fmtNumber(counts.all)}</span>
        </div>
      </div>
      <div className="card-b" style={{ padding: "14px 18px 18px" }}>
        <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
          Every scored candidate. <span style={{ color: "var(--ti-red)" }}>Drag a band</span> to label a
          group; the two lines are output cutoffs — <span style={{ color: "var(--green)" }}>ship above</span>,{" "}
          <span style={{ color: "var(--ti-red)" }}>drop below</span>, review the middle.
        </div>
        <div style={{ position: "relative", padding: "0 4px" }}>
          {/* Distribution histogram (brushable) */}
          {bins.length > 0 && (
            <div
              ref={histoRef}
              onMouseDown={onBrush ? (e) => { e.preventDefault(); const s = scoreAtClientX(e.clientX); setDrag({ a: s, b: s }); } : undefined}
              onMouseMove={onBrush ? (e) => { if (drag) setDrag((d) => ({ ...d, b: scoreAtClientX(e.clientX) })); } : undefined}
              onMouseUp={onBrush ? () => commitDrag(drag) : undefined}
              onMouseLeave={onBrush ? () => commitDrag(drag) : undefined}
              style={{
                position: "relative",
                display: "grid",
                gridTemplateColumns: `repeat(${bins.length}, 1fr)`,
                gap: 1,
                alignItems: "flex-end",
                height: 56,
                marginBottom: 4,
                cursor: onBrush ? "crosshair" : "default",
              }}
            >
              {bins.map((v, i) => {
                const probAt = rangeMin + i * binWidth;
                const cls = probAt >= threshold ? "var(--green)" : probAt < reviewLow ? "var(--ti-red)" : "var(--amber)";
                const h = v > 0 ? Math.max(4, (Math.log(v + 1) / Math.log(maxBin + 1)) * 100) : 0;
                return (
                  <div
                    key={i}
                    style={{
                      background: v > 0 ? cls : "transparent",
                      opacity: 0.85,
                      height: `${h}%`,
                      borderRadius: "2px 2px 0 0",
                    }}
                    title={`${probAt.toFixed(2)}–${(probAt + binWidth).toFixed(2)}: ${fmtNumber(v)}`}
                  />
                );
              })}
              {/* Brush selection overlay */}
              {hasSel && (
                <div
                  style={{
                    position: "absolute",
                    top: 0, bottom: 0,
                    left: pct(selLo),
                    width: `calc(${pct(selHi)} - ${pct(selLo)})`,
                    background: "rgba(76,120,168,.18)",
                    border: "1px solid #4C78A8",
                    borderRadius: 2,
                    pointerEvents: "none",
                  }}
                />
              )}
              {/* Output-cutoff lines, aligned to the score scale so they sit ON the clumps */}
              <div style={{ position: "absolute", top: -3, bottom: 0, left: pct(Math.max(threshold, rangeMin)),
                borderLeft: "2px solid var(--green)", pointerEvents: "none" }} />
              {reviewLow > rangeMin && (
                <div style={{ position: "absolute", top: -3, bottom: 0, left: pct(reviewLow),
                  borderLeft: "2px solid var(--ti-red)", pointerEvents: "none" }} />
              )}
            </div>
          )}

          {/* Scale labels */}
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
            {scaleLabels.map((v, i) => <span key={i}>{v.toFixed(2)}</span>)}
          </div>

          {/* Two output-cutoff sliders. Both run the FULL chart scale [rangeMin..1] and
              are full-width, so each handle sits directly under its cut-off line on the
              histogram (same pct() mapping). We clamp the value in onChange instead of
              narrowing min/max — narrowing made one slider rescale the other's track, so
              the handle jumped without its number changing and stopped matching the line. */}
          <div style={{ marginTop: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
              <span className="eyebrow" style={{ color: "var(--green)" }}>Auto-accept &ge;</span>
              <span className="mono" style={{ fontSize: 12 }}>{threshold.toFixed(2)}</span>
            </div>
            <input
              type="range" min={rangeMin} max={1} step="0.01" value={threshold}
              onChange={(e) => setThreshold(Math.min(1, Math.max(+e.target.value, reviewLow)))}
              className="slider" style={{ width: "100%", display: "block" }}
              title="Ships everything at or above this to the download"
            />
          </div>
          <div style={{ marginTop: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
              <span className="eyebrow" style={{ color: "var(--ti-red)" }}>{lowerName}</span>
              <span className="mono" style={{ fontSize: 12 }}>{reviewLow.toFixed(2)}</span>
            </div>
            <input
              type="range" min={rangeMin} max={1} step="0.01" value={reviewLow}
              onChange={(e) => setReviewLow && setReviewLow(Math.max(rangeMin, Math.min(+e.target.value, threshold)))}
              className="slider" style={{ width: "100%", display: "block" }} disabled={!setReviewLow}
              title="Drops everything at or below this from the download"
            />
          </div>

          {/* Zone summary */}
          <div className="muted" style={{ fontSize: 11.5, marginTop: 9 }}>
            <span style={{ color: "var(--ti-red)" }}>{dropWord} &le; {reviewLow.toFixed(2)}</span>
            {" · "}
            <span style={{ color: "var(--amber)" }}>review {reviewLow.toFixed(2)}–{threshold.toFixed(2)} ({fmtNumber(counts.review)})</span>
            {" · "}
            <span style={{ color: "var(--green)" }}>ship &ge; {threshold.toFixed(2)} ({fmtNumber(counts.accepted)})</span>
          </div>

          {previewDelta && (
            <div
              style={{
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
                marginTop: 10,
                alignItems: "center",
                fontSize: 12,
              }}
            >
              <span className="muted">Preview vs committed rule:</span>
              <span className="tag green">
                auto-accept {previewDelta.acceptedDelta >= 0 ? "+" : ""}
                {fmtNumber(previewDelta.acceptedDelta)}
              </span>
              <span className="tag amber">
                review {previewDelta.reviewDelta >= 0 ? "+" : ""}
                {fmtNumber(previewDelta.reviewDelta)}
              </span>
              {noMovement && (
                <span className="tag">
                  <span className="dot" />
                  no pairs move at this cutoff
                </span>
              )}
            </div>
          )}

          {onBrush && (
            <div className="muted" style={{ fontSize: 11, marginTop: 8, textAlign: "center" }}>
              {hasSel
                ? <>Band <span className="mono">{selLo.toFixed(2)}–{selHi.toFixed(2)}</span> selected — label it below.</>
                : <>Drag across the chart to grab a band to label. The line just sets the output cutoff (changes the download, saves no answers).</>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

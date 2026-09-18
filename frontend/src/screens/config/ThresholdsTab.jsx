/* ============================================================
   Config tab: Thresholds & Splink
   ------------------------------------------------------------
   The two decision lines, the Splink parameters the last run used,
   and what moving the lines would do to the latest completed run.
   ============================================================ */

import { useState, useEffect, useRef } from "react";
import { api } from "../../api";
import { Icons } from "../../components/Icons";
import { fmtNumber } from "../../components/ProbBar";

export function readThresholds(settings = {}) {
  return {
    high: +(settings.match_probability_threshold_high ?? settings.threshold_auto_accept ?? 0.92),
    review: +(settings.match_probability_threshold_review ?? settings.threshold_review_lower ?? 0.5),
  };
}

export function readSplinkParams(settings = {}) {
  const params = settings.splink_params || settings.comparisons || [];
  return Array.isArray(params)
    ? params
        .map((p) => ({
          feature: p.feature || p.output_column_name || p.name,
          m: p.m,
          u: p.u,
          importance: p.importance,
        }))
        .filter((p) => p.feature && p.feature !== "suffix_norm")
    : [];
}

function countBandsFromHistogram(histogram, high, review) {
  const hist = Array.isArray(histogram) ? histogram : [];
  const n = hist.length || 20;
  return hist.reduce(
    (acc, count, i) => {
      const mid = (i + 0.5) / n;
      if (mid >= high) acc.auto_accept += count || 0;
      else if (mid >= review) acc.review_band += count || 0;
      else acc.below_floor += count || 0;
      return acc;
    },
    { auto_accept: 0, review_band: 0, below_floor: 0 }
  );
}

export default function ThresholdsTab({ thresh, setThresh, reviewLow, setReviewLow, splinkParams }) {
  const [effect, setEffect] = useState(null);
  const [effectLoading, setEffectLoading] = useState(false);
  const effectDebounce = useRef(null);

  useEffect(() => {
    clearTimeout(effectDebounce.current);
    effectDebounce.current = setTimeout(() => {
      setEffectLoading(true);
      api
        .listRuns()
        .then((runs) => {
          const list = Array.isArray(runs) ? runs : runs?.items || [];
          const latest = list.find((r) => r.status === "complete" && r.id);
          if (!latest) return null;
          return api.getRunDiagnostics(latest.id).then((diag) => ({ latest, diag }));
        })
        .then((payload) => {
          if (!payload?.diag) {
            setEffect(null);
            return;
          }
          const hist = payload.diag.histogram || [];
          const currentHigh = +(payload.diag.thresholds?.threshold_high ?? thresh);
          const currentReview = +(payload.diag.thresholds?.threshold_review ?? reviewLow);
          const before = countBandsFromHistogram(hist, currentHigh, currentReview);
          const after = countBandsFromHistogram(hist, thresh, reviewLow);
          setEffect({
            run_id: payload.latest.id,
            scored: hist.reduce((sum, n) => sum + (n || 0), 0),
            auto_accept: after.auto_accept,
            review_band: after.review_band,
            below_floor: after.below_floor,
            auto_accept_delta: after.auto_accept - before.auto_accept,
            review_band_delta: after.review_band - before.review_band,
            below_floor_delta: after.below_floor - before.below_floor,
          });
        })
        .catch(() => setEffect(null))
        .finally(() => setEffectLoading(false));
    }, 250);
    return () => clearTimeout(effectDebounce.current);
  }, [thresh, reviewLow]);

  const params = splinkParams || [];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <h3>Decision thresholds</h3>
        </div>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="field">
            <label>Auto-accept threshold</label>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range"
                min="0.7"
                max="0.99"
                step="0.01"
                value={thresh}
                onChange={(e) => setThresh(+e.target.value)}
                className="slider"
              />
              <span className="mono" style={{ minWidth: 60, fontSize: 16, fontWeight: 600 }}>
                {thresh.toFixed(2)}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              Pairs at or above this go straight to{" "}
              <span className="tag green" style={{ verticalAlign: "middle" }}>
                auto-accept
              </span>
              . Higher = fewer false positives but more review work.
            </div>
          </div>
          <div className="field">
            <label>Review-band lower bound</label>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range"
                min="0.3"
                max="0.7"
                step="0.01"
                value={reviewLow}
                onChange={(e) => setReviewLow(+e.target.value)}
                className="slider"
              />
              <span className="mono" style={{ minWidth: 60, fontSize: 16, fontWeight: 600 }}>
                {reviewLow.toFixed(2)}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12 }}>
              The scorer only emits candidates at or above this floor. Lower it and run the
              pipeline again to inspect weaker possible matches; raising it leaves fewer candidates
              available for review.
            </div>
          </div>
          <hr className="rule" />
          <div>
            <div className="eyebrow" style={{ marginBottom: 8 }}>
              Splink model parameters
            </div>
            {params.length === 0 ? (
              <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
                No Splink parameters in this config version yet. They appear once a run has
                trained a model.
              </p>
            ) : (
              <table className="t" style={{ borderRadius: 0 }}>
                <thead>
                  <tr>
                    <th>Feature</th>
                    <th>Weight (m)</th>
                    <th>Weight (u)</th>
                    <th>Importance</th>
                  </tr>
                </thead>
                <tbody>
                  {params.map((r) => (
                    <tr key={r.feature}>
                      <td className="mono" style={{ fontSize: 12.5 }}>
                        {r.feature}
                      </td>
                      <td className="mono">{(r.m != null ? r.m : 0).toFixed(2)}</td>
                      <td className="mono">{(r.u != null ? r.u : 0).toFixed(2)}</td>
                      <td style={{ width: 160 }}>
                        <div className="probbar" style={{ width: 140 }}>
                          <i
                            style={{
                              width: `${(r.importance || 0) * 100}%`,
                              background: "var(--ti-red)",
                            }}
                          />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>

      <div className="card" style={{ alignSelf: "flex-start" }}>
        <div className="card-h">
          <h3>Effect on current run</h3>
          <span className="muted" style={{ fontSize: 12 }}>
            {effect?.run_id ? `latest complete run: ${effect.run_id}` : "(latest complete run)"}
          </span>
        </div>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div className="kpi" style={{ padding: 12 }}>
              <div className="label">Auto-accept</div>
              <div className="value" style={{ fontSize: 20 }}>
                {effectLoading ? "..." : effect ? fmtNumber(effect.auto_accept) : "--"}
              </div>
              {effect?.auto_accept_delta != null && (
                <div className={`delta ${effect.auto_accept_delta >= 0 ? "up" : "down"}`}>
                  {effect.auto_accept_delta >= 0 ? "+" : ""}
                  {effect.auto_accept_delta}
                </div>
              )}
            </div>
            <div className="kpi" style={{ padding: 12 }}>
              <div className="label">Review band</div>
              <div className="value" style={{ fontSize: 20, color: "var(--amber)" }}>
                {effect ? fmtNumber(effect.review_band) : "--"}
              </div>
              {effect?.review_band_delta != null && (
                <div className={`delta ${effect.review_band_delta >= 0 ? "up" : "down"}`}>
                  {effect.review_band_delta >= 0 ? "+" : ""}
                  {effect.review_band_delta}
                </div>
              )}
            </div>
            <div className="kpi" style={{ padding: 12 }}>
              <div className="label">Below floor</div>
              <div className="value" style={{ fontSize: 20, color: "var(--ti-red)" }}>
                {effectLoading ? "..." : effect ? fmtNumber(effect.below_floor) : "--"}
              </div>
              {effect?.below_floor_delta != null && (
                <div className={`delta ${effect.below_floor_delta >= 0 ? "down" : "up"}`}>
                  {effect.below_floor_delta >= 0 ? "+" : ""}
                  {effect.below_floor_delta}
                </div>
              )}
            </div>
            <div className="kpi" style={{ padding: 12 }}>
              <div className="label">Scored candidates</div>
              <div className="value" style={{ fontSize: 20 }}>
                {effectLoading ? "..." : effect ? fmtNumber(effect.scored) : "--"}
              </div>
              <div className="delta muted">from latest run histogram</div>
            </div>
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            Preview uses already-scored candidates from the latest completed run. Changing the lower
            floor affects what future runs ask Splink to emit; unseen weaker pairs are not estimated here.
          </div>
          <button className="btn primary">
            <Icons.play size={14} stroke="#fff" />
            Save &amp; re-run with this config
          </button>
        </div>
      </div>
    </div>
  );
}

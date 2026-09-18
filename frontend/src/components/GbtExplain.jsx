/* ============================================================
   GbtExplain — per-pair "why this score", the honest version.
   Fetches exact Tree-SHAP contributions (LightGBM pred_contrib)
   for the selected OCOD<->ROE pair and shows which signals pushed
   the score up or down. Unlike the Splink feature panel, this
   reflects the values the model actually scored on.
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "../api";
import { Icons } from "./Icons";

// Plain-English names for the model's features (shared with the table popover).
export const FEATURE_LABELS = {
  base_name_jw: "Name spelling similarity",
  base_exact: "Names exactly identical",
  base_len_diff: "Name length gap",
  name_core_exact: "Core name identical (ignoring Ltd/Co)",
  name_tokens_sorted_exact: "Same set of words",
  name_digits_sorted_exact: "Same numbers",
  unit_mismatch: "Different unit / number (e.g. 13 vs 4)",
  splink_p1: "Splink's own probability",
  idf_token_overlap: "Shared distinctive words",
};

export function GbtExplain({ runId, pair }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const oc = pair?.ocod_name_clean, rn = pair?.roe_company_number, jur = pair?.jurisdiction_clean;

  useEffect(() => {
    if (!runId || !oc || !rn) { setData(null); setErr(null); return; }
    let live = true;
    setLoading(true); setErr(null); setData(null);
    api.explainPair(runId, { ocod_name_clean: oc, jurisdiction_clean: jur, roe_company_number: rn })
      .then((d) => { if (live) setData(d); })
      .catch((e) => { if (live) setErr(e.message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [runId, oc, jur, rn]);

  if (!runId) return null;
  if (loading) {
    return (
      <div className="card"><div className="card-b">
        <p className="muted pulse" style={{ fontSize: 12, margin: 0 }}>Explaining the model score…</p>
      </div></div>
    );
  }
  // Stay invisible when there's no model / the pair isn't in this run — no noise.
  if (err || !data) return null;

  // Drop near-zero drivers; scale bars to the biggest mover.
  const items = (data.contributions || []).filter((c) => Math.abs(c.contribution) >= 0.05);
  const maxAbs = items.reduce((m, c) => Math.max(m, Math.abs(c.contribution)), 0.001);

  return (
    <div className="card">
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Why the model scored this</h3>
        <span className="muted" style={{ fontSize: 12, marginLeft: "auto" }}>
          model probability {(data.calibrated * 100).toFixed(0)}%
        </span>
      </div>
      <div className="card-b">
        {(
          <>
            <p className="muted" style={{ fontSize: 12, marginTop: 0, marginBottom: 10 }}>
              How much each signal pushed the score{" "}
              <span style={{ color: "var(--green)" }}>up</span> or{" "}
              <span style={{ color: "var(--ti-red)" }}>down</span> — biggest movers first.
            </p>
            <div style={{ display: "grid", gap: 6 }}>
              {items.map((c) => {
                const up = c.contribution >= 0;
                const w = (Math.abs(c.contribution) / maxAbs) * 50;
                return (
                  <div key={c.feature}
                    style={{ display: "grid", gridTemplateColumns: "1fr 110px 46px", gap: 8, alignItems: "center", fontSize: 12 }}>
                    <span title={`${c.feature} = ${c.value.toFixed(2)}`}
                      style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {FEATURE_LABELS[c.feature] || c.feature}
                    </span>
                    <div style={{ position: "relative", height: 12, background: "var(--paper-2, #f1f1f3)", borderRadius: 3 }}>
                      <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "var(--line)" }} />
                      <div style={{
                        position: "absolute", top: 1, bottom: 1,
                        ...(up ? { left: "50%" } : { right: "50%" }),
                        width: `${w}%`,
                        background: up ? "var(--green)" : "var(--ti-red)",
                        borderRadius: 2,
                      }} />
                    </div>
                    <span className="mono" style={{ textAlign: "right", color: up ? "var(--green)" : "var(--ti-red)" }}>
                      {up ? "+" : ""}{c.contribution.toFixed(2)}
                    </span>
                  </div>
                );
              })}
            </div>
            <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>
              Exact contributions (Tree-SHAP, in log-odds) summing from a {data.base.toFixed(2)} baseline to the raw
              score, which calibration maps to {(data.calibrated * 100).toFixed(0)}%. This is what the model
              actually scored on — the panel above is the simpler Splink view.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

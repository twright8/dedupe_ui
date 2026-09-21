/* ============================================================
   PairExplain — what each comparison contributed to the score
   ------------------------------------------------------------
   The pair detail returns one row per comparison: the agreement
   level it reached, the label Splink gives that level, and the
   evidence in bits. Positive bits push the pair towards a match,
   negative away from it, and a null level contributed nothing.

   The score at the end is a decimal, the same format as every
   other score on every other screen.
   ============================================================ */

import { useState } from "react";
import { Icons } from "./Icons";
import { Term } from "./Term";
import { fmtProb } from "./ProbBar";

function barWidth(weight, max) {
  if (weight == null || !max) return 0;
  return Math.min(100, (Math.abs(weight) / max) * 100);
}

export function PairExplain({ explanation, matchWeight, matchProbability }) {
  // The engine's own wording is kept for a bug report and a diagnostic screen.
  // It is never what a reviewer reads first.
  const [technical, setTechnical] = useState(false);
  const rows = Array.isArray(explanation) ? explanation : [];
  if (rows.length === 0) return null;

  const max = Math.max(1, ...rows.map((r) => Math.abs(r.match_weight || 0)));
  const hasEngineWords = rows.some((r) => r.engine_label);

  return (
    <div className="card">
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Why this score</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          evidence in bits · 0 is even odds, each point doubles them
        </span>
        {hasEngineWords && (
          <button
            className="btn sm ghost"
            style={{ marginLeft: "auto" }}
            onClick={() => setTechnical((v) => !v)}
            aria-pressed={technical}
          >
            {technical ? "Hide the technical wording" : "Technical wording"}
          </button>
        )}
        {matchWeight != null && (
          <span
            className="tag"
            style={{ marginLeft: hasEngineWords ? 0 : "auto", fontFamily: "var(--font-mono)" }}
          >
            total {matchWeight >= 0 ? "+" : "−"}
            {Math.abs(matchWeight).toFixed(2)}
          </span>
        )}
      </div>
      <div className="card-b">
        <div className="features">
          {rows.map((r, i) => {
            const w = r.match_weight;
            const none = w == null;
            const positive = !none && w >= 0;
            return (
              <div
                className="ft"
                key={`${r.column || "c"}-${r.gamma ?? i}`}
                style={{ alignItems: "center" }}
              >
                {/* `label` is what a reviewer reads: "Same forename", never
                    "Exact match on forename_canon". `column_label` is the
                    column as a phrase, so no stored column name reaches the
                    screen. The engine's own wording sits behind the toggle
                    above. */}
                <div className="lab" style={{ whiteSpace: "normal" }}>
                  {r.label || r.description || r.column_label || r.column}
                  <div className="muted" style={{ fontSize: 11 }}>
                    {none ? (
                      "nothing to compare"
                    ) : (
                      <>
                        {r.column_label || r.column}
                        {r.gamma != null && ` · comparison level ${r.gamma}`}
                      </>
                    )}
                  </div>
                  {technical && (
                    <div className="mono muted" style={{ fontSize: 11 }}>
                      {r.engine_label || "no engine wording"}
                      {r.column ? ` · ${r.column}` : ""}
                    </div>
                  )}
                </div>
                <div className="bar" title={none ? "contributed nothing" : `${w} bits`}>
                  <i
                    style={{
                      width: `${barWidth(w, max)}%`,
                      background: none
                        ? "var(--line-strong)"
                        : positive
                          ? "var(--green)"
                          : "var(--ti-red)",
                    }}
                  />
                </div>
                <div className="val">
                  {none ? (
                    <span className="muted">—</span>
                  ) : (
                    <span style={{ color: positive ? "var(--green)" : "var(--ti-red)" }}>
                      {positive ? "+" : "−"}
                      {Math.abs(w).toFixed(2)}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <p className="muted" style={{ fontSize: 11.5, margin: "10px 0 0", lineHeight: 1.5 }}>
          A <Term name="comparison" /> with a dash had nothing to compare on one side, so it
          neither helped nor hurt. The bits add up to the total, which the model turns into the
          score{matchProbability != null && <> of {fmtProb(matchProbability)}</>}.
        </p>
      </div>
    </div>
  );
}

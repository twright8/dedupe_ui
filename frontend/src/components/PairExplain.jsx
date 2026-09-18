/* ============================================================
   PairExplain — what each comparison contributed to the score
   ------------------------------------------------------------
   The pair detail returns one row per comparison: the agreement
   level it reached, the label Splink gives that level, and the
   evidence in bits. Positive bits push the pair towards a match,
   negative away from it, and a null level contributed nothing.
   The bar list keeps the shape of roe_ui's feature breakdown.
   ============================================================ */

import { Icons } from "./Icons";

function barWidth(weight, max) {
  if (weight == null || !max) return 0;
  return Math.min(100, (Math.abs(weight) / max) * 100);
}

export function PairExplain({ explanation, matchWeight, matchProbability }) {
  const rows = Array.isArray(explanation) ? explanation : [];
  if (rows.length === 0) return null;

  const max = Math.max(1, ...rows.map((r) => Math.abs(r.match_weight || 0)));

  return (
    <div className="card">
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Why this score</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          evidence in bits · 0 is even odds, each point doubles them
        </span>
        {matchWeight != null && (
          <span className="tag" style={{ marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
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
              <div className="ft" key={r.column || i} style={{ alignItems: "center" }}>
                {/* A null level has no Splink label worth reading — "x is NULL"
                    tells a reviewer nothing — so the comparison's own
                    description leads and the state is said in plain words. */}
                <div className="lab" style={{ whiteSpace: "normal" }}>
                  {none ? r.description || r.column : r.label || r.description || r.column}
                  <div className="mono muted" style={{ fontSize: 11 }}>
                    {none ? (
                      <span style={{ fontFamily: "var(--font-sans)" }}>nothing to compare</span>
                    ) : (
                      <>
                        {r.column}
                        {r.gamma != null && ` · level ${r.gamma}`}
                      </>
                    )}
                  </div>
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
          A comparison with a dash had nothing to compare on one side, so it neither helped nor
          hurt. The bits add up to the total, which the model turns into the score
          {matchProbability != null && <> of {(matchProbability * 100).toFixed(1)}%</>}.
        </p>
      </div>
    </div>
  );
}

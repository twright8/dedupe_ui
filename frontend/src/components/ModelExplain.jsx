/* ============================================================
   ModelExplain — why the model scored one pair the way it did
   ------------------------------------------------------------
   The pair detail carries exact Tree-SHAP contributions. Each one
   is how far a piece of evidence pushed this pair's score, in
   log-odds. A positive contribution pushed towards "the same", a
   negative one away. They are grouped by kind of evidence so the
   reader can see whether the name did the work or something else.
   ============================================================ */

import { useState } from "react";
import { Icons } from "./Icons";
import { groupColour } from "./ModelPanel";

const GROUP_LABELS = {
  splink: "Splink",
  name: "Name",
  rarity: "Name rarity",
  recipients: "Recipients",
  timing: "Timing",
  amounts: "Amounts",
  size: "Size",
};

const TOP = 8;

export function ModelExplain({ explanation }) {
  const [showAll, setShowAll] = useState(false);
  if (!explanation) return null;

  const all = Array.isArray(explanation.contributions) ? explanation.contributions : [];
  if (all.length === 0) return null;

  const rows = showAll ? all : all.slice(0, TOP);
  const max = Math.max(1e-9, ...all.map((c) => Math.abs(c.contribution || 0)));

  // The rows arrive biggest first. Grouping keeps that order inside each group
  // and puts the group with the strongest single contribution first.
  const groups = [];
  for (const row of rows) {
    const key = row.group || "other";
    let bucket = groups.find((g) => g.key === key);
    if (!bucket) {
      bucket = { key, rows: [] };
      groups.push(bucket);
    }
    bucket.rows.push(row);
  }

  return (
    <div className="card">
      <div className="card-h">
        <Icons.spark size={16} />
        <h3>Why the model scored it this way</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          version {explanation.version}
          {explanation.graded ? "" : " · cold start, so it decides nothing"}
        </span>
        <span className="tag" style={{ marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
          {(explanation.score * 100).toFixed(1)}%
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {groups.map((g) => (
          <div key={g.key}>
            <div className="eyebrow" style={{ marginBottom: 4, color: groupColour(g.key) }}>
              {GROUP_LABELS[g.key] || g.key}
            </div>
            <div className="features">
              {g.rows.map((c) => {
                const positive = (c.contribution || 0) >= 0;
                return (
                  <div className="ft" key={c.name} style={{ alignItems: "center" }}>
                    <div className="lab" style={{ whiteSpace: "normal" }}>
                      {c.label || c.name}
                      {(c.value_label || c.value != null) && (
                        <>
                          {" "}
                          <span className="mono">· {c.value_label ?? c.value}</span>
                        </>
                      )}
                      {c.value == null && !c.value_label && (
                        <div className="muted" style={{ fontSize: 11 }}>
                          nothing to compare
                        </div>
                      )}
                    </div>
                    <div
                      className="bar"
                      title={`${positive ? "pushes towards" : "pushes away from"} a match by ${Math.abs(
                        c.contribution
                      ).toFixed(3)}`}
                    >
                      <i
                        style={{
                          width: `${(Math.abs(c.contribution || 0) / max) * 100}%`,
                          background: positive ? "var(--green)" : "var(--ti-red)",
                        }}
                      />
                    </div>
                    <div className="val">
                      <span style={{ color: positive ? "var(--green)" : "var(--ti-red)" }}>
                        {positive ? "+" : "−"}
                        {Math.abs(c.contribution).toFixed(2)}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}

        {all.length > TOP && (
          <div>
            <button className="btn sm" onClick={() => setShowAll((v) => !v)}>
              {showAll ? "Show the strongest only" : `Show all ${all.length}`}
            </button>
          </div>
        )}

        <p className="muted" style={{ fontSize: 11.5, margin: 0, lineHeight: 1.55 }}>
          A green bar pushed this pair towards being the same thing, a red bar away from it. The bars
          start from what the model expects of any pair and add up to this score.{" "}
          {explanation.known_limit}
        </p>
      </div>
    </div>
  );
}

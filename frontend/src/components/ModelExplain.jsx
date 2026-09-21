/* ============================================================
   ModelExplain — why the model scored one pair the way it did
   ------------------------------------------------------------
   The pair detail carries one number per piece of evidence: how
   much that piece moved this pair's score. A positive number moved
   it towards "the same thing", a negative one away. They are
   grouped by kind of evidence, so the reader can see whether the
   name did the work or something else. The groups are named and
   coloured in ModelPanel, so both screens read the same way.
   ============================================================ */

import { useState } from "react";
import { Icons } from "./Icons";
import { Term } from "./Term";
import { fmtProb } from "./ProbBar";
import { groupColour, groupLabel } from "./ModelPanel";

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
          {explanation.graded ? null : (
            <>
              {" · "}
              <Term name="newModel" />, so it decides nothing
            </>
          )}
        </span>
        <span className="tag" style={{ marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
          score {explanation.score == null ? "—" : fmtProb(explanation.score)}
        </span>
      </div>
      <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {groups.map((g) => (
          <div key={g.key}>
            <div className="eyebrow" style={{ marginBottom: 4, color: groupColour(g.key) }}>
              {groupLabel(g.key)}
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
                      title={`${
                        positive ? "moved the score towards" : "moved the score away from"
                      } a match by ${Math.abs(c.contribution || 0).toFixed(3)}`}
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
                        {Math.abs(c.contribution || 0).toFixed(2)}
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
          A green bar moved this pair towards being the same thing, a red bar away from it. The bars
          start from what the model expects of any pair and add up to this score.{" "}
          {explanation.known_limit}
        </p>
      </div>
    </div>
  );
}

/* ============================================================
   PairEvidence — the two sides' child rows, side by side
   ------------------------------------------------------------
   Beyond the name, a reviewer judges a pair on the pattern of the
   rows behind each side (DESIGN.md D13a, D13b). Each side gets a
   one-line summary the profile declares, then its rows newest
   first, so the two read as one comparison rather than two lists.
   ============================================================ */

import { Cell, NUMERIC_TYPES } from "./cells";
import { noun, patternSummary } from "../profileText";

export { patternSummary };

// The date column an event table sorts on, chosen from what the profile names.
function dateKey(columns) {
  const byType = (columns || []).find((c) => c.type === "date");
  if (byType) return byType.key;
  const byName = (columns || []).find((c) => /date/i.test(c.key));
  return byName ? byName.key : null;
}

// One side's events, newest first.
function EventTable({ events, columns, truncated, rowsWord }) {
  const key = dateKey(columns);
  const rows = Array.isArray(events) ? events.slice() : [];
  if (key) {
    rows.sort((a, b) => String(b[key] || "").localeCompare(String(a[key] || "")));
  }

  if (rows.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12.5, margin: 0, padding: "10px 0" }}>
        No {rowsWord} recorded for this side.
      </p>
    );
  }

  return (
    <>
      <div className="tbl-wrap">
        <table className="t" style={{ borderRadius: 0 }}>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c.key}
                  style={{
                    textAlign: NUMERIC_TYPES.has(c.type) ? "right" : "left",
                    whiteSpace: "nowrap",
                  }}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((e, i) => (
              <tr key={i}>
                {columns.map((c) => {
                  const numeric = NUMERIC_TYPES.has(c.type);
                  return (
                    <td
                      key={c.key}
                      className={numeric ? "mono tnum" : ""}
                      style={{
                        textAlign: numeric ? "right" : "left",
                        whiteSpace: "normal",
                      }}
                    >
                      <Cell value={e[c.key]} type={c.type} />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && (
        <p className="muted" style={{ fontSize: 11.5, margin: "6px 0 0" }}>
          Only the first {rows.length} {rowsWord} are shown.
        </p>
      )}
    </>
  );
}

export function PairEvidence({ pair, eventColumns, profile }) {
  const columns = Array.isArray(eventColumns) ? eventColumns : [];
  const events = pair?.events || {};
  const left = pair?.left || {};
  const right = pair?.right || {};

  if (columns.length === 0) return null;

  return (
    <div className="card">
      <div className="card-h">
        <Icon />
        <h3>Evidence</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          the {noun(profile, "unit_evidence")} behind each side, newest first
        </span>
      </div>
      <div className="card-b">
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
          {/* Each side is headed by its own name, so neither column needs a
              word of its own. */}
          {[
            { side: left, rows: events.left },
            { side: right, rows: events.right },
          ].map((s, i) => (
            <div key={i} style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 2 }}>
                {s.side.name || <span className="muted">(no name)</span>}
              </div>
              <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                {patternSummary(s.side, profile) || "no summary recorded"}
              </div>
              <EventTable
                events={s.rows}
                columns={columns}
                truncated={pair?.events_truncated}
                rowsWord={noun(profile, "evidence_row_plural")}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// Local so this file does not pull the whole icon set in for one glyph.
function Icon() {
  return (
    <svg className="i" width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}>
      <rect x="3" y="4" width="18" height="16" rx="1" />
      <path d="M3 10h18M9 4v16" />
    </svg>
  );
}

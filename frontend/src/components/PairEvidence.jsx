/* ============================================================
   PairEvidence — the two sides' child rows, side by side
   ------------------------------------------------------------
   Beyond the name, the team judges a pair on the pattern of the
   underlying events: a donor who always gives £10,000, or two
   records whose giving never overlaps in time (DESIGN.md D13a,
   D13b). Each side gets a one-line summary built from the unit's
   own columns, then its events newest first, so the two read as
   one comparison rather than two lists.
   ============================================================ */

import { Cell, NUMERIC_TYPES } from "./cells";
import { fmtNumber } from "./ProbBar";

// The date column an event table sorts on, chosen from what the profile names.
function dateKey(columns) {
  const byType = (columns || []).find((c) => c.type === "date");
  if (byType) return byType.key;
  const byName = (columns || []).find((c) => /date/i.test(c.key));
  return byName ? byName.key : null;
}

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return "£" + Math.round(n).toLocaleString("en-GB");
}

/* One line saying what this side's giving looks like, from the columns the
   score stage adds. Every part is skipped when its column is missing, so a
   profile without donation patterns simply shows less. */
export function patternSummary(unit) {
  if (!unit) return "";
  const parts = [];

  const n = unit.n_donations ?? unit.n_events;
  if (n != null) parts.push(`${fmtNumber(n)} donation${Number(n) === 1 ? "" : "s"}`);

  const typical = unit.modal_value ?? unit.median_value;
  const typicalText = money(typical);
  if (typicalText) {
    parts.push(
      `${unit.modal_value != null ? "usually" : "typically"} ${typicalText}`
    );
  }

  if (unit.n_distinct_values != null && Number(unit.n_distinct_values) > 0) {
    parts.push(
      `${fmtNumber(unit.n_distinct_values)} different amount${
        Number(unit.n_distinct_values) === 1 ? "" : "s"
      }`
    );
  }

  const round = Number(unit.share_round_1000);
  if (Number.isFinite(round)) parts.push(`${Math.round(round * 100)}% round thousands`);

  const first = unit.first_year;
  const last = unit.last_year;
  if (first != null && last != null) {
    parts.push(first === last ? String(first) : `${first}–${last}`);
  }

  return parts.join(" · ");
}

// One side's events, newest first.
function EventTable({ events, columns, truncated }) {
  const key = dateKey(columns);
  const rows = Array.isArray(events) ? events.slice() : [];
  if (key) {
    rows.sort((a, b) => String(b[key] || "").localeCompare(String(a[key] || "")));
  }

  if (rows.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12.5, margin: 0, padding: "10px 0" }}>
        No individual donations recorded for this side.
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
          Only the first {rows.length} are shown.
        </p>
      )}
    </>
  );
}

export function PairEvidence({ pair, eventColumns }) {
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
          the donations behind each side, newest first
        </span>
      </div>
      <div className="card-b">
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
          {[
            { side: left, rows: events.left, label: "Left" },
            { side: right, rows: events.right, label: "Right" },
          ].map((s, i) => (
            <div key={i} style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 2 }}>
                {s.side.name || <span className="muted">(no name)</span>}
              </div>
              <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                {patternSummary(s.side) || "no pattern recorded"}
              </div>
              <EventTable
                events={s.rows}
                columns={columns}
                truncated={pair?.events_truncated}
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

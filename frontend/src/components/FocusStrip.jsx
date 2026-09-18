/* ============================================================
   FocusStrip — what a reviewer checks for this kind of record
   ------------------------------------------------------------
   The profile says which fields matter for each kind of record
   (D13c). The strip puts those few fields side by side at the top
   of a pair, and narrows the evidence tables to the columns worth
   reading for that kind. Everything else stays one click away.
   Nothing here knows about donations: the labels, the conditions
   and the column keys all come from the profile.
   ============================================================ */

import { Cell, NUMERIC_TYPES } from "./cells";
import { patternSummary } from "./PairEvidence";
import { evidenceFocusFor, pickColumns } from "../evidenceFocus";

// One row of the side-by-side comparison, skipped when neither side has a value.
function FocusRow({ col, left, right }) {
  const a = left?.[col.key];
  const b = right?.[col.key];
  if ((a == null || a === "") && (b == null || b === "")) return null;
  const same = String(a ?? "") === String(b ?? "");
  const numeric = NUMERIC_TYPES.has(col.type);
  return (
    <tr>
      <td className="muted" style={{ whiteSpace: "normal" }}>
        {col.label}
      </td>
      {[a, b].map((v, i) => (
        <td
          key={i}
          className={numeric ? "mono tnum" : ""}
          style={{
            textAlign: numeric ? "right" : "left",
            whiteSpace: "normal",
            overflowWrap: "anywhere",
            background: same ? undefined : "var(--ti-red-50)",
          }}
        >
          <Cell value={v} type={col.type} />
        </td>
      ))}
    </tr>
  );
}

// One side's events, narrowed and ordered by the focus.
export function FocusEvents({ events, columns, emptyNote }) {
  const rows = Array.isArray(events) ? events : [];
  if (columns.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12, margin: 0 }}>
        {emptyNote}
      </p>
    );
  }
  if (rows.length === 0) {
    return (
      <p className="muted" style={{ fontSize: 12, margin: 0 }}>
        No individual rows recorded.
      </p>
    );
  }
  return (
    <div className="tbl-wrap">
      <table className="t" style={{ borderRadius: 0 }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={{ textAlign: NUMERIC_TYPES.has(c.type) ? "right" : "left", whiteSpace: "nowrap" }}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 20).map((e, i) => (
            <tr key={i}>
              {columns.map((c) => {
                const numeric = NUMERIC_TYPES.has(c.type);
                return (
                  <td
                    key={c.key}
                    className={numeric ? "mono tnum" : ""}
                    style={{ textAlign: numeric ? "right" : "left", whiteSpace: "normal" }}
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
  );
}

/* Work out one pair's focus. The two sides can resolve differently, and then the
   left side's focus is used and the difference is said out loud. */
export function focusForPair(left, right, focuses) {
  const a = evidenceFocusFor(left, focuses);
  const b = evidenceFocusFor(right, focuses);
  return { focus: a || b, other: a && b && a.id !== b.id ? b : null };
}

export function FocusStrip({ pair, profile, showAll, onToggle }) {
  const focuses = profile.evidence_focus || [];
  if (focuses.length === 0) return null;

  const left = pair?.left || {};
  const right = pair?.right || {};
  const { focus, other } = focusForPair(left, right, focuses);
  if (!focus) return null;

  const recordCols = pickColumns(focus.record_columns, profile.display_columns);
  const eventCols = pickColumns(focus.event_columns, profile.event_columns);
  const events = pair?.events || {};

  return (
    <div className="card">
      <div className="card-h">
        <h3>What to check for {focus.label ? focus.label.toLowerCase() : "this record"}</h3>
        <div className="actions">
          <button className="btn sm" onClick={onToggle}>
            {showAll ? "Show less" : "Show everything"}
          </button>
        </div>
      </div>

      {other && (
        <div
          style={{
            background: "var(--amber-50)",
            borderTop: "1px solid var(--amber)",
            borderBottom: "1px solid var(--amber)",
            padding: "7px 16px",
            fontSize: 12.5,
          }}
        >
          The two sides are different kinds of record: {focus.label} on the left and {other.label} on
          the right. The left side's fields are shown.
        </div>
      )}

      {recordCols.length > 0 && (
        <div className="tbl-wrap">
          <table className="t" style={{ borderRadius: 0, tableLayout: "fixed" }}>
            <thead>
              <tr>
                <th style={{ width: 180 }}>Field</th>
                <th>{left.name || "Left"}</th>
                <th>{right.name || "Right"}</th>
              </tr>
            </thead>
            <tbody>
              {recordCols.map((c) => (
                <FocusRow key={c.key} col={c} left={left} right={right} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card-b">
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
          {[
            { side: left, rows: events.left },
            { side: right, rows: events.right },
          ].map((s, i) => (
            <div key={i} style={{ minWidth: 0 }}>
              <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                {patternSummary(s.side) || "no pattern recorded"}
              </div>
              <FocusEvents
                events={s.rows}
                columns={eventCols}
                emptyNote={`For ${focus.label ? focus.label.toLowerCase() : "this kind of record"}, the fields above are enough.`}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

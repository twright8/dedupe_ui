/* ============================================================
   RowCap — a long list opens 25 rows at a time
   ------------------------------------------------------------
   One entity can hold 1,191 records, and one exact group 1,144.
   Drawing all of them made a page 32,000 pixels tall and buried
   everything under it, so every expanded list starts at 25 rows
   and grows only when a reader asks.

   It always says where it is — "Showing 25 of 1,191" — and, when
   the API sent only part of the list, that the rest was never
   loaded. A count a reader is shown is never a count of what
   happened to arrive.
   ============================================================ */

import { useState, useEffect } from "react";
import { fmtNumber } from "./ProbBar";

export const ROW_STEP = 25;

/* How many rows to draw, reset whenever the thing being looked at changes.
   `resetKey` is that thing's id — a new array identity every render would
   reset the count on every render instead. */
export function useRowCap(resetKey) {
  const [shown, setShown] = useState(ROW_STEP);
  useEffect(() => {
    setShown(ROW_STEP);
  }, [resetKey]);
  return [shown, setShown];
}

/* The line under a capped list, with the two ways to see more. `loaded` is how
   many rows arrived, `total` how many there really are, and `truncated` says
   the API sent only part of them. */
export function RowCap({ shown, loaded, total, truncated, plural = "rows", setShown }) {
  const real = total ?? loaded;
  const visible = Math.min(shown, loaded);
  const more = loaded - visible;

  if (more <= 0 && !truncated && real <= loaded) return null;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        padding: "8px 2px 0",
      }}
    >
      <span className="muted" style={{ fontSize: 11.5 }}>
        Showing {fmtNumber(visible)} of {fmtNumber(real)} {plural}
        {truncated && real > loaded
          ? `. The first ${fmtNumber(loaded)} were loaded; the rest are in the export.`
          : "."}
      </span>
      {more > 0 && (
        <>
          <button className="btn sm" onClick={() => setShown((n) => n + ROW_STEP)}>
            Show {fmtNumber(Math.min(ROW_STEP, more))} more
          </button>
          <button className="btn sm ghost" onClick={() => setShown(loaded)}>
            Show all {fmtNumber(loaded)}
          </button>
        </>
      )}
      {more <= 0 && visible > ROW_STEP && (
        <button className="btn sm ghost" onClick={() => setShown(ROW_STEP)}>
          Show fewer
        </button>
      )}
    </div>
  );
}

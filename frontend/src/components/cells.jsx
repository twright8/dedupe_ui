/* ============================================================
   Cell — one profile column's value, formatted by its type
   ------------------------------------------------------------
   Shared by the records table and the exact-groups table, so a
   record reads the same wherever it appears. Nothing here knows
   about donations or PSC: the type comes from the profile.
   ============================================================ */

// Types that read as figures: right-aligned and in the mono face.
export const NUMERIC_TYPES = new Set(["money", "number", "year"]);

export function isNumericType(type) {
  return NUMERIC_TYPES.has(type);
}

export function Cell({ value, type }) {
  if (value == null || value === "") {
    return <span className="muted">—</span>;
  }

  if (type === "money") {
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    return "£" + Math.round(n).toLocaleString("en-GB");
  }

  if (type === "number") {
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    return n.toLocaleString("en-GB");
  }

  if (type === "year") {
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    return String(Math.trunc(n));
  }

  if (type === "list") {
    // The backend joins list values with " | ". Show the first, and keep the
    // rest one hover away rather than blowing the row height out.
    const items = String(value)
      .split("|")
      .map((s) => s.trim())
      .filter(Boolean);
    if (items.length === 0) return <span className="muted">—</span>;
    return (
      <span title={items.join(" | ")}>
        {items[0]}
        {items.length > 1 && (
          <span className="tag" style={{ marginLeft: 6 }}>
            +{items.length - 1}
          </span>
        )}
      </span>
    );
  }

  return String(value);
}

/* ============================================================
   Cell — one profile column's value, formatted by its type
   ------------------------------------------------------------
   Shared by the records table and the exact-groups table, so a
   record reads the same wherever it appears. Nothing here knows
   about donations or PSC: the type comes from the profile.
   ============================================================ */

/* Bookkeeping the pipeline needs and a reviewer does not. Every table that
   shows a record or a unit hides these, because each one is either shown
   deliberately somewhere else (unit size, the earlier IDs) or means nothing
   outside the pipeline. Kept in one place so the tables cannot drift apart. */
export const SYSTEM_COLUMNS = new Set([
  "unit_id",
  "unit_size",
  "record_id",
  "track",
  "review_state",
  "held_group_id",
  "existing_entity_id",
  "existing_entity_ids",
  "n_existing_ids",
  "entity_id",
  "cluster_id",
  "proposed_entity_key",
  "is_trust",
]);

/* The donation pattern is spelled out as a sentence beside the evidence, so the
   raw parts of it do not need a column of their own. */
export const PATTERN_COLUMNS = new Set([
  "n_distinct_values",
  "share_round_1000",
  "top_values",
]);

// The columns a reviewer should see, in the profile's own order.
export function readableColumns(columns, displayColumns) {
  const order = new Map((displayColumns || []).map((c, i) => [c.key, i]));
  return (columns || [])
    .filter((c) => !SYSTEM_COLUMNS.has(c.key) && !PATTERN_COLUMNS.has(c.key))
    .sort((a, b) => (order.get(a.key) ?? 999) - (order.get(b.key) ?? 999));
}

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

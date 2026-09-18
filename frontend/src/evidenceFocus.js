/* ============================================================
   Evidence focus — what a reviewer checks, by kind of record
   ------------------------------------------------------------
   The profile declares a list of focuses (D13c). Each one carries
   conditions, the record columns to put side by side, and the
   event columns worth reading. The entries are tried in order and
   the first whose conditions all hold wins, exactly as a track
   rule does. The last entry has no conditions and catches the rest.

   The conditions use only the five operators a browser can settle
   on its own. The semantics are the ones docs/RULESET.md states:
   matching is case-insensitive, and a null value never satisfies a
   condition. Nothing here knows about donations.
   ============================================================ */

function text(value) {
  if (value == null) return null;
  const s = String(value).trim();
  return s === "" ? null : s.toLowerCase();
}

// One condition against one row. A value the row does not carry is null, and a
// null never satisfies anything except is_null.
export function conditionHolds(condition, row) {
  const op = condition?.op;
  const value = text(row?.[condition?.column]);

  if (op === "is_null") return value == null;
  if (value == null) return false;
  if (op === "not_null") return true;
  if (op === "equals") return value === text(condition.value);
  if (op === "in") {
    return (condition.values || []).some((v) => text(v) === value);
  }
  if (op === "starts_with") {
    return (condition.values || []).some((v) => {
      const prefix = text(v);
      return prefix != null && value.startsWith(prefix);
    });
  }
  // An operator this evaluator does not know cannot be judged here, so the
  // focus falls through to the next entry rather than guessing.
  return false;
}

export function conditionsHold(conditions, row) {
  if (!Array.isArray(conditions) || conditions.length === 0) return true;
  return conditions.every((c) => conditionHolds(c, row));
}

// The first focus whose conditions all hold, or null when none does.
export function evidenceFocusFor(row, focuses) {
  if (!row || !Array.isArray(focuses)) return null;
  return focuses.find((f) => conditionsHold(f.when, row)) || null;
}

// The profile's columns, narrowed to the keys a focus names and kept in the
// focus's own order. Keys the profile does not describe are skipped.
export function pickColumns(keys, columns) {
  if (!Array.isArray(keys) || keys.length === 0) return [];
  const byKey = new Map((columns || []).map((c) => [c.key, c]));
  return keys.map((k) => byKey.get(k)).filter(Boolean);
}

/* ============================================================
   Run counts — which stages a run has been through.
   The backend defaults every count to 0, so presence is signalled
   by the explicit hasRecords / hasPairs flags, never by value.
   ============================================================ */

export function hasPairCounts(counts) {
  return !!counts && counts.hasPairs === true;
}

export function hasRecordCounts(counts) {
  return !!counts && counts.hasRecords === true;
}

// Whether the exact-key stage ran and left its numbers behind.
export function hasExactCounts(counts) {
  return !!counts && counts.hasExact === true;
}

// Whether stage 5 has proposed entity IDs for this run.
export function hasEntityCounts(counts) {
  return !!counts && counts.hasEntities === true;
}

// Per-track record count key: "person" -> "recordsPerson".
export function trackCountKey(trackKey) {
  return "records" + trackKey.charAt(0).toUpperCase() + trackKey.slice(1);
}

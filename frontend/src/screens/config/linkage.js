/* ============================================================
   linkage_settings — reading, normalising and building
   ------------------------------------------------------------
   The settings that drive Splink live beside the ruleset in every
   config version. LINKAGE.md is the contract: thresholds and EM
   settings at the top level, and everything else under
   tracks.<track>. Older versions kept one flat set of blocking
   rules and comparisons, so loading a draft converts those into
   the per-track shape.
   ============================================================ */

export const DEFAULT_MAX_PAIRS = 20000000;
export const DEFAULT_EM_ITERATIONS = 20;

// The fixed allow-list from LINKAGE.md, with the name each one goes by in the
// UI and the argument Splink expects for it.
export const COMPARISON_TYPES = [
  { fn: "cl.ExactMatch", label: "Exact match" },
  {
    fn: "cl.JaroWinklerAtThresholds",
    label: "Jaro-Winkler similarity",
    arg: "score_threshold_or_thresholds",
    kind: "score",
  },
  {
    fn: "cl.JaroAtThresholds",
    label: "Jaro similarity",
    arg: "score_threshold_or_thresholds",
    kind: "score",
  },
  {
    fn: "cl.LevenshteinAtThresholds",
    label: "Levenshtein distance",
    arg: "distance_threshold_or_thresholds",
    kind: "distance",
  },
  {
    fn: "cl.DamerauLevenshteinAtThresholds",
    label: "Damerau-Levenshtein distance",
    arg: "distance_threshold_or_thresholds",
    kind: "distance",
  },
  {
    fn: "cl.JaccardAtThresholds",
    label: "Jaccard similarity",
    arg: "score_threshold_or_thresholds",
    kind: "score",
  },
  { fn: "cl.NameComparison", label: "Name comparison", preserveArgs: true },
  {
    fn: "cl.ForenameSurnameComparison",
    label: "Forename and surname comparison",
    preserveArgs: true,
  },
  { fn: "cl.PostcodeComparison", label: "Postcode comparison" },
  {
    fn: "cl.ArrayIntersectAtSizes",
    label: "Array overlap",
    arg: "size_threshold_or_thresholds",
    kind: "size",
  },
];

export function comparisonSpec(fn) {
  return COMPARISON_TYPES.find((c) => c.fn === fn);
}

// What a fresh set of thresholds looks like for each kind of argument.
function defaultThresholds(kind) {
  if (kind === "distance") return [1, 2];
  if (kind === "size") return [1];
  return [0.92, 0.88];
}

// What the arguments list means in plain words, for the line under the boxes.
export function thresholdHelp(kind) {
  if (kind === "distance")
    return "Edit distances to compare at, closest first. One level per value.";
  if (kind === "size") return "How many shared items count as a level, largest first.";
  return "Similarity scores to compare at, highest first. One level per value.";
}

let _seq = 0;
function freshId(prefix) {
  _seq += 1;
  return `${prefix}_new_${_seq}`;
}

/* ---------- blocking rule SQL ---------- */

const EQUALITY = /^\s*l\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*r\.([A-Za-z_][A-Za-z0-9_]*)\s*$/;

// The columns a rule compares, when it is a plain AND of `l.col = r.col`.
// Null means the rule is hand-written SQL that the simple builder cannot hold.
export function columnsFromSql(sql) {
  const text = String(sql || "").trim();
  if (!text) return [];
  const parts = text.split(/\s+AND\s+/i);
  const columns = [];
  for (const part of parts) {
    const m = part.match(EQUALITY);
    if (!m || m[1] !== m[2]) return null;
    columns.push(m[1]);
  }
  return columns;
}

export function sqlFromColumns(columns) {
  return (columns || []).map((c) => `l.${c} = r.${c}`).join(" AND ");
}

/* ---------- normalising ---------- */

function num(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function normalizeBlockingRules(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.map((r, i) =>
    typeof r === "string"
      ? { id: `b${i + 1}`, description: "", sql: r }
      : {
          ...r,
          id: r.id || `b${i + 1}`,
          description: r.description || "",
          sql: String(r.sql || ""),
        }
  );
}

function oneComparison(raw, i, column) {
  const c = raw && typeof raw === "object" ? raw : {};
  const fn = c.splink_function || c.function || "cl.ExactMatch";
  const spec = comparisonSpec(fn);
  const args = c.splink_args && typeof c.splink_args === "object" ? c.splink_args : {};
  let splink_args = {};
  if (spec?.preserveArgs) splink_args = args;
  else if (spec?.arg) {
    const existing = args[spec.arg];
    splink_args = {
      [spec.arg]: Array.isArray(existing)
        ? existing
        : existing != null
          ? [existing]
          : defaultThresholds(spec.kind),
    };
  }
  return {
    id: c.id || `c${i + 1}`,
    column: column || c.column || c.output_column_name || c.feature || "",
    splink_function: fn,
    splink_args,
    term_frequency: !!c.term_frequency,
    description: c.description || "",
  };
}

// Comparisons arrive as a list, or — in older versions — as an object keyed by
// the column they compare.
function normalizeComparisons(raw) {
  if (Array.isArray(raw)) return raw.map((c, i) => oneComparison(c, i, null));
  if (raw && typeof raw === "object")
    return Object.entries(raw).map(([column, c], i) => oneComparison(c, i, column));
  return [];
}

function normalizeTrack(raw) {
  const t = raw && typeof raw === "object" ? raw : {};
  return {
    blocking_rules: normalizeBlockingRules(t.blocking_rules),
    comparisons: normalizeComparisons(t.comparisons),
    em_blocking_rules: (Array.isArray(t.em_blocking_rules) ? t.em_blocking_rules : []).map(String),
    max_pairs: num(t.max_pairs, DEFAULT_MAX_PAIRS),
  };
}

// True when the settings still carry one flat set of rules rather than a set
// per track. The tab says so, because the conversion is not saved until the
// user saves a new version.
export function isLegacyLinkage(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  if (s.tracks && typeof s.tracks === "object") return false;
  return !!(s.blocking_rules || s.comparisons || s.splink_params);
}

export function normalizeLinkage(raw, trackKeys) {
  const s = raw && typeof raw === "object" ? raw : {};
  const keys = trackKeys && trackKeys.length ? trackKeys : ["person", "organisation"];
  const legacy = isLegacyLinkage(s);
  const existing = s.tracks && typeof s.tracks === "object" ? s.tracks : {};

  const tracks = {};
  for (const key of keys) {
    // A flat set becomes every track's starting point; the user then edits each
    // track on its own.
    tracks[key] = normalizeTrack(legacy ? s : existing[key]);
  }

  // Anything else in the object is carried through untouched, minus the flat
  // keys we have just moved into the tracks.
  const { blocking_rules, comparisons, em_blocking_rules, max_pairs, splink_params, ...rest } = s;

  return {
    ...rest,
    tracks,
    em_iterations: num(s.em_iterations, DEFAULT_EM_ITERATIONS),
    probability_two_random_records_match:
      s.probability_two_random_records_match == null
        ? null
        : num(s.probability_two_random_records_match, null),
    match_probability_threshold_candidate: num(
      s.match_probability_threshold_candidate ?? s.threshold_candidate,
      0.05
    ),
    match_probability_threshold_review: num(
      s.match_probability_threshold_review ?? s.threshold_review_lower,
      0.5
    ),
    match_probability_threshold_high: num(
      s.match_probability_threshold_high ?? s.threshold_auto_accept,
      0.92
    ),
  };
}

/* ---------- editing helpers ---------- */

export function newBlockingRule(existingIds) {
  return { id: freshId("b"), description: "", sql: "" };
}

export function newComparison(column) {
  return {
    id: freshId("c"),
    column: column || "",
    splink_function: "cl.ExactMatch",
    splink_args: {},
    term_frequency: false,
    description: "",
  };
}

// Switching comparison type drops the arguments the new type cannot use. The
// two name comparisons keep whatever they already carry, because their options
// are Splink's own and this UI does not edit them.
export function retypeComparison(c, fn) {
  const spec = comparisonSpec(fn);
  const next = {
    id: c.id,
    column: c.column,
    description: c.description,
    term_frequency: !!c.term_frequency,
    splink_function: fn,
    splink_args: {},
  };
  if (spec?.preserveArgs) {
    next.splink_args = c.splink_args && typeof c.splink_args === "object" ? c.splink_args : {};
  } else if (spec?.arg) {
    const existing = c.splink_args?.[spec.arg];
    next.splink_args = {
      [spec.arg]: Array.isArray(existing) && existing.length ? existing : defaultThresholds(spec.kind),
    };
  }
  return next;
}

// The three thresholds must stay in order. Moving one pushes the others rather
// than letting the user save a set that cannot mean anything.
export function orderedThresholds(settings, which, value) {
  const candidate = settings.match_probability_threshold_candidate;
  const review = settings.match_probability_threshold_review;
  const high = settings.match_probability_threshold_high;
  const next = { candidate, review, high, [which]: value };

  if (which === "candidate") {
    next.review = Math.max(next.review, next.candidate);
    next.high = Math.max(next.high, next.review);
  } else if (which === "review") {
    next.candidate = Math.min(next.candidate, next.review);
    next.high = Math.max(next.high, next.review);
  } else {
    next.review = Math.min(next.review, next.high);
    next.candidate = Math.min(next.candidate, next.review);
  }

  return {
    match_probability_threshold_candidate: +next.candidate.toFixed(2),
    match_probability_threshold_review: +next.review.toFixed(2),
    match_probability_threshold_high: +next.high.toFixed(2),
  };
}

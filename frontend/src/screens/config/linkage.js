/* ============================================================
   linkage_settings — reading, normalising and building
   ------------------------------------------------------------
   The settings that drive Splink live beside the rest of every
   config version. LINKAGE.md is the contract: the three decision
   lines and the training settings at the top level, and everything
   else under tracks.<track>. Older versions kept one flat set of
   blocking rules and comparisons, so loading a draft converts those
   into the per-track shape.
   ============================================================ */

export const DEFAULT_MAX_PAIRS = 20000000;
export const DEFAULT_EM_ITERATIONS = 20;

/* ---------- the gate's four limits ----------
   Stage 4 joins the accepted pairs into clusters, then holds back for a person
   the ones that look wrong. These four numbers decide what "wrong" means. The
   first three are the same for every track. The fourth is set per track, one
   column at a time, and a track that names no column is never held back for
   that reason. The defaults are the backend's own (docs/ENTITIES.md). */

export const DEFAULT_CLUSTER_FLOOR = 0.2;
export const DEFAULT_MAX_CLUSTER_UNITS = 200;
export const DEFAULT_MAX_EXISTING_IDS = 1;

// The paths the backend reports a bad gate limit under.
export const GATE_PATHS = [
  "linkage_settings.cluster_floor",
  "linkage_settings.max_cluster_units",
  "linkage_settings.max_existing_ids",
  "linkage_settings.max_distinct_values",
];

// Where the backend reports a bad per-track accept line.
export const ACCEPT_LINE_PATH = `linkage_settings.${"match_probability_threshold_high_by_track"}`;

// Every error or warning about one gate limit, including the ones about a
// single row of it, so the message lands beside the box that caused it.
export function gateErrors(errors, path) {
  const p = String(path);
  return (errors || []).filter((e) => {
    const q = String(e.path || "");
    return q === p || q.startsWith(`${p}.`) || q.startsWith(`${p}[`);
  });
}

export function isGatePath(path) {
  return GATE_PATHS.some((g) => gateErrors([{ path }], g).length > 0);
}

/* One limit: the column or columns counted together, and the count they may
   not pass. Several columns are counted as ONE value, so a cluster showing
   1985-03 and 1985-07 shows two birth dates and not one. */
function oneLimit(entry) {
  const raw = entry.columns == null ? entry.column : entry.columns;
  const columns = Array.isArray(raw) ? raw.map((c) => String(c ?? "")) : [String(raw ?? "")];
  return { columns, count: Number(entry.count) };
}

/* One track's rules, always as a list of clauses, each clause a list of limits.
   A cluster is held when ANY clause holds, and a clause holds when EVERY limit
   in it is over its count. The stored shape allows a bare object for a single
   limit and `{all: [...]}` for a conjunction, so both are read as a clause. */
export function maxDistinctValues(settings, track) {
  const raw = (settings || {}).max_distinct_values;
  if (!raw || typeof raw !== "object") return [];
  const spec = raw[track];
  if (!spec) return [];
  return (Array.isArray(spec) ? spec : [spec])
    .filter((e) => e && typeof e === "object")
    .map((entry) =>
      Array.isArray(entry.all)
        ? entry.all.filter((l) => l && typeof l === "object").map(oneLimit)
        : [oneLimit(entry)]
    )
    .filter((clause) => clause.length > 0);
}

/* One limit in the shape the settings file stores. A limit on one column keeps
   the singular key it has always had, so a version nobody has touched is
   written back byte for byte. */
function storedLimit(limit) {
  const columns = limit.columns || [];
  return columns.length === 1
    ? { column: columns[0], count: limit.count }
    : { columns, count: limit.count };
}

/* Write one track's clauses back. An empty list drops the track, and the last
   track going drops the setting, so a config version never carries an empty
   shell the backend has to guess at. */
export function setMaxDistinctValues(settings, track, clauses) {
  const raw = settings?.max_distinct_values;
  const next = { ...(raw && typeof raw === "object" ? raw : {}) };
  const list = (clauses || []).filter((c) => c && c.length > 0);
  if (list.length === 0) delete next[track];
  else
    next[track] = list.map((clause) =>
      clause.length === 1 ? storedLimit(clause[0]) : { all: clause.map(storedLimit) }
    );
  const out = { ...settings };
  if (Object.keys(next).length === 0) delete out.max_distinct_values;
  else out.max_distinct_values = next;
  return out;
}

/* ---------- the accept line, per track ----------
   One accept line is the default every track starts from. A track that needs
   its own sets it here, because the two tracks do not want the same line:
   measured on the donations sheet the person track wants 0.96 and the
   organisation track 0.92 (docs/LINKAGE.md). */

export const ACCEPT_LINE_BY_TRACK = "match_probability_threshold_high_by_track";

// The accept line of every track named, with the shared line filled in.
export function acceptLines(settings, trackKeys) {
  const s = settings || {};
  const shared = num(s.match_probability_threshold_high, 0.92);
  const raw = s[ACCEPT_LINE_BY_TRACK];
  const own = raw && typeof raw === "object" ? raw : {};
  const out = {};
  for (const key of trackKeys || []) out[key] = num(own[key], shared);
  return out;
}

// Whether this track sets a line of its own rather than reading the shared one.
export function hasOwnAcceptLine(settings, track) {
  const raw = (settings || {})[ACCEPT_LINE_BY_TRACK];
  return !!(raw && typeof raw === "object" && Number.isFinite(Number(raw[track])));
}

/* Move one track's accept line.

   Every track's current line is written down, not just the one that moved, so
   moving one slider can never drag another. Without that, raising the person
   line would raise the shared line, and a track reading the shared line would
   follow it somewhere its owner never chose.

   The shared line is then the highest line any track reads. It is what a
   reader that knows nothing about per-track lines sees, so leaving it at the
   top means such a reader accepts no more than the tracks themselves do. The
   review line still binds: an accept line can never sit below it. */
export function setAcceptLine(settings, track, value, trackKeys) {
  const s = settings || {};
  const review = num(s.match_probability_threshold_review, 0.5);
  const keys = trackKeys && trackKeys.length ? trackKeys : [track];
  const next = { ...acceptLines(s, keys) };
  next[track] = num(value, next[track]);
  for (const key of Object.keys(next)) {
    next[key] = +Math.max(next[key], review).toFixed(2);
  }
  return {
    ...s,
    [ACCEPT_LINE_BY_TRACK]: next,
    match_probability_threshold_high: +Math.max(...Object.values(next)).toFixed(2),
  };
}

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
    fn: "custom.NumericDifferenceAtThresholds",
    label: "Numeric difference",
    arg: "thresholds",
    kind: "gap",
    noTermFrequency: true,
  },
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
  if (kind === "gap") return [0, 1];
  return [0.92, 0.88];
}

// What the arguments list means in plain words, for the line under the boxes.
export function thresholdHelp(kind) {
  if (kind === "gap")
    return "Makes a level for equal, within 1, within 2 and so on, so a large gap gets its own weight. Whole numbers, smallest first.";
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

export function newBlockingRule() {
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
// are Splink's own and this screen does not edit them.
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

// The three decision lines must stay in order: lowest score kept, then review
// line, then accept line. Moving one pushes the others rather than letting the
// user save a set that cannot mean anything.
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

  const out = {
    match_probability_threshold_candidate: +next.candidate.toFixed(2),
    match_probability_threshold_review: +next.review.toFixed(2),
    match_probability_threshold_high: +next.high.toFixed(2),
  };

  // A track with its own accept line is pushed by the review line too, so
  // moving the review line up can never leave a track accepting below it.
  const own = settings[ACCEPT_LINE_BY_TRACK];
  if (own && typeof own === "object") {
    const pushed = {};
    for (const [track, line] of Object.entries(own)) {
      pushed[track] = +Math.max(num(line, out.match_probability_threshold_high),
                                out.match_probability_threshold_review).toFixed(2);
    }
    out[ACCEPT_LINE_BY_TRACK] = pushed;
    out.match_probability_threshold_high = +Math.max(
      out.match_probability_threshold_high, ...Object.values(pushed)
    ).toFixed(2);
  }
  return out;
}

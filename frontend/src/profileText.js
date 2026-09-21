/* ============================================================
   Profile text — the words and summaries a profile supplies
   ------------------------------------------------------------
   One build serves several tools. Anything a reader would call by
   a different name in another tool comes from the profile: what a
   record is called, what its child rows are called, the sentence
   that describes the export, and the name of whatever earlier
   grouping the tool is measured against.

   Every reader here falls back to neutral wording, so a profile
   that has not declared a field yet still reads sensibly.
   ============================================================ */

import { conditionsHold } from "./evidenceFocus.js";

const FALLBACK_NOUNS = {
  record: "record",
  record_plural: "records",
  unit_evidence: "history",
  evidence_row: "row",
  evidence_row_plural: "rows",
};

// One noun, from the profile when it has one.
export function noun(profile, key) {
  const value = profile?.nouns?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : FALLBACK_NOUNS[key];
}

/* What the tool is measured against. Donations has an earlier manual grouping;
   a profile with nothing to compare against returns null, and every chip,
   column and figure about it is then left out. */
export function existingLabelName(profile) {
  const value = profile?.existing_label_name;
  if (value === null) return null;
  return typeof value === "string" && value.trim() ? value.trim() : "earlier manual grouping";
}

// Whether to show anything about that earlier grouping at all. A profile that
// declares none still shows the chips when the data plainly has some.
export function hasExistingLabels(profile, counts) {
  if (existingLabelName(profile) != null) return true;
  const c = counts || {};
  return !!(c.import || c.pairsDecidedByImport || c.import_agrees || c.import_disagrees);
}

// The label of the profile's first priority column, for a sort control.
export function priorityColumn(profile, columns) {
  const key = (profile?.priority_columns || [])[0];
  if (!key) return null;
  const list = columns && columns.length ? columns : profile?.display_columns || [];
  return list.find((c) => c.key === key) || null;
}

/* ---------- the pattern summary ---------- */

const PLACEHOLDER = /\{([A-Za-z0-9_]+)(?::([a-z]+))?\}/g;
const SEPARATOR = " · ";

function formatValue(value, format) {
  const n = Number(value);
  if (format === "money" && Number.isFinite(n)) {
    return "£" + Math.round(n).toLocaleString("en-GB");
  }
  if (format === "percent" && Number.isFinite(n)) return `${Math.round(n * 100)}%`;
  if (format === "number" && Number.isFinite(n)) return n.toLocaleString("en-GB");
  if (format === "year" && Number.isFinite(n)) return String(Math.trunc(n));
  return String(value);
}

/* One segment of a template. A segment naming a value the row does not have is
   dropped whole, so "born {dob_month}/{dob_year}" disappears rather than
   printing "born /". Nothing is evaluated: the only thing a template can do is
   name a column and pick one of four formats. */
function renderSegment(segment, row) {
  let missing = false;
  const out = segment.replace(PLACEHOLDER, (_, key, format) => {
    const value = row?.[key];
    if (value == null || value === "") {
      missing = true;
      return "";
    }
    return formatValue(value, format);
  });
  return missing ? null : out.trim();
}

/* The neutral summary for a profile that declares no template. Everything in it
   comes from the profile: how many child rows, the profile's own priority
   column with its label and type, and the years the rows span. A profile that
   carries none of those gets an empty line rather than invented wording. */
function fallbackSummary(row, profile) {
  if (!row) return "";
  const parts = [];

  const n = row.n_events ?? row.n_donations ?? row.n_companies;
  if (n != null) {
    const word = Number(n) === 1 ? noun(profile, "evidence_row") : noun(profile, "evidence_row_plural");
    parts.push(`${Number(n).toLocaleString("en-GB")} ${word}`);
  }

  const priority = priorityColumn(profile);
  if (priority && row[priority.key] != null && row[priority.key] !== "") {
    parts.push(`${priority.label} ${formatValue(row[priority.key], priority.type)}`);
  }

  if (row.first_year != null && row.last_year != null) {
    parts.push(
      row.first_year === row.last_year ? String(row.first_year) : `${row.first_year}–${row.last_year}`
    );
  }
  return parts.join(SEPARATOR);
}

/* One line describing a unit, built from the profile's own templates. The list
   is tried in order and the first entry whose conditions hold is used, exactly
   as an evidence focus is chosen. */
export function patternSummary(row, profile) {
  const templates = profile?.pattern_summary;
  if (!Array.isArray(templates) || templates.length === 0) {
    return fallbackSummary(row, profile);
  }
  const entry = templates.find((t) => conditionsHold(t.when, row));
  if (!entry || typeof entry.template !== "string") return fallbackSummary(row, profile);

  return entry.template
    .split(SEPARATOR)
    .map((segment) => renderSegment(segment, row))
    .filter(Boolean)
    .join(SEPARATOR);
}

/* What the export contains, for the Publish tab. The fallback says only what is
   true of every profile. */
export function exportDescription(profile) {
  const value = profile?.export_description;
  if (typeof value === "string" && value.trim()) return value.trim();
  return (
    "The input file as it came, with the record ID, the entity ID and how that ID was decided " +
    "added at the end. The Excel file carries a second sheet of retired IDs and a third naming " +
    "the run and its config version. The CSV is the first sheet only."
  );
}

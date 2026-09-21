/* ============================================================
   Glossary — one name per thing
   ------------------------------------------------------------
   This file is the only place a user-visible term is defined. No
   screen invents a word of its own. If a word here changes, it
   changes everywhere at once.

   Source: docs/GLOSSARY.md. The audit behind it is
   docs/TERMINOLOGY_AUDIT.md.

   Three things live here:
     1. TERMS — every concept, its one name, its plural and a
        definition that stands on its own.
     2. The provenance vocabulary — the single ordered answer to
        "How it was decided", with a mapping from every API value.
     3. RETIRED — the names we stopped using, read by
        scripts/check-terms.mjs so the old words cannot come back.

   A term marked `profileNoun` is named by the profile, because the
   right word differs between tools. Shared code never writes
   "donor" or "PSC record": it calls term("record", profile).
   ============================================================ */

import { noun, existingLabelName } from "./profileText.js";

/* ------------------------------------------------------------
   1. The things
   ------------------------------------------------------------ */

export const TERMS = [
  {
    key: "record",
    term: "record",
    plural: "records",
    definition: "One row of the file you loaded.",
    profileNoun: "record",
    retire: ["entry"],
  },
  {
    key: "track",
    term: "track",
    plural: "tracks",
    definition:
      "A group of records matched only against each other. This tool has two: people and organisations.",
    retire: ["partition"],
  },
  {
    key: "cleaningStep",
    term: "cleaning step",
    plural: "cleaning steps",
    definition: "One ordered change to a value. It writes a new column beside the raw one.",
    retire: ["cleaning rule", "standardisation step"],
  },
  {
    key: "derivedColumn",
    term: "derived column",
    plural: "derived columns",
    definition:
      "A new column whose value ordered rules set. It is used to standardise a category the source often gets wrong.",
    retire: ["standardised category", "standard value"],
    seeAlso: ["derivedColumnRule"],
  },
  {
    key: "derivedColumnRule",
    term: "derived column rule",
    plural: "derived column rules",
    definition:
      "One test that sets a derived column's value. The rules are tried in order and the first one that fits wins.",
    seeAlso: ["derivedColumn"],
  },
  {
    key: "trackRule",
    term: "track rule",
    plural: "track rules",
    definition:
      "One test that puts a record on a track. The rules are tried in order and the first one that fits wins.",
    seeAlso: ["track"],
  },
  {
    key: "matchKey",
    term: "match key",
    plural: "match keys",
    definition:
      "A named set of columns. Records holding the same value in every one of them are put together.",
    retire: ["exact key", "deterministic rule"],
    seeAlso: ["exactGroup", "guard"],
  },
  {
    key: "guard",
    term: "guard",
    plural: "guards",
    definition: "A limit on a match key. A set of records that breaks the limit is not merged.",
    retire: ["cap"],
    seeAlso: ["matchKey", "heldGroup"],
  },
  {
    key: "exactGroup",
    term: "exact group",
    plural: "exact groups",
    definition: "A set of records a match key put together.",
    retire: ["merged group", "match-key group"],
    seeAlso: ["matchKey", "heldGroup"],
  },
  {
    key: "heldGroup",
    term: "held group",
    plural: "held groups",
    definition:
      "A set of records a match key would have put together, stopped by a guard, waiting for a person.",
    retire: ["held-by-a-key group", "held for review"],
    seeAlso: ["guard", "exactGroup", "withheldCluster"],
  },
  {
    key: "unit",
    term: "unit",
    plural: "units",
    definition: "What the scorer compares: one exact group, or one record on its own.",
    retire: ["representative"],
    seeAlso: ["exactGroup", "pair"],
  },
  {
    key: "pair",
    term: "pair",
    plural: "pairs",
    definition: "Two units the scorer compared.",
    retire: ["candidate pair", "scored pair"],
    seeAlso: ["unit", "score"],
  },
  {
    key: "score",
    term: "score",
    plural: "scores",
    definition:
      "A number from 0 to 1 for one pair. It reads as the chance the two units are the same thing.",
    retire: ["match probability", "match weight"],
    seeAlso: ["splinkScore", "modelScore", "bucket"],
  },
  {
    key: "splinkScore",
    term: "Splink score",
    plural: "Splink scores",
    definition:
      "The score the unsupervised engine works out from the shape of the data. Splink is the open-source engine that does the work. This name is used only where both scores are shown.",
    retire: ["Splink probability"],
    seeAlso: ["score", "modelScore"],
  },
  {
    key: "modelScore",
    term: "model score",
    plural: "model scores",
    definition:
      "The score the trained model works out from saved labels. This name is used only where both scores are shown.",
    retire: ["GBT score"],
    seeAlso: ["score", "splinkScore", "gradedModel"],
  },
  {
    key: "scorer",
    term: "scorer",
    plural: "scorers",
    definition:
      "Whichever of the two scores a run used: the Splink score, or the model score once a model is in force.",
    seeAlso: ["score", "splinkScore", "modelScore"],
  },
  {
    key: "bucket",
    term: "bucket",
    plural: "buckets",
    definition: "Where a pair lands: Accepted, For review or Rejected.",
    retire: ["band", "auto-accept"],
    seeAlso: ["scoreBucket", "acceptLine", "reviewLine"],
  },
  {
    key: "scoreBucket",
    term: "score bucket",
    plural: "score buckets",
    definition:
      "The bucket the score alone gives, before any veto rule, earlier grouping or label moves it.",
    retire: ["raw bucket"],
    seeAlso: ["bucket"],
  },
  {
    key: "acceptLine",
    term: "accept line",
    plural: "accept lines",
    definition: "The score at or above which a pair is accepted without review.",
    retire: ["auto-accept threshold", "high line"],
    seeAlso: ["reviewLine", "candidateFloor", "bucket"],
  },
  {
    key: "reviewLine",
    term: "review line",
    plural: "review lines",
    definition: "The score below which a pair is rejected without review.",
    retire: ["review floor", "review band"],
    seeAlso: ["acceptLine", "candidateFloor", "bucket"],
  },
  {
    key: "candidateFloor",
    term: "candidate floor",
    plural: "candidate floors",
    definition: "The lowest score kept in the run's files. Anything weaker is thrown away.",
    retire: ["candidate threshold"],
    seeAlso: ["acceptLine", "reviewLine"],
  },
  {
    key: "veto",
    term: "veto rule",
    plural: "veto rules",
    definition:
      "A rule about a pair that stops the tool accepting it, whatever the score says. It sends the pair for review or rejects it, and it says why.",
    retire: ["stopped by a rule", "blocked pair"],
    seeAlso: ["score", "label"],
  },
  {
    key: "blockingRule",
    term: "blocking rule",
    plural: "blocking rules",
    definition:
      "A rule that says which pairs are worth comparing at all. It saves time, and it can hide a match if it is too tight.",
    retire: ["candidate rule"],
  },
  {
    key: "comparison",
    term: "comparison",
    plural: "comparisons",
    definition: "How one column is compared inside a pair.",
    seeAlso: ["comparisonLevel"],
  },
  {
    key: "comparisonLevel",
    term: "comparison level",
    plural: "comparison levels",
    definition:
      "One step of a comparison, from an exact match down to no match at all. Each step moves the score by a different amount.",
    seeAlso: ["comparison"],
  },
  {
    key: "label",
    term: "label",
    plural: "labels",
    definition:
      "One saved answer about one pair: Match or Not a match. It can carry a note and a source link, and it beats every score and every rule.",
    retire: ["verdict", "judgement"],
    seeAlso: ["reviewer", "groupDecision"],
  },
  {
    key: "groupDecision",
    term: "group decision",
    plural: "group decisions",
    definition:
      "One answer about a whole cluster or held group at once. It is saved as one label per pair inside it.",
    retire: ["bulk decision", "cluster decision"],
    seeAlso: ["label", "cluster"],
  },
  {
    key: "reviewer",
    term: "reviewer",
    plural: "reviewers",
    definition: "The person who saved a label.",
    retire: ["labeller"],
    seeAlso: ["label"],
  },
  {
    key: "earlierGrouping",
    term: "earlier grouping",
    plural: "earlier groupings",
    definition: "The grouping the team made before this tool existed.",
    profileNoun: "earlierGrouping",
    retire: ["imported labels", "imported label", "earlier manual work", "existing labels"],
    seeAlso: ["earlierId"],
  },
  {
    key: "earlierId",
    term: "earlier ID",
    plural: "earlier IDs",
    definition: "The ID a record already carried from the earlier grouping.",
    retire: ["existing entity ID", "old ID"],
    seeAlso: ["earlierGrouping", "entityId"],
  },
  {
    key: "cluster",
    term: "cluster",
    plural: "clusters",
    definition: "A set of units joined by accepted pairs. A cluster is a proposal, not a decision.",
    retire: ["component", "candidate entity"],
    seeAlso: ["unit", "entity", "withheldCluster"],
  },
  {
    key: "withheldCluster",
    term: "withheld cluster",
    plural: "withheld clusters",
    definition:
      "A cluster the gate held back for a person, because it conflicts, is too large, may be a chain, or mixes earlier IDs.",
    retire: ["held cluster", "flagged cluster", "withheld group"],
    seeAlso: ["cluster", "heldGroup"],
  },
  {
    key: "entity",
    term: "entity",
    plural: "entities",
    definition:
      "One real person or one real organisation, with one ID that stays the same from run to run.",
    retire: ["proposed entity", "final group"],
    seeAlso: ["entityId", "cluster"],
  },
  {
    key: "entityId",
    term: "entity ID",
    plural: "entity IDs",
    definition: "The ID an entity carries.",
    retire: ["standard ID", "final ID"],
    seeAlso: ["entity", "registry"],
  },
  {
    key: "proposal",
    term: "proposal",
    plural: "proposals",
    definition:
      "What a run produces: one entity ID per record, not yet written anywhere durable.",
    retire: ["draft entities", "candidate IDs"],
    seeAlso: ["publish", "registry"],
  },
  {
    key: "registry",
    term: "registry",
    plural: "registries",
    definition: "The durable store of entity IDs. A run is written into it only when it is published.",
    retire: ["register", "durable register"],
    seeAlso: ["publish", "entityId"],
  },
  {
    key: "publish",
    term: "publish",
    plural: "publishes",
    definition: "Writing a run's proposal into the registry.",
    retire: ["finalise", "adopt"],
    seeAlso: ["registry", "proposal"],
  },
  {
    key: "retiredId",
    term: "retired ID",
    plural: "retired IDs",
    definition: "An entity ID that lost a merge. It still leads to the ID that survived.",
    retire: ["alias"],
    seeAlso: ["survivingId", "entityId"],
  },
  {
    key: "survivingId",
    term: "surviving ID",
    plural: "surviving IDs",
    definition: "The entity ID that a merge kept.",
    retire: ["survivor"],
    seeAlso: ["retiredId"],
  },
  {
    key: "run",
    term: "run",
    plural: "runs",
    definition:
      "One execution of the whole method over one input file, with one config version.",
    retire: ["job"],
    seeAlso: ["configVersion"],
  },
  {
    key: "configVersion",
    term: "config version",
    plural: "config versions",
    definition: "One saved, frozen copy of every rule and setting. A run names the version it used.",
    retire: ["ruleset", "linkage settings"],
    seeAlso: ["run"],
  },
  {
    key: "trainingSet",
    term: "training set",
    plural: "training sets",
    definition: "The labels the model learns from.",
    retire: ["teaching labels", "teaches"],
    seeAlso: ["testSet", "label"],
  },
  {
    key: "testSet",
    term: "test set",
    plural: "test sets",
    definition:
      "The labels held back from training. They are used only to grade the model and to set its lines.",
    retire: ["frozen test set", "held out", "held back for testing", "eval set"],
    seeAlso: ["trainingSet", "gradedModel"],
  },
  {
    key: "gradedModel",
    term: "graded model",
    plural: "graded models",
    definition:
      "A model that has been measured against the test set, and may therefore set the buckets.",
    seeAlso: ["newModel", "testSet", "modelScore"],
  },
  {
    key: "newModel",
    term: "new model",
    plural: "new models",
    definition:
      "A model that has not been measured against the test set. It only re-orders the review queue. It cannot move a pair into a different bucket.",
    retire: ["cold start"],
    seeAlso: ["gradedModel", "testSet"],
  },
  {
    key: "pairPrecision",
    term: "pair precision",
    plural: "pair precisions",
    definition: "Of the pairs this run joins, the share the earlier grouping had already joined.",
    seeAlso: ["pairRecall", "precision"],
  },
  {
    key: "pairRecall",
    term: "pair recall",
    plural: "pair recalls",
    definition: "Of the pairs the earlier grouping joined, the share this run joins too.",
    seeAlso: ["pairPrecision", "recall"],
  },
  {
    key: "precision",
    term: "precision",
    plural: "precisions",
    definition: "Of the things the tool said were the same, the share that were.",
    seeAlso: ["recall"],
  },
  {
    key: "recall",
    term: "recall",
    plural: "recalls",
    definition: "Of the things that were the same, the share the tool found.",
    retire: ["coverage"],
    seeAlso: ["precision"],
  },
  {
    key: "calibration",
    term: "calibration",
    plural: "calibrations",
    definition:
      "Adjusting a model's scores so that a score of 0.9 really does mean nine times in ten.",
    seeAlso: ["modelScore"],
  },
  {
    key: "nameFrequencyTable",
    term: "name-frequency table",
    plural: "name-frequency tables",
    definition:
      "An outside table of how common each UK forename and surname is. It is used so that a rare name counts for more.",
    retire: ["rarity table"],
  },
  {
    key: "consensusColumn",
    term: "consensus column",
    plural: "consensus columns",
    definition:
      "A column whose one value for a whole entity is settled by its records. The records vote and the most common value wins.",
    seeAlso: ["entity"],
  },
  {
    key: "priorityColumn",
    term: "priority column",
    plural: "priority columns",
    definition:
      "The column this tool sorts by when you ask for the biggest cases first. The profile names it.",
  },
  {
    key: "evidenceFocus",
    term: "evidence focus",
    plural: "evidence focuses",
    definition:
      "A short list of the things worth checking for this kind of record, with the columns that answer them.",
  },
  {
    key: "patternSummary",
    term: "pattern summary",
    plural: "pattern summaries",
    definition: "One line describing a unit's own history, so two sides can be compared at a glance.",
  },
  {
    key: "oversizedBlock",
    term: "oversized block",
    plural: "oversized blocks",
    definition:
      "A set of records a blocking rule keeps together that is too big to compare pair by pair. The rule is too loose.",
    seeAlso: ["blockingRule"],
  },
  {
    key: "weakLink",
    term: "weak link",
    plural: "weak links",
    definition:
      "Two units inside one cluster that scored very low against each other. The cluster may be a chain of weak joins rather than one thing.",
    seeAlso: ["cluster", "withheldCluster"],
  },
  {
    key: "mixedEarlierIds",
    term: "mixed earlier IDs",
    plural: "mixed earlier IDs",
    definition: "One cluster joins records that the earlier grouping gave different IDs.",
    retire: ["mixed ids"],
    seeAlso: ["earlierId", "withheldCluster"],
  },
  {
    key: "idClash",
    term: "ID clash",
    plural: "ID clashes",
    definition:
      "Two proposed entities claimed the same earlier ID. One keeps it and the other takes a new ID.",
    retire: ["id collision", "re-minted"],
    seeAlso: ["entityId"],
  },
  {
    key: "percentagePoint",
    term: "percentage point",
    plural: "percentage points",
    definition:
      "The plain difference between two percentages. A move from 90% to 92% is two percentage points.",
    retire: ["pp"],
  },
  {
    key: "auditLog",
    term: "audit log",
    plural: "audit logs",
    definition: "A record of every action this tool took, who took it and when.",
  },
];

export const TERM_BY_KEY = Object.fromEntries(TERMS.map((t) => [t.key, t]));

/* The profile's own noun, where the glossary says the profile supplies one.
   Everything else returns the generic word unchanged. */
function profileWords(entry, profile) {
  if (!entry.profileNoun || !profile) return null;
  if (entry.profileNoun === "record") {
    return { term: noun(profile, "record"), plural: noun(profile, "record_plural") };
  }
  if (entry.profileNoun === "earlierGrouping") {
    const name = existingLabelName(profile);
    if (!name) return null;
    return { term: name, plural: name };
  }
  return null;
}

/* One term, in this profile's words. Returns null for an unknown key so a
   caller can fail loudly in development rather than print nothing. */
export function term(key, profile) {
  const entry = TERM_BY_KEY[key];
  if (!entry) return null;
  const words = profileWords(entry, profile);
  return {
    key: entry.key,
    term: words ? words.term : entry.term,
    plural: words ? words.plural : entry.plural,
    definition: entry.definition,
    seeAlso: entry.seeAlso || [],
  };
}

// The word on its own, for a string that cannot hold an element.
export function termText(key, profile, plural = false) {
  const t = term(key, profile);
  if (!t) return key;
  return plural ? t.plural : t.term;
}

// Anchor on the How it works page, so a definition can be read in full.
export function termAnchor(key) {
  return `term-${key}`;
}

// The terms in the order the How it works page lists them.
export function termsAlphabetical(profile) {
  return TERMS.map((t) => term(t.key, profile))
    .filter(Boolean)
    .sort((a, b) => a.term.localeCompare(b.term, "en-GB", { sensitivity: "base" }));
}

/* ------------------------------------------------------------
   2. How it was decided — one ordered vocabulary
   ------------------------------------------------------------
   One question, one phrase: "How it was decided". Weakest first.
   A later one always beats an earlier one.
   ------------------------------------------------------------ */

export const PROVENANCE_QUESTION = "How it was decided";

export const PROVENANCE = [
  {
    key: "alone",
    order: 1,
    label: "On its own",
    tag: "",
    definition: "Nothing joined this record to any other.",
  },
  {
    key: "matchKey",
    order: 2,
    label: "Match key",
    tag: "blue",
    definition: "A match key found the same values in every one of its columns.",
  },
  {
    key: "score",
    order: 3,
    label: "Score",
    tag: "violet",
    definition: "The score reached the accept line.",
  },
  {
    key: "veto",
    order: 4,
    label: "Veto rule",
    tag: "amber",
    definition: "A veto rule moved this pair, whatever the score said.",
  },
  {
    key: "earlier",
    order: 5,
    label: "Earlier grouping",
    tag: "green",
    definition: "Both sides already carried the same earlier ID.",
  },
  {
    key: "reviewer",
    order: 6,
    label: "Reviewer",
    tag: "ink",
    definition: "A person decided it.",
  },
];

/* Outside the ordered list. A machine-written suggestion is a proposal, not a
   decision, so it never ranks against the six above. */
export const SUGGESTED = {
  key: "suggested",
  order: null,
  label: "Suggested",
  tag: "dashed",
  definition: "A machine wrote this answer. It is a proposal. Check it before you trust it.",
};

export const PROVENANCE_BY_KEY = Object.fromEntries(
  [...PROVENANCE, SUGGESTED].map((p) => [p.key, p])
);

export const PROVENANCE_PRECEDENCE =
  "Read the list from the top down. Anything lower beats anything above it, so a reviewer's " +
  "answer beats the earlier grouping, the earlier grouping beats a veto rule, and a veto rule " +
  "beats the score.";

/* Every API value that answers "how it was decided", mapped to one of the six.
   `detail` is the second line the chip shows when it has one. */
export const PROVENANCE_MAP = {
  // pair.decided_by
  decided_by: {
    score: { key: "score", detail: "Splink score" },
    model: { key: "score", detail: "Model score" },
    veto: { key: "veto" },
    import: { key: "earlier" },
    human: { key: "reviewer" },
    exact_key: { key: "matchKey" },
    single: { key: "alone" },
  },
  // entity.entity_basis
  entity_basis: {
    single: { key: "alone" },
    exact_key: { key: "matchKey" },
    import: { key: "earlier" },
    score: { key: "score" },
    human: { key: "reviewer" },
  },
  // cluster edge.edge_source / edge.source
  edge_source: {
    score: { key: "score" },
    import: { key: "earlier" },
    human: { key: "reviewer" },
    veto: { key: "veto" },
    exact_key: { key: "matchKey" },
  },
  // pair_label.provenance
  provenance: {
    manual: { key: "reviewer", detail: "One pair at a time" },
    bulk_range: { key: "reviewer", detail: "A band of scores at once" },
    bulk_review: { key: "reviewer", detail: "A band of scores at once" },
    cluster_merge: { key: "reviewer", detail: "A whole cluster merged" },
    cluster_split: { key: "reviewer", detail: "A cluster split" },
    import: { key: "earlier" },
    llm: { key: "suggested" },
  },
  // a veto id, or any truthy vetoed_by
  vetoed_by: { "*": { key: "veto" } },
};

/* One API value, turned into the one label. `kind` names which field it came
   from. Returns null when the value is empty or unknown, so a caller can leave
   the chip out rather than print a raw word. */
export function provenanceFor(kind, value, detail) {
  if (value == null || value === "" || value === false) return null;
  const table = PROVENANCE_MAP[kind];
  if (!table) return null;
  const hit = table[String(value).toLowerCase()] || table["*"];
  if (!hit) return null;
  const meta = PROVENANCE_BY_KEY[hit.key];
  if (!meta) return null;
  return { ...meta, detail: detail || hit.detail || null };
}

/* ------------------------------------------------------------
   3. The three questions that are NOT "how it was decided"
   ------------------------------------------------------------
   Each answers something else, so each keeps its own phrase and its
   own words. Mixing them into the list above would say that a
   majority vote on one column is evidence that two records are the
   same thing, which it is not.
   ------------------------------------------------------------ */

export const VALUE_BASIS_QUESTION = "How this value was set";

export const VALUE_BASIS = {
  rule: {
    label: "Derived column rule",
    tag: "blue",
    definition: "A derived column rule set this value. It beats a raw value.",
  },
  majority: {
    label: "Most members",
    tag: "",
    definition: "The most common value among this entity's records.",
  },
  raw: {
    label: "Only member",
    tag: "",
    definition: "One record, so its own value stands.",
  },
  tie: {
    label: "Undecided",
    tag: "violet",
    definition: "Two values were equally common, so each record keeps its own.",
  },
  human: {
    label: "Reviewer",
    tag: "ink",
    definition: "A person set this value by hand.",
  },
};

export const ID_ORIGIN_QUESTION = "Where this ID came from";

export const ID_ORIGIN = {
  new: {
    label: "New",
    tag: "",
    definition: "Nothing in the registry claimed these records, so the tool made a new ID.",
  },
  kept: {
    label: "Kept",
    tag: "green",
    definition: "One registry entity already held these records, so its ID stands.",
  },
  survivor: {
    label: "Survived a merge",
    tag: "blue",
    definition:
      "Several registry entities held these records. This ID survives and the others become retired IDs.",
  },
  minted_after_collision: {
    label: "Re-made after a clash",
    tag: "amber",
    definition: "Two proposed entities claimed the same earlier ID. This one gave way and took a new ID.",
  },
};

export const AGREEMENT_QUESTION = "Against the earlier grouping";

export const AGREEMENT = {
  consistent: {
    label: "Agrees",
    tag: "green",
    definition: "Every record here carried the same earlier ID.",
  },
  conflict: {
    label: "Conflicts",
    tag: "red",
    definition: "The records here carried different earlier IDs.",
  },
  extends: {
    label: "Adds to a group",
    tag: "blue",
    definition: "This adds records the earlier grouping had left out.",
  },
  new: {
    label: "New",
    tag: "",
    definition: "The earlier grouping said nothing about these records.",
  },
};

/* ------------------------------------------------------------
   3b. The seven sets of figures
   ------------------------------------------------------------
   Precision and recall are reported seven ways. Four of the seven
   were named on the threshold panel and explained nowhere, so a
   reader could not tell them apart. One list, read by the panel and
   by How it works.
   ------------------------------------------------------------ */

export const FIGURE_SETS = [
  {
    key: "all",
    label: "Everything the run would publish",
    definition: "Every exact group, plus every pair the run accepted, however it was accepted.",
  },
  {
    key: "score_only",
    label: "The scorer on its own",
    definition:
      "Only the pairs the score accepted. Pairs accepted because both sides already carried the same earlier ID are left out. Use this one when you change a rule.",
  },
  {
    key: "without_vetoes",
    label: "The scorer on its own, with no veto rules",
    definition:
      "The same again, with every veto rule taken out. The gap between the two shows what the veto rules cost in recall and bought in precision.",
  },
  {
    key: "splink_only",
    label: "The Splink score on its own",
    definition: "What the Splink score would have decided on its own, with no model in force.",
  },
  {
    key: "model_only",
    label: "The model score on its own",
    definition: "What the model score would have decided on its own.",
  },
  {
    key: "with_human",
    label: "With your answers applied",
    definition:
      "The same, with every pair answered Match joined up and every pair answered Not a match pulled apart. This is what the run would publish today.",
  },
  {
    key: "exact_only",
    label: "The match keys on their own",
    definition: "Only the match keys, with no scoring at all. It shows how far the plain rules get you.",
  },
];

export const FIGURE_SET_BY_KEY = Object.fromEntries(FIGURE_SETS.map((f) => [f.key, f]));

/* ------------------------------------------------------------
   4. The names we stopped using
   ------------------------------------------------------------
   Read by frontend/scripts/check-terms.mjs, which fails the build
   when one comes back. `files` narrows a check to the screens where
   the word is wrong — "group" is right on the Exact groups tab and
   wrong on the cluster screen.
   ------------------------------------------------------------ */

export const RETIRED = [
  { bad: "auto-accept", use: "Accepted, or the accept line" },
  { bad: "auto-accepted", use: "Accepted" },
  { bad: "auto accept", use: "Accepted, or the accept line" },
  { bad: "match probability", use: "score" },
  { bad: "match weight", use: "score" },
  { bad: "gbt score", use: "model score" },
  { bad: "gbt", use: "model score" },
  { bad: "splink probability", use: "Splink score" },
  { bad: "cold start", use: "new model" },
  { bad: "exact key", use: "match key" },
  { bad: "exact keys", use: "match keys" },
  { bad: "deterministic rule", use: "match key" },
  { bad: "imported label", use: "earlier grouping" },
  { bad: "imported labels", use: "earlier grouping" },
  { bad: "existing labels", use: "earlier grouping" },
  { bad: "earlier manual work", use: "earlier grouping" },
  { bad: "merged group", use: "exact group" },
  { bad: "merged groups", use: "exact groups" },
  { bad: "held for review", use: "held group" },
  { bad: "withheld group", use: "withheld cluster" },
  { bad: "withheld groups", use: "withheld clusters" },
  { bad: "candidate pair", use: "pair" },
  { bad: "candidate pairs", use: "pairs" },
  { bad: "scored pair", use: "pair" },
  { bad: "scored pairs", use: "pairs" },
  { bad: "verdict", use: "answer, or label" },
  { bad: "auto-accept threshold", use: "accept line" },
  { bad: "review floor", use: "review line" },
  { bad: "review band", use: "For review, or the review line" },
  { bad: "candidate threshold", use: "candidate floor" },
  { bad: "frozen test set", use: "test set" },
  { bad: "held back for testing", use: "the test set" },
  { bad: "held out", use: "the test set" },
  { bad: "teaches", use: "training set" },
  { bad: "re-minted", use: "Re-made after a clash" },
  { bad: "survivor", use: "surviving ID" },
  { bad: "alias", use: "retired ID" },
  { bad: "aliases", use: "retired IDs" },
  { bad: "ruleset", use: "config version" },
  { bad: "linkage settings", use: "config version" },
  { bad: "standardisation step", use: "cleaning step" },
  { bad: "cleaning rule", use: "cleaning step" },
  { bad: "cleaning rules", use: "cleaning steps" },
  { bad: "representative", use: "unit" },
  { bad: "human", use: "reviewer, or a person" },
  { bad: "labeller", use: "reviewer" },
  { bad: "coverage", use: "recall" },
  { bad: "auc", use: "leave it out" },
  { bad: "shap", use: "how much each piece of evidence moved the score" },
  { bad: "wilson bound", use: "the cautious end of the range" },
  { bad: "ablation", use: "leave it out" },
  { bad: "monotone", use: "leave it out" },
  { bad: "term frequency", use: "how common the word is" },
  { bad: "ocod", use: "leave it out — this tool has no OCOD data" },
  { bad: "roe", use: "leave it out — this tool has no ROE data" },
  // Wrong only where a cluster is the subject.
  { bad: "group", use: "cluster", files: ["screens/ClusterScreen.jsx"] },
  { bad: "groups", use: "clusters", files: ["screens/ClusterScreen.jsx"] },
  // Database values, never words for a reviewer.
  { bad: "true", use: "Match", props: ["label", "title", "aria-label"] },
  { bad: "false", use: "Not a match", props: ["label", "title", "aria-label"] },
];

/* Tokens the checker must not flag. API field names read as snake_case on
   purpose in a few places, and the glossary itself has to name the old words
   in order to retire them. */
export const CHECK_ALLOW_FILES = ["glossary.js", "scripts/check-terms.mjs"];

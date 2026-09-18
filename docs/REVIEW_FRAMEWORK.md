# Reconciliation & Review — Framework for Audit

**Audience:** a UX/UI reviewer or a researcher with **no prior context** of this tool.
**Purpose:** describe what the tool is trying to achieve, the mental model, the modelling
stages (and what each one *means*), the modes of review, and the principles to audit against —
so the reviewer can judge whether the interface makes the right things **observable and
controllable**.

This is a specification of intent, not a description of the current screens.

---

## 1. The problem, in one paragraph

Two UK datasets need linking. **OCOD** lists ~91,000 UK properties owned by overseas entities,
with the owner's *name as recorded* (messy, free text) but usually no company ID. **ROE** (the
Register of Overseas Entities, part of Companies House) lists those overseas entities with a
clean company number (`OE…`). Linking "this property is owned by entity X" to "entity X has these
beneficial owners" requires matching the OCOD name to the right ROE company. Names rarely match
exactly (typos, punctuation, legal-form abbreviations, accents, word order), so matching is
**probabilistic** and a human confirms the uncertain cases. The output is each OCOD record tagged
with its ROE company number (or left blank) plus an audit trail of who decided what.

---

## 2. The core principle — two planes

Everything in the tool is one of two kinds of thing. Keeping them distinct is the spine of the
design.

- **Plane A — the automatic matcher.** Software proposes matches and gives each a *score*. It is
  fast, consistent, and **always provisional**. Thresholds turn scores into provisional
  decisions (accept / review / reject).
- **Plane B — human labels.** A reviewer's TRUE/FALSE judgement on a specific pair. A label is a
  *fact about the world*, not a setting. **Labels are paramount**: a human FALSE suppresses a
  match even if the score says "accept", and a human TRUE forces one even if the score is low.

**Audit question:** at every point, can the user tell whether they are looking at a *score-driven
provisional decision* or a *human-confirmed fact*? Can they tell when a label is overriding the
score?

---

## 3. The modelling pipeline, stage by stage

Each stage below lists **what it does**, **what the output means**, and **what should be
observable** (what a researcher needs to see to trust it). A reviewer should be able to inspect
each stage's output without reading code.

### Stage 0 — Standardise (clean the names and jurisdictions)
- **Does:** turns raw names into a canonical form so near-identical names compare equal — folds
  accents (`À → A`), normalises legal forms (`LIMITED → LTD`, `S.À R.L. → SARL`), removes
  punctuation, collapses spacing, singularises (`PROPERTIES → PROPERTY`), and maps jurisdiction
  spellings to one canonical value (`VIRGIN ISLANDS, BRITISH → BRITISH VIRGIN ISLANDS`).
- **Means:** two records that are "the same name written differently" become identical *strings*,
  so the easy matches fall out for free and the hard ones are isolated.
- **Observable:** for any pair, the **raw name AND the cleaned name** side by side; a
  standardisation report of which rules changed what. (Worked example: raw
  `BADBY PROPERTIES (MIDDLESBROUGH) S.À R.L.` → clean `BADBY PROPERTY MIDDLESBROUGH SARL`; the
  accent *is* stripped — any residual difference, e.g. `MIDDLESBROUGH` vs `MIDDLESBOROUGH`, is a
  real source typo, not a cleaning failure.)

### Stage 1 — Exact match (deterministic)
- **Does:** joins records whose cleaned name **and** jurisdiction are identical.
- **Means:** these are matches *by construction* — no judgement needed. They are removed from the
  hard pool so the probabilistic model only sees genuinely uncertain cases.
- **Observable:** the count and a sample; the fact that these are auto-accepted and **not** sent
  for review.

### Stage 2 — Probabilistic match (Splink / Fellegi-Sunter)
- **Does:** for every plausible pair (same jurisdiction), combines multiple weak signals (name
  similarity, shared tokens, shared digits, suffix-stripped name) into a single **match
  probability** 0–1, with the weights learned from the data.
- **Means:** "how likely, given the evidence, that these are the same entity." **Caveat the
  reviewer must understand:** this model emits only a *handful of discrete values* (e.g. 0.42,
  0.60, 0.95, 1.0) because the signals are bucketed — so the distribution looks like a few tall
  spikes ("clumps"), and many genuinely-different pairs share an identical score. This is why a
  raw probability is hard to fine-tune on.
- **Observable:** the score distribution (a histogram); for any pair, a **breakdown of which
  signals fired** and how much each moved the score ("why the model scored this").

### Stage 2.5 — Supervised re-score (gradient-boosted model, optional)
- **Does:** once humans have labelled some pairs, a second model (trained on those TRUE **and**
  FALSE labels) re-scores every pair into a **continuous, calibrated** probability.
- **Means:** it separates the pairs that Stage 2 left stuck on the same value, and "0.9" is meant
  to read as "~90% likely a match." **Caveat:** on easily-separable or sparse labels this model
  can become *over-confident*, collapsing scores to two spikes near 0 and 1 (bimodal) — which can
  empty the review band. That behaviour must be **observable**, not silent.
- **Observable:** the before/after distribution (raw vs re-scored); training-set size and class
  balance; held-out accuracy and calibration; **which score the buckets are using** (raw vs
  re-scored).

### Stage 3 — Bucket the scores (the thresholds)
- **Does:** partitions scored pairs by two cutoffs into **auto-accept** (≥ high), **review**
  (between), **drop** (< low). A separate **ambiguous** set flags one OCOD entity with two
  near-tied candidates.
- **Means:** turns continuous scores into a provisional work plan: "trust these, decide these,
  ignore these." The cutoffs are a *policy choice*, not truth — moving them is normal.
- **Observable:** the counts per bucket; where the cutoffs sit **on the distribution**; that
  moving a cutoff is a preview until committed.

### Stage 4 — Apply human labels (the override)
- **Does:** overlays the human labels on top of the bucketed result — TRUE forces a match, FALSE
  removes one, regardless of score/bucket — and produces the final export.
- **Means:** the human always wins. This runs **last**, every time, so labels are never lost to a
  threshold change.
- **Observable:** which rows were changed by a label vs by the model; that a re-run re-applies all
  labels automatically (no re-labelling).

---

## 4. What a score *means* (so the reviewer can read the chart)

- A score is **evidence strength**, not certainty. 0.95 ≠ "definitely"; it means the model's
  combined signals are strong.
- **Splink (Stage 2)** scores are *clumpy* — a few discrete values. Good for ranking, bad for
  fine cuts.
- **Re-scored (Stage 2.5)** scores are *continuous and calibrated* — meant to be read as a
  probability, and tunable. They can also be *over-confident* (bimodal) — see caveat above.
- The reviewer should always be able to see **which score is on screen** and **how it is
  distributed**, including the low end (a chart that hides low-score clumps is an observability
  failure).

---

## 5. The label model (what makes a decision durable)

- **Append-only, provenance-tagged observations.** Each label records who, when, the verdict, and
  its *source* — `manual` (a person), `bulk_range` (a person's bulk action), `llm` (machine
  suggestion), `implied_negative` (the losers when a person picks one candidate). Re-labelling
  supersedes, never destroys — disagreement is preserved.
- **Three roles, kept apart:**
  1. *Operational* — corrects this dataset's export (last writer wins).
  2. *Training* — teaches the re-score model (TRUE **and** FALSE; machine/bulk labels allowed,
     possibly down-weighted).
  3. *Evaluation* — a **frozen, human-only** set the model is **never** trained on, so accuracy
     numbers are honest.
- **Only human-authored labels train.** The model's own auto-accepts are *not* training data
  (training on your own output is circular). The UI should make "this trains / this doesn't"
  legible.
- **Durable across runs and people.** A label is keyed to the entity, not the run — re-running on
  new data re-applies it automatically; the library is shared across reviewers.

---

## 6. Modes of review (what the review surface must let a person do)

The review surface is one **workspace** ("cockpit"). The modes compose — they are not separate
screens. Baseline paradigm: **OpenRefine's reconciliation + faceting + clustering + bulk
operations** (see §7).

- **SEE.** The score distribution as a chart (full range, low end included); per-pair *why it
  scored* (signal breakdown); a link out to Companies House to inspect the candidate; a visual
  same-vs-different of the two names.
- **SLICE (non-destructive facets).** Narrow the working set by band (review / auto-accept /
  exact), jurisdiction, free-text search, **and a score window selected by dragging on the
  chart**. Slicing changes *what you're looking at*, nothing else. The current slice size is
  always shown.
- **PREVIEW thresholds (non-committing).** Move the auto-accept cutoff and *see* how the bands
  would change — counts and colours update live — **without changing anything**. This is for
  "where should the line be?" exploration.
- **AUTO-ACCEPT / AUTO-REJECT by level (committing).** As part of review, set the cutoff where you
  judge best and **commit** it: everything above is accepted, everything below the review floor is
  rejected, the middle stays for review. This re-buckets the run and re-applies labels (labelled
  pairs never move). Distinct from PREVIEW: this one acts.
- **BULK-LABEL a slice.** Take the current slice (filters + chart window) and **stage** it all as
  TRUE or FALSE, then **flip the few exceptions by hand**, then **commit** — so a period's worth
  of pairs is labelled in seconds instead of one click each. Bulk acts on *exactly the slice*,
  nothing outside it.
- **PER-ENTITY (reconcile one record).** Collapse one OCOD entity's multiple candidates into a
  single unit — pick the one correct ROE or "none correct" — with the **margin** to the runner-up
  shown so close calls stand out. (This is OpenRefine reconciliation: one source cell, ranked
  candidates, choose.)
- **OVERRIDE, visibly.** When a human label disagrees with the score band, it is marked as such —
  the human decision is shown winning.

**Audit question:** can a reviewer (a) explore cutoffs without fear of changing data, (b) act on a
filtered slice in bulk, (c) auto-accept/reject by level *as part of review*, and (d) always see
which of these they're doing? Are PREVIEW and COMMIT unmistakably different?

---

## 7. OpenRefine as the baseline

The interaction model we are aiming at is OpenRefine's, adapted to cross-dataset linkage:

| OpenRefine concept | Here |
|---|---|
| **Reconciliation** — match a cell to a candidate from an external source | Match an OCOD entity to a ROE company |
| **Pick best / none** from ranked candidates | Per-entity review with margin |
| **Facets** — slice the data by any dimension | Band / jurisdiction / search / score-window filters |
| **Clustering** — collapse variants of the same value | Group-by-entity; ambiguous/dedup surfacing |
| **Bulk edit on a facet** | Stage TRUE/FALSE on the current slice |
| **Undo/history** | Append-only labels + audit trail |

If a behaviour exists in OpenRefine's reconcile-and-cluster workflow and not here, that is a gap
worth flagging.

---

## 8. Observability principles (the audit checklist)

Every one of these should be true from the **interface alone**, with no tool knowledge:

1. **Every stage's output is inspectable** — raw vs cleaned names, exact vs probabilistic vs
   reviewed, the score breakdown, the buckets.
2. **The score on screen is identified** (raw Splink vs re-scored) and its **full distribution is
   visible**, including low-score clumps.
3. **A score is explainable** for any single pair (which signals fired).
4. **The two planes are distinguishable** — score-band vs human label — and overrides are shown.
5. **Preview vs commit is unmistakable** — looking never changes data; acting says so.
6. **Bulk actions state their scope** — exactly which pairs (the slice) will be affected.
7. **Every decision is attributable** — who, when, what source, and whether it trains the model.
8. **Model behaviour is surfaced** — training size, accuracy on a held-out human set, and
   over-confidence/collapse when it happens.
9. **Nothing silently dropped** — if a bulk/threshold action excludes pairs, that is visible.

---

## 9. The through-line ("in spirit")

A reviewer should be able to: **see** the distribution → **slice** it (facets + chart window) →
**act in bulk** on that slice → **refine** the exceptions by hand → **commit** — moving fluidly
between "explore where the line should be" and "lock in decisions", with the human-vs-machine
distinction visible throughout. Anything that forces these into disconnected screens, hides the
distribution, or lets a bulk action ignore the current slice, breaks the spirit.

---

## 10. Key decisions (the settled positions, condensed)

- Labels are paramount over thresholds; the override runs last on every run.
- Labels are append-only, provenance-tagged, durable across runs and reviewers; disagreement is
  preserved, not overwritten.
- Three label roles; the evaluation set is frozen, human-only, never trained on.
- Only human-authored labels train; auto-accepts never do.
- Matching is layered: clean → exact → probabilistic (candidate + features) → optional supervised
  re-score (the decision score) → bucket → human override. The probabilistic model generates
  candidates and features; the supervised model makes the continuous decision.
- Review is entity-centric where it matters (one OCOD → pick a ROE or none), with the margin to
  the runner-up shown.
- Threshold control and bulk labelling belong **in** the review workflow, operating on the
  current slice — not on a separate tab and not on the whole bucket.

---

## 11. Known issues to audit against (current gaps)

- **Inline distribution chart hides the low end.** It only renders from the review floor upward,
  so when the re-score model goes bimodal (a clump near 0 and a clump near 1) the user sees only
  the top clump. The chart should show the **full** distribution and mark the floor, not slice it.
- **Supervised model over-confidence.** On sparse/separable labels the re-score collapses to two
  spikes and can empty the review band. Surfacing this (and not auto-applying a collapsed model)
  is needed.
- **Raw vs cleaned name not shown together** in the flat list, so a reviewer can wrongly suspect a
  cleaning bug (e.g. an accent) when the real difference is a source typo.
- **"Trains / doesn't" and label provenance** are stored but under-surfaced in the review flow.

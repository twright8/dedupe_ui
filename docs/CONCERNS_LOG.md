# Concerns Log — Review & Reconciliation Audit

Running log of issues found while walking the flows. **We are NOT fixing these as we go** —
we collect, confirm, and fix the batch together at the end. Each entry: status, evidence,
the core rule it touches (from `REVIEW_FRAMEWORK.md` / `RECONCILIATION_DECISIONS.md`), and a
provisional direction (not a commitment).

**Status:** `CONFIRMED` (proven in code/data) · `OBSERVED` (seen in UI, needs root-cause) ·
`TO-TEST` (raised, not yet exercised) · `FIXED`.

_Started 2026-06-04. Source of A/B/C: user's initial concerns._

---

## C — Can't tell Splink from GBT, or whether GBT ran  `CONFIRMED`
The UI gives almost no signal of which model decided a run or where labels were used.
- **Evidence:** `run_2026_06_04a` has `gbt_score` in its parquet **and** a trained model on
  disk, but `linkage_settings.json` has no `gbt_enabled` flag, so Stage 3 bucketed on raw
  Splink (proof: clumpy `match_probability` `[0.409,0.513,0.959,…,1.0]`). Stage list is
  hardcoded with no 2.5 (`RunDetailScreen.jsx:345`, `pipeline_runner.py:195`); GBT apply is
  out-of-band (`/api/model/apply` → `rebucket_run`); only signal is the diagnostics
  histogram header (`RunDetailScreen.jsx:652`). "14 training labels" is a *Splink* number,
  not GBT.
- **Touches:** REVIEW §8.2 (score on screen identified), §8.8 (model behaviour surfaced),
  two-planes spine.
- **Direction:** surface a per-run "decision score: Splink | GBT (vX)" badge on the stage
  strip + each score; show Stage 2.5 as a real stage; label "training labels" by consumer.

## A — No way to see pairs below the review floor (<0.4)  `CONFIRMED`
- **Evidence:** Stage 2 filters output to `>= threshold_review` *before writing the parquet*
  (on-disk min = 0.409). Sub-0.4 pairs are **never retained**; only recoverable by lowering
  the floor and re-running. UI says as much but offers no inspection path.
- **Touches:** REVIEW §8.9 (nothing silently dropped), §4 (see the low end).
- **Direction:** decide whether "below floor" should be retainable/inspectable, or at least
  reported as an explicit dropped-count with a sample.

## B — Review panel: slider vs bulk-accept not clearly delineated  `OBSERVED`
- Slider (= auto-accept *rule*) and drag-on-chart (= *labelling window* for bulk) are
  separate in `ThresholdPanel` with terse help, but the conceptual split (preview vs commit,
  rule vs slice) reportedly reads as confusing; no walkthrough/explanation of the core loop
  ("these 0.7 reviews are all true → drop auto-accept just below 0.7 → confirm → keep
  reviewing"). **To assess live in Flow 2.**
- **Touches:** REVIEW §6 (modes compose), §8.5 (preview vs commit unmistakable).

## D — No "auto-accepted subset only" export  `CONFIRMED`
- Only `merged_dataset.csv` (full) / `/files/all` are offered; Flow 1 ("download just the
  high-confidence merges") requires the user to filter the merged file themselves.
- **Touches:** Flow 1 use case (Quick reconcile).
- **Direction:** add a high-confidence-only export, or a filter on the merged preview.

## E — Evaluation set is empty  `CONFIRMED`
- `held_out = 0` labels. The keystone rule (frozen, human-only eval set) has no data behind
  it → no honest precision/recall anywhere.
- **Touches:** RECONCILIATION §0 keystone, D7, O4.
- **Direction:** surface eval-set size + "designate eval" prominently; warn when 0.

## F — 17 labels have NULL provenance  `CONFIRMED`
- Library = 36 `manual` + 17 `NULL`. NULL set == the "17 applied to this run". Provenance
  tagging is incomplete → attributability gap.
- **Touches:** REVIEW §8.7 (every decision attributable), D7.
- **Direction:** backfill/normalise provenance; reject NULL on write.

## G — 35 of 52 labels didn't resolve onto the run  `OBSERVED`
- `labels_unmatched: 35` vs `labels_applied: 17`. Key drift, or leftover demo/seed labels?
  Needs root-cause (resolver clean-key vs raw-key, or wrong dataset).
- **Touches:** RECONCILIATION D10 (shared resolver), durability across runs.

---

## H — "Re-apply existing labels" checkbox is a dead control  `CONFIRMED`  (Flow 1 / New run)
- Frontend sends `reapply_labels` (`NewRunScreen.jsx:316,563`), but `CreateRunRequest`
  (`runs.py:148`) doesn't declare the field → Pydantic silently drops it. `_run_pipeline`
  applies labels **unconditionally** (D13: "labels always apply"). So unticking it does
  nothing; the control implies you can ship a label-free export but you can't.
- **Touches:** REVIEW §8.5 (controls must mean what they say), D13.
- **Direction:** remove the checkbox, or make a real "raw/label-free export" a separate action.

## I — "Quick mode — skip Phase 2" checkbox is a no-op  `CONFIRMED`  (Flow 1 / New run)
- `quick_mode` is in `CreateRunRequest` (`runs.py:159`) but **never passed** to
  `enqueue_run`/`start_run`/`_run_pipeline` (the body at `runs.py:295-303` ignores it).
  Ticking "skip Phase 2, exact only" still runs full Splink. Directly relevant to Flow 1's
  "quick" path — a user choosing it gets the opposite of what's promised.
- **Touches:** REVIEW §8.5; Flow 1 (Quick reconcile).
- **Direction:** wire quick_mode through to skip Stage 2/2.5, or remove the control.

## J — "Render HTML diagnostics" checkbox is a no-op  `CONFIRMED`  (Flow 1 / New run)
- Same as I: `render_diagnostics` accepted by the model (`runs.py:158`) but never passed on;
  diagnostics always render. Minor, but same dead-control class.
- **Direction:** wire it or remove it.

## C-bis — New-run "Pipeline preview" bakes in the Splink-only mental model  `CONFIRMED`
- The preview (`NewRunScreen.jsx:387-428`) hardcodes the same 5 stages (0/1/2/3/POST, no
  2.5) and labels Phase 2 as *"will train from N labels in library"* — telling the user, at
  the very entry point, that labels train Splink and that's the whole story. No GBT anywhere.
  Reinforces concern C. Also over-promises the POST stage ("N labels will be applied")
  using library total, not the resolvable count (ties to G).
- **Touches:** REVIEW §8.2/§8.8; same as C.

## K — Raw-Splink clumpiness makes the auto-accept threshold a blunt instrument  `CONFIRMED`  (Flow 1)
- run_2026_06_04b's *entire* Phase-2 score distribution is **8 discrete values**:
  `[0.409, 0.655, 0.908, 0.957, 0.991, 0.997, 0.999, 1.0]` — nothing in between. The
  auto-accept line only bites when it **crosses a clump**: sliding it inside a gap (0.96→0.99)
  changes nothing; crossing the 0.908 clump (0.95→0.90) reclassifies **172 pairs at once**.
  You cannot "auto-accept 0.92-and-up". This is exactly the "bad for fine cuts" problem the
  framework cites — and the reason the GBT de-clumper exists. `gbt_score` is present in the
  parquet but unused (see C).
- **Touches:** REVIEW §2.5 (clumpy), §4 (read the chart), and the whole GBT rationale.

## L — Splink scores are not stable run-to-run  `OBSERVED`  (Flow 1)
- Same data, two runs: run_a clumps at `0.513/0.959`; run_b at `0.655/0.908/0.957`. The EM
  re-fit because the library gained one label (14→15 training labels used). So a pair's score
  — and its bucket near a threshold — can move between runs due to labelling elsewhere
  (random_seed is fixed, so this is the label-driven EM re-fit, not RNG). Tuning a threshold
  on raw Splink is chasing a moving target.
- **Touches:** durability/observability; reinforces the case for the calibrated GBT.

## M — The "quick / no-labelling" flow still applies 18 library labels  `CONFIRMED`  (Flow 1)
- Even with no *new* labelling, run_b's `merged_dataset.csv` contains **20 `reviewed_true_review`**
  rows (human overrides) among its merges (`exact 79,999 · probabilistic 574 ·
  reviewed_true_review 20`). Labels always apply (H's dead checkbox can't stop it). It **is**
  honestly tagged via `match_method` (good — observable), but "quick = pure model output" is a
  false mental model.
- **Touches:** labels-paramount rule; consequence of H.

## D — update
- The auto-accepted-only file **does** exist and **is** individually downloadable from the
  **Files tab** (which has a DESCRIPTION column — e.g. `matches_high_confidence.csv` = "All
  high-confidence (exact + probabilistic)"). So D is a *discoverability* issue, not a missing
  file: the only prominent Summary action is **Export merged** = the full 91,217-row file
  (incl. 10,624 blank/unmatched). Severity downgraded to discoverability/labeling.

## B — root-caused into N/O/P/Q  `CONFIRMED`  (Flow 2 / review cockpit)
The capability the user wants ("these 0.9 reviews are all true → drop auto-accept below the
clump → they leave the queue → keep reviewing") **works** — demonstrated live via `/re-bucket`:
committing 0.95→0.90 moved review **197→25** (the 172-pair 0.908 clump flipped to auto-accept),
reversibly, re-applying labels. So concern B is **legibility, not capability**. Broken out:

### N — One histogram carries two different gestures  `CONFIRMED`
- The same chart is both the **rule-setter** (drag the slider handle = auto-accept threshold)
  and the **label-window selector** (drag across the chart body = brush a slice). Header says
  it outright: *"slider = rule · drag the chart to select a labelling window."* Two gestures,
  one widget, two meanings — inherently confusing. `ThresholdPanel.jsx:106-109` (brush) vs
  `:199-209` (slider).
- **Touches:** REVIEW §6, §8.5.

### O — Two adjacent red commit actions, under-differentiated  `CONFIRMED`
- **"Commit thresholds to run"** (`ReviewScreen.jsx:489` → `reBucketRun` → `/re-bucket`) sets a
  **provisional rule**. **"Stage slice TRUE/FALSE"** (`:518` → `createLabelsBatch` →
  `/labels/batch`) writes **durable human labels**. Both are prominent (FALSE is red), ~40px
  apart, under the same chart. The user's "accept these" maps to *either*, with no steering.
- **Touches:** two-planes spine; REVIEW §6 (auto-accept-by-level vs bulk-label-a-slice).

### Q — "Lower auto-accept + commit" accepts by RULE, not as confirmed/labelled matches  `CONFIRMED`
- After committing @0.90, the 172 freed pairs are `match_method=probabilistic` (auto-accepted
  by rule) — **no labels created**, not durable, don't train, re-evaluated every run. The
  user's words *"confirm them as matches in the data"* describe a **label**, but the slider
  path produces a **rule**. The cockpit never makes this consequence explicit at the moment of
  action. This is the two-planes confusion the framework exists to prevent — the crux of B.
- **Touches:** REVIEW §2 (two planes), §5 (labels = facts), keystone.
- **Direction:** at the point of "accept," make the choice explicit — "trust the model above X
  (rule)" vs "record these N as confirmed TRUE (labels, durable, trains)".

### P — No "how to use this" overview  `OBSERVED`
- Per-control micro-copy is good, but nothing states the loop (see→slice→bulk→refine→commit)
  or *when to move the rule vs when to label*. Matches the user's "no explanation on how to
  use it."

**Positives (keep):** preview-vs-commit is well handled (live `auto-accept +N / review +N`,
"no pairs move at this cutoff", the commit explainer); slice scope is explicit ("Slice: 197
pairs · 196 unlabelled · bucket: Review"); persistence is stated; the Diff view (queue +
TRUE/FALSE + notes + J/K nav) is clean and well-liked.

_(No labels were written during this walkthrough; run_b restored to 0.95.)_

## R — Applying the GBT COLLAPSES to bimodal and empties the review band  `CONFIRMED`  (Flow 3) ⚠️ headline
- Applying the trained GBT to run_b re-scored every pair to one of **two values (0.001 / 0.999)**
  (gbt_score distinct = 2; min/median/max 0.001/0.999/0.999) and drove the **review band 197 → 0**
  (auto-accept 315→437, ambiguous 66→28). The intended *de-clumper* produced an *over-confident
  bimodal* that leaves nothing for humans to review. This is precisely REVIEW §11 ("supervised
  model over-confidence … collapses to two spikes … can empty the review band") and §2.5's caveat,
  reproduced live. The panel's "de-clumps so thresholds become meaningful" promise is **inverted**
  here.
- **Touches:** REVIEW §2.5, §11; keystone (empty eval → over-confidence).
- **Direction:** don't let a collapsed/over-confident model be applied silently; detect bimodality
  + empty-review and refuse/flag; require a real eval set before apply.

## S — AUC 1.000 / Brier 0.0000 shown on an empty eval set  `CONFIRMED`  (Flow 3, ties E)
- Metrics: `eval_source=label_split`, `n_labels_eval=0`, AUC **1.000**, Brier **0.0000** — perfect
  scores because there is no held-out eval; it's scoring itself on a tiny separable split. The Model
  panel **does** warn (amber caution banner fires on all of: <100 labels, not held_out, perfect
  metric — `ModelPanel.jsx:64-67`) and the apply button confirms (<100 labels). Good. But AUC 1.000
  is still printed first/large, and the warning doesn't travel past the panel.
- **Touches:** keystone (fake precision); E.

## T — GBT application is per-run, out-of-band, one-way, and invisible outside Diagnostics  `CONFIRMED`  (Flow 3)
- Apply set `gbt_enabled=true` **only in run_b's** linkage_settings; **config v4 has no gbt flag**,
  so a **fresh run reverts to Splink** (you must re-apply GBT per run). Apply goes through
  `/model/apply`→`rebucket_run`, not the run pipeline, so the stage strip never shows it. After
  applying, **only the Diagnostics histogram header** ("GBT score") and score_column reflect it —
  Summary, stage strip, review cockpit, and per-pair scores all look identical to a Splink run.
  There is **no UI "un-apply"**: reverting a run to Splink needs a re-run (or a backend call).
- **Touches:** concern C; REVIEW §8.2/§8.8.

## Flow-3 positives + notes
- **A handled here:** the *Diagnostics* histogram shows the **full 0–1 range** with "below scored
  floor (<0.40)" marked + explainer — so concern A is addressed on this chart but **not** in the
  review cockpit's ThresholdPanel (which slices from the floor up). Inconsistent treatment of the
  low end between the two charts.
- **Good observability:** "Feature contribution (Splink m-values)" chart (name_jw .92, name_core
  .88, tokens .81, digits .74, jurisdiction 1.00); honest GBT explainer; caution banner; apply
  confirm.
- **Stub:** "Confusion (vs prior labels re-applied)" panel is a placeholder ("available in a future
  update").

_New concerns get appended below as we walk the flows._

---

# Fixes landed (verified in the running app)

- **H, I, J — FIXED.** Removed the three dead New-run controls (re-apply labels / quick mode /
  render diagnostics) and the fields from the createRun payload; replaced with an honest note
  ("labels always re-apply and diagnostics always render — both part of every run, not options").
  `NewRunScreen.jsx`. Verified: Vite HMR clean, no dangling refs.
- **C — FIXED (run level).** `RunSummary` now shows a **decision-score badge** — "Decision score:
  Splink (clumpy)" / "GBT (calibrated)" — plus a note ("a GBT is trained but not applied to this
  run" / "no GBT trained" / "⚠ unvalidated — no held-out eval set"). `RunDetailScreen.jsx`.
  Verified live on run_b. _Remaining: per-pair score source labels (ProbBar/DiffHero)._
- **T — PARTIAL.** Stage 2.5 GBT is now a **visible stage** in the strip ("2.5 · SKIP — trained
  but NOT applied — this run decided on Splink" / "applied · decides this run"). Verified live.
  _Remaining: no UI "un-apply"; apply still out-of-band._
- **C-bis — FIXED.** New-run preview copy now reads "trains Splink-EM from N labels (GBT is
  separate, opt-in)" and the POST stage says "resolvable ones", removing the Splink-only/over-promise
  framing.
- **N/O/Q/P — FIXED.** The review cockpit now labels the two actions in plain language and
  separates them: **"Option 1 — set the computer's cutoff (temporary)"** (button "Apply this
  cutoff to the run", note: temporary rule, re-checked every run, doesn't save/teach) vs
  **"Option 2 — confirm these matches yourself (saved, and teaches the model)"** (buttons "Mark
  all TRUE/FALSE", note: kept for good, teaches the model, always wins). Intro states both ways
  up front. `ReviewScreen.jsx`. Verified live. _Removes the rule-vs-fact confusion at the button._
- **R — FIXED (the headline safety fix).** `/api/model/apply` now has a **collapse guard**
  (`model.py`): it applies, measures the review band, and if a previously non-empty band emptied
  (197 → 0), **reverts to Splink and refuses with HTTP 409** + an interpretable message ("collapsed
  to an over-confident, near-bimodal score… nothing changed… add labels + a held-out eval set"),
  unless `force=true`. Verified live against run_b: 409 returned, review stayed 197, score_column
  stayed `match_probability`. A collapsed model can no longer silently empty your review queue.
  _Partly covers Theme 4.2 ("never auto-apply a collapsed model")._
- **D — FIXED.** Run detail now has a plain **"Download matches only"** button (matches_final.csv —
  the rows that got a match, no blanks) next to **"Export all rows (full)"**, each with a plain
  tooltip. `RunDetailScreen.jsx`. Verified: matches_final.csv serves 200 / 34.9 MB.
- **G — FIXED.** Exposed `labelsUnmatched` through `_normalize_counts` (`runs.py`) and the
  Apply-labels stage now reads "18 of your saved answers applied · **35 didn't match this run's
  data** · 53 saved in total". Verified live (158 backend tests green).
- **E / S (root) — ADDRESSED.** The GBT model panel now has a plain **Test set** line — "none set
  aside yet — accuracy above is measured on the model's own training data, so it can't be trusted"
  — with a one-click **"Set aside a test set"** button (`ModelPanel.jsx` → `designateEvalSet`).
  Verified: button renders; designating set aside 53 answers (40 TRUE / 13 FALSE); reverted after
  the check. Combined with the decision badge's "⚠ unvalidated" and the collapse guard (R), the
  fake-precision problem is now visible and actionable.
- **U — FIXED (found & fixed while building the test-set feature).** `designate_eval_set` now
  takes at most **half** of each answer type, so training always keeps an equal-or-larger
  remainder. Verified: 53 labels → designated 26, left 27 for training (was: took all 53). Returns
  `left_for_training`. `model.py`. Full suite green.

---

## 2026-07-03 — Fixes landed (pipeline reconciliation batch)

_Six concurrent changes to the linkage engine; verified against code + the full backend suite
and a clean frontend build. All uncommitted in the working tree at time of writing._

- **A — FIXED (sub-floor pairs retained + counted).** Stage 2 no longer filters to
  `>= threshold_review` before writing the parquet. It predicts down to a low **candidate floor**
  (`match_probability_threshold_candidate`, default 0.05; old configs fall back to the review
  threshold), so weak sub-review pairs are **retained** in `linkage_scored.parquet` (for GBT
  rescoring / inspection) and reported as an explicit `droppedBelowReview` count. The review
  threshold is now purely a Stage-3 bucketing line. `stage_2_probabilistic_link.py`,
  `stage_3_evaluate.py`, `pipeline_runner._dropped_below_review_count`.
- **C remnants — FIXED (per-pair decision provenance).** A `decision_model` column (`splink` /
  `gbt:<N>`) is now stamped through the scored parquet, the match CSVs and `merged_dataset.csv`,
  and exposed on the run payload as `decisionModel` / `decisionModelVersion`. The bare-number
  "which model decided this" gap is closed at the data level, not just the histogram header.
  `stage_3_evaluate.py`, `runs.py`, `RunDetailScreen.jsx`.
- **K / L root cause — REMOVED (labels no longer refit Splink).** Splink is now a purely
  unsupervised candidate generator: `m` is always EM-estimated, labels train **only** the GBT.
  This removes the label-driven EM re-fit that made Splink scores drift run-to-run (L) and it
  fixes a held-out-eval-label leak into the `splink_p1` feature. Clumpiness (K) is still the
  reason the GBT de-clumper exists; the GBT is now the decision layer, in-band. `stage_2_probabilistic_link._estimate_m_with_em`.
- **R — FIXED (guard blind spots closed).** The collapse guard no longer relies on "review band
  emptied to exactly 0": it now also refuses when the calibrated GBT is **near-bimodal**
  (≤3 distinct scores across all pairs — catches a first-ever apply where the Splink baseline
  band was already 0), and when the band shrinks to a **tiny remnant** (< 5% of the pre-apply
  count, not just to 0). `pipeline_runner._collapse_reason` / `_distinct_gbt_scores`.
- **T — FIXED (in-band + versioned + revert).** GBT models are now immutable **versioned**
  snapshots (`models/versions/<N>/`) with an `active.json` activation state. An active model
  **auto-applies in-band during every run** (collapse-guarded fallback to Splink; the run warns,
  never fails), so a fresh run no longer silently reverts to Splink. `POST /api/model/activate|
  deactivate|revert`; `pipeline_runner.apply_active_gbt_bucketing` / `revert_run_to_splink`.
- **G — ROOT-CAUSED & FIXED (raw-first keying).** The 35-unmatched mystery was cleaned-key drift.
  Raw values are now the durable identity: the shared `label_resolver` resolves raw-first, clean
  fallback, used by display, export and GBT training alike. Real-DB migration: **929 label rows,
  0 duplicates, 726/726 active labels resolve.** `label_resolver.py`, `label_applier.py`,
  `match_reader.py`, `gbt_train.py`.
- **E / S — ADDRESSED (honest threshold_metrics).** `gbt_metrics.json` now persists
  `threshold_metrics` — precision/recall at 0.05-step thresholds with **Wilson 95% lower bounds**,
  computed on the **held-out eval only**, with an explicit `unavailable` + reason payload when
  there is no eval set (rather than printing a fake perfect number). Surfaced via `GET /api/model`.
- **feature_mapper stale-module — FIXED (this batch).** The review-panel feature explainer
  hard-coded jurisdiction to 1.0 ("blocking guarantees shared jurisdiction" — false since the
  cross-jurisdiction name_core rule). It now derives a real three-state value (match 1.0 /
  mismatch 0.0 / unknown-neutral 0.5) via `gbt_features._jurisdiction_match`, comparing OCOD
  `jurisdiction_clean` vs a new `roe_jurisdiction_clean` CSV column (raw fallback for old runs).
  `feature_mapper.py`, `stage_3_evaluate.py`, `test_feature_mapper.py`.

### New operational caveats (not regressions — inherent to the new behaviour)

1. **Configs predating the candidate-floor key keep the old drop-below-review behaviour.**
   `match_probability_threshold_candidate` falls back to `threshold_review` when absent, so a
   run on an old saved config version will NOT retain sub-review pairs until a new config
   version is saved with the key present.
2. **The first multi-proprietor run on real data will likely halt on unmapped proprietor-2–4
   jurisdictions.** Stage 0 now fans OCOD out to one row per (title, proprietor); proprietors
   2–4 carry `Country Incorporated (2..4)` values that may not be in `jurisdiction_map.csv`.
   This is the intended **add-to-map-and-rerun** workflow (`UnmappedJurisdictionsError`), not a
   bug — but expect it on the first real multi-proprietor run.
3. **Old GBT model versions keep scoring without `jurisdiction_match`.** The new
   `jurisdiction_match` feature is appended last, so persisted older feature lists still score
   (column-selection compat). To *gain* the feature (and its monotone +1 constraint), a model
   must be **retrained**; old versions keep working, just without it.

# Reconciliation Framework — Decision Record

**Scope:** shared concept/contract for entity reconciliation across
`roe_ui` (OCOD ↔ ROE company linkage) **and** `psc reconcile`
(PSC person/company deduplication). Feature sets differ per project;
the framework, label semantics, and UX contract are shared.

**Status legend:**
`DECIDED` (agreed) · `PROPOSED` (Claude's recommendation, awaiting confirm) · `OPEN` (unresolved)

**Status: implemented in `roe_ui` (backend + frontend), verified by tests.**
The decisions below are now `DECIDED` and built; see §5 for the as-built map and the
one deviation. Port to `psc reconcile` is pending (§6).

_Last updated: 2026-06-03_

---

## 0. The core insight — a "label" does three different jobs

A single label record is secretly serving three masters with conflicting needs.
Conflating them causes silent failures (training on eval data → fake precision;
bulk/LLM labels inflating metrics). Therefore every label carries a **role/provenance**,
and the **evaluation set is a protected, frozen, human-only subset the model never trains on.**

| Role | Purpose | Requirement |
|---|---|---|
| Operational override | Correct *this* dataset's export | Recency, last-writer-wins, applies to current data |
| Training signal | Teach the GBT model | Volume, both classes, provenance-weighted |
| Evaluation | Honest precision/recall | Frozen, human-only, **never trained on** |

`STATUS: DECIDED (built)` — this is the keystone; everything else depends on it.

---

## 1. The five-pillar mental model  `STATUS: DECIDED (built)`

1. **Two planes.** Automatic matcher (Splink → threshold) vs human labels.
   Labels applied *last*, paramount. (Already true; keep inviolable.)
2. **Layered authority per pair:** human label **>** threshold rule.
   The threshold reclassifies only *unlabelled* pairs; never touches a labelled one.
3. **Labels are append-only, provenance-tagged observations** — not mutable facts.
   Current verdict = latest active row; history always retained.
4. **Architecture:** Splink/EM = candidate generator + feature source (`p1`, optional
   supervised-`m` `p2`); **GBT = the decision model** (`p3`), trained on TRUE *and* FALSE,
   output calibrated. EM is never discarded; supervised never wholesale-replaces it.
5. **Only human-authored labels train; the model's own auto-accepts never train**
   (training on own output = self-confirmation bias / label leakage).

---

## 2. Decisions by topic

### D1 — Review unit = entity-centric (many-to-one)  `STATUS: DECIDED (built)`
Review one OCOD entity with its **ranked ROE candidates**, not scattered pairs.
Interaction model = OpenRefine's **reconciliation service** (pick one candidate or "none"),
*not* its clustering (which is within-dataset dedup). Collapses the duplicate-row
clumpiness and structurally enforces "one OCOD entity → at most one ROE."
Underlying pairwise data is retained; only the *grouping/decision unit* changes.

### D2 — Auto-accept = best candidate, guarded by margin  `STATUS: DECIDED (built)`
Auto-accept the top candidate when (a) above the high threshold **and**
(b) the margin to the 2nd candidate is comfortable; thin margin → route to review.
**Dependency:** margins are meaningless on raw Splink scores (frequent exact ties,
e.g. `27A` vs `27B`). Margin-based auto-accept is a **GBT-dependent feature** (needs D5).

### D3 — Threshold slider = live preview + explicit commit  `STATUS: DECIDED (built)`
Keep the instant client-side preview (explore). Add a **commit** action that re-buckets
server-side (re-run stage_3 + re-apply labels; no Splink re-run). Enforce invariant in code:
threshold changes reclassify only *unlabelled* pairs (auto-accept ↔ auto-reject ↔ review);
they can **never** flip a labelled pair.

### D4 — Only human-authored labels train  `STATUS: DECIDED`
Single labels + *reviewed* bulk-range labels are training data.
Threshold auto-accepts are operational defaults only — invisible to training.
Distinction: bulk-label-TRUE = permanent human assertion (locks + trains);
auto-accept = provisional rule, re-evaluated every run.

### D5 — FALSE labels train the model (via GBT)  `STATUS: DECIDED`
Adopt PSC's supervised approach: GBT trained on TRUE **and** FALSE.
Negatives carry the discriminating power (catch Splink's confident false positives).
Kill the current bug where *any* TRUE label switches EM off; EM always runs as
feature/candidate generator, GBT layers on top.

### D6 — Disagreement: append-only supersede  `STATUS: DECIDED (built)`
Replace update-in-place upsert with **insert-new + deactivate-old**.
Yields last-writer-wins for the *current* value AND full history simultaneously.
Schema already has the `active` flag. Conflict-flagging (recent disagreeing verdicts)
becomes a trivial later query.

### D7 — Label provenance + protected eval set  `STATUS: DECIDED (built)`
Add a `provenance` field: `manual` / `bulk_range` / `llm` / `implied_negative` / `import`.
Plus a `role` or `held_out` marker. Evaluation set = frozen, human-only (`manual`) subset,
never used in training. LLM and bulk labels: trainable (possibly down-weighted), **never eval**.

### D8 — Active learning bootstraps from EM scores  `STATUS: DECIDED (built)`
Zero-label cold start: "uncertain" = review band + ties (from existing EM scores).
Later rounds: GBT uncertainty + EM/GBT disagreement region.
Active learning = the *policy for which unlabelled pairs a human sees next*.

### D9 — Bulk range-label = stage → review → commit  `STATUS: DECIDED (built)`
Select a score range on the chart → provisionally stage TRUE/FALSE on all →
human flips the wrong ones → commit writes per-pair labels (`provenance=bulk_range`).
Individual label always overrides (newest-wins, via append-only ordering).
Subsumes today's bucket-based `mark-unlabelled`.

### D10 — Single shared label resolver  `STATUS: DECIDED (built)`
One function resolves a label to data rows (clean key + raw-key fallback),
used by **both** the review-panel display and the export applier — so display and
export can never disagree about whether a pair is labelled. (Fixes the current
display=clean-only vs export=clean+raw mismatch.)

### D11 — Label application is part of the run success contract  `STATUS: DECIDED (built)`
If `apply_labels` throws, the run is failed/flagged — not silently shipped without labels.
(Remove the swallowing try/except.)

### D12 — Verdict vocabulary: TRUE / FALSE / UNCERTAIN  `STATUS: DECIDED (built)`
Validated on write (reject unknown strings). UNCERTAIN: does not change export,
does not train, **but** is a visible state and a top-priority active-learning target.

### D13 — Remove dead `reapply_labels` flag  `STATUS: DECIDED (built)`
Labels always apply. A raw/label-free export, if ever needed, is a separate explicit action.

### D14 — One shared framework, project-specific feature plugins  `STATUS: DECIDED (built)`
`roe_ui` and `psc reconcile` share the framework (candidate-gen → features → GBT →
calibration → active learning → provenance-tagged label store). Person-specific features
(DOB, gender, generational suffix, nationality) are PSC plugins; company-name features
are shared. Define the seam so improvements port both ways.

---

## 3. Open questions  `STATUS: OPEN`

- **O1** — `STATUS: IMPLEMENTED IN CODE.` Resolved *and now enforced in code*: there is no
  supervised-Splink `p2` rung. Splink is a purely unsupervised candidate generator (`m` is
  always EM-estimated; labels never feed m-estimation), and the GBT carries all supervised
  work. This also closed a held-out-eval-label leak into the `splink_p1` feature.
  (`stage_2_probabilistic_link._estimate_m_with_em`, 2026-07-03.)
- **O2** — `STATUS: PRAGMATICALLY ADDRESSED.` Stable OCOD-entity IDs remain future work, but
  the goal — label keys robust against cleaning-rule changes — is now met pragmatically by
  **raw-first keying** in the shared `label_resolver`: raw name/jurisdiction are the durable
  identity, cleaned key is the fallback, so a cleaning-rule edit no longer orphans a label.
  (929 real labels, 726/726 active resolve.) A full entity-ID dedup is still the deeper fix.
- **O3** — ROE-centric review: one ROE claimed by multiple distinct OCOD entities
  (a dedup signal). Needed as a surface, or out of scope for now?
- **O4** — Eval-set governance: target size, who curates it, refresh policy,
  how to keep it representative as data drifts.
- **O5** — How much PSC to port first. Recommended sequence below.

---

## 4. Recommended sequencing (not a commitment)

The keystone is the **label-role/provenance model + GBT re-scoring**; clumpiness,
margins, mark-by-range, and honest tuning all depend on continuous GBT scores.

1. Label semantics: provenance + role + append-only + shared resolver + run contract
   (D4, D6, D7, D10, D11, D12, D13). Foundation; unblocks everything; low risk.
2. GBT re-scoring layer trained on TRUE+FALSE, calibrated (D5, pillar 4). The de-clumper.
3. Entity-centric review + margin-guarded auto-accept (D1, D2) — now meaningful on `p3`.
4. Threshold preview+commit, mark-by-range with staging (D3, D9).
5. Active learning loop + LLM cold-start (D8).

Resolved open questions: **O1** — EM stays a pure feature/candidate generator, supervised
work is carried by the GBT (no separate supervised-Splink `p2` rung). **O3** — ROE-centric
review built now. **O4** — eval set is a frozen, human-only (`provenance='manual'`) held-out
subset, ~200 balanced TRUE/FALSE, designated explicitly and never trained on.

---

## 5. As-built map (`roe_ui`)

**Backend**
- Label store: `db.py` (+`provenance`, `held_out`, `superseded_by`); append-only supersede in
  `routers/labels.py:upsert_label`; verdict vocab TRUE/FALSE/UNCERTAIN; `services/label_resolver.py`
  is the single clean+raw resolver used by both `match_reader` (display) and `label_applier` (export);
  run contract = `pipeline_runner` no longer swallows `apply_labels` errors; dead `reapply_labels` removed.
- GBT layer: `pipeline/gbt_features.py` (name-only portable features), `gbt_proxy.py` (cold-start),
  `gbt_train.py` (TRUE+FALSE + proxy → LightGBM + isotonic calibration), `gbt_model.py` (persistence),
  `stage_2_5_gbt.py` (scores → `gbt_score` column), `gbt_active.py` (active sampler),
  `services/llm_labeler.py` (criteria + import). `stage_3_evaluate.py` buckets on `gbt_score` when
  enabled (swapped into `match_probability`, raw kept as `splink_probability`) and exposes
  `run_stage_3_bucket_only`. `pipeline_runner.rebucket_run` = re-bucket + re-apply labels.
- Endpoints: `routers/model.py` (train/status/apply/active-batch/import + eval-set designate/status);
  `routers/runs.py` (`/matches/by-ocod`, `/matches/by-roe`, `/re-bucket`); `routers/labels.py` (`/batch`).

**Frontend**

> ⚠️ **SUPERSEDED THE SAME DAY (commit `e016483`).** The entity-centric `EntityReviewScreen` /
> `RoeReviewScreen` described below were deliberately **removed**. Commit message: *"the entity-centric
> 'unify/replace' rewrite was an over-correction that dropped working features … Drop the redundant
> EntityReviewScreen/RoeReviewScreen (AmbiguousScreen already does many-to-one picking; ROE is clean CH
> data so a 'dedup ROE' view isn't core)."* **Live review surface = flat `ReviewScreen`** (rich cockpit:
> chart-brush + slice + bulk-stage + threshold-commit) with a **"Group by entity"** toggle, plus
> **`AmbiguousScreen`** for many-to-one tie-breaks. So **D1 (entity-centric as the _default_) is NOT
> as-built** — entity grouping is a one-click toggle, not the default screen. The removed screens remain
> in history at `da856fe` and are recoverable if ever wanted. _(Recorded 2026-06-04.)_

- `screens/EntityReviewScreen.jsx` (new default review; absorbs Ambiguous; margin; persists
  "none correct" as hard-negative FALSEs), `screens/RoeReviewScreen.jsx` (claimants),
  `components/ThresholdTuner.jsx` (preview+commit + mark-by-range staging),
  `components/ModelPanel.jsx` (train/apply/active batch), `LabelsScreen` (provenance + eval columns +
  designate). Routing/nav updated; `/ambiguous` → entity review.

**Deviation from the proposal:** D7 said proxy labels would be written to the store with
`provenance='proxy'`. As built, the cold-start proxy is an **in-memory training augmentation**
(generated per-train in `gbt_proxy.py`), NOT written to the `labels` table — to keep the label store
a record of human/LLM observations only and avoid bloating it with ~tens of thousands of synthetic
rows. Provenance values actually used: `manual`, `bulk_review`, `bulk_range`, `implied_negative`, `llm`.

## 6. Port path into `psc reconcile`  `STATUS: OPEN`

Shared (port these): the label semantics (provenance/held_out/append-only/resolver/run-contract), the
GBT train→score→calibrate→active loop, the proxy cold-start pattern, and the entity/ROE grouping +
review UX. Project-specific (do NOT port): `roe_ui`'s name-only feature set vs PSC's person features
(DOB, gender, nationality, generational suffix) and corporate veto rules — these stay feature plugins
behind the shared `gbt_features` seam. PSC already has most of the ML stack; the main import is the
**label semantics + review UX**, which it currently lacks.

# Remediation Plan — toward a self-explaining, interpretable platform

**Organizing principle.** Every concern we logged (A–T in `CONCERNS_LOG.md`) is, at root, the
*same* failure: the platform takes an action the user can't see, exposes a control whose
consequence isn't stated, draws a chart that hides data, or reports a quality number that isn't
honest. The fix is not twenty unrelated patches — it is to make the platform **explain its own
decisions where those decisions are consumed**, and **state each control's consequence before
the user acts**.

We group A–T into four interpretability themes. Each names the **interpretability principle** it
restores and the **core framework rule** (`REVIEW_FRAMEWORK.md` §) it satisfies. Sequence is at
the end.

> The test for every fix: *could a reviewer with no tool knowledge, looking only at the screen
> in front of them, correctly state what decided this and what their next click will do?*

---

## Theme 1 — A run must explain its own decision basis
**Concerns:** C, C-bis, T (and the visibility half of R/S/E)
**Principle:** *The score on screen is identified; model behaviour is surfaced where decisions
are consumed — not in one buried tab.* (REVIEW §8.2, §8.8; two-planes spine.)

Today a run never tells you, where you actually use it, **which model decided it**. Splink vs
GBT only appears in the Diagnostics histogram header; the stage strip has no Stage 2.5; applying
the GBT is out-of-band and one-way; a re-run silently reverts to Splink (proven: run_c).

**Fixes (ship together):**
1. **Decision-score badge** on the run Summary, the stage strip, the review cockpit, and each
   pair: `Decided by: Splink (clumpy)` or `GBT v3 (calibrated)`, with the eval caveat inline.
2. **Make Stage 2.5 a real stage** (`STAGE_NAMES` + the hardcoded strip): show `GBT —
   trained / scored / applied / not applied` per run.
3. **Label "training labels" by consumer** — the badge currently reads as "labels train Splink";
   say *which* model each label feeds (Splink-EM vs GBT).
4. **Re-run carries intent**: if the previous run was GBT-applied, either carry the flag or tell
   the user "this re-run is Splink unless you re-apply GBT."

## Theme 2 — Every control states its consequence; no dead controls
**Concerns:** N, O, Q (the concern-B cluster), H, I, J, P
**Principle:** *Preview vs commit, and rule vs fact, are unmistakable at the moment of action;
a control that does nothing is removed.* (REVIEW §6, §8.5.)

The New-run screen has **three dead checkboxes** (re-apply labels H, quick mode I, render
diagnostics J). The cockpit conflates two opposite-category actions on one chart (N) behind two
adjacent red buttons (O), and "lower the threshold + commit" produces a *provisional rule*, not
the *durable confirmed match* the user intends (Q).

**Fixes:**
1. **Kill or wire the dead controls** (H/I/J) — trivial, high-honesty. (Quick win.)
2. **Split the cockpit fork** so the choice is explicit at the button:
   - *"Trust the model ≥ X"* → provisional **rule**: re-buckets, re-evaluated every run, **does
     not train**.
   - *"Confirm these N as TRUE"* → durable **labels**: persist across runs, **train the model**,
     paramount.
   Separate the slider (rule) from a distinct "select-and-label" affordance so one chart isn't
   carrying two gestures.
3. **A one-screen "how this works"** describing the loop (see → slice → bulk → refine → commit)
   and *when to move the rule vs when to label* (P).

## Theme 3 — Nothing is silently dropped; the chart shows the whole truth
**Concerns:** A, K, D, G, L
**Principle:** *If data is dropped, excluded, or unresolved, that is visible.* (REVIEW §8.9, §4.)

The cockpit's ThresholdPanel **slices off everything below the review floor** (while the
Diagnostics chart correctly shows the full 0–1 range + a "below scored floor" marker — an
internal inconsistency). Clumpiness makes the threshold silently inert across wide ranges (K).
35 of 53 labels silently fail to resolve onto the run (G). The auto-accept-only export exists but
is undiscoverable (D).

**Fixes:**
1. **One honest distribution chart**, reused in both places: full range, floor marked,
   below-floor count shown (fixes A + the inconsistency).
2. **Surface clumpiness** (K): extend the existing "no pairs move at this cutoff" tag — show that
   the cutoff sits inside a clump and N pairs move together; this is also the standing argument
   for the GBT.
3. **Surface label resolution** (G): "18 of 53 labels applied; 35 don't resolve to this data —
   [why]" with a drill-in (key drift vs different dataset).
4. **Name the export for the job** (D): a prominent "confirmed matches only" download, not just
   "Export merged" (full file incl. blanks).

## Theme 4 — The model's quality claims must be honest  ⚠️ root cause
**Concerns:** E, S, R (and the keystone)
**Principle:** *Quality is measured on data the model never trained on; an over-confident or
collapsed model is never applied silently.* (RECONCILIATION §0 keystone, D7; REVIEW §11.)

This is the **root** of the worst behaviour. The eval set is **empty** (E), so the GBT reports
**AUC 1.0 / Brier 0.0** (S) — and when applied it **collapses to 0.001/0.999 and empties the
review band 197→0** (R). Many downstream problems are symptoms of *too few labels + no honest
eval*.

**Fixes:**
1. **Make designating a held-out eval set a first-class, prompted step** (endpoints already
   exist) — the run/model should refuse to call itself validated without one.
2. **Block or hard-gate Apply** when the eval set is empty *or* the scored distribution is
   bimodal/empties review: "Applying this model would leave 0 pairs for review — it is
   over-confident on N labels. Not recommended." Never auto-apply a collapsed model.
3. **Propagate the caution** the Model panel already computes: wherever a GBT-decided number is
   shown, mark it `⚠ unvalidated` until a real eval set exists. (Honesty that *travels*.)

---

## Sequencing (the batch fix)

| Order | Theme | Why first | Effort |
|---|---|---|---|
| **0** | **4 — honest eval + collapse guard** | Root cause; makes the GBT trustworthy or honestly un-trusted. Until this lands, the GBT is a footgun. | M |
| **1** | **1 — decision-score badge + Stage 2.5 visible** | The user's top question ("what decided this?"); mostly surfacing existing data. | S–M |
| **2** | **2 — kill dead controls (H/I/J), then the rule-vs-label fork** | H/I/J is a same-day honesty win; the fork is the concern-B redesign. | S then M |
| **3** | **3 — one honest chart + label-resolution + export naming** | Rounds out "nothing hidden"; depends on nothing above. | M |

**Same-day quick wins** (high clarity / low effort): remove the 3 dead checkboxes (H/I/J); add
the decision-score badge (Theme 1.1); reuse the full-range Diagnostics chart in the cockpit
(Theme 3.1); show the eval-set-empty warning on the run, not just the Model panel (Theme 4.3).

## Definition of done — the interpretability checklist
A fix is complete when, **from the screen alone**:
1. You can name which model decided the run, and whether its metrics are validated.
2. Every control tells you its consequence (rule vs fact, preview vs commit) before you click,
   and no control is inert.
3. Every chart shows the full distribution; anything dropped/unresolved is counted on screen.
4. A model that would empty the review band, or whose quality is unmeasured, cannot be applied
   without an explicit, informed override.

This is just REVIEW §8 (the audit checklist) made true in the product.

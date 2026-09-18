# GBT vs Splink — the decision, the numbers reconciled, and the most adaptable stack

**Date:** 2026-06-04. Consolidates the two detailed reports
([overall](./gbt-eval-2026-06-04.md), [thresholds](./gbt-thresholds-2026-06-04.md)) into one
decision, and **reconciles the AUC figures that looked inconsistent across the conversation** —
because they *were* quoted from different setups, and that's worth clearing up.

---

## 1. First, untangle the AUC confusion — it's two separate things I conflated

I quoted "GBT beats Splink on AUC" at different label counts at different times. Here is every AUC
number in one place, so you can see why, and what's actually true.

| What was measured | Splink | GBT @200 | GBT @400 | honest verdict |
|---|---:|---:|---:|---|
| AUC — **GBT's raw score** (its true ordering), 12-seed | 0.886 | 0.899 | 0.919 | tie @200, clear win @400 |
| AUC — **GBT after isotonic** calibration, 12-seed | 0.886 | 0.892 | 0.912 | isotonic *ties pairs together* and shaves ~0.01 off |
| **AP** (top-of-list ranking), GBT raw, 12-seed | 0.797 | 0.796 | 0.842 | tie @200, clear win @400 |

**Two things to take away, because they resolve the confusion:**

1. **Calibration does not change AUC at all.** AUC is pure ordering; calibrating a score (Splink *or*
   GBT) only relabels the dial, it never reorders. So **Splink's AUC is 0.886 whether you calibrate it
   or not** — the row is identical. When you said *"ahh it's because you're comparing to calibrated
   Splink"* — that's true for **Brier/ECE** (next section), but **not** for AUC. The AUC numbers
   wobbled because I was sometimes quoting the GBT's *raw* score (0.899) and sometimes its
   *isotonic-calibrated* score (0.892), plus single-seed vs 12-seed noise. Not because of the Splink
   side.

2. **At 200 labels the GBT's AUC edge (~0.006–0.013 over 0.886) is smaller than the seed-to-seed
   noise (±0.012).** That's a **tie**, not a win. My earlier "beats at ~150–200" was reading the
   average line crossing and was an overstatement. The noise-aware truth, confirmed by AP agreeing:
   **GBT ties Splink on ranking at ≤200 labels and clearly beats it at ~300–400.**

So your instinct is right: **aim for ~300 labels.** At 300 the ranking win is real (AUC 0.90–0.91 vs
0.886), the collapse risk is ~0, and the review-workload win is in full effect.

---

## 2. The Brier/ECE story — *this* is the one that changes when Splink is calibrated

| Metric | raw Splink | **calibrated** Splink | GBT @200 | GBT @491 |
|---|---:|---:|---:|---:|
| Brier (lower better) | 0.194 | **0.108** | 0.119 | 0.096 |
| ECE (lower better) | 0.283 | **~0.05** | ~0.06 | ~0.05 |

Raw Splink looks terrible here, and early on I let that flatter the GBT. But **just calibrating Splink
— with zero change to its ordering or any decision — closes almost the whole gap** (Brier 0.194 →
0.108). So once both sides are calibrated, the GBT does **not** clearly win on the honest-number front
until ~400 labels either. The GBT's calibration "win" was mostly "Splink was never calibrated."

**Net of §1 and §2: with both sides calibrated, the GBT wins on ranking and overall quality at ~300–400
labels, not 200.** At 200 it's a tie on everything except the one thing below.

---

## 3. The one thing the GBT wins regardless of labels: auto-reject

This is the result that doesn't depend on calibration or on hitting 300 labels, and it's the real
reason to keep a GBT (full detail in the [thresholds report](./gbt-thresholds-2026-06-04.md)):

- **Splink can auto-reject nothing.** Its lowest score band is still ~6% true matches, so there is no
  line below which the pile is clean. The reviewer must eyeball the entire bottom.
- **The GBT de-clumps the obvious junk down to ~0**, opening an auto-reject zone of ~470–600 pairs and
  cutting the contested "must review" middle roughly in half (e.g. 384 → 185 at a 90% purity bar).

So the GBT's value proposition is precise: **not better ranking at the top, not a better dial than
calibrated Splink — but the ability to clear ~half the pool off the reviewer's desk, which Splink
structurally cannot do.** That's worth a lot when review workload is the bottleneck.

---

## 4. Adaptability — and why it tips the decision to the GBT

You also want the **most adaptable** method — one that transfers to other reconciliation tasks like
`../psc reconcile`. I checked: **`psc reconcile` runs the same recipe** —
`train_gbt_corporate.py` (the GBT this project's features were lifted from) and
`calibrate_gbt.py`, which **also uses `IsotonicRegression`**. So:

- **The supervised GBT re-ranker is already the shared, portable backbone** across roe_ui (companies)
  and psc reconcile (people). You adapt it to a new task by **swapping the feature builder** (psc adds
  DOB / nationality / forename; roe_ui strips those and keeps name-only). That is exactly the
  adaptability you want — one method, task-specific features.
- **"Calibrated Splink" does not transfer as a *method*.** It only re-maps Splink's single score; it
  can't absorb new features or a different candidate generator. It's a roe_ui-specific shortcut, not a
  reusable backbone.
- **The isotonic→Platt fix is a cross-project win.** Both codebases use isotonic and both inherit its
  problems (it ties scores, can collapse, re-clumps the gradient). Switching to a smooth calibrator
  improves *both* and is more robust across different label volumes — i.e. more adaptable.

**So when adaptability is a goal, the decision is clear: build on the GBT re-ranker, not on
calibrated-Splink.** For roe_ui *alone* the lean calibrated-Splink path is nearly as good and simpler;
but as a *method to reuse*, the GBT (with a smooth calibrator) is the right backbone — it's literally
the one you already share.

---

## 5. The best, most adaptable stack

```
clean → exact-match → Splink (candidate generation + features)      ← keep
      → GBT re-ranker        ← the portable backbone; swap features per task (companies / people / …)
      → Platt or beta calibration   ← NOT isotonic; fit on the TRAINING labels (out-of-fold), not the eval set
      → tune accept + reject thresholds per run on the honest dial, to a chosen purity bar
      → human override + collapse guard   ← belt-and-braces backstop
```

with **~300–400 good labels** (Sonnet/human, not a noisy bulk labeller — κ 0.95 vs 0.45), and
**active learning** to reach that with ~half the labels.

**Why each piece (one line each):**
- **GBT re-ranker:** the adaptable, shared backbone; unlocks auto-reject; beats Splink ranking at 300+.
- **Platt/beta not isotonic:** keeps the de-clumped gradient, preserves AUC (isotonic ties cost ~0.01),
  doesn't collapse the review band, calibrates as well or better — and fixes psc reconcile too.
- **Calibrate on training labels:** the app currently fits calibration on the held-out eval set and
  then grades itself on it (in-sample, optimistic: reports 0.106 vs honest 0.122). Fixing this is free
  honesty and frees the eval set for real measurement.
- **~300–400 labels:** below 100 it collapses (33% at N=50) and under-ranks Splink; 300 is the safe,
  stable, clear-win floor.

**"Is 300 all good?"** — Yes: at 300 the collapse rate is ~0, the AUC win is real (0.90–0.91 vs 0.886),
the dial is honest (with Platt), and the review pile is roughly halved. 400 gives a little more margin;
past ~400 is diminishing returns.

---

## 6. How hard is the Platt swap? — small, doable this session

The calibration is stored as a simple lookup grid (`x_grid → y_grid`) and applied by interpolation
(`gbt_model.apply_calibration`). **Nothing about storage or the apply path needs to change** — only
*how the grid is produced* inside `gbt_train._fit_calibration`. Today:

```python
# now (isotonic — a staircase):
iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, y)
cal = iso.predict(grid)
```
becomes:
```python
# Platt (one smooth logistic curve), sampled onto the same grid:
from sklearn.linear_model import LogisticRegression
lr  = LogisticRegression().fit(raw.reshape(-1, 1), y)
cal = lr.predict_proba(grid.reshape(-1, 1))[:, 1]
```

That's a **~4-line change in one function** — the booster, the feature code, the score column, the
`save_calibration` grid, and `apply_calibration` are all untouched. (Beta calibration is the same idea
with two features, `[ln p, ln(1-p)]`, if you want a touch more flexibility.) Two companion tweaks are
slightly larger but optional: **(a)** fit the calibration on the *training* labels (a few lines where
`train()` picks `calib_X`), and **(b)** drop the apply-time collapse guard's sensitivity once Platt is
in (it rarely triggers). **All doable in this session.**

**UI wording to revisit** (the frontend talks about the score in a few places — needs a quick sweep,
not a rewrite):
- Any copy that warns the calibrated score "may collapse / go bimodal" — with Platt that's largely
  designed out, so the warning should be softened, not removed.
- The model panel's "held-out accuracy / calibrated Brier" label — should say it's measured on the
  training split (once §5's honesty fix lands), not imply a separate held-out grade.
- The diagnostics header that names the score ("GBT score" vs "Splink probability") stays correct;
  "calibrated probability" wording stays valid (Platt is still a calibration).

I can implement the Platt swap on a branch and re-run this experiment's harness to confirm the numbers
hold (it already shows GBT+Platt beating GBT+isotonic), then do the UI wording sweep — just say go.

---

## 7. Honest caveats (unchanged)

LLM gold labels (~2% Sonnet self-noise); gold enriched for hard/ambiguous pairs so figures are
conservative; one frozen Splink run; active learning is a pool simulation; numbers are this dataset —
the *method* recommendation (GBT backbone + smooth calibration + 300–400 labels) is what's meant to
travel to psc reconcile and beyond.

# Matching OCOD to ROE — what we tested, what we changed, and why

**A plain-English methodology.** Date: 2026-06-05.
This paper supersedes the three working reports from this investigation
([gbt-eval](./gbt-eval-2026-06-04.md), [gbt-thresholds](./gbt-thresholds-2026-06-04.md),
[gbt-decision](./gbt-decision-2026-06-04.md)) — keep them as the dated lab notebook; this is the
consolidated story. No maths required; technical terms are explained where they first appear.

---

## In one minute

We set out to answer a single question — *does the trained "GBT" re-scorer actually improve matching,
or is it theatre?* — and measured it honestly against a frozen set of human-quality answers. The
short verdict:

- **It helps, but not where you'd assume.** The trained model's *ranking* (which pairs are more
  likely matches) only clearly beats the simple matcher once you've given it a few hundred examples,
  and even then by a modest amount. Its real, unique value is at the **bottom**: it can confidently
  **throw out the obvious non-matches**, which the simple matcher structurally cannot do. That alone
  roughly halves the human's review pile.
- **The number it produces is now trustworthy.** The simple matcher's "0.42" actually meant ~6%
  likely; the trained model's score, after we fixed the calibration step, reads as a real probability.
- On the back of those findings we made five concrete changes to the system (all committed): a better
  calibration method, a training-data bug fix, two new matching clues (one a big win, one a small
  safety net), a clearer way to manage answers, and **auto-accept / auto-reject lines that the system
  now derives from each model's own test data** instead of using fixed numbers that don't fit.

---

## 1. The problem

Two UK datasets need joining. **OCOD** lists ~91,000 properties owned by overseas companies, with the
owner's name *as written* — messy, and usually with no ID number. **ROE** (the Register of Overseas
Entities) lists those companies with a clean name and an ID (the "OE number"). The job is to attach
the right OE number to each property. Names rarely match exactly (typos, "Limited" vs "Ltd", accents,
word order), so it's part automatic, part human judgement.

The pipeline already worked like this: **clean the names → match the obvious exact ones (about
three-quarters, for free) → score the tricky leftovers with a probabilistic matcher (Splink) → 
optionally re-score with a trained model (the "GBT") → split into accept / review / reject by two
cut-offs → a human's saved yes/no answers override everything.** Our question was about that *optional
trained model*: is it worth it?

---

## 2. The two ways a score can be "good"

This distinction runs through everything, so it's worth 30 seconds. A matching score can be good in two
*independent* ways:

1. **Order** — does it rank true matches above non-matches? If you only ever sort candidates and pick
   the best, this is all you need. The standard measure is **AUC** (0.5 = coin-flip, 1.0 = perfect
   order). AUC is blind to the actual numbers — it only cares about which is higher.
2. **Honesty of the number** — does "0.7" actually mean "70% likely"? This is **calibration**,
   measured by **ECE** (the average gap between the claimed probability and reality; lower is better).
   It matters the moment you draw a fixed line ("auto-accept everything above 0.7").

The simple matcher (Splink) turned out to be **good at #1, terrible at #2**: it ranks well (AUC 0.886)
but its "0.42" really means about 6% likely. That mismatch is the whole reason a calibration step exists.

---

## 3. How we tested it (so the numbers can be trusted)

- **Real data, frozen.** We used a real OCOD↔ROE run (~91k properties) and held the simple-matcher
  output fixed, so the *only* thing changing in our experiments was the trained model.
- **A "gold" set of answers.** We took ~230 hard pairs — deliberately loaded with the ambiguous,
  look-alike cases — and had a strong LLM (Claude Sonnet) judge each one TRUE/FALSE from the names
  alone. We **froze** these and never let any model train on them: they're the honest report card.
  (We call answers the model is *graded on but never learns from* the **Test** set; the rest are the
  **Teaching** set.)
- **A training pool** of ~500 more pairs, kept entirely separate, labelled the same way.
- **A label budget sweep.** We trained the model on 25, 50, 100, … up to ~500 answers and measured, on
  the frozen gold set: AUC (order), Brier and ECE (honesty), and the *shape* of the score
  distribution. Everything was repeated over many random draws so we report averages, not lucky runs.

> **A guard against fooling ourselves:** a perfect score (AUC 1.0) would be a red flag, not a triumph —
> it would mean the test was too easy. Ours never were; the gold set is genuinely hard.

---

## 4. What we found

**Q1 — Does it help overall?** Two different answers depending on what you measure:
- **Honesty of the number: a big, cheap win.** Calibration error (ECE) dropped from **0.28 to ~0.05**,
  and the overall-quality score (Brier) roughly halved (0.19 → ~0.11) — from as few as 25 answers.
- **Order (AUC): a modest, label-hungry win.** The simple matcher already ranks at 0.886. The trained
  model ties it at ~150–200 answers and pulls clearly ahead (to ~0.91–0.94) only at ~300–500.
- **Honest twist:** much of the calibration win is simply "the simple matcher was never calibrated" —
  *calibrating Splink directly* gets most of the same number-honesty with no trained model at all. So
  the trained model has to justify itself on order and on the next point.

**Q2 — Is the gradient trustworthy?** Yes on direction, with a catch. Higher score does reliably mean
more likely a match (a usable, monotone gradient). The *raw* trained score genuinely de-clumps the
simple matcher's handful of values into a smooth spread. **But** the calibration step we inherited
(isotonic) could squash it back into a few steps and push everything to 0 or 1 — a "bimodal collapse"
that empties the review queue and silently auto-decides the uncertain cases. We saw it happen on
sparse answers. (We fixed this — §5.1.)

**Q3 — How many answers do you need?** A clear map:
- **Danger zone: under ~100.** The model can rank *worse* than the simple matcher and collapses on up
  to a third of label draws. At 0 answers it's useless (it scores everything ~0).
- **Golden point: ~200–300.** Stable, no collapse, clearly ahead.
- Active-learning (labelling the most informative pairs) reaches the same point with about half the
  answers.

**The reviewer's real prize — the two lines.** Reframed as "where do the auto-accept and auto-reject
lines go, and how wide is the contested middle a human must read?":
- The **simple matcher cannot auto-reject anything** — even its lowest scores are ~6% true matches, so
  there's no clean "no" line. The reviewer is forced to eyeball the whole bottom.
- The **trained model unlocks the auto-reject line** — it pushes obvious junk to ~0, so you can safely
  bin **~500 of ~1,080** pairs (verified ~95%+ correct). That, not a better "yes" line, is its gift.
- The **auto-accept side stays hard**: the very top is genuinely muddy (real matches and convincing
  look-alikes sit together), so you cannot safely auto-accept most matches by score alone — those need
  a human (or better clues).

**Label noise (the weak link).** The gold answers are LLM-made, so we checked them. The strong model
agreed with *itself* 98% of the time (very reliable); a cheap model (Haiku) agreed with it only 74%
(Cohen's κ 0.45 — much noisier). So we standardised on the strong model and treat the gold set's ~2%
self-noise as the floor on our precision — small relative to the effects we measured.

---

## 5. What we changed as a result

Findings are only worth acting on. Five changes, all committed and tested (158 backend tests pass):

### 5.1 Calibrate with a smooth curve (Platt), not a staircase (isotonic)

The calibration step turns the raw score into an honest probability. The old method (**isotonic**) is a
flexible staircase; on few answers it flattens the score into wide steps, which (a) ties pairs together
and *drops* the ranking, (b) re-clumps the de-clumped score, and (c) causes the bimodal collapse. We
switched to **Platt scaling** — fitting one smooth S-curve. Measured effect: distinct score values
70 → 162, the over-confident "everything at 0 or 1" mass 50% → 0%, the review queue no longer collapses,
the ranking is preserved, and — importantly — we now **fit the calibration on the training answers and
grade on the held-out set**, fixing a quiet bug where the model graded its own calibration on the same
answers (making it look better than it was). *(Nuance: isotonic is still the right choice for the
simple matcher's clumpy score; Platt is right for the trained model's smooth one.)*

### 5.2 A training-data bug: "duplicate company" answers were poisoning the model

When you pick one company as the match for a property, the tool auto-marks the *other* candidates as
"not a match". Usually fine — but Companies House sometimes lists the **same company twice** under two
OE numbers. So the tool was generating answers that said *"GISBURN TRADING LTD is **not** GISBURN
TRADING LTD"* — teaching the model that identical strings mean "not a match". We now **drop these
auto-negatives from training whenever the rejected name's core is identical to the chosen one**, while
keeping the genuinely-useful ones (e.g. "BRINDLEY 5" vs "BRINDLEY 3").

### 5.3 Two new clues — measured, not assumed

We added matching clues and **tested each against the standard metrics, keeping only what helped**:
- **Rare-word weighting (IDF) — kept, the real win.** Two names sharing "MERIDIAN" is far stronger
  evidence than sharing "HOLDINGS", but every shared word used to count the same. Weighting rare words
  more lifted AUC by **+0.008 to +0.011** at 200–500 answers — enough to clearly beat the simple
  matcher at *fewer* answers. (It costs a little calibration, recoverable separately; we judged the
  ranking gain worth it given that's the priority.)
- **Unit/ordinal mismatch — kept, a cheap safety net.** A flag for names identical *except* a unit:
  "27A" vs "27B", "FUND II" vs "III" — the non-numeric cases the existing digit clue misses. It's
  pinned so a mismatch can only ever *lower* a score. On this dataset it touches only 4 pairs (so it
  doesn't move the headline numbers) but it's correct, free, and will matter more on other data.
- **Dropped after measuring:** the simple matcher's internal sub-scores (≈ no gain), and **addresses**
  — we checked, and a property-owner's address matches the company's registered office only ~16% of the
  time (the registered agent is usually a different place), with almost no power to separate true from
  false. Not worth building.

### 5.4 Clearer answers — Teaching vs Testing

The answer-management screen conflated three different things under fuzzy words ("gold", "freeze",
"held-out"). We separated them: every answer has a **verdict** (match / no), a **source** (you /
LLM-suggested / auto), and a **role** — **Teaches** the model or **Tests** it. The screen now lets you
filter by role and source, flip any answer between Teaches/Tests, move a whole filtered batch at once,
and reads in plain English what each button does and *why* a Test set exists (you can't grade a student
on the questions they revised).

### 5.5 Auto-accept / auto-reject lines the system derives itself

The old cut-offs (0.7 accept, 0.4 reject) live on the *simple matcher's* scale and are wrong for the
trained model's. But a *fixed* trained-model pair doesn't work either, because the calibrated score's
range shifts with the model (a 200-answer model tops out around 0.8). So when the trained model is
applied, the system now **computes the lines from the model's own Test set** — auto-reject below the
highest score where ≥90% of those are confirmed non-matches, auto-accept above the lowest score where
≥90% are matches (a "conformal" guarantee, verified on data not used to pick the line). On our model
that yields ~15 auto-accept / ~560 review / ~500 auto-reject — the honest split: aggressive, safe
rejection; conservative acceptance; the muddy middle to a human. *(The simple matcher's own emit/bucket
floor is untouched, so plain runs are unaffected.)*

---

## 6. Where this leaves us — the recommendation

**Ship the trained model — its job is to clear the junk, and it does that with a guarantee.** Concretely:

| Decision | Answer |
|---|---|
| Is it worth it? | Yes, mainly for auto-**reject** (clears ~half the pile) + an honest probability. |
| How many answers? | ~200 to be safe, ~300–400 for a clear, stable win; under ~100 is the danger zone. |
| Calibration | Platt (smooth), fit on training answers, graded on the Test set. |
| Thresholds | Derived per-model from the Test set, not hardcoded. |
| Answer quality | Use the strong labeller (κ 0.95), not the cheap one (κ 0.45). |
| Auto-accept | Keep it conservative — the top is genuinely muddy; let humans handle it. |

**It travels.** The trained-model recipe is shared with the sister tool **PSC reconcile** (which
matches people); the calibration fix and the "derive thresholds from data" approach apply there too —
you adapt by swapping the clues, not the engine.

---

## 7. Honest caveats

- The gold answers are **LLM-made, not human-adjudicated** — ~2% self-noise plus any systematic bias.
- The gold set is **deliberately hard** (enriched for look-alikes), so these are conservative numbers;
  three-quarters of real matches are solved by exact-name matching and never reach the trained model.
- **One real run, one configuration.** And the IDF ranking gain trades a little calibration — worth
  watching as label volume grows.
- The mid-session loss of the on-disk run artifacts (a separate housekeeping issue) didn't affect any
  result here — all experiments used an isolated copy, and the label store is intact.

---

## 8. Parked ideas (measured-but-not-built, or future)

- **Label the high-scored-but-wrong look-alikes** as you review — the single best way to clean the muddy
  auto-accept top (better than any feature).
- **ROE de-duplication** — the real cure for the duplicate-company problem (we patched the symptom).
- **Token-rarity already in (IDF);** learned name embeddings would be the next ranking lever if
  multilingual/heavily-reordered names become common.
- Discarded for good reason: addresses (no signal here), the simple matcher's sub-scores (no gain),
  LLMs in the live decision path (you need human review and a stable taxonomy across pipelines).

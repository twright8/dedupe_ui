# Session prompt — does the GBT re-scorer actually help? (backend pipeline test)

> Paste everything below into a new Claude Code session.

---

You are testing the **backend** matching pipeline of the `roe_ui` project — an OCOD↔ROE
company-name reconciliation tool. **No frontend.** Drive everything through the Python pipeline
modules / services (or the API). Programmatically mimic what a research analyst does in the UI:
**run Splink → label some pairs → train the GBT → apply it → keep labelling** — and rigorously
measure whether the GBT is worth it.

## The three questions to answer — with numbers, not vibes
1. **Absolute lift.** Does the GBT improve match quality *overall* vs raw Splink? (AUC,
   precision/recall at the decision thresholds, Brier — measured on a frozen, never-trained-on
   eval set.)
2. **Is the probability gradient correct?** The GBT's whole selling point is turning Splink's
   clumpy discrete scores into a continuous, *calibrated* gradient. Verify:
   - **calibration** — do predicted probabilities match observed match rates? (reliability curve, ECE)
   - **ranking** — does AUC / ordering improve?
   - **de-clumping vs collapse** — does the score spread into a usable *monotonic* gradient
     (higher score ⇒ higher true-match rate), or collapse into an over-confident **bimodal**
     (≈0 / ≈1) that empties the review band? The bimodal collapse on sparse/separable labels is a
     KNOWN failure mode — detect it explicitly.
3. **Golden point for labelling.** Sweep the number of training labels and find where the GBT
   first beats Splink and where it plateaus (diminishing returns) — i.e. the minimum/optimal
   label budget. Map the danger zone (too few labels ⇒ collapse).

## Orient yourself first (read before assuming any API)
- `docs/PIPELINE_CONTEXT.md`, `docs/PIPELINE_AS_BUILT.md`, `docs/REVIEW_FRAMEWORK.md`,
  `docs/RECONCILIATION_DECISIONS.md` — what the pipeline is and the label semantics.
- `backend/app/pipeline/`: `stage_2_probabilistic_link.py` (Splink), `stage_2_5_gbt.py`,
  `gbt_train.py`, `gbt_features.py`, `gbt_proxy.py` (cold-start proxy), `gbt_model.py`,
  `gbt_active.py` (active-learning sampler), `stage_3_evaluate.py`.
- `backend/app/services/llm_labeler.py` (labelling criteria + import), `label_applier.py`,
  `label_resolver.py`.
- `backend/app/routers/model.py` (train / apply / active-batch / import-labels / eval-set
  designate+status), `runs.py` (`/re-bucket`).
- `backend/app/db.py` (labels schema: `provenance`, `held_out`; note the **one-TRUE-per-OCOD**
  invariant in `routers/labels.py:upsert_label` — a new TRUE auto-FALSEs the entity's other
  candidates as `implied_negative`).
- `backend/seed_demo_run.py` (fast synthetic run), `backend/data/models/gbt_metrics.json` (metric shape).

## How to run (backend only)
- Python: `backend/.venv/bin/python`. Env: `DATA_DIR=<dir>` and `SITE_PASSWORD=x` (required even
  for plain imports — `app.auth` reads it at import time).
- **Work in an isolated `DATA_DIR`** (fresh copy) so you fully control the label budget and don't
  clobber the dev data/labels. Seed/`random_seed` everything for reproducibility.
- Inputs: real zips are in the repo root / `backend/data/uploads/`
  (`OCOD_FULL_2026_06.zip`, `BasicCompanyDataAsOneFile-2026-06-01.zip`). A full run is ~1–2 min.
  Use `seed_demo_run.py` only to debug the mechanics — do the real evaluation on real data.
- Drive the pipeline by calling stage/service functions directly (preferred for scripting) or via
  `uvicorn app.main:app` + the endpoints.

## The LLM is your "analyst" — use Haiku and Sonnet
You produce labels by handing an LLM the two company names (raw + cleaned), the jurisdiction, and
the criteria from `llm_labeler.LABELLING_CRITERIA`, and asking for `TRUE` / `FALSE` / `UNCERTAIN`
+ a confidence + a one-line reason. Then write them into the label store.
- **Gold eval set: use Sonnet** (higher quality). ~200 cases, a deliberate mix of **certain**
  (clear yes / clear no) and **ambiguous** (middle scores; OCOD entities with ≥2 close ROE
  candidates — the `is_ambiguous` / tied cases). For ambiguous entities pick **one** TRUE per OCOD
  (the invariant), the rest FALSE. Freeze it (`held_out=1`); **never train on it**.
- **Training labels: use Haiku** (cheap/fast) for the bulk budgets in the sweep.
- Label efficiently — batch + prompt caching, or parallel subagents. Record each label's model +
  confidence so you can study label noise (the eval set is the weak link).

## Experiment
1. **Frozen eval set** (~200 mixed, Sonnet, `held_out=1`). Ground truth for everything below.
2. **Baseline:** raw-Splink scores vs the eval set — AUC, precision/recall at the run thresholds,
   Brier, calibration curve, and the score-distribution shape (n distinct values).
3. **Sweep the training budget** N ∈ {0, 25, 50, 100, 200, 400, …}, disjoint from the eval set:
   - Haiku-label N pairs. Try both **random** and **active-learning-selected** (`gbt_active`) — compare.
   - Train (`gbt_train`), apply (`stage_2_5_gbt` / `/model/apply`), re-bucket.
   - On the **frozen eval set**: AUC, Brier (raw + calibrated), precision/recall at thresholds,
     ECE/reliability curve, and the gbt_score distribution (distinct values, spread, monotonic
     score↔observed-true-rate, **bimodality flag**, review-band size).
   - Flag the **over-confidence collapse** wherever it occurs.
4. **Tabulate + plot every metric vs N.** Identify: the N where GBT first beats Splink; the
   plateau (golden point); the collapse danger zone.
5. **Manual-vs-quantitative validation:** on the gold 200, compare the LLM's TRUE/FALSE against
   Splink and GBT scores — where do they agree/disagree, especially on the **ambiguous** cases?
   Does the quantitative AUC/calibration actually track the manual labels? Quantify LLM label noise
   (re-label a subset, or Haiku-vs-Sonnet agreement).

## Guardrails
- **Never train on the eval set.** Keep provenance honest (`llm` for these; eval = `held_out`).
- **Determinism:** seed Splink (`random_seed`), the GBT, and all sampling.
- **Don't trust a perfect score** (AUC 1.0 / Brier 0) — it means the split is tiny/separable;
  report it as a red flag, not a success.
- LLM labels are imperfect — use the stronger model for the gold set and measure label noise.
- Isolated `DATA_DIR`; don't touch dev data.

## Deliverable
A report at `docs/experiments/gbt-eval-<date>.md` answering Q1/Q2/Q3 with **tables + the
metric-vs-N curves**, plus:
- A plain verdict: *does the GBT help, by how much, and from how many labels?*
- Whether the probability gradient is trustworthy (calibrated + monotonic) or just clumpy/collapsed.
- The recommended label budget (golden point) and the danger zone.
- Honest caveats (LLM-label noise, eval-set size, real vs synthetic data).

**Done = a reader can decide whether to ship the GBT and how many labels to collect first, with the
numbers to back it.**

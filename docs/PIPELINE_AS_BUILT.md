# Pipeline — As-Built, End to End

**Purpose.** Document the *actual* end-to-end behaviour of the running system — every
stage, which backend function produces it, which screen/endpoint surfaces it, and how
(mechanically and via explainer text) the review UI links back to the engine. This is a
description of the system **as it runs today**, to be read alongside the *intent* docs
(`REVIEW_FRAMEWORK.md`, `RECONCILIATION_DECISIONS.md`). Where the two diverge, that gap is
logged in `CONCERNS_LOG.md`.

**Compiled from:** direct code reading + live inspection of run `run_2026_06_04a` on
2026-06-04. Lines cited as `file:line` are accurate as of that date.

---

## 0. The running stack

| Piece | What | How to run |
|---|---|---|
| Backend | FastAPI, `app.main:app`, port 8000; health at `/api/health` | `cd backend && SITE_PASSWORD=… DATA_DIR=data .venv/bin/python -m uvicorn app.main:app --port 8000` |
| Frontend | React + Vite, port 5173; proxies `/api` → `:8000` (`vite.config.js`) | `cd frontend && npm run dev -- --port 5173` |
| Auth | Site password (`SITE_PASSWORD` env, required) → session cookie → user picker. `auth.py`; public paths `/api/auth/login`, `/api/health` | login `POST /api/auth/login {password}` |
| Data | `backend/data/`: `linkage.db` (runs, labels, config, audit), `runs/<id>/` (parquets + CSVs + diagnostics), `models/` (GBT), `uploads/` | `DATA_DIR=data` |

Run state lives in `runs/<id>/`; the label/config/audit state lives in `linkage.db`.

---

## 1. The end-to-end pipeline, as it actually runs

Driver: `pipeline_runner._run_pipeline` (`backend/app/services/pipeline_runner.py:281`).
It executes **six** things in order. Note Stage 2.5 — it always runs but is invisible.

| Step | Backend | Output | Decision score | Surfaced in UI? |
|---|---|---|---|---|
| **0 Preprocess** | `stage_0_preprocess.run_stage_0` | cleaned `ocod/roe_preprocessed.parquet`, `ocod_dedup`, `standardisation_report.txt` | — | ✅ stage box; rules table on RunDetail |
| **1 Exact** | `stage_1_exact_match.run_stage_1` | `exact_matches.parquet` (~22.5k, conf=1.0); `*_phase2.parquet` = the hard pool | =1.0 | ✅ stage box ("N matches") |
| **2 Splink** | `stage_2_probabilistic_link.run_stage_2` | `linkage_scored.parquet` (Fellegi-Sunter; **purely unsupervised EM — labels never train `m`**; second blocking rule = exact `name_core` across jurisdictions; `jurisdiction_cmp` is a live 3-level comparison). **Predicts down to a low candidate floor (`match_probability_threshold_candidate`, 0.05); sub-review pairs are RETAINED and counted `droppedBelowReview`.** | clumpy `match_probability` | ✅ stage box ("N scored · K training labels") |
| **2.5 GBT** | `stage_2_5_gbt.run_stage_2_5_gbt` | **No-op unless a model is trained.** Adds `gbt_score` / `gbt_score_raw`. When a model is **active** (`models/active.json`), a fresh run scores with that immutable **version** and **auto-applies it in-band** (`apply_active_gbt_bucketing`) — enabling GBT bucketing BEFORE Stage 3, guarded so a collapse falls back to Splink (run **warns**, never fails). Manual apply/activate/deactivate/revert are versioned. | GBT (in-band, guarded) | ➖ in-band; decision surfaced via `decisionModel` badge + `gbtWarning` |
| **3 Evaluate** | `stage_3_evaluate.run_stage_3` | buckets into exact / auto-accept (≥high) / review / ambiguous; emits all match CSVs + `merged_dataset.csv` (**one row per (title, proprietor)**, now carrying `entity_id`/`entity_key`/`entity_uid`/`entity_identified`) + **`merged_roe.csv`** (one row per ROE company, with matched title counts); stamps `decision_model` (`splink`/`gbt:<N>`) through parquet, CSVs and merged; diagnostics regenerated with the true decision score | Splink **or** GBT (whichever decided) | ✅ stage box; Outcome composition bar |
| **POST Apply labels** | `label_applier.apply_labels` | overlays human labels onto the export — TRUE forces, FALSE suppresses. Runs **last, every time**; if it throws the run **fails** (success contract). Counts are collected **twice** — once before (`counts.pre_labels`) and once after — so the model-only result stays visible | human override | ✅ stage box + "Before and after your labels" panel |

**In-band now: an ACTIVE model auto-applies during the run.** Once a model version is
activated (`models/active.json`), every fresh run scores with it (Stage 2.5) and buckets on
it (Stage 3) automatically, with a collapse-guard fallback to Splink — so a fresh run no
longer silently reverts to Splink. Model **versions are immutable snapshots**
(`models/versions/<N>/`); `POST /api/model/activate|deactivate|revert` manage activation.

**Still out-of-band: manual apply / re-bucket.** The Model panel can also `/api/model/train`
then `/api/model/apply` → `pipeline_runner.rebucket_run(score_with_gbt=True)`, which re-scores
+ re-buckets + re-applies labels **without re-running Splink**. Threshold "commit" and
"mark-by-range" use the same `rebucket_run` path (`/api/runs/{id}/re-bucket`).

---

## 1a. Entity identity and the two directions of the join

`merged_dataset.csv` is an OCOD-left join: every OCOD (title, proprietor) row, plus an OE
number where one was found. `merged_roe.csv` is its mirror: every ROE company, plus the
titles matched to it. **The unmatched half of each file is the control on the other.** An
unmatched OCOD proprietor could mean the entity never registered *or* that the link failed;
a large unmatched-ROE set points at the second. Neither file alone can tell you which.

Entity identity is carried in four columns on the OCOD side:

| Column | Meaning |
|---|---|
| `entity_id` | the OE number, or empty when unmatched |
| `entity_key` | `name_clean` + `\|` + `jurisdiction_clean` — the fallback identity |
| `entity_uid` | **deduplicate on this**: `entity_id` when present, else `NAME:<entity_key>` |
| `entity_identified` | `True` when the id is a real registration, `False` when name-based |

A matched row has a durable identity — the OE number, stable across runs and across monthly
files. An unmatched row has none, *by definition*: that absence is the thing being measured.
So the fallback is a name key, and `entity_identified` keeps the two kinds apart, because a
single "distinct entities" figure that mixes them would overstate what is actually known.

**Known limit:** a name-based `entity_uid` still splits one company across spellings
(`ABC HOLDINGS LTD` vs `ABC HOLDINGS LIMITED` are two entities here). Collapsing those needs
an OCOD-to-OCOD clustering pass, which the pipeline does not do.

---

## 2. The two scores (the thing the UI hides)

- **Splink `match_probability`** — discrete Fellegi-Sunter levels. *Clumpy* (e.g. this run:
  `0.409, 0.513, 0.959, 0.992, 0.997, 0.999, 1.0`). Good for ranking, bad for fine cuts.
- **GBT `gbt_score`** — continuous, calibrated. When applied, Stage 3 **swaps it into
  `match_probability`** and keeps raw Splink as `splink_probability`.

**How to tell which one decided a run:** the authoritative signal is now the
**`decision_model` column** (`splink` / `gbt:<N>`), stamped through the scored parquet, the
match CSVs and `merged_dataset.csv`, and surfaced on the run payload as `decisionModel` /
`decisionModelVersion` (plus `gbtWarning` when a collapsed active model fell back to Splink).
RunDetail renders it as a decision badge; the Diagnostics histogram header and title also name
the decision score. (Was: the histogram header was the *only* signal — see `CONCERNS_LOG.md#C`.)

---

## 3. The label model, as built

- **Append-only, provenance-tagged.** `labels.upsert_label` inserts-new + deactivates-old
  (`active` flag); current verdict = latest active row, history retained.
- **Provenance values in use:** `manual`, `bulk_review`, `bulk_range`, `implied_negative`,
  `llm` (the cold-start proxy is in-memory only, never written — deviation noted in
  `RECONCILIATION_DECISIONS.md §5`).
- **Three roles:** operational (export) / training (GBT, TRUE+FALSE, human-authored only) /
  evaluation (`held_out=1`, human-only, never trained on).
- **Raw-first identity.** Raw values (`ocod_name_raw`, `ocod_jurisdiction_raw`) are the
  durable key — cleaning rules are editable config, so a stored cleaned key can go stale, but
  the raw source never changes for the same records. Resolution tries the raw key first, the
  cleaned key as fallback (`build_frame_key_index` / `find_indexed_rows`), with a raw-triple
  supersede + partial-unique index + dedupe migration behind it.
- **One resolver** (`services/label_resolver.py`) used by *all three* consumers — display
  (`match_reader`), export (`label_applier`) and GBT training (`gbt_train._labelled_feature_frame`)
  — so they can't disagree about whether a pair is labelled.
- **Live data state (run_2026_06_04a, 2026-06-04):** library 52 active = 36 `manual` +
  17 `NULL`-provenance; **`held_out = 0` (eval set empty)**; for this run 17 applied,
  14 fed Splink's supervised-`m`, **35 unmatched**. See concerns E/F/G.

---

## 4. Screen ↔ endpoint ↔ backend map

| Screen | Endpoint(s) | Backend |
|---|---|---|
| Runs list | `GET /api/runs` | `routers/runs.py` |
| New run | `POST /api/runs` (zips, config v, thresholds) | `enqueue_run` → `start_run` |
| Run detail / stages | `GET /api/runs/{id}` (+ SSE progress) | `runs.py`; events from `events.jsonl` |
| Diagnostics (the only Splink/GBT signal) | `GET /api/runs/{id}` diag payload | `stage_3` diagnostics |
| Review queue | `GET /api/runs/{id}/matches?bucket=review` | `runs.py` + `match_reader` |
| Entity review (default) | `GET /api/runs/{id}/matches/by-ocod` | `runs.py` |
| ROE review | `GET /api/runs/{id}/matches/by-roe` | `runs.py` |
| Threshold preview | client-side only (no call) | — |
| Threshold **commit** / mark-by-range | `POST /api/runs/{id}/re-bucket` | `rebucket_run` |
| Label (single / bulk) | `POST /api/labels`, `POST /api/labels/batch` | `routers/labels.py` |
| Model panel (GBT) | `GET /api/model`; `POST /api/model/{train,apply,active-batch,import}`; `/api/model/eval-set` | `routers/model.py` |
| Label library | `GET /api/labels` | `labels.py` |
| Config & rules | `GET/PUT /api/config/{kind}` (+ history) | `routers/config.py` |
| Audit log | `GET /api/audit` | `routers/audit.py` |
| Export | `GET /api/runs/{id}/files/{name}` · `/files/all` | static file serve |

---

## 5. The three flows we are validating

1. **Quick / no-label:** New run (high `threshold_high`) → run → RunDetail → **Export merged**
   (`merged_dataset.csv`). *Gap:* no "auto-accepted subset only" download — you get the full
   merged file and filter yourself.
2. **Label & reconcile:** as (1) → Review/Entity review → stage + commit labels → labels
   re-apply on export (POST stage).
3. **Re-run with labels:** as (2) → re-run → labels persist by key and re-apply. **This is
   where the Splink-vs-GBT question lives:** re-running alone keeps you on Splink unless you
   train + apply the GBT.

---

## 6. Qualitative links (explainers) — where the UI explains itself

The review surface *does* carry some inline explainer text (good), e.g.:
- ThresholdPanel header: `"slider = rule · drag the chart to select a labelling window"`.
- ThresholdPanel footer: `"Drag across the chart to select a score window to bulk-label.
  The slider just sets the auto-accept rule."`
- RunDetail / review band note: `"Stage 2 only emits candidate pairs at or above the review
  floor … To inspect weaker possible matches, lower the review floor and run the pipeline
  again."`

What it does **not** explain (tracked in `CONCERNS_LOG.md`): which model scored a pair,
whether a GBT exists / was applied, what "training labels" feeds, or how a label propagates
to the export. Auditing these explainers — mechanically (does the button hit the endpoint I
think?) and qualitatively (does the text tell the truth, and enough of it?) — is the point
of the walkthrough.

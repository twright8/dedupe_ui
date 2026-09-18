# OCOD-ROE Linkage Pipeline — Context, Intent, and Operational Guide

This document is the handoff context for the pipeline that lives in this
repository. It describes what the pipeline does, why each part is shaped the
way it is, the operational use cases it supports, how to tune it, and what
the UI on top of it needs to provide for users.

It does not describe the UI design. It describes the pipeline so that
someone building a UI, an API on top, or a future maintainer of the pipeline
has the full context without needing to read the conversation history that
produced it.

---

## 1. Purpose

Transparency International UK wants to link two UK datasets:

- **OCOD** — the Land Registry's "Overseas Companies that Own property in
  England & Wales" file. ~91,000 title rows. Each row is a UK property
  whose registered proprietor is an overseas legal entity. The proprietor
  name is recorded but the proprietor's Companies House number is usually
  not.
- **Companies House Basic Company Data** (the bulk monthly snapshot of all
  UK-registered companies). Within this file are ~30,000 rows whose
  `CompanyNumber` begins with `OE` — these are entries in the **Register
  of Overseas Entities (ROE)**, a register introduced by the Economic
  Crime Act 2022 that requires overseas entities owning UK property to
  declare beneficial owners.

Linking the two is the only practical way to map "this UK property is owned
by overseas entity X" to "overseas entity X has these beneficial owners".
The OE number is the key that unlocks the rest of the Companies House API
(beneficial owners / PSC data, filings, incorporation history). Without
the OE number, the OCOD records are dead ends for further investigation.

### Downstream use (currently out of scope)

Once OCOD records have OE numbers attached, the user queries the
Companies House per-company API for:

- Beneficial ownership (PSC data) — who actually controls the entity
- Incorporation history
- Officer information

The pipeline in this repository **does not** make those API calls. It
produces a merged dataset of OCOD records enriched with OE numbers (and a
confidence/review label per row), which becomes the input to that
follow-on API step.

---

## 2. Data shapes

### OCOD (~91,000 rows)

Each row is a Land Registry title with up to 4 overseas proprietors per
row (the entity that owns the property). The current pipeline only uses
proprietor 1 — proprietors 2-4 are rare (~7% of rows have more than one
proprietor) and can be added later.

Key fields used (after renaming inside the pipeline):

- `title_number` — Land Registry title ID
- `ocod_name_raw` — proprietor 1 name as recorded (free text, mixed case)
- `ocod_reg_no` — proprietor 1's overseas registration number. Populated
  only ~4.8% of the time, and when populated it is the **overseas**
  registration number (e.g. Jersey, Isle of Man), **not** the
  Companies House OE number. So it cannot be used directly as a join key.
- `ocod_jurisdiction_raw` — country/jurisdiction of incorporation, free
  text and inconsistent (`BRITISH VIRGIN ISLANDS`, `DELAWARE, U.S.A.`,
  `ABU DHABI` etc.)
- `ocod_address_1/2/3` — the proprietor's UK service/contact address for
  property matters. Free text, three lines. **This is not the entity's
  home-jurisdiction registered address.**
- `property_address`, `property_postcode`, `district`, `region` — the UK
  property's location

### Companies House Basic Company Data (~5.7M rows)

A bulk monthly CSV of every UK-registered company. Pipeline filters to
the ~30,000 rows whose `CompanyNumber` begins with `OE`. These are the
ROE entries.

Key fields used:

- `CompanyNumber` — the OE number we want as the output
- `CompanyName` — the entity's name as registered on the ROE
- `CountryOfOrigin` — jurisdiction of incorporation (consistently UPPER
  CASE in this file, though some rows have Title Case which the pipeline
  handles)
- `RegAddress.AddressLine1`, `AddressLine2`, `PostTown`, `PostCode`,
  `Country` — the entity's **registered address in its home
  jurisdiction**. This is the address its registered agent files for it,
  which is often different from the OCOD service address.
- `CompanyStatus` — always "Active" for OE entries (struck-off OEs aren't
  in the snapshot)
- `CompanyCategory` — always "Overseas Entity"

### Quirks worth knowing

1. **Leading-space column headers** in the CH CSV: `" CompanyNumber"`,
   `" RegAddress.AddressLine2"`, several `" PreviousName_*"` columns. The
   pipeline strips whitespace from headers on load. If you ever read this
   CSV elsewhere, do the same.

2. **No historical names available**. The CH CSV has columns
   `PreviousName_1.CompanyName` through `PreviousName_10.CompanyName` but
   for OE-prefixed rows these are **all empty** (0 of 30,221 populated).
   ROE is new (2022) and either entities haven't changed name yet, or
   the bulk file doesn't carry historical names for overseas entities. If
   historical names are wanted, they'd have to be fetched via the
   per-company CH API — out of scope for this pipeline.

3. **Address fields don't align reliably between OCOD and ROE**. They
   describe different things by design: ROE has the entity's registered
   address in its home jurisdiction; OCOD has the entity's UK service
   address for property correspondence. Even for confirmed matches, only
   ~27% have agreeing postcodes when both present; full-address token
   Jaccard ≥ 0.5 holds for only ~20% of confirmed matches. Crown
   Dependencies (Jersey, Isle of Man, Guernsey) have moderate overlap
   (30-50%); tax-haven jurisdictions (BVI, Cayman, Panama, Hong Kong)
   essentially never overlap. **The pipeline does not use address fields
   for matching**, but does carry both sides through to the merged
   output so downstream users can see them side-by-side.

4. **Jurisdiction naming differs between datasets**. OCOD records US
   states individually (`DELAWARE, U.S.A.`, `NEW YORK, U.S.A.`) while ROE
   uses `UNITED STATES`. OCOD has `BRITISH VIRGIN ISLANDS`; ROE has
   `VIRGIN ISLANDS, BRITISH`. OCOD has UAE emirates as separate entries
   (`ABU DHABI`, `DUBAI`); ROE has `UNITED ARAB EMIRATES`. A
   comprehensive jurisdiction map handles all of these (see Configuration
   section below).

5. **Garbage data exists in OCOD jurisdictions**. One row's jurisdiction
   is literally `1811435`. The pipeline maps such values to a sentinel
   `UNKNOWN` and excludes them from blocking but keeps them in the
   unmatched-OCOD output for manual review.

---

## 3. Pipeline architecture

Four stages. Each writes outputs that the next stage reads. Each stage
can be re-run independently.

### Stage 0 — Preprocess (`src/stage_0_preprocess.py`)

Inputs: OCOD zip, CH zip in the project folder.

Steps:
1. Stream-read the CH CSV in 200K-row chunks, keeping only 8 of 55
   columns (memory-bounded; full CSV would be several GB).
2. Filter CH rows to those with `OE`-prefixed CompanyNumber.
3. Load the OCOD CSV (small; ~91K rows).
4. Uppercase all name and jurisdiction fields.
5. Apply jurisdiction mapping (from `config/jurisdiction_map.csv`).
6. Apply name normalisation rules (from `config/name_rules.json`).
7. Compute derived columns: `name_clean`, `name_digits_sorted`,
   `name_core` (entity-suffix stripped), `name_tokens_sorted`.
8. Deduplicate OCOD by `(name_clean, jurisdiction_clean)` for linkage;
   keep the full title-row table separately for join-back.

Validates that every distinct jurisdiction value is covered by the
mapping. If a new value appears in a future data drop, the stage halts
with a clear "add these values to jurisdiction_map.csv" message.

Outputs: `output/ocod_preprocessed.parquet`, `output/ocod_dedup.parquet`,
`output/roe_preprocessed.parquet`, `output/standardisation_report.txt`.

### Stage 1 — Phase 1 (deterministic exact match) (`src/stage_1_exact_match.py`)

Inner-joins `ocod_dedup` and `roe_preprocessed` on `(name_clean,
jurisdiction_clean)`. This is a hash join — essentially zero cost, no
cross-product. Records that match here are confident matches by
construction: after cleaning, their names and jurisdictions are
identical.

Catches ~22,400 of ~30,200 ROE entries on the current data (about 74%
of the ROE pool, covering ~87% of OCOD title rows since the same
deduped OCOD record often corresponds to multiple title rows).

Outputs: `output/exact_matches.parquet`, `output/ocod_phase2.parquet`,
`output/roe_phase2.parquet`. The phase-2 files are the records that
DIDN'T exact-match — these go to the probabilistic stage.

### Stage 2 — Phase 2 (probabilistic, Splink) (`src/stage_2_probabilistic_link.py`)

Runs Splink (a probabilistic record linkage library built by the UK
Ministry of Justice) on the post-exact-match pool. Splink uses a
Fellegi-Sunter Bayesian model:

- Blocks on **either** of two rules ORed together: (1) same
  `jurisdiction_clean` (excluding `UNKNOWN` = `UNKNOWN` pairs), **or**
  (2) exact non-empty `name_core` match across jurisdictions. The second
  rule (wired to the `cross_jurisdiction_name_matching` run toggle,
  default on) rescues records whose jurisdiction is mis-recorded or
  `UNKNOWN` — those now reach Phase 2 instead of being blocked out.
- Compares on:
  - `name_clean` via Jaro-Winkler at thresholds `[0.97, 0.95, 0.92, 0.90, 0.85, 0.80]`
  - `name_core` (entity-suffix stripped) — exact match
  - `name_tokens_sorted` (unique tokens sorted) — exact match
  - `name_digits_sorted` (numeric tokens sorted) — exact match
  - `jurisdiction_clean` — now a real ExactMatch comparison (via a
    derived `jurisdiction_cmp` column that is NULL for `UNKNOWN`/empty,
    giving three levels: agree / disagree / unknown-neutral). It only
    contributes evidence because the name_core rule lets pairs disagree
    on jurisdiction; with jurisdiction-only blocking it stays constant
    within a block and is skipped.
- Learns m/u probabilities for each comparison level via EM
  (Expectation-Maximization). Splink is **purely unsupervised** — labels
  never feed m-estimation (they train only the GBT); explained in detail
  in Section 5.
- Predicts pairwise match probabilities down to a low **candidate floor**
  (`match_probability_threshold_candidate`, default 0.05; configs that
  predate this key fall back to `threshold_review`). Everything above the
  candidate floor — including weak sub-review pairs — is retained in
  `linkage_scored.parquet`; the review threshold is applied later as a
  Stage-3 bucketing line only.

Resource usage is bounded: DuckDB memory limit 4GB, threads 4, temp dir
under `output/duckdb_tmp/`. Pair count after blocking is ~4M (was
~100-150M before the Phase-1 split, which is why the two-phase
architecture exists — Phase 1 removes the bulk of the load before
Splink runs).

Outputs: `output/linkage_scored.parquet`, `output/splink_model.json`.

### Stage 3 — Evaluate & Export (`src/stage_3_evaluate.py`)

Reads exact matches + scored Phase 2 pairs and produces:

- **`matches_exact.csv`** — Phase 1 deterministic matches
- **`matches_high_confidence.csv`** — Phase 1 + Phase 2 matches at
  `>= threshold_high` (default 0.70)
- **`matches_for_review.csv`** — Phase 2 matches in
  `[threshold_review, threshold_high)`. Has empty `is_true_match` and
  `reviewer_notes` columns for human decisions.
- **`matches_ambiguous.csv`** — OCOD records with >1 high-confidence
  candidate whose top-vs-second probability gap is < 0.05 (i.e. truly
  tied, not just multiply-matched). User picks one TRUE per OCOD group.
- **`unmatched_ocod.csv`** — OCOD records (deduped) with no
  high-confidence match.
- **`unmatched_roe.csv`** — ROE entries with no OCOD match.
- **`merged_dataset.csv`** — one row per **(title, proprietor)** — a
  title with N overseas proprietors contributes N independently-matched
  rows, tagged with `proprietor_index` (contract change; it used to be
  one row per title). Each row is joined with that proprietor's best ROE
  match (if any). Includes `roe_company_number`, `match_method` (`exact`
  / `probabilistic` / `reviewed_true_*`), `match_probability`,
  `match_count`, `is_ambiguous`, `decision_model` (`splink` /
  `gbt:<N>`). Title-grain counts (a title counts as matched if any of its
  proprietor rows matched) are computed separately for the UI.

Also writes diagnostic HTML charts (see Section 6) and an embedded
dashboard explaining what each chart shows.

### Stage 4 — Apply user reviews (`src/stage_4_apply_reviews.py`)

Reads `matches_for_review_reviewed.csv` and/or
`matches_ambiguous_reviewed.csv` if present (i.e. the user's edited
versions of the CSVs from Stage 3). For each row marked `is_true_match
= TRUE` (or T / Y / 1), updates `merged_dataset.csv` so that the OCOD
title rows for that proprietor pick up the user-confirmed OE number,
tagged `match_method = reviewed_true_review` or
`reviewed_true_ambiguous`.

No-op if no reviewed file present. This stage is the bridge between the
automated pipeline and human curation.

### Orchestration

`run_all.py` chains stages 0-4 sequentially, fail-fast. Each stage is
also runnable on its own. Typical iteration loop while tuning:

1. Run all four stages once on the full data
2. Edit `config/*.json` or `config/*.csv`
3. Re-run only Stage 2 + Stage 3 (the cleaning rules + model + scoring
   change, but Stage 0's parquet outputs are still valid as long as you
   regenerate them on rule changes)

If you only changed thresholds (in `linkage_settings.json`), re-run only
Stage 3 — the model and scored pairs haven't changed, only the
bucketing.

---

## 4. Configuration

All matching behaviour is driven by editable files in `config/`. No
hard-coded rules in Python.

### `config/jurisdiction_map.csv`

Maps every raw jurisdiction value from both OCOD and ROE to a
standardised value. Columns:

```
source_dataset, raw_value, standardised_value
ocod, "BRITISH VIRGIN ISLANDS", BRITISH VIRGIN ISLANDS
ocod, "DELAWARE, U.S.A.", UNITED STATES
ocod, ABU DHABI, UNITED ARAB EMIRATES
roe, "VIRGIN ISLANDS, BRITISH", BRITISH VIRGIN ISLANDS
roe, UNITED STATES, UNITED STATES
ocod, 1811435, UNKNOWN
```

About 200 distinct OCOD values and 140 distinct ROE values are mapped.
Pipeline halts if the underlying data contains a value not in this map,
listing the unmapped values so the analyst can add them. The
"corner-case" mappings (e.g. `CHONBURI → THAILAND`, `PENANG → MALAYSIA`,
`TENERIFE → SPAIN`, the misspelling `THE REPUBLIC OF LIBE RIA → LIBERIA`)
were derived from manual inspection of the actual data.

`UNKNOWN` is the sentinel for empty/garbage values. Records with
`UNKNOWN` jurisdiction are excluded from blocking but still appear in
`unmatched_*.csv` for manual review.

### `config/name_rules.json`

Ordered list of regex find/replace pairs that transform raw names into
cleaned names. Ordering matters; descriptions document what each rule
does. The current ruleset, in execution order:

1. **Accent folding** (Unicode NFKD decomposition) — `À → A`, `é → e`,
   `ü → u`. Special-case handled in code rather than regex.
2. **Special Latin chars not decomposed by NFKD** — `Ø → O`, `Ł → L`,
   `Đ/Ð → D`, `Æ → AE`, `Œ → OE`, `ß → SS`.
3. **Drop leading `THE `**.
4. **`& ` → ` AND `** (only when surrounded by spaces).
5. **Hyphens / en-dashes / em-dashes → space** (so `SORA-OREWA` matches
   `SORA OREWA`; not `SORAOREWA`).
6. **Multi-word entity abbreviations** — `PUBLIC LIMITED COMPANY → PLC`
   (must run before `LIMITED → LTD`).
7. **Entity word abbreviations** — `LIMITED → LTD`,
   `INCORPORATED → INC`, `CORPORATION → CORP`, `PROPRIETARY → PTY`,
   `COMPANY → CO`.
8. **`LTD LIABILITY CO → LLC`** and **`LTD LIABILITY PARTNERSHIP → LLP`**
   (run after the abbreviation rules above so the input form is
   already-abbreviated).
9. **Plural normalisation** — `HOLDINGS → HOLDING`, `INVESTMENTS →
   INVESTMENT`, `PROPERTIES → PROPERTY`, etc.
10. **Road suffix abbreviations** — `STREET → ST`, `ROAD → RD`,
    `AVENUE → AVE`, etc.
11. **Punctuation removal** — strips `[".,/#!$%^&*;:{}=_\`~()']`.
    Hyphens already replaced with space earlier so they're not in this
    class. Including `"` is what makes `"LADY ROSE" PRIVATSTIFTUNG`
    match `LADY ROSE PRIVATSTIFTUNG`.
12. **`NO 24 → NO24`** and **`NUMBER 24 → NO24`** — runs after
    punctuation removal so `NO. 24` is handled (becomes `NO 24` then
    `NO24`).
13. **Split-suffix collapse** — `SA RL → SARL`, `B V → BV`, `N V → NV`,
    `A G → AG`, `L L C → LLC`. Runs after punctuation removal because
    `S.A.R.L.` becomes `SARL` (no spaces) automatically; but
    `S.A. R.L.` becomes `SA RL` (mid-string space preserved) which this
    rule collapses.
14. **Collapse whitespace** to single spaces.
15. **Trim leading/trailing whitespace**.

The order is intentional. Multi-word rules run before their substrings.
Number-collapse runs after punctuation removal. Whitespace cleanup runs
last.

### `config/legal_entity_tokens.json`

A single list of tokens that get stripped from the end of `name_clean`
to produce `name_core`. Stripping is iterative — walk from the end,
drop any token in the set, stop at the first non-matching token. Used
as a separate Splink comparison column so that names like
`SOCIETA SEMPLICE KENGARDEN 2005 LIMITED PARTNERSHIP` and
`SOCIETA SEMPLICE KENGARDEN 2005` reduce to the same `name_core` and
get high probability via that comparison.

The set covers ~50 legal-entity forms: `LTD`, `INC`, `CORP`, `CO`,
`PLC`, `PTY`, `PTE`, `LLC`, `LP`, `LLP`, `SA`, `SAS`, `SARL`, `SRL`,
`SPA`, `LDA`, `BV`, `NV`, `AG`, `GMBH`, `KG`, `KGAA`, `AB`, `AS`,
`OY`, `WLL`, `BHD`, `SDN`, `ANSTALT`, `STIFTUNG`, etc.

### `config/linkage_settings.json`

Splink model configuration. Key knobs:

- **`blocking_rules`** — two rules, ORed: same-jurisdiction (excluding
  `UNKNOWN`=`UNKNOWN`) **or** exact non-empty `name_core` across
  jurisdictions. The cross-jurisdiction rule is added by run creation
  when the `cross_jurisdiction_name_matching` toggle is on (default).
  Jurisdiction-only blocking gives ~4M pairs; the name_core rule adds a
  bounded number of same-core cross-jurisdiction pairs.
- **`comparisons`** — defines each Splink comparison column:
  - `name_clean`: Jaro-Winkler at 6 thresholds `[0.97, 0.95, 0.92,
    0.90, 0.85, 0.80]`
  - `name_digits_sorted`: ExactMatch
  - `name_core`: ExactMatch
  - `name_tokens_sorted`: ExactMatch
  - `jurisdiction_clean`: ExactMatch — now a **live** comparison, scored
    on a derived `jurisdiction_cmp` column (NULL for `UNKNOWN`/empty, so
    an unknown jurisdiction reads as no-evidence rather than a
    disagreement). It is auto-skipped only when every blocking rule
    already forces jurisdiction equal (i.e. jurisdiction-only blocking).
- **`match_probability_threshold_candidate`** — 0.05. The low floor
  Stage 2 predicts down to, so weak sub-review pairs are retained for GBT
  rescoring / inspection. Older configs without this key fall back to
  `threshold_review` (reproducing the previous drop-below-review
  behaviour).
- **`cross_jurisdiction_name_matching`** — true. Toggles the second
  (name_core) blocking rule at run creation.
- **`em_iterations`** — 20 (typically converges in ~22)
- **`probability_two_random_records_match`** — 0.001. The Bayesian
  prior. Default Splink is 0.0001; we raised it 10x because the
  post-exact-match pool has a higher base rate of true matches than
  the default assumes.
- **`match_probability_threshold_high`** — 0.70. Pairs at or above this
  go straight to `matches_high_confidence.csv` (auto-accepted).
- **`match_probability_threshold_review`** — 0.40. Purely a Stage-3
  bucketing line: pairs in `[0.40, 0.70)` go to `matches_for_review.csv`.
  Pairs below 0.40 are **no longer dropped** — they are retained in
  `linkage_scored.parquet` (down to the candidate floor) and reported as
  a `droppedBelowReview` count, but are not surfaced for review.

---

## 5. The Splink model

### What it does

Probabilistic record linkage using the Fellegi-Sunter model. For each
candidate pair (i.e. each pair that satisfies the blocking rule), it
computes a match probability based on the comparison columns. The
output of Splink is a scored DataFrame: every candidate pair, with its
probability and the comparison-level breakdown.

### Why probabilistic instead of just rule-based fuzzy matching

The diagram TI was using before this pipeline described a sequential
process: cleaned-name exact match → manually review failures → fuzzy
match → manually review again. Two issues:

1. The "fuzzy match" step requires threshold and weight decisions made
   by hand. There's no principled way to set them.
2. Different features (name similarity, digit-set agreement, etc.) have
   different discriminating power. A rule-based approach has to weight
   them by intuition.

The Fellegi-Sunter framework gives a principled way to combine evidence
from multiple comparison features into a single probability, with the
weights learned from the data itself.

### How m and u are learned without labels

For each comparison level, Splink learns:

- **m** = P(records agree at this level | they ARE a match)
- **u** = P(records agree at this level | they are NOT a match)

**`u` is easy**: sample millions of random pairs from the data, count
agreement rates. Random pairs are overwhelmingly non-matches, so this
empirical rate is essentially the non-match agreement rate.

**`m` is bootstrapped via EM**. We don't have a list of true matches.
But "two records that agree on multiple features at once" is very
likely a true match — random entities almost never line up on 5
features simultaneously. EM exploits this structure:

1. Start with a guess for `m`.
2. Score every pair using current `m` and `u`.
3. Re-estimate `m` weighted by the current probabilities (a pair
   scoring 0.8 contributes 80% of itself to the "matches" count).
4. Repeat until `m` stops changing.

After ~22 iterations on this data, `m` stabilises. The learned values
are "the m that makes the model's predictions internally consistent" —
not guaranteed to be "true" but a reasonable inductive answer in the
absence of labels.

**Splink never trains on labels.** `m` is always EM-estimated; human
labels train only the GBT decision layer (Stage 2.5). This keeps Phase-2
scores stable run-to-run and stops held-out evaluation labels leaking
into the `splink_p1` feature the GBT consumes.

### Why this only works with multiple features

If we had only one comparison column (say JW name similarity), EM
couldn't reliably distinguish "matches that score high because they ARE
matches" from "non-matches that happen to score high on this one
feature". The four comparison columns we have
(`name_clean`/`name_core`/`name_tokens_sorted`/`name_digits_sorted`)
give enough constraints that EM converges to sensible values.

### The two-phase split

Splink works best on data with a mix of clear matches and clear
non-matches. The 22K Phase 1 exact matches would dominate Splink's
training data and crowd out signal from the fuzzy region. Pulling them
out first means Splink sees only the hard cases and learns weights
appropriate for them.

A side effect: in Phase 2, EM never sees a pair that's an exact name
match (those went to Phase 1). So `m` for the "exact name match" level
is untrained and reported as such. Splink warns about it; the pipeline
ignores the warning. Any pair that would have hit "exact" in Phase 2 is
impossible by construction.

### How the model is saved

Splink writes the full trained model (m/u values, comparison settings,
blocking rules) to `output/splink_model.json`. This is human-readable
and can be diffed across runs. Stage 3 reloads it to generate
diagnostic charts.

---

## 6. Output files and what they contain

All written to `output/` with UTF-8 BOM encoding (so Excel reads them
correctly without an import dialog).

### Match files

| File | Contents | Auto-accepted? |
|---|---|---|
| `matches_exact.csv` | Phase 1 deterministic matches | yes |
| `matches_high_confidence.csv` | Phase 1 + Phase 2 ≥ threshold_high | yes |
| `matches_for_review.csv` | Phase 2 in `[threshold_review, threshold_high)` | no — user reviews |
| `matches_ambiguous.csv` | OCOD records with >1 high-conf candidate within prob_gap < 0.05 | no — user disambiguates |
| `matches_user_confirmed.csv` | Subset of reviewed file marked TRUE (Stage 4 output) | yes (after review) |
| `matches_final.csv` | All confirmed matches: Phase 1 + Phase 2 high + user-confirmed | yes |
| `unmatched_ocod.csv` | Deduped OCOD records with no match | — |
| `unmatched_roe.csv` | ROE entries with no match | — |

Each match-file row includes:
- `is_true_match` and `reviewer_notes` (empty by default; user fills)
- `match_method` (`exact` / `probabilistic` / `reviewed_true_review` / `reviewed_true_ambiguous`)
- `match_probability`
- Both OCOD and ROE: `name_raw`, `name_clean`, `jurisdiction`
- `roe_company_number` (the OE number)
- `ocod_unique_id`, `roe_unique_id` (for stable joining)

### Merged dataset

`merged_dataset.csv` is the final consumable output. One row per OCOD
title (91,329 rows), with these added columns:

- `roe_company_number` — the matched OE number, blank if no match
- `roe_name_raw` — the ROE name as recorded in CH
- `match_method` — which path produced this match
- `match_probability` — 1.0 for Phase 1 / reviewed; Splink probability for Phase 2
- `match_count` — how many high-confidence ROE candidates existed for this OCOD record
- `is_ambiguous` — True if `match_count > 1` AND top-vs-second probability gap was < 0.05

A user who only wants "OCOD plus its OE numbers" reads only this file.

### Diagnostics (HTML)

`output/diagnostics/dashboard.html` is the entry point. It embeds all
the charts below with inline explanations of what each one shows and
what to look for:

- `score_distribution.html` — histogram of Phase 2 match probabilities,
  bars colour-coded by band (grey = filtered, orange = review, green =
  high-confidence), threshold lines drawn.
- `match_weights.html` — Splink-generated chart of the bayes factor
  (log2 m/u) for each comparison level. Tall positive bars = strong
  positive evidence; tall negative bars = strong "not-a-match" evidence.
- `m_u_parameters.html` — Splink-generated chart of learned m and u
  side-by-side per comparison level.
- `waterfall_top_high_confidence.html` — for the 20 highest-scoring
  pairs, a feature-by-feature breakdown of how the probability was
  built up.
- `waterfall_top_review_band.html` — same for the 20 highest-scoring
  pairs in the review band. The most useful chart for deciding whether
  to lower `threshold_high`.
- `waterfall_lowest_scored.html` — same for the 20 lowest-scoring pairs
  (sanity check; these should be clear non-matches).
- `README.md` — text-only reading guide for terminals.

---

## 7. Tuning workflow

### Without labels (current state)

The diagnostics dashboard is the primary tuning surface. Open
`dashboard.html` and:

1. **Look at the score distribution.** Are there natural valleys
   between clusters of bars? The threshold should sit IN a valley.
2. **Look at top-of-review-band waterfall.** If most of these pairs
   look like real matches with one weak feature dragging them down,
   lower `threshold_high`. If most look noisy, leave it.
3. **Look at lowest-scored waterfall.** These should all look like
   clear non-matches. If a real match appears, something's wrong with
   the model.
4. **Edit `config/linkage_settings.json`** to change thresholds.
5. **Re-run Stage 3** (`python src/stage_3_evaluate.py`). Stages 0-2
   don't need to be re-run since the model and scored pairs haven't
   changed — only the bucketing changes.

### With labels (future, planned)

Once users start filling in `is_true_match = TRUE/FALSE` in
`matches_for_review.csv`, those labels become validation data. A
follow-on tool (not yet built; planned as part of the UI integration)
will:

- For each candidate `threshold_high` value, count how many of the
  TRUE-labelled pairs would be auto-accepted (recall) and how many of
  the FALSE-labelled pairs would be auto-accepted (precision loss).
- Plot precision-recall curves vs threshold.
- Recommend a threshold that hits a chosen precision target.

This is the single biggest unlock for tuning. With ~50-100 labels
across the probability range, threshold setting becomes data-driven
instead of visual.

A further extension (also planned, not built): supply the labelled
pairs to Splink as training data and skip the EM bootstrap. Splink
supports this directly. Once you have ~500 labels, this gives more
accurate `m` values than EM.

### Tuning the rules

Editing `name_rules.json` or `jurisdiction_map.csv` requires re-running
from Stage 0 (the cleaned names change). The standardisation report at
`output/standardisation_report.txt` shows samples of what each rule did,
which is useful for spotting over-aggressive rules.

When adding new rules, always re-run on the full data and inspect the
new diagnostics — a rule change can shift hundreds of pairs between
buckets unexpectedly.

### Tuning the Splink config

`linkage_settings.json` exposes:
- **`probability_two_random_records_match`** — the Bayesian prior. If
  the absolute probabilities feel too low, raise this. If they feel too
  generous, lower it. The relative ranking of pairs is unaffected by
  this knob; only the absolute scores shift.
- **JW thresholds** — adding more levels (e.g. 0.99, 0.97, 0.95, ...)
  gives finer probability discrimination at the top end. Removing
  levels loses information.
- **Blocking rules** — current rule blocks on jurisdiction. Tighter
  blocking (e.g. jurisdiction + first 4 chars of name) reduces pair
  count further but risks missing matches whose names start
  differently. We tried this and reverted because the two-phase split
  achieved the same memory reduction without losing recall.

---

## 8. Match-rate expectations and limitations

### Current state on the sample data

- Total OCOD title rows matched: **~88.5%** (80,805 / 91,329)
- Of which:
  - Phase 1 (exact deterministic): 87.6%
  - Phase 2 high-confidence (probabilistic): 0.9%
- Unmatched: 4,668 deduped OCOD records, 7,297 ROE entries

### Why the 11.5% unmatched

This is mostly **not a matching problem** — it's a data problem. The
overseas entities that own UK property but don't appear in the matched
output are mostly entities that **haven't registered on the ROE**.
Registration is required by law but the register is incomplete.
Penalties for non-registration are still being enforced. Some entities
that did register did so with substantially different names than the
ones recorded in OCOD (which OCOD itself records from old paperwork).

Matching improvements would gain maybe +1pp on match rate. Going from
88.5% to ~95+% requires the registry itself to fill in.

### Known false positives in high-confidence

The current model has known precision issues in the Phase 2 band:

1. **Alphanumeric-suffix variations** — e.g. `ACRIS COURT 27A LTD` vs
   `ACRIS COURT 27B LTD`. These score equally high (both share digit
   set `{27}`, both have very similar names) but are different
   entities. The pipeline currently surfaces these as `is_ambiguous`
   with `match_count = 2` and lets the user disambiguate.
2. **Numeric-suffix variations** when the digit-set comparison is
   defeated — e.g. `BRINDLEY 5 SARL` vs `BRINDLEY 3 SA RL`. With the
   digit-set comparison this case now scores LOW correctly, but if
   names share OTHER digits the comparison can be defeated.

The ambiguous-CSV workflow exists specifically to handle these.

### Address fields are deliberately not used

See Section 2 quirks. We confirmed empirically that postcode and
post_town overlap is low for non-Crown-Dependency jurisdictions and
adds little signal even where it is moderate. The address fields ARE
exported in the merged dataset so the user can see both sides
side-by-side after matching.

---

## 9. Use cases / flows

The pipeline supports two practical flows. Both share the same stages;
they differ in whether the human-in-the-loop steps are exercised.

### Flow A — Quick reconcile

Run pipeline → consume `merged_dataset.csv` directly.

Used for routine data refreshes when the user trusts the existing
configuration and doesn't need to inspect borderline cases. About 88%
of OCOD titles get an OE number; the rest are blank (unmatched). User
exports the merged dataset and moves on.

### Flow B — Tune

Run pipeline → open the diagnostics dashboard → work through the
review queue and ambiguous queue → re-run Stage 4 to incorporate
decisions → re-export.

Used when:
- New data exposes new edge cases not in the rules
- Match rate is lower than expected for a particular jurisdiction
- Borderline pairs need authoritative T/F decisions
- Thresholds need adjustment based on the score distribution

### Re-running on updated data

Every month-ish the user gets a new OCOD release and a new CH bulk
snapshot. The workflow:

1. Replace the zips in the project folder
2. Run Stage 0. If new jurisdiction values appear, it halts and lists
   them
3. Update `jurisdiction_map.csv` (only need to add the new values)
4. Run all stages

The configuration files (rules, maps, thresholds) carry across runs
unless the user explicitly edits them.

### Persistent labels across re-runs (planned)

Currently `matches_for_review.csv` is regenerated on each run, and any
human decisions in `matches_for_review_reviewed.csv` are applied via
Stage 4. But there's no persistence across DATA UPDATES — when the
OCOD/CH zips change, the unique IDs change, and old reviewed CSVs
can't be applied directly.

The UI adds a label store (database, not CSV) keyed **raw-first** — the
raw name + jurisdiction + ROE company number are the durable identity,
with the cleaned key as a fallback — so labels survive data updates AND
cleaning-rule changes, re-applying automatically when the same OCOD
entity reappears in a new release. (Storing only the cleaned key was the
original plan, but a cleaning-rule edit could then silently orphan a
label; raw-first keying via the shared `label_resolver` closes that gap.)

---

## 10. What the UI on top of this needs

The UI is being built separately. This pipeline is the engine; the UI
is the cockpit. Key requirements the UI must satisfy (and the pipeline
must support):

### Label persistence

A database (SQLite to start) of labels keyed by entity identity,
preserving:
- `(ocod_name_raw, ocod_jurisdiction_raw, roe_company_number)` as the
  **durable** join key, with `(ocod_name_clean, jurisdiction_clean, …)`
  as a fallback. Raw values are the durable identity: cleaning rules are
  editable config, so a stored cleaned key can go stale, but the raw
  source values never change for the same records. Resolution therefore
  tries the raw key **first** (shared `label_resolver`, used by display,
  export and GBT training alike so they can never disagree)
- `is_true_match` (TRUE/FALSE/UNCERTAIN)
- `reviewer` (who)
- `timestamp` (when)
- `reviewer_notes`
- `run_id` it was first labelled in
- Whether currently active (allows soft-delete and version tracking)

On each pipeline run, Stage 4 (or its UI equivalent) reads this store
and applies labels by key. Labels where the underlying entity no longer
appears in the data are kept but flagged as "not currently applicable".

### Editable configuration

The four config files should be editable in the UI with validation:

- **Name rules**: regex compile check; live "test on this name" input
  showing the cleaned output as the user types
- **Jurisdiction map**: filterable table with counts of records using
  each raw value (so the user can see "this value has 0 records, it
  was old data"); validation that no raw value appears twice
- **Legal entity tokens**: simple list; warning if a token is too
  short / could conflict
- **Linkage settings**: form view with explanations of each knob

Every save creates a new config version with a changelog entry. The UI
should show which run used which version, and a diff view across
versions.

### Observability

Every action attributable. Every config change recorded. Every label
has a user + timestamp + notes. The UI should expose:

- Labels page: every label ever made, by whom, when, in which run,
  whether currently applied
- Audit page: every config change, threshold adjustment, run trigger
- Per-match traceability: clicking a match should show its history
  (was it ever labelled? was it ever in a different bucket?)

### Two flows in the UI

- **Quick**: upload + run + download merged dataset. No human review
  surface needed for runs where the user trusts the existing model.
- **Tune**: upload + run + diagnostics dashboard + review queue +
  ambiguous queue + re-export. The full curation experience.

Same pipeline; different exposure of intermediate surfaces.

### Specific UI screens that map to pipeline outputs

| UI screen | Backed by |
|---|---|
| Runs list | metadata table of past pipeline executions |
| Run detail / diagnostics | the HTML diagnostic files this pipeline emits, plus the model JSON |
| Review queue | `matches_for_review.csv` rows, joined with label store |
| Ambiguous queue | `matches_ambiguous.csv` rows |
| Merged dataset preview | `merged_dataset.csv` |
| Unmatched view | `unmatched_ocod.csv` / `unmatched_roe.csv` |
| Configuration | the four config files, with versioning |
| Labels / audit | the label store + audit table |

---

## 11. Decisions log — things we tried, considered, or deliberately did NOT do

### Used (in pipeline today)

- Two-phase split (Phase 1 exact then Phase 2 Splink): keeps Splink's
  pair count manageable (~4M vs ~840M) and gives Splink cleaner
  training data.
- Multiple Splink comparison columns (`name_clean`, `name_core`,
  `name_tokens_sorted`, `name_digits_sorted`): gives EM enough
  constraints to converge to sensible m values. Each was added in
  response to a concrete failure mode found in the review data.
- UTF-8-BOM CSV output: Excel auto-detects UTF-8 with the BOM.
- Comprehensive jurisdiction map: 200+ OCOD values mapped explicitly,
  pipeline halts on unknown values to force the analyst to add new
  ones rather than silently drop records.
- Ambiguous-cases CSV: separates "model is uncertain" (top vs second
  probability gap < 0.05) from "model is confident but wrong".

### Considered and rejected

- **Address features for matching** — ROE and OCOD addresses describe
  different things by design (registered vs service). Empirical
  agreement is poor (27% postcode, 20% strong full-address Jaccard).
  Post_town is essentially redundant with jurisdiction blocking.
  Decision: don't use addresses for matching; do include both in the
  merged-dataset output.
- **Historical names from CH** — bulk CSV has the columns but they're
  empty for all 30,221 OE entries. Would require per-company CH API
  calls, which is out of scope here. Decision: skip.
- **Tighter blocking (jurisdiction + name prefix)** — initially tried
  `(jurisdiction, first_4_chars_of_name)`. Rejected because the
  two-phase split achieved the same memory reduction without
  introducing recall risk from name-prefix variation.
- **Embedding-based matching** — overkill for structured company
  names; less interpretable than Splink. Decision: stick with Splink.
- **Single Splink stage on the full data** — Splink would have to
  handle ~100M+ pair comparisons. Memory/disk-intensive on a typical
  laptop. Decision: two-phase split.

### Planned but not built

- **Label-driven threshold tuning** — once labels accumulate, compute
  precision/recall at each threshold value. Recommend optimal
  threshold for a given precision target.
- **Label-driven Splink training** — supply labelled pairs to Splink
  as training data; skip the EM bootstrap. Splink supports this
  natively.
- **API-based PSC enrichment** — query CH API per matched OE number
  for beneficial ownership data. Out of scope for THIS pipeline but
  the natural next step in the user's broader workflow.

---

## 12. Technical specifics for the implementer

- **Python 3.10+**
- **Dependencies**: `splink>=4.0`, `duckdb`, `pandas`, `pyarrow`,
  `tqdm`, `altair` (transitive via Splink). See `requirements.txt`.
- **Memory**: Stage 0 uses ~200MB peak (chunked CH read). Stage 2
  capped at 4GB via DuckDB pragma. Stages 1/3/4 use < 1GB.
- **Disk**: ~50MB of output parquet/CSV files. DuckDB temp directory
  is `output/duckdb_tmp/` (under our control, not /tmp). On the
  sample data it peaks at < 1GB.
- **Time**: full pipeline runs in ~90 seconds on a modern laptop
  (mostly Stage 0's CH CSV streaming).

### File layout

```
.
├── config/
│   ├── jurisdiction_map.csv
│   ├── legal_entity_tokens.json
│   ├── linkage_settings.json
│   └── name_rules.json
├── docs/
│   ├── PIPELINE_CONTEXT.md          (this file)
│   └── superpowers/specs/2026-05-19-ocod-roe-linkage-design.md
├── src/
│   ├── standardise.py               (shared cleaning helpers)
│   ├── stage_0_preprocess.py
│   ├── stage_1_exact_match.py
│   ├── stage_2_probabilistic_link.py
│   ├── stage_3_evaluate.py
│   └── stage_4_apply_reviews.py
├── output/                          (gitignored; regenerated each run)
│   ├── *.parquet
│   ├── *.csv
│   ├── splink_model.json
│   ├── standardisation_report.txt
│   └── diagnostics/
│       ├── dashboard.html
│       ├── score_distribution.html
│       ├── match_weights.html
│       ├── m_u_parameters.html
│       ├── waterfall_*.html
│       └── README.md
├── run_all.py
├── requirements.txt
├── BasicCompanyDataAsOneFile-YYYY-MM-DD.zip      (input — not committed)
└── OCOD_FULL_YYYY_MM.zip                          (input — not committed)
```

### How to run

```bash
# First time: install deps
pip install -r requirements.txt

# Drop the two source zips in the project root, then:
python run_all.py

# Iteration: edit config, then re-run just the affected stages:
python src/stage_3_evaluate.py            # threshold changes only
python src/stage_2_probabilistic_link.py  # model / comparison changes
python src/stage_0_preprocess.py          # name rules / jurisdiction map changes

# After reviewing matches_for_review.csv:
#  1. Save reviewed version as matches_for_review_reviewed.csv
#  2. Run stage 4
python src/stage_4_apply_reviews.py
```

### API surface the UI will need

The UI will eventually wrap this pipeline as a service. Minimal API:

- `POST /runs` — accepts OCOD and CH zip uploads, kicks off pipeline,
  returns run ID
- `GET /runs` — list of past runs with summary stats
- `GET /runs/{id}` — full run detail incl. diagnostics paths
- `GET /runs/{id}/matches?bucket=review|ambiguous|...` — paginated
  match listings
- `POST /labels` — submit a TRUE/FALSE decision on a match
- `GET /labels` — query the label store
- `GET /config/{kind}` — read current config file
- `PUT /config/{kind}` — save new config version (with audit entry)
- `GET /config/{kind}/history` — version history with diffs
- `GET /audit` — audit log

The pipeline itself doesn't expose an API today; it's a CLI. Wrapping
it in FastAPI is one of the next steps.

---

## 13. Out of scope (for this pipeline)

These are real follow-on needs that are deliberately not in this
pipeline:

- Companies House API calls for PSC / beneficial owner data
- Historical name lookup (would require per-company CH API calls)
- Cross-jurisdiction entity resolution (e.g. linking the OE-numbered
  ROE entry to the entity's own home-jurisdiction filings)
- Multi-tenant access controls (the UI handles this; the pipeline is
  single-tenant)
- Real-time matching (the pipeline is batch; runs in 90 seconds on
  this scale)
- Proprietors 2-4 in OCOD (the pipeline only uses proprietor 1; the
  cleaning code is shaped to extend to others by adding columns)

---

## 14. Open questions / future work

Beyond what's listed in the planned section above, things that would
require explicit design decisions if pursued:

- **Cross-jurisdiction matching**: what if an OCOD entity in
  "DELAWARE, U.S.A." actually appears in ROE under a different
  jurisdiction? The current pipeline only matches within blocked
  jurisdictions. Relaxing this is doable but expensive.
- **Confidence interval on m/u**: Splink supports running EM multiple
  times with different starts to get parameter confidence. Could be
  added as a diagnostic.
- **Active learning**: prioritise which pairs to label first to
  maximise threshold-calibration value per label. Requires the label
  store first.
- **De-duplication of OCOD ENTITIES**: currently we dedup by
  `(name_clean, jurisdiction_clean)`. Some entities appear under
  multiple cleaned names in OCOD (because the original recording
  varied). Detecting these would let you collapse multiple OCOD
  entries into one entity-level record. The ROE-side multi-match
  cases (one ROE → multiple OCOD records) are hints at where this
  matters.

# PSC handover — slices 8a and 8b

Written 2026-09-18 at the end of the session that built the PSC profile and
began making the pipeline out-of-core. Fact, not plan.

## 1. Running PSC outside the web server

Use a scratch `DATA_DIR`. Never `backend/data` — the donations dev server on
port 8100 owns it.

```bash
cd /home/tomwright/PycharmProjects/dedupe_ui/backend
export SP=/tmp/psc-scratch                  # any empty directory
export PROFILE=psc SITE_PASSWORD=x PYTHONPATH=$PWD DATA_DIR=$SP/pscdata
```

Sample: `/home/tomwright/PycharmProjects/dedupe_final/psc-snapshot-2026-09-18_1of32.zip`
(500,000 records). Full: `/mnt/c/Users/TomWright/Downloads/persons-with-significant-control-snapshot-2026-09-18.zip`.

```python
run = f"{DATA_DIR}/runs/psc_sample"; config = f"{run}/config"
# copy app/profiles/defaults/psc/{ruleset,linkage_settings}.json into config/ first
from app.pipeline.dedupe import (stage_0_load, stage_1_clean, stage_2_exact,
                                 stage_3_score, stage_4_cluster, stage_5_entities)
stage_0_load.run_stage_0_load(run_dir=run, input_path=ZIP)
stage_1_clean.run_stage_1_clean(run_dir=run, config_dir=config)
stage_2_exact.run_stage_2_exact(run_dir=run, config_dir=config)
stage_3_score.run_stage_3_score(run_dir=run, config_dir=config)
stage_4_cluster.run_stage_4_cluster(run_dir=run, config_dir=config)
stage_5_entities.run_stage_5_entities(run_dir=run)
```

Env knobs: `CLEAN_BATCH_ROWS` (500,000), `PSC_QUICK_ROWS` (200,000),
`PSC_DUCKDB_MEMORY` (2GB), `PSC_DUCKDB_THREADS` (2).

Run it in the background; it is finished when `pgrep -f <script>` prints nothing
and the log ends with the stage 5 line. **My scratch directory was session-local
and is gone — re-run.** The re-score with the fixed DOB settings was still
training when I stopped, so **no numbers from it are here**.

### Reading a finished run

```python
import json, math, pandas as pd
pairs = pd.read_parquet(f"{run}/pairs.parquet")
print(pairs.groupby(["track", "score_bucket"]).size())            # by bucket

for c in json.load(open(f"{run}/splink_model_person.json"))["comparisons"]:
    for lv in c["comparison_levels"]:                              # learned weights
        mp, u = lv.get("m_probability"), lv.get("u_probability")
        print(c["output_column_name"], lv.get("label_for_charts"), mp, u,
              math.log2(mp / u) if (mp and u) else None)

units = pd.read_parquet(f"{run}/units.parquet").set_index("unit_id")
sub = pairs[(pairs.track == "person") & (pairs.score_bucket == "accept")]
for _, r in sub.nlargest(10, "match_probability").iterrows():      # ten examples
    a, b = units.loc[r.unit_id_l], units.loc[r.unit_id_r]
    print(r.match_probability, a.forename_canon, a.surname_clean, a.dob_year,
          "|", b.forename_canon, b.surname_clean, b.dob_year)
```

Swap `person`/`accept` for the other track and `review`.

## 2. Three designs to keep

**FIFO loader.** `zipfile` decompresses the member in a thread into a FIFO;
DuckDB reads it as newline-delimited JSON and writes parquet. Chosen over
`/dev/stdin` so decompression stays in-process (no `unzip` dependency) and the
13 GB member never touches disk. The schema is explicit
(`columns={'company_number':'VARCHAR','data':'JSON'}`), so a key missing from
the first rows cannot change how the rest is read. **Each side opens the FIFO
once and reads to EOF** — I tried reopening it per batch and it deadlocks, the
writer blocking for a reader DuckDB has already finished being.

**Batch cleaning (stage 1).** Reads `records_raw.parquet` in `CLEAN_BATCH_ROWS`
batches through one incremental `ParquetWriter`; one file, so no reader changed.
**The Arrow schema comes from the ruleset, not the first batch**
(`written_columns()`) — otherwise a batch with one track empty writes a
different schema mid-file. Sound because every engine op is row-independent.
`record_id` uniqueness moved to a DuckDB `GROUP BY`. Measured 1,183 MB peak
against 2,619 MB, same wall time, output identical column-for-column.

**Path-returning loader.** `load_records`/`load_events` may return a pandas
frame **or a path to a parquet the profile already wrote**. Stage 0's `_place()`
takes either; `validate_records_file()` checks a file in DuckDB. PSC returns
paths and projects its events out of the records parquet in SQL; donations still
returns frames. Stats must be identical either way.

## 3. The EM lesson and the two guards

Splink **cannot estimate a comparison whose column every `em_blocking_rules`
entry holds equal**: no disagreement inside the training block to learn from, so
the level comes back `m = None` and contributes **zero**, silently. PSC person
shipped that way — the date of birth counted for nothing and pairs 37 birth-years
apart scored 1.0000. PSC organisation had the same fault on `type_bucket`. Fix:
two training rules per track fixing different columns. Both tracks now have it;
donations was checked and is clean.

- `linkage.linkage_warnings()` flags it at save time; `POST /api/config/validate`
  returns `warnings` beside `errors`.
- `stage_3_score.untrained_levels()` / `inspect_trained_model()` flag it after
  EM: `untrained_comparisons` in `blocking_report.json`, the run count
  `untrained_comparisons` (`untrainedComparisons` in the API), and a `warning`
  progress event.

## 4. What remains

- **B5, the rest.** Full-frame `pd.read_parquet` in `stage_4_cluster` (units,
  members, pairs, groups), `stage_5_entities` (clusters, members, records),
  `psc_export`.
- **The units corpus read** in `pairs_reader.model_explanation`. The pair and
  events reads are now by key in DuckDB; `units` is still whole, because the
  organisation feature builder fits TF-IDF over every unit and filtering it
  would change a number donations reviewers already see. Proposed fix: a profile
  flag saying whether its builder needs a corpus, plus **persisting the fitted
  IDF at scoring time** so a one-pair explanation reuses it instead of refitting.
- **B3** projection-only stage 2 proof at 16M. **B4** `build_units` at 16M — the
  per-column modal vote may need narrowing to the columns Splink, the features,
  the evidence focus and the display actually use. **B6** full-scale blocking
  pricing, proposed budget, and deduping's hot-key tightening (refine blocks over
  60 with the forename initial, drop over 200). **C** the full-scale run of
  stages 0–2 plus units, stopping below 15 GB free disk.
- **The junk-postcode and placeholder-number token lists** in
  `defaults/psc/ruleset.json` came from the 500,000 sample (18 postcodes at
  deduping's >5,000-per-7.5M proportion, threshold 333). **Rebuild from the full
  snapshot** or a full run means nothing.

## 5. What surprised me

- The dead comparison was invisible. Nothing failed, the run completed, the
  numbers looked plausible. I found it only by printing m and u per level after
  a human read of the example pairs looked wrong. **Read the examples.**
- Postcode learned +13.4 bits on the PSC person track, more than an exact
  surname match — which is why the review queue filled with siblings at one
  address. deduping's D7 warned about exactly this and its own later code
  re-added postcode anyway. Treat that history as unresolved, not settled.
- Donations person `surname` "all other" carries only −1.06 bits (m = 0.479),
  because one training block is forename-only. That is the likely cause of the
  0.70–0.80 pile nobody could explain. I did not change it: it needs a run
  measured against the baseline (person P 0.9872 R 0.6236).
- I would default `CLEAN_BATCH_ROWS` lower than 500,000. The batch is the memory
  ceiling and 100,000 cost nothing measurable.

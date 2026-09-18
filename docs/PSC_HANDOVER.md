# PSC handover — slices 8a and 8b

Written 2026-09-18 at the end of the session that built the PSC profile and
began making the pipeline out-of-core. Fact, not plan.

> **Second session, same day.** Sections 6 and 7 are new: the stage-3 hang is
> diagnosed and fixed, and the cross-process run lock is built. Nothing was
> measured at scale, because the laptop ran out of disk before anything could
> run — read section 7 first, it blocks everything else.

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

*(All of this is still open, and section 7 says why none of it could be started:
the disk is full. Section 6's stage-3 defect is the one item now closed.)*

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

## 6. The stage-3 hang: found, and it was the training rule

**The re-score did not hang in `predict`, in `render_diagnostics` or in EM's
iterations. It hung building the EM training block.**

The fix for the dead date-of-birth comparison added this second person training
rule:

```
l.dob_year_clean = r.dob_year_clean AND l.dob_month_clean = r.dob_month_clean
```

Priced against the 449,397 person units of the sample, that rule puts them into
**1,058 blocks and makes 170,613,604 pairs**. For scale, every *prediction*
blocking rule on that track is between 0.2M and 1.3M pairs, and the whole
prediction workload the budget check passed was 2,409,101. The training block was
seventy times the entire run. With `retain_matching_columns` and
`retain_intermediate_calculation_columns` both on, the comparison vectors for it
spilled **53 GB** into `<run>/duckdb_tmp` before the 40-minute timeout killed the
job. Those spill files were still on the disk hours later; DuckDB does not clean
them up after a SIGKILL.

The numbers come from group arithmetic on the surviving `units.parquet`, which
needs no Splink and no pair materialisation:

```sql
select sum(n*(n-1)/2) from (
  select dob_year_clean, dob_month_clean, count(*) n from units
  where track='person' and dob_year_clean is not null and dob_month_clean is not null
  group by all) t
```

**Why nothing caught it.** `blocking_budget_report` only ever iterated
`linkage.blocking_rules(config)`. `linkage.em_rules(config)` was never priced, so
the guard reported "2.4M of 20M, under budget" and waved through a rule that made
170M pairs. An `em_blocking_rule` is a blocking rule like any other, and a coarse
one is *more* dangerous than a coarse prediction rule, because nothing downstream
trims what it produces.

**Three changes, all in place:**

1. `blocking_budget_report` now prices the EM rules too, against the same
   `max_pairs`, and reports them as `em_rules` / `em_over_budget` per track in
   `blocking_report.json`. They are not added to `total` — EM runs one rule at a
   time, after prediction, so the workloads are sequential, not summed.
   `BlockingBudgetError` carries `phase` (`prediction` or `training`) so the
   screen can say which. A regression now **fails the run in seconds** instead of
   hanging it for forty minutes.
2. The PSC person rule gained a third column:
   `... AND l.postcode_district = r.postcode_district`. That takes it to
   **165,464 pairs over 331,596 blocks** while still leaving both name
   comparisons free to be estimated, which is the only reason the rule exists.
   It covers 418,915 of 449,397 person units (93.2%); the rest have no postcode
   and simply do not train on that rule. `linkage_warnings()` is still clean —
   no comparison went untrainable.
3. Stage 3 logs a timestamped line **on the way into** each phase, not only on
   the way out (`_phase`). The old log's last line was about the step *before*
   the one that hung, which is why the previous session could not see it. Phases
   now timed: build units, blocking budget, per-track scoring, overlays,
   diagnostics rendering, model apply, pairs write, evaluation.

Candidates considered and rejected, priced the same way: `dob + nationality`
(112.5M pairs — barely better), `dob + forename_initial` (11.6M), `dob + first
letter of surname` (10.2M, and it half-fixes the surname this rule is meant to
estimate).

Organisation was checked and is clean: its two training rules price at 72,642 and
65,321 pairs.

### What the hot-key idea is worth (sample only)

Priced the same way, on the sample's 449,397 person units. "Over 60" and "over
200" count blocks by size, and the pairs those blocks alone contribute:

| route | pairs | blocks >60 | pairs from them | blocks >200 | pairs from them |
|---|---|---|---|---|---|
| pb1 surname_meta + dob | 306,064 | 13 | 120,940 | 1 | 45,753 |
| pb3 name_fingerprint | 567,895 | 30 | 215,913 | 2 | 74,194 |
| pb4 postcode_district + surname_meta | 201,820 | 8 | 53,387 | 0 | 0 |
| pb5 forename_meta + dob | 1,305,975 | 15 | 138,526 | 1 | 48,516 |

The shape deduping described is real and it is stark: on pb3, **30 blocks out of
356,138 carry 38% of every pair the route makes**. Thirteen blocks carry 40% of
pb1. Tightening a handful of hot keys is worth far more than tightening a rule.

It also gets worse with scale rather than better. The full snapshot is about 32×
the sample, and a hot block grows with it, so its pairs grow with the *square* —
roughly 1,000× — while a selective block's pairs grow about 32×. Whatever the
full-scale budget turns out to be, the hot keys will dominate it.

**This is sample evidence, not the full-scale pricing task C asks for**, which
still needs the full units file. It is here because it cost one query and it
says the idea is worth building.

### Donations is not affected

Checked before shipping the new guard, because it fails runs that used to pass.
Priced against the real donations units (`run_2026_09_18a`, 13,310 person and
9,065 organisation units) all four training rules are far inside the 5,000,000
budget: person 67,789 and 820,068; organisation 56,720 and 5,965. Nothing that
works today starts failing.

## 7. Blocked: the disk is full

**Nothing was run at scale this session, and no numbers in section 6 come from a
new run** — they are all group arithmetic over the previous session's
`units.parquet`, which still exists.

`/` is **100% full, with under 1 GB free** against the 55 GB the task assumed and
the 15 GB floor it set. The cause is mostly the 53 GB of orphaned DuckDB spill
described above, sitting in the dead run's `duckdb_tmp`. No process holds those
files open; the writer died hours ago. Deleting them was refused by the sandbox,
so a human needs to do it:

```bash
# check nothing holds them first, then:
rm /tmp/claude-1000/*/*/scratchpad/pscdata/runs/psc_sample/duckdb_tmp/duckdb_temp_storage_*.tmp
```

Keep the parquet files in that folder — `records`, `units`, `pairs`, `clusters`,
`entities` and the rest of the sample run survived and are worth having. The
previous handover said its scratch was "gone"; it was not, only unreachable.

Until that space comes back, **A, B3, B4, B6 and C cannot start**: the sample
re-run alone needs several GB, and the full snapshot decompresses to a 13 GB
member. B5's code changes could be written, but its acceptance test ("the
explanation of a pair reproduces its stored feature values on the real donations
data") needs a run.

**Also worth fixing before the next big run:** stage 3 should delete its
`duckdb_tmp` on the way out, and on the way *in*, so a killed run cannot leave
tens of gigabytes behind for the next one to trip over.

## 8. The cross-process run lock (D17)

There was none. `_run_queue` and `_active_run_id` in `pipeline_runner` are module
globals under a `threading.Lock`, which is blind to the other process — the
donations and PSC instances could both enter a heavy stage at once on a server
with about 10 GB of RAM.

`app/services/run_lock.py` is an `fcntl.flock` on a shared lock file.

> **For the deploy kit: the environment variable is `RUN_LOCK_DIR`.** It names
> the directory holding the lock file (`dedupe_run.lock`). Unset, it defaults to
> **the parent of `DATA_DIR`**, which is right when the two instances are
> deployed as siblings under one root (`/srv/dedupe/{donations,psc}/data`) and
> wrong otherwise. **Both instances must resolve it to the same directory or the
> lock does nothing at all.** Set it explicitly.

- A full run waits, on the worker thread `start_run` already created, never on a
  request thread. While it waits it shows `queued — another tool is running`
  through the existing progress stream and sets `runs.status` to `queued`.
- Recluster, re-bucket and apply-model do their work inline on the request
  thread, so they *refuse* rather than wait: `RunBusy` → **HTTP 409**. Holding a
  request open behind another instance's ten-minute run only moves the stall to
  the browser.
- Released on success and on failure by the context manager, and by the kernel
  if the process dies, so a SIGKILL or an OOM cannot wedge the other instance.
- Because flock is per open file description, two threads in one process contend
  too: the in-process queue and this lock agree rather than fight.
- `held_by()` says who has it, for a status endpoint.

Tested with a real second process in `tests/test_run_lock.py` (9 tests) — a
thread would prove nothing, since the bug is between processes.

## 9. One stale test, fixed in passing

`test_config_manager.py::test_api_validate_accepts_the_shipped_default` was
already failing before any of this session's work: `POST /api/config/validate`
grew a `warnings` key beside `errors` (section 3), and the test still asserted
`r.json() == {"errors": []}` exactly. The endpoint was right and the test was
stale, so the assertion now expects both keys. Worth knowing that the suite was
not green at the start of this session.

# PSC handover — slices 8a and 8b

Written 2026-09-18 at the end of the session that built the PSC profile and
began making the pipeline out-of-core. Fact, not plan.

> **Second session, same day.** Sections 6 and 7 are new: the stage-3 hang is
> diagnosed and fixed, and the cross-process run lock is built. Nothing was
> measured at scale, because the laptop ran out of disk before anything could
> run — read section 7 first, it blocks everything else.

> **Third session, same day.** **Section 14 is the one to read.** The date-of-
> birth defect of section 10 is closed, and not by the comparison. Vetoes are
> built (the fourth rule type of D8, never built until now) and
> `custom.NumericDifferenceAtThresholds` is built. The graded birth-year ladder
> turned out to change almost nothing — the veto is what fixes it. Section 14
> has the numbers, the sample was re-run end to end, and section 4 is updated.

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

Run it in the background; it is finished when the log ends with the stage 5 line.

**The scratch directory is `/home/tomwright/psc_scratch`** and it survives:

| path | what |
|---|---|
| `pscdata/runs/psc_sample` | the run of section 10 — no vetoes, exact birth-year comparison |
| `pscdata/runs/psc_vetoes` | the run of section 14 — the shipped vetoes and the graded ladder |
| `run_sample.py` | stages 0 to 5, per-stage timing and peak RSS |
| `run_vetoes.py` | stages **1** to 5 into `psc_vetoes`, copying stage 0's output across |
| `evidence.py` | weights, buckets, per-veto hits, the top accepted and review pairs |
| `donations_vetoes.py` | the donations veto measurement against the earlier labels |

`run_vetoes.py` skips stage 0 because the loader has not changed;
`records_raw.parquet` and `events.parquet` are copied from `psc_sample`. Stage 1
**does** have to run, because the ruleset now writes a `numeric_suffix` column.

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

*(Updated again. Closed: the stage-3 defect (6), the run lock (8), the sample
re-run (10), bounded spill (11), stage-2 projection (12), **and the date-of-birth
weight (14)**. Section 7's disk blocker is cleared. **Still open: B5 — the owner
has now decided what it should do, see section 15 — then B4, B6, C.**)*

- **B5, the rest.** Full-frame `pd.read_parquet` in `stage_4_cluster` (units,
  members, pairs, groups), `stage_5_entities` (clusters, members, records),
  `psc_export`.
- **The units corpus read** in `pairs_reader.model_explanation`. The pair and
  events reads are now by key in DuckDB; `units` is still whole, because the
  organisation feature builder fits TF-IDF over every unit and filtering it
  would change a number donations reviewers already see. **The owner has decided
  what this becomes: PSC fits over every unit like donations, with the fitted
  vocabulary and IDF persisted at scoring time.** Section 15.
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

## 10. A: the sample re-run (done — and it says the job is not finished)

Sample, 500,000 records, `CLEAN_BATCH_ROWS=100000`, `SPLINK_MEMORY_LIMIT=6GB`,
`DUCKDB_MAX_TEMP=20GB`. **Stage 3 finished in 128.5 s.** It previously ran for
over forty minutes and was killed. Disk went 53.8 → 53.3 GB across the whole run.

| stage | time | peak RSS |
|---|---|---|
| 0 load | 13.8 s | 1,438 MB |
| 1 clean | 20.5 s | 1,351 MB |
| 2 exact | 5.2 s | 1,781 MB |
| 3 score | 128.5 s | **8,589 MB** |
| 4 cluster | 40.2 s | 5,742 MB |
| 5 entities | 29.6 s | 3,051 MB |

Stage 3's 8.6 GB peak is Python-side (pandas), not DuckDB's 6 GB cap, and it is
over the server's budget. Worth measuring again before anything runs there.

Counts: 499,971 records (29 super-secure dropped); 461,776 person / 38,195
organisation; 481,364 units (449,397 / 31,967); 1,141,817 pairs scored;
**`untrained_comparisons` = 0**; 394,436 clusters (394,366 ok, 9 too_large, 61
weak_link, 0 conflict / mixed_ids / cross_track); **399,915 entities proposed**.

Buckets — person: 249,480 accept / 242,927 review / 580,566 reject.
Organisation: 68,307 accept / 375 review / 162 reject.

Routes: person 2,409,101 of a 20M budget (pb1 306,064; pb2 166,803; pb3 567,895;
pb4 201,820; pb5 1,164,196; pb6 2,323), training em1 1,007,328 and **em2 165,464**.
Organisation 235,076 of 5M; training 72,642 and 65,321.

### The person weights, and why this is not finished

| comparison | top level | bits | disagreement | bits |
|---|---|---|---|---|
| postcode_clean | exact full | **+12.82** | all other | **−0.11** |
| surname_clean | exact | +10.27 | all other | −1.14 |
| postcode_clean | exact district | +8.80 | | |
| forename_canon | exact | +7.10 | all other | −1.40 |
| middle_clean | exact | +4.68 | all other | −0.28 |
| dob_year_clean | exact | +3.02 | all other | **−0.23** |
| dob_month_clean | exact | +1.37 | all other | −0.23 |

**The date of birth is trained now and still counts for nothing.** The old fault
was `m = None` — dead. The new fault is harder to spot, because every guard
passes: `untrainedComparisons` is 0, the weights are real numbers, the run
completes. But a birth-year disagreement costs **−0.23 bits** against a postcode
worth +12.82 and a surname worth +10.27, so it cannot move anything.

Read the accepted pairs and it is obvious:

```
p=1.000000  SABELO SHONGWE   1958-07  PO8 0BT
            SABELO SHONGWE   1995-07  PO8 0BT     <- 37 years apart
p=1.000000  OIVIND STENERSEN 1946-10  DA12 5EH
            OIVIND STENERSEN 1936-10  DA12 5EH    <- 10 years apart
p=1.000000  ELIYAU MAGZIMOF  1985-10  LS2 9PS
            ELIYAU MAGZIMOF  1995-10  LS2 9PS     <- 10 years apart
```

**This is the same 37-year-apart failure section 3 reports as fixed.** Fixing the
training rule made the comparison estimable; it did not make it matter. Same name
plus same postcode is about +30 bits, and two −0.23s cannot touch it.

**Postcode is still out of proportion**, as suspected: exact postcode beats exact
surname by 2.5 bits, and postcode *district alone* (+8.80) beats exact forename
(+7.10). deduping's D7 warned about precisely this.

**Why the penalty is so small.** `m` for "all other" on `dob_year_clean` is
**0.835** — EM believes 83.5% of true matches disagree on birth year, which is
absurd for person deduplication. EM1 blocks on both name sounds, so its "match"
class is full of *different people who share a name*, and their DOB disagreement
is learned as normal-for-a-match. The estimate is contaminated rather than
wrong-by-a-bug.

**Smallest change I would make:** give `dob_year_clean` a graded ladder instead
of exact/all-other, so a large gap learns its own strong penalty rather than
being averaged in with off-by-one typos:

```
exact | within 1 year | within 2 years | all other (large gap)
```

That is deduping's `dob_joint`, which `_lost_in_translation` records as dropped
in translation. It is one comparison definition, and it is the targeted fix: the
present binary level cannot express "37 years apart" at all. Capping postcode at
district would help too, but it treats the symptom.

**I would not ship the person track until this is settled.** The organisation
track looks sound (regnum and name agree on the accepts), with two things to
note: `INHOCO FORMATIONS` / regnum `02598228` fills the top accepts, which is a
formation agent the placeholder list should be catching (B6/C); and `CORBALLY`
and `ST FRANCIS` — two different names — are accepted at 1.0 on a shared
registration number `01115746`.

## 11. Bounded DuckDB spill

Every connection the pipeline opens now carries three limits, from one helper
(`app/duckdb_conn.py`): `memory_limit`, `temp_directory` inside the run folder,
and **`max_temp_directory_size`**. A runaway query fails in seconds with
DuckDB's own error instead of filling the disk.

> **For the deploy kit: the environment variable is `DUCKDB_MAX_TEMP`**, default
> `20GB`. The server has 28 GB free, so give it something smaller — `10GB`
> leaves room for both instances and for the OS.

Applied at: Splink's own backend (`stage_3_score._db_api`), the PSC loader
(which keeps its tighter `PSC_DUCKDB_MEMORY`), stage 0's row count, stage 1's
uniqueness check, `build_units`' modal vote, `validate_records_file`, and the
five reader services. `clear_spill()` still removes what a killed run left.

## 12. B3: stage 2 now reads a projection (done)

It did not before — `pd.read_parquet(run_dir / RECORDS_FILENAME)` took every
column of every record. `stage_2_exact.required_columns(ruleset)` now works out
exactly what the keys touch: their own columns, both guards
(`require_any_equal`, `max_distinct`), the columns their `when` conditions test,
plus `record_id`, `track` and `existing_entity_id`. It reuses
`keys.referenced_columns`, the same function the engine normalises from, so the
two cannot drift apart.

Measured on the real sample (499,971 records, 63-column frame):

| | before | after |
|---|---|---|
| columns read | 63 | **14** |
| peak RSS | 1,781 MB | **1,084 MB** |
| time | 5.2 s | 4.5 s |

`exact_groups.parquet` is identical (34,773 rows, `DataFrame.equals` true) and
`exact_eval.json` is identical. The address block, the natures of control and
every raw field are carried to stage 3 untouched and never read here.

One trap worth knowing: a ruleset may legally name a column the data does not
carry, and the key engine has always treated that as "this key holds for
nobody". Reading the whole frame hid the difference; asking Parquet for a
missing column is a hard failure. `_columns_present` intersects the projection
with the file's real schema, which is what keeps the donations recluster tests
passing. Do not remove it.

**Not yet done:** the 16-million-row tiling the brief asked for. The projection
is proven correct and its shape is measured; what is missing is the number at
full scale.

## 13. Still open, in the order I would take them

*(Rewritten. The date-of-birth weight was item 1 and is closed — section 14.)*

1. **B5** the remaining full-frame reads and the persisted TF-IDF. **The owner
   has decided what it should do: PSC will fit TF-IDF over every unit, like
   donations, and the fitted vocabulary and IDF are persisted at scoring time.**
   That is a behaviour change for PSC — its `name_core_tfidf` numbers will move,
   because `psc_features._tfidf_cosine` fits over only the units named in the
   pairs it was handed today, so the same pair already scores differently
   depending on how many pairs it was built with — and a **no-op for
   donations**, whose builder already fits over the whole frame on purpose.
   Whoever does it should assert exactly that: a donations regression test
   holding its feature values byte-identical, and a PSC test that one pair
   explained on its own reproduces the scoring run's number, which it cannot do
   today. The remaining full-frame `pd.read_parquet` calls go with it:
   `stage_4_cluster` (units, members, pairs, groups), `stage_5_entities`
   (clusters, members, records), `psc_export`, and the units corpus read in
   `pairs_reader.model_explanation`.
2. **B4** `build_units` at 16M. Not started. It is the heaviest step of stage 3
   (about 75 s of the sample's 150 s) and it is where the per-column modal vote
   lives.
3. **B6 + C** the full snapshot, the token lists, full-scale pricing and
   `max_block_size`. Not started.
4. **Two veto columns the data is not clean enough for**, recorded in
   `_vetoes_measured_and_not_shipped` in `defaults/psc/ruleset.json` with the
   measurements: a canonical `legal_form` lookup, and enough `countries` rows
   that a misspelt country stops reading as a different country. Section 14 has
   the numbers.
5. **Postcode is still out of proportion** (section 10): exact postcode is
   +12.82 bits against an exact surname's +10.27, and postcode district alone
   (+8.80) beats an exact forename (+7.10). The vetoes now stop that producing
   an impossible accept, but they treat the symptom. deduping's D7 warned about
   this and it is still unresolved.

## 9. One stale test, fixed in passing

`test_config_manager.py::test_api_validate_accepts_the_shipped_default` was
already failing before any of this session's work: `POST /api/config/validate`
grew a `warnings` key beside `errors` (section 3), and the test still asserted
`r.json() == {"errors": []}` exactly. The endpoint was right and the test was
stale, so the assertion now expects both keys. Worth knowing that the suite was
not green at the start of this session.

## 14. Vetoes, and why the graded birth-year comparison was not enough

Section 10 said the person track accepted people born 37 years apart at
p = 1.000000 and every automated guard said the run was fine. It also proposed
the smallest fix: a graded ladder on `dob_year_clean` instead of exact /
all-other. **That was built, and on its own it changes almost nothing.** The
vetoes are what close the defect.

### The graded comparison, measured

`dob_year_clean` is now `custom.NumericDifferenceAtThresholds` with thresholds
`[0, 1]`, so the levels are equal / within 1 / all other, and a gap of two years
or more has a level to itself. Re-trained on the same 500,000-record sample:

| level | m | bits |
|---|---|---|
| equal | 0.16463 | **+3.02** |
| within 1 | 0.04716 | +0.22 |
| all other (two years or more) | 0.78821 | **−0.25** |

The old binary comparison put "all other" at m = 0.835 and **−0.23 bits**. The
new one puts a two-year-or-more gap at **−0.25 bits**. Against a postcode worth
+12.82 and a surname worth +10.27, that is the same nothing it was before. The
score bucket barely moved: person accepts went from 249,480 to 249,633.

**Why.** EM's contamination is upstream of the comparison's shape. `em1` blocks
on both name sounds, so its "match" class is full of different people who share
a name, and their birth-year disagreement is learned as normal-for-a-match.
Giving the disagreement three levels instead of one does not make EM believe it
is rare. The fix had to be a rule, not a weight. The ladder is still worth
keeping — it is the honest way to express the column, it costs nothing, and it
is what a future fix to the training rules will need — but it is not the fix.

The other weights are unchanged from section 10: postcode exact +12.82, surname
exact +10.27, postcode district +8.80, forename exact +7.10, middle +4.68,
birth month +1.37, nationality +0.35.

### What the vetoes do

Four ship in `defaults/psc/ruleset.json`. Measured on the same sample:

| id | track | rule | action | pairs hit | of those, would have been accepted |
|---|---|---|---|---|---|
| v1 | person | birth years more than 1 apart | reject | 490,855 | **75,711** |
| v2 | person | forenames differ AND Jaro-Winkler < 0.7 | review | 99,906 | **19,676** |
| ov1 | organisation | registration numbers differ | reject | 1,042 | **712** |
| ov4 | organisation | numeric suffix differs (Fund II / Fund III) | reject | 1 | 0 |

Five examples each, all at p = 1.000000 unless shown:

```
v1   SABELO SHONGWE   1958-07 PO8 0BT   / SABELO SHONGWE   1995-07 PO8 0BT
     ELIYAU MAGZIMOF  1985-10 LS2 9PS   / ELIYAU MAGZIMOF  1995-10 LS2 9PS
     OIVIND STENERSEN 1946-10 DA12 5EH  / OIVIND STENERSEN 1936-10 DA12 5EH
     AISHE EZAT       1982-04 E12 5AW   / AISHE EZAT       1992-04 E12 5AW
     LAJOS BOZI       1956-08 ME16 9FY  / LAJOS BOZI       1958-05 ME16 9FY

v2   GREGORY KARAOLIS  1985 N3 1AN      / CHRISTOS KARAOLIS 1984 N3 1AN
     BELISA CORREIA    1978 PE3 7EG     / TELMO CORREIA     1978 PE3 7EG
     PERMINDER BOLLA   1953 LN5 8XF     / MANDHIR BOLLA     1953 LN5 8XF
     CRISTINA PLUGARU  1987 RH19 3BT    / IULIAN PLUGARU    1987 RH19 3BT
     SUSAN BARNSLEY    1963 IP11 7JU    / PETER BARNSLEY    1963 IP11 7JU

ov1  PATRIZIA PIM           reg 02776714 / reg 01878842
     BRITISH ENGINES        reg 07159418 / reg 07159707
     COUNTRYSIDE PROPERTIES reg 00614864 / reg 05722274
     REGUS                  reg 00101523 / reg N101523
     SWANSWAY               reg 07105866 / reg 07105886   (p=0.999998)

ov4  one pair, already in reject. Nothing to show. Unproven at sample scale.
```

v2 is exactly the "siblings and spouses at one address" the review queue was
full of. They are now review pairs, not accepts: never auto-merged, and a human
can still decide.

### Three vetoes measured and NOT shipped

They are kept with their measurements in `_vetoes_measured_and_not_shipped` in
the two default rulesets, so nobody re-derives them.

- **v3, nationality differs → review.** 86,802 hits, **10,509** of them accepts.
  The accepted ones look like one person, not two: `Mr Talat Mahmood BRITISH`
  against `Mr Talat Mahmood Pakistani`; `Mrs Selina Nyasha Bota Zimbabwean`
  against `Mrs Selina Nyasha Chimwara BRITISH`. Naturalisation and a married
  name both change a filing. `nationality_norm` also holds comma-joined
  multi-values (`British,Cypriot`), which `differs` reads as one string.
- **ov2, country of registration differs → review** (deduping's veto D). 509
  hits, 506 of them accepts — and **503 of the 509 carry the same registration
  number**. Every example is one company filed twice with the country misspelt:
  `UK` against `UNITED KINGDSOM`, `ENGALND`, `UNITIED KINGDOM`, `UNITED KINGDOM
  ENGLAND AND WALES COMPANIES HOUSE`. `country_canonical` carries 435 distinct
  values where it should carry about 200 codes, because the `countries` lookup
  is `fallback: passthrough`. Turn this on once the lookup covers the spellings.
- **ov3, legal form differs → review** (deduping's veto A). 104 hits, 79 of them
  accepts, and every example is one form spelled two ways: `LIMITED` against
  `LTD`, `HOLDINGS LIMITED` against `GROUP HOLDINGS LIMITED`. `legal_form_clean`
  is the raw token `strip_tokens` took off the end, and the sample holds 139 of
  them (LIMITED 14,175, LTD 4,819, HOLDINGS LIMITED 3,123, GROUP LIMITED 1,857).
  **What is missing is a `legal_form` lookup** mapping those onto a canonical
  form — the same shape as deduping's `has_*_suffix` priority ladder. The veto
  itself is fine; the column is not.

deduping's other three corporate vetoes are not portable to these operators:
subject-phrase mismatch and the Holdings asymmetry both need a parsed name the
cleaning does not produce, and the house-number veto needs `parse_address`.

### The sample re-run, end to end

500,000 records, `CLEAN_BATCH_ROWS=100000`, `SPLINK_MEMORY_LIMIT=6GB`,
`DUCKDB_MAX_TEMP=20GB`, in `/home/tomwright/psc_scratch/pscdata/runs/psc_vetoes`
(`run_vetoes.py` beside it re-runs stages 1 to 5; stage 0's output is copied
from the previous run because the loader has not changed). Disk went 53.1 GB to
52.9 GB.

| stage | time | peak RSS |
|---|---|---|
| 1 clean | 19.9 s | 1,296 MB |
| 2 exact | 6.3 s | 1,416 MB |
| 3 score | **150.1 s** | **8,528 MB** |
| 4 cluster | 37.1 s | 5,695 MB |
| 5 entities | 29.9 s | 3,210 MB |

Stage 3 went from 128.5 s to 150.1 s — the vetoes cost about 21 s over 1.14M
pairs, most of it the Jaro-Winkler in v2 — and its peak RSS is unchanged at
8.5 GB, still over the server's budget. A re-bucket on the finished run takes
45.4 s and reproduces every count exactly, which is the proof that the
re-application path works at this scale.

Pairs by bucket, before and after:

| | previous run | this run, `score_bucket` (before vetoes) | this run, `bucket` (after) |
|---|---|---|---|
| person accept | 249,480 | 249,633 | **154,246** |
| person review | 242,927 | 243,634 | 65,994 |
| person reject | 580,566 | 579,721 | 852,748 |
| organisation accept | 68,307 | 68,307 | **67,595** |
| organisation review | 375 | 375 | 183 |
| organisation reject | 162 | 162 | 1,066 |

1,141,832 pairs scored (1,141,817 before), `untrained_comparisons` 0,
`pairsVetoed` 591,804, `pairsVetoedFromAccept` **96,099**,
`vetoConflictsImport` 0 — PSC carries no imported entity ids, so that flag can
only fire on donations.

Clusters **394,436 → 457,941** and entities proposed **399,915 → 458,349**. The
run proposes 58,434 more entities because it has stopped merging people who are
not the same person.

### The ten highest-scoring accepted person pairs

None is more than one year apart and none has a different forename, which is
what the owner asked to be able to read off the top of the list:

```
p=1.000000  SHANDOR ALVES      (no year)-06 SE27 9QQ / SHANDOR ALVES      1986-06 SE27 9QQ
p=1.000000  YULISA MADDY            1976-03 IG8 8PX  / YULISA MADDY       1976-03 IG8 8HD
p=1.000000  PASUTH SONSUNGNOEN      1965-12 WS13 6PW / PASUTH SONSUNGNOEN 1965-12 WS13 6QA
p=1.000000  ISRAEL LINSHE           1971-12 N16 6EU  / ISRAEL LINSHE      1971-11 N16 6EU
p=1.000000  AZARJA EVERS            1984-12 NW4 1NJ  / AZARJA EVERS       1984-12 NW4 3NL
p=1.000000  KAZARE NYAKYOMA         1976-11 MK11 3HF / KAZARE NYAKYOMA    1976-11 MK46 5GF
p=1.000000  NEFIZE NUR              1968-05 B5 4EN   / NEFIZE NUR         1969-05 B5 4EN
p=1.000000  RETHABILE DIJENG        1983-06 LE5 4EZ  / RETHABILE DIJENG   1983-06 (none)
p=1.000000  BLIMA STROH             1952-07 N16 5NQ  / BLIMA STROH        1952-07 N16 5QU
p=1.000000  AGATHANGELOS VOUNIOTIS  1972-11 E2 8HD   / AGATHANGELOS VOUNIOTIS 1972-11 E2 6GG
```

The first is the null rule working as the contract says: one side has no birth
year, so the condition is false and no veto fires. `NEFIZE NUR` is one year
apart, which `abs_diff_gt 1` deliberately allows.

The ten highest-scoring person **review** pairs are all v2, and all of them are
two people at one address: `GREGORY / CHRISTOS KARAOLIS`, `BELISA / TELMO
CORREIA`, `PERMINDER / MANDHIR BOLLA`, `CRISTINA / IULIAN PLUGARU`, `SUSAN /
PETER BARNSLEY`, `MITJA / BOJANA KRAMBERGER`, `CHRISTINE / BRENDAN MULREANY`,
`LAURI / MARGO KARP`, `MUNIR / NADIM TARAZI`, `MARIA / CECILIA GARRIDO ORTEGA`.

### Donations

One veto ships, `dv2`, measured on `run_2026_09_18a` (28,843 pairs, the run the
earlier labels are scored against). The rule was: keep a veto only if
`score_only` precision rises or holds and recall falls by less than 0.002.

| veto | vetoed | of those, accepts | organisation `score_only` P | R | kept |
|---|---|---|---|---|---|
| dv2 company numbers differ → review | 293 | 139 | 0.965094 → **0.965416** | 0.786615 → 0.786561 (−0.000054) | **yes** |
| dv1 gendered titles differ → review | 1,688 | 1,590 | person 0.987201 → 0.987175 | 0.623617 → 0.622330 (−0.001287) | **no** |

dv1 fails on precision, and the examples say why: MR, SIR and LORD all mark the
same gender, so what it really catches is an honorific. `Mr Jonathan P Marland`
against `Lord Jonathan Marland`; `Sir Christopher Gent` against `Mr Christopher
C Gent`; `Mr John S Wheeler` against `Sir John Stuart Wheeler`. 1,589 of its
1,688 hits were pairs the imported labels had already accepted. A title veto
needs a title-to-gender lookup with the honorifics collapsed, not a token list.

dv2's hits read right: `Cairns Didge UK Limited` 04345773 against `CAIRNS DIDGE
PROPERTY UK LTD` 05132672; `Bestway (Holdings) Limited` 01392861 against
`Bestway Wholesale` 01207120; `Dalglen (No. 1813) Limited` SC570493 against
`Dalglen (No 1811) Limited` SC559988. Overall `score_only` precision 0.965657 →
0.965971, recall 0.781240 → 0.781187. Entities after 18,581 → 18,666.

The `without_vetoes` figure set in `score_eval.json` reproduces the no-veto
baseline exactly, which is the check that the two are comparable.

## 15. The B5 corpus decision (from the owner)

**PSC will fit TF-IDF over every unit, like donations, and the fitted vocabulary
and IDF are persisted at scoring time.** That settles the design question
section 13 used to record as open. It is a behaviour change for PSC and a no-op
for donations, and whoever builds it should assert exactly that — see item 1 of
section 13.

## 16. One more bug fixed in passing: PSC pair detail was a 500

`GET /api/runs/{id}/pairs/{pair_id}` failed on **every** PSC pair, before any of
this session's work — the old `psc_sample` run reproduces it too. `unit_events`
in `pairs_reader` ordered the evidence rows with `ORDER BY e.date`, and PSC's
evidence rows are the companies a person controls: `record_id`,
`company_number`, `notified_on`, `ceased_on`, `natures_of_control`, `kind`,
`postcode`, `locality`. No `date`, and DuckDB refuses to bind it, so the whole
request raised `BinderException`.

It sorts on `date`, then `notified_on`, then nothing, falling back to the record
id. Donations is unaffected and still comes back newest first. Worth knowing
because it means the PSC review screen has never opened a pair, and the veto
reason this slice adds is shown there.

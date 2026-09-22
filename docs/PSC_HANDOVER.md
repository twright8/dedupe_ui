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

> **Fourth session, same day. Read sections 20 to 24 first — this section
> below is the state as of the third session and is kept for its detail.**
> Closed since: stage 3's Python-side memory (20), the persisted corpus (21),
> B5 for stages 4 and 5 (17), B4 (18), the hot-key blocking control (19), and C
> — the full snapshot is loaded and through the exact keys, with the token lists
> rebuilt from it (23). **Still open, and section 24 has the numbers: nothing
> downstream of stage 2 can run at full scale until `build_units` is out of
> core, because it needs about 84 GB at 15 million records.** Stage 2 itself
> needs 12.5 GB and is next after that.


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

## 19. The hot-key blocking control

Section 6 priced the idea and said it was worth building. It is built, in shared
code, in `backend/app/rules/linkage.py`. The four keys, the generated SQL, the
counting function and the validation are all documented in `docs/LINKAGE.md`.
This section is the measurement.

A blocking rule may now carry `max_block_size`, `on_oversize`, `refine_with` and
`drop_above`. An oversized block is re-blocked on the extra columns, and one
still too big after that is dropped. It is generated SQL, so a pair inside an
oversized block is never made. Nothing else changed: a rule with none of the
four keys blocks exactly as it did, and the shipped PSC and donations settings
carry none of them. **No settings file was edited.** The numbers below are what
turning the control on would do.

### Every PSC route, priced before and after

On the sample's `units.parquet` — 449,397 person and 31,967 organisation units.
The control is `max_block_size` 60, refine on `forename_initial` for person and
`postcode_district` for organisation, `drop_above` 200. Every number is group
arithmetic; no pair was materialised to get it.

| track | rule | pairs before | blocks | blocks > 60 | pairs after | change | units dropped |
|---|---|---|---|---|---|---|---|
| person | pb1 surname_meta + dob | 306,064 | 349,792 | 13 | 259,088 | −15.3% | 303 |
| person | pb2 surname3 + initial + dob | 166,803 | 413,237 | 13 | 121,050 | −27.4% | 303 |
| person | pb3 name_fingerprint | 567,895 | 356,138 | 30 | 493,465 | −13.1% | 542 |
| person | pb4 postcode_district + surname_meta | 201,820 | 338,550 | 8 | 192,324 | −4.7% | 0 |
| person | pb5 forename_meta + dob, surname differs | 1,164,196 | 214,186 | 15 | 1,161,024 | −0.3% | 312 |
| person | pb6 surname_meta + forename_meta, dob missing | 1,007,328 | 296,868 | 56 | 929,351 | −7.7% | 553 |
| person | em1 surname_meta + forename_meta | 1,007,328 | 296,868 | 56 | 929,351 | −7.7% | 553 |
| person | em2 dob + postcode_district | 165,464 | 331,596 | 8 | 165,132 | −0.2% | 0 |
| organisation | ob1 name_core | 68,535 | 29,883 | 7 | 26,022 | −62.0% | 0 |
| organisation | ob2 name_core_meta + type_bucket | 72,642 | 28,340 | 7 | 30,129 | −58.5% | 0 |
| organisation | ob3 regnum_clean | 65,321 | 26,998 | 7 | 22,998 | −64.8% | 0 |
| organisation | ob4 name_first_token + postcode_district | 28,578 | 24,492 | 4 | 28,578 | 0.0% | 0 |
| organisation | em1 name_core_meta + type_bucket | 72,642 | 28,340 | 7 | 30,129 | −58.5% | 0 |
| organisation | em2 regnum_clean | 65,321 | 26,998 | 7 | 22,998 | −64.8% | 0 |

The person prediction total falls from 3,414,106 pairs to 3,156,302, about 7.6%.

Two of the before figures correct section 6. pb5 is 1,164,196 here, not
1,305,975, and pb6 was never priced. Section 6's arithmetic ignored pb5's
`l.surname_metaphone <> r.surname_metaphone`; the counting function applies it
exactly, by subtracting the pairs inside each same-surname-sound sub-block, and
it also leaves out the units whose surname sound is null, which can never
satisfy a `<>`. Every other before figure in section 6 reproduces exactly:
pb1 306,064, pb3 567,895, pb4 201,820, em2 165,464, organisation 72,642 and
65,321.

pb6 is the one rule the counting function cannot price exactly. Its
`(l.dob_year_clean IS NULL OR r.dob_year_clean IS NULL)` is not something a
`GROUP BY` can apply, so its figures are an upper bound and `price_rule` says so
in `exact: false`.

### The generated SQL was checked against real pairs

Sixteen cases — pb1, pb3, pb5 and ob1, each with no control, refine only, drop
only, and both — were priced and then run as a real self-join over the sample
units. The counted number and the materialised number agree exactly in all
sixteen. Pricing and blocking say the same thing.

### What the numbers actually say: refining on the forename initial barely works here

The person cuts are much smaller than section 6's "38% of pb3's pairs sit in 30
blocks" suggests, and the reason is worth carrying forward. **The PSC hot blocks
are placeholder names, and everybody in them shares one forename initial.** The
largest pb1 block is `('FLTS', 1950, 11)` with 303 units and exactly **one**
distinct forename initial. Next is `('TN', 1945, 1)` with 181 units and two
initials, 180 of them in one. Ten of the 13 oversized pb1 blocks have a single
forename initial.

So refining on the forename initial splits almost nothing, and nearly all of the
cut in the table comes from `drop_above` throwing the biggest block away, not
from refinement. The lesson is general: **the refine column has to be
independent of the block key.** On PSC the hot keys are junk names, where every
name field agrees, so no name column will refine them. What would work is a
non-name column — postcode district, or nationality — or a veto on the junk
names themselves, which is the ruleset's job rather than blocking's.

Dropping alone is much stronger than refining here: pb1 at `on_oversize: "drop"`
over 60 is 185,124 pairs, −39.5%, against −15.3% for refine-then-drop. If these
blocks are junk, dropping them is also the right answer.

### The organisation figures are about a null column, not a hot key

Organisation looks like the big winner at −60%, and it is not. The refinement
there is `postcode_district`, which 4,336 of the 31,967 organisation units do
not have — including 496 of the units inside the hot `name_core` blocks. A null
never equals a null, so those units pair with nobody once the block is refined.
`price_rule` reports them separately as `units_unrefinable` for exactly this
reason. Priced with `type_bucket` instead, ob1 goes from 68,535 to 68,462, which
is a truer picture of what refining an organisation block buys: almost nothing.
Organisation does not have a hot-key problem worth solving.

### What to do with this

1. The control is there, it is tested, and it costs nothing while unused.
2. Do **not** turn it on for PSC with `forename_initial`. It buys 7% on the
   person track and most of that is really `drop_above` at work.
3. The real target is the placeholder names — `FLTS`, `PRSNS`, `SMS` — which
   belong in a veto or a cleaning rule. `on_oversize: "drop"` over 60 is a
   blunt version of the same thing and is worth considering as a stopgap at full
   scale, where the same blocks will be about 32× larger and their pairs about
   1,000× more.
4. Full-scale pricing still needs the full snapshot. Everything here is the
   sample.

## 20. Stage 3's Python-side memory (the 8.5 GB)

Measured on the 500,000-record sample, phase by phase, before touching
anything. The number in the previous handover was one peak with no breakdown,
and the breakdown is what said where to work.

| | before | after |
|---|---|---|
| stage 3 peak RSS | **7,351 MB** | **3,985 MB** |
| stage 3 wall time | 95.8 s | **65.7 s** |
| `score_distribution_person.html` | **67.9 MB** | **8.7 KB** |

Every count is unchanged: 1,141,832 pairs scored, 221,841 accept / 66,177
review / 853,814 reject, 591,804 vetoed, 96,099 vetoed from accept, 457,941
entities after scoring, `untrained_comparisons` 0. `pairs.parquet` matches the
committed run column for column; `match_probability` and `match_weight` differ
by at most 7e-13 and 5e-12, which is EM's own run-to-run noise — the two runs
trained their own models — and every other column, including every gamma and
every bucket, is identical value for value.

Where the 7.4 GB was, and what happened to each part:

| phase | peak rose by | now |
|---|---|---|
| build units | +1,739 MB | +1,874 MB, unchanged (that is B4's) |
| blocking budget | +910 MB | **+0** |
| scoring person (`predict` into pandas) | **+3,315 MB** | +811 MB |
| the overlays | +227 MB | +0 |
| everything after | +0 | +0 |

**The biggest single thing was pulling `predict` into pandas.** Splink is asked
to retain the matching columns and the intermediate calculations, because the
per-pair explanation needs the gammas. That also makes it hand back both sides'
*values* for every compared column, plus a Bayes factor and a term-frequency
adjustment each — about 3.2 KB per pair on this data. `finalise_pairs` threw
every one of those columns away again, but only after 1.07 million of them had
become Python objects. They are now dropped in SQL, on the way out of DuckDB,
and the predictions go straight to a parquet (`write_predictions`). Nothing
Splink predicts is ever a pandas frame.

The rest, in the order they mattered:

- **The whole frames are dropped once the units are built.** Stage 3 read
  `records.parquet` whole (63 columns), kept `units` and `unit_members` in
  memory for the whole stage, and handed `units` whole to the budget check, to
  each track's training, to the overlays and to the evaluation. It now writes
  the two files, drops all three frames, and reads projections: four unit
  columns for the overlays, the columns a track's rules and comparisons name
  for Splink, three record columns for the evaluation. RSS after the unit build
  goes from 1,915 MB to 730 MB.
- **The overlays stream.** `overlay_predictions` reads each track's predictions
  in `PAIR_BATCH_ROWS` batches (default 500,000), lays them out on the union of
  every track's columns so the file has one schema, applies the buckets, the
  vetoes and the priority totals, and appends through one `PairWriter`. The
  union is worked out from the prediction files' schemas before the first batch
  — it is exactly what `pd.concat` used to leave behind, a null wherever a track
  has no such comparison. There is a test that a batch size of one writes the
  same file as a batch size of a thousand, and the same file the in-memory path
  always wrote.
- **The evaluation streams too.** `score_eval.evaluate` takes a path now as well
  as a frame. Every figure in it is a sum over rows, so a `_Tally` accumulates
  the bucket counts, the veto counts, the review split and the histogram a batch
  at a time, and the seven accepted-pair sets are gathered as **int32 edge
  arrays** rather than as frames. A test holds the path and the frame to
  byte-identical JSON.
- **The score histogram is counted in SQL.** Altair embeds its data in the page,
  so one row per pair meant a 67.9 MB HTML file for the sample and would have
  meant gigabytes at full scale — for a picture with fifty bars. `histogram_counts`
  groups in DuckDB and the chart is drawn from fifty rows per bucket.
- **The re-bucket, apply-model and revert-model paths stream.** `rewrite_pairs`
  reads the pairs a batch at a time through one writer and swaps the result in,
  instead of holding three copies. Stage 3b batches over `MODEL_BATCH_PAIRS`
  (default 2,000,000) and gets the collapse guard's numbers out of the same
  pass, because reading a hundred million pairs twice to ask "did the review
  band survive" is not worth doing.
- **Splink's connection is closed between tracks.** It has no `close`, and the
  tables it made live in its own in-memory database. After the person track that
  is about a gigabyte and the organisation track is about to ask for its own.
  `release_linker` closes it and collects.

**What is left, and it is not pairs.** The peak is now the unit build (1,874 MB
of transient on top of the records frame) and Splink's own DuckDB working set
during the person track (+811 MB). Neither grows with the number of pairs;
both grow with the number of records, which is B4's and `SPLINK_MEMORY_LIMIT`'s
business respectively. The 3 GB target was not reached on the sample — 3,985 MB
— and the reason is those two, not anything the pairs do.

### Two knobs

`PAIR_BATCH_ROWS` (500,000) is how many pairs any Python step may hold.
`MODEL_BATCH_PAIRS` (2,000,000) is the same for a model's feature build and
predict, which is a fixed cost per call and so wants a bigger batch.

## 21. The corpus decision, built (item 3)

`app/model/corpus.py`. A feature builder that needs corpus statistics
**declares** them as a `CorpusSpec`; the statistics are fitted ONCE over every
unit in the declared scope at scoring time, written into `<run>/corpus/`, and
read back by batched scoring, by apply-model and by a one-pair explanation.

The defect it closes: PSC's `name_core_tfidf` was fitted over the units named
in the pairs the builder happened to be handed, so the same pair scored
differently depending on how many others arrived with it — and the per-pair
explanation, which builds features for one pair, could not reproduce a single
number the scoring run had written. Batching the scorer, which section 20 has
now done, would have changed every value in the file.

**The scope is declared, and the two profiles declare different ones.**

- PSC declares `scope="track"`. `name_core` is an organisation column and the
  sample's 449,397 person units carry none, so counting them in would add that
  many empty documents, lift every IDF by roughly the same amount, and flatten
  the distinction the feature exists to draw.
- donations declares `scope="run"`, because that is what its builder has always
  done — it is handed the whole units frame and fits over all of it, the 13,310
  person units included beside 9,065 organisation ones. Narrowing it to the
  track would be an improvement **and would move every feature value the trained
  donations model depends on**, so it is a change to make deliberately with a
  retrain, not a side effect of moving the fit.

`tests/test_corpus.py` (12 tests) holds both halves: a PSC pair scores the same
alone as in a batch, and the donations value is reproduced bit for bit against
the arithmetic the old in-line fit did.

The declaration is read off the profile (`corpus_specs(track)`), falling back to
the profile's own `<profile>_features` module, which is where the declaration
sits beside the builder that reads it.

## 22. Two bugs found in passing

**The donations organisation features crashed on a run with no nature rows.**
`_nature_frame` returns an empty pandas frame when there are no usable natures,
an empty object column carries no type at all, so DuckDB gives it a scalar one
and `list_distinct` refuses to bind: `Binder Error: No function matches the
given name and argument types 'list_distinct(VARCHAR)'`. Eight tests in
`test_model_features.py` were failing on it before this session started. Both
sides are now cast to `VARCHAR[]`, which is a no-op when there are real rows.

**Stage 0 could not read the full snapshot, twice over.** Section 2 of this
document praises the FIFO, and the FIFO cannot do it. Two separate limits:

1. The extract was materialised as a `CREATE TEMP TABLE`. An in-memory DuckDB
   cannot evict base-table data to its temp directory, so the whole extract has
   to fit under `PSC_DUCKDB_MEMORY`; the full snapshot died with
   `OutOfMemoryException` at 1.8 GiB of a 2 GiB cap.
2. Worse, and it looks like it works: **DuckDB's JSON reader cannot seek a pipe,
   so it buffers the input instead of streaming it.** The memory it needs grows
   with the file, not with the query. Measured with a 2 GiB cap: 1,000,000
   records through a FIFO is fine, 4,000,000 fails, and the same 4,000,000 read
   from an ordinary file stream through in 44 seconds well inside the cap. No
   memory limit makes a 13 GB member work through a pipe.

The member is now decompressed a chunk of lines at a time (`PSC_CHUNK_LINES`,
default 1,000,000) into a temp file; DuckDB extracts each chunk into a staging
parquet and the chunk file is deleted before the next is written. So the
member does touch disk, about 800 MB of it at a time, which earlier versions
went out of their way to avoid — and the reason they could is that they were
never run at full scale. Output on the 500,000-record sample is identical to
the temp-table path, column for column.

## 18. B4: build_units at 16 million rows

**The per-column modal vote is not the bottleneck, so the machinery the task
described was not built.** The vote is 3% of `build_units` at 4 million rows.
One bug was found and fixed instead, and it is a crash, not a slow path.

### How it was measured

`records.parquet` and `exact_groups.parquet` from
`pscdata/runs/psc_vetoes` (499,971 records, 64 columns) tiled N times, each
tile with its own `-tNN` suffix on `record_id` and `group_id` so the ids stay
unique. The scripts are in `/home/tomwright/psc_scratch/agent_b4`:
`measure.py` tiles in memory, `tile_to_disk.py` writes a tiling one tile at a
time, `phases.py` is a copy of `build_units` with a timer per phase, and
`verify.py` rebuilds a stored run's units and compares them.

**The 32× tiling does not fit.** Peak RSS is about 2.6 GB per million records,
so 16 million records need roughly 84 GB. This machine has 29 GB. 8× peaked at
21.5 GB, which is already most of it. So 1×, 4× and 8× were measured and 32× is
an extrapolation.

### The numbers

`build_units` alone, records already in memory, events not passed:

| tiling | records | before the fix | after | peak RSS |
|---|---|---|---|---|
| 1× | 499,971 | 24.3 s | **18.6 s** | 3.2 GB |
| 4× | 1,999,884 | 126.3 s | **95.4 s** | 10.2 GB |
| 8× | 3,999,768 | 349.7 s | **283.4 s** | 21.5 GB |

Where the 283.4 s goes at 8×:

| phase | secs | share |
|---|---|---|
| scatter the voted values back per column | 120.4 | 42.5% |
| order the columns and sort on `unit_id` | 94.0 | 33.2% |
| split pooled units from lone ones | 12.4 | 4.4% |
| sum the priority columns | 11.1 | 3.9% |
| unit sizes | 10.9 | 3.8% |
| **the per-column modal vote** | **9.5** | **3.4%** |
| join records to members | 8.9 | 3.1% |
| the profile's own aggregates | 8.2 | 2.9% |
| everything else | 8.0 | 2.8% |

### Why the vote is cheap

Only pooled units are voted on, and almost nothing pools. At every tiling
6.4% of records sit in a pooled unit — 31,755 rows at 1×, 254,040 at 8× — so
the 61 DuckDB queries run over a sixteenth of the frame, not over it.

The worst case was measured too. Forcing all 499,971 sample records into units
of two, so that every row is voted on, costs **10.2 s for 62 columns**. That is
20 µs per pooled row. At 16 million records with the sample's pooling rate the
vote costs about 30 s; even if every one of the 16 million records pooled it
would cost about 5.5 minutes.

Extrapolating the whole of `build_units` to 16 million records: 19 minutes if it
stayed linear in rows, about 42 minutes if the 4×-to-8× curve holds. The vote is
under 3% of that either way. Narrowing it to a declared column set would save
tens of seconds out of tens of minutes, and it would cost a declaration that has
to stay in step with the Splink comparisons, the blocking rules, the feature
builder, the vetoes, the evidence focus, the display columns and the priority
and consensus columns. That trade is not worth taking, so it was not taken.

### What was changed

One line, in `units.py`. The per-unit `held_group_id` was
`joined.groupby("unit_id")["held_group_id"].min()` over the whole frame. It is
now `_smallest_held_group`, which groups only the rows that actually carry a
held group.

**It was a crash, not just slow.** Pandas has no fast path for `min` over an
object column, so it falls back to Python and fills a group's nulls with a float
sentinel before comparing. A unit whose members are only partly in a held group
then compares a string to a float and the run dies with `TypeError: '<=' not
supported between instances of 'str' and 'float'`. The real donations run has
194 such units. `build_units` could not be run on `don_veto` at all until this
was fixed.

This is a pandas 2.x fault. `requirements.lock` pins pandas 3.0.6, which stores
these columns as a string dtype and has a fast path, and that is what the stored
runs were built with. `requirements.txt` says only `pandas>=2.0`, and this
machine has 2.3.1, which is also what the test suite runs on. The fix is correct
on both versions and does strictly less work on both.

It is also a fifth to a quarter of the runtime on pandas 2.3.1: 7.9 s of 24.3 s
at 1×, 34.2 s of 126.3 s at 4×, 74.6 s of 349.7 s at 8×. Afterwards it is 0.2 s,
0.8 s and 2.0 s.

### What was not changed

- No profile-declared vote set. `Profile` in `app/profiles/base.py` is untouched,
  and so are `psc.py` and `donations.py`.
- Every column of the records frame still gets a modal vote, and `units.parquet`
  still carries every column it carried before.
- Nothing in the two heavy phases above. They are the place to start next time,
  not the vote.

### The proof that nothing moved

`verify.py` rebuilds a stored run's units from its own `records.parquet`,
`exact_groups.parquet` and `events.parquet` and compares three ways.

| run | rows | `DataFrame.equals` | bytes |
|---|---|---|---|
| donations `don_veto` | 22,375 units | **True** | identical to the round-trip |
| PSC `psc_vetoes` | 481,364 units | **True** | identical to the round-trip |

**Read "identical to the round-trip", not "identical to the stored file".** The
stored files were written by pyarrow 25.0.1 under pandas 3.0.6; this machine has
pyarrow 22.0.0 and pandas 2.3.1. Reading a stored `units.parquet` and writing it
straight back out changes its md5, because the writer stamps its own version and
the pandas metadata records a different dtype name. So the control is the stored
file read and rewritten here, and the rebuilt file matches that control byte for
byte — same md5 — for both runs. No PSC column moves. No donations column moves.

**Anyone comparing parquet files on this machine needs that control.** A bare
md5 against a file from `psc_scratch` will differ for reasons that have nothing
to do with the data.

### One number that did not reproduce

Section 13 records `build_units` as about 75 s of the sample's 150 s stage 3,
and `veto_run.log` logs the phase at 67.0 s. Timed here on the same inputs and
the same machine, the whole phase — read the three parquets, build, write
`units.parquet`, `unit_members.parquet` and `scored_units.parquet` — is
**26.6 s**, of which `build_units` is 21.1 s. The gap was not chased. The
original was measured inside a full stage-3 run with Splink resident and under a
different pandas. Worth re-timing in a real run before anyone plans around
either figure.

### Tests

`backend/tests/test_units.py` is new: 11 tests over the held-group contract and
the modal vote. Six of them pin held groups, including the partly-held unit that
used to crash — that one fails on the old line with the same `TypeError`. The
rest pin what must not move: a unit of one keeps its own values, the modal vote
still breaks ties on the smallest `record_id`, and a column nothing reads still
reaches `units.parquet`.

`test_units.py`, `test_donations_profile.py` and `test_stage_3_score.py`:
85 passed. `test_psc_profile.py` with `test_units.py`: 59 passed.
`test_config_derived_api.py`: 22 passed.

## 17. B5: stages 4 and 5 out of core

Stage 4 read `units.parquet`, `unit_members.parquet`, `pairs.parquet` and
`exact_groups.parquet` whole. Stage 5 read `clusters.parquet`,
`unit_members.parquet` and every one of `records.parquet`'s 64 columns. The
clusters list and the entities list built one Python dict per row before showing
a page of fifty. None of that fits 15 million units, 100 million pairs and
16 million records in 29 GB of RAM.

All of it is now DuckDB, on connections from `app/duckdb_conn.py`, so every one
carries the memory cap, the run's own temp directory and
`max_temp_directory_size`. There is no bare `duckdb.connect()` in any of it.

### What changed, file by file

**`stage_4_cluster.py`** — rewritten. The three inputs are bound to fixed view
names, and `run_stage_4_cluster` binds parquet files where `build_clusters`
binds frames. One SQL body serves both, so the frame form the tests use cannot
drift from the file form the run uses. The accepted edges, the human overlay,
the import star, the human FALSE deletions, the gate's five tests and
`clusters.parquet` itself are all SQL; the file is written by `COPY ... TO`.
Only four columns of `units.parquet` are read. The per-cluster status strings
were a Python loop over every cluster — 458,000 iterations on the sample — and
are now five vector operations.

**The components step** works on integers. The unit ids are dense-coded in SQL
(`row_number() OVER (ORDER BY unit_id)`) and SciPy is handed two int32 arrays.
`components(n_units, rows, cols)` raises `TypeError` on anything else. The old
code built a `pd.Series` indexed by 15 million unit-id strings and called
`.map()` on it twice.

**`stage_5_entities.py`** — the per-record work moved to SQL. The clusters and
the unit members are joined into a DuckDB table, the per-entity summary is a
group-by, `entities.parquet` is a `COPY ... TO`, the three registry invariants
are three aggregates, and both evaluation reports (`entities` and
`versus_existing_entity_id`) are aggregates rather than `keys_eval` over two
frames of every record. `records.parquet` is read as a **projection**: six
columns of 64 on PSC. The pandas functions above `run_stage_5_entities` are kept
as the in-memory reference and the tests hold the two paths to one answer.

**`psc_export.py`** — the decision table is a DuckDB join, sorted in SQL. The
parquet form is `COPY ... TO`. The CSV and the Elasticsearch bulk file are
written from batches, because a byte-order mark and a JSON line are what SQL
cannot write. **The batch size is `EXPORT_BATCH_ROWS`, default 200,000.** It is
a memory ceiling and never part of the answer; a test writes the same files at
sizes 1, 3 and 200,000. `context["entities"]` may now be left out, in which case
the run's own `entities.parquet` is read.

**`clusters_reader.py`** and **`entities_reader.py`** — the base query is
materialised once into a temp table, and the filters, the counts, the sort and
the page are SQL with `LIMIT`/`OFFSET`. Both readers now open on the run's temp
directory. `entities_reader` reads `id_status` off the proposal instead of
needing the caller's map, and `GET /api/runs/{id}/entities/{entity_id}` looks one
entity up directly instead of paging the whole list and searching it.

`records_reader.py` and `exact_groups_reader.py` were already correct and are
unchanged. `donations_export.py` was read and left alone: its promise is the
user's own sheet back, row for row, and that sheet is 94,141 rows — it already
streams them from the frame's numpy columns and there is nothing at PSC scale
behind it. `match_reader.py` is the OCOD/ROE tool, not this pipeline, and was
left alone too; its two reads are already projected and de-duplicated.

### Before and after

Three runs of each, back to back, same machine, `DUCKDB_MAX_TEMP=10GB`. Medians.
The interpreter and its imports are 140 MB of every figure.

PSC sample, `psc_vetoes` — 499,971 records, 481,364 units, 1,141,832 pairs:

| stage | time before | time after | peak RSS before | peak RSS after |
|---|---|---|---|---|
| 4 cluster | 25.5 s | **1.6 s** | 2,211 MB | **1,461 MB** |
| 5 entities | 23.3 s | **8.7 s** | 2,477 MB | **1,774 MB** |

Donations, `don_veto` — 51,839 records, 22,375 units, 28,843 pairs:

| stage | time before | time after | peak RSS before | peak RSS after |
|---|---|---|---|---|
| 4 cluster | 1.2 s | **0.3 s** | 289 MB | 308 MB |
| 5 entities | 1.1 s | **0.6 s** | 350 MB | 375 MB |

**Donations uses about 20 MB more, not less.** At 22,375 units DuckDB's buffers
cost more than the pandas frames they replace. That is the honest shape of this
change: it is not a saving at small scale, it is a ceiling that stops rising.

The two list reads, measured on the PSC sample in a process of their own:

| read | time before | time after | peak RSS before | peak RSS after |
|---|---|---|---|---|
| entities list, first page | 7.3 s | **0.5 s** | 2,007 MB | **1,012 MB** |
| clusters list, first page | 5.3 s | **0.6 s** | 1,908 MB | **1,147 MB** |

### The outputs are identical

Both committed sample runs were re-clustered from a copy into a scratch folder
and compared against the originals, which were never written to.

- `clusters.parquet` and `entities.parquet`: `DataFrame.equals` true after
  sorting on the natural key, with the same columns in the same order.
- `entity_report.json` and `score_eval.json`: identical key by key, including
  every float in the evaluation blocks.
- PSC: 481,364 cluster rows, **457,941 clusters**, 21 withheld, 33 held groups,
  499,971 entity rows, **458,349 entities proposed** — the committed baseline.
- Donations: 22,375 cluster rows, 17,106 clusters, 324 withheld, 371 held
  groups, 51,839 entity rows, 17,547 entities, **53 id collisions broken**. The
  collision path is exercised and its minted ids come out the same.

The comparison script and the runner are in `/home/tomwright/psc_scratch/agent_b5/`.

### Tests

`tests/test_stage_4_cluster.py` is new, 13 tests. It holds a stored fixture —
the whole of `clusters.parquet` and `entities.parquet` for a nine-record run, as
literals — plus the file path against the frame path for both stages, the
components step refusing anything but integer codes, a spy proving the real run
hands it int32, the `EXPORT_BATCH_ROWS` default and fallbacks, the export being
byte-identical across three batch sizes, and the two readers paging in SQL.
`tests/test_entities.py` (55) passes unchanged, which is the point: no contract
moved. Wider sweep, 362 passed: `test_stage_4_cluster`, `test_entities`,
`test_run_counts`, `test_entity_and_roe_export`, `test_exact_groups_api`,
`test_records_api`, `test_donations_profile`, `test_psc_profile`,
`test_model_apply`, `test_match_reader`, `test_duckdb_conn`, `test_pairs_api`,
`test_labels`, `test_label_applier`, `test_runs_api`.

### What is still O(records) or O(entities), and why

Honest list. None of it grows with the number of **pairs**, which was the brief.

1. **The mint frame.** `Profile.mint_entity_ids` takes a whole frame, so every
   record of every proposal the registry does not know is still a pandas frame —
   at PSC that is all of them. It is now six columns instead of 64, but the row
   count is the record count. Fixing it properly means a hook that takes
   per-entity aggregates instead of members. Note also that the PSC hook then
   loops in Python over every entity key: 458,000 on the sample, 15 million on
   the full snapshot.
2. **The per-entity summary** in stage 5 and the **per-cluster summary** in
   stage 4 are pandas frames of one row per entity or cluster. That is the size
   of the answer; it cannot be smaller. Both are narrow.
3. **`store.current_members(db_path)`** still returns a Python dict of every
   published record. It is empty on both sample runs and it is not stage 5's
   code, but at 16 million published records it is a problem of its own.
4. **`routers/entities.py`** still does `pd.read_parquet(entities_path)` before
   calling the profile's export, and `_id_statuses()` still builds a dict of
   every entity id with a bare `duckdb.connect()`. Both are one-line changes and
   both are in a file this slice was told not to touch. `psc_export` no longer
   needs the frame — it reads the run's own `entities.parquet` when none is
   given — so the export line can simply stop passing one.
5. **`consensus()`** is still pandas. It is fed two narrow projections now
   (`record_id` + `entity_key`, and `record_id` + the column + its `_rule`), so
   it is 4 columns of the record count rather than 64.

### Two things worth knowing

**`track` collides in the mint merge.** `proposed.merge(records, on="record_id")`
suffixes `track` to `track_x` and `track_y`, so the PSC mint hook's
`"track" in members.columns` is False and **every** PSC entity id gets the
person prefix `PSCP-`, organisations included. That is true of the committed
baseline too. It is reproduced exactly here, because value-identical was the
constraint. Fixing it is a separate decision and it renumbers every PSC
organisation.

**A profile that mints from an undeclared column would break.** The projection
is `stage_5_entities.MINT_COLUMNS` unless the profile declares `mint_columns`.
Both shipped profiles are covered. `psc.py` and `donations.py` should declare
their own, which is two lines in files another slice owns — reported rather than
changed.

## 23. C: the full snapshot through stages 0 to 2

Ran as a background job on this laptop, `CLEAN_BATCH_ROWS=100000`,
`PSC_DUCKDB_MEMORY=2GB`, `DUCKDB_MAX_TEMP=15GB`, in
`/home/tomwright/psc_scratch/pscfull/runs/psc_full`.

| stage | time | peak RSS | output |
|---|---|---|---|
| 0 load | 1,207.8 s | 1,731 MB | `records_raw.parquet` 1,069 MB, `events.parquet` 546 MB |
| 1 clean | 615.2 s | 2,490 MB | `records.parquet` 1,891 MB |
| 2 exact | 279.2 s | **12,483 MB** | `exact_groups.parquet` 245 MB |

Disk 79.0 GB free before, 70.5 GB after. Nothing came close to the 15 GB floor.

**Rows.** 15,952,486 input rows. 545 dropped as super-secure and **922,678
dropped for having no name at all** — 5.8% of the file. Counted by `kind`, that
turns out to be nothing to worry about and everything to write down:

```
  922,564  100.0%  persons-with-significant-control-statement
      108    0.0%  exemptions
        3    0.0%  corporate-entity-person-with-significant-control
        2    0.0%  individual-person-with-significant-control
        1    0.0%  totals#persons-of-significant-control-snapshot
```

**They are statements, not people.** A `persons-with-significant-control-statement`
is a company saying something *about* its PSCs — "no registrable person",
"steps not completed" — so it has no name because there is nobody to name, and
dropping it is right. Only **five** records in the whole register are a real PSC
entry with no name at all (three corporate, two individual), plus 108 exemption
notices and one totals row. 15,029,263 records kept: **13,921,888 person,
1,107,375 organisation**.

**The exact keys.** 1,814,293 merged groups over 5,016,497 records; 3,899 held
groups over 326,954 records; **11,827,059 entities after the keys**, down from
15,029,263.

| key | track | groups | records | held groups | held records |
|---|---|---|---|---|---|
| k1 | person | 1,649,990 | 4,367,317 | 3,092 | 231,878 |
| k2 | organisation | 150,861 | 597,948 | 738 | 87,748 |
| k3 | organisation | 13,442 | 51,232 | 69 | 7,328 |

The ten largest held groups, which is where the guards are earning their keep:

```
person        7,718   H-k1-03479577_c8HtRYIUXnH2iGsblYz4s0q0_Ik
person        4,839   H-k1-04340028_8t7TpRoUsW2GMtu98LrsTjqRMMQ
organisation  4,490   H-k2-05138530_-01QXxhVL8_OpPvsbzNKcOgg3nE
person        4,355   H-k1-08932458_Tlgr1d_QxERr0ypMQvMJ2fDU3dM
organisation  4,260   H-k2-10201968_9FzqS7Rh9T-KC4pS8NWnQlBr95o
person        3,872   H-k1-05520556_iKEG6t2s9DwZh7ZIDO-nt3UZBcg
organisation  3,424   H-k2-07645529_4T5KrarVvzKe7c3MqRV2QPtii4g
person        2,926   H-k1-08566829_e23xpn1O5tiSjzvTmp7qUnnyi9o
person        2,865   H-k1-04340028_bB7gYyEp8d4oLrnFP7toFSldwfw
organisation  2,826   H-k2-03520309_01lDV_zFFtSsXeD3-9u6NbItVFw
```

The largest *merged* group is 50 records, on every one of the top ten, which is
the `max_group_size` guard doing exactly what it is for.

**Stage 2 needs 12.5 GB.** That is the projection of fourteen columns over
15 million records in pandas. It fits on this machine and it would not fit on
the server's 6 GB budget. B3 proved the projection correct and measured its
shape; this is the number at scale, and it says stage 2 is the next thing to
batch after `build_units`.

### The token lists, rebuilt from the full data

The old lists came from the 500,000-record sample and one of them was measured
against the wrong column. **`postcode_clean` is null exactly where the junk list
already nullified it**, so a list rebuilt from `postcode_clean` comes back
empty and looks like the list is wrong. It has to be counted on the raw
`postcode`.

- **`junk_postcodes`: 18 → 20.** deduping's proportion is more than 5,000
  records per 7.5 million individuals, which on 13,921,888 person records is a
  threshold of **9,281**. Six added (`E6 2JA` 18,221, `HA4 7AE` 15,191,
  `W6 0NB` 14,622, `EC2A 4NA` 11,820, `HR5 3DJ` 11,042, `M40 8WN` 10,034), four
  dropped (`SW1Y 5EA`, `N12 0DR`, `SW15 2BF`, `EC1V 9BD` — all under the
  threshold at full scale). The biggest is `WC2H 9JQ` at 180,477 records.
- **`placeholder_numbers`: 13 → 47.** Every registration number used by ten or
  more distinct filed names that `fake_nulls` does not already catch, plus the
  thirteen hand-written ones, which stay whether or not this snapshot happens to
  contain them — the list is a rule, not a census. What the full data adds is
  two kinds the sample never showed: **words in the number box** (`ENGLAND`,
  `UNITED KINGDOM`, `ENGLAND AND WALES`, `BEING INCORPORATED`, `BEING
  REGISTERED`, `TBA`, `USA`, `UK`, `NOT AVAILABLE`) and **formation agents'
  real numbers**, which are the dangerous ones because they look like valid
  identifiers: `09361466` and `9361466` (45 filed names between them, 4,260
  records), `07168188` (23 names, 2,738), `SO303142` (12 names, 2,195),
  `CHE-108.562.489` (12 names, 2,164).

### Stages 1 and 2 re-run on the full snapshot with the new lists

| stage | time | peak RSS |
|---|---|---|
| 1 clean | 543.8 s | 2,436 MB |
| 2 exact | 343.5 s | 13,100 MB |

The record counts do not move — the lists change what a value *means*, not
which rows are kept — but the exact keys do:

| | sampled lists | rebuilt lists | change |
|---|---|---|---|
| merged groups | 1,814,293 | 1,811,774 | −2,519 |
| merged records | 5,016,497 | 5,041,612 | +25,115 |
| held groups | 3,899 | 3,842 | −57 |
| held records | 326,954 | 332,009 | +5,055 |
| **entities after the keys** | **11,827,059** | **11,799,425** | **−27,634** |

Fewer groups holding more records each: dropping four postcodes from the junk
list gives those records a usable address again, so the address key joins them
to groups that already existed. **27,634 entities the sampled lists would have
left apart are now merged**, which is 0.23% of the register and is the size of
the prize for measuring a token list on the data it will be run against rather
than on a thirty-second of it.

`exact_groups.parquet` in
`/home/tomwright/psc_scratch/pscfull/runs/psc_full` is this second run, so it
is the one anything downstream should use.

## 24. Does the full pipeline fit on this laptop? No, and the blocker is `build_units`

Written before trying it, so the estimate can be read against the outcome.

**It cannot start.** `build_units` needs about **2.6 GB of peak RSS per million
records** (section 18, measured at 1x, 4x and 8x tilings of the sample). At
15,029,263 records that is **about 84 GB against this machine's 29 GB**, and
there is no environment variable that changes it: the frame, the join and the
per-column scatter are all pandas. Stage 3 cannot begin, so stages 4 and 5
cannot either. The stage-3 work in section 20 removed everything that scaled
with the number of *pairs*; what is left scales with the number of *records*,
and this is it.

For when that is fixed, here is what the rest would cost, extrapolated from the
sample's measured rates.

**Units.** 11,827,059 after the exact keys — about 11.2 million person and
0.62 million organisation. That is **24.9×** the sample's person units and
19.5× its organisation units.

**Blocking, uncontrolled.** The sample's person routes make 2,409,101 pairs.
A route's pairs do not all scale the same way: a selective key's blocks stay
small and its pairs grow with N, while a hot key's block grows with N and its
pairs grow with **N squared**. Section 19 measured the split — blocks over 60
carry 13% to 38% of a route's pairs. Taking a quarter as hot:

```
linear part  0.75 x 2,409,101 x 24.9   =   45,000,000
hot part     0.25 x 2,409,101 x 24.9²  =  373,000,000
                                          -----------
                                          ~418,000,000 pairs
```

That is nearly three times the 150 million the person track is meant to fit in,
and essentially all of the excess is the hot keys.

**Blocking, with the control of section 19.** `max_block_size: 60` with
`drop_above: 200` caps a block at 200 units, so it can make at most 19,900
pairs however big the data gets, and the number of blocks grows with N. The
route becomes linear: **an estimated 60 to 90 million person pairs**, inside a
`max_pairs` of 150,000,000 with room to spare. **This is what the control is
for.** On the sample it buys 7.6%; at full scale it is the difference between
fitting and not.

**Predict.** The sample scores 2.4 million blocked comparisons in about five
seconds, so roughly 500,000 comparisons a second. At 90 million that is **about
three minutes of predict**, and training (u sampling plus two EM rules) is the
same order again. Call the person track 10 to 20 minutes.

**Memory and disk for predict.** `pairs.parquet` is 55 bytes a pair on disk, so
90 million pairs is about 5 GB of output. DuckDB's comparison vectors are the
expensive part and are what `SPLINK_MEMORY_LIMIT` bounds; above it they spill,
bounded in turn by `DUCKDB_MAX_TEMP`. With 16GB and 76 GB of free disk this is
affordable — the 53 GB that filled the disk in an earlier session came from a
170-million-pair training rule with both retain flags on, and both of those are
now priced before anything runs.

**So the order of work is:** make `build_units` out of core, then batch stage 2
(12.5 GB at 15 million records), then run stages 3 to 5 with the hot-key control
on and `max_pairs` at 150,000,000. Nothing else is in the way.

### Will stages 4 and 5 fit the full snapshot? Stage 4 yes, stage 5 barely

Asked while this was being written, because the full snapshot is now loaded:
15,029,263 cleaned records, and stage 2 leaves **11,827,059 units**. Against the
sample that is 30.1× the records and 24.6× the units.

Everything DuckDB holds is bounded by `SPLINK_MEMORY_LIMIT` and spills past it,
so the question is only what is still a pandas frame. Measured on the sample,
then scaled on row count — the ids are the same length at either scale, so the
scaling is linear:

| frame | sample | rows | at full scale |
|---|---|---|---|
| stage 4, the per-cluster summary | 119 MB | 457,941 | **~2.9 GB** |
| stage 5, the mint frame | 306 MB | 499,971 | **~9.2 GB** |
| stage 5, the per-entity summary | 219 MB | 458,349 | **~5.4 GB** |
| stage 5, the two consensus frames | 167 MB | 458,349 × 2 | ~4.1 GB |

**Stage 4: about 3 to 4 GB of Python, plus DuckDB's cap.** The summary is the
bulk of it. The components arrays are int32 and small: 11.8 million unit codes
is 47 MB of labels and 95 MB of the frame that carries them back, and even 50
million accepted edges is about 700 MB inside SciPy. With a 4 GB DuckDB cap the
whole stage should sit under 8 GB. It fits.

**Stage 5: about 16 to 18 GB of Python at its worst moment**, which is inside
`resolve_ids`: the per-entity summary, the mint frame and the minted id series
are all alive at once. Plus DuckDB. On a 29 GB machine that is survivable with a
small `SPLINK_MEMORY_LIMIT` and nothing else running, and it is not comfortable.
This is ~1.1 GB per million records, against the ~2.6 GB per million another
agent measured for `build_units`.

**The mint frame is the thing to cut**, and it is cuttable. Two thirds of its
306 MB is four copies of a 37-character id per record — `record_id`, `unit_id`,
`entity_key`, `cluster_id` — and the shipped hooks read only `record_id` and
`entity_key`. Dropping `unit_id` and `cluster_id` from what
`Profile.mint_entity_ids` is handed takes it to about 5.5 GB at full scale and
is a no-op for both shipped profiles. It was not done here because it narrows a
published hook contract in files this slice was told not to touch. Beyond that,
a hook that takes per-entity aggregates instead of member rows removes the term
altogether.

One more thing that will bite before the memory does: the PSC mint hook loops in
Python over every entity key. That is 458,000 iterations on the sample and about
11.3 million on the full snapshot, building a dict of the same size.

## 26. The sample, end to end, after everything

Stages 1 to 5 on the 500,000-record sample in
`/home/tomwright/psc_scratch/final/runs/psc_final`, `CLEAN_BATCH_ROWS=100000`,
`SPLINK_MEMORY_LIMIT=6GB`, `DUCKDB_MAX_TEMP=10GB`. Disk 74.0 → 72.6 GB.

| stage | before (section 14) | now | peak RSS before | now |
|---|---|---|---|---|
| 1 clean | 19.9 s | 16.6 s | 1,296 MB | 1,012 MB |
| 2 exact | 6.3 s | 3.0 s | 1,416 MB | 920 MB |
| 3 score | 150.1 s | **63.5 s** | 8,528 MB | **4,048 MB** |
| 4 cluster | 37.1 s | **1.3 s** | 5,695 MB | **2,476 MB** |
| 5 entities | 29.9 s | **8.0 s** | 3,210 MB | **2,676 MB** |

**243 s to 92 s, and the biggest peak 8.5 GB to 4.0 GB.**

### Does it equal the committed baseline? Yes for the code, no for the rules — and that is the point

Two separate questions, and they have to be answered separately.

**The code changes move nothing.** Stage 3 was re-run on the sample with the
run's *own snapshotted ruleset*, the one section 14 used, and every count came
back identical: 1,141,832 pairs scored, 221,841 accept / 66,177 review /
853,814 reject, 591,804 vetoed, 96,099 vetoed from accept, `untrained_comparisons`
0, **457,941 entities after scoring**. `pairs.parquet` matches column for column
except `match_probability` and `match_weight`, which differ by at most 7e-13 —
EM's own run-to-run noise, since the two runs each trained their own model.
Stages 4 and 5 were verified the same way and reproduce `clusters.parquet`,
`entities.parquet`, `entity_report.json` and `score_eval.json` exactly (section
17). The corpus change of section 21 is a behaviour change for PSC's
`name_core_tfidf` and moves no stage output, because **there is no trained PSC
model**, so nothing reads that feature yet.

**The rebuilt token lists do move things, and they are meant to.** The run above
used the *current* default ruleset, with the junk postcodes and placeholder
numbers rebuilt from the full snapshot (section 23). Against section 14:

| | section 14 | now | why |
|---|---|---|---|
| units | 481,364 | 480,968 | four postcodes left the junk list, so those records have a usable postcode again and the address key merges them |
| person units | 449,397 | 449,396 | |
| organisation units | 31,967 | 31,572 | 34 more placeholder and formation-agent numbers nullified, changing what the registration key merges |
| pairs scored | 1,141,832 | 1,143,522 | |
| accept / review / reject | 221,841 / 66,177 / 853,814 | 224,060 / 64,435 / 855,027 | |
| vetoed (from accept) | 591,804 (96,099) | 591,383 (95,825) | |
| clusters | 457,941 | 457,686 | |
| **entities proposed** | **458,349** | **458,192** | |

Cluster statuses: 457,662 ok, 1 too_large, 23 weak_link, 0 conflict, 0
mixed_ids, 0 cross_track. 32 held groups open, 56 in the review queue, 7,211
attribute ties, 0 id collisions.

So the sample is 157 entities short of where it was, and the whole of that
difference is the token lists being right rather than sampled. Anyone comparing
a future run to section 14 should compare to this table instead.

> **Superseded in part by sections 100 to 104**, written by the session of
> 2026-09-21 under the project interpreter. `build_units` is verified and
> wired into stage 3, the four failing tests are gone, stage 5 is narrowed and
> vectorised, the refine column is found and priced, and the full snapshot is
> loaded, cleaned, keyed and unitised again with every count reproduced. Read
> 104 before planning anything: turning the hot-key control on for the first
> time found two faults in it that only exist above ten million units.

## 99. Where the session of 2026-09-18/19 stopped (written by the coordinating session)

State of the code: `main` = `378e4c2` plus one docs commit, all deployed to the server and green. Everything after that is on the branch `wip/psc-scale` (this commit): stage 3 streaming, stages 4 and 5 out of core, the corpus persistence, the hot-key block control, the loader's chunk files, the rebuilt PSC token lists, and a partly finished DuckDB rewrite of `build_units` in `app/pipeline/dedupe/units.py` (an agent was mid-edit when the session ended: review that file first, and `git diff main -- backend/app/pipeline/dedupe/units.py`).

Do NOT merge this branch into `main` or deploy it until the full suite is green under the PROJECT interpreter:
`cd backend && SITE_PASSWORD=testpass123 .venv/bin/python -m pytest tests -q -p no:cacheprovider`
Last result under that interpreter: 1,355 passed, 4 failed. All four are NaN-versus-None comparisons in tests written under the wrong interpreter: `test_stage_3_score.py::test_a_batched_overlay_writes_what_one_pass_writes`, `::test_a_writer_keeps_one_schema_when_the_first_batch_is_all_null`, `::test_rewriting_the_pairs_in_batches_is_the_in_memory_answer`, `test_stage_4_cluster.py::test_stage_4_writes_the_stored_clusters_fixture`. Fix by comparing with missing values normalised, without weakening what they prove (two of them prove that batched and one-pass writes give the same file).

THE INTERPRETER TRAP. The laptop's default `python` is miniconda (pandas 2.3.1, pyarrow 22, duckdb 1.4.4). The project and the server use `backend/.venv` (pandas 3.0.6, pyarrow 25.0.1, duckdb 1.5.5). The agent that wrote sections 15 to 26 ran its tests, measurements and the full-snapshot jobs under miniconda. Treat its timings and memory figures as approximate, re-measure the ones that matter, and run the real full-scale run with `backend/.venv/bin/python` only. Its report of a `build_units` crash on donations was a pandas 2 artefact.

Open, in order: (1) finish and verify `build_units` out of core (identity on the donations run and the PSC sample; a 32x tiling); (2) the four tests above; (3) stage 5: stop handing `unit_id`/`cluster_id` to `mint_entity_ids`, and vectorise `psc.mint_entity_ids` (it loops over every entity); (4) a refine column for the hot-key control that is independent of the blocking key (the forename initial is not); (5) the owner-approved full-scale run, stages 3 to 5, estimate first (section 24), vetoes on, `max_pairs` 150,000,000, disk floor 15 GB; (6) stage 2's 13 GB peak; (7) postcode still outweighs surname on the PSC person track.

Full-snapshot outputs of stages 0 to 2 (made under miniconda, fine as a scale proof): `/home/tomwright/psc_scratch/pscfull/runs/psc_full`. 11,799,425 entities after the exact keys with the rebuilt token lists.

## 100. Session of 2026-09-21 — items 1 and 2

Everything in this section and the ones after it was measured under the
**project interpreter**, `backend/.venv/bin/python` (pandas 3.0.6, pyarrow
25.0.1, duckdb 1.5.5). Where a figure disagrees with sections 15 to 26, this
one is the right one.

### 1. `build_units` in DuckDB: verified, and one thing it was not wired into

The rewrite is correct. The old pandas build is saved at
`/home/tomwright/psc_scratch/agent_units2/old_units.py`, and
`/home/tomwright/psc_scratch/full_venv/verify_units.py` runs both over the same
stored inputs and compares every value with the missing ones normalised, plus
every dtype and the column order.

| run | records | units | old vs new | against the stored `units.parquet` |
|---|---|---|---|---|
| donations `don_veto` | 51,839 | 22,375 | **identical** | **identical** |
| PSC `psc_final` (sample) | 499,971 | 480,968 | **identical** | one dtype |

`unit_members` is identical in both. The one dtype is `unit_id`: the stored
sample file says `object` because it was written under miniconda's pandas 2.3.1,
where `astype(str)` gives object; under pandas 3 it is the `str` dtype. The
donations file, written under pandas 3.0.6, matches exactly. **It is the
interpreter, not the code** — checked by reading the `pandas_version` out of
each file's footer. Speed on the sample: 45.6 s old, 27.6 s new.

**The defect worth having found.** `stage_3_score` never called the out-of-core
entry point. It read `records.parquet` whole into pandas, called the in-memory
`build_units`, which writes the two files and reads them **back** into pandas,
and then wrote them out again with `to_parquet`. Every gigabyte the rewrite
saved was spent again by its only caller. Stage 3 now calls
`build_units_files` with the paths and never holds either frame;
`scored_units.parquet` and the unit counts come off the written file through
two new helpers, `units.fingerprint_from_file` and `units.counts_from_file`.

**What the read found, and what was fixed.**

- *Identifier quoting.* Every column name already went through `_quote`. Pinned
  with a test that puts a column called `odd" name, x` through the build.
- *The tie-break.* `min(record_id)` is now `min(CAST(record_id AS VARCHAR))`.
  Inside the build the column is already VARCHAR so nothing moves, but
  `representatives()` is a public entry point and the rule `LINKAGE.md` states
  is text order — `"10"` before `"9"`.
- *A group row with no status.* The build read `status <> 'merged'`, and SQL
  makes that NULL, not true, so a row with a null status joined no unit and its
  held group vanished. pandas compared `None == 'merged'` and held it. Now
  `IS DISTINCT FROM`.
- *Trimming the existing entity id.* The pandas build used `str.strip()`;
  DuckDB's bare `trim` takes spaces and nothing else, so a label ending in a
  tab would have differed. `_trim` now names the whole ASCII whitespace set.
- *The profile's aggregate hook.* It runs over the pooled units only, which is
  right exactly while every column it returns is also a records column — both
  shipped profiles are. A hook that invents a column would have given every
  unit of one a silent null, because the pandas build ran over every unit. That
  now raises with the column named.
- *`held_group_id` smallest, `existing_entity_ids` sorted and `" | "`-joined,
  the priority sum, the column order, zero pooled units and zero records* were
  all read against the old build and are right. The last two now have tests.

`tests/test_units.py` is 19 tests, up from 11.

### 2. Stage 5 at scale

**(a) The mint frame.** `Profile.mint_entity_ids` was handed `unit_id`,
`cluster_id` and `basis` beside `record_id` and `entity_key`. Neither shipped
hook reads any of the three, and at the full snapshot `unit_id` and
`cluster_id` alone are two more copies of a 37-character id per record — about
9 GB. The proposal side is now `MINT_PROPOSED_COLUMNS`: `record_id`,
`entity_key`, `track`. **`track` stays on purpose.** Dropping it would stop
pandas suffixing the record side's `track` to `track_y`, the PSC hook would
start finding a `track` column it has never found, and every PSC organisation
id would change. `docs/ENTITIES.md` says so.

**(b) The per-entity loop.** `psc.mint_entity_ids` walked the sorted keys one
at a time — 11.3 million iterations at full scale, building a dict of the same
size — and called `_single` once per group. Both are whole-frame now: the
counter is a `cumsum` over the keys that need one, so the nth key needing an id
still takes the nth counter value and the high-water mark moves by exactly the
number minted. At 2,000,000 keys: **1.87 s against 4.94 s**, same ids.
`_loop_mint` in `tests/test_psc_profile.py` keeps the old walk and holds the two
together over every case the rule distinguishes. Worth knowing: the shipped PSC
ruleset derives no `company_number_padded`, so on a shipped run every id takes
the counter branch and the agreed-number branch is untested by the data.

**(c) `routers/entities.py`.** `_id_statuses` opened a bare `duckdb.connect()`
and read every row of `entities.parquet` — eleven million at full scale — on
every entities request, to build a map the reader then ignores, because
`id_status` is a column of the file and `_base_sql` already selects it as
`stored_id_status`. A file too old to have the column had nothing to build the
map from, so the old code returned `{}` for that case too. It now returns `{}`
always. The other two bare connections in the file are on `duckdb_conn.connect`
with the run's temp directory, so they carry the memory cap and the spill
limit.

**Proof it is all a no-op.** Stage 5 was re-run on a copy of both finished runs
and `entities.parquet` compared column by column, plus `entity_report.json`:
donations `don_veto` 51,839 rows identical, PSC `psc_final` 499,971 rows and
**458,192 entities** identical, which is section 26's figure.

Full suite under the project interpreter: **1,375 passed** (1,359 before, 16
tests added).

## 101. What the web tool needs to show a finished run

Written while the full run was loading, so the shape is here and section 104
carries the full-scale sizes.

### The files

A run folder is `<DATA_DIR>/runs/<run_id>`. These are the ones a reader opens;
the sizes are the 500,000-record sample, and the full snapshot is roughly 24 to
30 times each.

| file | written by | read by | sample |
|---|---|---|---|
| `records.parquet` | stage 1 | records list and detail, every group, cluster and entity detail, the evaluation | 66 MB |
| `units.parquet` | stage 3 | pairs list and detail, clusters, entities, blocking | 79 MB |
| `unit_members.parquet` | stage 3 | every detail that shows a unit's records; the label overlay | 33 MB |
| `pairs.parquet` | stage 3 | pairs list, pair detail, the histogram, cluster edges | 58 MB |
| `clusters.parquet` | stage 4 | the review queue and the cluster detail | 48 MB |
| `entities.parquet` | stage 5 | the Entities tab and the publish preview | 68 MB |
| `exact_groups.parquet` | stage 2 | the exact-groups list and detail, held groups in the queue | 1.8 MB |
| `events.parquet` | stage 0 | the evidence rows on a unit, when the profile has any | 20 MB |
| `records_raw.parquet` | stage 0 | nothing the UI reads; kept so stage 1 can re-run | 38 MB |
| `scored_units.parquet` | stage 3 | `never_scored` — which units no run has compared | 16 MB |
| `config/ruleset.json`, `config/linkage_settings.json` | the run | the run's own rules, and the config version a reader shows | 2 files |
| `blocking_report.json` | stage 3 | the blocking tab: pairs per rule, the budget, the control | 5 KB |
| `score_eval.json` | stage 3 | precision and recall against the existing labels | 18 KB |
| `exact_eval.json` | stage 2 | the same for the match keys alone | 2 KB |
| `entity_report.json` | stage 5 | the entity summary and the collisions | 10 KB |
| `contradictions.json` | stage 3 | FALSE labels an exact key contradicts | small |
| `splink_model_person.json`, `splink_model_organisation.json` | stage 3 | the model tab's learned weights | 15 KB each |
| `diagnostics/*.html` | stage 3 | the six Splink charts, served as files | small |

`duckdb_tmp/` is spill. It is not needed and should not be copied.

### Getting the run into the instance's database

**There was no way to do this.** A `runs` row is only ever written by
`POST /api/runs`, which creates the row and *then* runs the pipeline into the
folder. A folder built anywhere else was invisible to the tool.

`backend/scripts/adopt_run.py` is that path.

```bash
# on the server, with the folder already under <DATA_DIR>/runs/
cd /srv/dedupe/backend
.venv/bin/python scripts/adopt_run.py /srv/dedupe/data/runs/psc_full \
    --db /srv/dedupe/data/app.db \
    --label "PSC full snapshot 2026-09-18" \
    --counts /srv/dedupe/data/runs/psc_full/counts.json \
    --input-filename persons-with-significant-control-snapshot-2026-09-18.zip
```

It writes the `runs` row a completed run would have had — `status` `complete`,
`started_at` and `finished_at` from the folder's own mtimes, the thresholds out
of `config/linkage_settings.json`, and `counts_json` — and the
`config_versions` row the run's config snapshot describes, reusing an existing
version when the text matches so adopting several runs of one configuration
leaves one version.

`--counts` is the union of what the five stages returned, in the pipeline's
snake_case. Every key it leaves out is derived from the folder itself: row
counts of the five parquets, the bucket counts out of `pairs.parquet`, the
distinct entity ids, and whatever the report JSONs hold. So a folder with no
counts file still shows its numbers.

**It never overwrites.** A run id the database already holds is refused with a
message naming it, unless the row is exactly the one the script would write, in
which case it says "already registered" and changes nothing. Re-running it is
safe. `tests/test_adopt_run.py` is twelve tests, including one that adopts a
folder and then asks `GET /api/runs` for it.

### Copying the run to the server

```bash
rsync -av --progress \
  --exclude duckdb_tmp --exclude records_raw.parquet \
  /home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full/ \
  tom@server:/srv/dedupe/data/runs/psc_full/
```

`records_raw.parquet` is excluded because nothing the UI reads opens it; keep
it if stage 1 may be re-run on the server.

## 102. The readers at PSC scale

Timed through the reader functions directly against the finished 500,000-record
sample run (`psc_final`: 1,143,522 pairs, 480,968 units, 458,192 entities),
under the project interpreter. Two of them were already over the two-second
line at a thirtieth of the full snapshot.

| call | before | after |
|---|---|---|
| records list, page 1 | 0.09 s | 0.10 s |
| records list, person, page 40 | 0.09 s | 0.10 s |
| exact groups list, page 1 | 0.08 s | 0.11 s |
| **pairs list sorted by score** | **2.51 s** | **1.21 s** |
| **pairs list sorted by priority** | **3.32 s** | **1.20 s** |
| **pairs list, review bucket, by score** | **2.34 s** | **0.88 s** |
| clusters queue, page 1 | 0.46 s | 0.54 s |
| entities list, page 1 | 0.47 s | 0.53 s |
| entities list sorted by size | 0.44 s | 0.51 s |
| one pair detail | 0.49 s | 0.49 s |
| **one cluster detail** | **11.99 s** | **0.50 s** |
| one entity detail | 0.18 s | 0.19 s |

### One cluster detail was 200 queries

`get_cluster` called `_members_of(con, run_dir, unit_id)` once per unit shown,
up to `MAX_UNITS` — 200 — and each call was a scan of `unit_members.parquet`
joined to `records.parquet`. Twelve seconds on the sample; at sixteen million
records both files are thirty times bigger and the count of calls does not
fall. It is now one query with a `row_number()` window, split per unit in
Python: `_members_for` and `_events_for`, with `_members_of` and `_events_of`
kept as one-unit wrappers for the other callers. A held group's detail did the
same thing per record and now uses the same batch. **24× faster, same output.**

### The pairs list carried every unit column three times

`get_pairs` asks one query three things — the chip counts, the filtered total,
and the page — and that query joined `units.parquet` twice, all sixty-eight
columns, so the whole unit row travelled through all three. Only the page ever
shows a unit column.

The query now reads three unit columns: `unit_id`, `existing_entity_id` (the
import agreement) and `name` (the search and the name sort). Every filter and
every sort is satisfied by those three plus the pair row. The page then runs a
second step that fetches the two whole unit rows for its fifty pairs —
`units.parquet` is written in `unit_id` order, so the id filter is answered
from the row-group statistics rather than by a scan. **2.6× to 2.8× faster.**

Eight parametrised tests in `tests/test_pairs_api.py` run the list both ways —
narrow and wide — over every sort, a filter, a search and a page offset, and
require the same totals, the same counts and the same items; one more requires
the page's `left`/`right` structs to carry every unit column. A test in
`tests/test_entities.py` counts the member lookups a cluster detail makes and
requires exactly one.

### Two frames stage 3 held for no reason

`exact_groups.parquet` was read into pandas at the top of stage 3 and used at
the bottom, and the overlay projection of `units.parquet` was read before the
blocking budget and used after both tracks were scored. Both were resident
through training and prediction, which is where the stage's peak is. Both are
now read where they are used.

## 103. A refine column that is independent of the blocking key

Section 19 built the hot-key control and then showed why turning it on with
`forename_initial` was not worth doing: PSC's hot person blocks are placeholder
names, everybody in them shares one initial, and almost the whole cut came from
`drop_above` throwing the biggest block away. **The refine column has to be
independent of the route's own key.** This section prices the candidates on the
full snapshot's units.

The candidates, per route, are the columns the route's own key does not already
fix:

| track | route | key | candidates priced |
|---|---|---|---|
| person | pb1 | surname sound + birth year + month | `postcode_district`, `nationality_norm`, both |
| person | pb2 | surname3 + forename initial + birth year + month | `postcode_district`, `nationality_norm` |
| person | pb3 | name fingerprint | `postcode_district`, birth year + month, `nationality_norm` |
| person | pb4 | postcode district + surname sound | birth year + month, birth year, `nationality_norm` |
| person | pb5 | forename sound + birth year + month, surname differs | `postcode_district`, `nationality_norm` |
| person | pb6 | both name sounds, birth year missing | `postcode_district`, `nationality_norm` |
| organisation | ob1 | core name | `postcode_district`, `country_canonical`, `type_bucket` |
| organisation | ob2 | core-name sound + type | `postcode_district`, `country_canonical` |
| organisation | ob3 | registration number | `postcode_district`, `type_bucket`, `country_canonical` |
| organisation | ob4 | first token + postcode district | `type_bucket`, `country_canonical` |

The task named `nationality_clean`; the column the ruleset writes is
`nationality_norm` (home nations collapsed), and that is what was priced.
`company_number` was **not** priced for the person routes: two PSC filings of
one person are almost always at different companies, so refining a person block
on the company number would leave almost nothing comparable. It is what the
whole exercise is trying to find.

Every figure is `linkage.price_rule` — DuckDB group arithmetic, no pair
materialised — on the run's own `units.parquet`. Each route is also priced at
`on_oversize: "drop"` as the reference, because section 19 found that dropping
alone did most of the work.

**pb6 is an upper bound.** Its `(l.dob_year_clean IS NULL OR r.dob_year_clean
IS NULL)` is not a condition a `GROUP BY` can apply, so `price_rule` returns
`exact: false`. On the sample its group-arithmetic price was 1,007,328 and its
true Splink count was **2,323**, because nearly every person unit does have a
birth year. The true figure for pb6, and the final total, come from Splink's
own `count_comparisons_from_blocking_rule` — the same call the blocking budget
makes — which is run before the settings are changed.

**The EM training rules cannot take a control.** `em_blocking_rules` is a list
of SQL strings; `blocking_budget_report` prices each one with no control and
fails the run if the worst of them is over `max_pairs`. So `max_pairs` has to
cover the worst EM rule as well as the prediction total, and if an EM rule is
too big the only levers are its own SQL and the budget. A refine column would
not be safe there anyway: whatever it held equal inside the training block
would stop that comparison being estimated, which is the exact defect section 3
exists to prevent, and `linkage_warnings()` would not see it because it reads
the rule text.

## 104. The full run, under the project interpreter

Ran in `/home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full`, one
stage at a time, `CLEAN_BATCH_ROWS=100000`, `DUCKDB_MAX_TEMP=20GB`, the disk
checked before and after each. Every number here is
`backend/.venv/bin/python` (pandas 3.0.6, pyarrow 25.0.1, duckdb 1.5.5).

| stage | time | peak RSS | disk free after | vs the miniconda run (section 23) |
|---|---|---|---|---|
| 0 load | 1,033.3 s | 1,704 MB | 68.6 GB | 1,207.8 s, 1,731 MB |
| 1 clean | 500.6 s | 4,058 MB | 66.7 GB | 543.8 s, 2,436 MB |
| 2 exact | 303.2 s | **18,109 MB** | 66.5 GB | 343.5 s, 13,100 MB |

Stage 0 read 15,952,486 input rows, dropped 545 super-secure and 922,678 with
no name, and kept **15,029,263** records — 13,921,888 person and 1,107,375
organisation. Every one of those figures is identical to section 23's.

Stage 2's exact keys reproduce section 23's rebuilt-list figures to the
record: **1,811,774** merged groups over **5,041,612** records, **3,842** held
groups over **332,009** records, **11,799,425** entities after the keys, 0
conflicts.

**Both stages are heavier under pandas 3 than the miniconda run reported, and
stage 2 is much heavier: 18.1 GB against 13.1 GB.** Stage 1 is 4.1 GB against
2.4 GB. That is 62% of this machine's RAM for one stage, and it is the
projection of fourteen columns over fifteen million records held in pandas —
the B3 work that was already named as the next thing to batch after
`build_units`. Nobody should size a server from the old numbers, and stage 2
will not fit the server's 6 GB budget by a factor of three.

### The token lists were already right

Section 23's lists were counted under the wrong interpreter, so they were
re-counted on this run's own `records.parquet`. They are data facts and they
reproduce exactly.

- **`junk_postcodes`.** deduping's proportion is more than 5,000 records per
  7.5 million individuals, which on 13,921,888 person records is a threshold of
  **9,281**. Twenty postcodes are over it. The stored list is those twenty:
  nothing missing, nothing on the list that falls under the threshold. The
  biggest is `WC2H 9JQ` at 180,477 records, then `N1 7GU` 152,796,
  `EC1V 2NX` 150,044, `CF14 8LH` 95,789, `EC2A 4NE` 64,331.
- **`placeholder_numbers`.** Thirty-nine registration numbers are used by ten
  or more distinct filed names and are not already caught by `fake_nulls`.
  Every one is on the list. The other eight entries (`00000001`, `00000002`,
  `00000003`, `00123456`, `01234567`, `11111111`, `88888888`, `99999999`) are
  the hand-written placeholders, which stay whether or not this snapshot
  happens to contain them — the list is a rule, not a census. The worst are
  `00000000` (231 distinct names), `0` (146), `00000` (128), `000000` (79) and
  `ENGLAND` (67).

**No rebuild was needed.** `defaults/psc/ruleset.json` is unchanged.

### The bug the full snapshot found in `build_units`

The DuckDB build was identical to the pandas one on the donations run and on
the 500,000-record sample, and it still could not build the full snapshot. It
died here:

```
_duckdb.OutOfMemoryException: Out of Memory Error: failed to pin block of
size 256.0 KiB (7.4 GiB/7.4 GiB used)
  CREATE OR REPLACE TABLE voted AS SELECT u.unit_id, v0.…, v1.…, … v60.…
  FROM (SELECT unit_id FROM pooled_sizes) u
  LEFT JOIN vote_0 v0 ON … LEFT JOIN vote_1 v1 ON … (61 of them)
```

Each column's modal vote is its own table, and they were all joined to the unit
list in **one statement**. That gives DuckDB one hash table per column, all
live at once, and **a hash join's build side is pinned** — the buffer manager
cannot evict it, so `memory_limit` does not bound it. The query fails instead
of spilling. At the full snapshot that is 61 hash tables over 1.8 million
pooled units.

It could not have shown up at any smaller scale: the same 61 joins over the
sample's 31,755 pooled records fit in a few hundred megabytes.

`_vote_table` now folds the votes in a batch of columns at a time
(`UNIT_VOTE_JOIN_BATCH`, default 8), dropping each vote table once it is in. At
most eight hash tables are live. The cost is rewriting `voted` once per batch —
a sequential scan and write of a table with one row per pooled unit, which is a
third of the records at PSC scale and a sixteenth on the sample. Identity on
both profiles was re-checked afterwards and is unchanged.

**The general lesson, worth carrying:** `memory_limit` does not bound a DuckDB
query whose plan pins many build sides at once. Widening a query by one join
per column is the shape that does it.

### What the routes cost at full scale, and what refines them

11,205,785 person units and 593,640 organisation units. Every figure is
`price_rule` group arithmetic. pb6's is an upper bound (`exact: false`).

**Uncontrolled, the person track is 1.3 billion pairs.**

| route | uncontrolled | drop > 60 only | **refine, > 20, drop > 60** | units dropped | unrefinable |
|---|---|---|---|---|---|
| pb1 surname sound + dob | 198,530,709 | 49,783,443 | **20,130,171** | 79,408 | 308,457 |
| pb2 surname3 + initial + dob | 114,611,766 | 20,499,131 | **16,755,794** | 78,845 | 59,753 |
| pb3 name fingerprint | 351,486,369 | 30,379,752 | **13,563,011** | 81,642 | 192,790 |
| pb4 postcode district + surname sound | 127,149,799 | 30,043,448 | **16,907,827** | 79,408 | 2,210 |
| pb5 forename sound + dob, surname differs | 527,628,584 | 45,769,400 | **11,624,404** | 79,120 | 546,236 |
| pb6 both name sounds, no dob | ≤582,535,879 | ≤43,054,672 | **≤15,762,398** | 82,709 | 353,152 |
| **person total** | **≤1,901,943,106** | ≤219,529,846 | **≤94,743,605** | | |
| ob1 core name | 47,524,349 | 303,340 | **398,683** | 69,887 | 12,250 |
| ob2 core-name sound + type | 55,301,940 | 371,014 | **558,336** | 69,913 | 14,408 |
| ob3 registration number | 26,960,626 | 349,935 | **398,407** | 54,742 | 7,713 |
| ob4 first token + postcode district | 34,919,308 | 472,175 | **477,181** | 75,635 | 0 |
| **organisation total** | **164,706,223** | 1,496,464 | **1,832,607** | | |

The refine column is `postcode_district` on every person route but pb4, which
already fixes it and takes `dob_year_clean` + `dob_month_clean` instead. The
organisation routes take `postcode_district` except ob4, which already fixes it
and takes `type_bucket`.

**This is the opposite of section 19's finding, and the reason is the column.**
Refining on `forename_initial` bought 4% to 27% on the sample, because the hot
blocks are placeholder names where everyone shares an initial. Refining on the
postcode district buys **86% to 98%** of the same routes at full scale, because
where somebody lives has nothing to do with what their name sounds like. That
is what "independent of the blocking key" means in practice.

**Dropping alone is not the answer, even though it cuts slightly more.**
`on_oversize: "drop"` at 60 costs pb5 **4,720,900 units** — 42% of the person
track can no longer be compared on that route at all. Refining at 20 costs
79,120, which is 0.7%, and leaves 546,236 more unrefinable because they have no
postcode. Nearly the same cut for a sixtieth of the loss.

### The estimate, written before stage 3 was started

| | |
|---|---|
| units | 11,799,425 — 11,205,785 person, 593,640 organisation |
| person pairs after the control | ≤94,743,605 blocked comparisons |
| organisation pairs after the control | 1,832,607 |
| predict | the sample scored 2.4M comparisons in 1.65 s of Splink `predict`; at a conservative 400,000/s, 96.6M is **about 4 minutes** |
| `pairs.parquet` | the sample kept 44% of its blocked comparisons above the 0.05 candidate floor at 50 bytes a pair, so **about 42 million pairs and 2.1 GB** |
| stage 3 pandas | overlay projection 1.6 GB, person budget rows 1.8 GB, person Splink projection 2.3 GB plus the frame handed to Splink 2.1 GB, and at the end records 0.95 GB + members 1.3 GB + groups 0.7 GB + units 0.9 GB. **Peak about 6 GB**, plus DuckDB's cap. |
| stage 4 | four columns of the units and the accepted edges; **3 to 4 GB** |
| stage 5 | the mint frame is now three proposal columns and three record columns over 15.0M rows, about 3.8 GB, plus an 11.3M-row per-entity summary. **8 to 10 GB** |
| disk | 63.9 GB free. Spill is the term that matters: the session that spilled 53 GB did it with a 170-million-pair training rule and both `retain_*` flags on, which is 312 bytes a pair, and those flags are still on because the gamma columns depend on them. |

**Measured, not estimated, because these stages have already run:** stage 0
1,033 s / 1.7 GB, stage 1 501 s / 4.1 GB, stage 2 303 s / **18.1 GB**,
`build_units` 448 s / 11.9 GB.

### Stage 3 cannot run as the settings stand, and the blocker is EM

`em_blocking_rules` takes no hot-key control — it is a list of SQL strings, and
`blocking_budget_report` prices each one uncontrolled and fails the run if the
worst is over `max_pairs`. At full scale:

| rule | as shipped | drop > 60 | drop > 20 |
|---|---|---|---|
| person em1 `surname sound + forename sound` | **582,535,879** | 43,054,672 | 13,640,689 |
| person em2 `dob + postcode district` | **105,630,616** | 58,985,721 | 30,041,203 |
| organisation em1 `core-name sound + type` | 48,450,086 | 639,077 | 188,006 |
| organisation em2 `registration number` | 26,960,626 | 349,935 | 34,106 |

**582.5 million training pairs is three and a half times the rule that took the
machine down.** Section 6's 170-million-pair rule spilled 53 GB and ran until a
40-minute timeout killed it, with the same two `retain_*` flags on. At 312
bytes a pair, 582.5 million is about **180 GB of spill** against 63.9 GB of
free disk. It cannot run, and it would not be wise to start it to find out.

**What does not work.** Adding a column to em1's SQL is the change that needs no
code, and every candidate fails:

| em1 + | pairs | why not |
|---|---|---|
| `residence_norm` | 520,137,998 | barely cuts it |
| `nationality_norm` | 461,904,779 | barely cuts it |
| `surname_clean` in place of the sound | 398,731,789 | not enough, and it narrows what the surname comparison can learn |
| `dob_year_clean` | 111,156,550 | em2 holds the birth year equal too, so no rule lets it vary — `linkage_warnings()` fires, which is the defect of section 3 |
| `postcode_district` | **40,630,627** | em2 holds the district equal too, so `cl.PostcodeComparison`'s area and all-other levels never vary in training and come back with no `m`. `linkage_warnings()` would **not** catch it — it matches the comparison's column name, `postcode_clean`, against the rule's, `postcode_district` — but `untrainedComparisons` would not be 0. |

**The smallest change that makes it fit** is to let an `em_blocking_rule` carry
the same hot-key control a prediction rule can, and give person em1
`max_block_size: 60, on_oversize: "drop"`. That is 582,535,879 → 43,054,672,
and em2 105,630,616 → 58,985,721. No refine column is needed, so nothing is
held equal that was not held equal before and both guards stay clean. What it
drops from *training* is the hot blocks — the placeholder-name blocks of
section 19 — which is not a recall loss, because prediction still sees those
units under its own control and a model should not be learning its weights from
`FLTS` and `PRSNS` in the first place. It is exactly what the control was
built for.

### Turning the control on found two more things, and neither could be seen below 11 million units

Section 19 shipped the hot-key control with "No settings file was edited" and
"it costs nothing while unused". Turning it on for the first time is what
found these. Both are in shared code, both are fixed, and both are the kind of
fault that only exists at scale.

**1. The control inlines every hot key, and the cap was 5,000.** A Splink
blocking rule is a predicate over `l.` and `r.` columns and has nowhere to read
a set from, so `controlled_sql` writes the oversized key values into the SQL as
literals. `MAX_INLINE_KEYS` was 5,000. The PSC person routes need this many at
`max_block_size: 20`:

| route | pb1 | pb2 | pb3 | pb4 | pb5 | pb6 |
|---|---|---|---|---|---|---|
| oversized blocks at 20 | 68,981 | 11,725 | 41,423 | 32,309 | 91,047 | 66,807 |
| at 60 | 11,239 | 913 | 9,550 | 3,735 | 32,863 | 17,564 |
| at 200 | 231 | 109 | 1,404 | 422 | 5,869 | 2,861 |

Only pb2 and pb4 fit under 5,000, and only by loosening the limit to 60, which
costs most of the cut. The cap is now 100,000. That generates **4.8 MB of
blocking SQL across the six person routes** — pb5 alone is 1.6 MB — and takes
0.5 to 0.8 seconds a rule to build. The blocking report carries the size of the
generated SQL rather than the text, because the report is served to a browser.

**2. Splink cannot count a refined rule, at any scale.** `on_oversize:
"refine"` generates

```
<the rule> AND (<key> NOT IN (…) OR <the refine columns agree>)
```

and that `OR` stops Splink's blocking analyser recognising the equi-join at
all. `count_comparisons_from_blocking_rule` falls back to the cartesian bound
and refuses to count. Measured on the full snapshot's 593,640 organisation
units, ob1:

| | Splink's counter | `price_rule` |
|---|---|---|
| uncontrolled | 47,524,349 | 47,524,349 |
| `drop` over 20 | 25,558 | 25,558 |
| `refine` over 20 | **refused** — "exceeded max_rows_limit … above 3.524e+11" | 398,683 |

3.524e+11 is 593,640 squared over two. Splink was estimating the cartesian
product. A plain `NOT IN`, a row-constructor `NOT IN`, and the rule wrapped in
brackets all count correctly — it is specifically the `OR`.

`blocking_budget_report` is the guard that decides whether a run may start, so
until this was fixed **turning the control on would have failed every run**,
whatever `max_pairs` said. The budget now prices a refined rule with
`price_rule` and everything else with Splink's counter: `count_rule_pairs`
chooses. The two agree to the pair wherever both can answer, and `price_rule`
was already checked against materialised pairs in sixteen cases on the sample
(section 19).

**DuckDB itself has no trouble with the `OR`** — the rule still carries
top-level equalities, so it hash-joins on those and applies the rest as a
residual. It is only the estimator that is defeated.

### The controls that shipped

`app/profiles/defaults/psc/linkage_settings.json` now carries, on **every**
route:

```json
{ "max_block_size": 20, "on_oversize": "refine",
  "refine_with": ["postcode_district"], "drop_above": 60 }
```

with `dob_year_clean` + `dob_month_clean` on pb4 and `type_bucket` on ob4,
because those two routes already fix the postcode district in their own key.
`max_pairs` is **150,000,000** on the person track — about 1.5× the priced
94,743,605 — and 5,000,000 on the organisation track, which is already
generous against 1,832,607.

Both EM training rules on both tracks carry `{"max_block_size": 60,
"on_oversize": "drop"}`. No refine column, so nothing is held equal that was
not held equal before, and both guards stay clean:
`linkage_warnings()` returns zero and `validate_linkage_settings()` returns
zero errors against the shipped ruleset.

A test in `tests/test_block_control.py` now asserts that every PSC route
carries a control, that its `refine_with` shares no column with what the
route's own key already holds equal — the whole lesson of section 19 — and
that donations still carries none.

**3. Splink refuses more than refined rules.** The first full-scale stage 3
died in the budget check on pb5, which blocks on the forename sound and the
date of birth and then filters on
`l.surname_metaphone <> r.surname_metaphone`. Splink counts the block *before*
the filter, and at 11.2 million person units that is over its own
`max_rows_limit`, so it returns the string `exceeded max_rows_limit` where an
integer belongs and `count_blocking_pairs` raises. **This has nothing to do
with the control** — an uncontrolled full-scale run would have died in the same
place. `count_rule_pairs` now falls back to `price_rule` when Splink will not
give a number, and `pairs_before_control` — which is shown, not acted on — is
priced rather than counted, so it can never lose a run.

All three faults are covered by tests in `tests/test_block_control.py`,
including one that pins the budget's answer for a refined rule against
`price_rule` and asserts it is not the cartesian bound.

### The budget, as stage 3 priced it on the full snapshot

Every figure is what `blocking_budget_report` wrote, and every one matches the
estimate above to the pair.

| track | rule | before the control | after |
|---|---|---|---|
| person | pb1 | 198,530,709 | **20,130,171** |
| person | pb2 | 114,611,766 | **16,755,794** |
| person | pb3 | 351,486,369 | **13,563,011** |
| person | pb4 | 127,149,799 | **16,907,827** |
| person | pb5 | 527,628,584 | **11,624,404** |
| person | pb6 | ≤582,535,879 | **≤15,762,398** |
| **person total** | **≤1,901,943,106** | **≤94,743,605** of 150,000,000 |
| person | em1 | 582,535,879 | **43,054,672** |
| person | em2 | 105,630,616 | **58,985,721** |
| organisation | ob1 | 47,524,349 | **398,683** |
| organisation | ob2 | 55,301,940 | **558,336** |
| organisation | ob3 | 26,960,626 | **398,407** |
| organisation | ob4 | 34,919,308 | **477,181** |
| **organisation total** | **164,706,223** | **1,832,607** of 5,000,000 |

**A twentyfold cut on the person track and a ninetyfold cut on the
organisation track**, for about 79,000 units dropped per person route out of
11,205,785 — 0.7%.

pb6's figure is the `price_rule` upper bound, because the refined path is
priced by group arithmetic and its `(l.dob_year_clean IS NULL OR
r.dob_year_clean IS NULL)` is not something a `GROUP BY` can apply. The budget
is therefore conservative for that one rule, which is the right direction for a
budget to be wrong in.

### Stage 3 on the full snapshot, as it ran

| phase | time | what it did |
|---|---|---|
| build units | 458.2 s | 15,029,263 records → 11,799,425 units, the same as the standalone build |
| blocking budget | 1,058.4 s | every rule priced after its control; both tracks under budget |
| u by random sampling | 24.2 s | 5,000,000 pairs |
| EM, person em1 | the training block took about ten minutes to build at 43,054,672 pairs, then iterated |

**The budget check is 17.6 minutes and most of it is the control.** The
oversized keys are measured once per rule to build the SQL and the rule is then
priced, and `train_track` measures them a second time because it has to apply
the control to the frame Splink will actually score. At 11.2 million person
units each measurement is about 50 seconds a rule. Caching the oversized key
sets between the budget and the training would take about seven minutes off
every PSC run, and is the obvious next thing to do to this code.

**Peak RSS through stage 3** was 19.9 GB, reached while Splink was loading the
person frame. That is the highest of the whole pipeline — higher than stage
2's 18.1 GB — and it is 69% of this machine. The DuckDB cap was 10 GB and
`DUCKDB_MAX_TEMP` 35 GB; the spill reached about 11 GB building the first EM
training block, taking the disk from 63.6 GB free to 53 GB.

## 105. Where the session of 2026-09-21 stopped

**The code is finished and green: 1,407 tests pass under
`backend/.venv/bin/python` on a quiet machine.** Items 1, 2, 3 and 5 are done.
Item 4 — the full run — is through stages 0, 1 and 2 and the unit build, and
stage 3 reached the person `predict` before the DuckDB spill cap stopped it.
The section above this one has the numbers and names the change that makes it
fit: **predict route by route**. Nothing else is in the way, and the model
trained cleanly — Splink reported every comparison estimated, so the guard of
section 3 holds at full scale.

**The run.** `/home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full`,
driven one stage at a time by `/home/tomwright/psc_scratch/full_venv/run_stage.py`
(`run_stage.py 3` writes its timings into `full_timings.json`). Stages 0 to 2
and the unit build are complete and every count reproduces section 23. Stage 3
passed the blocking budget and is in EM on the person track. When it finishes:

```bash
cd /home/tomwright/PycharmProjects/dedupe_ui/backend
for STAGE in 4 5; do
  nohup env PROFILE=psc SITE_PASSWORD=x PYTHONPATH=$PWD \
    DATA_DIR=/home/tomwright/psc_scratch/full_venv/fulldata \
    SPLINK_MEMORY_LIMIT=10GB DUCKDB_MAX_TEMP=35GB \
    bash -c ".venv/bin/python /home/tomwright/psc_scratch/full_venv/run_stage.py $STAGE; echo EXIT=\$?" \
    > /home/tomwright/psc_scratch/full_venv/stage$STAGE.log 2>&1
done
```

Then, for the report item 4 asks for:

- `/home/tomwright/psc_scratch/full_venv/evidence.py <run_dir>` — the learned
  m, u and match weight per level for both tracks, buckets before and after
  the vetoes, per-veto hits, and thirty person and fifteen organisation pairs
  sampled **across** each bucket's score range with both names, the birth
  month and year or the registration number, the postcode, the score and the
  gamma levels that decided it. It uses `ntile` so the sample is spread, not
  the top of the list.
- `/home/tomwright/psc_scratch/full_venv/time_readers.py <run_dir>` — the
  reader timings of section 102, against the full run folder this time.
- `backend/scripts/adopt_run.py <run_dir> --label …` to register it.

**Two things to fix before anyone runs this again.**

1. **Cache the oversized key sets.** The budget measures them to build each
   rule's SQL, and `train_track` measures them again because it applies the
   control to the frame Splink scores. That is about 50 seconds a rule twice
   over on 11.2 million units — seven minutes of a PSC run, for an answer that
   cannot have changed.
2. **Stage 2 needs batching.** 18.1 GB at fifteen million records, which is the
   highest of any stage but the Splink load and three times the server's
   budget. It is the projection of fourteen columns held in pandas, and it is
   the B3 work that was already named as next after `build_units`.

**And one thing to decide, not to fix.** Section 5 recorded that postcode
learned more weight than an exact surname match on the PSC person track, and
that deduping's own D7 warned about exactly this. Nothing in this session
changed that, and the hot-key control has now made the postcode district
load-bearing in the blocking as well. Read the person pairs before trusting
the accepted merges.

### Stage 3 got as far as the person predict, and then the spill cap stopped it

The person track ran for **6,484.5 s** — the control SQL regenerated per rule,
the prior, u by random sampling in 24.2 s, EM on em1 converging in 10
iterations (1,987.4 s) and on em2 in 5 (2,664.7 s), then 375.1 s of prediction
blocking — and died inside `predict`:

```
_duckdb.OutOfMemoryException: Out of Memory Error: failed to offload data
block of size 256.0 KiB (32.5 GiB/32.5 GiB used)
```

32.5 GiB is `DUCKDB_MAX_TEMP=35GB`. **This is the bounded failure the cap
exists to produce** — the run stopped in seconds with an error that names the
problem, instead of filling the disk and taking the machine down, which is what
happened to the session of section 7. The spill was released on exit and the
disk went straight back to 64 GB free.

**What it measures.** 94,743,605 blocked comparisons need **more than 35 GB**
of DuckDB spill at this comparison width. That is about 370 bytes a pair,
against the 312 bytes a pair section 6 measured, and my own estimate of 30 GB
was the one number in section 104 that was wrong — and wrong in the dangerous
direction.

**Two things the model did get to, and both are good news.** Splink reported
*"Your model is fully trained. All comparisons have at least one estimate for
their m and u values"* — so `untrainedComparisons` would have been 0, which is
the guard of section 3 holding at full scale. And `blocking_report.json` was
written before the failure and is the evidence for the whole of section 104:
person 94,743,605 of 150,000,000, organisation 1,832,607 of 5,000,000, neither
over budget, neither EM rule over budget.

**The obvious lever is closed.** The width comes from
`retain_matching_columns` and `retain_intermediate_calculation_columns`, and
the comment in `train_track` says the gamma columns need both. That was an
assumption; it is now measured. With `retain_matching_columns=False` Splink
emits no `gamma_*` columns at all:

```
True  -> …, gamma_forename_canon, gamma_surname_clean, surname_clean_l, surname_clean_r, …
False -> bf_forename_canon, bf_surname_clean, match_key, match_probability, match_weight, unit_id_l, unit_id_r
```

Turning the flags off would halve the spill and lose the per-pair explanation
the review screen is built on. It is not a trade to make quietly.

**So the smallest change that makes the person track fit is to predict route by
route.** Splink is asked for all six rules in one `predict`, and the comparison
table it builds is the sum of them. Six passes of about 16 million comparisons
each need about 6 GB of spill apiece, appended to the predictions file the way
`PairWriter` already appends to `pairs.parquet`. Nothing about the model or the
blocking changes; only how many pairs are alive at once. The other levers, for
completeness: raise `DUCKDB_MAX_TEMP` to 45–50 GB (64 GB free against a 15 GB
floor leaves 49 GB, so this is a coin flip, not a fix); tighten
`max_block_size` below 20, which starts costing real units; or score the two
tracks in separate processes, which does not help because the person track
alone is the problem.

**What did not run:** the organisation track, stages 4 and 5, and therefore the
sampled pairs item 4 asks for. `evidence.py`, `time_readers.py` and
`adopt_run.py` are written and tested and are waiting for a finished run.

## 106. Session of 2026-09-21 (second) — predicting route by route, and the bug that made it necessary

Everything in this section was measured under the **project interpreter**,
`/home/tomwright/PycharmProjects/dedupe_ui/backend/.venv/bin/python` (pandas
3.0.6, pyarrow 25.0.1, duckdb 1.5.5), against the code tree
`/home/tomwright/PycharmProjects/dedupe_ui_pscrun/backend` (branch `psc-run`).

### The hot-key control was doing nothing on four of the six person routes

Section 105 concluded that the person `predict` needed more than 35 GB of
DuckDB spill for 94.7 million comparisons, which is about 370 bytes a pair.
That reading was wrong, and the reason is worth writing down.

`train_track` built the controlled prediction SQL from `rows` — the projection
read straight off `units.parquet` — and then handed Splink `frame`, which is
`_splink_frame(rows, config)`. Those are not the same values.
`custom.NumericDifferenceAtThresholds` compares numbers and the cleaning engine
writes text, so `_splink_frame` casts `dob_year_clean` with `pd.to_numeric`.
With anything missing — and something is always missing — that gives float64.

The control writes its oversized block keys into the SQL as string literals,
built from `coalesce(cast(<column> as varchar), '')`. DuckDB spells the string
`1985` as `'1985'` and the float `1985.0` as `'1985.0'`. Measured on `rows` and
applied to `frame`, every `NOT IN` was true, so:

```
(l.surname_metaphone = r.surname_metaphone AND l.dob_year_clean = r.dob_year_clean
 AND l.dob_month_clean = r.dob_month_clean)
AND (<key> NOT IN ('RS§1985§3', ...)   <- never matches '…§1985.0§…'
     OR (l.postcode_district = r.postcode_district))
```

was the bare rule. **pb1, pb2, pb5 and pb6 all block on `dob_year_clean`, and
all four ran with no control at all.** So did person em2. The person track was
not predicting 94.7 million comparisons; on section 104's own uncontrolled
figures it was predicting about **1.46 billion**:

| route | priced, after the control | what actually ran |
|---|---|---|
| pb1 | 20,130,171 | 198,530,709 |
| pb2 | 16,755,794 | 114,611,766 |
| pb3 | 13,563,011 | 13,563,011 (text key, control worked) |
| pb4 | 16,907,827 | 16,907,827 (text key, control worked) |
| pb5 | 11,624,404 | 527,628,584 |
| pb6 | ≤15,762,398 | ≤582,535,879 |
| **total** | **≤94,743,605** | **≤1,453,769,756** |

That is what filled 32.5 GiB of spill in 375 seconds of prediction blocking,
and it means **370 bytes a pair was never measured** — the real width is about
24 bytes a pair, which is the number to size a spill cap from.

**The fix is one word.** `train_track` now measures the control on `frame`, the
frame Splink will apply the SQL to, which is what `controlled_rules`' own
docstring already said it had to be. `_budget_rows` casts the same columns the
same way, so the budget prices the rule against the values the rule will meet
and the two frames agree. `cast_numeric_columns` is now a named function with
the reason in its docstring, because it is a contract between two places and
not four lines inside one of them.

**Why nothing smaller than the full snapshot could have caught it.** The
500,000-record PSC sample has no block over `max_block_size: 20` on any person
route, so `controlled_sql` returns the rule untouched and the two spellings
never diverge. Checked directly: all six shipped routes give byte-identical SQL
from either frame on that sample, and the same pair counts. A column of digits
with nothing missing also casts to int64, whose text is the same as the
string's — so a tidy fixture passes too. `tests/test_stage_3_score.py::
test_a_numeric_block_key_is_controlled_on_the_frame_splink_scores` pins it with
a frame that has one missing value.

### Route-by-route prediction

Even with the control working, 94.7 million comparisons in one `predict` is a
comparison table the size of all six routes at once. The stage now predicts one
blocking rule at a time whenever the blocking budget has priced the track above
`PREDICT_ROUTE_BY_ROUTE_ABOVE` (default **5,000,000**), and keeps the single
pass below it, so donations and the PSC sample are untouched.

**It is the same answer, and the reason is that the rule objects are the
trained model's own.** Splink deduplicates across blocking rules while it
blocks: rule *n* carries `AND NOT (rule 0 OR ... OR rule n-1)`, and both that
clause and the `match_key` column come from one place,
`BlockingRule.preceding_rules` — `match_key` is simply its length. So handing
`predict` a list of one *rule object* rather than a list of one *rule* changes
nothing about what that rule does: its preceding rules are still the five in
front of it, its SQL is byte-for-byte the branch of the `UNION ALL` it would
have been, and its `match_key` is still its own index. Splink does this to
itself in `estimate_u.py`. **No after-the-fact deduplication is needed, and
none is done** — the exclusion is still Splink's.

What changes is how much is alive at once. Each route's prediction table is
copied straight to its own parquet and dropped before the next route starts, so
the spill is the largest single route rather than the sum. The per-route files
are then concatenated with one `read_parquet([...])`, which refuses a schema
mismatch — the check worth making, since a pair's columns must not depend on
which route made it.

**Proved identical on both real runs.** `prove_routes.py` trains each track's
model once and predicts it twice, comparing pair for pair in DuckDB:

| run | track | units | pairs | pairs only in one | duplicates | max Δprobability | max Δweight | gamma differences | `match_key` differences |
|---|---|---|---|---|---|---|---|---|---|
| donations `don_veto` | person | 13,310 | 25,503 | 0 / 0 | 0 | 0.0 | 0.0 | 0 | 0 |
| donations `don_veto` | organisation | 9,065 | 4,973 | 0 / 0 | 0 | 0.0 | 0.0 | 0 | 0 |
| PSC `psc_final` | person | 449,396 | 1,073,249 | 0 / 0 | 0 | 0.0 | 0.0 | 0 | 0 |
| PSC `psc_final` | organisation | 31,572 | 70,273 | 0 / 0 | 0 | 0.0 | 0.0 | 0 | 0 |

Route attribution matches route for route — PSC person, both ways:
`{0: 220,790, 1: 2,695, 2: 424,015, 3: 127,620, 4: 297,825, 5: 304}`. The
difference is 0.0 exactly, not 1e-9. Script:
`/home/tomwright/psc_scratch/full_venv/prove_routes.py <run_dir> <work_dir>`.

In the suite: `test_route_by_route_prediction_is_the_one_pass_prediction`
(same pairs, scores, gammas and `match_key` on a forty-record fixture with
three overlapping routes), `test_a_pair_is_made_by_one_route_only_when_the_
routes_overlap`, and `test_the_stage_takes_the_route_by_route_path_above_the_
limit`, which runs the whole stage both ways and diffs `pairs.parquet`.

`blocking_report.json` now carries, per track, a `prediction` block naming
which path ran, the pairs, and each route's `match_key`, rule id, pairs and
seconds.

### The oversized key sets are measured once

The budget measured each control to price the rule, and `train_track` measured
it again to build the SQL — about 50 seconds a rule twice over on 11.2 million
person units. `controlled_rules` now takes a `cache` dict, which
`run_stage_3_score` keeps for the length of the stage.

The key is the track, the rule, the control, the row count **and the dtype of
every column the control reads**. The dtypes are in the key because the
generated SQL inlines key values as text and the text depends on the type —
the bug above, turned into a guard. Casting `_budget_rows` the same way
`_splink_frame` casts means the two frames now agree and the cache hits;
before the cast they would have missed, correctly.

### Stage 2 was not batched, and here is why

`apply_match_keys` is a whole-frame NumPy algorithm: one `UnionFind` over every
record, a normalised copy of every column any key reads, and per-tier
"eligible before" masks that are global by definition — a tier-2 key has to
know who tier 1 already covered, across the whole snapshot. Batching it means
either a DuckDB rewrite of all 585 lines of `app/rules/keys.py` (token lists,
conditions, guards, blocklists, tiers) or a two-pass design with a global
union-find, and either way `keys_eval.evaluate` and `exact_overlay` sit on the
same frame. That is not a small change and it is not one to make in the same
session as the scoring fixes. **18.1 GB at fifteen million records stands, and
stage 2 still will not fit the server's 6 GB budget by a factor of three.** It
is the next thing to do to this pipeline.

### The estimate, written before the full run was started

| | |
|---|---|
| input | 15,029,263 records, 11,799,425 units (11,205,785 person, 593,640 organisation) — reused from `/home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full` |
| build units | 458 s, 11.9 GB (measured, section 104) |
| blocking budget | about 1,060 s (measured); the cache saves the *second* measurement, not this one |
| u by random sampling | 24 s (measured) |
| EM person em1 | about 1,990 s — unchanged, its keys are text and its control already worked |
| EM person em2 | about 1,500 s — it ran uncontrolled at 105.6M pairs in 2,665 s and will now run at 59.0M |
| person predict | six routes of 11.6M to 20.1M comparisons. Blocking is a hash build over 11.2M units per route plus the output, so 100 to 200 s a route: **900 to 1,800 s**, plus about 100 s to concatenate |
| organisation | priced at 1,832,607, under the 5,000,000 limit, so **one pass**; a few hundred seconds all in |
| overlays and pairs | about 42 million pairs at 500,000 a batch, each batch joined to the 11.8M-row overlay projection: **400 to 900 s** |
| **stage 3 total** | **8,000 to 11,000 s — 2.2 to 3.1 hours** |
| stage 3 peak RSS | **about 20 GB**, reached while Splink loads the person frame, exactly as in section 104. The frame is unchanged |
| spill | the largest single route is 20.1M comparisons. At the 24 bytes a pair the failed run actually measured that is under 1 GB, but the blocked-pairs table and the hash builds dominate: **expect 8 to 15 GB peak**, and keep `DUCKDB_MAX_TEMP=35GB` so a surprise is still bounded |
| stage 4 clusters | **600 to 1,200 s, 3 to 4 GB** |
| stage 5 entities | **900 to 1,800 s, 8 to 10 GB** |
| files | `predictions_person.parquet` about 2.1 GB (plus the six route parts alive at the same moment, another 2.1 GB), `pairs.parquet` 4 to 6 GB, `clusters.parquet` about 1 GB, `entities.parquet` about 2 GB |
| disk | 64.0 GB free at the start; **expect about 39 GB free at the worst moment**, against a 15 GB floor |

What would make this wrong, in the order I would bet on it: the overlay pass
over 42 million pairs (it has never run at more than a million), the concat
step (never run at more than 1.07 million), and stage 5's mint frame.

### A trained model survives a failed prediction

`train_track` saves the model the moment EM finishes, **before** it predicts,
with `splink_trained_<track>.json` beside it holding a fingerprint of
everything that shaped it: the comparisons, the prior and how it is estimated,
the EM settings and the seed, the unit count, and the blocking and training SQL
*after* their hot-key controls — the part that depends on the data, since a
control inlines the keys it found. A later attempt in the same run folder whose
fingerprint matches loads the model and goes straight to prediction. Set
`REUSE_TRAINED_MODEL=0` to train anyway.

This is insurance for exactly the failure that happened: a PSC run spends about
an hour and a half in EM and then hours in prediction, and it is prediction
that fails. Before this, every attempt paid for the training again.

## 107. The full run, finished — and what it says

Stages 3, 4 and 5 over the full PSC snapshot, in
`/home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full`, driven by
`run_stage_pscrun.py`, under
`/home/tomwright/PycharmProjects/dedupe_ui/backend/.venv/bin/python` (pandas
3.0.6, pyarrow 25.0.1, duckdb 1.5.5) against the code tree
`/home/tomwright/PycharmProjects/dedupe_ui_pscrun/backend` at `psc-run`.
`SPLINK_MEMORY_LIMIT=10GB`, `DUCKDB_MAX_TEMP=35GB`.

### Time, memory and disk, against the estimate

| stage | estimated | **measured** | estimated peak RSS | **measured** |
|---|---|---|---|---|
| 3 score | 8,000–11,000 s | **8,128.5 s** | about 20 GB | **27.1 GB** |
| 4 cluster | 600–1,200 s | **69.0 s** | 3–4 GB | **12.2 GB** |
| 5 entities | 900–1,800 s | **458.2 s** | 8–10 GB | **19.1 GB** |

Disk 63.2 GB free before stage 3, 57.8 GB after stage 5, **lowest 31.3 GB**
against a 15 GB floor. Spill was released after every phase; nothing was left
behind. The run folder is **12 GB**.

Inside stage 3:

| phase | estimated | **measured** |
|---|---|---|
| build units | 458 s | **492.0 s** — 15,029,263 records to 11,799,425 units |
| blocking budget | about 1,060 s | **930.7 s** |
| u by random sampling | 24 s | **23.1 s** |
| EM person em1 | about 1,990 s | **717.9 s** |
| EM person em2 | about 1,500 s | **884.0 s** |
| person predict, six routes | 900–1,800 s | **421.3 s** |
| organisation, all in | a few hundred s | **21.4 s** |
| overlays and the pairs file | 400–900 s | **2,790.3 s** |

**The estimate was right about the total and wrong about where the time goes.**
Prediction, the thing that had failed, is now the cheapest part of the person
track. EM came in at a third of the estimate. The overlays are three times the
estimate and are now the largest single phase of the pipeline — 46 minutes to
bucket, veto and finalise 41 million pairs at 500,000 a batch. Each batch
rebuilds a string index over all 11.8 million overlay units, twice (once in
`apply_overlays`, once in `vetoes.SideValues`); building it once outside the
loop is the obvious next thing to do.

**Peak RSS was 27.1 GB, not 20 GB, and it is not Splink.** It is reached in
`score_eval.evaluate`, gathering the accepted-pair edge arrays over 41 million
pairs. That is 93% of this machine and it is the new high-water mark for the
whole pipeline.

### The controls, and the route-by-route prediction

Every priced figure reproduces section 104 to the pair — person 94,743,605 of
150,000,000 and organisation 1,832,607 of 5,000,000. **The oversized key sets
were measured once**: all eight person controls came back "(already measured)"
in under a second where the budget had spent about 50 seconds on each.

| route | priced, after the control | before the control | pairs kept | seconds |
|---|---|---|---|---|
| pb1 | 20,130,171 | 198,530,709 | 11,760,742 | 92.1 |
| pb2 | 16,755,794 | 114,611,766 | 3,251,892 | 21.4 |
| pb3 | 13,563,011 | 351,486,369 | 9,448,408 | 53.6 |
| pb4 | 16,907,827 | 127,149,799 | 10,424,656 | 52.5 |
| pb5 | 11,624,404 | 527,628,584 | 5,448,066 | 89.6 |
| pb6 | ≤15,762,398 | ≤582,535,879 | 8,878 | 82.8 |
| **person** | **≤94,743,605** | **≤1,901,943,106** | **40,342,642** | **421.3** |
| organisation, one pass | 1,832,607 | 157,854,369 | 649,509 | 21.4 |

**Duplicate pairs across routes in `pairs.parquet`: 0.** Splink's own
`AND NOT` exclusion survived being asked one rule at a time, at full scale.
The organisation track took the one-pass path, as designed, because 1,832,607
is under the 5,000,000 limit.

`untrainedComparisons` is **0** on both tracks.

### The numbers a researcher would ask for

**Pairs by bucket.** 40,992,151 scored.

| track | score bucket | after the vetoes |
|---|---|---|
| person accept | 14,993,817 | **6,390,630** |
| person review | 13,737,743 | 4,516,638 |
| person reject | 11,611,082 | 29,435,374 |
| organisation accept | 561,162 | **446,894** |
| organisation review | 48,489 | 4,183 |
| organisation reject | 39,858 | 198,432 |

**`pairsVetoedFromAccept`, per veto.**

| track | veto | pairs hit | from accept |
|---|---|---|---|
| person | v1 | 17,924,433 | **7,648,900** (to reject) |
| person | v2 | 9,667,587 | **954,287** (to review) |
| organisation | ov1 | 193,410 | **113,702** |
| organisation | ov4 | 849 | **566** |

The vetoes are doing more than half the work on the person track: 27,592,020
pairs are `decided_by: veto` against 12,750,622 `decided_by: score`. **v1 alone
moves 7.6 million pairs out of accept** — more than the 6.4 million that
survive there.

**Clusters.** 8,460,758 clusters. By status: person 7,908,239 ok, 2,865
weak_link, **17 too_large**; organisation 549,599 ok, 38 weak_link. 57,635
person unit rows and 474 organisation ones are withheld. Person cluster sizes:
6,026,297 of 1; 1,230,804 of 2; 577,631 of 3–5; 73,131 of 6–20; 3,204 of
21–100; **54 over 100, holding 40,247 units**.

**Entities: 8,515,947 proposed** from 11,799,425 units and 15,029,263 records —
7,965,874 person and 550,073 organisation, all `new`, 0 id collisions, 469,749
attribute ties.

| records per entity | person entities | organisation entities |
|---|---|---|
| 1 | 5,354,336 | 384,570 |
| 2 | 1,403,856 | 76,340 |
| 3–5 | 949,868 | 59,305 |
| 6–20 | 247,878 | 26,509 |
| 21–100 | 9,898 | 3,343 |
| **over 100** | **38** | **6** |

### The learned weights say what the problem is

Person track, match weight in bits (`log2(m/u)`):

| comparison | level | weight |
|---|---|---|
| postcode_clean | exact full postcode | **+10.14** |
| postcode_clean | exact sector | **+9.51** |
| surname_clean | **exact match** | **+9.33** |
| postcode_clean | **exact district** | **+9.26** |
| surname_clean | Jaro-Winkler ≥ 0.92 | +7.85 |
| forename_canon | exact match | +6.97 |
| dob_year_clean | **equal** | **+2.56** |
| dob_month_clean | **exact** | **+1.00** |
| nationality_norm | exact | +0.34 |

**A shared postcode district is worth as much as a shared surname, and nine
times a shared birth month.** That is section 5's warning and deduping's D7,
now measured on fifteen million records rather than a sample. The organisation
track is healthy by comparison: an exact `name_core` is +11.59 and an exact
registration number +11.34, both far above anything a coincidence supplies.

### Would I let a researcher use the person track's accepted merges? No.

**Nineteen of the twenty largest proposed person entities are wrong**, and they
are wrong in the same way.

| records | units | distinct names | distinct postcodes | what it is |
|---|---|---|---|---|
| 217 | 195 | **165** | 171 | every "Mateusz <different surname>" |
| 214 | 180 | **143** | 157 | the same, another Mateusz block |
| 202 | 179 | 5 | 6 | *Joaquim Almeida — plausible* |
| 198 | 173 | **148** | 161 | Dariusz / Mariusz, different surnames |
| 190 | 169 | **129** | 157 | every "Catalin <different surname>" |
| 190 | 169 | **118** | 99 | "Jing Yao", "Xinguang Wang", "Bing Wang", "Bin Cao" |
| 184 | 153 | **101** | 124 | Mohammed/Muhammad/Hamza, 33 birth years |
| 173 | 124 | **51** | 112 | Gagandeep / Amandeep / Jasdeep Singh |

Only one of the twenty — Joaquim Almeida, 5 names, 6 postcodes, one birth year
— looks like one person. **The failure is systematic and it falls on non-British
naming conventions**: Polish, Romanian, Chinese, Pakistani and Sikh records,
where a very common forename plus a shared birth month and year is enough.

The sampled accepted pairs show it happening one pair at a time. Sampled across
the accept range, not the top of it:

- p=0.9726 — Mr Ciprian Tofan (PL1 2PP) and Mr Ciprian Cornel Turiac (ME16 0RE).
  **Different surnames.** Same forename, same birth month and year, both
  Romanian. Route pb5.
- p=0.9909 — Mr Hemanshu Udani and Mr Hemanshu Malaviya. **Different surnames.**
- p=0.9980 — Binbin Ma and Binbin Cai. **Different surnames.**
- p=0.9500 — Mrs Kirsten Cherie Walker (SG11, New Zealander, April 1974) and
  Kirsten Walker (RH20, British, February 1973). Different birth month,
  different year, different nationality, 90 miles apart.
- p=0.9636 — Mr Kenneth John Cowan (Suffolk, March 1971) and Mr Kenneth Gordon
  Cowan (Ayrshire, November 1972). Different middle name, different birth date,
  400 miles apart.
- p=0.9967 — Mr Jason James Wignall (Devon, October 1986) and Mr Jason David
  Wignall (Lancashire, June 1986).

The top of the accept range is fine — same full name, same birth date,
different postcode, which is a person who moved. **It is the bottom two-thirds
of accept that is not safe**, and it is not a handful of cases: 6.4 million
pairs are in accept and the 0.95 line is where those examples sit.

The organisation track is the opposite. Its accepted pairs agree on the
registration number and the postcode, and its largest entities are corporate
families (Legal & General, Aviva, Places for People, Punch Taverns) that are
over-merged at worst. One is plainly wrong and worth naming: **"Carlyle Bus &
Coach Limited" of West Bromwich is in the same entity as "The Carlyle Group,
Inc."** — a bus company and a US private equity firm, joined on the first name
token.

**The smallest fix.** Not the thresholds — moving the accept line up would lose
the good merges with the bad. Three changes, in the order I would make them:

1. **A veto that refuses a person pair whose surnames disagree unless something
   else identifies them.** pb5 exists to catch a surname change at marriage,
   and every bad example above comes through it. A veto in the shape of the
   existing v1/v2 — "surnames differ and neither postcode nor middle name
   agrees" — would take out the Ciprian, Hemanshu and Binbin cases without
   touching a genuine name change, which almost always keeps the address.
2. **Stop the postcode district out-weighing the surname.** `cl.PostcodeComparison`
   learns a district match at +9.26 because a district is small compared to the
   country, but the hot-key control now *blocks* on the district on five of the
   six person routes, so the pairs the model sees are not the pairs its `u` was
   sampled from. Either sample `u` under the same blocking, or collapse the
   district level into "area" so the model cannot spend nine bits on it.
3. **Cap a proposed person entity's distinct names.** An entity built from 165
   different names is not a person under any reading, and the `too_large`
   cluster guard did not catch it — it fires on 17 clusters and these are all
   under its limit. A guard on *distinct names per entity*, not size, would
   have withheld every one of the twenty.

Until at least the first of those is in, the person track's accepted merges are
research-grade only above about p=0.999, and the review queue — 4.5 million
pairs — is not a queue anybody can work through.

### Item 6: the readers, and getting the run onto a server

`time_readers_pscrun.py` against the finished folder. Two rounds: the server's
6 GB DuckDB budget, and 14 GB.

| call | at 6 GB | at 14 GB |
|---|---|---|
| records list, page 1 | 0.7 s | 1.0 s |
| records list, person, deep page | 24.7 s | 54.8 s |
| exact groups list, page 1 | 3.9 s | 5.1 s |
| pairs list by score | 146.7 s | 23.4 s |
| pairs list by priority | 144.1 s | 14.5 s |
| pairs list, review bucket | 29.4 s | 8.3 s |
| pairs list, deep page | **out of memory** | 16.7 s |
| clusters queue, page 1 | 94.6 s | 10.5 s |
| entities list, page 1 | **out of memory** | 22.1 s |
| entities list by size | **out of memory** | 16.4 s |
| one pair detail | 6.8 s | 5.7 s |
| one cluster detail | 10.2 s | 10.9 s |
| one entity detail | — | 4.5 s |

**Not one reader is under two seconds at full scale, and three fail outright
inside the server's budget.** One thing was fixed here and the rest is named
rather than rushed:

- **Fixed: `duckdb_conn.reader_connect`.** Every reader now opens with
  `preserve_insertion_order=false` and the run's own temp directory.
  `pairs_reader` and `exact_groups_reader` were opening a bare connection whose
  spill went to the *system* temp directory, outside `DUCKDB_MAX_TEMP` and
  outside anything a run owns. Releasing insertion order turned the clusters
  queue from an out-of-memory error into an answer at 6 GB. A reader ends every
  query in an explicit `ORDER BY ... LIMIT`, so it never depended on the order
  rows arrived in.
- **Not fixed, and this is the real problem: every list reader computes a
  whole-run aggregate on every request.** `pairs_reader.get_pairs` runs
  seventeen `count(*) FILTER` aggregates and a filtered total over a query that
  joins 41 million pairs to 11.8 million units *twice*, then sorts the result to
  take fifty rows — three passes over the join per page.
  `entities_reader.get_entities` and `clusters_reader.get_clusters` each build a
  temp table by joining two multi-gigabyte parquets and grouping to one row per
  entity or cluster, with `list()` aggregates that cannot spill. That is what
  runs out of memory at 6 GB.
  **The fix is a per-run index file** — the entity and cluster summaries written
  once at the end of stages 5 and 4, the way `scored_units.parquet` is already
  written — plus taking the pairs chip counts off `pairs.parquet` alone, which
  already carries `bucket`, `decided_by`, `track`, `vetoed_by` and
  `import_disagrees`. None of the three needs the unit join.
- **Also latent: `pairs_reader.model_explanation` reads `units.parquet` whole**
  — 1.8 GB — and `psc_features._company_sets` then builds a company-to-units map
  over all 11.8 million of them, for one pair. It returns early here because no
  stage-3b model is active, so it never fired on this run. It would need the
  same treatment as the corpus statistics before the model screen could be
  opened at this scale.

**Adopting the run.** `scripts/adopt_run.py` registered the folder and every
page then opened through the API with a `TestClient`, all 200s, against the
scratch data directory `/home/tomwright/psc_scratch/full_venv/fulldata` —
nothing under `backend/data` was touched. Re-running it with the same arguments
is a no-op, as intended.

**A defect it found.** `adopt_run` defaulted its database to `<data>/app.db`,
and its own usage example said the same. **`app.main` opens
`<DATA_DIR>/linkage.db`.** Following the script's example registered the run
into a file the web tool never reads, and the only symptom was an empty runs
list. The default is now `linkage.db` and a test asserts it equals
`Path(app.main.DB_PATH).name`.

### What the owner copies to the server, and the commands

The run folder is 12 GB. Everything in it is needed except `records_raw.parquet`
(stage 0's untouched input, 1.0 GB, which nothing reads after stage 1) — so
**11 GB to copy**:

| file | size | what reads it |
|---|---|---|
| `pairs.parquet` | 2,553 MB | the pairs list and detail, the histogram |
| `records.parquet` | 1,883 MB | the records list, the entity and group details |
| `units.parquet` | 1,801 MB | every screen that shows a unit |
| `entities.parquet` | 1,722 MB | the entities list and detail, the export |
| `clusters.parquet` | 1,128 MB | the clusters queue and detail |
| `unit_members.parquet` | 848 MB | the cluster and entity details |
| `events.parquet` | 546 MB | the evidence panel |
| `scored_units.parquet` | 367 MB | which units a recluster has already seen |
| `exact_groups.parquet` | 247 MB | the exact-groups screen |
| `splink_model_person.json` | 7.0 MB | the gamma labels on the pair detail |
| `splink_model_organisation.json`, `splink_trained_*.json` | 0.1 MB | the same, and the model-reuse fingerprint |
| `blocking_report.json`, `score_eval.json`, `exact_eval.json`, `entity_report.json`, `contradictions.json` | < 1 MB | the run summary screens |
| `config/` | 0.1 MB | the run's ruleset and linkage settings snapshot |
| `diagnostics/` | 0.1 MB | Splink's charts |

```bash
# 1. copy the folder, leaving the raw input behind
rsync -av --progress --exclude records_raw.parquet --exclude duckdb_tmp \
  /home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full/ \
  user@server:/srv/dedupe/data/runs/psc_full/

# 2. register it (the default database is now the right one)
ssh user@server
cd /srv/dedupe/backend
.venv/bin/python scripts/adopt_run.py /srv/dedupe/data/runs/psc_full \
  --label "PSC full snapshot 2026-09-18" \
  --input-filename persons-with-significant-control-snapshot-2026-09-18.zip \
  --counts /srv/dedupe/data/runs/psc_full/adopt_counts.json
```

`adopt_counts.json` is the union of the five stages' counts;
`/home/tomwright/psc_scratch/full_venv/fulldata/adopt_counts.json` is the one
this run produced and can be copied with the folder.

**Do not put this in front of a researcher yet.** The server has a 6 GB DuckDB
budget; at that budget the entities list and two of the pairs views run out of
memory on this run, and nothing else is under twenty seconds. The reader index
files come first.

## 108. The quality fix — vetoes, the name gate, and the readers

Everything here is the **project interpreter**,
`/home/tomwright/PycharmProjects/dedupe_ui/backend/.venv/bin/python` (pandas
3.0.6, pyarrow 25.0.1, duckdb 1.5.5), against the code tree
`/home/tomwright/PycharmProjects/dedupe_ui/backend` on `main`.

Section 107 ended with "Would I let a researcher use the person track's accepted
merges? No." This section is the answer to that, measured on the same run. No
rescoring was needed: the vetoes and the gate are re-applied over the finished
run's own `pairs.parquet`, and stages 4 and 5 are run again on top. The baseline
stays where it was, in
`/home/tomwright/psc_scratch/full_venv/fulldata/runs/psc_full`; the re-applied
run is `/home/tomwright/psc_scratch/tw_quality/fulldata/runs/psc_veto`.

### 1. Four new veto rules on the person track, and one new operator

`not_equal_or_missing` is the one pair operator a null makes TRUE. `differs`
asks "do these two disagree?", which a missing value cannot answer. The new one
asks "does this column agree?", and a column nobody filed does not agree. It is
what a veto rule needs to say "and nothing else identifies them". Because it is
true for most pairs on its own, it is only ever written beside a condition that
is not. **Key `not_equal_or_missing`, label "Not the same, or missing".**

| id | in words | action |
|---|---|---|
| v3 | Two clearly different surnames (Jaro-Winkler under 0.85 on the cleaned surname), and neither the full postcode nor the middle names agree | reject |
| v4 | Two clearly different surnames, whatever else agrees | review |
| v5 | Two clearly different middle names, two different birth months, and no shared full postcode | reject |
| v6 | A different birth year AND a different birth month — two dates, not one date filed twice | reject |

**0.85 was read off the run.** Among accepted pairs whose surnames differ, the
share that also agree on a middle name is 29.1% at 0.95 and over, 20.1% from
0.90 to 0.95 and 18.6% from 0.85 to 0.90, then falls to 11.3% from 0.80 to 0.85
and 4.6% from 0.70 to 0.80 — against 42.8% for an accepted pair whose surnames
match. Below 0.85 the surname difference stops behaving like a spelling variant.

v4 is the rule that keeps a marriage name-change reachable. It caps every
clearly-different-surname pair at review, so the ones v3 spares — the ones the
postcode or the middle name corroborates — go to a reviewer instead of being
merged in silence.

### 2. Pairs by bucket, before and after

| track | bucket | before | after |
|---|---|---|---|
| person | accept | 6,390,630 | **5,317,154** |
| person | review | 4,516,638 | **2,605,985** |
| person | reject | 29,435,374 | **32,419,503** |
| organisation | accept | 446,894 | 446,894 |
| organisation | review | 4,183 | 4,183 |
| organisation | reject | 198,432 | 198,432 |

`decided_by`: score 13,205,872 → 7,073,810, veto 27,786,279 → 33,918,341.
The organisation track is untouched to the pair, as intended — nothing was added
to it.

**Per rule.** `vetoed_by` names the first rule carrying the strongest action, so
these are pairs each rule is *credited* with, not pairs it hits; a pair v2 and
v3 both hit reads `v3`. `pairsVetoedFromAccept` is the pairs the score alone
would have accepted.

| track | veto | pairs credited | pairsVetoedFromAccept | to reject | to review |
|---|---|---|---|---|---|
| person | v1 | 17,924,433 | 7,648,900 | 17,924,433 | 0 |
| person | v2 | 8,964,602 | 618,302 | 7,225,392 | 1,739,210 |
| person | **v3** | 5,528,993 | **639,342** | 5,528,993 | 0 |
| person | **v4** | 136,194 | **70,136** | 24,267 | 111,927 |
| person | **v5** | 427,299 | **246,107** | 427,299 | 0 |
| person | **v6** | 742,561 | **453,876** | 742,561 | 0 |
| organisation | ov1 | 193,410 | 113,702 | 193,410 | 0 |
| organisation | ov4 | 849 | 566 | 849 | 0 |

### 3. Looking for harm, and not finding much

1,073,476 person pairs left accept. Of those:

- **0 had a matching surname AND a matching full date of birth.** None of the
  four rules can fire on such a pair, and the measurement agrees.
- 362,067 had a matching surname — those are v5 and v6, where the birth date
  disagrees.
- 57,710 shared a full postcode.
- 43,593 had the marriage shape: same forename, same full date of birth, same
  full postcode, different surname.

**Where the marriage shape lands now:** 47,181 in review (all v4), 26,217 still
in accept because the surnames are near variants (0.85 or better), and **none in
reject**. That is the rule working as written.

### 4. A cluster gate on different names: `mixed_names`

The match keys have `max_distinct` and the cluster gate had nothing like it. The
new limit is `max_distinct_values` in `linkage_settings.json`,
`{track: [{"column": ..., "count": ...}, ...]}`, and a cluster trips it when ANY
column the track names shows more distinct values than that column's own limit.
A track with no entry is not gated this way; donations names none.

PSC person names three, and each was added because the previous one was not
enough:

| column | count | why |
|---|---|---|
| `surname_clean` | 3 | A birth name, a married name, one inconsistently filed hyphenated variant. On the baseline run, a person cluster of two units averages 1.11 distinct surnames, one of 6 to 20 units 2.33, one of over 100 units 104 |
| `forename_canon` | 3 | With the surname gated, the twelve largest remaining wrong person entities were one surname and many forenames — 206 records under 55 names, every one of them Singh, and the same for Khan, Mia and Ali. A chain that cannot run away on the surname runs away on the forename |
| `dob_year_clean` | 2 | With both names gated, the largest wrong one left was 111 records under 57 names, all "Mohammed &lt;something&gt; Ali", over 29 birth years. A person has one birth year and v1 leaves a year of slack, so two adjacent years is honest and three is a chain. 1,713,278 person clusters show one birth year and 36,156 show two, against 2,037 over two — a clean break |

**Key `mixed_names`, label "Mixed names", definition "This cluster holds more
different values of a name or a birth year than one person could have, so it is
really several people."** It is withheld exactly like the other statuses —
rebuilt from the trusted edges alone — and it sits just under `too_large` in the
status order, because it asks the same question of a cluster small enough to
pass the size cap.

### 5. Clusters and entities, before and after

| | before | after |
|---|---|---|
| clusters | 8,460,758 | 9,041,366 |
| `ok` | 8,457,838 | 9,036,652 |
| `too_large` | 17 | **1** |
| `mixed_names` | — | **3,765** |
| `weak_link` | 2,903 | 948 |
| clusters withheld | 2,920 | 4,714 |
| held groups waiting | 3,842 | 3,842 |
| review queue | 6,762 | 8,556 |
| **entities proposed** | 8,515,947 | **9,074,905** |
| attribute ties | 469,749 | 423,456 |
| id collisions | 0 | 0 |

Stage 4 ran in 193.0 s at 11.3 GB; stage 5 in 552.2 s at 18.3 GB.

Records per entity:

| records | person before | person after | organisation before | organisation after |
|---|---|---|---|---|
| 1 | 5,354,336 | 5,968,713 | 384,570 | 384,570 |
| 2 | 1,403,856 | 1,446,628 | 76,340 | 76,340 |
| 3–5 | 949,868 | 899,339 | 59,305 | 59,305 |
| 6–20 | 247,878 | 203,082 | 26,509 | 26,509 |
| 21–100 | 9,898 | 7,070 | 3,343 | 3,343 |
| **over 100** | **38** | **0** | 6 | 6 |

### 6. The twenty largest person entities, again

Nineteen of the twenty now look like one person. The one that does not is
**"Mohammed Imran" / "Muhammad Imran"** — 75 records, 5 names, one surname, two
birth years, and **56 different postcodes**. A single common name with two birth
years passes every gate there is.

The rest are what a researcher would hope to see: Jaime Coronado Llanos (82
records, 5 names, 9 postcodes, one birth year), Daniel O'Connell (72, 3 names, 2
postcodes), Bertrand Perrodo, Rivka Dreyfuss, Mathew Causon, Cay Arff, Francois
Perrodo, Michail Logothetis, Michael Grimes (one name, two postcodes), Aslam
Dahya, Tyler Golding-Reddock, Jonathan Round, Diane Wilson, Ethan Sandifer,
Rosalind Mason, Matthew Harrison, Andrew De-Long, Robert Keane, Irvine Jay.
Every one is a spelling or a title varying around a single filed identity.

**Forty accepted pairs sampled across the score range** (not the top of it) are
now all same-surname, and nearly all same birth month and year with a different
address — a person who moved. Two are worth naming as the residual:

- Mr Daniel Edmunds (RM3 0WL, May 1989) and Mr Daniel Phillip Edmunds (NP23 4GB,
  October 1989), p = 0.978. Same surname, same year, different month, different
  end of the country, and only one side filed a middle name, so v5 cannot see it
  and v6 needs the year to differ too.
- Dr Mohandeep Singh Arora (PE7 8PB) and Mr Mandeep Singh Arora (TW5 0UQ), both
  September 1986, p = 0.9986. The forenames are 0.9 alike, so v2 does not fire.

**Twenty review pairs** are siblings and spouses — Yohannes and Jeleesa Solomon,
Deborah Jane and Joanne Lockhart, Heidi and Paul Meader, Mustafa and Zohair
Poonawala — plus the surname-change candidates v4 sends there, such as Jaspal
Singh Chumber against Jaspal Singh Sandhu. That is a queue a person can work.

### Would I let a researcher use the person track's accepted merges now? Yes, with two things said plainly.

The systematic failure is gone. It was a rule about surnames and a gate about
names, not a threshold move, and it cost no pair that agrees on the surname and
the full date of birth.

**What remains, in the order I would take it:**

1. **A common forename and surname with two birth years still merges.** The
   "Mohammed Imran" entity is 75 records over 56 postcodes. The gate allows two
   birth years because v1 allows one year of slack; tightening either would
   start costing genuine off-by-one filings. The honest next step is a rule
   about *how many addresses* one proposed person may hold — the same shape as
   `max_distinct_values`, on `postcode_district`, and it wants measuring before
   it is set.
2. **Same surname, same birth year, different birth month, different address.**
   v6 needs both to differ and v5 needs both middle names filed. A third rule in
   that family would catch the Daniel Edmunds case; it was not written because
   nothing in the sampled forty suggests it is common, and a month is only 1.00
   bit of evidence.
3. **The review queue is 2.6 million pairs.** Down from 4.5 million, and now
   made of pairs a person can actually decide, but still not a queue anyone
   works through end to end. It wants sorting by something better than the
   score.

### 7. Why a postcode district was worth as much as a surname — and it is not the `u`

Section 107 guessed that the hot-key control blocking on the district had spoiled
the `u`. **It had not.** `estimate_u_using_random_sampling` is called on the
whole 11,205,785-unit person frame (`stage_3_score.py`, `U_SAMPLE_PAIRS = 5e6`,
seed 42), before any blocking, and the control only ever rewrites blocking SQL.
Measured directly on the run's own `units.parquet`, the true random-pair rate for
a district match is **0.000543** against the **0.000575** the model learned — 5.9%
out, and in the direction that *understates* the weight.

A UK postcode district really is about as rare as a UK surname: 4,935 districts
behave like 1,342 effective values, and 996,588 surnames behave like 2,211,
because SMITH, KHAN, JONES, SINGH, PATEL and AHMED alone cover about 1.4 million
person units.

**The defect is the term-frequency asymmetry.** `pc1` (surname) had
`term_frequency: true` and `pc6` (postcode) had `false`, so agreeing on SMITH is
worth 5.53 bits after the adjustment while agreeing on E14 — as common as JONES —
is worth a flat 9.26. `term_frequency` is now `true` on `pc6`.

**It is a partial fix and the run has NOT been rescored with it.** Splink
attaches a frequency table only to an exact-match level, and
`cl.PostcodeComparison` builds its sector, district and area levels by regular
expression, so the flat district weight stands. Proved on the 500,000-record
sample, scored both ways with today's defaults: the learned m and u are identical
to the digit on every level, the only difference in the model file is
`tf_adjustment_column: postcode_clean` on the full-postcode level, and over
401,746 person pairs the largest weight move is 5.31 bits, the mean 0.24, and
**42 pairs change bucket**. Accept goes 19,134 → 19,131.

Individualising the district needs a custom comparison built the way
`custom.NumericDifferenceAtThresholds` was, with `tf_adjustment_column` set to a
`postcode_district` column of the frame. That is a code change plus a full
rescore, and it is the next thing to do to the person model.

### 8. The overlays: 2,790 s to 1,072 s

Each of the 82 batches used to rebuild a string index over all 11.8 million
overlay units three times — once in `apply_overlays`, once in `_priority_totals`
and once in `vetoes.SideValues`. The index does not depend on the batch.
`vetoes.UnitLookup` now builds it once per run and every path that lays the
overlays down takes it: `overlay_predictions`, `rewrite_pairs`,
`_score_pairs_file` and the forced-pairs append.

**Measured on the full run: 2,790.3 s → 1,071.6 s**, and the new time is for
strictly more work — six person veto rules instead of two, two of them
Jaro-Winkler over 40 million pairs. Reading the overlay projection off
`units.parquet` is 1.5 s of that. Peak RSS 9.2 GB.

Identity is pinned by tests rather than argued: a pairs file written with a
prebuilt index and one written without hash to the same bytes, on both the
overlay path and the re-bucket path, and a test counts the index builds and
asserts there is one.

### 9. The readers, at full scale, in the server's 6 GB

Every list reader used to compute a whole-run aggregate on every request.
`get_pairs` joined 41 million pairs to 11.8 million units twice and did it three
times a page; the clusters and entities queues built temp tables with `list()`
aggregates DuckDB cannot spill. Four stages now write the answer once:

| file | written by | size on the full run | build time |
|---|---|---|---|
| `pairs_index.parquet` | stage 3, and every path that rewrites `pairs.parquet` | 2,464 MB | 33.2 s |
| `clusters_index.parquet` | stage 4 | 442 MB | 11.9 s |
| `entities_index.parquet` | stage 5 | 499 MB | 14.4 s |
| `exact_groups_index.parquet` | stage 2 | 91 MB | 15.8 s |

Plus `pairs_counts.json`, a few hundred bytes: the seventeen chips above the
pairs list, which describe the whole run and ignore the filters. It records what
it was computed from — the pairs file's size and time, the index's time, and a
fingerprint of the active labels — so a stale one is ignored and rewritten. No
other code has to remember to refresh it.

**A group-by that returns a `list()` column cannot spill.** The entity index
failed at a 9.3 GB memory limit on the first attempt, which is the same shape as
section 104's 61-way join: the lists are built inside the hash table and the
buffer manager cannot evict them. `app/services/index_chunks.py` splits the
groups by a hash of their key, writes each piece, and concatenates them with a
plain scan. Eight pieces; every index now builds inside the server's 6 GB.

| call | section 107, 6 GB | now, 6 GB |
|---|---|---|
| records list, page 1 | 0.7 s | 1.1 s |
| records list, person, deep page | 24.7 s | **1.3 s** |
| exact groups list, page 1 | 3.9 s | **0.1 s** |
| pairs list by score | 146.7 s | **1.2 s** |
| pairs list by priority | 144.1 s | **1.1 s** |
| pairs list, review bucket | 29.4 s | **1.2 s** |
| pairs list, deep page (offset 200,000) | out of memory | **2.4 s** |
| pairs list, sort=useful | — | **1.2 s** |
| pairs histogram | — | **0.1 s** |
| clusters queue, page 1 | 94.6 s | **1.5 s** |
| entities list, page 1 | out of memory | **0.5 s** |
| entities list by size | out of memory | **0.5 s** |
| one pair detail | 6.8 s | **0.8 s** |
| one cluster detail | 10.2 s | **0.9 s** |
| one entity detail | — | **0.9 s** |

**Everything is under two seconds except a pairs page 200,000 rows deep**, which
is 2.4 s. That residual is DuckDB's top-N over 41 million rows with a 200,000
offset and nothing else: offset 0 is 1.34 s, offset 1,000 is 1.25 s, offset
20,000 is 1.30 s. It is not worth an index of its own.

Three more things in the same work:

- **`model_explanation` read `units.parquet` whole** — 1.8 GB — to explain one
  pair. It now reads the two units by key, with `pd.read_parquet(..., filters=)`
  rather than DuckDB, so the dtypes are the ones the feature builders expect.
- **The records page sorted fifteen million rows of forty columns** to show
  fifty. It now sorts two columns, takes fifty record ids and fetches those rows.
- **`get_pairs` returned a SQL string where `total` belonged** whenever
  `sort=useful` and the run had priority columns: a local named `total` shadowed
  the row count. Fixed, and the priority sum is now called `priority_sum`.

An index is used only when it is at least as new as the file it was made from and
carries this profile's own columns. A stale one is ignored, not served, so a
missed call site costs a slow page and never a wrong answer. Every reader keeps
its old query as the fallback, so a run from an older pipeline — or one adopted
from a folder — still opens.

**The run folder is now 15 GB rather than 12**, and the indexes are 3.5 GB of
that. `records_raw.parquet` (1.0 GB) still need not be copied.

### 10. Donations is untouched

The donations pipeline was run end to end twice: once against this tree and once
against `git archive HEAD backend`, the code as it was before this work.
`records`, `exact_groups`, `units`, `unit_members`, `clusters`, `entities` and
`scored_units` are **byte-identical**, and so are `score_eval.json`,
`exact_eval.json`, `entity_report.json` and `blocking_report.json`. Only the four
index files are new.

`pairs.parquet` differs in the last two digits of `match_probability` — and it
differs the same way between two runs of the *unchanged* tree, so it is DuckDB's
parallel summation and not this work. The largest difference is 6.3e-14 in the
probability and 8.4e-13 in the weight, and **no pair changes bucket**.

### 11. What is still open

- **Stage 2 is still 18.1 GB at fifteen million records** and still three times
  the server's budget. Nothing here changed it. The plan stands from section 106:
  `apply_match_keys` is a whole-frame NumPy algorithm with a global union-find
  and per-tier "eligible before" masks, so batching it means either rewriting all
  585 lines of `app/rules/keys.py` in DuckDB or a two-pass design with a global
  union-find, and `keys_eval.evaluate` and `exact_overlay` sit on the same frame.
  **The paragraph of plan asked for:** do it in two passes over `records.parquet`
  and never hold the whole projection. Pass one reads, per tier and per key, only
  the columns that key's conditions and guards name, and writes `(record_id,
  key_id, key_value)` triples to a parquet — one narrow file, streamed, with the
  token lists and blocklists applied as it goes. Pass two groups those triples in
  DuckDB to get each key's groups, feeds the edges to one union-find over integer
  record codes exactly as stage 4 already does for units, and evaluates the
  guards as group-level aggregates rather than per-row masks. The "eligible
  before" mask becomes a join against the tiers already resolved, which is a
  semi-join on a code column and not a boolean array over fifteen million rows.
  `exact_overlay` then reads the group table rather than the frame. Expect stage 2
  to drop from 18 GB to about 3, and expect the work to be a week rather than an
  afternoon, because every match key's semantics have to be reproduced exactly and
  `exact_eval.json` is the thing that proves it.
- **The person model has not been rescored** with `term_frequency: true` on the
  postcode, and the district level cannot be individualised without a custom
  comparison.
- **`price_rule` and the blocking budget** are unchanged, and so is everything in
  sections 104 to 107 about how the run was produced.

## 109. Two more person vetoes: v7 on the birth month, v8 on the review queue

Section 108 left the person track with six vetoes and a review queue of
2,605,985 pairs. The evidence report of 2026-09-22
(`psc_scratch/agent_reports/psc_person_evidence_2026-09-22.md`) said where the
next two rules were. This section is what they do, measured on the full run.

### 1. What was added

**v7, person, review.** The birth month differs and the two full postcodes
differ. Both postcodes have to be filed; `differs` is false when either side is
null. v6 already rejects a month that differs when the years differ too, and
reject beats review, so v7 lands on pairs that share a birth year — or on pairs
where one side filed no year at all, because then v6 cannot fire.

**v8, person, reject.** The forename is clearly different, on the same test v2
uses (`forename_canon`, Jaro-Winkler below 0.7), the two full postcodes differ,
and the two sides share no given name at all.

The last condition needed a column that did not exist. `no_overlap` compares two
`" | "`-joined sets, and nothing in the cleaning engine wrote one. The change is
one new function in the library, `token_set`: distinct tokens, sorted, joined by
`" | "`. It is `sorted_tokens` with a different separator. Two cleaning steps use
it, and they copy the shape of p16 and p17, which already build
`name_fingerprint` the same way:

- p28 concatenates `forename_clean` and `middle_clean` into `given_names`, with
  `require_all: false` so a person who filed no middle name still gets a value;
- p29 runs `token_set` over that into `given_tokens`.

A new library function was the smallest change that works. It needs no new op,
no entry in `vocabulary`, and no new branch in the validator, so
`GET /api/config/functions`, the cleaning editor and the veto editor all pick it
up on their own. `POST /api/config/preview-vetoes` reads the column with no
change at all; there is a test that proves it.

### 2. How it was measured

Nothing was rescored. The vetoes were re-applied to the 40,992,151 pairs the
psc_veto run had already scored, using `stage_3_score.rebucket`, which is the
path a threshold move takes: it streams `pairs.parquet`, strips the overlays,
applies the buckets and the vetoes again from the run's own snapshotted ruleset,
and swaps the file in.

v8 needs `given_tokens` on the units, and `rebucket` does not run stage 1. But
p28 and p29 read only `forename_clean` and `middle_clean`, and both are already
materialised in `units.parquet`. So the two steps were run over the existing
units file through the engine's own `_run_step` — the same code on the same
inputs, which is what a full re-clean would have produced. That took 66 seconds
for 11,799,425 units instead of re-cleaning fifteen million records.

One thing to know if that route is used again. Read back from parquet a missing
value arrives as NaN, but stage 1 hands these columns to the step as None, and
`_run_concat` treats only None as missing. Without normalising first, the
literal string "nan" is concatenated into the key. In the live pipeline p27 and
p16 never hit this, because their sources are engine-written and because p27
requires every source, but a `concat` step with `require_all: false` over a raw
column could.

### 3. The numbers

Person track, before and after:

| bucket | before | after | change |
|---|---|---|---|
| accept | 5,317,154 | 5,129,300 | −187,854 |
| review | 2,605,985 | 1,696,849 | −909,136 |
| reject | 32,419,503 | 33,516,493 | +1,096,990 |

The whole run moved on exactly two transitions, and nothing else moved at all:

| from | to | pairs | rule |
|---|---|---|---|
| review | reject | 1,096,990 | v8 |
| accept | review | 187,854 | v7 |

That the table has only two rows is also the check that the baseline is
comparable. If the code had drifted since psc_veto was built, some pair would
have moved for a reason that is not v7 or v8. None did.

What each rule holds after the run:

- **v7** — 199,617 in review and 2,026 in reject. Of the review ones, 187,854
  came out of accept and 11,763 were in review already. It moves nothing into
  reject; the 2,026 were rejected already for another reason.
- **v8** — 7,252,033, all in reject. 1,096,990 of those came out of review. The
  other 6,155,043 were rejected already, on the score or by another veto. **It
  takes no accepted pair**, which is what makes it free: v2 already caps a
  clearly different forename at review.

The review queue falls from 2,605,985 to 1,696,849, down 34.9%.

Both rules hit less hard than the evidence report predicted — 187,854 against
207,630 for v7, and 1,096,990 against 1,487,802 for v8. The reason is the same
in both cases and it is deliberate. The report sized the rules with `NOT
pc_same`, which is true when a postcode is missing. These rules use `differs`,
which needs both sides filed. A pair where nobody filed a postcode is not a pair
whose addresses disagree, and pattern P2 of the report — district missing —
is 420,805 pairs, so the gap is about the size expected.

### 4. Twenty pairs each, judged

**v7, twenty of the 187,854 it moves out of accept.** These are the rule working
as intended and they are a mixed bag, which is exactly why the action is review.
Two are plainly one person: Liene Krastina, BD1 5DL against BD7 1RA, both in
Bradford, born 1990, months 9 and 7; and Parmjit Kaur Dhillon, UB4 0HR against
UB3 1AP, both in Hayes, identical given names, scoring 1.0000. Both now go to a
reviewer rather than being merged unseen. One is plainly two people: Joanne
Connor against Jean Connor, WA8 8QU and WA8 8PU, which v2 never caught because
JOANNE and JEAN score above its threshold. The other seventeen — Daniel Drew of
BL1 against LS1, Alison Ramsden of CM9 against M46, Aftab Iqbal of KT6 against
BD7 — are pairs nobody can settle from the filing. A human has to read them.
That is the whole argument for review over reject: the rule defers a merge, it
never destroys one.

**v8, twenty of the 1,096,990 it moves out of review.** Nineteen are clearly two
different people. Nigel James against Sally Elizabeth Mackinnon. Rebecca Mary
against Tracey May Ireland. Steven Andrew against Jonathan Francis Aspinall.
Leanne Marie against Phillip Ronald Hatch. Zeeshan against Usama Khalid, two
brothers in B33. The twentieth, Panna Nitin Kotecha of YO10 4HW against Kalpana
Kotecha of LE4 4DF, is one a reader could argue about: both born January 1959,
and Panna is not obviously a short form of Kalpana. One doubtful pair in twenty,
on a rule that costs no accepted pair, is worth it.

Both rules ship. Neither was added to `_vetoes_measured_and_not_shipped`.

### 5. What this section does not say

**The entity counts are not measured.** Stages 4 and 5 never ran. The rebuild
was stopped by the machine part way through stage 3, after `pairs.parquet` had
been rewritten and swapped in but before the pair index and the evaluation were
written. The pairs numbers above are all complete and correct, because they come
from that finished file. The proposed person entity count before and after is
still open.

What can be said is the input to it. Stage 4 clusters the accepted edges, and
the accepted person edges fall from 5,317,154 to 5,129,300. The evidence report
modelled a cut of that size as roughly +118,000 proposed person entities, but
that is its arithmetic and not a measurement of this run.

The run folder `psc_scratch/agent_c/runs/psc_v7v8` holds the rewritten pairs and
the rebuilt units, so stages 4 and 5 can be finished from it without redoing the
25 minutes the pairs rewrite took.

### 6. Timings, and what else was checked

The units rebuild took 66 seconds for 11,799,425 units. The pairs rewrite took
about 25 minutes for 40,992,151 pairs, single-threaded through one writer. The
total for a full rebuild is not known, because the job did not finish.

**Donations does not move.** The donations pipeline was run headless through
stages 1 to 5 twice, once on the unpatched code and once on the patched code,
from the same `records_raw.parquet`. Every bucket count, both entity counts, the
stage counts, `entity_report.json`, `score_eval.json` and a full hash digest of
the cleaned `units.parquet` are identical. Donations never names `token_set`,
and adding a function to the registry changes nothing that existing rules read.
For the record: person accept 23,358, review 1,059, reject 1,086; organisation
accept 3,599, review 176, reject 1,198; 10,990 person and 6,578 organisation
entities.

**The suite.** 1,478 passed against 1,469 before the change, which is the nine
tests added here. Both runs fail `test_the_real_checkout_has_a_commit`, and it
failed before the change as well; it reads the git metadata of the checkout.

**The frontend.** `npm ci`, `npm run build` and `node scripts/check-terms.mjs`
all pass. No frontend source needed changing: the veto editor offers operators
out of `vocabulary.VETO_OP`, which is untouched, and it offers `given_tokens` as
a column because it is a cleaning target like any other.

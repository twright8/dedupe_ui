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

## 99. Where the session of 2026-09-18/19 stopped (written by the coordinating session)

State of the code: `main` = `378e4c2` plus one docs commit, all deployed to the server and green. Everything after that is on the branch `wip/psc-scale` (this commit): stage 3 streaming, stages 4 and 5 out of core, the corpus persistence, the hot-key block control, the loader's chunk files, the rebuilt PSC token lists, and a partly finished DuckDB rewrite of `build_units` in `app/pipeline/dedupe/units.py` (an agent was mid-edit when the session ended: review that file first, and `git diff main -- backend/app/pipeline/dedupe/units.py`).

Do NOT merge this branch into `main` or deploy it until the full suite is green under the PROJECT interpreter:
`cd backend && SITE_PASSWORD=testpass123 .venv/bin/python -m pytest tests -q -p no:cacheprovider`
Last result under that interpreter: 1,355 passed, 4 failed. All four are NaN-versus-None comparisons in tests written under the wrong interpreter: `test_stage_3_score.py::test_a_batched_overlay_writes_what_one_pass_writes`, `::test_a_writer_keeps_one_schema_when_the_first_batch_is_all_null`, `::test_rewriting_the_pairs_in_batches_is_the_in_memory_answer`, `test_stage_4_cluster.py::test_stage_4_writes_the_stored_clusters_fixture`. Fix by comparing with missing values normalised, without weakening what they prove (two of them prove that batched and one-pass writes give the same file).

THE INTERPRETER TRAP. The laptop's default `python` is miniconda (pandas 2.3.1, pyarrow 22, duckdb 1.4.4). The project and the server use `backend/.venv` (pandas 3.0.6, pyarrow 25.0.1, duckdb 1.5.5). The agent that wrote sections 15 to 26 ran its tests, measurements and the full-snapshot jobs under miniconda. Treat its timings and memory figures as approximate, re-measure the ones that matter, and run the real full-scale run with `backend/.venv/bin/python` only. Its report of a `build_units` crash on donations was a pandas 2 artefact.

Open, in order: (1) finish and verify `build_units` out of core (identity on the donations run and the PSC sample; a 32x tiling); (2) the four tests above; (3) stage 5: stop handing `unit_id`/`cluster_id` to `mint_entity_ids`, and vectorise `psc.mint_entity_ids` (it loops over every entity); (4) a refine column for the hot-key control that is independent of the blocking key (the forename initial is not); (5) the owner-approved full-scale run, stages 3 to 5, estimate first (section 24), vetoes on, `max_pairs` 150,000,000, disk floor 15 GB; (6) stage 2's 13 GB peak; (7) postcode still outweighs surname on the PSC person track.

Full-snapshot outputs of stages 0 to 2 (made under miniconda, fine as a scale proof): `/home/tomwright/psc_scratch/pscfull/runs/psc_full`. 11,799,425 entities after the exact keys with the rebuilt token lists.

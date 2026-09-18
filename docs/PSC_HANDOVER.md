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

*(Updated. Closed: the stage-3 defect (6), the run lock (8), the sample re-run
(10), bounded spill (11), stage-2 projection (12). Section 7's disk blocker is
cleared. **Still open: B4, B5, B6, C, and — new and more important than any of
them — the date-of-birth weight in section 10.**)*

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

1. **The date-of-birth weight (section 10).** Bigger than anything below. The
   person track currently accepts people born 37 years apart at p = 1.0 and
   every automated guard says the run is fine.
2. **B4** `build_units` at 16M. Not started. It is the heaviest step of stage 3
   (about 75 s of the sample's 128 s) and it is where the per-column modal vote
   lives.
3. **B5** the remaining full-frame reads and the persisted TF-IDF. Not started —
   see the note below, the PSC and donations builders disagree about what the
   corpus even is, so this is a design decision before it is a refactor.
4. **B6 + C** the full snapshot, the token lists, full-scale pricing and
   `max_block_size`. Not started.

**A B5 finding worth having before anyone starts it.** The two feature builders
do not agree on what "corpus" means, and only one of them is right:

- `donations_features._tfidf_cosine` fits over **every unit in the frame**, on
  purpose and documented — which is why `model_explanation` still reads the
  whole units file.
- `psc_features._tfidf_cosine` fits over **only the units named in the pairs it
  was handed** (`subset`). So the same PSC pair already scores differently
  depending on how many pairs it was built with, and a one-pair explanation
  cannot reproduce a scoring run's number today.

Persisting the fitted vocabulary and IDF at scoring time fixes both, but note
that it is a **behaviour change for PSC** (its numbers will move) and a
**no-op for donations** (its numbers must not). Whoever does it should assert
exactly that.

## 9. One stale test, fixed in passing

`test_config_manager.py::test_api_validate_accepts_the_shipped_default` was
already failing before any of this session's work: `POST /api/config/validate`
grew a `warnings` key beside `errors` (section 3), and the test still asserted
`r.json() == {"errors": []}` exactly. The endpoint was right and the test was
stale, so the assertion now expects both keys. Worth knowing that the suite was
not green at the start of this session.

# Linkage — scoring pairs within one dataset

Implements decisions D9, D10, D11, D13 and D17 in `DESIGN.md`. This file is the contract for slice 3.

## What gets compared

Stage 2 (exact keys) leaves merged groups, held groups, and single records. Stage 3 compares **units**, not raw records:

- each merged exact group is one unit
- every other record is its own unit, including each member of a held group

A unit's ID is the smallest `record_id` among its members, compared as strings. That is the same record the group ID `X-<id>` names. A single record's unit ID is its own `record_id`.

Each unit gets one representative row. For every cleaned or raw column, the representative takes the most frequent non-null value among the members, with ties broken by the smallest `record_id`. Every column of the records frame is voted on this way — none is dropped, because the review screen and the exports read columns nothing else touches. A unit of one member is its own representative, so the vote runs only over the pooled units. Priority columns are summed. The row also carries `unit_size`, the distinct `existing_entity_id` values of its members, and `held_group_id` when the record sits in a held group.

The members' entity ids arrive as three columns, because the overlay below has to know when there is exactly one: `existing_entity_ids` is the sorted distinct ids joined with `" | "`, `n_existing_ids` counts them, and `existing_entity_id` is that one id when the count is 1 and null otherwise. A record that sits in more than one held group takes the smallest of those group ids. A unit takes the smallest `held_group_id` any of its members carries, and null when no member carries one — a merged group and a held group may overlap, so a pooled unit can hold both kinds of member.

Units are scored per track. Two units in different tracks are never compared.

## `linkage_settings`

Stored beside the ruleset in each config version. Users edit it on the "Thresholds & Splink" tab.

```json
{
  "tracks": {
    "person": {
      "blocking_rules": [ { "id": "b1", "description": "", "sql": "l.surname = r.surname AND l.forename_initial = r.forename_initial" } ],
      "comparisons":    [ { "id": "c1", "column": "forename", "splink_function": "cl.JaroWinklerAtThresholds",
                            "splink_args": { "score_threshold_or_thresholds": [0.92, 0.85] },
                            "term_frequency": false, "description": "" } ],
      "em_blocking_rules": [ "l.surname = r.surname" ],
      "max_pairs": 20000000
    },
    "organisation": { "...": "same shape" }
  },
  "em_iterations": 20,
  "probability_two_random_records_match": null,
  "match_probability_threshold_candidate": 0.05,
  "match_probability_threshold_high": 0.92,
  "match_probability_threshold_review": 0.50
}
```

- A blocking rule is SQL over `l.` and `r.` columns of the representative rows. A rule of the form `l.col = r.col` becomes Splink's `block_on`. Anything else is passed through as a custom rule.
- `splink_function` is one of a fixed allow-list: `cl.ExactMatch`, `cl.JaroWinklerAtThresholds`, `cl.JaroAtThresholds`, `cl.LevenshteinAtThresholds`, `cl.DamerauLevenshteinAtThresholds`, `cl.JaccardAtThresholds`, `cl.NameComparison`, `cl.ForenameSurnameComparison`, `cl.PostcodeComparison`, `cl.ArrayIntersectAtSizes`. The UI offers these and nothing else. One more comparison is built by this tool rather than taken from Splink's library: `custom.NumericDifferenceAtThresholds` with `splink_args: {"thresholds": [0, 1, 2]}`. It makes one level per threshold ("equal", "within 1", "within 2") plus "all other", so a large gap between two numbers, such as two birth years, learns its own weight and is not averaged in with near misses. Friendly name: "Numeric difference".

  `thresholds` must be a non-empty list of numbers, each zero or more, in ascending order. A term-frequency adjustment is refused on it: the levels are gaps between numbers, not values, and down-weighting a common birth year is the mistake that let a 37-year gap score 1.0 in the first PSC sample. It is built from Splink's `CustomComparison` over a `NullLevel`, one `AbsoluteDifferenceLevel` per threshold and an `ElseLevel`, with the labels "Equal `<column>`", "`<column>` within N" and "All other", so the per-pair explanation reads in words.

  **The column must be a number, and cleaning writes text.** `dob_year_clean` leaves `nullify_outside_range` as a string of digits. Stage 3 therefore casts every column a numeric-difference comparison names — and only those — with `pd.to_numeric(..., errors="coerce")` as it builds the frame it hands Splink (`_splink_frame`). `units.parquet` keeps the text, so the review screen, the exports and the vetoes still see what was filed. A value that is not a number becomes null and lands on the null level.
- `term_frequency: true` turns on Splink's term-frequency adjustment for that column.
- `probability_two_random_records_match: null` means "estimate it from the deterministic rules". The deterministic rules are that track's match keys, read out of the ruleset, and `deterministic_recall` (default 0.8) is how much of the truth they are assumed to find. An estimate that fails is logged and Splink's own default stands, because a prior is not worth losing a run over.
- `max_pairs` is the blocking budget. Before Splink predicts, the stage counts the pairs each blocking rule would create. If the total is over budget, the run fails with a structured error that names each rule and its count. Nothing is scored.
- Labels never train Splink. u comes from random sampling and m from EM.

## Pair outcomes

Every scored pair lands in one bucket:

| bucket | rule |
|---|---|
| `accept` | score at or above `threshold_high` |
| `review` | score between `threshold_review` and `threshold_high` |
| `reject` | score below `threshold_review` (kept in the file down to `threshold_candidate`) |

The bucket the score alone gives is kept as `score_bucket`, beside the `bucket` the overlays below leave behind. Without it, "how many review pairs do the imported labels agree with" is zero by construction — the first overlay has already moved every agreeing pair to `accept` — and the owner cannot see what the score is doing on its own. Vetoes never touch `score_bucket` either, so what the score made of a pair on its own is always readable.

Three overlays then apply, in this order. The later one wins.

1. **Vetoes** (`RULESET.md`, "Vetoes"). A `review` veto caps the pair at review and a `reject` veto puts it in reject, whatever the score or a graded model said. The pair records `vetoed_by` and `veto_reason`, and `decided_by` becomes `veto`.
2. **Imported labels** (D11). If the two units carry the same single `existing_entity_id`, the pair is `accept` with `decided_by: "import"`, **even when a veto hit it** — an earlier real group is a trusted merge. Such a pair keeps its `vetoed_by` and `veto_reason` and is flagged `veto_conflicts_import: true` so a reviewer can look. Scoring only decides pairs blocking produced, so stage 4 goes further and joins every unit carrying one such id to the others carrying it, within its track, as `import` edges (`ENTITIES.md`). If the two units carry different ids, the pair keeps its bucket and gets `import_disagrees: true`. That flag is a weak signal for training and for sorting. It never decides a pair.
3. **Human labels** from the UI. A TRUE label makes the pair `accept`, a FALSE label makes it `reject`, both with `decided_by: "human"`. A human label always wins.

`pairs.parquet` holds the score and the first two overlays. The human one is joined on where the pairs are read — in SQL by `pairs_reader`, in pandas by `label_overlay.apply_to_pairs` — because a label is a row in a table and recording one must not rewrite a file that will one day hold millions of rows. Only the run's counts and its evaluation are worked out again.

**Vetoes are materialised, not joined at read time.** A run snapshots its ruleset into `config/ruleset.json` when it starts, so the vetoes cannot change under a finished run, and materialising them means the pairs list, the histogram, stage 4 and `score_eval` all read one answer rather than four copies of the rule. The cost is that every path that moves a bucket has to apply them again. They all do it through one function, `stage_3_score.apply_overlays`, which takes the ruleset: stage 3 itself, the re-bucket, applying a model and reverting one. `_strip_overlays` drops `vetoed_by`, `veto_reason` and `veto_conflicts_import` with the other overlay columns, so a stale veto cannot survive a re-bucket.

When more than one veto hits a pair, the strongest action applies — `reject` beats `review` — and `vetoed_by` names the first veto in document order with that action. `{left}` and `{right}` in a reason are filled from the two sides' values of the **first** column the veto's conditions name.

Pairs whose two units sit in the same held group are flagged with that `held_group_id`. The review screen can show or hide them as one block.

## Labels

Table `pair_labels`. One row per decision, append-only:

`id, record_id_a, record_id_b, track, is_match ("TRUE" | "FALSE"), provenance ("manual" | "bulk_range" | "llm" | "import" | "cluster_merge" | "cluster_split"), held_out (0 | 1), reviewer, notes, evidence_url, name_a, name_b, run_id, config_version, created_at, active, superseded_by, decision_id, decision_scope`

`cluster_merge` and `cluster_split` are what a whole-cluster decision writes:
one label per pair inside the cluster, all sharing one `decision_id`, so the
decision can be shown, superseded or undone as a whole. On screen the two
answers are **Match** and **Not a match**; the API keeps TRUE and FALSE. Every
provenance maps onto the one ordered list in `GLOSSARY.md` through
`app/vocabulary.PROVENANCE_MAP`.

`evidence_url` holds a source link for a decision made on outside evidence, such as press reporting that confirms two donors are one person (D13a).

- `record_id_a` is always the smaller of the two IDs, compared as strings. The pair (a, b) and the pair (b, a) are one label.
- The IDs are unit IDs at the time of labelling, which are real record IDs. A label is a statement about those two records. In a later run, it applies to whichever two units contain them.
- A new label on the same pair deactivates the old row and points `superseded_by` at the new one.
- If both records of a TRUE label now sit in one unit, the label is already satisfied. If both records of a FALSE label now sit in one merged exact group, the run reports a **contradiction** and lists it, because an exact key has merged what a human kept apart.
- A label whose two records are in two different units decides that pair. When blocking never produced it, or its score fell below the candidate floor, stage 3 **forces** it into `pairs.parquet` with no score, so a decision is never lost behind a blocking rule.
- A label naming a record this run did not load is skipped. It belongs to another dataset, or to a record the loader no longer keeps.

## Files in a run folder

| File | Holds |
|---|---|
| `events.parquet` | the profile's evidence rows, keyed on `record_id` (D13b). Written by stage 0 when the profile supplies them |
| `units.parquet` | one representative row per unit |
| `unit_members.parquet` | `unit_id`, `record_id` |
| `pairs.parquet` | `unit_id_l`, `unit_id_r`, `track`, `match_probability`, `match_weight`, the `gamma_` columns, `score_bucket`, `bucket`, `decided_by`, `import_disagrees`, `vetoed_by`, `veto_reason`, `veto_conflicts_import`, `held_group_id`, and the summed priority columns of both units, each named `priority_<column>` |
| `splink_model_<track>.json`, `diagnostics/` | the trained model and Splink's charts |
| `blocking_report.json` | pairs per blocking rule, per track, and the budget |
| `score_eval.json` | what the exact groups plus the accepted pairs do to the existing labels |
| `contradictions.json` | the FALSE labels an exact match key has overruled |

## Scoring the result against the existing labels

`score_eval.json` does for the whole chain what `exact_eval.json` does for the match keys. Every accepted pair is an edge between two units; the connected components over those edges are the entities the run would publish. Precision and recall are then counted exactly as in `RULESET.md`, from component sizes, so the two files can be read side by side.

It reports five sets of figures:

- the top level: the exact groups plus **every** accepted pair — the score, the vetoes and the import overlay together;
- `score_only`: the same, leaving out the pairs the import overlay accepted. Those were accepted because the two units already carry the same old entity id, so counting them as recall would be measuring the labels against themselves. A vetoed pair is not in it either, because a vetoed pair is not accepted. This is the number to tune rules against;
- `without_vetoes`: the whole run as it would read with no veto in the ruleset, with a `score_only` of its own inside it. Reading it beside the two above is how "what did this veto cost in recall and buy in precision" is answered from the file. It equals the top level and `score_only` exactly while the ruleset has no vetoes. Beside it, `vetoes` counts `vetoed`, `vetoed_from_accept`, `conflicts_import` and a `by_veto` breakdown, overall and per track;
- `with_human`: the top level with the reviewers' decisions on top — TRUE labels joined up, FALSE labels pulled apart. It is what the run would publish today, and it equals the top level exactly while nobody has labelled anything;
- `exact_only`: the match keys on their own, the same figures `exact_eval.json` holds.

Alongside those it reports the pairs per bucket overall and per track, how many pairs the import overlay decided and how many it flags as disagreeing, the review queue split by whether the imported labels agree, and a 50-bin score histogram per track split the same way.

A track with fewer than two units, or with no blocking rules, is skipped with a log line. That is not an error: a profile may have nothing to compare on one side.

## Scale guards

DuckDB is capped by the `SPLINK_MEMORY_LIMIT` environment variable, which defaults to `6GB` — the server's budget under D17. The blocking budget check runs before any model is trained, so an exploding rule costs a few counting queries and not a night of swapping. It prices each rule **after** its hot-key control, because a budget that prices the uncontrolled rule is not a budget, and it prices the EM training rules too.

**Nothing in stage 3 holds the pairs.** Splink predicts through DuckDB and the result is copied straight to a parquet, keeping only the two unit ids, the two scores and the gamma columns — the retained comparison values are dropped in SQL and never become Python objects. Everything downstream reads that file in bounded batches:

| knob | default | what it bounds |
|---|---|---|
| `PAIR_BATCH_ROWS` | 500,000 | pairs held by any Python step: the buckets, the vetoes, the priority totals, a re-bucket |
| `MODEL_BATCH_PAIRS` | 2,000,000 | pairs held by a model's feature build and predict (stage 3b) |
| `PREDICT_ROUTE_BY_ROUTE_ABOVE` | 5,000,000 | priced pairs above which a track predicts one blocking rule at a time |

**Predicting one blocking rule at a time.** Asking Splink for every blocking rule in one `predict` builds one comparison table the size of their sum, and Splink holds both sides' values for every comparison in it. Above `PREDICT_ROUTE_BY_ROUTE_ABOVE` priced pairs, the stage predicts one rule at a time instead, writes each route's predictions to its own parquet, drops the table before the next route starts, and concatenates the files at the end. Below the limit nothing changes.

It is the same answer. Splink deduplicates across blocking rules while it blocks — rule *n* carries `AND NOT (rule 0 OR … OR rule n-1)` — and both that clause and the `match_key` column come from `BlockingRule.preceding_rules`, which `match_key` is the length of. Handing `predict` a list of one *rule object* rather than a list of one *rule* therefore changes nothing about what the rule does. Nothing is deduplicated afterwards; the exclusion is still Splink's. Proved pair for pair against the one-pass prediction on the donations run and on the 500,000-record PSC sample, and in the suite on a fixture whose routes overlap.

`blocking_report.json` records which path ran, and each route's `match_key`, rule id, pairs and seconds.

**A trained model is saved before the prediction, not after**, with `splink_trained_<track>.json` beside it carrying a fingerprint of everything that shaped it — the comparisons, the prior, the EM settings and seed, the unit count, and the blocking and training SQL *after* their controls. An attempt in the same run folder whose fingerprint matches loads the model and goes straight to prediction. `REUSE_TRAINED_MODEL=0` trains anyway.

`score_eval.evaluate` takes either a frame or a path to `pairs.parquet`. Given a path it reads eleven columns in batches, accumulates every figure as a running total, and gathers the accepted-pair sets as integer-coded edge arrays rather than frames. The two forms are held to identical output by a test. The score-distribution chart is drawn from bin counts computed in DuckDB, not from one row per pair — Altair embeds its data in the page, and one row per pair made a 68 MB HTML file for a picture with fifty bars in it.

Stage 3 also drops the records, units and unit-members frames once the units are written, and reads projections of `units.parquet` after that: four columns for the overlays, the columns a track's rules and comparisons name for Splink, three record columns for the evaluation.

## The hot-key blocking control

A blocking rule puts units into blocks, and a block of n units makes n(n-1)/2 pairs. A handful of very common keys therefore carry most of the work. On the PSC sample, 30 blocks out of 356,138 carry 38% of every pair route pb3 makes. It gets worse with scale, not better: a hot block grows with the data, so its pairs grow with the square, while a selective block's pairs grow in step with the data.

A blocking rule may carry four optional keys that tighten those blocks and leave the rest of the rule alone.

| key | type | means |
|---|---|---|
| `max_block_size` | whole number above zero | a block with more units than this is **oversized** |
| `on_oversize` | `"refine"` or `"drop"` | what to do with an oversized block. Defaults to `"refine"` when `refine_with` is given and `"drop"` when it is not |
| `refine_with` | list of column names | an oversized block is re-blocked by **also** requiring these columns to be equal |
| `drop_above` | whole number, optional | after refining, a block still larger than this is dropped and its units are reported as too common to compare on this rule |

```json
{ "id": "pb3", "sql": "l.name_fingerprint = r.name_fingerprint",
  "max_block_size": 60, "on_oversize": "refine",
  "refine_with": ["forename_initial"], "drop_above": 200 }
```

That rule reads, in the UI: **"Blocks of more than 60 records are compared only when the forename initial also matches; blocks of more than 200 are skipped."** `linkage.control_description(rule)` writes that sentence. A rule with no control has no sentence.

**It is generated SQL, not a filter over pairs.** A pair inside an oversized block is never made. `linkage.controlled_sql(con, units, rule, track)` measures the oversized key values with one `HAVING count(*) > max_block_size` query over that track's units, then writes them into the rule as a literal list:

```sql
(l.name_fingerprint = r.name_fingerprint)
AND (coalesce(cast(l.name_fingerprint as varchar), '') NOT IN ('SMITH', ...)
     OR (l.forename_initial = r.forename_initial))
AND coalesce(cast(l.name_fingerprint as varchar), '') || chr(31)
    || coalesce(cast(l.forename_initial as varchar), '') NOT IN ('SMITH\x1fA', ...)
```

Only the `l.` side is named, because the rule already holds the key equal on both sides. A composite key is its parts joined by the unit separator. The result is a Splink `CustomRule`, so it fits `build_blocking_rule` with nothing new underneath.

The literal list is the cost. A Splink blocking rule is a predicate over `l.` and `r.` columns and has nowhere else to read a set from, so the oversized keys have to be written into the SQL. That is fine while they are few, which is the case the control exists for: 30 values on PSC's pb3, 13 on pb1. `linkage.MAX_INLINE_KEYS` caps it at 5,000 and refuses beyond that with a message saying the rule is too coarse to fix one key at a time.

**Pricing a rule after the control.** `linkage.price_rule(con, units, rule, track)` returns `pairs`, `blocks`, `oversized_blocks`, `pairs_before`, `blocks_before`, `units_dropped`, `units_unrefinable`, `exact` and `residual`. It is DuckDB group arithmetic only — `sum(n * (n - 1) / 2)` per block. No pair is built and Splink is not involved, so a rule that would make a hundred million pairs is priced in one pass over the units.

It reads a rule's SQL into a block key, one-sided filters, `l.x <> r.x` conditions and a residual. Keys and filters are counted exactly. An `l.x <> r.x` is counted exactly too, by subtracting the pairs of its sub-blocks. Anything left over cannot be applied by counting, so `exact` comes back `false`, `residual` names the condition, and every count is an upper bound. PSC's pb6 is the one shipped rule in that position, because of its `(l.dob_year_clean IS NULL OR r.dob_year_clean IS NULL)`.

Three null rules, all of them just SQL's own behaviour. A unit with a null key never blocks, because `l.k = r.k` is false for a null. A unit with a null `refine_with` value cannot be refined and makes no pair once its block is refined; those units are counted in `units_unrefinable`, not in `units_dropped`. A unit with a null on an `l.x <> r.x` column makes no pair on that rule at all.

**Validation** refuses a `max_block_size` that is not a whole number above zero, an `on_oversize` outside `refine` and `drop`, a `refine_with` that is not a non-empty list of strings or that names a column the track does not have, a `drop_above` below `max_block_size`, any of the three other keys with no `max_block_size` beside them, and a control on a rule with no `l.column = r.column` to block on. The errors come back in the same `{path, message}` shape as everything else in `linkage_settings`.

**An EM training rule may carry the control too.** An entry in `em_blocking_rules` may be SQL text, as it always could, or an object `{sql, max_block_size, on_oversize, refine_with, drop_above}`. It has to be able to: an EM rule is priced against the same `max_pairs`, nothing downstream trims it, and PSC's person rule "both name sounds agree" makes 582,535,879 training pairs on the full snapshot against 94,743,605 for the whole of prediction. `linkage.em_entries()` returns the entries as dicts; `linkage.em_rules()` still returns their SQL and every existing caller reads that.

**A control is measured on the frame Splink will score, not on `units.parquet`.** The control inlines its oversized key values into the SQL as text, and the text a column yields depends on its type: `dob_year_clean` is text in `units.parquet` and a float in the scoring frame, because a numeric-difference comparison needs numbers, and DuckDB spells the one `1985` and the other `1985.0`. A control measured on the wrong frame matches nothing, so its `NOT IN` is always true and it silently does nothing. `stage_3_score.cast_numeric_columns` is the contract: the budget's frame and Splink's frame are cast the same way, and the oversized key sets are measured once and shared (`controlled_rules(cache=…)`), keyed on the dtypes as well as the rule.

**Splink cannot count a refined rule.** `on_oversize: "refine"` generates `<the rule> AND (<key> NOT IN (…) OR <the refine columns agree>)`, and that `OR` stops Splink's blocking analyser recognising the equi-join: `count_comparisons_from_blocking_rule` falls back to the cartesian bound and refuses. So the blocking budget prices a refined rule with `price_rule` and everything else with Splink's own counter — `stage_3_score.count_rule_pairs` chooses. Where both can answer they agree to the pair. DuckDB itself is fine with the `OR`, because the rule still carries top-level equalities to hash-join on; it is only the estimator that is defeated.

**`MAX_INLINE_KEYS` is 100,000.** The oversized keys are written into the SQL as literals because a Splink blocking rule has nowhere else to read a set from, so this is the cap on how coarse a rule the control can fix. The PSC person routes need 11,725 to 91,047 of them at `max_block_size: 20`, which is 4.8 MB of SQL across six rules and under a second a rule to build. The blocking report carries `sql_after_control_bytes` rather than the text when the text is over 20 KB, because the report is served to a browser.

**A rule with none of the four keys is untouched.** Its SQL comes back byte for byte, `price_rule` reports `pairs` equal to `pairs_before`, and `blocking_rules()` returns the same three keys it always did. The shipped donations and PSC settings ask for none of this and block exactly as before.

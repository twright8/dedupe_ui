# Linkage — scoring pairs within one dataset

Implements decisions D9, D10, D11, D13 and D17 in `DESIGN.md`. This file is the contract for slice 3.

## What gets compared

Stage 2 (exact keys) leaves merged groups, held groups, and single records. Stage 3 compares **units**, not raw records:

- each merged exact group is one unit
- every other record is its own unit, including each member of a held group

A unit's ID is the smallest `record_id` among its members, compared as strings. That is the same record the group ID `X-<id>` names. A single record's unit ID is its own `record_id`.

Each unit gets one representative row. For every cleaned or raw column, the representative takes the most frequent non-null value among the members, with ties broken by the smallest `record_id`. Priority columns are summed. The row also carries `unit_size`, the distinct `existing_entity_id` values of its members, and `held_group_id` when the record sits in a held group.

The members' entity ids arrive as three columns, because the overlay below has to know when there is exactly one: `existing_entity_ids` is the sorted distinct ids joined with `" | "`, `n_existing_ids` counts them, and `existing_entity_id` is that one id when the count is 1 and null otherwise. A record that sits in more than one held group takes the smallest of those group ids.

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

`id, record_id_a, record_id_b, track, is_match ("TRUE" | "FALSE"), provenance ("manual" | "bulk_range" | "llm" | "import"), held_out (0 | 1), reviewer, notes, evidence_url, name_a, name_b, run_id, config_version, created_at, active, superseded_by`

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

DuckDB is capped by the `SPLINK_MEMORY_LIMIT` environment variable, which defaults to `6GB` — the server's budget under D17. The blocking budget check runs before any model is trained, so an exploding rule costs a few counting queries and not a night of swapping.

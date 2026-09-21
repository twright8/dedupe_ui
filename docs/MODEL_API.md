# Model API — what the model panel reads

`docs/MODEL.md` is the contract for slice 5: one GBT per track, trained on
labels, calibrated, versioned. This file is the wire contract for it. Everything
the model panel, the per-pair explanation and the "most useful to label" sort
need is here.

All endpoints need the session cookie, like the rest of the API. Every response
is plain JSON with no NaN in it: a missing number is `null`. Request and
response bodies use `snake_case`, the same as the pairs API. Run **counts** use
`camelCase`, because that is what `GET /api/runs/{id}` already serves.

`{track}` is `person` or `organisation`. Any other value is **400** with
`{"detail": "track must be person or organisation"}`.

| Endpoint | Purpose |
|---|---|
| `GET /api/model/{track}` | the track's models: active, latest, the version list, any running job |
| `GET /api/model/{track}/versions/{n}` | one version, with its whole training report |
| `GET /api/model/{track}/features` | the feature list and what each feature means |
| `POST /api/model/{track}/train` | start a training job, returns at once |
| `GET /api/model/{track}/train/{job_id}` | poll one training job |
| `GET /api/model/{track}/train/{job_id}/progress` | the same job as a live SSE stream |
| `POST /api/model/{track}/activate` | make one version the active one |
| `POST /api/model/{track}/deactivate` | clear the active model for the track |
| `GET /api/model/{track}/test-set` | what the frozen test set holds |
| `POST /api/model/{track}/test-set/designate` | freeze some human labels as the test set |
| `POST /api/runs/{id}/apply-model` | score a finished run with the active models |
| `POST /api/runs/{id}/revert-model` | put the run back on the Splink score |

**Not these.** `GET /api/model/eval-set` and `POST /api/model/eval-set/designate`
are `roe_ui`'s, and they read the old two-dataset `labels` table. They still
answer, because the tool this app was copied from still needs them, and they can
see none of this tool's labels. Everything here uses `pair_labels`. The Label
library must call `/api/model/{track}/test-set` instead.

Pairs endpoints gain three things, set out at the end of this file: `gbt_score`
on every item, `sort=useful`, and `model_explanation` on pair detail.

---

## Vocabulary

Four words carry most of the meaning. They appear in nearly every response.

**Version.** One trained model. Versions are numbered from 1 per track and never
change once written. `DATA_DIR/models/<track>/versions/<n>/` holds the booster,
the calibration, the feature list and the report.

**Active.** At most one version per track is active. A fresh run scores with the
active version; a finished run scores with it when someone calls `apply-model`.
Training never activates anything.

**Graded.** A version is graded when it was trained on at least
`min_human_labels` human labels (default 50) **and** it has a frozen test set of
at least `min_test_labels` (default 20) with both verdicts in it. Only a graded
model may set buckets. A model that is not graded still writes `gbt_score`, so
the review queue can be re-ordered by it, and the buckets stay on the Splink
score. This is the cold-start rule in `MODEL.md`.

**Cold start.** Fewer than `min_human_labels` human labels. The model trains and
calibrates on the imported labels instead. `graded` is then `false`.

None of these numbers are hard-coded. They live in `linkage_settings` under a
`model` key, are versioned with the ruleset, and every one has a default:
`min_human_labels`, `min_test_labels`, `target_precision`, the four training
weights, the two imported-positive caps, and the LightGBM parameters. A config
version with no `model` key trains on the defaults.

---

## `GET /api/model/{track}`

Everything the top of the model panel needs in one call.

`scorer` is `splink` or `model` — which score a pair of this track would be
decided on right now. `score_column` is the column that score lives in, and
`score_column_label` is the one word for it ("Splink score" or "Model score").
The same three fields are on `GET /api/runs/{id}/diagnostics` and on
`GET /api/runs/{id}/pairs/histogram`, from one definition in
`services/pairs_reader.score_column_for`, so the panel never has to fetch a
histogram to learn one word.

### Response

```json
{
  "track": "person",
  "label": "People",
  "active_version": 2,
  "latest_version": 3,
  "can_auto_accept": false,
  "scorer": "model",
  "score_column": "gbt_score",
  "score_column_label": "Model score",
  "training": null,
  "warnings": [
    {
      "code": "cold_start",
      "message": "This is a new model, trained on the earlier grouping alone — 0 reviewer answers, and 50 are needed. It re-orders the review queue and cannot decide a pair."
    }
  ],
  "active": {
    "version": 2,
    "track": "person",
    "trained_at": "2026-09-18T15:41:02.118372+00:00",
    "active": true,
    "graded": false,
    "cold_start": true,
    "seed": 42,
    "run_id": "run_2026_09_18a",
    "config_version": 1,
    "note": null,
    "n_features": 27,
    "n_train_rows": 9614,
    "n_human_labels": 0,
    "auc": 0.991761,
    "average_precision": 0.999386,
    "accept": null,
    "reject": null
  },
  "versions": [
    { "version": 3, "trained_at": "2026-09-18T16:02:55.004913+00:00", "active": false, "graded": false, "auc": 0.9801, "n_train_rows": 9614, "n_human_labels": 0, "accept": null, "reject": null, "note": "after the rarity table landed" },
    { "version": 2, "trained_at": "2026-09-18T15:41:02.118372+00:00", "active": true, "graded": false, "auc": 0.9793, "n_train_rows": 9614, "n_human_labels": 0, "accept": null, "reject": null, "note": null },
    { "version": 1, "trained_at": "2026-09-18T15:12:40.771004+00:00", "active": false, "graded": false, "auc": 0.9612, "n_train_rows": 9614, "n_human_labels": 0, "accept": null, "reject": null, "note": null }
  ]
}
```

`versions` is newest first and carries only the summary fields above — never the
whole report, because a panel that lists ten versions should not download ten
reports. `active` repeats the active version's summary so the panel does not
have to search the list. It is `null` when nothing is active.

`can_auto_accept` is `active` present **and** `active.graded` **and**
`active.accept` not null. It is the one flag that says whether the model is
allowed to decide a pair.

`training` is the running or most recent training job for this track, in the
shape `GET .../train/{job_id}` returns, or `null` when the track has never
trained in this process. A panel can therefore restore a progress bar after a
page reload.

A track with no model at all answers with `active_version`, `latest_version`,
`active` all `null` and `versions` empty. That is **200**, not 404: "no model
yet" is a normal state the panel draws a Train button for.

---

## `GET /api/model/{track}/versions/{n}`

One version, with the whole training report. **404** with
`{"detail": "No version 7 for track person"}` when it does not exist.

### Response

```json
{
  "version": 2,
  "track": "person",
  "trained_at": "2026-09-18T15:41:02.118372+00:00",
  "active": true,
  "graded": false,
  "cold_start": true,
  "seed": 42,
  "run_id": "run_2026_09_18a",
  "config_version": 1,
  "note": null,
  "n_features": 27,
  "n_train_rows": 9614,
  "n_human_labels": 0,
  "auc": 0.991761,
  "average_precision": 0.999386,
  "accept": null,
  "reject": null,
  "report": { "...": "the training report, in full, below" },
  "features": [ { "...": "the feature metadata, as GET /features returns it" } ]
}
```

### The training report

This is the whole of `report`. Every number in it comes from one training run
and never changes afterwards.

```json
{
  "track": "person",
  "version": 2,
  "trained_at": "2026-09-18T15:41:02.118372+00:00",
  "seed": 42,
  "run_id": "run_2026_09_18a",
  "config_version": 1,
  "graded": false,
  "cold_start": true,
  "known_limit": "The imported labels were made mostly on the name. They cannot teach the model when two people with one name are different people. Only new keep-apart labels can.",

  "labels": {
    "total_rows": 9614,
    "positives": 9496,
    "negatives": 118,
    "human_total": 0,
    "by_source": [
      { "source": "human",           "held_out": 0, "rows": 0,    "positives": 0,    "negatives": 0,   "weight": 1.0,  "capped": false },
      { "source": "decision",        "held_out": 0, "rows": 0,    "positives": 0,    "negatives": 0,   "weight": 1.0,  "capped": false },
      { "source": "import_agree",    "held_out": 0, "rows": 9496, "positives": 9496, "negatives": 0,   "weight": 0.25, "capped": false },
      { "source": "import_disagree", "held_out": 0, "rows": 118,  "positives": 0,    "negatives": 118, "weight": 0.1,  "capped": false },
      { "source": "human",           "held_out": 1, "rows": 0,    "positives": 0,    "negatives": 0,   "weight": null, "capped": false }
    ],
    "weights": { "human": 1.0, "decision": 1.0, "import_agree": 0.25, "import_disagree": 0.1 },
    "sampling": { "import_agree_available": 9496, "import_agree_kept": 9496, "cap": 200000, "per_human_label": 50, "cap_applied": null }
  },

  "folds": {
    "n_folds": 4,
    "grouping": "connected components over units",
    "n_groups": 7811,
    "rows_per_fold": [2411, 2402, 2401, 2400]
  },

  "auc": { "value": 0.9793, "source": "out_of_fold", "n": 9614 },
  "average_precision": { "value": 0.9891, "source": "out_of_fold", "n": 9614 },
  "brier": { "raw": 0.0271, "calibrated": 0.0244 },

  "calibration": {
    "method": "platt",
    "fitted_on": "imported",
    "n": 9614,
    "curve": [ { "raw": 0.0, "calibrated": 0.0032 }, { "raw": 0.005, "calibrated": 0.0033 } ],
    "points": [
      { "bin": 0, "score_from": 0.0, "score_to": 0.1, "mean_predicted": 0.021, "observed": 0.016, "n": 610,
        "observed_wilson_lower": 0.009, "observed_wilson_upper": 0.028 }
    ]
  },

  "thresholds": {
    "available": false,
    "reason": "no frozen test set (no human labels with held_out = 1)",
    "target_precision": 0.99,
    "accept": null,
    "reject": null,
    "accept_metrics": null,
    "reject_metrics": null,
    "n_test": 0,
    "n_test_positive": 0,
    "n_test_negative": 0,
    "grid": []
  },

  "importance": {
    "gain": [
      { "name": "match_weight", "label": "Splink match weight", "group": "splink", "value": 21044.2, "share": 0.38 }
    ],
    "shap": [
      { "name": "match_weight", "label": "Splink match weight", "group": "splink", "value": 1.912, "share": 0.31 }
    ],
    "shap_sample_rows": 9614,
    "ablation": {
      "metric": "average_precision_out_of_fold",
      "full": 0.9891,
      "rows": [
        { "group": "name", "label": "Name", "n_features": 8, "value": 0.9402, "delta": -0.0489 }
      ]
    }
  },

  "references": [
    { "name": "uk_name_frequencies", "label": "UK name frequencies", "present": true,
      "rows": 4196886, "path": "references/uk_name_frequencies.parquet",
      "built_at": "2026-09-18T16:03:11.482915+00:00",
      "affects": ["surname_log_frequency", "full_name_log_frequency"],
      "description": "Forename and surname counts over about 7.5 million UK individuals…" }
  ],

  "warnings": [
    { "code": "cold_start", "message": "Trained on imported labels only — 0 human labels, and 50 are needed. The model re-orders the review queue and cannot decide a pair." },
    { "code": "no_test_set", "message": "No frozen test set, so no accept or reject line was set and every pair the model scores goes to review." }
  ],

  "timings": { "features_secs": 6.41, "train_secs": 1.98, "ablation_secs": 12.7, "total_secs": 22.4 },
  "features": [ { "...": "the feature metadata, as GET /features returns it" } ]
}
```

Field by field, where it is not obvious:

**`labels.by_source`** is the table in `MODEL.md`, counted. `source` is one of
`human`, `decision`, `import_agree`, `import_disagree`. The `held_out = 1` row
is the frozen test set: it has no weight, because it is never trained on.
`capped` says the sampler dropped rows from that source.

**`labels.sampling`** explains the imported-positive cap. `cap` is the absolute
row limit; `per_human_label` is the second limit, at most that many imported
positives per human label, which only bites once humans have labelled anything.
`cap_applied` names which limit bound, or is `null` when neither did.

**`folds.grouping`** is fixed text. Folds split on connected components over
**units**: every training pair is an edge, the components are the groups, and a
group never straddles a fold. One donor's pairs therefore never sit on both
sides.

**`auc.source`** is `out_of_fold` or `held_out`. It is `held_out` when the
frozen test set has both classes; then `n` is the test set size. Otherwise it is
the out-of-fold score over the training rows, which is honest about ordering but
not about a model's behaviour on fresh reviewers' work. `value` is `null` when
neither is computable, with `source: "none"`.

**`calibration.fitted_on`** is `human` or `imported`. `MODEL.md` requires human
rows only; `imported` appears only in a cold start, and then `graded` is
`false`. `curve` is the fitted Platt curve on a 201-point grid, for drawing the
mapping. `points` is the reliability diagram: ten equal-width bins of the
calibrated score, each with how often the label was actually positive and a
Wilson 95% interval on that. A bin with no rows is left out.

**`thresholds`** is the frozen test set, worked out on the calibrated score.
`grid` walks thresholds in 0.05 steps; each row is `{threshold,
n_predicted_positive, tp, fp, precision, precision_wilson_lower, recall,
recall_wilson_lower}`. `accept` is the lowest threshold whose
`precision_wilson_lower` reaches `target_precision`. `reject` is the highest
threshold below which the same test says the pairs are non-matches at the target
precision. Either can be `null`, which means "no line, everything to review".
`accept_metrics` and `reject_metrics` are that grid row, repeated, so the panel
can print "precision 0.994 (lower bound 0.981), recall 0.86" without searching
the grid. `available: false` carries a `reason` and an empty grid — no
fabricated perfect numbers on an empty test set.

**`importance`** is the same feature list three ways. `gain` is LightGBM's own
total split gain. `shap` is the mean absolute SHAP value from the booster's
`pred_contrib`, over `shap_sample_rows` training rows. Both carry `share`, the
value as a fraction of the total, so a bar chart needs no arithmetic. `ablation`
retrains the model without each feature group, reusing the same folds, and
reports the out-of-fold average precision. `delta` is `value - full`, so a
negative delta means the group was helping. Every list is sorted, biggest first.

**`references`** lists every outside table the profile declares, whether it is
there, how big it is, when it was built, and which features go null without it.
This is the **only** place that says a reference table is missing — there is no
second list of missing names beside it.

**`warnings`** codes in use: `metrics_on_imported_labels`, `cold_start`,
`no_test_set`, `test_set_single_class`, `missing_reference`, `few_negatives`,
`uneven_folds`, `feature_all_null`. `metrics_on_imported_labels` is the one to
print largest on a cold start: it says the AUC measures how well the model
reproduces the old name-based work, not whether it decides pairs correctly.

---

## `GET /api/model/{track}/features`

The feature metadata. The panel uses it for labels and grouping; the per-pair
explanation uses it to turn a feature name into plain words. It does not need a
trained model — it describes what the next training run would build.

### Response

```json
{
  "track": "person",
  "n_features": 29,
  "groups": [
    { "key": "splink", "label": "Splink", "n_features": 5 },
    { "key": "name", "label": "Name", "n_features": 8 },
    { "key": "rarity", "label": "Name rarity", "n_features": 2 },
    { "key": "recipients", "label": "Recipients", "n_features": 3 },
    { "key": "timing", "label": "Timing", "n_features": 2 },
    { "key": "amounts", "label": "Amounts", "n_features": 6 },
    { "key": "kind", "label": "Kind of donor", "n_features": 1 },
    { "key": "size", "label": "Size", "n_features": 2 }
  ],
  "features": [
    { "name": "match_weight", "label": "Splink match weight", "group": "splink", "monotone": 1, "source": "generic", "null_when": null, "categories": null },
    { "name": "gamma_surname", "label": "Surname agreement level", "group": "splink", "monotone": 0, "source": "generic", "null_when": null, "categories": null },
    { "name": "name_jaro_winkler", "label": "Whole name similarity", "group": "name", "monotone": 1, "source": "profile", "null_when": "either side has no cleaned name", "categories": null },
    { "name": "surname_log_frequency", "label": "How common the surname is in the UK", "group": "rarity", "monotone": -1, "source": "profile", "null_when": "the UK name frequency table is missing", "categories": null },
    { "name": "status_kind", "label": "Kind of donor", "group": "kind", "monotone": 0, "source": "profile", "null_when": null,
      "categories": ["kinds differ", "unknown", "individual", "public fund", "company or registered body", "trade union", "association, trust or other", "registered political party"] }
  ],
  "references": [
    { "name": "uk_name_frequencies", "label": "UK name frequencies", "present": true,
      "rows": 4196886, "built_at": "2026-09-18T16:03:11.482915+00:00",
      "path": "references/uk_name_frequencies.parquet",
      "affects": ["surname_log_frequency", "full_name_log_frequency"] }
  ]
}
```

`monotone` is the constraint handed to LightGBM: `1` means the feature may only
push the score up, `-1` only down, `0` no constraint. `source` is `generic` when
shared code builds it from `pairs.parquet` and `profile` when the profile's
builder does. `null_when` is plain words for when the feature has no value, or
`null` when it always has one.

`categories` is `null` for an ordinary number. When it is a list, the feature is
a **category**: the built value is an index into that list, and LightGBM splits
it by set membership rather than by `<=`. A category carries no monotone
constraint — there is no order in which a trade union sits between a company and
a trust — so `monotone` is always 0 for one. The list belongs to the version it
came with: a screen showing an older model must read that version's list, never
the one a fresh train would build.

`render` says how the backend puts a value for this feature into words. It is
for information — the per-pair explanation already carries the rendered string —
so a panel never has to implement it. The kinds:

| `render` | A value reads as |
|---|---|
| `number` | `1.87` |
| `bits` | `3.48 bits` |
| `similarity` | `0.87` |
| `share` | `50%` |
| `flag` | `yes` / `no` |
| `count` | `3` |
| `years` | `10 years`, or `no gap` |
| `log_records` | `99 records` |
| `log_people` | `about 52,089 in the UK`, or `not in the UK name table` |
| `log_ratio` | `about 100 times apart`, or `the same` |
| `category` | `trade union` |
| `gamma` | the Splink comparison level's own label, e.g. `Jaro-Winkler >= 0.92` |

A null value always reads `nothing to compare`, whatever the kind.

The `splink` group's `gamma_` features depend on the config's comparisons, so
the list changes when someone edits the comparisons. Everything else is fixed
per track.

---

## `POST /api/model/{track}/train`

Starts a training job and returns at once. Training reads one finished run's
`pairs.parquet`, `units.parquet` and `events.parquet` for the features, and the
`pair_labels` table for the human labels.

### Request

```json
{ "run_id": "run_2026_09_18a", "seed": 42, "note": "after the rarity table landed" }
```

`run_id` is required. `seed` defaults to 42; the same seed on the same inputs
gives the same model, byte for byte. `note` is free text shown in the version
list, and may be `null`.

### Response — **202**

```json
{ "job_id": "mj_7f3c1a92d4e0", "track": "person", "state": "queued", "run_id": "run_2026_09_18a" }
```

Errors:

- **404** `{"detail": "Run not found"}`
- **400** `{"detail": "Run has no scored pairs yet"}` — the run never reached stage 3.
- **400** `{"detail": "Nothing to train on: this run has no labels and no imported entity ids for the person track"}`
- **409** `{"detail": "A model is already training for the person track"}` — one job per track at a time.

---

## `GET /api/model/{track}/train/{job_id}`

Poll a job. Also served as `training` inside `GET /api/model/{track}`.

```json
{
  "job_id": "mj_7f3c1a92d4e0",
  "track": "person",
  "run_id": "run_2026_09_18a",
  "state": "running",
  "step": "importance",
  "step_label": "Working out which features matter",
  "percent": 70,
  "message": "Ablating group 3 of 6",
  "started_at": "2026-09-18T15:40:40.223311+00:00",
  "finished_at": null,
  "version": null,
  "error": null
}
```

`state` is `queued`, `running`, `done` or `failed`. `step` walks a fixed list, so
a panel can draw a strip like the run screen's:

| `step` | `step_label` | `percent` when it starts |
|---|---|---|
| `labels` | Collecting labels | 5 |
| `features` | Building features | 15 |
| `folds` | Splitting into folds | 40 |
| `fit` | Fitting the model | 45 |
| `calibrate` | Calibrating | 60 |
| `grade` | Grading on the test set | 65 |
| `importance` | Working out which features matter | 70 |
| `save` | Saving the version | 95 |

On `done`, `version` is the new version number and `percent` is 100. On
`failed`, `error` is the message and `version` stays `null`. **404** for an
unknown job id.

## `GET /api/model/{track}/train/{job_id}/progress`

The same job as an SSE stream, exactly like `GET /api/runs/{id}/progress`. Each
message is one JSON object on a `data:` line:

```
data: {"event": "step", "step": "features", "step_label": "Building features", "percent": 15, "message": null, "timestamp": 1789663240.41}

data: {"event": "complete", "version": 3, "percent": 100, "timestamp": 1789663262.85}
```

`event` is `step`, `complete` or `error`. An `error` message carries
`{"event": "error", "message": "..."}`. The stream closes after `complete` or
`error`. Subscribing to a job that has already finished gets its final event and
then the close, so a late subscriber never hangs.

---

## `POST /api/model/{track}/activate`

### Request

```json
{ "version": 3 }
```

`version` may be omitted, which activates the newest version of that track.

### Response

```json
{
  "ok": true,
  "track": "person",
  "active_version": 3,
  "graded": false,
  "can_auto_accept": false,
  "warnings": [
    { "code": "cold_start", "message": "Trained on imported labels only — 0 human labels, and 50 are needed. The model re-orders the review queue and cannot decide a pair." }
  ]
}
```

Activating a version that is not graded is allowed and is the normal cold-start
path — the model re-orders the queue and decides nothing. The `warnings` say so,
and the panel should print them beside the Active badge.

**400** `{"detail": "No version 9 for track person"}`.

## `POST /api/model/{track}/deactivate`

No body. Response `{"ok": true, "track": "person", "active_version": null}`.
Deactivating when nothing is active is fine and returns the same thing.

---

## The frozen test set

Nothing is graded without one, so no model can decide a pair until a test set
exists. These two endpoints are how one is made. They replace `roe_ui`'s
`/api/model/eval-set` pair, which reads a table this tool does not use.

The split is designated **after** labelling, not chosen at label time. A
reviewer cannot sensibly decide "this one is for testing" on every pair, and a
test set cannot be chosen before there are labels to choose from. A new label
starts at `held_out = 0`, and re-deciding a pair inherits whatever role it
already had, so a label never falls out of the test set by being looked at again.

### `GET /api/model/{track}/test-set`

```json
{
  "track": "person",
  "total": 40,
  "by_verdict": { "TRUE": 22, "FALSE": 18 },
  "by_answer": { "Match": 22, "Not a match": 18 },
  "training": 61,
  "designatable": 61
}
```

`by_answer` is the same two numbers under the words a reviewer reads, so no
screen has to know that TRUE means Match.

`total` is the frozen set. `training` is every other active label of this track.
`designatable` is how many of those could still be frozen: only a reviewer's own
decision on that pair counts, so `manual` and `bulk_range` and nothing else. A
cluster decision writes a star of labels from one click, and an imported label
was never confirmed in this UI (D11) — grading on either would flatter the model.

### `POST /api/model/{track}/test-set/designate`

Two ways to ask. **The tool chooses** — the newest answers, balanced between the
two answers:

```json
{ "n": 200 }
```

```json
{
  "designated": 14,
  "left_for_training": 61,
  "track": "person",
  "total": 40,
  "by_verdict": { "TRUE": 22, "FALSE": 18 },
  "by_answer": { "Match": 22, "Not a match": 18 },
  "training": 61,
  "designatable": 47
}
```

**The reviewer chooses**, by id. This is what the Label library needs: the old
two-dataset tool had a `POST /api/labels/role` that moved chosen labels between
the training set and the test set, and nothing here could do that. This is that,
and only that — `label_ids` wins over `n` when both are sent:

```json
{ "label_ids": [412, 418, 431] }
```

```json
{
  "frozen": [412, 418],
  "refused": [
    { "label_id": 431, "reason": "Freezing this would leave fewer than half the Match answers to train on" }
  ],
  "track": "person",
  "total": 42,
  "by_verdict": { "TRUE": 23, "FALSE": 19 },
  "by_answer": { "Match": 23, "Not a match": 19 },
  "training": 59,
  "designatable": 45
}
```

`frozen` are the ids now in the test set. `refused` says which were not and why,
one row each, in words a reviewer can act on. Nothing is refused silently.

**400** when a named id is not in the library:
`{"detail": "No label 99999 in the library"}`.

The three rules hold whichever way an answer is chosen:

- **Only a reviewer's own answer.** `manual` and `bulk_range` and nothing else.
  A machine-written answer, an imported one and a group decision are refused
  with "Only an answer a reviewer saved one at a time, or from a band of
  scores, can go in the test set".
- **Never more than half of either answer.** Designating a test set must not
  empty the training pool. With twelve Match answers a call takes six, whatever
  `n` or `label_ids` says, and the cap counts what is already frozen, so two
  calls cannot do what one call is refused.
- **Freezing is permanent.** There is no unfreeze, no endpoint that takes an
  answer back out, and no service function that sets `held_out` back to 0 — a
  figure quoted off a frozen test set has to stay quotable. A second call tops
  the set up rather than replacing it.

---

## `POST /api/runs/{id}/apply-model`

Scores a finished run with the active model of each track and re-buckets on it.
Splink is **not** re-run: the stage reads `pairs.parquet`, adds `gbt_score`, and
re-applies the overlays and the labels on top, exactly as `LINKAGE.md` sets out.

### Request

```json
{ "force": false }
```

`force` overrides the collapse guard. The body may be omitted.

### Response

```json
{
  "ok": true,
  "tracks": [
    { "track": "person", "version": 3, "graded": false, "applied": true, "decided_by_model": 0, "warning": null },
    { "track": "organisation", "version": 1, "graded": false, "applied": true, "decided_by_model": 0, "warning": null }
  ],
  "reclustered": false,
  "counts": { "...": "the run's counts, in the camelCase GET /api/runs/{id} serves" },
  "review_before": 1050,
  "review_after": 1050
}
```

`counts` is the run's **whole** counts, merged. Applying a model recomputes
stage 3's keys and no others, so the loader's, the match keys' and the entities'
numbers are laid under it rather than replaced — `GET /api/runs/{id}` after an
apply says everything it said before, plus the model keys, and the `has*` flags
the run screen draws its tabs from all survive. Every partial rerun in this app
works this way: re-bucket, recluster, a label write, apply and revert all merge.

### `reclustered`

A cluster is a connected component over the accepted pairs, so a bucket that
moves leaves the clusters and the proposed entities stale. When the buckets
move, apply and revert finish by running **stages 4 and 5** again — the two the
recluster endpoint runs, with no Splink and no re-reading the input — and
`counts` then carries the new `clustersTotal`, `entitiesProposed` and the rest.

`reclustered` says which happened:

- `true` — buckets moved, stages 4 and 5 ran, so the clusters and entities on
  screen are current.
- `false` — not one pair changed bucket, so nothing downstream could have
  changed and the work was skipped. This is the ordinary cold-start case: the
  model scored every pair, `decided_by_model` is 0 on every track, and the
  buckets are still Splink's.

`POST /api/runs/{id}/revert-model` returns `reclustered` the same way.

The run's counts gain six model keys. The same six appear on the histogram
response, with the same names and the same shapes.

| Key | Shape | Meaning |
|---|---|---|
| `modelActive` | boolean | at least one track scored with a model |
| `modelGraded` | boolean | **every** track that scored is graded, so the buckets really are the model's |
| `modelVersion` | `{"person": 3}` | the version each track scored with; a track that did not score is absent |
| `modelAcceptLine` | `{"person": 0.61}` | the accept line each track was bucketed on, `null` inside the object when that model set none |
| `modelRejectLine` | `{"person": 0.12}` | the same for the reject line |
| `modelWarning` | string or `null` | the collapse-guard message, when one was forced past |

The three per-track values are **objects keyed by track**, always, even when only
one track scored. They are recorded **on the run**, not looked up from the model
store, so activating a newer version never changes what a finished run says it
was decided by — the read-only lines a threshold panel draws stay the lines the
pairs were actually bucketed on.

On a cold-start apply the shape is `{"modelActive": true, "modelGraded": false,
"modelVersion": {"person": 1, "organisation": 1}, "modelAcceptLine":
{"person": null, "organisation": null}, ...}`: the model scored both tracks and
decided nothing.

**409** when the collapse guard fires and `force` is not set. The run is put back
on the Splink score first, so nothing is left half-applied:

```json
{ "detail": "Applying this model was refused: the review band fell from 1050 pairs to 3. Reverted to Splink; nothing changed. Add human labels and a frozen test set, retrain, then apply — or send force: true." }
```

The guard is `roe_ui`'s, carried over: it fires when the applied scores take
fewer than 10 distinct values (near two-valued), or when a review band that had
pairs in it collapses to zero or a tiny remnant.

**400** `{"detail": "No active model for any track"}`.

## `POST /api/runs/{id}/revert-model`

No body. Puts the run back on the Splink score.

```json
{ "ok": true, "counts": { "...": "the run's counts" } }
```

It is the exact inverse: the `gbt_score` column comes off the pairs, the run's
model state goes, `modelActive` is `false` and the three per-track objects are
empty. `sort=useful` and the histogram fall back to Splink. Leaving the scores
behind was the alternative and is worse — a number on a pair that nothing is
deciding by reads as if it were, and re-applying is one request.

Reverting a run that was never applied is fine and changes nothing.

## What a recluster does to `gbt_score`

Reclustering merges units, and the pairs of a merged unit are re-pointed onto
the surviving one. Where two old pairs land on one new pair, the row with the
better Splink score wins **whole** — so its `gbt_score` travels with it — and the
new row is marked `rescored: false`, meaning Splink has not seen this pairing.

That score is therefore a carried-over one, not a score of the pair as it now
stands: the units on both sides may hold more records than when it was computed.
`POST /api/runs/{id}/apply-model` recomputes `gbt_score` for every pair in the
file from the units as they are now, so re-applying after a recluster is what
makes the scores current. A screen showing `rescored: false` should say the same
thing about the model score as it does about the Splink one.

---

## What the pairs endpoints gain

### `gbt_score` on every item

`GET /api/runs/{id}/pairs` items and `GET /api/runs/{id}/pairs/{pair_id}` gain
one field:

```json
{ "pair_id": "104841|99117", "match_probability": 0.917624442966025, "gbt_score": 0.8814, "...": "..." }
```

It is `null` on every pair of a run that has not been scored by a model, and on
a pair of a track whose model is not active. `match_probability` never changes —
both scores are always readable side by side.

`decided_by` gains a fourth value, `model`, for a pair a graded model put in its
bucket. The four are now `score`, `model`, `import`, `human`, and the
`decided_by` filter takes any of them.

The list gains two filters, `min_gbt` and `max_gbt`, each 0 to 1 inclusive. Both
are **ignored** on a run no model has scored, rather than emptying the list: a
saved screen with a `gbt` filter on it must not go blank when someone deactivates
the model.

### `GET /api/runs/{id}/pairs/histogram`

One shape for every series: `edges` has `bins + 1` numbers, and every count
series is a flat array of `bins` numbers lined up with them. `by_score_column`
is the same object of the same arrays, so nothing has to be read two ways.

```json
{
  "track": "person",
  "bins": 5,
  "edges": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
  "score_column": "gbt_score",
  "total":  [836, 1775, 458, 16867, 3227],
  "accept": [0, 0, 0, 16867, 3227],
  "review": [0, 1775, 458, 0, 0],
  "reject": [836, 0, 0, 0, 0],
  "agrees": [12, 1180, 401, 16810, 3221],
  "disagrees": [770, 0, 0, 0, 0],
  "unknown": [54, 595, 57, 57, 6],
  "by_score_column": {
    "total":  [1484, 67, 53, 67, 21492],
    "accept": [1301, 41, 33, 52, 18699],
    "review": [110, 21, 16, 12, 1054],
    "reject": [73, 5, 4, 3, 1739],
    "agrees": [1002, 40, 34, 51, 21497],
    "disagrees": [402, 12, 7, 5, 344],
    "unknown": [80, 15, 12, 11, 651]
  },
  "modelActive": true,
  "modelGraded": false,
  "modelVersion": { "person": 1 },
  "modelAcceptLine": { "person": null },
  "modelRejectLine": { "person": null },
  "modelWarning": null
}
```

- The top-level series always count `match_probability`. `by_score_column`
  counts `gbt_score`, and is `null` when the run has none. The threshold panel
  can therefore draw both dials, or one.
- `score_column` names which dial is deciding: `gbt_score` when a model has
  scored the run, `match_probability` otherwise.
- The six `model*` keys are exactly the ones the run's counts carry, and mean the
  same things. With `?track=person` the three per-track objects are narrowed to
  that track, so a single-track panel gets a single-track answer.
- Every series counts the same rows: `accept + review + reject` is `total`, and
  `agrees + disagrees + unknown` is `total` too.

### `sort=useful`

A fourth value for `sort` on `GET /api/runs/{id}/pairs`: the pairs most worth a
reviewer's time first. The formula is fixed here so the panel can explain it.

For one pair:

```
uncertainty  = 1 − |2 · gbt_score − 1|
disagreement = |gbt_score − match_probability|
weight       = ln(1 + priority) / ln(1 + priority_max)
usefulness   = (0.5 · uncertainty + 0.5 · disagreement) · (0.5 + 0.5 · weight)
```

- `priority` is the pair's summed priority column — donations: the two units'
  total donated. `priority_max` is the largest value of it anywhere in the run,
  so `weight` lands in 0 to 1. With no priority column, or a maximum of zero,
  `weight` is 1.
- The priority term is `0.5 + 0.5 · weight`, never `weight` itself. Money tilts
  the order; it never buries a genuinely uncertain pair behind a rich one.
- Without a `gbt_score` — no model applied — `uncertainty` uses
  `match_probability` in its place and `disagreement` is 0. The sort therefore
  works before any model exists, and means "nearest the middle of the Splink
  score, biggest first".
- Pairs a human has already decided are still listed; filter them out with
  `labelled=no` as usual.
- **`order=desc` is most-useful-first, and `desc` is the default.** `order=asc`
  puts the least useful first, which is only ever wanted for a sanity check.
- `pair_id` breaks every tie, so paging is stable.

Items sorted this way carry the working, so the screen can say why a pair is
near the top. A real item, from the September 2026 donations run with a
cold-start model applied:

```json
{
  "pair_id": "46311|99121",
  "match_probability": 0.9082720163058292,
  "gbt_score": 0.5181353024709403,
  "bucket": "review",
  "decided_by": "score",
  "usefulness": {
    "score": 0.5820647995343373,
    "uncertainty": 0.9637293950581194,
    "disagreement": 0.3901367138348889,
    "weight": 0.7197115599865745
  },
  "...": "..."
}
```

`usefulness` is present only when `sort=useful`.

### `model_explanation` on pair detail

`GET /api/runs/{id}/pairs/{pair_id}` gains one object beside the Splink
explanation it already serves. It is `null` when the pair's track has no active
model.

A real one, for the pair above:

```json
{
  "model_explanation": {
    "track": "person",
    "version": 1,
    "graded": false,
    "raw": 0.443574,
    "score": 0.518135,
    "base": 9.731239,
    "contributions": [
      { "name": "name_jaro_winkler",       "label": "Whole name similarity",                 "group": "name",       "value": 0.8666163901458019, "value_label": "0.87",                        "contribution": -8.799158 },
      { "name": "middle_initial_agrees",   "label": "Middle initials agree",                 "group": "name",       "value": 0.0,                "value_label": "no",                          "contribution": -1.423844 },
      { "name": "full_name_log_frequency", "label": "How common the whole name is in the UK","group": "rarity",     "value": 0.0,                "value_label": "not in the UK name table",    "contribution": 0.524252 },
      { "name": "same_local_unit",         "label": "Gave through the same local unit",      "group": "recipients", "value": 0.0,                "value_label": "no",                          "contribution": -0.335865 },
      { "name": "gamma_surname",           "label": "Surname agreement level",               "group": "splink",     "value": 3.0,                "value_label": "Exact match on surname",      "contribution": 0.203321 },
      { "name": "amount_distribution_distance", "label": "Distance between their gift-size patterns", "group": "amounts", "value": 1.8674301523274175, "value_label": "about 74 times apart", "contribution": -0.190084 },
      { "name": "status_kind",             "label": "Kind of donor",                         "group": "kind",       "value": 2.0,                "value_label": "individual",                  "contribution": 0.0 }
    ],
    "known_limit": "The imported labels were made mostly on the name. They cannot teach the model when two people with one name are different people. Only new keep-apart labels can."
  }
}
```

- `base` plus every `contribution` sums to the raw margin in log-odds, so
  `1 / (1 + exp(−sum))` is `raw` exactly. These are exact Tree-SHAP values from
  LightGBM's `pred_contrib`, not an approximation. `base` is the average margin
  over the training set, which is why it is large and positive here: the imported
  labels are mostly matches, so the model starts by expecting one, and the whole
  name similarity of 0.87 is what pushes this pair back down.
- `raw` is the booster's probability; `score` is it after calibration, which is
  the number the run buckets on and the number `gbt_score` carries.
- `contributions` is sorted by absolute contribution, biggest first, and holds
  every feature with a non-zero contribution. A panel showing the top ten should
  take the first ten.
- `value` is the feature's raw value for this pair, `null` when it had none.
  **`value_label` is that value in plain words, and is never null** — it is what
  a screen prints beside the bar, because the raw number on its own is not
  readable. "0.87" means one thing for a name similarity and another for the log
  of a count; `1` means "yes" for a flag and "1 record" for a count; a category
  code means nothing at all. The rendering follows the feature's `render` kind
  in the table above, and a null value reads "nothing to compare". Both the
  rendering rules and any category list come from the metadata stored with
  **this** version, so an older model is always read the way it was written.
- `known_limit` repeats `MODEL.md`'s sentence, so it travels with the
  explanation rather than living only in the model panel.

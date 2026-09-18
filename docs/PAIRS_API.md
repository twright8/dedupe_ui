# Pairs API — what the review screen reads

Stage 3 scores pairs of **units** and writes `pairs.parquet`. These endpoints
serve that file. `docs/LINKAGE.md` says what a unit is and how a pair gets its
bucket; this file is the wire contract.

Every example below is a real response from a run over the September 2026
donations sheet: 51,839 records, 22,435 units, 28,843 scored pairs, and ten
pairs a reviewer has decided.

All endpoints need the session cookie, like the rest of the API. Every response
is plain JSON with no NaN in it: a missing number or string is `null`.

| Endpoint | Purpose |
|---|---|
| `GET /api/runs/{id}/pairs` | one page of scored pairs, both units side by side |
| `GET /api/runs/{id}/pairs/histogram` | the score histogram for the threshold panel |
| `GET /api/runs/{id}/pairs/{pair_id}` | one pair, with members, events and an explanation |
| `GET /api/runs/{id}/score-eval` | what the run scored against the old labels |
| `GET /api/runs/{id}/blocking-report` | pairs per blocking rule against the budget |
| `GET /api/runs/{id}/contradictions` | FALSE labels an exact key has overruled |
| `POST /api/runs/{id}/re-bucket` | move the accept and review lines, no rerun |
| `POST /api/runs/{id}/labels` | record human decisions on up to 500 pairs |
| `DELETE /api/runs/{id}/labels/{pair_id}` | withdraw the active decision on one pair |
| `GET /api/labels` | the label library, newest first |
| `GET /api/labels/export.csv` | every active label as a CSV |
| `POST /api/labels/import` | load labels from a CSV |

A run that has not reached stage 3 returns **404** with
`{"detail": "Run has no scored pairs yet"}`. A run id that does not exist
returns 404 with `{"detail": "Run not found"}`.

## Pair ids

`pair_id` is `<unit_id_l>|<unit_id_r>`, with the two unit ids compared as
strings so the smaller is always on the left. URL-encode the bar as `%7C`.

## `GET /api/runs/{id}/pairs`

### Query

| Name | Values | Default |
|---|---|---|
| `track` | `person`, `organisation` | every track |
| `bucket` | `accept`, `review`, `reject` | every bucket |
| `decided_by` | `score`, `model`, `import`, `human` | every one |
| `import` | `agrees`, `disagrees`, `unknown` | every one |
| `labelled` | `yes`, `no` | both shown |
| `held` | `hide`, `only` | both shown |
| `min_score`, `max_score` | 0 to 1, inclusive | no limit |
| `min_gbt`, `max_gbt` | 0 to 1, inclusive | no limit |
| `q` | case-insensitive substring of either side's name or unit id | none |
| `sort` | `score`, `priority`, `name`, `useful` | `score` |
| `order` | `asc`, `desc` | `desc` |
| `offset` | 0 or more | 0 |
| `limit` | 1 to 500 | 50 |

`model`, `min_gbt`, `max_gbt`, `sort=useful` and the `gbt_score` field on every
item belong to the GBT (`docs/MODEL.md`). They are set out in
`docs/MODEL_API.md`, which is the contract for them; on a run no model has
scored, `gbt_score` is `null`, the two `gbt` filters are ignored, and
`sort=useful` falls back to the Splink score.

`import` is the two units' imported entity ids compared: `agrees` when both
carry the same single old id, `disagrees` when both carry ids and they differ,
`unknown` when at least one side has no single id. A unit with two or more
distinct old ids counts as having none.

`labelled=yes` is the pairs a human has decided, which is the same set as
`decided_by=human`. `labelled=no` is the rest — the work queue.

`priority` sorts on the pair's summed priority columns (donations: the two
units' total donated). `name` sorts on the left unit's name, and units with no
name sort last either way. `pair_id` breaks every tie, so paging is stable.

A bad `sort`, `order`, `bucket`, `track`, `import` or `held` is **400** with the
message in `detail`. A `limit` above 500 is **422** from FastAPI.

### Response

`total` follows the filters. `counts` describe the whole run and ignore them, so
the chips above the list stay still while a search narrows it. `columns` labels
every column of a unit, exactly as `GET /api/runs/{id}/records` does.

Real response for `?track=person&bucket=review&sort=score&order=desc&limit=1`,
with the unit columns cut down for reading — a real `left` and `right` carry
every column of `units.parquet`:

```json
{
  "total": 1050,
  "offset": 0,
  "limit": 1,
  "items": [
    {
      "pair_id": "104841|99117",
      "track": "person",
      "match_probability": 0.917624442966025,
      "match_weight": 3.4776156003003527,
      "bucket": "review",
      "score_bucket": "review",
      "decided_by": "score",
      "import_disagrees": false,
      "import_agreement": "unknown",
      "held_group_id": null,
      "left": {
        "unit_id": "104841",
        "unit_size": 1,
        "held_group_id": null,
        "existing_entity_id": null,
        "existing_entity_ids": null,
        "n_existing_ids": 0,
        "name": "Hilary J Stone",
        "donor_status": "Individual",
        "parties": "Liberal Democrats",
        "first_year": 2026,
        "last_year": 2026,
        "n_donations": 1,
        "median_value": 5000.0,
        "modal_value": 5000.0,
        "top_values": "5,000",
        "track": "person",
        "name_clean": "HILARY J STONE",
        "title": null,
        "forename": "HILARY",
        "middle_names": "J",
        "surname": "STONE",
        "postcode_clean": null,
        "total_value": 5000.0
      },
      "right": {
        "unit_id": "99117",
        "unit_size": 1,
        "held_group_id": null,
        "existing_entity_id": null,
        "existing_entity_ids": null,
        "n_existing_ids": 0,
        "name": "Hilary A Stone",
        "donor_status": "Individual",
        "parties": "Liberal Democrats",
        "first_year": 2023,
        "last_year": 2025,
        "n_donations": 5,
        "median_value": 3000.0,
        "modal_value": 4000.0,
        "top_values": "4,000 | 3,500 | 3,000 | 2,500 | 2,000",
        "track": "person",
        "name_clean": "HILARY A STONE",
        "title": null,
        "forename": "HILARY",
        "middle_names": "A",
        "surname": "STONE",
        "postcode_clean": null,
        "total_value": 15000.0
      },
      "priority": { "total_value": 20000.0 },
      "label": null,
      "gammas": {
        "company_number_clean": null,
        "forename_canon": 3.0,
        "legal_form": null,
        "middle_names": 0.0,
        "name_core": null,
        "name_tokens_sorted": null,
        "postcode_clean": null,
        "surname": 3.0,
        "title": -1.0
      }
    }
  ],
  "counts": {
    "all": 28843,
    "accept": 26406,
    "review": 1268,
    "reject": 1169,
    "score": 4025,
    "import": 24808,
    "human": 10,
    "import_agrees": 24808,
    "import_disagrees": 1164,
    "import_unknown": 2871,
    "held": 22570,
    "labelled": 10,
    "unlabelled": 28833,
    "person": 25503,
    "organisation": 3340
  },
  "columns": [
    { "key": "unit_id", "label": "unit_id", "type": "text", "source": "cleaning" },
    { "key": "name", "label": "Donor", "type": "text", "source": "profile" },
    { "key": "total_value", "label": "Total", "type": "money", "source": "profile" }
  ],
  "priority_columns": ["total_value"]
}
```

Field notes:

- `match_probability` is Splink's score, 0 to 1. `match_weight` is the same
  evidence in bits: 0 means even odds, and each point doubles them.
- `bucket` is what the pair ends up as, after the overlays. `score_bucket` is
  what the score alone made of it. They differ when the imported labels or a
  human decided the pair, and the difference is worth showing.
- `decided_by` is `score`, `model`, `import` or `human`. A human decision always
  wins, then the import overlay, then the score — `model` in place of `score`
  where a graded GBT is what set the bucket (`docs/MODEL_API.md`).
- `label` is the active human decision on this pair, or `null`:
  `{is_match, reviewer, created_at, notes, evidence_url, provenance, held_out}`.
  `is_match` is `"TRUE"` or `"FALSE"`; `held_out` is `0` (Teaches — the model
  trains on it) or `1` (Tests — held aside and only graded on).
- `import_disagrees` is a flag, never a decision (LINKAGE.md, D11).
- `held_group_id` is set only when **both** units sit in the same held group, so
  the screen can fold those pairs into one block.
- `priority` sums each priority column over the two units.
- `gammas` is the agreement level each comparison reached: higher means closer,
  `-1` means one side was null, and `null` means the comparison belongs to the
  other track. `GET /pairs/{pair_id}` turns these into words.
- The two tracks share one file, so a person pair carries `null` for every
  organisation comparison.

## `GET /api/runs/{id}/pairs/{pair_id}`

The same item, plus each side's member records (at most 200 a side, ordered by
`record_id`), each side's **evidence rows** and a per-comparison explanation.

`events` is the profile's child rows for the two sides (D13b): for donations,
the individual donations behind each unit, newest first, at most 200 a side,
with `events_truncated` saying whether more exist. `event_columns` labels them
the way `columns` labels a unit. A profile that supplies no evidence rows
returns `events: {"left": [], "right": []}` and `event_columns: []`.

`explanation` reads the level labels out of `splink_model_<track>.json`.
`match_weight` there is the evidence that one comparison contributed, in bits:
`log2(m / u)`. A null level contributes nothing and reports `null`.

Real response for `/api/runs/run_2026_09_18a/pairs/104841%7C99117`, with the
repeated unit columns cut:

```json
{
  "pair_id": "104841|99117",
  "track": "person",
  "match_probability": 0.917624442966025,
  "match_weight": 3.4776156003003527,
  "bucket": "review",
  "score_bucket": "review",
  "decided_by": "score",
  "import_disagrees": false,
  "import_agreement": "unknown",
  "held_group_id": null,
  "left": {
    "unit_id": "104841",
    "unit_size": 1,
    "name": "Hilary J Stone",
    "median_value": 5000.0,
    "top_values": "5,000",
    "total_value": 5000.0,
    "members": [
      { "record_id": "104841", "name": "Hilary J Stone",
        "parties": "Liberal Democrats", "units": "London", "n_donations": 1,
        "total_value": 5000.0, "median_value": 5000.0, "modal_value": 5000.0,
        "n_distinct_values": 1, "share_round_1000": 1.0, "top_values": "5,000",
        "track": "person", "surname": "STONE", "middle_names": "J" }
    ],
    "members_truncated": false
  },
  "right": {
    "unit_id": "99117",
    "unit_size": 1,
    "name": "Hilary A Stone",
    "median_value": 3000.0,
    "top_values": "4,000 | 3,500 | 3,000 | 2,500 | 2,000",
    "total_value": 15000.0,
    "members": [
      { "record_id": "99117", "name": "Hilary A Stone",
        "parties": "Liberal Democrats", "n_donations": 5, "total_value": 15000.0,
        "median_value": 3000.0, "modal_value": 4000.0, "n_distinct_values": 5,
        "share_round_1000": 0.6, "track": "person", "surname": "STONE",
        "middle_names": "A" }
    ],
    "members_truncated": false
  },
  "priority": { "total_value": 20000.0 },
  "label": null,
  "events": {
    "left": [
      { "record_id": "104841", "date": "2026-04-30", "value": 5000.0,
        "recipient": "Liberal Democrats", "unit": "London",
        "donation_type": "Cash", "nature": null, "is_sponsorship": false,
        "reporting_period": "Q2 2026", "ec_ref": "C0839264" }
    ],
    "right": [
      { "record_id": "99117", "date": "2025-11-06", "value": 2500.0,
        "recipient": "Liberal Democrats", "unit": "Hazel Grove",
        "donation_type": "Cash", "nature": null, "is_sponsorship": false,
        "reporting_period": "Q4 2025", "ec_ref": "C0835132" }
    ],
    "left_truncated": false,
    "right_truncated": false
  },
  "event_columns": [
    { "key": "date", "label": "Date", "type": "text" },
    { "key": "value", "label": "Amount", "type": "money" },
    { "key": "recipient", "label": "Recipient", "type": "text" },
    { "key": "unit", "label": "Local unit", "type": "text" },
    { "key": "donation_type", "label": "Type", "type": "text" },
    { "key": "nature", "label": "Nature", "type": "text" },
    { "key": "is_sponsorship", "label": "Sponsorship", "type": "text" },
    { "key": "reporting_period", "label": "Reporting period", "type": "text" },
    { "key": "ec_ref", "label": "EC reference", "type": "text" }
  ],
  "gammas": { "forename_canon": 3.0, "middle_names": 0.0, "surname": 3.0,
              "title": -1.0 },
  "explanation": [
    { "column": "forename_canon", "gamma": 3.0,
      "label": "Exact match on forename_canon", "match_weight": 6.6538,
      "m_probability": 0.9682797961831835, "u_probability": 0.009616545209316187 },
    { "column": "middle_names", "gamma": 0.0, "label": "All other comparisons",
      "match_weight": -0.1976, "m_probability": 0.8393631446644406,
      "u_probability": 0.9625928318216416 },
    { "column": "surname", "gamma": 3.0, "label": "Exact match on surname",
      "match_weight": 9.3857, "m_probability": 0.5029747655413064,
      "u_probability": 0.000751939844812415 },
    { "column": "title", "gamma": -1.0, "label": "title is NULL",
      "match_weight": null, "m_probability": null, "u_probability": null }
  ],
  "columns": [
    { "key": "unit_id", "label": "unit_id", "type": "text", "source": "cleaning" }
  ]
}
```

A pair id with no bar in it is **400**. A well-formed id that is not in this run
is **404** with `{"detail": "No pair '9|99' in this run"}`.

## `GET /api/runs/{id}/pairs/histogram`

`track` is optional and `bins` defaults to 50 (maximum 200). Every series has
one entry per bin, in order; `edges` has `bins + 1` entries.

On a run a model has scored, the response also carries `score_column`,
`by_score_column` (the same series over `gbt_score`) and the six `model*` keys.
`docs/MODEL_API.md` sets those out.

Real response for `?track=person&bins=10`:

```json
{
  "track": "person",
  "bins": 10,
  "edges": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
  "total":     [316, 520, 1841, 42, 44, 479, 3546, 14059, 3248, 1408],
  "accept":    [17, 24, 1600, 22, 14, 67, 3363, 13878, 3034, 1344],
  "review":    [0, 0, 0, 0, 0, 412, 183, 181, 214, 60],
  "reject":    [299, 496, 241, 20, 30, 0, 0, 0, 0, 4],
  "agrees":    [17, 24, 1600, 22, 14, 67, 3363, 13878, 3034, 759],
  "disagrees": [104, 218, 100, 5, 15, 78, 17, 82, 58, 93],
  "unknown":   [195, 278, 141, 15, 15, 334, 166, 99, 156, 556]
}
```

`accept + review + reject` sums to `total` per bin, and so does
`agrees + disagrees + unknown`. The bucket series are the **final** buckets, so
an accepted bin can sit below the accept line when the import overlay put it
there.

## `GET /api/runs/{id}/score-eval`

The run's `score_eval.json`, served as written. It is the file the owner tunes
rules against. Shape, with the real figures:

```json
{
  "thresholds": { "candidate": 0.05, "review": 0.5, "high": 0.92 },
  "units_total": 22435,
  "pairs_total": 28843,
  "by_bucket": { "accept": 26401, "review": 1278, "reject": 1164 },
  "by_score_bucket": { "accept": 4102, "review": 21854, "reject": 2887 },
  "decided_by_import": 24808,
  "import_disagrees": 1164,
  "entities_after": 18744,
  "pair_precision": 0.969327,
  "pair_recall": 0.788848,
  "labelled_pairs": 2480929,
  "labelled_pairs_agreeing": 2404831,
  "manual_pairs": 3048536,
  "manual_pairs_found": 2404831,
  "conflicts": 318,
  "review": {
    "pairs": 1278,
    "score_bucket_pairs": 21854,
    "import_agrees": 20576,
    "import_disagrees": 338,
    "import_unknown": 940
  },
  "histogram": { "bins": 50, "edges": [], "agrees": [], "disagrees": [], "unknown": [] },
  "by_track": {
    "person": {
      "units": 13310,
      "pairs": 25503,
      "by_bucket": { "accept": 23358, "review": 1059, "reject": 1086 },
      "by_score_bucket": { "accept": 1288, "review": 21452, "reject": 2763 },
      "decided_by_import": 22778,
      "import_disagrees": 770,
      "review": { "pairs": 1059, "score_bucket_pairs": 21452,
                  "import_agrees": 20393, "import_disagrees": 253,
                  "import_unknown": 806 },
      "histogram": { "bins": 50 },
      "pair_precision": 0.990402,
      "pair_recall": 0.899622,
      "score_only": { "pair_precision": 0.987201, "pair_recall": 0.623617 },
      "with_human": { "pair_precision": 0.990358, "pair_recall": 0.899662 },
      "exact_only": { "pair_precision": 0.993304, "pair_recall": 0.541521 }
    },
    "organisation": { "...": "the same shape" }
  },
  "score_only": {
    "entities_after": 19650,
    "pair_precision": 0.970064,
    "pair_recall": 0.772776,
    "by_track": {}
  },
  "with_human": {
    "entities_after": 18739,
    "pair_precision": 0.969325,
    "pair_recall": 0.788849,
    "labels_applied": 10,
    "labels_true": 5,
    "labels_false": 5,
    "by_bucket": { "accept": 26406, "review": 1268, "reject": 1169 },
    "by_track": {}
  },
  "exact_only": {
    "entities_after": 22435,
    "pair_precision": 0.998394,
    "pair_recall": 0.664288,
    "by_track": {}
  }
}
```

Four sets of figures, all measured the same way, so a screen can show them side
by side:

- the top level is the exact groups **plus every accepted pair**, which is the
  score and the import overlay together;
- `score_only` leaves out the pairs the import overlay accepted. Those were
  accepted because the two units already carry the same old entity id, so they
  cannot be evidence that the scorer found anything. This is the honest number
  for tuning;
- `with_human` is the top level with the human decisions laid on top: TRUE
  labels joined up, FALSE labels pulled apart. It is what the run would publish
  today, and it equals the top level exactly while nobody has labelled anything;
- `exact_only` is the match keys on their own — the same figures as
  `GET /api/runs/{id}/exact-eval`.

`pair_precision` and `pair_recall` are `null`, never 0, when there is nothing to
divide.

## `GET /api/runs/{id}/blocking-report`

The run's `blocking_report.json`, written before Splink starts:

```json
{
  "memory_limit": "6GB",
  "tracks": {
    "person": {
      "units": 13310,
      "budget": 5000000,
      "total": 127611,
      "over_budget": false,
      "rules": [
        { "id": "pb1", "description": "Same surname and same first initial.",
          "sql": "l.surname = r.surname AND l.forename_initial = r.forename_initial",
          "pairs": 26958 },
        { "id": "pb2", "description": "Surname sounds the same and the forename matches.",
          "sql": "l.surname_metaphone = r.surname_metaphone AND l.forename_canon = r.forename_canon",
          "pairs": 23915 },
        { "id": "pb3", "description": "Same forename, surname starts with the same letter.",
          "sql": "l.forename_canon = r.forename_canon AND substr(l.surname, 1, 1) = substr(r.surname, 1, 1)",
          "pairs": 76738 }
      ]
    },
    "organisation": {
      "units": 9125,
      "budget": 5000000,
      "total": 11197,
      "over_budget": false,
      "rules": [
        { "id": "ob1", "description": "Same cleaned postcode.",
          "sql": "l.postcode_clean = r.postcode_clean", "pairs": 6033 },
        { "id": "ob2", "description": "Same padded company number.",
          "sql": "l.company_number_clean = r.company_number_clean", "pairs": 1209 },
        { "id": "ob3", "description": "Same first word of the name, same postcode district.",
          "sql": "l.name_first_token = r.name_first_token AND l.postcode_district = r.postcode_district",
          "pairs": 3955 }
      ]
    }
  }
}
```

When a track is over budget the run **fails** and nothing is scored. The report
is still written, and the run row carries the same thing as structured detail:

```json
{
  "kind": "blocking_budget",
  "track": "person",
  "budget": 5000000,
  "total": 88521395,
  "rules": [{ "id": "pb1", "description": "...", "pairs": 88521395 }]
}
```

`GET /api/runs/{id}` returns that under `error_detail`, next to an
`error_message` naming the worst rule — the same pattern as
`unmapped_lookup_values`. The fix is to tighten the rule or raise `max_pairs`
on the Thresholds & Splink tab.

## `POST /api/runs/{id}/re-bucket`

Body: `{"threshold_high": 0.98, "threshold_review": 0.5}`. Either may be
omitted, and then the run's current line stands.

Stage 3 keeps every pair down to the candidate floor, so moving a line is a
re-read of `pairs.parquet` and not a rerun. The run's thresholds, its stored
counts and `score_eval.json` all move with it. The import overlay is applied
again afterwards, so a pair the labels accept stays accepted whatever the line.

Returns `{"ok": true, "counts": {...}}`, where `counts` is the run's counts in
the same camelCase shape `GET /api/runs/{id}` returns. A review line above the
accept line is **400**.

## Human labels

A label is a statement about two **records**, not about a run (D10). It is kept
in the `pair_labels` table, append-only: a new decision on the same pair
deactivates the old row and points its `superseded_by` at the new one, so who
said what and when is never lost. `record_id_a` is always the smaller of the two
ids compared as strings, so (a, b) and (b, a) are one label.

### Where the overlay is applied

`pairs.parquet` holds the **score and the import overlay only**. The human
overlay is joined on at read time, in the DuckDB query, from the active labels:
a label write touches the database, the run's counts and `score_eval.json`, and
never rewrites the parquet. Labelling one pair therefore costs one insert and a
recount, not a 29,457-row rewrite — which matters when PSC makes that number
millions.

Because a label names records and the file names units, the join goes through
`unit_members`: a label applies to whichever two units hold those two records in
this run.

### `POST /api/runs/{run_id}/labels`

```json
{
  "labels": [
    { "pair_id": "104841|99117", "is_match": "TRUE",
      "notes": "Same address on the 2025 and 2026 returns",
      "evidence_url": "https://www.example.org/report" },
    { "pair_id": "85232|104482", "is_match": "FALSE" }
  ],
  "provenance": "manual"
}
```

At most 500 labels per request (**422** above that, the same batch size roe_ui
uses). `provenance` is `manual` or `bulk_range`, and defaults to `manual`.
`is_match` is `TRUE` or `FALSE`, any case. `notes` and `evidence_url` are
optional; `evidence_url` must be empty or an `http(s)` URL, or the request is
**400**.

`pair_id` may name a pair in either order and does not have to exist in
`pairs.parquet` — a reviewer may decide two units the scorer never put together
— but both unit ids must exist in this run's `units.parquet`, or the row is
**400**. A decision on a pair the file does not hold is stored and counted, and
it moves `with_human`, but it only appears in the pairs list once a rerun forces
the pair in.

Everything else is filled in for you: `reviewer` from the session user, `track`,
`name_a`, `name_b` from the run's units, and `run_id` and `config_version` from
the run. `held_out` is `0` for a new label and inherited when one supersedes an
earlier label, exactly as roe_ui does it, so re-deciding a pair never drops an
entity out of the frozen evaluation set. Each write is an audit event.

```json
{
  "saved": 2,
  "superseded": 0,
  "counts": { "pairsScored": 28843, "pairsAccept": 26406, "pairsReview": 1268,
              "pairsReject": 1169, "labelsTrue": 5, "labelsFalse": 5,
              "labelsTotal": 10, "labelContradictions": 0,
              "entitiesAfterHuman": 18739, "humanPairPrecision": 0.969325,
              "humanPairRecall": 0.788849 }
}
```

`counts` is the run's counts in the same camelCase shape `GET /api/runs/{id}`
returns, already refreshed.

### `DELETE /api/runs/{run_id}/labels/{pair_id}`

Withdraws the active decision on that pair. Append-only: the row stays with
`active = 0`, so the history reads the same afterwards. **404** when there is no
active label on that pair.

```json
{
  "pair_id": "104841|99117",
  "bucket": "review",
  "decided_by": "score",
  "counts": { "...": "the run's refreshed counts" }
}
```

`bucket` and `decided_by` are what the pair falls back to once the human
decision is gone — the score plus the import overlay.

### `GET /api/labels`

The library, server-paged.

| Name | Values | Default |
|---|---|---|
| `track` | `person`, `organisation` | every track |
| `is_match` | `TRUE`, `FALSE` | both |
| `provenance` | `manual`, `bulk_range`, `llm`, `import`, `cluster_merge`, `cluster_split` | every one |
| `reviewer` | exact name | every reviewer |
| `held_out` | `0`, `1` | both |
| `decision_id` | one group decision's labels | every label |
| `created_from`, `created_to` | ISO date or timestamp, **inclusive** | no limit |
| `q` | substring of either name, either record id, the notes or the reviewer | none |
| `active` | `1` live, `0` the superseded history | `1` |
| `sort` | `created_at`, `name`, `reviewer`, `is_match`, `provenance` | `created_at` |
| `order` | `asc`, `desc` | `desc` |
| `group_by` | `decision` | off |
| `offset`, `limit` | `limit` 1 to 500 | 0, 50 |

`sort` is checked against that list and refused otherwise — **400** — because
the value reaches the query. The row id breaks every tie, so paging never
repeats or skips a row however many labels share a timestamp.

A bare `created_from` date means from its first moment and a bare `created_to`
date to its last, so `created_from=2026-09-01&created_to=2026-09-01` is that one
day. A full timestamp is compared as given.

```json
{
  "total": 10,
  "offset": 0,
  "limit": 50,
  "items": [
    { "id": 10, "record_id_a": "104482", "record_id_b": "85232", "track": "person",
      "is_match": "FALSE", "provenance": "manual", "held_out": 0,
      "reviewer": "Tom", "notes": null,
      "evidence_url": "https://example.org/evidence",
      "name_a": "James E Sharp", "name_b": "Mr James E H Sharp",
      "run_id": "run_2026_09_18a", "config_version": 2,
      "created_at": "2026-09-18T13:50:26.549701+00:00",
      "active": 1, "superseded_by": null }
  ],
  "counts": { "active": 10, "true": 5, "false": 5, "held_out": 0,
              "person": 9, "organisation": 1,
              "manual": 10, "bulk_range": 0, "llm": 0, "import": 0 },
  "grouped": false
}
```

`counts` describe the whole library and ignore the filters. `grouped` says which
shape the items are in.

### `GET /api/labels?group_by=decision`

One cluster decision can write a thousand labels, and a reviewer wants to see
the decision rather than the star it produced. In this mode the labels sharing a
`decision_id` come back as one item, and a label with no decision stays as it
was. `total`, `offset` and `limit` are counted in **rows of this list**, so
paging is over decisions, not labels.

```json
{
  "total": 2,
  "offset": 0,
  "limit": 50,
  "grouped": true,
  "items": [
    { "decision_id": "d_5f2c1f0a", "decision_scope": "C-102719", "kind": "merge",
      "provenance": "cluster_merge", "n_labels": 25, "n_true": 25, "n_false": 0,
      "names": ["John James", "John E James", "John Edward James", "J James"],
      "reviewer": "Tom", "created_at": "2026-09-18T13:50:26.524783+00:00",
      "notes": "One donor, three spellings",
      "evidence_url": "https://www.example.org/report" },
    { "decision_id": null, "decision_scope": null, "kind": null,
      "provenance": "manual", "n_labels": 1, "n_true": 0, "n_false": 1,
      "names": ["James E Sharp", "Mr James E H Sharp"],
      "reviewer": "Tom", "created_at": "2026-09-18T13:50:26.549701+00:00",
      "notes": null, "evidence_url": null }
  ],
  "counts": { "...": "the same label counts as the flat list" }
}
```

- `kind` is `merge` or `split` for a group decision, and `null` for an ordinary
  label. An **attribute** decision writes no labels at all, so it does not
  appear in this library; the cluster's own `decision` field carries it
  (`docs/ENTITIES_API.md`).
- `names` is up to four distinct member names, in the order the labels were
  written.
- `counts` are still labels, not decisions, so the two modes report the same
  totals for the library as a whole.
- To open one, ask the flat list for it: `GET /api/labels?decision_id=d_5f2c1f0a`
  returns its 25 member labels.

### `GET /api/labels/export.csv`

The labels the list would show, as `text/csv` with
`Content-Disposition: attachment; filename=pair_labels.csv`. Columns:
`record_id_a, record_id_b, track, is_match, provenance, held_out, reviewer,
notes, evidence_url, name_a, name_b, run_id, config_version, created_at,
decision_id, decision_scope`.

It takes **exactly the same filters and sort** as the list, so a download is
what was on screen rather than everything. With none it is the whole active
library, as before. `group_by` does not apply: the file is always one row per
label.

The rows are streamed, so a library of any size costs one row of memory. An
invalid `sort` is a **400** before the first byte, never a half-written file.

### `POST /api/labels/import`

Body `{"csv": "<the file's text>", "run_id": "<optional>"}`. The CSV needs
`record_id_a`, `record_id_b` and `is_match`; `notes`, `evidence_url` and
`provenance` are optional and everything else is ignored. Rows are applied
through the same append-only path as a normal label write, so importing over an
existing library supersedes rather than overwrites.

Every row's two ids must exist in the latest complete run's records, and a row
whose ids are unknown is **rejected and reported**, never quietly dropped:

```json
{
  "imported": 8,
  "superseded": 3,
  "rejected": [
    { "row": 4, "record_id_a": "999999", "record_id_b": "12",
      "reason": "record_id_a '999999' is not in run run_2026_09_18a" },
    { "row": 7, "record_id_a": "12", "record_id_b": "12",
      "reason": "a label needs two different records" }
  ],
  "run_id": "run_2026_09_18a"
}
```

## `GET /api/runs/{id}/contradictions`

A **contradiction** is a FALSE label whose two records an exact match key has
since merged into one unit: a rule now says "same" where a human said "not the
same". Stage 3 writes `contradictions.json`; this serves it.

```json
{
  "total": 1,
  "items": [
    { "record_id_a": "12", "record_id_b": "34", "unit_id": "12",
      "group_id": "X-12", "key_ids": ["k3"], "track": "person",
      "name_a": "John Smith", "name_b": "John Smith",
      "label": { "is_match": "FALSE", "reviewer": "Tom",
                 "created_at": "2026-09-18T14:41:02+00:00",
                 "notes": "Different people, same name",
                 "evidence_url": null, "provenance": "manual", "held_out": 0 } }
  ]
}
```

`key_ids` names the match keys that did the merging, so the fix is one edit away.
An unscored run returns **404**.

## How a label survives a rerun

A label names two records, so a later run applies it to whichever units now hold
them. Stage 3 sorts every active label into one of three outcomes and reports
the counts:

| Outcome | When | What happens |
|---|---|---|
| satisfied | TRUE, and both records are now in one unit | counted, no pair needed — the exact keys already did it |
| contradiction | FALSE, and both records are now in one unit | listed in `contradictions.json` and counted |
| applied | the records are in two different units | the pair carries the decision |

An applied label whose pair blocking never generated, or whose score fell below
the candidate floor, is **forced into `pairs.parquet`** with
`match_probability: null`, `match_weight: null` and `score_bucket: "reject"`, so
a human decision is never lost behind a blocking rule. Those rows are visible
through the pairs API like any other; sort by score puts them last.

## Evidence rows elsewhere

`GET /api/profile` gains `event_columns`, the same list the pair detail returns,
so a screen can lay out an evidence table before it has opened a pair. It is
`[]` for a profile with no evidence rows.

`GET /api/runs/{id}/exact-groups/{group_id}?events=1` adds `events` and
`event_columns` to a group's members, in the same shape: the rows for every
member record, newest first, capped at 200, with `events_truncated`. Without
`events=1` the response is unchanged, because the group list does not need them.

## Evidence focus

`GET /api/profile` also carries `evidence_focus`: what a reviewer actually
checks, by kind of record (D13c). It is `[]` for a profile that judges every
record the same way.

```json
{ "id": "individual", "label": "Individual",
  "when": [ { "column": "donor_status_std", "op": "in", "values": ["Individual"] } ],
  "record_columns": ["name"],
  "event_columns": ["recipient", "unit", "value", "date", "donation_type"] }
```

The entries are tried in order and the first whose conditions all hold decides
the focus, exactly as a track rule does. The last entry has an empty `when` and
catches everything else. Two entries may share an `id`: donations tests the
standardised donor status first and the raw one second, which is one focus
reached two ways.

`record_columns` name `display_columns` keys and `event_columns` name
`event_columns` keys, so a screen can show those fields at the top of a pair, a
cluster or an exact group without knowing anything about donations.

The conditions use only `equals`, `in`, `is_null`, `not_null` and `starts_with`
— the subset a browser can evaluate in a line of JavaScript — because the
review screen picks the focus for each side itself. Anything server-side that
needs the same answer calls `Profile.evidence_focus_for(row)`, which is the one
implementation of the rule.

## Run counts the score stage adds

`GET /api/runs/{id}` and `GET /api/runs` carry these inside `counts`:

| Key | Meaning |
|---|---|
| `unitsTotal`, `unitsPerson`, `unitsOrganisation` | units the exact keys left |
| `pairsScored` | rows in `pairs.parquet` |
| `pairsAccept`, `pairsReview`, `pairsReject` | final buckets |
| `pairsDecidedByImport` | accepted because the old ids agree |
| `pairsImportDisagrees` | flagged because the old ids differ |
| `entitiesAfterScore` | components over units once the accepts are joined up |
| `scorePairPrecision`, `scorePairRecall` | exact groups plus accepted pairs, or `null` |
| `labelsTotal`, `labelsTrue`, `labelsFalse` | active human labels this run could apply |
| `labelsSatisfied` | TRUE labels whose two records the exact keys already merged |
| `labelsForced` | labelled pairs the scorer never produced, added to the file |
| `labelContradictions` | FALSE labels an exact key has overruled |
| `entitiesAfterHuman` | entities once the human decisions are applied too |
| `humanPairPrecision`, `humanPairRecall` | the `with_human` figure set, or `null` |
| `hasPairs` | true once the score stage has run |
| `hasUnits` | true once the units are built |
| `hasLabels` | true once a human has decided at least one pair in this run |

Every count defaults to 0, so read `hasPairs` rather than testing `pairsScored`
for zero: a run with no pairs and a run that never scored look the same by value.

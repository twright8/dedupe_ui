# Ruleset — the user-editable rules document

Implements decision D8 in `DESIGN.md`. A ruleset is one JSON document. Each config version stores one ruleset plus `linkage_settings`. Users edit it through forms on the Config screen and never as raw JSON.

The engine that applies a ruleset lives in `backend/app/rules/`. The pipeline and every preview call the same engine functions. A preview must never re-implement a rule.

Matching is case-insensitive everywhere. A null input stays null unless a step says otherwise. An empty string after a step becomes null.

## Document shape

```json
{
  "schema": 1,
  "token_lists": { "<name>": { "description": "", "tokens": ["MR", "MRS"] } },
  "lookups":     { "<name>": { "description": "", "fallback": "passthrough", "rows": [{ "raw": "BILL", "canonical": "WILLIAM" }] } },
  "track_rules": [ TrackRule ],
  "default_track": "organisation",
  "cleaning":    { "person": [ Step ], "organisation": [ Step ] },
  "match_keys":  [ MatchKey ],
  "vetoes":      []
}
```

`vetoes` is reserved for the scoring slice and stays empty until then.

## Token lists and lookups

A token list is a named set of upper-case tokens. Tokens may contain spaces ("RT HON").

A lookup maps `raw` to `canonical`. `fallback` says what happens when the value is not in the table: `passthrough` keeps the input, `null` returns null, `error` stops the run and reports the unmapped values.

## Track rules

Rules are tried in order. The first rule whose conditions all hold sets the track. If none holds, `default_track` applies. Conditions read the raw profile columns, before cleaning.

```json
{ "id": "t2", "description": "Titled names are people",
  "when": [ { "column": "donor_status", "op": "in", "values": ["Impermissible Donor", "Other"] },
            { "column": "name", "op": "starts_with_token", "lists": ["person_titles"] } ],
  "track": "person" }
```

Condition operators:

| op | arguments | true when |
|---|---|---|
| `equals`, `not_equals` | `value` | the whole value matches |
| `in`, `not_in` | `values` | the whole value is in the list |
| `is_null`, `not_null` | none | the value is or is not null |
| `starts_with_token`, `ends_with_token`, `contains_token` | `lists` | a token from any named list appears at that position, on word boundaries |
| `matches` | `pattern` | the regular expression finds a match |

## Cleaning steps

Each track has an ordered list of steps. A step reads `source` and writes `target`. A target may be a new column or an existing one. Later steps may read earlier targets. Raw profile columns are never overwritten: a step whose target is a raw column name is a validation error.

```json
{ "id": "p3", "description": "Remove titles", "op": "strip_tokens",
  "source": "name_clean", "target": "name_clean",
  "lists": ["person_titles"], "position": "leading", "repeat": true, "keep_as": "title" }
```

| op | arguments | effect |
|---|---|---|
| `copy` | | copy source to target |
| `upper`, `lower`, `trim`, `collapse_spaces`, `accent_fold` | | the obvious text change |
| `strip_punctuation` | `keep` (characters to keep, default `" "`) | remove every character that is not a letter, a digit, or in `keep` |
| `regex_replace` | `pattern`, `replacement` | `re.sub` |
| `strip_tokens` | `lists`, `position` (`leading`, `trailing`, `anywhere`), `repeat` (default true), `keep_as` (optional column), `keep_one` (default true) | remove list tokens at that position. `keep_as` stores what was removed. `keep_one` never removes the last remaining token |
| `nullify` | `lists` | null when the whole value equals a token |
| `lookup` | `table`, `scope` (`value` or `tokens`) | map the whole value, or each token, through a lookup |
| `function` | `name`, plus that function's arguments | call a library function |

## Function library

Fixed in code at `backend/app/rules/functions.py`. `GET /api/config/functions` lists them for the UI. A function writes `target`, except multi-output functions, which write the fixed columns named below.

| name | output | what it does |
|---|---|---|
| `parse_person_name` | `forename`, `middle_names`, `surname`, `forename_initial` | split a cleaned, title-free name. One token: surname only. "SURNAME, FORENAME" is handled |
| `normalise_postcode` | target | "le11fb" to "LE1 1FB". Not a UK postcode shape: null |
| `postcode_district` | target | "LE1 1FB" to "LE1" |
| `normalise_company_number` | target | strip non-alphanumerics, upper-case, left-pad pure digits to 8. Fewer than 2 characters, or no digit: null |
| `metaphone`, `soundex` | target | phonetic key of the source |
| `sorted_tokens` | target | distinct tokens, sorted, joined by a space |
| `first_token`, `last_token`, `initials` | target | token helpers |

In `GET /api/config/functions`, a single-output function reports `outputs: ["target"]`; a multi-output one reports its fixed column names. A step calling a multi-output function needs no `target`, and one calling any other function does.

All ops and functions run on the distinct values of the source column and map the results back. That keeps a 16-million-row PSC run practical.

## Match keys

```json
{ "id": "k1", "name": "Company number", "track": "organisation", "tier": 1,
  "columns": ["company_number_clean"], "allow_null": false,
  "applies_when": "always",
  "guards": { "blocklists": ["placeholder_numbers"],
              "max_group_size": 50,
              "max_distinct": { "column": "name_core", "count": 3 },
              "require_any_equal": ["postcode_clean", "name_core"] },
  "on_guard_fail": "review" }
```

A key needs at least one column, a unique `id`, and a `tier` that is a whole number of 1 or more. Every column it names — in `columns`, in `max_distinct.column`, in `require_any_equal` — must exist for that key's track, as a raw column or as something that track's cleaning writes. `blocklists` names token lists. `max_group_size` is 2 or more and `max_distinct.count` is 1 or more.

- Records with the same values in every key column form a candidate group. A group of one record is not a group.
- Keys run in `tier` order within a track; keys of the same tier run in document order.
- A record is eligible for a key when it is in the key's track; with `allow_null` false, every key column is non-null; no key-column value is in a blocklist; and, with `applies_when: "no_earlier_key"`, it was not eligible for any key of an earlier tier of that track. Keys of the same tier are not earlier than each other. Matching is case-insensitive and a blank counts as missing.
- `blocklists`: a key value found in any named token list makes that record ineligible.
- Guards run in this order: `max_group_size`, then `max_distinct` (distinct non-null values of that column inside the group). A group over either limit is not merged.
- `on_guard_fail` is `review` (the group is `held` for a human, with a `guard` reason such as `max_distinct:name_core=7>3` or `max_group_size:12>10`) or `skip` (the records are left unmerged, silently).
- `require_any_equal`: inside a group that passed the guards, records are joined only when they share a non-null value in at least one of these columns. The group becomes those connected parts, so A joined to B on the postcode and B joined to C on the name is one part. A part of one record is left unmerged.
- Merged groups from different keys that share a record are united into one. Held groups are never united with anything.

Stage 2 writes one row per record per group it belongs to: `record_id`, `group_id`, `track`, `status` (`merged` or `held`), `key_ids` (the contributing key ids joined with `|`, in document order), and `guard` (null unless held). A record appears at most once as `merged`, and may also appear in any number of held groups.

A `group_id` is deterministic and stable for the same membership: `X-` plus the smallest member `record_id` for a merged group, and `H-<key id>-` plus the smallest member `record_id` for a held one. Record ids compare as text.

The stage reports, per key, `{id, name, track, tier, eligible_records, groups, records, held_groups, held_records, blocked_values}` — where `groups` and `records` are what that key alone merged, before the union across keys — plus an overall block `{records, merged_groups, merged_records, held_groups, held_records, entities_after}`, where `entities_after` is `records − (merged_records − merged_groups)`.

### Scoring the keys against the existing labels

Every record carries `existing_entity_id`, null when nobody has reviewed it. `exact_eval.json` measures each merged group against those decisions:

| Field | Meaning |
|---|---|
| `agreement` | `conflict` when two or more distinct existing ids meet in the group; else `new` when no member is labelled; else `extends` when the group brings an unlabelled member to a settled id; else `consistent` |
| `pair_precision` | of the labelled pairs inside merged groups, the share the reviewers had already put together |
| `pair_recall` | of the pairs of records sharing an `existing_entity_id`, the share that also share a merged group |
| `records_attached` | unlabelled members inside `extends` groups — what a run would newly give an existing id |
| `conflicts` | how many merged groups are `conflict` |

Both scores are reported overall and per track under `by_track`, and are null rather than zero when there is nothing to divide. They are counted from group sizes with n·(n−1)/2; nothing walks a pair.

The donations labels have a known bias (`DESIGN.md`, D13): reviewers merged 99.6% of identical-name pairs of individuals, so they cannot show when two people with the same name are different. Precision here is an agreement figure, not an accuracy figure.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/config/current`, `GET /api/config/versions/{v}` | `{version, created_at, created_by, note, ruleset, linkage_settings}`. Null before the first save |
| `POST /api/config` | body `{ruleset, linkage_settings, note}`. Validates first. 422 with `{"detail": {"errors": [{path, message}]}}` on failure |
| `POST /api/config/validate` | body `{ruleset}`. Returns `{errors: [...]}` without saving |
| `GET /api/config/functions` | the function library: `[{name, description, outputs, args, example}]` |
| `GET /api/config/columns?track=`, `POST /api/config/columns` | `{raw: [{key, label}], steps: [{step_id, targets: [..]}], all: [..]}`. The GET uses the saved ruleset; the POST takes `{ruleset, track}` for a draft |
| `POST /api/config/preview-cleaning` | body `{ruleset?, track, values?: [{column: value}], run_id?, q?, n?}`. Returns `{track, samples: [{input, steps, output}]}` |
| `POST /api/config/preview-tracks` | body `{ruleset?, run_id}`. Returns `{total, tracks: {person, organisation}, rules: [...]}` |
| `POST /api/config/lookups/{name}/rows` | body `{rows: [{raw, canonical}], note}`. Saves a new version with the rows added. Returns `{version, added}`; `added: 0` saves nothing |
| `GET /api/config/diff/{v1}/{v2}` | one entry per ruleset section plus `linkage_settings` |
| `POST /api/config/preview-keys` | body `{ruleset?, run_id}`. Returns `{keys, overall, eval, baseline}` |
| `GET /api/pipeline/stages` | the stages that exist today: `[{key, label, description}]` |
| `GET /api/runs/{id}/exact-groups` | one page of the groups stage 2 made |
| `GET /api/runs/{id}/exact-groups/{group_id}` | one group plus its member records |
| `GET /api/runs/{id}/exact-eval` | the run's `exact_eval.json`: `{keys, overall, eval}` |

`preview-keys` reruns the whole chain in memory on a run's `records_raw.parquet` with the DRAFT ruleset — track assignment, cleaning, keys, evaluation — so editing a cleaning step shows its effect on the groups. An invalid draft returns 422 in the same shape as `POST /api/config`. A run of more than 1,000,000 records returns 400; previewing a sample of one is a later slice. Each entry of `keys` is that key's stats plus `examples`: up to 8 groups, largest first, as `{group_id, size, status, guard, names}`. `eval` carries `{pair_precision, pair_recall, conflicts, by_agreement}`. `baseline` is the `{overall, eval}` the run itself saved, so the screen can show a delta, and is null for a run made before stage 2 existed.

`exact-groups` takes `track`, `key`, `status` (`merged`, `held`), `agreement` (`consistent`, `conflict`, `extends`, `new`), `q`, `sort` (`size`, `priority`, `name`), `order`, `offset` and `limit` (default 50, maximum 200). `q` is a case-insensitive substring of any member's name or record id, or of the group id. It returns `{total, offset, limit, items, counts}`, where `total` follows the filters and `counts` — `{merged, held, consistent, conflict, extends, new}` — describe the whole run and ignore them. Each item is:

```json
{ "group_id": "X-1042", "track": "organisation", "status": "merged", "guard": null,
  "key_ids": ["k1", "k2"], "size": 4, "n_labelled": 3, "existing_ids": ["600"],
  "agreement": "extends", "names": ["Acme Ltd", "Acme Limited"],
  "priority": { "total_value": 12500.0 } }
```

`existing_ids` shows up to 5 and `names` up to 4 distinct member names. `priority` sums the profile's `priority_columns` over the members, and is what `sort=priority` orders on. A held group has `agreement: null`. The single-group endpoint adds `members` — the full record rows, capped at 500 — and `members_truncated`.

`preview-cleaning`: `n` defaults to 8 and is capped at 50. With `run_id`, the sample comes from that run's raw records, taking the track the DRAFT ruleset assigns and filtering on `q` as a case-insensitive substring of the name. Each sample is:

```json
{ "input": { "name": "Mr John Smith", "record_id": "1" },
  "steps": [ { "id": "p1", "op": "upper", "description": "Upper-case the name",
               "source": "name", "before": "Mr John Smith",
               "outputs": { "name_clean": "MR JOHN SMITH" },
               "changed": true, "error": null } ],
  "output": { "name_clean": "JOHN SMITH", "surname": "SMITH" } }
```

`input` carries the raw columns the steps read, plus `record_id` when the sample came from a run. `changed` compares the step's target against `before`. `error` is the message from a draft rule that could not run — the step is skipped and the rest still run, so a bad regex never fails the request. A lookup with `fallback: "error"` is different: it is a data problem, not a typo, and returns 422 with `{"detail": {"kind": "unmapped_lookup_values", "table", "values"}}`.

`preview-tracks` returns one entry per rule, in order, with a final entry whose id is `"default"`:

```json
{ "id": "t2", "description": "...", "track": "person", "hits": 137,
  "examples": [ { "record_id": "1", "name": "Mr A Smith", "donor_status": "Other" } ] }
```

`hits` counts the records a rule actually decided, so the hits sum to `total`. Examples carry `record_id`, `name` and the columns that rule's conditions read, up to 10.

`diff` reports one entry per section — `token_lists`, `lookups`, `track_rules`, `default_track`, `cleaning.person`, `cleaning.organisation`, `match_keys`, `vetoes`, `linkage_settings`. A section of items reports `{changed, added, removed, modified}`, each a list of ids (or names, for `token_lists` and `lookups`). `default_track` and `linkage_settings` are compared whole and report `{changed, v1, v2}`.

`GET /api/runs/{id}/records` adds `columns: [{key, label, type, source}]`, in frame order. `source` is `profile` for a column the profile declares (with its label and type) and `cleaning` for one a rule wrote, whose label is its own name and whose type is `text`.

A run that fails on an unmapped lookup stores `error_detail` as `{kind: "unmapped_lookup_values", table, values}`, which is what `POST /api/config/lookups/{name}/rows` is there to fix.

## Pipeline files in a run folder

| File | Written by | Holds |
|---|---|---|
| `records_raw.parquet` | stage 0, load | the profile's records, no track |
| `records.parquet` | stage 1, clean | raw columns, `track`, and every cleaning target |
| `config/ruleset.json` | the runner | the ruleset this run used |
| `config/linkage_settings.json` | the runner | the settings this run used |
| `exact_groups.parquet` | stage 2, exact keys | `record_id`, `group_id`, `track`, `status`, `key_ids`, `guard` |
| `exact_eval.json` | stage 2, exact keys | `{keys, overall, eval}` — the per-key stats, the totals, and the label scores |

A column only one track produces is null for the records of the other track. The two tracks share one frame, so the records API and every later stage read one file.

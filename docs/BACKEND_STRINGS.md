# Backend strings that reach the screen with a retired term

Collected while applying `docs/GLOSSARY.md` to the frontend. Canonical terms
are in `frontend/src/glossary.js` and, for the backend, in
`backend/app/vocabulary.py`; the two are checked against each other by
`backend/tests/test_vocabulary.py`.

## Status, 2026-09-21

Everything below is applied, except where this note says otherwise.
`backend/tests/test_backend_strings.py` is the check that keeps it applied: it
fails if a retired word comes back in the stage list, in a default rule
description, in a veto reason, in a guard reason, in a pair explanation or in a
validation message.

Three things were deliberately left as they are:

- **`rules/linkage.py`'s two validation messages** (`on_oversize must be one
  of …`, `splink_function must be one of …`). Another agent is working in that
  file, so it was not touched. They should become
  `vocabulary.choice_error("on_oversize", value, ON_OVERSIZE)` and
  `vocabulary.choice_error("splink_function", value, …)`, which is a two-line
  change once that work lands.
- **`/api/labels/export`'s CSV column names** (`is_match`, `held_out`,
  `provenance`). That file is re-imported by the same tool, so its headers are
  a machine contract, not prose.
- **`exact-eval keys[].name` falling back to the key id.** A key the user did
  not name has no other name, and the id is what they typed.

## 1. Pipeline stage labels — `GET /api/pipeline/stages`

`backend/app/pipeline/dedupe/__init__.py` `STAGES`. Printed on the New run card
and used as the one source of stage names.

| Field | Text | Problem |
|---|---|---|
| `label` | `"Exact keys"` | "exact key" is retired. The rule is a **match key**. |
| `exact.description` | "Group records that agree on a match key, hold the groups a guard stops, and score the result against the **existing labels**" | "existing labels" → **earlier grouping**. |
| `score.description` | "Compare the units the **exact keys** left — one per **merged group**, one per other record" | → match keys; → **exact group**. |
| `cluster.description` | "would merge groups the **earlier manual work** kept apart" | → **earlier grouping**. |

The list also has no model stage and no derived-column stage, so it does not
match How it works.

## 2. Model warnings and notes — `GET /api/models/{track}`

- `backend/app/model/train.py:680` — `"cold start: {n} human labels, and …"`.
  "cold start" → **new model**; "human" → **reviewer**.
- `train.py:702` — `"Trained on imported labels only — …"` → **earlier grouping**.
- `train.py:49` — `"The imported labels were made mostly on the name. They cannot teach the model …"` → **earlier grouping**.
- `warnings[].code` is literally `"cold_start"`; ModelPanel matches the message
  text with `/(\d+) are needed/`, so rewording it must keep that number.
- Model version report: `auc.source === "held_out"` → **test set**. `report.known_limit`,
  `report.warnings[].message`, `thresholds.reason` and `version.note` are printed
  verbatim and were never audited.
- Test-set endpoint returns `by_verdict`, keyed `TRUE` / `FALSE` — "verdict" is
  retired, and TRUE/FALSE are database values, not words for a reviewer.
- `report.labels.by_source[].source` = `"human"` → **reviewer**.
- `report.references[].affects[]` — raw snake_case feature names, still shown.

## 3. Errors and validation messages printed verbatim

- `POST /api/runs/{id}/model/apply` 409 `detail` contains **"review band"** →
  **review line** / **For review**. ReviewScreen matches it with
  `/refused|review band|two-valued/i`.
- `backend/app/services/pair_labels.py:99` — `"is_match must be TRUE or FALSE, not …"`.
- `backend/app/routers/entities.py:236` — `"column must be one of …"` lists raw
  column keys.
- `backend/app/rules/engine.py:893,1032,1286,1364`, `rules/linkage.py:956,1034`,
  `rules/vetoes.py:604,635` — `"op must be one of …"`, `"fallback must be one of …"`,
  `"on_guard_fail must be one of …"`, `"applies_when must be one of …"`. All dump
  raw enum values at a researcher.
- `PAIRS_API.md:887` — `"record_id_a '999999' is not in run run_2026_09_18a"`.
- `POST /api/config/validate` `errors[].message` / `warnings[].message` carry
  internal names; `path` (e.g. `match_keys[2].columns`) is the only handle
  support has, so keep it but label it.
- `unscored_note` on `POST /api/runs/{id}/recluster` — "… have no **scored
  pairs**. Rerun the pipeline to score them." → **pairs**; names no stage.

## 4. Pair explanation and guards — shown verbatim on Review and Exact groups

- `explanation[].label` (`PAIRS_API.md:338-370`): `"Exact match on forename_canon"`,
  `"Exact match on surname"`, `"Equal dob_year_clean"`, `"dob_year_clean within 1"`,
  `"title is NULL"`, `"All other comparisons"`. These leak cleaned-column names
  and SQL onto the Review screen.
- `columns[].label` for cleaning columns arrives as the key, e.g.
  `{ "key": "unit_id", "label": "unit_id" }` (`PAIRS_API.md:201`).
- Exact-group `guard` is raw syntax: `"max_distinct:name_core=7>3"`,
  `"max_group_size:12>10"` (`RULESET.md:162`). The frontend parses it, but
  `name_core` still reaches the screen.
- `exact-eval keys[].name` falls back to the key id when absent.
- `veto_reason` is clean: `"Born 1958 and 1962: more than 2 years apart"`.

## 5. Run files and diagnostics — `backend/app/routers/runs.py:133-137`

- `"units.parquet"`: "One **representative** row per unit — a **merged group** or a single record" → **unit**, **exact group**.
- `"score_eval.json"`: "What the exact groups plus the accepted pairs do to the **existing labels**" → **earlier grouping**.
- `runs.py:1432` (legacy OCOD query description): `"exact, high, review, ambiguous, unmatched_ocod, unmatched_roe"` — a second, dead bucket vocabulary.
- `runs.py:964`: `"Bulk marked {n} unlabelled {bucket} matches as {verdict} …"` → **verdict**.
- `diag.score_column` / `histogram.score_column` = `"gbt_score"` → **model score**.
- `clustersByStatus` keys (`too_large`, `mixed_names`, `weak_link`, `mixed_ids`) ship no display label.

## 6. Export column names and files

- `backend/app/profiles/donations_export.py:25-32` — `NEW_COLUMNS` =
  `RecordID, EntityID, EntityBasis, DonorStatusStandardNew, DonorStatusBasis`.
  `EntityBasis` carries raw values (`human`, `exact_key`, `import`, `score`,
  `single`); `DonorStatusBasis` carries `rule`/`majority`/`raw`/`tie`/`human`.
  The screen now says "How it was decided" and "How this value was set".
- `ALIAS_HEADER` = `retired_entity_id, survivor_entity_id, …`, sheet named
  `"aliases"`. "alias" → **retired ID**, "survivor" → **surviving ID**.
- `_run_info` (`donations_export.py:122-138`) dumps every count under its raw
  camelCase key (`pairsVetoedFromAccept`, `clustersByStatus` as JSON).
- `psc_export.py:169` — `"aliases.csv    entity IDs that retired into another"`.
- `/api/labels/export` CSV carries `is_match`, `held_out`, `provenance` as
  column names.

## 7. Default config text passed straight through to the screen

All of these render verbatim on the Config tabs:
`track_rules[].description`, `cleaning[].description`,
`derived_columns[].rules[].description`, `match_keys[].name` and
`match_keys[].description`, `vetoes[].reason`, and
`linkage_settings.comparisons[].description`.

Worst offenders — dense statistics paragraphs a researcher cannot use:

- `psc/linkage_settings.json:52` — "deduping route 5, the marriage route: … deduping measured the current routes catching only 42.9% of 37,467 verified surname-change pairs, and this lifting it to 67.2%."
- `psc/linkage_settings.json:123` — "**EM** put m = 0.835 on 'all other', because its **training block** is full of different people who share a name, so a disagreement cost 0.23 **bits**."
- `psc/linkage_settings.json:85,139` — "**Term frequency** on: agreeing on FOTHERGILL is far stronger evidence than agreeing on SMITH."

GLOSSARY.md retires EM, bits and term frequency on sight. Move the measurements
into a separate `note` field the UI does not show by default.

## 8. Self-declared identity

`current_user()` (`backend/app/auth.py:50-61`) falls back to the string `"user"`,
so `reviewer`, `published_by` and `triggered_by` can all read `"user"`. The
Label library now prints an em dash instead, but the value is still stored.

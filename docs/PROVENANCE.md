# Provenance — what is recorded, where, and how to read it

This file answers one question three ways: **why are these two records one
entity?** Once from the screen, once from an export, and once from the registry
alone, with the run folder deleted.

The words are `GLOSSARY.md`'s. The ordered list that answers "How it was
decided" is, weakest first: **On its own, Match key, Score, Veto rule, Earlier
grouping, Reviewer**. A later one always beats an earlier one. **Suggested** — a
machine-written answer — sits outside the list and never ranks. One place holds
all of it: `backend/app/vocabulary.py`, served at `GET /api/vocabulary`.

## What is recorded, and where

| What | Where it lives | Survives deleting the run folder? |
|---|---|---|
| Why a record is in its entity | `entities.parquet` `entity_basis`; registry `entity_members.entity_basis` | Yes |
| Where the entity's ID came from | `entities.parquet` `id_status`; registry `entity_members.id_status` | Yes |
| Every accepted link inside an entity | registry `entity_edges` | Yes |
| How a value was settled for a whole entity | `entity_attributes` `basis`, `rule_id`, `tally_json`, with `since_run` / `until_run` | Yes |
| Entity IDs two proposals both claimed | registry `entity_id_collisions` | Yes |
| What produced the run | `run_manifest.json` in the run folder, and five columns on the `runs` row | Partly — the five columns do |
| Which lines put a pair in its bucket | `bucketing_history.json` in the run folder | No |
| Which model wrote a score | `pairs.parquet` `gbt_model_version`, and `model_state.json` | No |
| Who did what, when | `audit_log` | Yes |

### The join log

`entity_edges` is written once, at publish, inside the same transaction as the
entities. It is never rebuilt, because the thing it is rebuilt from — the run's
`pairs.parquet` — is exactly what goes missing.

One row per accepted link inside a published entity. Each row carries only what
its kind of link has:

- **Match key** — the key's name and id, and the exact group.
- **Score** — the score, whether Splink or the model produced it, and the model
  version when it was the model.
- **Veto rule** — the veto a reviewer overrode by joining the two anyway.
- **Earlier grouping** — the earlier ID both sides carried.
- **Reviewer** — the label id, who saved it, when, their note and their source
  link.

An exact-group merge is logged as a **star**, not as every pair: the group's
smallest record id joined to each of the others. A match key that puts 1,000
records together makes 499,500 pairs and 999 links, and the 999 say the same
thing.

The edges are built in DuckDB over the run's parquet files and loaded into
SQLite in one transaction with the indexes dropped first and rebuilt after.
The donations run writes 55,573 edges in under two seconds. Publishing twice is
a no-op: the second publish deletes what the first wrote and writes it again.

### What produced a run

`run_manifest.json` is written when a run starts and closed when it ends. It
holds the input file's name, size, sha256, row count and upload time; the code
version; the config version; the accept line, the review line and the lowest
score kept; per track, the scorer that decided and the model version with its
training date and label counts; each reference table's name, row count, build
time and file hash; the versions of pandas, duckdb, splink and lightgbm; the
start and end times; and who started it.

The code version is the git commit, read from `.git/HEAD` without shelling out.
With no `.git`, the `APP_VERSION` environment variable answers. With neither,
it says `unknown` rather than guessing.

Five of those facts are also on the `runs` row — `code_version`,
`input_sha256`, `input_bytes`, `input_rows`, `input_uploaded_at` — so a list
view and an export can read them without opening a file.

### Which lines set the buckets

`pairs.parquet` holds each pair's bucket and not the lines that put it there,
and a re-bucket moves the lines. Stamping three floats onto a hundred million
rows to record something that changes ten times in a run's life is the wrong
trade, so `bucketing_history.json` records the **change** instead: the time,
who, the three lines, the scorer, the model version and the resulting bucket
counts. Four things append an entry — the first scoring, a re-bucket, applying
a model and reverting one. The pairs list and the pair detail return the entry
in force as `bucketing`.

## How to answer "why are these two records one entity?"

### From the UI

`GET /api/runs/{run_id}/entities/{entity_id}/provenance` while the run exists,
and `GET /api/registry/entities/{entity_id}/provenance` for anything published.
Both return the answer twice:

- `steps` — an ordered list a person can read, weakest evidence first, so it
  reads as the story of the merge. Each step names its own evidence and carries
  up to five examples.
- `edges` — the structured join log the steps were written from.

`question` and `precedence` are the words to print above the list. They come
from the same vocabulary the screens read, so the panel and the chips agree.

A worked example from the donations run, entity `833` (86 records):

```
This entity holds 86 records.
Match key         A match key put these records together: Company number,
                  Name and postcode. That is 80 links.
Score             5 links were accepted on the Splink score. The scores ran
                  from 1.00 to 1.00.
Earlier grouping  10 links were accepted because both sides already carried
                  the same earlier ID: 833.
Where this ID     New. Nothing in the registry claimed these records, so the
came from         tool made a new ID.
```

`docs/ENTITIES_API.md` has the full response for both endpoints.

To see why one *record* reads the way it does — why its name cleaned to what it
did — `POST /api/config/preview-cleaning` with `run_id` and `record_id` replays
**that run's own frozen rules** on that record, step by step. It does not use
today's rules, because today's rules did not make that value.

### From an export

Every export carries a **run sheet** with the same facts as the manifest, in the
words the screens use: the input file and its hash, the code version, the config
version, the three lines, the scorer and its model version, the reference
tables, the library versions, who exported it and when, and whether the run is
published. No count reaches a reader under a raw key such as
`pairsVetoedFromAccept`.

Beside it is a **"How to read this file"** sheet: one row per column the export
adds, with a plain definition, then the ordered provenance list with its
definitions, then the two lists that answer different questions — "How this
value was set" and "Where this ID came from".

- **Donations, xlsx** — four sheets: `donations`, `Retired IDs`, `run`,
  `How to read this file`. `EntityBasis` and `DonorStatusBasis` keep their
  column names and carry the canonical labels as their values, so the file says
  "Earlier grouping" where the screen does.
- **Donations, csv** — the CSV has one sheet, so the other three are written
  beside it: `export_<scope>_run_sheet.txt`, `export_<scope>_how_to_read.txt`
  and `export_<scope>_retired_ids.csv`.
- **PSC** — a zip holding the decision table, the Elasticsearch bulk file,
  `retired_ids.csv`, `README.txt` (the manifest) and `HOW_TO_READ.txt`.

### From the registry alone

Delete the run folder and the registry still answers. `entity_members` says why
each record is there and where the ID came from; `entity_edges` holds every
link; `entity_attributes` holds the value in force and every value before it,
with the run that set each one; `entity_id_collisions` holds every time two
proposals claimed one ID. `GET /api/registry/entities/{entity_id}/provenance`
reads all four, follows a retired ID to the one that is live, and returns the
same `steps` and `edges` as the run-scoped endpoint.

## Who did what

The `audit_log` covers: a run created, started, finished, failed, deleted,
reclustered or adopted from a folder built offline; a label saved or deleted; a
group decision; an attribute a reviewer settled; a config version saved; the
methodology notes saved; a threshold moved; an upload, with the file's hash; an
export downloaded, with who, the scope, the format and the file's size; a model
trained, activated, deactivated, applied or reverted, and a test set
designated; and a publish, with the number of entities and the number of links
written.

Nothing stored as a name is ever the literal word `user`. Where nobody said who
they are, the value is `unknown`, which is the true thing.

## What is deliberately not recorded

- **The lines on every pair row.** See "Which lines set the buckets" above.
- **Every pair inside an exact group.** See "The join log" above.
- **A tally for a value every member agrees on.** The value already says it.
- **The old database columns.** `runs.ocod_filename`, `runs.ch_filename` and
  the whole `labels` table belonged to the two-dataset tool this app was copied
  from. Nothing reads or writes them any more. They stay because dropping a
  column is destructive and the rows cost nothing.

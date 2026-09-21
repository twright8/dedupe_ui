# Terminology and provenance audit

Read `docs/GLOSSARY.md` first: it holds the proposed words. This file holds the
evidence, the provenance gaps and the work list. Scope: `frontend/src/**`, the
backend's user-facing output, the default rulesets and the contract docs. Branch
`wip/psc-scale`, read 2026-09-21; backend line numbers were read while another
agent was editing `backend/`, so re-grep before citing one. Counts are the exact
phrase, and where a word is also a code identifier only rendered uses count.

# Part 1 — Inventory

## 1. record / row / donor / PSC record / member

| Name | Where | Count |
|---|---|---|
| "record", "records" | `RunDetailScreen.jsx:203,394,456,466,672,684,836,1491`; `EntitiesTable.jsx:275`; `RecordsTable.jsx:246` | ~130 |
| "row" / "member" | `FocusStrip.jsx:59` "No individual rows recorded"; `ClusterScreen.jsx:1259` "No member records returned." | 2 rendered |
| profile noun | `donations.py:569` "donor"; `psc.py:631` "PSC record"; fallback `base.py:112`, `profileText.js:17` | 2 profiles |

No component hard-codes "donor" or "PSC record", but `nouns.record` and
`nouns.record_plural` are declared and never read (`PROFILE_UI.md:30-31`), so
every screen says "record" even where the profile has a better word.

## 3. exact group / merged group / held group

| Name | Where | Count |
|---|---|---|
| "Exact groups" (tab) | `RunDetailScreen.jsx:1492,1527`; `ExactGroupsTable.jsx:469` | 4 |
| "Merged" / "Held" chips | `ExactGroupsTable.jsx:374-393,549` | 4 |
| "Held for review" / "Held groups open" / "Withheld groups" | `RunDetailScreen.jsx:679` / `:520` / `:513` | 3 |
| "Held by a key" / "held group" (prose) | `ClusterScreen.jsx:58`; `ReviewScreen.jsx:771,791,988` | 7 |
| backend | `rules/keys.py:21-22` `MERGED`/`HELD`; cluster status `held_key` | 2 vocabularies |

Four words for two things: "held" means a guard stopped a match-key group,
"withheld" means the cluster gate stopped a cluster, and the run summary uses
both within six lines (`RunDetailScreen.jsx:513` and `:520`).

## 5. score / probability / model score

| Name | Where | Count |
|---|---|---|
| "score" (generic) | pervasive | ~50 |
| "Match probability" (aria-label) | `DiffHero.jsx:163` | 1 |
| "Splink score" | `ThresholdPanel.jsx:181,439`; `ModelPanel.jsx:167,294`; `RunDetailScreen.jsx:554` | 6 |
| "Splink probability" and "GBT score" | `RunDetailScreen.jsx:998` | 2 |
| "model score" | `ThresholdPanel.jsx:178`; `ModelExplain.jsx:54`; `ModelPanel.jsx:667` | 4 |
| "the model" / "cold start" | — / `RunDetailScreen.jsx:559` | ~15 / 1 |
| fields | `match_probability`, `gbt_score`, `match_weight` | — |

Four names for one quantity on adjacent screens; "cold start" is jargon with no
gloss; the same probability is a bare 0–1 decimal in `DiffHero.jsx:171` and a
percentage in `ModelExplain.jsx:60`.

## 6. bucket names

| Bucket | On-screen names | Where |
|---|---|---|
| `accept` | "auto-accept" | `DiffHero.jsx:29`; `ThresholdPanel.jsx:21` |
| | "Auto-accepted" / "Accepted" | `ReviewScreen.jsx:37`, `RunDetailScreen.jsx:588` / `RunsScreen.jsx:330` |
| | "Auto-accept threshold" / "Auto-accept" | `NewRunScreen.jsx:470`; `ThresholdsTab.jsx:140,339` |
| `review` | "review", "Review", "Review queue", "Review band", "Review floor" | `DiffHero.jsx:30`; `ReviewScreen.jsx:36,444`; `NewRunScreen.jsx:493`; `ThresholdsTab.jsx:149,351` |
| `reject` | "rejected", "Rejected", "Below floor" | `DiffHero.jsx:31`; `ReviewScreen.jsx:38`; `ThresholdsTab.jsx:363` |

"Auto-accepted" is wrong: a pair a reviewer marked Match sits in the `accept`
bucket and is shown as "auto-accept" (`DiffHero.jsx:29`, reached from
`ClusterScreen.jsx:979`), though nothing automatic happened. A dead second
vocabulary sits unused in `ProbBar.jsx:19-27`: "exact", "reviewed ✓",
"resolved", "ambiguous".

## 7. label / answer / decision / verdict

| Name | Where | Count |
|---|---|---|
| "label" (rendered) | `Layout.jsx:86` "Label library"; `LabelsScreen.jsx:551` "Labels" | ~25 |
| "answer(s)" | `MethodologyScreen.jsx` throughout; `ModelPanel.jsx:175,210,231`; `ThresholdPanel.jsx:172,400` | ~30 |
| "Verdict" / "decision", "Decided by" | `LabelsScreen.jsx:566,396`; `RunDetailScreen.jsx:545,570`; `ClusterScreen.jsx:281,533` | ~22 |
| "TRUE" / "FALSE" buttons | `ReviewScreen.jsx:534,542,1018,1037,1509,1524` | 10 |
| "Match" / "No", and "Match" / "Not a match" | `LabelsScreen.jsx:401,407,791`; `ReviewScreen.jsx:1557` | 6 |
| backend | `routers/labels.py:26` `normalize_verdict` | — |

Five vocabularies for one saved answer. A reviewer presses a button marked
**TRUE**, finds the row in the Label library under a column marked **Verdict**
reading **Match**, and reads about it in How it works as **your answer**. Group
decisions add four more: "Group decision" (`LabelsScreen.jsx:7`), "Group merge"
and "Group split" (`:30-31`), "Merged" and "Split" (`:42`), `cluster_merge`.

## 8. earlier grouping

| Name | Where | Count |
|---|---|---|
| "earlier manual grouping" | `profileText.js:36`; `donations.py:587` | 2 |
| "the earlier grouping" | `ThresholdPanel.jsx:466,502`; `ReviewScreen.jsx:786`; `RunDetailScreen.jsx:636` | ~20 |
| "Imported labels" / "Imported" / "Imported label" | `ReviewScreen.jsx:46`; `LabelsScreen.jsx:32`; `ClusterScreen.jsx:1032`; `EntitiesTable.jsx:26` | 4 |
| "earlier entity ID" / "Earlier ID" column | `ReviewScreen.jsx:48,750`, `DiffHero.jsx:37` / `ClusterScreen.jsx:884` | 6 |
| "existing labels" / "earlier manual work" | `dedupe/__init__.py:31,50`; `LINKAGE.md:110` | 6 |
| `DonorIDStandardTR` | backend tests and `DESIGN.md:92` only | 0 on screen |

The frontend is nearly consistent, but the backend's stage descriptions, served
to the screen by `/api/pipeline/stages`, use two phrases inside one list.

## 9. veto / rule / match key / cleaning step / derived column

The word **rule** covers five unrelated things, none sharing a screen name.

| Thing | On-screen names | Where |
|---|---|---|
| cleaning step | "Cleaning rules" tab, "Cleaning steps" heading, "step" rows | `ConfigScreen.jsx:68`; `CleaningTab.jsx:443,466,493,570` |
| track rule / derived-column rule / blocking rule | "Track rules" / "Rules" / "Blocking rules" | `TracksTab.jsx:51`; `DerivedTab.jsx:259`; `LinkageTrack.jsx:235` |
| veto | "Vetoes" tab, "a rule about a pair", "Stopped by a rule", "Stopped by rules" | `ConfigScreen.jsx:120`; `DiffHero.jsx:63`; `RunDetailScreen.jsx:629` |
| match key | "Match keys" tab, "Exact keys" stage, "exact key" glossary, "Exact groups" tab | `ConfigScreen.jsx:106`; `dedupe/__init__.py:28`; `MethodologyScreen.jsx:504`; `RunDetailScreen.jsx:1492` |

Guards are the exception: every JSON key is translated (`MatchKeysTab.jsx:341-418`).

## 10. cluster / group / entity

`ClusterScreen.jsx` is titled "Cluster review" (`:266`, `App.jsx:46`) and calls
the same thing a **group** in every line of body copy — `:42`, `:48`, `:60`,
`:268`, `:844`, `:863`, `:1191`. About 20 uses of "group", none of "cluster".
"withheld" never reaches the screen; the KPI says "Withheld groups"
(`RunDetailScreen.jsx:513`).

## 12. "How it was decided" — six vocabularies

| Field | Values | Shown as | Where |
|---|---|---|---|
| pair `decided_by` | `score`, `model`, `veto`, `import`, `human` | chip suffix "rule / import / human / model"; tooltips "decided by the score", "a rule stopped this pair being accepted", "accepted because both sides carry the same earlier entity ID", "decided by a reviewer" | `DiffHero.jsx:33-40`; `pairs_reader.py:47` |
| Review filter | same field | "Score", "Imported labels", "Human" — `model` and `veto` missing | `ReviewScreen.jsx:42-51` |
| `entity_basis` | `single`, `exact_key`, `import`, `score`, `human` | "Single", "Exact key", "Imported label", "Score", "Human" | `EntitiesTable.jsx:20-30`; `stage_5_entities.py:50` |
| `edge_source` | `score`, `import`, `human` | "Score", "Imported label", "Human" | `ClusterScreen.jsx:1025-1033` |
| label `provenance` | `manual`, `bulk_range`, `llm`, `import`, `cluster_merge`, `cluster_split` | "You", "Bulk", "Suggested", "Imported", "Group merge", "Group split" | `LabelsScreen.jsx:27-34`; `pair_labels.py:25` |
| attribute `basis` | `human`, `rule`, `majority`, `raw`, `tie` | "Human", "A derived-column rule set this value…", then the raw value | `EntitiesTable.jsx:50-55`; `stage_5_entities.py:51` |
| `id_status` | `new`, `kept`, `survivor`, `minted_after_collision` | "New", "Kept", "Survivor", "Re-minted" | `EntitiesTable.jsx:33-48` |
| `<column>_rule` | a rule id | never shown | `rules/engine.py:32` |
| `vetoed_by` | a veto id | never shown, only its truthiness | `DiffHero.jsx:59,86` |
| run "Decided by" KPI | — | "Splink score" / "Model v3 (graded)" | `RunDetailScreen.jsx:554-572` |

Direct clashes. **`human` vs `manual`**: one person's answer is `human` on a
pair, `manual` on the label, "You" in the Label library, "Human" on the entity.
**`import`** appears as "earlier grouping", "Imported labels", "Imported" and
"Imported label" across four screens. **`score` vs `model`** are split on a pair
and merged on an entity, so a pair the model accepted becomes
`entity_basis: score`. **Precedence is reversed**: `BASIS_ORDER`
(`stage_4_cluster.py:60`) ranks `score` above `import`, `RULESET.md:204` ranks
`import` above `score`. **"Decided by" means two things**: which scorer, on the
run summary; which authority, on a pair.

## 13. frozen test set

Six surface names, one concept: "Test set" (`LabelsScreen.jsx:294`), "frozen for
testing" (`:311`), "Teaches" / "Tests" (`:339,348,419,427`), "held back for
testing" (`ModelPanel.jsx:409,441,470`; `ThresholdPanel.jsx:172`), "frozen test
set" (`ModelPanel.jsx:234`), plus `held_out`, `evalSet` and `getTestSet`.

## 14. run / config version / ruleset

"Config & rules" is the nav item (`Layout.jsx:87`) but the empty state says
"Check the keys on the Config screen" (`ExactGroupsTable.jsx:487`). "ruleset",
"linkage settings" and "draft" never render, except "Load v{n} into the draft"
(`VersionHistoryTab.jsx:227`). Three stage numberings reach the user:

| Where | Numbering |
|---|---|
| `/api/pipeline/stages` (`dedupe/__init__.py:12-59`) | 6 stages: Load, Clean, Exact keys, Score pairs, Cluster, Entity IDs. No model stage, no derived columns |
| How it works (`MethodologyScreen.jsx:77-339`) | 10 stages, 0–9, including Tracks, Derived columns and The model |
| contract docs | stages 0–5 plus "stage 3b"; `RULESET.md:95` and `ENTITIES.md:79` also use "stage 1" and "stage 2" for the two halves of status standardisation |

A user therefore reads "Stage 2 alone, for comparison" for the match keys on
`ThresholdPanel.jsx:459` and "Stage 4 alone, with no scoring" for the same figure
on `MethodologyScreen.jsx:424`.

## 16. precision, recall and the figure sets

Seven sets are on screen (`ThresholdPanel.jsx:416-461`): "Everything the run
would publish", "The scorer on its own", "The scorer on its own, before the
rules", "The Splink score on its own", "The model on its own", "With the human
decisions applied", "The match keys on their own". How it works explains three
(`MethodologyScreen.jsx:416-425`) and says "worked out three ways" (`:411`), so
four have no explanation a user can reach. "human decisions" is used here only;
elsewhere it is "your answers".

## 2, 4, 11 and 15 — the clusters that are already consistent

| Cluster | Finding |
|---|---|
| unit | One word, ~45 rendered uses. Explained only in the How it works glossary (`MethodologyScreen.jsx:493`). "representative" is in `LINKAGE.md:14`, never on screen |
| pair | "pair" ~130 uses; "candidate pair" twice (`ModelPanel.jsx:209`); "scored pair" four times. "edge" never reaches the screen — `ClusterScreen.jsx:995` heads the list "How these units are joined" |
| track | Every label comes from `profile.tracks[].label` (`base.py:132-135`). "All tracks" is the only literal (`ReviewScreen.jsx:679`) |
| publish | "Publish & export" (`RunDetailScreen.jsx:1494`), "Aliases created" (`PublishPanel.jsx:31`), "Survivor" (`EntitiesTable.jsx:37`). One mismatch: `PublishPanel` says "register", `ENTITIES.md` says "registry" |

## Terms on screen and defined nowhere a user can reach

The How it works glossary (`MethodologyScreen.jsx:490-555`) defines 13 terms:
record, unit, track, exact key, guard, held group, blocking rule, score, bucket,
veto, label, cluster, entity. These 32 are on screen and defined nowhere:
`cold start` · `graded` · `Teaches` / `Tests` · `held out` · `Splink` ·
`EM training block` · `AUC` · `SHAP` · `Average precision` · `ablation` ·
`Wilson bound` · `calibration` · `term frequency` · `Jaro-Winkler` ·
`comparison level` · `derived column` · `consensus column` · `priority column` ·
`evidence focus` · `proposal` · `survivor` · `alias` · `id collision` ·
`re-minted` · `attribute tie` · `cross-track ID` · `weak link` · `mixed IDs` ·
`pattern summary` · `oversized block` · `pp` (`MatchKeysTab.jsx:773`) ·
`bits` (`PairExplain.jsx:30`).

## Internal names that leak to the screen

| What | Where |
|---|---|
| `clean_name` as a column header | `RunDetailScreen.jsx:1053` "Final clean_name" |
| attribute `basis` value and column key printed raw | `ClusterScreen.jsx:1183,1186`; `EntitiesTable.jsx:278-282` |
| label `provenance` raw (` · bulk_range`), and as a fallback | `ReviewScreen.jsx:1558`; `LabelsScreen.jsx:715,822` |
| cluster `status` and `REASON_LABELS` raw fallbacks | `ClusterScreen.jsx:82`; `RunDetailScreen.jsx:472` |
| rule ids in tables: `pb3`/`b1`, `k1`, `d1r1`, `t2`, `v1` | `RunDetailScreen.jsx:280,408`; `ExactGroupsTable.jsx:326-331,546`; `DerivedTab.jsx:511`; `TracksTab.jsx:247`; `VetoesTab.jsx:517` |
| feature `group` key raw (`splink`, `rarity`) | `ModelPanel.jsx:826` — `ModelExplain.jsx:15-23` maps the same keys to words |
| `splink_args` JSON, Splink column and gamma level, `l.`/`r.` SQL | `LinkageTrack.jsx:170,186,465`; `PairExplain.jsx:57-58` |
| internal names and enums in errors: `is_match`, consensus columns, operator lists | `pair_labels.py:99`; `routers/entities.py:236`; `rules/engine.py:893,1032,1286,1364`; `rules/linkage.py:956,1034`; `rules/vetoes.py:604,635` |
| `clustersByStatus` keys (`too_large`, `weak_link`, `mixed_ids`) | `routers/runs.py` `_normalize_counts` — no backend enum ships a display label |
| export "run" sheet dumps every camelCase count key | `donations_export.py:133-137` |

## Leftover OCOD / ROE wording

| What | Where |
|---|---|
| "OCOD–ROE Linkage Review UI" | `frontend/src/styles/index.css:2` |
| dead OCOD hook and dead band vocabulary, both still exported | `hooks/useLabels.js:72-80`; `components/ProbBar.jsx:19-27` |
| live OCOD endpoints still mounted, plus the whole legacy reader | `routers/runs.py:858,888,1429,1475,1494`; `services/match_reader.py` |
| a second `bucket` vocabulary in a live query description | `routers/runs.py:1432` "exact, high, review, ambiguous, unmatched_ocod, unmatched_roe" |
| `provenance="bulk_review"`, not in `PROVENANCES` | `routers/runs.py:954` (legacy `labels` table) |
| dead OCOD columns, and stale provenance values in schema comments | `db.py` `runs.ocod_filename`, `runs.ch_filename`; `db.py:64` lists `proxy`, `implied_negative` |
| "Left" / "Right" column headers | `ReviewScreen.jsx:918-919`; `FocusStrip.jsx:154-155`; `DiffHero.jsx:179,183` |
| "as in roe_ui" comments | `DiffHero.jsx:7`; `PairExplain.jsx:8`; `ThresholdPanel.jsx:4,218` |

## Spelling, and numbers shown with no unit or base

Spelling is clean and needs no action: every user-visible string is British, with
`en-GB` formatting. The only American spellings are CSS properties, library
keyword arguments (`analyzer=`, `color=`) and Python identifiers, none of which
reaches a screen. Numbers are a different matter.

| What | Where |
|---|---|
| "useful 62" — a 0–1 score times 100, no `%`, no scale | `ReviewScreen.jsx:972-976` |
| score as a bare 3-decimal number, but a percentage elsewhere; thresholds bare too | `DiffHero.jsx:171` vs `ModelExplain.jsx:60`, `PairExplain.jsx:92`; `ThresholdsTab.jsx:59`; `NewRunScreen.jsx:472,495` |
| "0.92", "0.50", "0.20", "200" stated as facts though all four are editable settings | `MethodologyScreen.jsx:271-273,356` |
| AUC, weight, ablation delta as bare numbers; "pp" never expanded | `ModelPanel.jsx:261,432,886`; `MatchKeysTab.jsx:773` |
| default `review` differs between components: 0.7 vs 0.5 | `ProbBar.jsx:6` vs `DiffHero.jsx:155` |
| pair precision and recall carry no denominator beside the number | `RunDetailScreen.jsx:642,649`; `ThresholdPanel.jsx:502` |

## Docs that no longer match the code

`ENTITIES.md:18-25` omits the cluster statuses `cross_track_ids` and
`attribute_tie`; `ENTITIES.md:50` omits the attribute basis `human`;
`LINKAGE.md:86` omits the label provenances `cluster_merge` and `cluster_split`;
`DESIGN.md:191` calls the legacy pipeline modules deletable, though they are
still mounted as live endpoints.

---
# Part 3 — Provenance gaps

"Provenance" here means: for anything shown or exported, a user can find out how
it got that way, from what, by whom and when. Ranked by what a researcher
defending a published merge would miss first.

### 1. The registry does not record why records are together (critical)

`entities.parquet` carries `entity_basis` and `id_status` per record
(`stage_5_entities.py:413`). The registry does not: the `entities` table is
`entity_id, track, created_run, created_at, status, alias_of, retired_run`
(`db.py:103-111`) and `publish()` never writes a basis
(`registry/store.py:249-268`). Delete the run folder — `DELETE /api/runs/{id}`,
`runs.py:476` — and "how was this decided" is gone for every published entity.
To record: copy both columns onto `entity_members` at publish.

### 2. There is no chain of links for a published merge (critical)

For a live run a reviewer can see the edges: `GET /api/runs/{id}/clusters/{cluster_id}`
returns `pair_id`, `match_probability`, `bucket`, `score_bucket`, `decided_by`
and `source` (`clusters_reader.py:662-691`). That is rebuilt at read time from
`pairs.parquet`, so it goes when the run is deleted or reclustered, and only the
strongest edge is stored (`edge_source`, `stage_4_cluster.py:531-552`). The model
version behind a `gbt_score` is not on the pair row: it lives in the run's
`config/linkage_settings.json` as `gbt_model_version`, which apply-model and
revert-model overwrite in place (`pipeline_runner.py:376-401`).

Already on disk: `pairs.parquet` (`match_probability`, `gbt_score`, `bucket`,
`decided_by`, `vetoed_by`, `veto_reason`) and `pair_labels` (reviewer, notes,
`evidence_url`, `created_at`, `decision_id`, `superseded_by`). To record: a
per-entity join log at publish — every edge with its score, model version,
thresholds in force, and any veto it overrode.

### 3. No input file hash, no code version (high)

`runs` stores `input_filename` and nothing else about the file (`db.py:15-34`).
There is no hash anywhere in `backend/app`: grepping `hashlib`, `sha256`,
`git rev-parse` and `__version__` finds only PSC's own record-ID hashing and
pyarrow's version string. `upload_sessions.size_bytes` and `completed_at` exist
(`db.py:170-181`) and are never joined to the run. To record: a sha256 at upload
and a code version at run start.

### 4. The export's run sheet is thin and internal (high)

`_run_info` (`donations_export.py:122-138`) writes Run, Config version, Scope,
Exported at, Input file, then dumps every count under its camelCase key
(`pairsVetoedFromAccept`, `clustersByStatus` as JSON). It omits the thresholds —
they are `runs.threshold_high` and `threshold_review`, never merged into
`counts_json` — the model version, who exported it, and whether the run is
published. The CSV drops the alias and run sheets entirely
(`donations_export.py:184-188`); PSC's `README.txt` carries less again
(`psc_export.py:159-176`).

### 5. A pair's bucket cannot be tied to the thresholds that set it (high)

`pairs.parquet` stores `bucket` and `score_bucket` but not the threshold values
(`stage_3_score.py:805-831` reads them from the config and writes only the
result), and a re-bucket updates `runs.threshold_high` in place
(`runs.py:1543-1562`). To say which lines were in force when a pair landed in
review you must correlate `audit_log` rows of kind `threshold` by timestamp. Same
for a withheld cluster: `status` and `statuses` are stored
(`stage_4_cluster.py:474-478`), the limits behind them are not. To record: those
values stamped onto each pair and cluster row.

### 6. "Which rule set this value" is thrown away at the entity level (medium)

`records.parquet` carries `<column>_rule` with the winning rule's id
(`rules/engine.py:713-741`). `consensus()` reads it and keeps only the word
`rule` (`stage_5_entities.py:302-313`); no entity or cluster API exposes the id,
and a `majority` basis keeps neither the tally nor the losing values. To record:
the rule id and the vote counts.

### 7. Cleaning provenance is preview-only in practice (medium)

`trace_cleaning` and `apply_cleaning` share one loop (`rules/engine.py:451-547`),
so the trace is faithful. `POST /api/config/preview-cleaning` takes a `run_id`
and reads that run's `records_raw.parquet` (`routers/config.py:282-330`). But it
replays the current or draft ruleset, not the run's own `config/ruleset.json`; it
finds records by a name substring, not by `record_id`; and nothing links to it
from the Records tab or the Review screen. Nothing new needs recording: change
the endpoint to take `run_id` plus `record_id`, use the run's own snapshot, and
link to it.

### 8. Entity attributes have no history; ID collisions do not survive (medium)

`publish()` deletes and re-inserts `entity_attributes` per entity
(`registry/store.py:259-268`), so only the latest value, basis and run survive;
`entity_members` does keep history, through `since_run` and `until_run`. ID
collisions are listed only in the run's `entity_report.json`
(`stage_5_entities.py:214-276`), and `build_plan` keeps only the first 20
examples of each change kind (`registry/plan.py:159-174`).

### 9. The audit log is good, with three holes (medium)

Eight kinds: `run`, `label`, `config`, `threshold`, `upload`, `export`, `model`,
`publish` (`audit_logger.py:10-11`). Every action in the brief is logged — single
labels with `label_id`, `pair_id`, `is_match` and `provenance`
(`runs.py:1282-1290`); group decisions and attribute overrides
(`entities.py:202,251,292`); config saves (`config_manager.py:46`); publish
(`entities.py:554`); exports (`entities.py:650`); model train, apply, activate,
deactivate and revert (`model.py:87-350`); run start (`runs.py:286`); re-bucket
(`runs.py:1543`). The holes:

- `backend/scripts/adopt_run.py` writes `runs` and `config_versions` directly
  with no `log_event` call. A PSC run built offline and adopted leaves no trace
  of who or when.
- `PUT /api/notes/methodology` (`routers/notes.py:50-60`) writes
  `app_settings.updated_by`, keeps no history and never calls `log_event`.
- Every name is self-declared. `current_user()` falls back to the string `"user"`
  (`auth.py:50-61`) and the cookie's name is whatever the client sets. Every
  `reviewer`, `published_by` and `triggered_by` inherits that.
- The Audit screen cannot filter for a publish: `kinds` (`AuditScreen.jsx:57-66`)
  and `KindTag` (`:26-35`) both omit it, so a publish renders as a raw grey tag.

### 10. The name-frequency table is self-describing but unlinked (low)

`build_name_frequencies.py:269-286` writes a sidecar JSON with `source` (a local
path), `built_at`, `source_rows`, `rows` and the cleaning-step ids used, read
back by `references.meta_for` (`model/references.py:65-131`). But no run and no
model version records which build it used, the file is overwritten in place, and
the source is a path string, not a hash.

---

# Part 4 — Application plan

Sizes: S under half a day, M one to three days, L more than three. **[DECIDE]**
marks an item needing the owner's answer before it is built.

## FRONTEND

| # | Item | Size |
|---|---|---|
| F1 | Add `frontend/src/glossary.js`: one export per concept from `GLOSSARY.md` — `{ term, plural, definition, retired: [] }`. No component hard-codes a term after this. | M |
| F2 | Add `<Term name="unit" />`: renders the term and a hover definition from F1. Use it on first mention per screen. | S |
| F3 | Replace the How it works glossary (`MethodologyScreen.jsx:490-555`) with a render of F1, so the page cannot drift from the app. | S |
| F4 | Drive every chip map from F1: `DiffHero.jsx:27-48` (bucket, decided_by), `EntitiesTable.jsx:20-55` (basis, id_status, attribute basis), `ClusterScreen.jsx:36-87` (cluster status), `LabelsScreen.jsx:27-34` (source), `ReviewScreen.jsx:42-51` (decided_by filter). | M |
| F5 | One verdict vocabulary. Change `ReviewScreen.jsx:534,542,1018,1037,1509,1524` from TRUE/FALSE to **Match** / **Not a match**; change `LabelsScreen.jsx:401,407,566,791` "No" to "Not a match" and the column header "Verdict" to "Answer". **[DECIDE]** | S |
| F6 | One bucket vocabulary: **Accepted**, **For review**, **Rejected**. Fix `DiffHero.jsx:29-31`, `ReviewScreen.jsx:36-38`, `RunsScreen.jsx:330`, `RunDetailScreen.jsx:588,604`, `ThresholdPanel.jsx:21-23`, `ThresholdsTab.jsx:339-363`, `NewRunScreen.jsx:470,493`. Drop "auto-" everywhere. | M |
| F7 | One threshold vocabulary: **accept line**, **review line**, **candidate floor**. Fix `ThresholdsTab.jsx:140,149,158`, `NewRunScreen.jsx:470,493`, `ThresholdPanel.jsx:309,329`, `MethodologyScreen.jsx:271`. | S |
| F8 | Cluster screen: use "cluster" in body copy, or "group" in the title — not both. Fix `ClusterScreen.jsx:42,48,60,268,844,863,929,1191`. **[DECIDE]** | M |
| F9 | Held vs withheld: use "held group" only for a match-key hold and "withheld cluster" only for a gate hold. Fix `RunDetailScreen.jsx:513,520,679`, `ClusterScreen.jsx:58`. | S |
| F10 | One score vocabulary. Fix `RunDetailScreen.jsx:554,998` ("Splink probability", "GBT score"), `DiffHero.jsx:163`. Show every score the same way — **[DECIDE]** decimal or percentage — and fix `ModelExplain.jsx:60`, `PairExplain.jsx:92`, `DiffHero.jsx:171`. | M |
| F11 | Rename the run KPI `RunDetailScreen.jsx:570` "Decided by" to "Scored by"; gloss "cold start" as "new model" at `:559`. | S |
| F12 | Stop the raw leaks: `RunDetailScreen.jsx:1053,280,408,472`, `ClusterScreen.jsx:82,1183,1186`, `ReviewScreen.jsx:1558`, `LabelsScreen.jsx:715,822`, `ModelPanel.jsx:826`, `DerivedTab.jsx:511`, `TracksTab.jsx:247`, `VetoesTab.jsx:517`, `LinkageTrack.jsx:465`, `ExactGroupsTable.jsx:326-331`. Give each rule a name beside its id. | M |
| F13 | Read `nouns.record` / `record_plural` (declared, never read — `PROFILE_UI.md:30-31`) in every empty state and record count. | S |
| F14 | Explain the four unexplained figure sets: extend `MethodologyScreen.jsx:415-426` to all seven in `ThresholdPanel.jsx:416-461`, and fix "three ways" at `:411`. | S |
| F15 | Fix the stage-number clash: `ThresholdPanel.jsx:459` "Stage 2 alone" vs `MethodologyScreen.jsx:424` "Stage 4 alone". Name stages, never number them. **[DECIDE]** | S |
| F16 | Show the run's real settings on How it works instead of the hard-coded 0.92 / 0.50 / 0.20 / 200 (`MethodologyScreen.jsx:271-273,356`), or say plainly that they are defaults. | M |
| F17 | Units on numbers: `ReviewScreen.jsx:974` ("useful 62"), `MatchKeysTab.jsx:773` ("pp"), `ModelPanel.jsx:261,432,886`. Align `ProbBar.jsx:6` default `review` with `DiffHero.jsx:155`. | S |
| F18 | Delete the dead OCOD code: `hooks/useLabels.js`, `ProbBar.jsx:19-27` `BandTag`. Fix the header of `styles/index.css:2`. Change "Left"/"Right" headers to the two units' names with a neutral fallback (`ReviewScreen.jsx:918-919`, `FocusStrip.jsx:154-155`, `DiffHero.jsx:179,183`). | S |
| F19 | Add a `publish` filter and tag to the Audit screen (`AuditScreen.jsx:26-35,57-66`) and make the intro at `:129` list every kind. | S |
| F20 | Add `scripts/check-terms.mjs`: grep `frontend/src/` for every retired term in F1 and fail the build. Wire it into `npm run lint`. | S |
| F21 | One preview vocabulary across the Config tabs — today six ("Step preview", "Preview", "Track preview", "Test keys", "What these would stop", "Effect on current run"). | S |

## BACKEND

| # | Item | Size |
|---|---|---|
| B1 | Ship a display label beside every enum. No backend enum has one today (`STATUSES`, `BASES`, `ID_STATUSES`, `AGREEMENTS`, `PROVENANCES`, `DECIDED_BY`). Serve them at `GET /api/vocabulary` so the frontend and the docs read one source. | M |
| B2 | Settle the precedence clash: `BASIS_ORDER` (`stage_4_cluster.py:60`) vs `RULESET.md:204`. **[DECIDE]** | S |
| B3 | Add `entity_basis` and `id_status` to `entity_members` at publish (`registry/store.py:249-268`). Gap 1. | M |
| B4 | Write a per-entity join log at publish: every edge with its score, model version, thresholds in force, and any veto overridden. New table `entity_edges`. Gap 2. | L |
| B5 | Add `model_version` to `pairs.parquet` (`stage_3b_model.py`) so a rescore cannot erase which model produced a `gbt_score`. Gap 2. | M |
| B6 | Stamp the thresholds onto each pair row, and the gate limits onto each cluster row. Gap 5. | M |
| B7 | Hash the input file at upload (sha256) and copy the hash, size, row count and upload time onto the run row. Gap 3. | M |
| B8 | Capture a code version at run start and store it on the run row. **[DECIDE]** git commit or a release string. Gap 3. | S |
| B9 | Rewrite the export run sheet: plain English labels from B1 instead of raw count keys; add thresholds, model version, input hash and size, code version, exported by, published at. Make the CSV carry a second file with the same content. Gap 4. | M |
| B10 | Carry the winning rule id through `consensus()` into `entities.parquet` and `entity_attributes`; record the majority tally. Gap 6. | M |
| B11 | Change `POST /api/config/preview-cleaning` to take `record_id` and a `run_id`, and replay that run's own `config/ruleset.json`. Gap 7. | M |
| B12 | Version `entity_attributes` like `entity_members` (`since_run`/`until_run`) instead of delete-and-reinsert. Copy the ID-collision list into the registry. Gap 8. | M |
| B13 | Add `log_event` to `scripts/adopt_run.py` and `routers/notes.py`. Gap 9. | S |
| B14 | Stamp the reference table's `built_at` and `rows` into every model version's training report; hash the source file. Gap 10. | S |
| B15 | Reword the six stage descriptions in `dedupe/__init__.py:12-59` to the glossary terms, and add the model stage and the derived-column stage so the list matches How it works. | S |
| B16 | Reword the default rule descriptions and veto reasons in `profiles/defaults/*/ruleset.json` and `linkage_settings.json` to the glossary terms. Several are dense statistics paragraphs — e.g. `psc/linkage_settings.json:52`. Move the measurements into a separate `note` field the UI does not show by default. | M |
| B17 | Take the internal names out of user-facing errors: `routers/entities.py:236`, `services/pair_labels.py:99`, `rules/engine.py:893,1032,1286,1364`, `rules/linkage.py:956,1034`, `rules/vetoes.py:604,635`. | M |
| B18 | Delete or gate the legacy OCOD endpoints and modules: `routers/runs.py:858,888,1429,1475,1494`, `services/match_reader.py`, the `labels` table path, `db.py` `ocod_filename`/`ch_filename`. **[DECIDE]** delete or keep behind a flag. | M |
| B19 | Fix the two `AGREEMENTS` tuples that share a name for different vocabularies (`keys_eval.py:26` vs `score_eval.py:26`). Rename the second `IMPORT_AGREEMENTS`. Field name only, not user-visible. | S |
| B20 | Update the stale docs listed in Part 1: `ENTITIES.md:18-25,50`, `LINKAGE.md:86`, `DESIGN.md:191`. | S |

## Decisions the owner must make

| # | Question | Recommendation |
|---|---|---|
| D-a | On the cluster screen, is it a **cluster** or a **group**? (F8) | **Cluster.** "Group" already means an exact group and a held group; a third meaning is the worst of the three clashes. |
| D-b | What are the two answers on a pair called? (F5) | **Match** and **Not a match**. TRUE/FALSE is a database value, not a word for a reviewer. |
| D-c | Is a score shown as `0.92` or `92%`? (F10) | **`0.92`**, everywhere. A percentage invites the reader to treat it as a frequency, and the thresholds are already written as decimals. |
| D-d | Does `entity_basis` rank Score above Earlier grouping, or the other way? (B2) | **Earlier grouping above Score**, matching `RULESET.md:204`. A trusted earlier merge is stronger evidence than a score, and one rule is easier to defend than two. |
| D-e | Do we name pipeline stages or number them? (F15) | **Name them.** There are three numberings in use and any renumbering breaks the other two. |
| D-f | Is the rule called a **match key** and its output an **exact group**, or should both share a word? (Part 1 §9) | **Match key** and **exact group**. They are a rule and a result; one word for both would hide that. Retire "exact key" for the rule. |
| D-g | Is "cold start" replaced by "new model"? (F11) | **Yes.** It is never defined on screen and nothing depends on the phrase. |
| D-h | Do the legacy OCOD endpoints get deleted, or kept behind a flag? (B18) | **Deleted.** They are unreachable from the UI, they carry a second `bucket` vocabulary, and leaving them invites someone to wire them back up. |
| D-i | Does every export row need a code version, or is run id plus config version enough? (B8) | **Add it.** A config version pins the rules but not the code that read them; a published merge is defended against both. |

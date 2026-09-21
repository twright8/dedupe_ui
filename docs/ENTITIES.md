# Entities — clusters, durable IDs, group decisions, export

Implements decisions D8a (stage 2), D14 and D15 in `DESIGN.md`. This file is the contract for slice 4.

## Stage 4: cluster

Input: `units.parquet`, `unit_members.parquet`, `pairs.parquet`, the active `pair_labels`, `exact_groups.parquet`.

1. **Edges.** An edge joins two units when their pair is in the `accept` bucket after both overlays (score, import, human). Each edge keeps its source: `score`, `import` or `human`.
   Two more things make an edge, and neither depends on blocking having produced the pair:
   - **An earlier manual group** (D11). Every unit whose members carry exactly one distinct `existing_entity_id` is joined to the others carrying that same id, within its track, as `import` edges — a star from the smallest unit id, never a clique. A real group in the earlier work is a trusted merge, not a guess, so a run must not keep apart what it settled. A unit whose members carry two or more distinct ids says nothing here; it stays as it is and its cluster is flagged `mixed_ids`.
   - **A human TRUE label**, whether or not the scorer made the pair.

   A human FALSE label from the UI then removes the edge, whatever made it: a new decision beats an older imported one. `import` and `human` are the trusted edges the withheld rebuild keeps.
2. **Clusters.** Connected components over those edges, per track, by union-find. A unit with no edge is a cluster of one.
3. **Gate.** Each cluster of two or more units gets a status:

| status | when |
|---|---|
| `ok` | none of the below |
| `conflict` | a human FALSE label joins two of its units |
| `weak_link` | a scored pair inside it sits below `cluster_floor` (a linkage setting, default 0.20), so the cluster may be a chain |
| `too_large` | more units than `max_cluster_units` (a linkage setting, default 200) |
| `mixed_names` | its units show more than `count` distinct values of a named column. The setting is `max_distinct_values`, `{track: {"column": ..., "count": ...}}` in `linkage_settings.json`, and a track with no entry is not gated this way. PSC person is `{"column": "surname_clean", "count": 3}`; donations names none. The shape is the match keys' own `max_distinct` guard, so a reader who has met one has met both |
| `mixed_ids` | its records carry more than `max_existing_ids` distinct `existing_entity_id` values (default 1), so it would merge groups the earlier grouping kept apart |
| `cross_track_ids` | one earlier ID covers both a person and an organisation, so the tool keeps them as two entities |
| `attribute_tie` | two values were equally common, so one value for the whole cluster could not be settled |
| `held_key` | a guard on a match key stopped these records being put together; the group joins the same queue |

A cluster may carry several statuses. The first in the order conflict,
too_large, mixed_names, weak_link, mixed_ids, cross_track_ids is its main one.
`mixed_names` sits just under `too_large` because it asks the same question —
is this one entity at all? — of a cluster small enough to pass the size cap.
The labels a user sees for all of these are in `app/vocabulary.CLUSTER_STATUS`.

4. **What is proposed.** An `ok` cluster becomes one entity. A cluster with any other status is **withheld**: it is rebuilt from trusted edges only (`import` and `human`), and each of those smaller parts becomes an entity. The withheld cluster goes to the cluster review queue. A human decision always wins, so an edge from a human TRUE label is never withheld, and a human FALSE label always separates.
5. **Held exact groups** from stage 2 also go to the cluster review queue, with status `held_key`. Their records stay separate until a human decides.

Output `clusters.parquet`: `cluster_id`, `unit_id`, `track`, `status`, `statuses`, `withheld`, `proposed_entity_key` (the part it falls in), and `edge_source` (the strongest accepted edge inside that part, which is what stage 5 reads for `entity_basis`). `cluster_id` is `C-` plus the smallest unit ID in the cluster.

The file is one row per **unit**, so it holds the real clusters only. A held exact group is not a unit; it joins the same review queue at read time, from `exact_groups.parquet`. Two clarifications the build settled:

- `weak_link` ignores a pair a human has already decided. A reviewer who merges two units that scored 0.01 should not see the cluster come straight back.
- A human TRUE label is an edge whether or not the scorer ever made that pair, so a whole-cluster merge takes effect at the next recluster instead of waiting for a full rerun.

**Nothing in stage 4 reads a whole file.** The units, the unit members and the pairs are named to DuckDB and every edge, every group-by and `clusters.parquet` itself are SQL. `run_stage_4_cluster` binds the run's parquet files; `build_clusters` binds three frames. They run the same SQL, so the frame form is the readable description of the contract and not a second implementation.

The connected components are the one step DuckDB does not do, because SciPy owns that walk. It is given **integer unit codes**, never strings: `DENSE_RANK`-style coding in SQL turns each `unit_id` into a position in `0 .. n-1`, and `components(n_units, rows, cols)` refuses an array that is not an integer one. Two int32 arrays per edge is 8 bytes; two Python strings is about 150.

Only five columns of `units.parquet` are read — `unit_id`, `unit_size`, `track`, `existing_entity_id`, and the one column `max_distinct_values` names for the track, when it names one. The PSC units file carries sixty-odd.

**The index files (item 6).** Stage 4 also writes `clusters_index.parquet`, one row per cluster with everything the review queue shows, and stage 5 writes `entities_index.parquet`, one row per entity with what the Entities list shows. Each is built by that list's own SQL — `clusters_reader.write_index` and `entities_reader.write_index` — so the file and the query it replaces cannot drift apart. A reader uses the index when it is there and whose columns are this profile's; it rebuilds the aggregate when it is not, so a run from an older pipeline still opens. Without them, every request joined two multi-gigabyte parquets and grouped them with `list()` aggregates DuckDB cannot spill, which is what ran out of memory in the server's 6 GB budget.

## Entity IDs and the registry

The registry is durable across runs. Tables:

- `entities`: `entity_id`, `track`, `created_run`, `created_at`, `status` (`active` or `retired`), `alias_of` (the surviving entity when retired), `retired_run`
- `entity_members`: `record_id`, `entity_id`, `since_run`, `until_run` (null while current), `entity_basis`, `id_status` — the last two are copied from the proposal at publish, so "how was this decided" survives deleting the run folder
- `entity_attributes`: `entity_id`, `column_name`, `value`, `basis` (`rule`, `majority`, `raw`, `tie`, `human`), `run_id`, `since_run`, `until_run`, `rule_id`, `tally_json` — versioned like `entity_members`, so an earlier answer and the run that gave it stay on the record. `rule_id` names the derived-column rule that won; `tally_json` is `{value: count}` when the members voted. `column` is a SQL keyword, so the column is `column_name`
- `entity_edges`: the join log. One row per accepted link inside a published entity — `entity_id`, `run_id`, `source`, `record_id_a`, `record_id_b`, `unit_id_a`, `unit_id_b`, `match_key`, `match_key_id`, `group_id`, `score`, `scorer`, `model_version`, `veto_overridden`, `veto_reason`, `label_id`, `reviewer`, `decided_at`, `note`, `evidence_url`, `earlier_entity_id`, `config_version`. An exact-group merge is logged as a star from the group's smallest record id, not as every pair
- `entity_id_collisions`: the run's ID-collision list, copied in at publish
- `entity_publications`: `run_id`, `published_at`, `published_by`, `summary_json`. Publishing one run twice is then a no-op, and publishing an older run over a newer one can be refused

A run never writes the registry. It writes a **proposal**, `entities.parquet`: `record_id`, `entity_id`, `entity_basis`, `id_status`, `cluster_id`, `track`, `unit_id`, `entity_key`, plus `<column>_entity`, `<column>_entity_basis`, `<column>_entity_rule` and `<column>_entity_tally` for each consensus column. A user **publishes** a run, and only that writes the registry.

How a proposed entity gets its ID, in order:

1. If its records already belong to exactly one active registry entity, it keeps that ID.
2. If they belong to several, the survivor is chosen by the profile (donations: the lowest ID, D15). The others are retired as aliases of the survivor at publish.
3. If they belong to none, the profile mints the ID.

Profile hook `mint_entity_id(members) -> str`. Donations (D15): if members carry one `existing_entity_id`, use it. If they carry several, use the lowest. If none, use the smallest `record_id`, which already has the `TR` prefix for trusts. IDs compare as numbers after any `TR` prefix is removed, and a `TR` ID sorts after a plain one. The default hook for other profiles mints `E` plus a zero-padded counter.

**The mint hook is handed a projection.** Stage 5 no longer reads `records.parquet` whole. It reads `record_id`, `track`, `existing_entity_id`, the consensus columns with their `_rule` twins, and `stage_5_entities.MINT_COLUMNS`, which is what the two shipped mint hooks look at. A profile whose hook reads any other record column **must declare `mint_columns`** on itself, or that column will not be in the frame it is given and it will mint different IDs. On PSC this is six columns of sixty-four.

**The proposal side is a projection too.** The hook is handed `stage_5_entities.MINT_PROPOSED_COLUMNS` — `record_id`, `entity_key`, `track` — and no longer `unit_id`, `cluster_id` or `basis`. Neither shipped hook reads those three, and at the full PSC snapshot `unit_id` and `cluster_id` alone were two more copies of a 37-character id per record, about 9 GB of the mint frame. `track` stays because dropping it would stop pandas suffixing the record side's `track`, which would change every PSC organisation ID — see the next paragraph.

The frame the hook receives is still `proposed.merge(records, on="record_id")`, suffixes and all: a column both sides carry becomes `<name>_x` (the proposal) and `<name>_y` (the record). `track` is the one that collides today, which is why the PSC hook finds no `track` column and mints every ID with the person prefix. That is a defect, recorded here so it is not mistaken for a change; fixing it changes every PSC organisation ID.

**PSC mints without a loop.** `psc.mint_entity_ids` used to walk the sorted entity keys one at a time — about 11.3 million iterations at the full snapshot, each formatting a string into a dict of the same size — and to call `_single` once per group to find an agreed company number. Both are now whole-frame operations: the counter is a `cumsum` over the keys that need one, so the nth key needing an ID still takes the nth counter value and the profile's high-water mark still moves by exactly the number minted. `_single` is stated as a group-by with a distinct count. `_loop_mint` in `tests/test_psc_profile.py` keeps the old walk as the authority and the two are held together there. Note that the shipped PSC ruleset derives no `company_number_padded` column, so on a shipped run every ID takes the counter branch.

`entity_basis` says how the record reached its entity: `single` (no merge), `exact_key`, `score`, `import`, `human`, in rising order of precedence. The strongest edge on the record's path applies.

The order is `app/vocabulary.BASIS_ORDER`, and it comes from the one ordered provenance list in `GLOSSARY.md` (`DESIGN.md` D22): On its own, Match key, Score, Veto rule, Earlier grouping, Reviewer, weakest first. **Earlier grouping beats Score.** It used to be the other way round here and the right way round in `RULESET.md`, so the same two words ranked in opposite orders in two places. Settling it changed nothing about which records are together: on the donations run all 51,839 records keep the same entity ID and all 17,568 entities hold exactly the same records. It changed what 1,574 records *report* — they read `import` (Earlier grouping) instead of `score`, because their path held both kinds of edge and the earlier grouping is the stronger evidence.

A split never reuses a retired ID. When a published entity is split, the part that holds the smallest `record_id` keeps the ID and the other parts are minted. Record IDs compare as text here, as everywhere else in the pipeline.

Two proposed entities must never claim one ID. A claim the registry has already granted always beats one the profile has just minted. Between two registry claims the smallest `record_id` wins, which is the split rule above; between two minted claims the larger part wins, ties going to the smaller record ID. Every re-mint is counted and listed as an `id_collision`.

The proposal also carries `id_status` (`new`, `kept`, `survivor`, `minted_after_collision`), `unit_id` and `entity_key`, so the Entities tab and the publish preview never work an ID out twice.

## Entity-level attributes (D8a, stage 2)

A profile lists `consensus_columns` (donations: `donor_status_std`). For each entity and column:

- If any member's value was set by a rule (`<column>_rule` is not null), the most frequent rule-set value wins. Basis `rule`.
- Otherwise the most frequent value wins. Basis `majority`. A single-member entity has basis `raw`.
- A tie keeps each record's own value and marks the entity with basis `tie`. It appears in the cluster review queue with status `attribute_tie`.

The proposal stores the result as `<column>_entity` on each record, with `<column>_entity_basis`, `<column>_entity_rule` (the id of the derived-column rule that won) and `<column>_entity_tally` (how the members voted, as `{value: count}`, when more than one value was in the running). Publishing copies all four into `entity_attributes`.

## Group decisions

A reviewer decides a whole cluster or a whole held group at once. The decision is stored as ordinary `pair_labels`, so every rule about labels still holds.

- **Merge all**: TRUE labels from the smallest record ID to a representative record of every other unit (a star). Provenance `cluster_merge`.
- **Split into parts**: the reviewer assigns members to parts. Inside each part, a TRUE star. Between the smallest records of every two parts, a FALSE label. Provenance `cluster_split`. Members left unassigned get no label.
- Every label of one decision shares a `decision_id`, so the decision can be shown, superseded, or undone as a whole. It also carries `decision_scope`, the cluster or held-group ID it was about, which makes "the latest decision on this cluster" a lookup rather than a guess.

`exact_groups.parquet` gains `split_by_human` and `merged_by_human`, so a group the keys made and a group a reviewer made are told apart downstream.

**Splitting an exact group.** Stage 2 reads the active labels. When a merged exact group contains a human FALSE pair, the group is dissolved into the parts that its human TRUE labels connect. Records that no TRUE label reaches become single units. This replaces the "contradiction" report from slice 3 for groups that have been decided. A FALSE pair inside a group with no other labels still reports a contradiction.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/runs/{id}/clusters?track=&status=&withheld=&q=&sort=size\|priority\|name&order=&offset=&limit=` | the cluster review queue and the full cluster list. Items carry names, size in units and records, statuses, distinct earlier IDs, priority sums, and whether a decision exists |
| `GET /api/runs/{id}/clusters/{cluster_id}` | units with their representative rows and members, the edges between units (score, bucket, source, label), the proposed parts, evidence rows per unit behind `?events=1` |
| `POST /api/runs/{id}/clusters/{cluster_id}/decision` | body `{kind: "merge"}` or `{kind: "split", parts: [[record_id, ...], ...]}` plus `notes`, `evidence_url`. Works for held exact groups too (`cluster_id` is then the held group ID). Returns the labels written and the refreshed counts |
| `DELETE /api/runs/{id}/clusters/{cluster_id}/decision` | deactivates the labels of the latest decision |
| `POST /api/runs/{id}/recluster` | reruns stages 4 and 5 only, after decisions, without rescoring |
| `GET /api/runs/{id}/entities?track=&basis=&q=&min_size=&sort=&order=&offset=&limit=` | proposed entities: ID, size, names, basis mix, attributes, priority sums, whether the ID is new, kept, or a survivor |
| `GET /api/runs/{id}/entities/{entity_id}` | members with their records and evidence |
| `GET /api/runs/{id}/publish-preview` | what publishing would change: entities new, kept, merged (with the aliases it would create), split, and records moved. Compared with the registry |
| `POST /api/runs/{id}/publish` | writes the registry in one transaction, with an audit event. Refuses when a newer run is already published, unless `force` |
| `GET /api/registry/entities/{entity_id}` | follows aliases to the active entity. `GET /api/registry/aliases.csv` lists every retired ID and its survivor |
| `GET /api/runs/{id}/export?format=xlsx\|csv&scope=proposal\|published` | the profile's export |

## Reading these files at scale

Every read here is a query, not a load. The rules the code follows:

- A connection comes from `app/duckdb_conn.connect(temp_dir)`, never a bare `duckdb.connect()`, so it carries the memory cap, the run's own temp directory and `max_temp_directory_size`. Stages 4 and 5 clear their spill on the way in and on the way out.
- The clusters list and the entities list filter, count, sort and page **in SQL**. Building one Python dict per cluster and then showing fifty is what they used to do; a PSC run has 458,000 clusters on the 500,000-record sample and about 15 million on the full snapshot.
- `entities.parquet` carries `id_status`, so the entities list reads it from the file. The caller's `statuses` map is a fallback for a run written before that column existed.
- `GET /api/runs/{id}/entities/{entity_id}` looks the one entity up directly. It used to page the whole list first and then search it.

## Export

Profile hook `export(run, scope) -> file`. Donations: the original input sheet, row for row and column for column, plus appended columns: `EntityID`, `EntityBasis`, `DonorStatusStandardNew`, `DonorStatusBasis`, `RecordID`. `EntityID` follows D15, so it lines up with the existing `DonorIDStandardTR`. A second sheet lists aliases. A third lists the run, the config version, and the counts.

PSC: the decision table is a DuckDB join of `records.parquet` to the proposal, projected to six columns and sorted in SQL. The parquet form is written by `COPY ... TO`. The CSV and the Elasticsearch bulk file are written from **batches** of that join, because a byte-order mark and a JSON line are the two things SQL cannot write. The batch size is `EXPORT_BATCH_ROWS`, default 200,000; it is a memory ceiling and never part of the answer, and a test holds the files identical across three batch sizes. `context["entities"]` may be the proposal frame or left out, in which case the run's own `entities.parquet` is read.

## Run counts

`hasEntities`, `reviewQueue`, `idCollisions`, `publishedAt`, `clustersTotal`, `clustersWithheld`, `clustersByStatus` (object), `heldGroupsOpen`, `entitiesProposed`, `entitiesNew`, `entitiesKept`, `entitiesMerged`, `attributeTies`, `decisionsTotal`.

## Reclustering after a decision

Splink is not run again, so two questions have to be answered honestly.

**Which units has nothing ever compared?** Stage 3 writes `scored_units.parquet`
— each unit's id and size, which together fingerprint its membership, because a
unit's id is its smallest record and any change of membership changes the size.
A recluster calls a unit **unscored** when that pair is not in the file. It is
not "a unit in no pair": most units are in no pair in any run, because most
records have no candidate at all. Merging one held group of 66 units makes
exactly one unscored unit, not 11,716.

**What happens to the pairs of the units a merge replaced?** They follow their
records onto whichever unit now holds them. A pair whose two sides end up in one
unit is answered by the merge and goes; where two old pairs land on one new pair
the better score survives. Any pair that moved is marked `rescored: false`,
because Splink has not seen that pairing. Dropping them instead would mean a
merge quietly lost every candidate its members had.

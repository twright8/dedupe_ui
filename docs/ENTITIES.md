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
| `mixed_ids` | its records carry more than `max_existing_ids` distinct `existing_entity_id` values (default 1), so it would merge groups the earlier manual work kept apart |

A cluster may carry several statuses. The first in the order conflict, too_large, weak_link, mixed_ids is its main one.

4. **What is proposed.** An `ok` cluster becomes one entity. A cluster with any other status is **withheld**: it is rebuilt from trusted edges only (`import` and `human`), and each of those smaller parts becomes an entity. The withheld cluster goes to the cluster review queue. A human decision always wins, so an edge from a human TRUE label is never withheld, and a human FALSE label always separates.
5. **Held exact groups** from stage 2 also go to the cluster review queue, with status `held_key`. Their records stay separate until a human decides.

Output `clusters.parquet`: `cluster_id`, `unit_id`, `track`, `status`, `statuses`, `withheld`, `proposed_entity_key` (the part it falls in), and `edge_source` (the strongest accepted edge inside that part, which is what stage 5 reads for `entity_basis`). `cluster_id` is `C-` plus the smallest unit ID in the cluster.

The file is one row per **unit**, so it holds the real clusters only. A held exact group is not a unit; it joins the same review queue at read time, from `exact_groups.parquet`. Two clarifications the build settled:

- `weak_link` ignores a pair a human has already decided. A reviewer who merges two units that scored 0.01 should not see the cluster come straight back.
- A human TRUE label is an edge whether or not the scorer ever made that pair, so a whole-cluster merge takes effect at the next recluster instead of waiting for a full rerun.

## Entity IDs and the registry

The registry is durable across runs. Tables:

- `entities`: `entity_id`, `track`, `created_run`, `created_at`, `status` (`active` or `retired`), `alias_of` (the surviving entity when retired), `retired_run`
- `entity_members`: `record_id`, `entity_id`, `since_run`, `until_run` (null while current)
- `entity_attributes`: `entity_id`, `column_name`, `value`, `basis` (`rule`, `majority`, `raw`, `tie`), `run_id` — `column` is a SQL keyword, so the column is `column_name`
- `entity_publications`: `run_id`, `published_at`, `published_by`, `summary_json`. Publishing one run twice is then a no-op, and publishing an older run over a newer one can be refused

A run never writes the registry. It writes a **proposal**, `entities.parquet`: `record_id`, `entity_id`, `entity_basis`, `cluster_id`, `track`. A user **publishes** a run, and only that writes the registry.

How a proposed entity gets its ID, in order:

1. If its records already belong to exactly one active registry entity, it keeps that ID.
2. If they belong to several, the survivor is chosen by the profile (donations: the lowest ID, D15). The others are retired as aliases of the survivor at publish.
3. If they belong to none, the profile mints the ID.

Profile hook `mint_entity_id(members) -> str`. Donations (D15): if members carry one `existing_entity_id`, use it. If they carry several, use the lowest. If none, use the smallest `record_id`, which already has the `TR` prefix for trusts. IDs compare as numbers after any `TR` prefix is removed, and a `TR` ID sorts after a plain one. The default hook for other profiles mints `E` plus a zero-padded counter.

`entity_basis` says how the record reached its entity: `single` (no merge), `exact_key`, `import`, `score`, `human`, in rising order of precedence. The strongest edge on the record's path applies.

A split never reuses a retired ID. When a published entity is split, the part that holds the smallest `record_id` keeps the ID and the other parts are minted. Record IDs compare as text here, as everywhere else in the pipeline.

Two proposed entities must never claim one ID. A claim the registry has already granted always beats one the profile has just minted. Between two registry claims the smallest `record_id` wins, which is the split rule above; between two minted claims the larger part wins, ties going to the smaller record ID. Every re-mint is counted and listed as an `id_collision`.

The proposal also carries `id_status` (`new`, `kept`, `survivor`, `minted_after_collision`), `unit_id` and `entity_key`, so the Entities tab and the publish preview never work an ID out twice.

## Entity-level attributes (D8a, stage 2)

A profile lists `consensus_columns` (donations: `donor_status_std`). For each entity and column:

- If any member's value was set by a rule (`<column>_rule` is not null), the most frequent rule-set value wins. Basis `rule`.
- Otherwise the most frequent value wins. Basis `majority`. A single-member entity has basis `raw`.
- A tie keeps each record's own value and marks the entity with basis `tie`. It appears in the cluster review queue with status `attribute_tie`.

The proposal stores the result as `<column>_entity` on each record, with `<column>_entity_basis`.

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

## Export

Profile hook `export(run, scope) -> file`. Donations: the original input sheet, row for row and column for column, plus appended columns: `EntityID`, `EntityBasis`, `DonorStatusStandardNew`, `DonorStatusBasis`, `RecordID`. `EntityID` follows D15, so it lines up with the existing `DonorIDStandardTR`. A second sheet lists aliases. A third lists the run, the config version, and the counts.

## Run counts

`hasEntities`, `reviewQueue`, `idCollisions`, `publishedAt`, `clustersTotal`, `clustersWithheld`, `clustersByStatus` (object), `heldGroupsOpen`, `entitiesProposed`, `entitiesNew`, `entitiesKept`, `entitiesMerged`, `attributeTies`, `decisionsTotal`.

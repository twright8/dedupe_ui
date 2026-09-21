# Entities API — clusters, entity IDs, publishing and export

Stage 4 groups the scored pairs into **clusters** and puts the doubtful ones in
a review queue. Stage 5 turns the settled ones into a **proposal**: one entity
ID per record. Publishing writes that proposal into the durable **registry**.
`docs/ENTITIES.md` is the contract; this file is the wire.

Names carry over from `docs/PAIRS_API.md` unchanged: `unit_id`, `track`,
`priority`, `existing_entity_ids`, `events`, `event_columns`, `columns`.

Every example below is a real response from a run over the September 2026
donations sheet: 51,839 records, 22,435 units, 28,843 scored pairs.

All endpoints need the session cookie. Every response is plain JSON with no NaN
in it: a missing number or string is `null`.

| Endpoint | Purpose |
|---|---|
| `GET /api/runs/{id}/clusters` | the cluster review queue and the full list |
| `GET /api/runs/{id}/clusters/{cluster_id}` | one cluster: units, edges, parts, evidence |
| `POST /api/runs/{id}/clusters/{cluster_id}/decision` | merge a cluster, or split it into parts |
| `POST /api/runs/{id}/clusters/{cluster_id}/attribute` | settle a consensus column for the whole cluster |
| `DELETE /api/runs/{id}/clusters/{cluster_id}/decision` | undo the latest decision as a whole |
| `POST /api/runs/{id}/recluster` | redo stages 4 and 5 after decisions, without rescoring |
| `GET /api/runs/{id}/entities` | the proposed entities |
| `GET /api/runs/{id}/entities/{entity_id}` | one entity's members, records and evidence |
| `GET /api/runs/{id}/publish-preview` | what publishing would change |
| `POST /api/runs/{id}/publish` | write the registry, in one transaction |
| `GET /api/registry/entities/{entity_id}` | follow aliases to the live entity |
| `GET /api/registry/aliases.csv` | every retired ID and its survivor |
| `GET /api/runs/{id}/export` | the profile's export file |

A run that has not clustered returns **404** with
`{"detail": "Run has no clusters yet"}`; one that has not reached stage 5
returns `{"detail": "Run has no entities yet"}`. An unknown run id is 404 with
`{"detail": "Run not found"}`.

## Cluster ids

`cluster_id` is `C-` plus the smallest unit ID in the cluster. A **held exact
group** from stage 2 sits in the same queue under its own `H-<key>-<id>` id, and
every cluster endpoint accepts either kind.

## `GET /api/runs/{id}/clusters`

### Query

| Name | Values | Default |
|---|---|---|
| `track` | `person`, `organisation` | every track |
| `status` | `ok`, `conflict`, `too_large`, `mixed_names`, `weak_link`, `mixed_ids`, `cross_track_ids`, `held_key`, `attribute_tie` | every status |
| `withheld` | `yes`, `no` | both |
| `decided` | `yes`, `no` | both |
| `min_units` | 1 or more | 2 — the queue is about clusters, not singletons |
| `q` | case-insensitive substring of a member name, a unit id or the cluster id | none |
| `sort` | `size`, `records`, `priority`, `name` | `size` |
| `order` | `asc`, `desc` | `desc` |
| `offset`, `limit` | `limit` 1 to 200 | 0, 50 |

`min_units=1` includes the clusters of one unit, which is most of a run and is
what the full list shows. Anything else a caller sends for `status`, `track`,
`withheld`, `decided`, `sort` or `order` is **400**.

### Response

`total` follows the filters. `counts` describe the whole run and ignore them.

```json
{
  "total": 214,
  "offset": 0,
  "limit": 2,
  "items": [
    {
      "cluster_id": "C-102719",
      "track": "person",
      "status": "mixed_ids",
      "statuses": ["mixed_ids"],
      "withheld": true,
      "n_units": 4,
      "n_records": 11,
      "existing_entity_ids": ["11578", "93648"],
      "n_existing_ids": 2,
      "names": ["John James", "John E James", "John Edward James"],
      "priority": { "total_value": 191163.15 },
      "parts": 2,
      "decision": null,
      "guard": null,
      "attribute_basis": { "donor_status_std": "raw" }
    },
    {
      "cluster_id": "H-k3-40214",
      "track": "person",
      "status": "held_key",
      "statuses": ["held_key"],
      "withheld": true,
      "n_units": 141,
      "n_records": 141,
      "existing_entity_ids": ["14332"],
      "n_existing_ids": 1,
      "names": ["A Bown", "Mr A Bown", "Andrew Bown"],
      "priority": { "total_value": 2841000.0 },
      "parts": 141,
      "decision": null,
      "guard": "max_group_size:141>60",
      "attribute_basis": {}
    }
  ],
  "counts": {
    "all": 18744,
    "reviewable": 214,
    "withheld": 73,
    "decided": 0,
    "ok": 18530,
    "conflict": 0,
    "too_large": 1,
    "mixed_names": 0,
    "weak_link": 58,
    "mixed_ids": 14,
    "held_key": 141,
    "attribute_tie": 0,
    "cross_track_ids": 6,
    "decisions": 0,
    "person": 12406,
    "organisation": 6338
  },
  "columns": [
    { "key": "unit_id", "label": "unit_id", "type": "text", "source": "cleaning" },
    { "key": "name", "label": "Donor", "type": "text", "source": "profile" }
  ],
  "priority_columns": ["total_value"]
}
```

Field notes:

- `status` is the main one; `statuses` is every status the gate raised, because a
  cluster can be both too large and mixed. The order of precedence is
  `conflict`, `too_large`, `mixed_names`, `weak_link`, `mixed_ids` (ENTITIES.md).
- `withheld` says the cluster was **not** proposed as one entity. It was rebuilt
  from the trusted edges alone — import and human — and each part became an
  entity. `parts` is how many entities it produced.
- `names` is up to 5 distinct member names; `existing_entity_ids` up to 5.
- `decision` is `null`, or `{decision_id, kind, reviewer, created_at, n_labels,
  notes, evidence_url}` for the latest decision on this cluster.
- `attribute_basis` is how each consensus column was settled for this cluster's
  entities: `rule`, `majority`, `raw` or `tie`.
- `counts.reviewable` is what the queue still holds: every withheld cluster and
  held exact group **without a decision**. `counts.decided` is how many of those
  a human has settled, and `counts.decisions` is every decision in the run. A
  decided item leaves the queue, so the number falls as the work is done.
- `guard` is the reason a match key held a group back, for a `held_key` item —
  `max_group_size:141>60`, say. It is `null` for a real cluster.
- `cross_track_ids` marks a cluster carrying an earlier manual ID that the other
  track also claims: one group spanning a person and their own company. The tool
  never suggests a merge across tracks (D5), so both halves stay separate
  entities and one ID is re-minted. The status exists so the owner can find them.

## `GET /api/runs/{id}/clusters/{cluster_id}`

Adds the units in full, the pairs between them, and the parts the gate proposed.
`?events=1` adds each unit's evidence rows (D13b), newest first, capped at 200
per unit with `events_truncated`.

```json
{
  "cluster_id": "C-102719",
  "track": "person",
  "status": "mixed_ids",
  "statuses": ["mixed_ids"],
  "withheld": true,
  "n_units": 4,
  "n_records": 11,
  "existing_entity_ids": ["11578", "93648"],
  "priority": { "total_value": 191163.15 },
  "decision": null,
  "units": [
    {
      "unit_id": "102719",
      "unit_size": 9,
      "existing_entity_ids": "11578 | 93648",
      "n_existing_ids": 2,
      "name": "John James",
      "track": "person",
      "total_value": 141163.15,
      "proposed_entity_key": "102719",
      "members": [
        { "record_id": "102719", "name": "John James", "n_donations": 1,
          "total_value": 15000.0, "donor_status_std": "Individual" }
      ],
      "members_truncated": false
    }
  ],
  "edges": [
    { "pair_id": "102719|103054", "unit_id_l": "102719", "unit_id_r": "103054",
      "match_probability": 0.9190644788771419, "match_weight": 3.505321161060017,
      "bucket": "accept", "score_bucket": "review", "decided_by": "human",
      "source": "human", "inside_part": true,
      "label": { "is_match": "TRUE", "reviewer": "Tom",
                 "created_at": "2026-09-18T13:50:26.524783+00:00",
                 "notes": "same donor", "evidence_url": null,
                 "provenance": "cluster_merge", "held_out": 0,
                 "decision_id": "d_5f2c1f0a" } }
  ],
  "parts": [
    { "proposed_entity_key": "102719", "unit_ids": ["102719", "103054"],
      "n_records": 10 },
    { "proposed_entity_key": "98800", "unit_ids": ["98800", "99117"],
      "n_records": 1 }
  ],
  "weak_pairs": [
    { "pair_id": "98800|99117", "match_probability": 0.1721, "bucket": "reject" }
  ],
  "attributes": {
    "donor_status_std": { "value": "Individual", "basis": "majority",
                          "values": { "Individual": 10, "Other": 1 } }
  },
  "columns": [
    { "key": "unit_id", "label": "unit_id", "type": "text", "source": "cleaning" }
  ],
  "event_columns": []
}
```

- `edges` are the pairs whose two units are both in this cluster, accepted or
  not, so the reviewer can see the weak link that made it a chain. `source` is
  `human`, `import` or `score` for an accepted edge and `null` for one that was
  not accepted. `inside_part` says the edge survived into a proposed part.
- `weak_pairs` lists the internal pairs below `cluster_floor`, weakest first,
  and is empty unless the status is `weak_link`.
- For a held exact group the `units` are its member records as single units,
  `edges` is empty, and `parts` is one part per record.

## `POST /api/runs/{id}/clusters/{cluster_id}/decision`

One reviewer decision about a whole cluster, stored as ordinary `pair_labels`
(ENTITIES.md) so every rule about labels still applies. Every label of one
decision shares a `decision_id`.

Merge everything:

```json
{ "kind": "merge", "notes": "One donor, three spellings",
  "evidence_url": "https://www.example.org/report" }
```

Split into parts. Each part is a list of **record ids**; a member left out of
every part gets no label at all:

```json
{ "kind": "split",
  "parts": [["102719", "102754"], ["103054"]],
  "notes": "Two different John Jameses" }
```

A merge writes a TRUE star from the smallest record id of the cluster to a
representative record of every other unit. A split writes a TRUE star inside
each part and a FALSE label between the smallest records of every two parts.
Both go through the same append-only path as a single label, so a second
decision on the same cluster supersedes the first pair by pair.

```json
{
  "decision_id": "d_5f2c1f0a",
  "kind": "merge",
  "cluster_id": "C-102719",
  "labels_written": 3,
  "superseded": 0,
  "needs_recluster": true,
  "counts": { "...": "the run's refreshed counts" }
}
```

A merge writes **one label per unit**, not per record: a star from the smallest
record of the cluster to the smallest record of every other unit. Records inside
one unit are already one thing, so labelling them again would say nothing. A
26-unit cluster therefore writes 25 labels, whatever its record count.

A cluster a human has merged is no longer withheld: the gate's `too_large`,
`mixed_names`, `weak_link` and `mixed_ids` all stand down, because a decision
always wins.

`needs_recluster` is always true: the decision has changed the labels, and the
clusters and entities are stale until `POST /recluster` runs. **400** for a
`kind` other than `merge` or `split`, a split with fewer than two parts, a part
naming a record that is not in this cluster, or a record in two parts. A split
of more than 500 labels is **422**, the same cap as the pairs API.

## `DELETE /api/runs/{id}/clusters/{cluster_id}/decision`

Deactivates every label of the latest decision on that cluster, as a unit.
Append-only, so the rows stay with `active = 0`. **404** when the cluster has no
decision.

```json
{ "decision_id": "d_5f2c1f0a", "labels_withdrawn": 3,
  "attributes_withdrawn": 0,
  "needs_recluster": true, "counts": { "...": "refreshed" } }
```

It undoes an attribute decision too, when that is the latest one on the cluster:
`labels_withdrawn` is then 0 and `attributes_withdrawn` says how many records
were freed.

## `POST /api/runs/{id}/clusters/{cluster_id}/attribute`

Settle a consensus column the entity could not settle itself — what an
`attribute_tie` is waiting for. It works on any cluster, not only a tied one.

```json
{ "column": "donor_status_std", "value": "Unincorporated Association",
  "notes": "A members' club, not a company" }
```

The answer is stored against every **record** of the cluster, not against the
entity, so it survives a rerun that groups those records differently — the same
reasoning as a pair label. The consensus order becomes: the human value, then a
value a rule set, then the majority. The column's basis reads `human`, and the
export's `DonorStatusBasis` says `human` for those rows.

```json
{
  "decision_id": "d_9b41c7e2",
  "column": "donor_status_std",
  "value": "Unincorporated Association",
  "records_set": 7,
  "needs_recluster": true
}
```

**400** for a column the profile does not list as a consensus column, **404**
for a cluster this run does not have.

`GET /api/labels` carries `decision_id` on every item and takes
`decision_id=<id>` as a filter, so one decision's labels can be listed.

## `POST /api/runs/{id}/recluster`

Redoes the human overlay on the exact groups, the units when those changed,
then stages 4 and 5. Splink is **not** rerun: the scored pairs are reused for
every unit that still exists.

```json
{
  "ok": true,
  "exact_groups_rebuilt": true,
  "units_rebuilt": true,
  "unscored_units": 3,
  "unscored_note": "3 unit(s) were created by a split and have no scored pairs. Rerun the pipeline to score them.",
  "elapsed_seconds": 11.4,
  "counts": { "...": "the run's refreshed counts" }
}
```

A split can create units the scorer never saw. Those units are clustered on
their human edges alone and reported as `unscored_units`; a full rerun is what
gives them scores. `unscored_note` is `null` when there are none.

## `GET /api/runs/{id}/entities`

### Query

| Name | Values | Default |
|---|---|---|
| `track` | `person`, `organisation` | every track |
| `basis` | `single`, `exact_key`, `import`, `score`, `human` | every basis |
| `id_status` | `new`, `kept`, `survivor`, `minted_after_collision` | every one |
| `min_size` | records per entity | 1 |
| `q` | substring of a name, a record id or the entity id | none |
| `sort` | `size`, `priority`, `name`, `entity_id` | `size` |
| `order`, `offset`, `limit` | `limit` 1 to 200 | `desc`, 0, 50 |

```json
{
  "total": 18744,
  "offset": 0,
  "limit": 1,
  "items": [
    {
      "entity_id": "1",
      "entity_basis": "exact_key",
      "bases": { "exact_key": 1115, "score": 3 },
      "track": "organisation",
      "cluster_id": "C-1",
      "n_records": 1118,
      "n_units": 2,
      "names": ["Joseph Rowntree Reform Trust Ltd"],
      "existing_entity_ids": ["1", "TR1"],
      "priority": { "total_value": 12390601.42 },
      "id_status": "new",
      "attributes": {
        "donor_status_std": { "value": "Company", "basis": "rule" }
      }
    }
  ],
  "counts": {
    "all": 18744,
    "single": 15012,
    "exact_key": 2907,
    "import": 613,
    "score": 205,
    "human": 7,
    "new": 18744,
    "kept": 0,
    "survivor": 0,
    "minted_after_collision": 5,
    "person": 12406,
    "organisation": 6338,
    "attribute_ties": 0
  },
  "columns": [
    { "key": "record_id", "label": "record_id", "type": "text", "source": "cleaning" }
  ],
  "priority_columns": ["total_value"]
}
```

- `entity_basis` is the strongest way any member reached this entity, in the
  order `single`, `exact_key`, `import`, `score`, `human`. `bases` counts the
  records by their own basis.
- `id_status` is `new` (nothing in the registry claimed these records), `kept`
  (rule 1 — one active entity already held them), `survivor` (rule 2 — several
  did and this one wins, the rest become aliases), or
  `minted_after_collision` (two proposed entities both claimed one earlier ID
  and this is the one that gave way).

`GET /api/runs/{id}/entities/{entity_id}` returns the same item plus `members`
(the full record rows, capped at 500, with `members_truncated`), `unit_ids`, and
`events` / `event_columns` behind `?events=1`.

## `GET /api/runs/{id}/publish-preview`

What publishing this run would do to the registry. Nothing is written.

```json
{
  "run_id": "run_2026_09_18a",
  "registry_entities": 0,
  "published_at": null,
  "published_by": null,
  "can_publish": true,
  "blocked_by": null,
  "summary": {
    "new": 18744, "kept": 0, "merged": 0, "split": 0,
    "aliases": 0, "records_moved": 0, "records_total": 51839,
    "id_collisions": 5, "retired": 0
  },
  "new_examples": [
    { "entity_id": "1", "n_records": 1118, "names": ["Joseph Rowntree Reform Trust Ltd"] }
  ],
  "kept_examples": [
    { "entity_id": "600", "n_records": 4, "names": ["Acme Ltd"] }
  ],
  "alias_examples": [
    { "retired_entity_id": "9312", "survivor_entity_id": "600",
      "names": ["Acme Ltd", "Acme Limited"] }
  ],
  "merged_examples": [
    { "entity_id": "600", "absorbs": ["9312"], "n_records": 7,
      "names": ["Acme Ltd", "Acme Limited"] }
  ],
  "split_examples": [
    { "entity_id": "4821", "keeps_records": 3, "new_entities": ["58210"],
      "names": ["J Smith"] }
  ],
  "moved_examples": [
    { "record_id": "58210", "from_entity_id": "4821", "to_entity_id": "58210",
      "name": "John Smith" }
  ],
  "id_collision_examples": [
    { "existing_entity_id": "8842", "kept_by": "8842", "n_records_kept": 4,
      "minted": "93117", "n_records_minted": 1 }
  ]
}
```

- `kept` counts entities whose ID rule 1 handed back unchanged. `merged` counts
  entities that absorb one or more registry entities — each absorbed ID becomes
  an alias. `split` counts registry entities this run breaks up: the part with
  the smallest `record_id` keeps the ID, the rest are minted.
- `records_moved` counts records whose entity ID changes.
- `can_publish` is false with `blocked_by` set when a **newer** run is already
  published — publishing an older run would undo newer decisions. `force: true`
  on the POST overrides it.
- `published_at` and `published_by` describe the **latest** publication in the
  registry, whichever run made it, or are null when nothing has been published.
- Each `*_examples` list holds at most 20. `kept_examples` are the entities rule
  1 hands back unchanged; `alias_examples` are the redirects a publish would
  create, one per absorbed ID.

## `POST /api/runs/{id}/publish`

Body `{"force": false}` (the default). Writes `entities`, `entity_members` and
`entity_attributes` in one SQLite transaction and records an audit event.
Publishing the same run twice is a no-op that returns the same summary with
`"already": true`.

```json
{
  "ok": true,
  "run_id": "run_2026_09_18a",
  "published_at": "2026-09-18T15:02:44.119820+00:00",
  "published_by": "Tom",
  "already": false,
  "summary": { "new": 18744, "kept": 0, "merged": 0, "split": 0,
               "aliases": 0, "records_moved": 0, "records_total": 51839,
               "id_collisions": 5, "retired": 0 }
}
```

**409** when a newer run is published and `force` is false:
`{"detail": {"kind": "newer_run_published", "run_id": "run_2026_09_19b", "published_at": "..."}}`.

## `GET /api/registry/entities/{entity_id}`

Follows the alias chain to the live entity, however long it is (A→B then B→C
answers C for A).

```json
{
  "requested": "9312",
  "entity_id": "600",
  "track": "organisation",
  "status": "active",
  "redirected": true,
  "chain": ["9312", "600"],
  "created_run": "run_2026_09_18a",
  "created_at": "2026-09-18T15:02:44.119820+00:00",
  "n_records": 7,
  "records": ["3", "4", "12"],
  "members": [
    { "record_id": "3", "entity_basis": "import", "id_status": "new",
      "since_run": "run_2026_09_18a",
      "entity_basis_label": "Earlier grouping", "id_status_label": "New" }
  ],
  "attributes": {
    "donor_status_std": {
      "value": "Company", "basis": "rule", "basis_label": "Derived column rule",
      "rule_id": "d1r4", "tally": null, "since_run": "run_2026_09_18a"
    }
  }
}
```

**404** when the ID has never existed. A chain that loops is cut and reported as
`{"detail": "Alias chain for '9312' does not end"}` (500) rather than hanging.

## How this was decided

Two endpoints answer the same question — *why are these records one entity?* —
from two different places. Use the run one while the run exists. Use the
registry one for anything published, because it keeps working after the run
folder is deleted.

Both return the answer twice. `steps` is an ordered list a person can read,
**weakest evidence first**, so it reads as the story of the merge. `edges` is
the join log the steps were written from, one row per link, with only the
fields that kind of link fills in. `question` and `precedence` are the words to
print above the list; they come from `GET /api/vocabulary`.

`n_edges` is how many links the entity has. `edges` is one page of them, capped
by `limit`, and `edges_truncated` says whether anything was left out. `steps`
always describes the **whole** merge: `n_links` on a step is the true count even
when the page is capped, and a step appears for every kind of link the entity
has, with no `examples` when the page holds none of that kind. So
`sum(steps[].n_links)` equals `n_edges`, and neither has to be guessed from the
other.

### `GET /api/runs/{id}/entities/{entity_id}/provenance`

Built from the run's own files. `limit` (default 2000, max 20000) caps `edges`;
`steps` always counts every link.

A real response, from the donations run `run_2026_09_18a`, entity `833`
(86 records, shortened to two edges and two members):

```json
{
  "entity_id": "833",
  "source": "run",
  "run_id": "run_2026_09_18a",
  "config_version": 1,
  "n_records": 86,
  "n_edges": 95,
  "edges_truncated": false,
  "id_status": "new",
  "id_status_label": "New",
  "question": "How it was decided",
  "precedence": "Read the list from the top down. Anything lower beats anything above it, so a reviewer's answer beats the earlier grouping, the earlier grouping beats a veto rule, and a veto rule beats the score.",
  "steps": [
    { "order": 0, "source": null, "label": "This entity",
      "text": "This entity holds 86 records.", "n_links": 0 },
    { "order": 1, "source": "exact_key", "label": "Match key",
      "definition": "A match key found the same values in every one of its columns.",
      "text": "A match key put these records together: Company number, Name and postcode. That is 80 links.",
      "n_links": 80,
      "examples": [
        { "record_id_a": "10016", "record_id_b": "10305", "source": "exact_key",
          "match_key": "Name and postcode", "match_key_id": "k2",
          "group_id": "X-10016" }
      ] },
    { "order": 2, "source": "score", "label": "Score",
      "definition": "The score reached the accept line.",
      "text": "5 links were accepted on the Splink score. The scores ran from 1.00 to 1.00.",
      "n_links": 5,
      "examples": [
        { "record_id_a": "10016", "record_id_b": "101777", "source": "score",
          "score": 0.9999995088663194, "scorer": "splink" }
      ] },
    { "order": 3, "source": "import", "label": "Earlier grouping",
      "definition": "Both sides already carried the same earlier ID.",
      "text": "10 links were accepted because both sides already carried the same earlier ID: 833.",
      "n_links": 10,
      "examples": [
        { "record_id_a": "10016", "record_id_b": "11212", "source": "import",
          "score": 0.9999995088663194, "earlier_entity_id": "833" }
      ] },
    { "order": 4, "source": null, "label": "Where this ID came from",
      "text": "New. Nothing in the registry claimed these records, so the tool made a new ID.",
      "n_links": 0 }
  ],
  "edges": [
    { "entity_id": "833", "run_id": "run_2026_09_18a", "source": "exact_key",
      "record_id_a": "10016", "record_id_b": "10305",
      "unit_id_a": null, "unit_id_b": null,
      "match_key": "Name and postcode", "match_key_id": "k2",
      "group_id": "X-10016",
      "score": null, "scorer": null, "model_version": null,
      "veto_overridden": null, "veto_reason": null,
      "label_id": null, "reviewer": null, "decided_at": null,
      "note": null, "evidence_url": null,
      "earlier_entity_id": null, "config_version": 1 }
  ],
  "members": [
    { "record_id": "10016", "entity_basis": "import",
      "entity_basis_label": "Earlier grouping" }
  ]
}
```

**404** when the run has no entities yet, or proposes no entity with that ID.

### `GET /api/registry/entities/{entity_id}/provenance`

The same answer from the registry alone. It follows the alias chain, so a
retired ID works. It adds the settled values, their history, and any time this
entity's ID was claimed twice.

```json
{
  "entity_id": "833",
  "requested": "833",
  "redirected": false,
  "chain": ["833"],
  "source": "registry",
  "created_run": "run_2026_09_18a",
  "runs": ["run_2026_09_18a"],
  "n_records": 86,
  "n_edges": 95,
  "edges_truncated": false,
  "id_status": "new",
  "id_status_label": "New",
  "question": "How it was decided",
  "precedence": "Read the list from the top down. Anything lower beats anything above it, so a reviewer's answer beats the earlier grouping, the earlier grouping beats a veto rule, and a veto rule beats the score.",
  "steps": [ "… the same five steps as above …" ],
  "edges": [
    { "id": 27526, "entity_id": "833", "run_id": "run_2026_09_18a",
      "source": "exact_key", "record_id_a": "10016", "record_id_b": "10305",
      "match_key": "Name and postcode", "match_key_id": "k2",
      "group_id": "X-10016", "config_version": 1 }
  ],
  "members": [
    { "record_id": "10016", "entity_basis": "import", "id_status": "new",
      "since_run": "run_2026_09_18a",
      "entity_basis_label": "Earlier grouping", "id_status_label": "New" }
  ],
  "attributes": {
    "donor_status_std": {
      "value": "Company", "basis": "rule", "basis_label": "Derived column rule",
      "rule_id": "d1r4", "tally": null, "since_run": "run_2026_09_18a"
    }
  },
  "attribute_history": [
    { "column_name": "donor_status_std", "value": "Company", "basis": "rule",
      "rule_id": "d1r4", "tally_json": null, "tally": null,
      "since_run": "run_2026_09_18a", "until_run": null,
      "run_id": "run_2026_09_18a" }
  ],
  "id_collisions": []
}
```

**404** when the ID has never existed.

### An edge, field by field

| Field | Filled in when | Holds |
|---|---|---|
| `source` | always | one of `exact_key`, `score`, `model`, `veto`, `import`, `human` — the value `GET /api/vocabulary` maps to a label |
| `record_id_a`, `record_id_b` | always | the two records. For a match-key merge, `record_id_a` is the group's smallest record id |
| `unit_id_a`, `unit_id_b` | a scored pair | the two units the scorer compared |
| `match_key`, `match_key_id`, `group_id` | `exact_key` | the key's name, its id, and the exact group |
| `score`, `scorer` | `score`, `model` | the score, and `splink` or `model` |
| `model_version` | `model` | which model produced the score |
| `veto_overridden`, `veto_reason` | a veto was on the pair | the veto the accepted link overrode |
| `label_id`, `reviewer`, `decided_at`, `note`, `evidence_url` | `human` | the saved answer and who saved it |
| `earlier_entity_id` | `import` | the earlier ID both sides carried |
| `config_version` | always | the config the run used |

**Why an exact group is a star.** A match key that puts 1,000 records together
makes 499,500 pairs and 999 links. The log keeps the 999: the group's smallest
record id joined to each of the others. That says all 1,000 are one group, and
it is what a reader wants to see.

`GET /api/registry/aliases.csv` is `text/csv` with
`retired_entity_id,survivor_entity_id,track,retired_run,retired_at`, one row per
retired entity, and resolves to the **final** survivor so a client never has to
walk a chain.

## `GET /api/runs/{id}/export`

| Name | Values | Default |
|---|---|---|
| `format` | `xlsx`, `csv` | `xlsx` |
| `scope` | `proposal`, `published` | `proposal` |

Streams the file. `scope=published` reads the registry instead of the run's
proposal and is **409** when the run has not been published.

Donations: the original input sheet, row for row and column for column in its
original order, plus `RecordID`, `EntityID`, `EntityBasis`,
`DonorStatusStandardNew` and `DonorStatusBasis`. A row the loader dropped —
no `DonorId` — stays in place with those five cells blank. Sheet 2 is the
aliases, sheet 3 the run, its config version, the counts, the export time and
the scope. The CSV is sheet 1 only.

Other profiles get the records frame plus the same five columns, as CSV.

## Run counts

`GET /api/runs/{id}` and `GET /api/runs` carry these inside `counts`, beside the
slice-3 ones:

| Key | Meaning |
|---|---|
| `clustersTotal` | clusters, singletons included |
| `clustersWithheld` | clusters the gate did not propose as one entity |
| `clustersByStatus` | `{ok, conflict, too_large, mixed_names, weak_link, mixed_ids, attribute_tie}` |
| `heldGroupsOpen` | held exact groups still waiting for a decision |
| `reviewQueue` | withheld clusters + open held groups + attribute ties |
| `entitiesProposed`, `entitiesNew`, `entitiesKept`, `entitiesMerged` | the proposal against the registry |
| `attributeTies` | entities whose consensus column could not be settled |
| `idCollisions` | entities re-minted because two claimed one earlier ID |
| `decisionsTotal` | active cluster decisions |
| `crossTrackIds` | clusters carrying an earlier ID the other track also claims |
| `hasEntities` | true once stage 5 has run |
| `publishedAt` | when this run was published, or `null` |

## `GET /api/runs/{id}/score-eval`

Served by the pairs API (`docs/PAIRS_API.md`), and slice 4 adds two keys to it.
A label write rewrites this file, so both are carried over rather than dropped.

```json
{
  "exact_only":  { "pair_precision": 0.998394, "pair_recall": 0.664288 },
  "score_only":  { "pair_precision": 0.970064, "pair_recall": 0.772776 },
  "with_human":  { "pair_precision": 0.969327, "pair_recall": 0.788848 },
  "entities": {
    "circular": "The imported labels are both an input and the yardstick here: every earlier group joins its units as trusted import edges (D11), so this set and with_human score near 1.0 recall by construction. Tune on score_only, which leaves the import overlay out.",
    "entities_after": 17505,
    "pair_precision": 0.998809,
    "pair_recall": 0.987519,
    "conflicts": 123,
    "by_track": { "person": {}, "organisation": {} }
  },
  "versus_existing_entity_id": {
    "labelled_records": 41608,
    "identical": 0.985796,
    "different": 0.014204,
    "identical_records": 41017,
    "different_records": 591,
    "reasons": { "merged_two_earlier_groups": 546, "split": 362,
                 "collision_re_mint": 0 },
    "reason_shares": { "merged_two_earlier_groups": 0.013122,
                       "split": 0.008701, "collision_re_mint": 0.0 },
    "circular": "The earlier ids are an input to the run as well as the yardstick here (D11), so a high share identical is agreement, not accuracy."
  }
}
```

`entities` and `with_human` are **circular**: the imported labels join the units
in the first place, so they score near 1.0 recall by construction. `score_only`
is the figure to tune rules against, and both sets say so in their own `circular`
note rather than leaving a reader to work it out.

## Two limits worth knowing

**A cluster detail sends at most 200 units.** `n_units` and `n_records` are the
**true** size, so they match the queue item; `units_shown` is how many the
response carries and `units_truncated` says whether more exist. A held group of
248 units therefore reports `n_units: 248, units_shown: 200,
units_truncated: true`.

A decision is not limited by that. `{kind: "merge"}` covers the whole cluster or
group, listed or not, because the server works the members out for itself. A
`{kind: "split"}` names records explicitly, and any member the reviewer does not
put in a part is left unassigned and gets no label — so splitting a group longer
than the page needs the parts to be built from more than one page.

**`publish-preview` is cached per run.** It reads the whole proposal and the
whole registry, so a screen that polls it would pay for that each time. The
cache key carries the proposal file's size and modification time and how many
publications and retirements the registry holds, so a recluster or a publish
invalidates it by itself — no call has to remember to.

# Glossary — one name per thing

Proposed. Nothing here is applied yet. `docs/TERMINOLOGY_AUDIT.md` holds the
evidence and the work list.

Rules this file follows. One canonical term per concept. Plain words, short
sentences. Every definition stands on its own. A term that differs by profile is
marked, and the profile supplies its own noun through `GET /api/profile`.

Reading the table: **Term** is the word to use on screen and in the docs.
**Retire** lists the words to stop using for that concept. **Fields** names the
API and file fields that hold it. Field names are not renamed unless the entry
says so: prefer changing the words on screen.

## The things

| Term (plural) | Plain definition | Retire | Fields | Where it appears |
|---|---|---|---|---|
| **record** (records) | One row of the file you loaded. | row, entry | `record_id`; `records.parquet` | Records tab, every table. Profile noun: donations "donor", PSC "PSC record" (`nouns.record`) |
| **track** (tracks) | A group of records matched only against each other. This tool has two: people and organisations. | partition, side, class | `track`; `track_rules` | Every filter, Config tabs |
| **cleaning step** (cleaning steps) | One ordered change to a value, writing a new column beside the raw one. | cleaning rule, standardisation step | `ruleset.cleaning[]` | Config → Cleaning rules (rename the tab to "Cleaning steps") |
| **derived column** (derived columns) | A new column whose value ordered rules set, used to standardise a category the source often gets wrong. | standard value, standardised category | `ruleset.derived_columns[]`, `<target>_rule` | Config → Derived columns |
| **match key** (match keys) | A named set of columns. Records holding the same value in every one of them are put together. | exact key, key, deterministic rule | `ruleset.match_keys[]`, `key_ids` | Config → Match keys |
| **guard** (guards) | A limit on a match key. A group that breaks the limit is not merged. | cap, check | `guards`, `on_guard_fail`, `guard` | Config → Match keys, Exact groups |
| **exact group** (exact groups) | A set of records a match key put together. | merged group, match-key group | `exact_groups.parquet` `status = merged`, `group_id` `X-…` | Exact groups tab |
| **held group** (held groups) | A set of records a match key would have put together, stopped by a guard, waiting for a person. | held-by-a-key group, withheld group, held for review | `status = held`, `group_id` `H-…`, cluster `status = held_key` | Exact groups tab, Cluster review |
| **unit** (units) | What the scorer compares: one exact group, or one record on its own. | representative, node | `unit_id`, `units.parquet`, `unit_size` | Review, Cluster review, run summary |
| **pair** (pairs) | Two units the scorer compared. | candidate pair, scored pair, edge, link | `pair_id`, `pairs.parquet` | Review, Cluster review |
| **score** (scores) | A number from 0 to 1 for one pair. It reads as the chance the two units are the same thing. | match probability, probability, match weight | `match_probability` (Splink), `gbt_score` (model) | Review, histogram, Cluster review |
| **Splink score** | The score the unsupervised engine works out from the shape of the data. Name it only where both scores are shown. | the probability, the raw score | `match_probability` | Review, Diagnostics |
| **model score** | The score the trained model works out from saved labels. Name it only where both scores are shown. | GBT score, gbt_score, the GBT | `gbt_score` | Review, Diagnostics |
| **bucket** (buckets) | Where a pair lands: **Accepted**, **For review** or **Rejected**. | band, outcome, class | `bucket`, `score_bucket` | Review tabs, run summary |
| **score bucket** | The bucket the score alone gives, before any rule, earlier grouping or label moves it. | raw bucket | `score_bucket` | Review filters, `score_eval.json` |
| **accept line** | The score at or above which a pair is accepted without review. | auto-accept threshold, threshold_high, high line | `match_probability_threshold_high` | Config → Thresholds, New run |
| **review line** | The score below which a pair is rejected without review. | review floor, review band lower bound, threshold_review | `match_probability_threshold_review` | Config → Thresholds, New run |
| **lowest score kept** | The lowest score kept in the run's files. Anything weaker is thrown away. | candidate threshold | `match_probability_threshold_candidate` | Config → Thresholds |
| **veto** (vetoes) | A rule about a pair that stops the tool accepting it, whatever the score says. It sends the pair for review or rejects it, and it says why. | stopped by a rule, blocked pair, rule | `ruleset.vetoes[]`, `vetoed_by`, `veto_reason` | Config → Vetoes, Review, Cluster review |
| **blocking rule** (blocking rules) | A rule that says which pairs are worth comparing at all. | block, candidate rule | `linkage_settings.blocking_rules[]` | Config → Thresholds & Splink |
| **comparison** (comparisons) | How one column is compared inside a pair. | feature (in the Config sense) | `linkage_settings.comparisons[]`, `gamma_<column>` | Config → Thresholds & Splink |
| **label** (labels) | One saved answer about one pair: **Match** or **Not a match**. It can carry a note and a source link, and it beats every score and every rule. | answer, verdict, decision (for one pair), TRUE/FALSE, judgement | `pair_labels` table, `is_match` | Review, Label library |
| **group decision** (group decisions) | One answer about a whole cluster or held group at once, saved as one label per pair inside it. | cluster decision, bulk decision | `decision_id`, `decision_scope`, provenance `cluster_merge` / `cluster_split` | Cluster review, Label library |
| **reviewer** (reviewers) | The person who saved a label. | user, labeller, human | `reviewer`, `decided_by = human` | Label library, Audit |
| **earlier grouping** | The grouping the team made before this tool existed. The profile names it; donations calls it the earlier manual grouping, PSC has none. | imported labels, existing labels, earlier manual work, earlier labels, the manual work | `existing_entity_id`, label provenance `import`, `decided_by = import` | Review filters, run summary, Cluster review |
| **earlier ID** (earlier IDs) | The ID a record already carried from the earlier grouping. | existing entity ID, DonorIDStandardTR, old ID | `existing_entity_id`, `existing_entity_ids`, `n_existing_ids` | Cluster review column, Review chips |
| **cluster** (clusters) | A set of units joined by accepted pairs. A cluster is a proposal, not a decision. | group (on the cluster screen), component, candidate entity | `cluster_id`, `clusters.parquet` | Cluster review |
| **cluster for review** (clusters for review) | A cluster the gate held back for a person, because it conflicts, is too large, holds too many different names, may be a chain, or mixes earlier IDs. | held cluster, flagged cluster | `withheld`, cluster `status` | Cluster review |
| **mixed names** | A cluster that holds more different values of a name or a birth year than one person could have, so it is really several people. | mixed-name cluster | cluster `status` = `mixed_names` | Cluster review |
| **entity** (entities) | One real person or one real organisation, with one ID that stays the same from run to run. | proposed entity, merged group, final group | `entities.parquet`, registry `entities` | Entities tab, export |
| **entity ID** (entity IDs) | The ID an entity carries. | EntityID, standard ID, final ID | `entity_id`, export `EntityID` | Entities tab, export |
| **proposal** | What a run produces: one entity ID per record, not yet written anywhere durable. | draft entities, candidate IDs | `scope = proposal`, `entities.parquet` | Publish & export |
| **registry** | The durable store of entity IDs, written only when a run is published. | register, store, durable register | registry tables `entities`, `entity_members` | Publish & export, How it works |
| **publish** (published) | Writing a run's proposal into the registry. | commit, finalise, adopt | `POST /api/runs/{id}/publish`, `entity_publications` | Publish & export |
| **retired ID** (retired IDs) | An entity ID that lost a merge. It still leads to the ID that survived. | alias | registry `status = retired`, `alias_of` | Publish & export, export alias sheet |
| **surviving ID** | The entity ID that a merge kept. | survivor | `id_status = survivor`, `alias_of` target | Publish & export |
| **run** (runs) | One execution of the whole method over one input file, with one config version. | job, build | `runs` table, `run_id` | Runs, run summary |
| **config version** (config versions) | One saved, frozen copy of every rule and setting. A run names the version it used. | ruleset, settings, linkage settings, the config | `config_versions`, `config_version` | Config → Version history |
| **training set** | The labels the model learns from. | teaching labels | `held_out = 0` | Label library, Model panel |
| **test set** | The labels held back from training, used only to grade the model and to set its lines. | frozen test set, held out, held back for testing, eval set | `held_out = 1` | Label library, Model panel |
| **pair precision** | Of the pairs this run joins, the share the earlier grouping had already joined. | precision, accuracy | `pair_precision` | Run summary, Config → Match keys |
| **pair recall** | Of the pairs the earlier grouping joined, the share this run joins too. | recall, coverage | `pair_recall` | Run summary, Config → Match keys |
| **name-frequency table** | An outside table of how common each UK forename and surname is, used so a rare name counts for more. | reference table, rarity table | `uk_name_frequencies.parquet` | Model panel warnings |

### Terms that differ by profile

The generic term is always the one above. The profile supplies its own noun and
the frontend reads it from `GET /api/profile` (`nouns.*`, `existing_label_name`).

| Generic | donations | psc |
|---|---|---|
| record | donor | PSC record |
| evidence rows behind a unit | donation history | companies controlled |
| one evidence row | donation | company |
| earlier grouping | earlier manual grouping | none — leave every mention out |

### Statistical words, glossed once

- **precision** — of the things the tool said were the same, the share that were.
- **recall** — of the things that were the same, the share the tool found.
- **calibration** — adjusting a model's scores so that a score of 0.9 really
  does mean nine times in ten.
- **graded model** — a model that has been measured against the test set, and
  may therefore set the buckets. A model that has not is a **new model** (retire
  "cold start"): it only re-orders the review queue.

No other statistical word should reach the screen. Retire on sight: AUC, SHAP
(say "how much each piece of evidence moved the score"), Wilson bound (say "the
cautious end of the range"), EM, term frequency, gamma, monotone, ablation.

---

# Provenance vocabulary

"How it was decided" is asked in six places today and answered in six different
vocabularies. This section proposes one.

**One question, one phrase: "How it was decided".** Use it as the column header,
the chip label and the filter name everywhere — pairs, clusters, entity IDs,
attribute values and labels.

## The one ordered list

Weakest first. A later one always beats an earlier one.

| # | Label | Definition |
|---|---|---|
| 1 | **On its own** | Nothing joined this record to any other. |
| 2 | **Match key** | A match key found the same values in every one of its columns. |
| 3 | **Score** | The score reached the accept line. Say which scorer in a second line: Splink score, or model score with its version. |
| 4 | **Rule** | A veto rule moved this pair, whatever the score said. |
| 5 | **Earlier grouping** | Both sides already carried the same earlier ID. |
| 6 | **Reviewer** | A person decided it. Say who, when, and their note. |

**Suggested** (a machine-written label, `provenance = llm`) sits outside this
list. It is a proposal, not a decision. Show it as its own chip and never let it
rank.

## Mapping every existing value

| Existing field | Value | Maps to |
|---|---|---|
| pair `decided_by` | `score` | 3 Score (Splink) |
| pair `decided_by` | `model` | 3 Score (model) |
| pair `decided_by` | `veto` | 4 Rule |
| pair `decided_by` | `import` | 5 Earlier grouping |
| pair `decided_by` | `human` | 6 Reviewer |
| pair `vetoed_by` | a veto id | 4 Rule — the id names which one |
| cluster `status` | `mixed_names` | **different question** — what the gate found |
| veto condition `op` | `not_equal_or_missing` | **different question** — what the rule compares. "Not the same, or missing": the two values differ, or one side has no value at all. It is how a veto rule says that nothing here corroborates the pair |
| cluster/edge `edge_source` | `score` | 3 Score |
| cluster/edge `edge_source` | `import` | 5 Earlier grouping |
| cluster/edge `edge_source` | `human` | 6 Reviewer |
| `entity_basis` | `single` | 1 On its own |
| `entity_basis` | `exact_key` | 2 Match key |
| `entity_basis` | `import` | 5 Earlier grouping |
| `entity_basis` | `score` | 3 Score |
| `entity_basis` | `human` | 6 Reviewer |
| label `provenance` | `manual` | 6 Reviewer, one pair at a time |
| label `provenance` | `bulk_range` | 6 Reviewer, a band of scores at once |
| label `provenance` | `cluster_merge` | 6 Reviewer, a whole cluster merged |
| label `provenance` | `cluster_split` | 6 Reviewer, a cluster split |
| label `provenance` | `import` | 5 Earlier grouping |
| label `provenance` | `llm` | outside the list — Suggested |
| attribute `basis` | `rule` | **cannot map** — see below |
| attribute `basis` | `majority`, `raw`, `tie` | **cannot map** |
| attribute `basis` | `human` | 6 Reviewer |
| `<column>_rule` | a derived-rule id | **cannot map** |
| `id_status` | `new`, `kept`, `survivor`, `minted_after_collision` | **different question** — see below |
| exact-group `agreement` | `consistent`, `conflict`, `extends`, `new` | **different question** |

## Where the vocabularies cannot be mapped cleanly

1. **`entity_basis` ranks Score above Earlier grouping. Every other vocabulary
   ranks Earlier grouping above Score.** `BASIS_ORDER` in
   `backend/app/pipeline/dedupe/stage_4_cluster.py:60` is
   `single < exact_key < import < score < human`. The pair rule in
   `docs/RULESET.md:204` is `score or model < veto < import < human`. Two
   vocabularies, same two words, opposite order. Owner decision needed.
2. **`entity_basis` has no value for a veto and none for the model.** A pair the
   model accepted and a pair Splink accepted both read `score`. A veto never
   reaches an entity at all, because a vetoed pair is not accepted — but a
   reviewer who overrode a veto cannot see that from the entity.
3. **Attribute `basis` answers a different question.** `rule` there means a
   derived-column rule, not a veto. `majority` and `raw` have no equivalent in
   the list because they are about one column's value, not about why two records
   are together. Keep them, and give them their own phrase: "How this value was
   set", with the words **Rule**, **Most members**, **Only member** and
   **Undecided** (retire `tie`).
4. **`id_status` answers "where the ID came from", not "why these records are
   together".** Keep it separate, phrase it "Where this ID came from", with the
   words **New**, **Kept**, **Survived a merge** and **Re-made after a clash**.
5. **`agreement` is a measurement, not a provenance.** It compares a group with
   the earlier grouping. Phrase it "Against the earlier grouping", with the words
   **Agrees**, **Conflicts**, **Adds to a group** and **New**.
6. **The run summary's "Decided by" means something else again.** It names the
   scorer in force for the whole run, not the authority on one pair. Rename it
   **Scored by**.

### Word choices Tom confirmed on 2026-09-21

- **unit** stays. A unit can be one record on its own, so "grouped record" would be wrong for most of them. The hover definition explains it.
- **cluster for review** replaces "withheld cluster", which looked too much like "held group" and meant something different. A held group was stopped by a match key's guard. A cluster for review was stopped by the gate.
- **lowest score kept** replaces "candidate floor".
- **bucket** stays, shown rarely. The words a reviewer sees are "Accepted", "For review" and "Rejected".

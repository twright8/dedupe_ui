# dedupe_ui — design

Date: 2026-09-18. Status: agreed in a design interview with Tom. Nothing is built yet.

## Goal

Three name-matching tools run on one server address. A chooser page lets the user pick one.

| Tool | Job | Codebase |
|---|---|---|
| ROE–OCOD linkage | Link names across two datasets | `roe_ui` (in production, final) |
| PSC reconciliation | Give one entity ID to PSC records that are the same person or business | `dedupe_ui`, PSC profile |
| Donations reconciliation | Give one entity ID to donor records that are the same person or organisation | `dedupe_ui`, donations profile |

All three look and work the same way: rule-based cleaning, deterministic matching, Splink, a GBT with calibration, human labels, and rules that users edit in the UI. The tools assist a human. They are not fully automatic.

## Decisions

Each decision has a number so that later notes can refer to it.

### Approach

**D1. Copy first, merge later.** `roe_ui`'s code is frozen. `dedupe_ui` starts as a copy of `roe_ui` at tag `pre-multitool-20260918` (commit `5d03ba0`). Merging `roe_ui` into a shared core is a separate, later project.

**D2. One new codebase with two profiles.** `dedupe_ui` serves both new tools. It runs as two instances. Each instance has its own data folder, its own local port, and one profile. A profile holds the column mapping, the default rules, and the feature builder. Anything specific to PSC or donations goes in the profile and never in shared code.

**D3. Start from `roe_ui`, port the engine from `deduping`.** The `roe_ui` copy brings the UI, login, rules editor, run launching, uploads, audit log, and GBT versioning. `deduping` supplies the parts `roe_ui` lacks: cleaning functions, deterministic grouping with an over-merge split, the blocking cost check, the registry of durable entity IDs, and the publish gate. `deduping`'s frontend is not reused, because it is an older, cut-down copy of `roe_ui`'s frontend. `psc reconcile` supplies the business-track features and its 757 corporate labels.

**D4. Donations first, PSC second.** Donations is small (about 52,000 donors), so a full run takes minutes. It already has 4,284 manually merged groups to test against. PSC comes second, and its work is mostly about scale.

### Two tracks in every profile

**D5. Persons and organisations get separate algorithms.** Each profile has a person track and an organisation track. A user-editable rule assigns each record to a track. The tool never suggests a merge across tracks. A reviewer can still force one by hand.

**D6. The organisation track does not trust the ID alone.** In the PSC data the registration number fails for about one record in ten. 8.6% of corporate records have no usable number. 9% of numbers that appear more than once carry more than one name, including placeholders such as `00000001` and formation-agent numbers. The track therefore works like this:

1. Cleaning rules scoped to organisations.
2. ID match with guards: a blocklist for placeholder and agent numbers, a cap on how many names one number may carry, and review for pairs with the same number but different names.
3. Splink and GBT for records with no usable number, and for same-name pairs with different numbers.
4. Veto rules that cap a score at 0.49, which forces the pair into review.
5. The same review screen as persons, with a Person / Organisation switch.

**D7. In donations, donor type is a feature, not a partition.** All organisation types share one track, because 160 existing merge groups mix types. "Impermissible Donor" and "Other" are sorted into a track by name pattern.

### Rules

**D8. Four new rule types, all versioned and previewable.** `roe_ui` has regex name rules, one lookup table, token lists, blocking rules, and thresholds. `dedupe_ui` adds:

- named functions from a fixed library (postcode cleaning, company-number cleaning and padding, phonetic keys, date-of-birth range check)
- general lookup tables with a declared fallback (nicknames, countries, nationalities)
- deterministic match keys: ordered columns, tiers, an ID-join flag, a per-key choice of whether missing values may match, and guards (maximum group size, a required corroborating field, a split rule)
- veto rules: a pair condition, an action (cap score, force review, or block), and a reason shown in the UI

Every rule can be scoped to a track. The live preview runs the real pipeline code. (`roe_ui`'s preview skips one hard-coded step at `standardise.py:67`. `dedupe_ui` does not copy that flaw.)

**D8a. Standardising a category is two stages** (added 2026-09-18, from Tom). Donor status is often wrong at source: companies marked as unincorporated associations, LLPs as companies.

1. *Record level.* A fifth rule type, "derived column" rules: ordered conditions, first match sets the value, written to a new column (donations: `donor_status_std`). They run after cleaning, so they can read the cleaned company number. Defaults: an `OC`, `SO` or `NC` prefix means LLP; an `SC` or `NI` prefix, or a number starting with a digit, means Company; an `IP` or `SP` prefix means Friendly Society (a registered society); a record with a company number cannot be a Trust. The company-number rules may only change a status in the overridable set: Company, LLP, Unincorporated Association, Other, Trust. They never change Registered Political Party, Trade Union, Friendly Society, Public Fund or Individual, so an incorporated political party stays a party. Tom agreed both on 2026-09-18. Every rule can be edited or switched off in the UI.
2. *Entity level.* After entity IDs exist, a status set by a rule beats a raw status, otherwise the majority among the entity's members applies. A tie keeps the raw value and flags the entity for review.

The export carries the standard status and the standard ID, each with how it was decided. The model uses the standardised status as its donor-type feature. PSC can reuse both stages, for example for the PSC kind.

### Labels and models

**D9. Labels train the GBT only.** Splink stays unsupervised and supplies candidates and a prior score. This is `roe_ui`'s rule and it carries over unchanged.

**D10. Symmetric label key.** A label is stored against an ordered pair of record IDs. Labels are append-only. A later label supersedes an earlier one, and the earlier one stays on record with the reviewer's name.

**D11. Import of the donations manual labels.**

- `DonorIDStandardTR` of `0` or blank means "never reviewed". It carries no label. Those donors are the main work queue.
- Real groups are trusted merges, imported with provenance `import`.
- Two donors in different real groups are a weak "not the same" signal. It is used for training only.
- The frozen test set comes only from labels confirmed in the new UI.

**D12. Name rarity comes from outside the donations sheet.** The Electoral Commission often gives a repeat donor a new `DonorId`, so a frequent name inside the sheet usually means one frequent donor. A UK name-frequency table built from the PSC individuals feeds Splink's term-frequency adjustment and the GBT's rarity features.

**D13. Same decision process in every profile.** Score thresholds set accept, review, and reject. There is no donation-value rule. Each profile names priority columns that the review table can sort by (donations: total donated by the pair; PSC: for example, number of companies controlled). Sorting changes the order of review and nothing else.

**D13a. Evidence the reviewers use for individuals** (added 2026-09-18, from Tom). Beyond the name, the team judges two things by hand. (1) The size pattern of donations, for example a donor who always gives £10,000. This becomes a GBT feature family: typical single donation, shared exact amounts, share of round amounts, and distance between the two amount distributions. The review screen also shows each side's donation history, so the reviewer sees the same evidence. (2) Public reporting, for example a journalist confirming that one person gave to several parties. This cannot be computed. A label therefore carries notes and a source link, so the evidence is recorded with the decision.

**D13c. What reviewers check, by kind of donor** (added 2026-09-18, from Steve Goodrich via Tom). Individuals: recipient, local unit, amount, date accepted, donation type. Public funds: nature of donation, amount, date accepted. Companies, LLPs, friendly societies and building societies: registration number, postcode, amount, date accepted. Trade unions: donor status and name are enough. Unincorporated associations, trusts and other: postcode, amount, date accepted. Three consequences. (1) The profile declares an `evidence_focus` per kind of donor, and the review and cluster screens show those fields side by side at the top of a pair. (2) A match key may carry a condition, so a key can apply to one kind of record only. Donations gains a default key for trade unions on standard status plus cleaned name, kept only if it holds precision against the existing labels. (3) The model gains the overlap in nature of donation, and the kind of donor as a category, so it can learn that a name settles a trade union but not a company.

**D13b. Evidence rows.** A profile may supply child rows for each record, shown when a pair or group is opened. Donations: the individual donations (date, amount, recipient, local unit, type). PSC: the companies a person controls, with dates. Feature builders may read them.

Known limit: the existing donations labels were made almost entirely on the name. Reviewers merged 99.6% of identical-name pairs of individuals, even across different parties. The labels therefore cannot show when two people with the same name are different. New "keep apart" labels from the UI are the fix. Context features (party, local unit, year gap, title and middle-initial conflict) are built in, and their value will show once such labels exist.

### Entities and IDs

**D14. Clustering and durable IDs come from `deduping`'s registry.** IDs stay stable between runs. A human decision always beats the model. A cluster is published only when every internal pair is decided or scores above a floor. Retired IDs become aliases.

**D15. Donations keeps its ID convention.** The record key is `DonorId` plus a trust flag, because trusts use a separate number range. A cluster with already-labelled donors keeps its `DonorIDStandardTR`. A new cluster takes its lowest `DonorId`, with a `TR` prefix for trusts. When two groups merge, the lower ID survives. The export is the original sheet plus two columns: the entity ID, and how it was decided.

### PSC data

**D16. Source is the bulk snapshot.** Record IDs are the company number plus the last part of `links.self`, which is identical to the API-based IDs. The write-back to Elasticsearch index `ch-cred-pscs-v1` therefore keeps working. Columns are trimmed to the ones matching needs. Ceased PSCs stay in. Super-secure records are dropped. Officers are out of scope for version one. The Elasticsearch write-back is a separate step started by hand.

**D17. PSC runs are guarded.** The run button works in both profiles. Each run has a hard memory cap (about 6 GB on the server) and low CPU priority. One run executes at a time across both instances. The blocking cost check runs first and stops an over-budget run before Splink starts. A full rebuild can run on a laptop from the command line, with results copied to the server for review.

### Server

**D18. One address through Caddy.** Caddy takes port 8000. The `roe_ui` service moves to `127.0.0.1:8001`, which is a service-file change and not a code change. `/psc/*` and `/donations/*` go to the two `dedupe_ui` instances, which are built with those base paths. Exactly `/` shows the chooser page. Every other address goes to `roe_ui`, so existing bookmarks keep working.

**D19. One login.** All three tools share the site password and a fixed `SECRET_KEY`. Setting that key in `/etc/roe_ui.env` also stops `roe_ui` logging everyone out at each restart. There are no roles. The users are a small trusted team.

**D20. Go-live safety.** The new tools first run on a spare port, with `roe_ui` untouched. The cutover happens once, at a quiet time, after a copy of `/var/lib/roe_ui`, the service file, and the Caddyfile. A smoke test checks `roe_ui` before and after. Tom runs every command that changes the server. Claude runs read-only checks only.

## Differences from `roe_ui` that the user will see

These follow from working within one dataset.

- The new-run screen takes one input file, not two.
- The review screen compares record A with record B, and both sides show the same columns.
- `roe_ui`'s "ambiguous" screen (one left record against several right candidates) becomes a cluster screen: one cluster, its members, and controls to split or merge.
- The rules screen has extra tabs for the four new rule types, and a track selector.
- The run summary reports clusters and entity IDs, where `roe_ui` reports matched and unmatched records.

Everything else keeps `roe_ui`'s layout, styles, shortcuts, and wording.

## Build order

Each slice ends with something Tom can check in the browser.

| # | Slice | Check |
|---|---|---|
| 1 | Copy `roe_ui`, add profiles and the donations loader | Donation records appear in the UI |
| 2 | New rule types, track assignment, match keys with guards | Rule edits change the exact merges |
| 3 | Splink within one dataset, symmetric labels, review screen with priority sort, import of manual labels | Donor pairs can be labelled |
| 4 | Clustering, registry, entity IDs, export | Exported sheet follows the ID convention |
| 5 | GBT, calibration, per-pair explanations | Model panel works as in `roe_ui` |
| 6 | Organisation track for donations | Companies and unions dedupe under their own rules |
| 7 | Staging on the server, then the cutover with the chooser page | All tools on port 8000 |
| 8 | PSC profile: bulk loader, person track, scale guards | A full PSC person run completes |
| 9 | PSC organisation track | Placeholder numbers no longer merge silently |

## Progress (updated 2026-09-18)

| # | Slice | State |
|---|---|---|
| 1 | Profiles, base path, donations loader, Records tab | done, commit `d75e3ec` |
| 2 | Rules: tracks, cleaning, tables, match keys with guards, exact groups | done, `a2e42fb` |
| 2+ | Derived columns (status standardisation), conditional match keys, trade union key, evidence focus | done, `e02ee3f` and the slice 5 commit |
| 3 | Scoring per track, symmetric labels, review cockpit, evidence rows | done, `3958c25` |
| 4 | Clusters and gate, registry, group decisions, publish, export | done, `66f9dfd` |
| 5 | GBT per track, cold start, explanations, model panel, "most useful to label" | done |
| 6 | Organisation track for donations | folded into slices 2 to 5: both tracks were built together |
| 7 | Staging on the server, then the cutover with the chooser page | kit written and tested locally (`deploy/`, `docs/DEPLOY.md`), commit `3282dc4`. Nothing applied. Tom runs every command that changes the server |
| 8 | PSC profile: bulk loader, both tracks, scale guards | 8a done on a 500,000-record sample (`3282dc4`). 8b (full scale) in progress: see `PSC_HANDOVER.md` |
| 9 | PSC organisation track | built with slice 8a. Needs the full-scale run and tuning |

Contracts live beside this file: `RULESET.md`, `LINKAGE.md`, `PAIRS_API.md`, `ENTITIES.md`, `ENTITIES_API.md`, `MODEL.md`, `MODEL_API.md`.

How to run it locally: `cd backend && PROFILE=donations BASE_PATH=/donations DATA_DIR=data SITE_PASSWORD=devpass SECRET_KEY=dev-only-fixed-key .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100`, then open `http://127.0.0.1:8100/donations/`. Build the frontend first with `cd frontend && npm run build`. Tests: `cd backend && SITE_PASSWORD=testpass123 .venv/bin/python -m pytest tests -q`.

Known follow-ups: donations person scores pile up at 0.70 to 0.80 because Splink learned that a surname mismatch costs only 1.06 bits (m = 0.479 on the last level): one EM training rule fixes only the forename, and inside that block most "matches" are different people who share a first name. Change the training rules, then keep the change only if `score_only` holds person precision 0.9872 at recall 0.6236 or better; a full PSC rebuild belongs on the laptop (scoring a 3% sample already peaks at 6.3 GB, the server's cap); tune Splink for organisations (a shared postcode now carries too much weight, precision 97.0%); the person track needs human "keep apart" labels before the model can be graded; six earlier groups join a person with an organisation and forced cross-track merges are not built; `build_units` is 3.4 s per 52,000 records and should be measured again on PSC data; the legacy `roe_ui` pipeline modules (`stage_0_preprocess.py`, `stage_1_exact_match.py`, `stage_2_probabilistic_link.py`, `stage_3_evaluate.py`, `gbt_*.py`, the old `labels` table and its services) are unreachable from a dedupe run and can be deleted once nothing imports them.

## Open items

- The full PSC snapshot is still downloading. `dedupe_final/` holds part 1 of 32.
- Features, thresholds, and blocking rules for both profiles will be tuned after the platform works.

Resolved or set aside:

- Server disk space. On 2026-09-18 the raw API crawl `/home/ubuntu/ch_data` (19 GB) was deleted from the server after a checksum-verified copy to `/home/tomwright/PycharmProjects/ch_bulk`. Free space is now 34 GB. The server scripts `run_ingest.sh` and `run_flat.sh` need the crawl copied back before they can run again.
- The Kibana password on the process command line, and HTTPS on port 8000: Tom set both aside on 2026-09-18.

## Sources

| What | Where |
|---|---|
| Base app | `/home/tomwright/PycharmProjects/roe_ui` (tag `pre-multitool-20260918`) |
| Dedupe engine to port | `/home/tomwright/PycharmProjects/deduping/backend` (`pipeline/`, `registry/`, `config/linkage_spec.py`) |
| Business features and labels | `/home/tomwright/PycharmProjects/psc reconcile/backend/app/pipeline` |
| Donations sheet and PSC snapshot | `/home/tomwright/PycharmProjects/dedupe_final` |
| Production | `roe-prod`: code `/opt/roe_ui`, data `/var/lib/roe_ui`, service `roe_ui.service`, Caddy at `/etc/caddy/Caddyfile` |

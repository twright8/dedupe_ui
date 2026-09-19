# Model — the GBT that decides pairs

Implements decisions D9, D11, D12, D13a in `DESIGN.md`. This file is the contract for slice 5. It carries over `roe_ui`'s design: Splink finds candidate pairs and supplies features, and a gradient boosted tree (GBT, LightGBM) makes the decision. Only labels train it.

## One model per track

Each track has its own model: people and organisations. A model belongs to one profile instance. Versions are immutable folders under `DATA_DIR/models/<track>/versions/<n>/`, holding the model file, the calibration, the feature list, the training report, and the config version it was trained on. One version per track may be **active**.

## Features

A feature row describes one scored pair of units. Features come from two places.

1. **Generic**, built by shared code from `pairs.parquet`: the Splink match weight, and one agreement level per comparison (`gamma_<column>`).
2. **Profile features**, built by the profile's feature builder: `Profile.build_pair_features(pairs, units, events, references) -> DataFrame`, with metadata for each feature: `name`, `label` (plain words for the UI), `group`, and `monotone` (+1, −1 or 0).

The builder must be vectorised. It receives whole frames and never loops over pairs.

Donations, people:

| group | features |
|---|---|
| name | Jaro-Winkler of the full cleaned name, forename exact, forename is an initial of the other, canonical forename equal (nicknames), middle initial agrees, middle initial conflicts, title gender conflicts, post-nominals agree |
| rarity (D12) | log frequency of the surname, and of forename plus surname, in an **outside** UK name table. Never the frequency inside the donations sheet, because a repeat donor gets many donor IDs |
| recipients | Jaccard overlap of recipients, same local unit, both gave to one party only and it differs |
| timing | gap in years between the two donation ranges, ranges overlap |
| amounts (D13a) | ratio of median single donations, most common amount equal, count of exact amounts both gave, difference in share of round thousands, distance between the two amount distributions, both gave only once |
| kind (D13c) | the kind of donor, as a **category** |
| size | log of records in each unit |

Donations, organisations: Jaro-Winkler and token Jaccard of `name_core`, TF-IDF cosine of name tokens (token rarity inside the sheet is sound for organisation words such as CLUB or LIMITED), postcode equal, postcode district equal, company number equal, company number conflicts, exactly one side has a company number, legal form equal, standard status equal, **Jaccard overlap of the natures of the two sides' donations (D13c)**, plus the recipients, timing, amounts, kind and size groups.

**Kind of donor (D13c).** Reviewers judge different donors on different evidence: a name settles a trade union and settles nothing about a company; a public fund is read on what the donation was for; a company on its registration number and its postcode. The model cannot learn any of that while every organisation looks alike to it. `status_kind` is therefore a feature on both tracks, taken from the standardised donor status — falling back to the raw one — and bucketed into D13c's groups: individual; public fund; company, LLP, friendly society or building society; trade union; unincorporated association, trust or other; registered political party; unknown. Where the two sides are not the same kind it reads "kinds differ".

It is a **category**, not a number: LightGBM splits it by set membership, because there is no order in which a trade union sits between a company and a trust. It carries no monotone constraint, and its list of codes is written into every version's feature list, so re-ordering the list in code cannot silently re-map an older model. `donor_status_std_equal` stays beside it — the kind groups a company with an LLP, and the equality feature can still tell them apart.

**Nature of donation (D13c).** `nature_overlap` is the Jaccard overlap of the distinct natures of the two units' donations, from `events.parquet`, and is null when either side records none. Most donations carry no nature, so it is null on most pairs; the ones it speaks about are the public funds and the non-cash gifts, which is exactly where D13c says reviewers read it. It has a group to itself, because the ablation table is where "does the nature of the donation help" gets answered, and burying it inside another group would hide the answer.

**Corpus statistics.** A feature such as "how rare are the words in this name" is not a fact about a pair. It is a fact about the pair *and the collection the words were counted in*, so it is only a feature at all if every reader counts over the same collection. A builder that needs such statistics **declares** them as a `CorpusSpec` (`app/model/corpus.py`): a name, the unit column the documents come from, a scope, and the vectoriser settings. The statistics — the vocabulary and the IDF vector — are fitted **once**, over every unit in that scope, at scoring time, written into `<run>/corpus/`, and read back by batched scoring, by apply-model and by a one-pair explanation. `transform` never fits.

Two things fail without this. The value of a pair moves with the batch it was scored in, so batching the scorer changes every number in the file. And a one-pair explanation fits over a corpus of two names, so it prints numbers the scoring run never produced — which defeats the screen that is meant to let a reviewer check the machine.

`scope` is `"track"` (every unit of the track being scored) or `"run"` (every unit in the run). **PSC declares `track`** for `name_core_tfidf`: `name_core` is an organisation column and the person units carry none, so counting them in would add hundreds of thousands of empty documents and push every IDF towards the same value. **Donations declares `run`**, because that is what its builder has always done and the values in the trained donations model depend on it. Narrowing donations to the track would be an improvement; it is a change to make deliberately, with a retrain, and it is written down here so it is not made by accident.

**Reference tables.** `references` holds outside data the profile declares. Donations declares `uk_name_frequencies`: forename and surname counts built once from the PSC individuals (`deduping/backend/data/source/pscs_individual.parquet`) by a script in `backend/scripts/`. The built table lives in `DATA_DIR/references/`. If it is missing, the rarity features are null and the training report says so.

## Training data

| source | use |
|---|---|
| human labels from the UI, `held_out = 0` | training, full weight |
| human labels from the UI, `held_out = 1` | the frozen test set. Never trained on. Used for grading and for choosing thresholds |
| imported labels that agree (two units share one earlier ID) | training as positives, at reduced weight, sampled so they do not swamp human labels |
| imported labels that disagree (`import_disagrees`) | training as weak negatives, at lower weight still (D11) |
| decisions (`cluster_merge`, `cluster_split`) | as human labels |

Calibration is Platt scaling, fitted out-of-fold on human labels only. With fewer than 50 human labels the model is a **cold start**: it trains on imported labels, is calibrated on them, is marked "not graded", and may not auto-accept. It only re-orders the review queue.

Folds split by connected group of units, never by pair, so one donor's pairs never sit on both sides of a fold.

Known limit, stated in every training report: the imported labels were made mostly on the name. They cannot teach the model when two people with one name are different people. Only new "keep apart" labels can.

## Scoring and decisions

Stage 3b runs after Splink when the track has an active model. It writes `gbt_score` beside `match_probability`. When a graded model is active, buckets use `gbt_score`, and `decided_by` is `model`. Human labels and imported labels overlay it exactly as they overlay the Splink score.

Thresholds for a graded model come from the frozen test set: the auto-accept line is the lowest score whose precision lower bound (Wilson, 95%) meets the target precision (a linkage setting, default 0.99), and the reject line is set the same way for non-matches. With too few test labels to reach the target, no line is set and everything the model scores goes to review.

Guard, carried over from `roe_ui`: if applying the model empties the review band or makes the scores near two-valued, the run reverts to the Splink score and records a warning.

Applying a model to an existing run does not rerun Splink: `POST /api/runs/{id}/apply-model`. Reverting is `POST /api/runs/{id}/revert-model`.

## What the user sees

- **Model panel** per track: train, the version list, activate or deactivate, and the training report: label counts by source, AUC, precision and recall at the chosen lines with Wilson bounds, a calibration chart, and feature importance.
- **Feature importance** in three forms: gain, mean absolute SHAP value, and an ablation table by feature group (average precision with that group removed). This is the mathematical answer to "which evidence matters", with the known limit printed beside it.
- **Per-pair explanation**: SHAP contributions from LightGBM's own `pred_contrib`, shown in the review screen next to Splink's explanation, in plain feature labels.
- **Most useful to label**: a review sort that puts first the pairs the model is least sure about and the pairs where the model and Splink disagree most, weighted by the profile's priority column.

## API

`GET /api/model/{track}`, `GET /api/model/{track}/versions/{n}`, `POST /api/model/{track}/train`, `POST /api/model/{track}/activate`, `POST /api/model/{track}/deactivate`, `GET /api/model/{track}/features` (the feature metadata), `POST /api/runs/{id}/apply-model`, `POST /api/runs/{id}/revert-model`. Pair items gain `gbt_score`; pair detail gains `model_explanation`. The pairs list gains `sort=useful`. Response shapes are fixed in `docs/MODEL_API.md`, written by the backend before it builds.

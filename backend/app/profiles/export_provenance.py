# backend/app/profiles/export_provenance.py
"""What every export says about the run that made it.

An export leaves the tool. Months later someone opens it and has to be able to
say what produced it without the tool in front of them: which file went in and
what it hashed to, which code read it, which rules were in force, where the
accept line was, which model scored, and who exported it when.

The old run sheet dumped every count under its raw key — `pairsVetoedFromAccept`,
`clustersByStatus` as a lump of JSON — and left out the lines, the model and the
code version entirely. This module writes the same facts in the words the
screens use (`docs/GLOSSARY.md`), so the sheet and the app say one thing.

Two things are built here and used by both profiles' exports:

``run_rows``
    the run sheet, as ``[label, value]`` pairs.
``how_to_read_rows``
    one row per column the export adds, with its plain definition, followed by
    the ordered provenance list. A reader who has never seen the tool can read
    the file from this sheet alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import vocabulary
from app.services import bucketing_history, run_manifest

#: Plain names for the run's counts. A key that is not here is turned into a
#: sentence by ``_plain_key``, so a new count can never reach a user as
#: ``pairsVetoedFromAccept``.
COUNT_LABELS: dict[str, str] = {
    "input_rows": "Rows in the input file",
    "input_rows_dropped": "Rows the loader could not use",
    "event_rows": "Evidence rows loaded",
    "records_total": "Records loaded",
    "records_person": "Records on the people track",
    "records_organisation": "Records on the organisations track",
    "records_labelled": "Records a reviewer has answered on",
    "records_unreviewed": "Records nobody has looked at",
    "exact_merged_groups": "Exact groups a match key merged",
    "exact_merged_records": "Records inside those exact groups",
    "exact_held_groups": "Groups a guard held for a person",
    "exact_held_records": "Records inside those held groups",
    "exact_entities_after": "Entities after the match keys",
    "exact_conflicts": "Exact groups that conflict with the earlier grouping",
    "exact_pair_precision": "Pair precision after the match keys",
    "exact_pair_recall": "Pair recall after the match keys",
    "exact_split_by_human": "Exact groups a reviewer split",
    "exact_merged_by_human": "Held groups a reviewer merged",
    "units_total": "Units compared",
    "units_person": "Units on the people track",
    "units_organisation": "Units on the organisations track",
    "untrained_comparisons": "Comparison levels the engine could not learn",
    "pairs_scored": "Pairs scored",
    "pairs_accept": "Pairs accepted",
    "pairs_review": "Pairs for review",
    "pairs_reject": "Pairs rejected",
    "pairs_decided_by_import": "Pairs the earlier grouping decided",
    "pairs_import_disagrees": "Pairs that disagree with the earlier grouping",
    "pairs_vetoed": "Pairs a veto rule moved",
    "pairs_vetoed_from_accept": "Pairs a veto rule stopped being accepted",
    "veto_conflicts_import": "Vetoes that contradict the earlier grouping",
    "entities_after_score": "Entities after the score",
    "score_pair_precision": "Pair precision after the score",
    "score_pair_recall": "Pair recall after the score",
    "labels_total": "Answers a reviewer has saved",
    "labels_true": "Answers of Match",
    "labels_false": "Answers of Not a match",
    "labels_satisfied": "Answers the match keys had already settled",
    "labels_forced": "Answers whose pair the scorer never made",
    "labels_applied": "Answers this run acted on",
    "labels_in_library": "Answers in the label library",
    "label_contradictions": "Answers a match key contradicts",
    "entities_after_human": "Entities after the reviewers",
    "human_pair_precision": "Pair precision after the reviewers",
    "human_pair_recall": "Pair recall after the reviewers",
    "clusters_total": "Clusters",
    "clusters_withheld": "Clusters for review",
    "held_groups_open": "Held groups still open",
    "review_queue": "Waiting for a person",
    "decisions_total": "Group decisions saved",
    "cross_track_ids": "Earlier IDs that span both tracks",
    "entities_proposed": "Entities proposed",
    "entities_new": "Entity IDs made new",
    "entities_kept": "Entity IDs kept from the registry",
    "entities_merged": "Entity IDs that absorbed another",
    "attribute_ties": "Values left undecided",
    "id_collisions": "Entity IDs claimed twice",
    "published_at": "Published at",
}

#: Counts that say nothing to a reader of a spreadsheet.
SKIP_COUNTS = {
    "has_records", "has_exact", "has_pairs", "has_units", "has_labels",
    "has_clusters", "has_entities", "model_active", "model_graded",
    "model_accept_line", "model_reject_line", "model_version", "model_warning",
}

#: The columns the donations export appends, with their plain definitions.
DONATIONS_ADDED_COLUMNS = [
    ("RecordID", "The record this row belongs to. Several rows can share one."),
    ("EntityID", "The ID of the one person or organisation this record is part of. "
                 "It stays the same from run to run."),
    ("EntityBasis", "How it was decided that this record belongs to that entity. "
                    "One of the labels in the list below."),
    ("DonorStatusStandardNew", "The donor status the tool settled on for the whole "
                               "entity, which may differ from the raw status on the row."),
    ("DonorStatusBasis", "How that value was set: Derived column rule, Most members, "
                         "Only member, Undecided, or Reviewer."),
]

#: The columns the PSC export writes.
PSC_ADDED_COLUMNS = [
    ("record_id", "The PSC record. It is also the document id in the search index."),
    ("entity_id", "The ID of the one person or organisation this record is part of. "
                  "It stays the same from run to run."),
    ("entity_basis", "How it was decided that this record belongs to that entity. "
                     "One of the labels in the list below."),
    ("track", "Which track the record was matched inside: people or organisations."),
    ("company_number", "The company number on the record, as cleaned."),
    ("name", "The name on the record."),
]


def _plain_key(key: str) -> str:
    """A snake_case or camelCase key as a sentence. The last resort, never the plan."""
    spaced = "".join(
        f" {char.lower()}" if char.isupper() else char for char in str(key)
    ).replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:] if spaced else str(key)


def count_label(key: str) -> str:
    return COUNT_LABELS.get(str(key)) or _plain_key(key)


def _count_rows(counts: dict) -> list[list]:
    """Every count, under its plain name, with the status breakdown spelled out."""
    rows: list[list] = []
    for key in sorted(counts):
        if key in SKIP_COUNTS:
            continue
        value = counts[key]
        if key == "clusters_by_status" and isinstance(value, dict):
            for status in sorted(value):
                label = vocabulary.CLUSTER_STATUS.get(status, {}).get(
                    "label", _plain_key(status))
                rows.append([f"Clusters for review — {label}", value[status]])
            continue
        if isinstance(value, dict):
            for inner in sorted(value):
                rows.append([f"{count_label(key)} — {_plain_key(inner)}", value[inner]])
            continue
        if isinstance(value, list):
            value = json.dumps(value)
        rows.append([count_label(key), value])
    return rows


def _scorer_rows(manifest: dict, bucketing: dict | None) -> list[list]:
    """Per track: the scorer that decided, and which model, trained when."""
    rows: list[list] = []
    scorers = manifest.get("scorers") or {}
    if not scorers:
        scorer = (bucketing or {}).get("scorer") or "splink"
        rows.append(["Scored by", "The model" if scorer == "model"
                     else "Splink, the unsupervised engine"])
        return rows
    for track in sorted(scorers):
        detail = scorers[track] or {}
        name = vocabulary.TRACK.get(track, {}).get("label", _plain_key(track))
        rows.append([f"Scored by — {name}",
                     "The model" if detail.get("scorer") == "model"
                     else "Splink, the unsupervised engine"])
        if detail.get("model_version") is not None:
            rows.append([f"Model version — {name}", detail["model_version"]])
            rows.append([f"Model graded — {name}",
                         "Yes, it may set the buckets" if detail.get("graded")
                         else "No, it only re-orders the review queue"])
            if detail.get("trained_at"):
                rows.append([f"Model trained on — {name}", detail["trained_at"]])
            if detail.get("n_human_labels") is not None:
                rows.append([f"Reviewer answers it learnt from — {name}",
                             detail["n_human_labels"]])
            if detail.get("n_train_rows") is not None:
                rows.append([f"Training rows — {name}", detail["n_train_rows"]])
    return rows


def run_rows(context: dict, run_dir=None) -> list[list]:
    """The run sheet: what produced this file, in plain words.

    *context* is what the export router builds. *run_dir* lets the sheet read
    the run's own manifest and bucketing history; without it the sheet falls
    back to what the context carries.
    """
    manifest = context.get("manifest") or (
        run_manifest.read(run_dir) if run_dir else {})
    bucketing = context.get("bucketing") or (
        bucketing_history.current(run_dir) if run_dir else None)
    counts = context.get("counts") or {}
    thresholds = manifest.get("thresholds") or {}
    file_facts = manifest.get("input") or {}

    rows: list[list] = [
        ["What this file is", ""],
        ["Run", context.get("run_id")],
        ["Run started", manifest.get("started_at")],
        ["Run finished", manifest.get("finished_at")],
        ["Run started by", manifest.get("triggered_by") or context.get("triggered_by")],
        ["Scope", _scope_label(context.get("scope"))],
        ["Exported at", context.get("exported_at")],
        ["Exported by", context.get("exported_by")],
        ["Published at", context.get("published_at") or "Not published"],
        ["", ""],
        ["What produced it", ""],
        ["Code version", manifest.get("code_version") or "unknown"],
        ["Config version", context.get("config_version")
         or manifest.get("config_version")],
        ["Input file", file_facts.get("filename") or context.get("input_name")],
        ["Input file size in bytes", file_facts.get("size_bytes")],
        ["Input file sha256", file_facts.get("sha256")],
        ["Rows in the input file", file_facts.get("row_count")],
        ["Input file uploaded at", file_facts.get("uploaded_at")],
        ["", ""],
        ["The lines in force", ""],
        ["Accept line", thresholds.get("accept_line")
         if thresholds else (bucketing or {}).get("accept_line")],
        # A track may read a line of its own, so one number is no longer the
        # whole answer (docs/LINKAGE.md). One row per track that set one.
        *[[f"Accept line, {track}", line] for track, line in sorted(
            ((thresholds.get("accept_line_by_track")
              if thresholds else (bucketing or {}).get("accept_line_by_track"))
             or {}).items())],
        ["Review line", thresholds.get("review_line")
         if thresholds else (bucketing or {}).get("review_line")],
        ["Lowest score kept", thresholds.get("lowest_score_kept")
         if thresholds else (bucketing or {}).get("lowest_score_kept")],
    ]
    if bucketing:
        rows += [
            ["Buckets last set", bucketing.get("at")],
            ["Buckets last set by", bucketing.get("who") or "the run"],
            ["What set them", bucketing.get("action")],
        ]
    rows.append(["", ""])
    rows.append(["The scorer", ""])
    rows += _scorer_rows(manifest, bucketing)

    references = manifest.get("references") or []
    if references:
        rows.append(["", ""])
        rows.append(["Reference tables", ""])
        for reference in references:
            name = (reference.get("label") or _plain_key(
                reference.get("name") or reference.get("key") or ""))
            rows.append([f"{name} — rows", reference.get("rows")])
            rows.append([f"{name} — built at", reference.get("built_at")])
            rows.append([f"{name} — sha256", reference.get("sha256")])
            if reference.get("source_sha256"):
                rows.append([f"{name} — source sha256", reference["source_sha256"]])

    libraries = manifest.get("libraries") or {}
    if libraries:
        rows.append(["", ""])
        rows.append(["Library versions", ""])
        for name in sorted(libraries):
            rows.append([name, libraries[name]])

    rows.append(["", ""])
    rows.append(["What the run found", ""])
    rows += _count_rows(counts)
    return rows


def _scope_label(scope) -> str:
    if scope == "published":
        return "Published — the entity IDs in the registry"
    if scope == "proposal":
        return "Proposal — what this run suggests, not yet in the registry"
    return str(scope or "")


def how_to_read_rows(added_columns) -> list[list]:
    """One row per added column, then the ordered provenance list."""
    rows: list[list] = [
        ["How to read this file", ""],
        ["", ""],
        ["Column", "What it means"],
    ]
    rows += [[name, definition] for name, definition in added_columns]
    rows += [
        ["", ""],
        [vocabulary.PROVENANCE_QUESTION, "What it means"],
    ]
    rows += [[entry["label"], entry["definition"]] for entry in vocabulary.PROVENANCE]
    rows.append([vocabulary.SUGGESTED["label"], vocabulary.SUGGESTED["definition"]])
    rows += [
        ["", ""],
        ["Reading the list", vocabulary.PROVENANCE_PRECEDENCE],
        ["", ""],
        [vocabulary.VALUE_BASIS_QUESTION, "What it means"],
    ]
    rows += [[meta["label"], meta["definition"]]
             for meta in vocabulary.VALUE_BASIS.values()]
    rows += [
        ["", ""],
        [vocabulary.ID_ORIGIN_QUESTION, "What it means"],
    ]
    rows += [[meta["label"], meta["definition"]]
             for meta in vocabulary.ID_ORIGIN.values()]
    return rows


def as_text(rows: list[list], title: str = "") -> str:
    """The same rows as plain text, for a README beside a CSV.

    A row whose value is the empty string is a section heading — that is how
    ``run_rows`` marks one. A row whose value is None is a fact nobody
    recorded, and it says so rather than turning into a heading.
    """
    lines = [title, "=" * len(title), ""] if title else []
    for label, value in rows:
        heading = value == ""
        if not str(label) and heading:
            lines.append("")
        elif heading:
            lines.append(f"{label}")
            lines.append("-" * len(str(label)))
        else:
            lines.append(f"{label}: {'not recorded' if value is None else value}")
    return "\n".join(lines) + "\n"


def sidecar_path(out_path, suffix: str = "_run_sheet.txt") -> Path:
    """Where a CSV export's run sheet goes, beside the CSV itself."""
    out_path = Path(out_path)
    return out_path.with_name(out_path.stem + suffix)

# backend/app/vocabulary.py
"""One vocabulary, defined once, served once.

Every enumerated value that reaches a user is named here: the value the API
carries, the one label to show, and a one-sentence definition. Nothing else in
the backend may invent a second word for the same thing.

Three rules this file keeps (`docs/DESIGN.md` D21, D22, `docs/GLOSSARY.md`):

1. **One name per thing.** A value has exactly one label. If the label changes,
   it changes here and everywhere at once.
2. **One ordered provenance list.** "How it was decided" is answered by six
   labels, weakest first: On its own, Match key, Score, Veto rule, Earlier
   grouping, Reviewer. A later one always beats an earlier one. "Suggested"
   sits outside the list and never ranks.
3. **Three other questions keep their own words.** How this value was set,
   where this ID came from, and how a group compares with the earlier grouping
   are different questions. Folding them into the list above would claim that a
   majority vote on one column is evidence that two records are one thing.

The frontend's copy of this is ``frontend/src/glossary.js``. They are checked
against each other by ``backend/tests/test_vocabulary.py``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. How it was decided — the one ordered list
# ---------------------------------------------------------------------------

PROVENANCE_QUESTION = "How it was decided"

PROVENANCE_PRECEDENCE = (
    "Read the list from the top down. Anything lower beats anything above it, "
    "so a reviewer's answer beats the earlier grouping, the earlier grouping "
    "beats a veto rule, and a veto rule beats the score."
)

#: Weakest first. ``order`` is the precedence: a higher number always wins.
PROVENANCE: tuple[dict, ...] = (
    {
        "key": "alone",
        "order": 1,
        "label": "On its own",
        "definition": "Nothing joined this record to any other.",
    },
    {
        "key": "matchKey",
        "order": 2,
        "label": "Match key",
        "definition": "A match key found the same values in every one of its columns.",
    },
    {
        "key": "score",
        "order": 3,
        "label": "Score",
        "definition": "The score reached the accept line.",
    },
    {
        "key": "veto",
        "order": 4,
        "label": "Veto rule",
        "definition": "A veto rule moved this pair, whatever the score said.",
    },
    {
        "key": "earlier",
        "order": 5,
        "label": "Earlier grouping",
        "definition": "Both sides already carried the same earlier ID.",
    },
    {
        "key": "reviewer",
        "order": 6,
        "label": "Reviewer",
        "definition": "A person decided it.",
    },
)

#: Outside the ordered list. A machine-written suggestion is a proposal, not a
#: decision, so it never ranks against the six above.
SUGGESTED = {
    "key": "suggested",
    "order": None,
    "label": "Suggested",
    "definition": "A machine wrote this answer. It is a proposal. Check it before you trust it.",
}

PROVENANCE_BY_KEY: dict[str, dict] = {
    entry["key"]: entry for entry in (*PROVENANCE, SUGGESTED)
}

#: The precedence number for one provenance key. ``suggested`` never ranks, so
#: it sorts below everything.
PROVENANCE_ORDER: dict[str, int] = {
    entry["key"]: entry["order"] for entry in PROVENANCE
}


def provenance_rank(key: str | None) -> int:
    """How strongly *key* decides. Unknown and ``suggested`` both rank 0."""
    return PROVENANCE_ORDER.get(str(key or ""), 0)


# ---------------------------------------------------------------------------
# 2. Every stored value, mapped onto the list
# ---------------------------------------------------------------------------
#
# ``key`` names one of the six (or ``suggested``). ``detail`` is the second
# line a chip shows when the value says more than the label does.

PROVENANCE_MAP: dict[str, dict[str, dict]] = {
    # pairs.parquet / pair detail — which authority put the pair in its bucket
    "decided_by": {
        "score": {"key": "score", "detail": "Splink score"},
        "model": {"key": "score", "detail": "Model score"},
        "veto": {"key": "veto"},
        "import": {"key": "earlier"},
        "human": {"key": "reviewer"},
        "exact_key": {"key": "matchKey"},
        "single": {"key": "alone"},
    },
    # entities.parquet / registry entity_members — why a record sits in its entity
    "entity_basis": {
        "single": {"key": "alone"},
        "exact_key": {"key": "matchKey"},
        "score": {"key": "score"},
        "veto": {"key": "veto"},
        "import": {"key": "earlier"},
        "human": {"key": "reviewer"},
    },
    # clusters.parquet edge_source / entity_edges.source — one accepted link
    "edge_source": {
        "score": {"key": "score"},
        "model": {"key": "score", "detail": "Model score"},
        "exact_key": {"key": "matchKey"},
        "veto": {"key": "veto"},
        "import": {"key": "earlier"},
        "human": {"key": "reviewer"},
    },
    # pair_labels.provenance — how one saved answer was reached
    "provenance": {
        "manual": {"key": "reviewer", "detail": "One pair at a time"},
        "bulk_range": {"key": "reviewer", "detail": "A band of scores at once"},
        "bulk_review": {"key": "reviewer", "detail": "A band of scores at once"},
        "cluster_merge": {"key": "reviewer", "detail": "A whole cluster merged"},
        "cluster_split": {"key": "reviewer", "detail": "A cluster split"},
        "import": {"key": "earlier"},
        "llm": {"key": "suggested"},
    },
    # any veto id, or any truthy vetoed_by
    "vetoed_by": {"*": {"key": "veto"}},
}


def provenance_for(field: str, value, detail: str | None = None) -> dict | None:
    """One stored value, turned into the one label.

    Returns ``{key, order, label, definition, detail}``, or None when the value
    is empty or unknown — so a caller can leave the chip out rather than print
    a raw word at a researcher.
    """
    if value is None or value is False or value == "":
        return None
    table = PROVENANCE_MAP.get(field)
    if not table:
        return None
    hit = table.get(str(value).lower()) or table.get("*")
    if not hit:
        return None
    meta = PROVENANCE_BY_KEY.get(hit["key"])
    if not meta:
        return None
    return {**meta, "detail": detail or hit.get("detail")}


def provenance_label(field: str, value, detail: str | None = None) -> str:
    """The label alone, with its detail in brackets. Empty when unknown."""
    found = provenance_for(field, value, detail)
    if not found:
        return ""
    if found.get("detail"):
        return f"{found['label']} ({found['detail']})"
    return found["label"]


# ---------------------------------------------------------------------------
# 3. The questions that are NOT "how it was decided"
# ---------------------------------------------------------------------------

VALUE_BASIS_QUESTION = "How this value was set"

#: entity_attributes.basis, entities.parquet ``<column>_entity_basis``.
VALUE_BASIS: dict[str, dict] = {
    "rule": {
        "label": "Derived column rule",
        "definition": "A derived column rule set this value. It beats a raw value.",
    },
    "majority": {
        "label": "Most members",
        "definition": "The most common value among this entity's records.",
    },
    "raw": {
        "label": "Only member",
        "definition": "One record, so its own value stands.",
    },
    "tie": {
        "label": "Undecided",
        "definition": "Two values were equally common, so each record keeps its own.",
    },
    "human": {
        "label": "Reviewer",
        "definition": "A person set this value by hand.",
    },
}

ID_ORIGIN_QUESTION = "Where this ID came from"

#: entities.parquet ``id_status``, registry ``entity_members.id_status``.
ID_ORIGIN: dict[str, dict] = {
    "new": {
        "label": "New",
        "definition": "Nothing in the registry claimed these records, so the tool made a new ID.",
    },
    "kept": {
        "label": "Kept",
        "definition": "One registry entity already held these records, so its ID stands.",
    },
    "survivor": {
        "label": "Survived a merge",
        "definition": (
            "Several registry entities held these records. This ID survives and "
            "the others become retired IDs."
        ),
    },
    "minted_after_collision": {
        "label": "Re-made after a clash",
        "definition": (
            "Two proposed entities claimed the same earlier ID. This one gave way "
            "and took a new ID."
        ),
    },
}

AGREEMENT_QUESTION = "Against the earlier grouping"

#: exact_groups.parquet ``agreement``.
AGREEMENT: dict[str, dict] = {
    "consistent": {
        "label": "Agrees",
        "definition": "Every record here carried the same earlier ID.",
    },
    "conflict": {
        "label": "Conflicts",
        "definition": "The records here carried different earlier IDs.",
    },
    "extends": {
        "label": "Adds to a group",
        "definition": "This adds records the earlier grouping had left out.",
    },
    "new": {
        "label": "New",
        "definition": "The earlier grouping said nothing about these records.",
    },
}

#: score_eval.json — whether one accepted pair matches the earlier grouping.
#: A second list that used to share the name ``AGREEMENTS`` with the one above
#: (`docs/TERMINOLOGY_AUDIT.md` B19). It answers a different question, so it
#: has a different name.
IMPORT_AGREEMENT_QUESTION = "Against the earlier grouping, pair by pair"

IMPORT_AGREEMENT: dict[str, dict] = {
    "agrees": {
        "label": "Agrees",
        "definition": "Both sides carried the same earlier ID, and this run joins them too.",
    },
    "disagrees": {
        "label": "Disagrees",
        "definition": "The two sides carried different earlier IDs.",
    },
    "unknown": {
        "label": "Not covered",
        "definition": "At least one side carried no earlier ID, so there is nothing to compare.",
    },
}


# ---------------------------------------------------------------------------
# 4. The other enumerated values a user sees
# ---------------------------------------------------------------------------

BUCKET_QUESTION = "Where this pair landed"

#: pairs.parquet ``bucket`` and ``score_bucket``.
BUCKET: dict[str, dict] = {
    "accept": {
        "label": "Accepted",
        "definition": "The tool joins these two without asking.",
    },
    "review": {
        "label": "For review",
        "definition": "The tool cannot decide. A person has to.",
    },
    "reject": {
        "label": "Rejected",
        "definition": "The tool keeps these two apart.",
    },
}

EXACT_GROUP_STATUS_QUESTION = "What the match key did"

#: exact_groups.parquet ``status``.
EXACT_GROUP_STATUS: dict[str, dict] = {
    "merged": {
        "label": "Merged",
        "definition": "A match key put these records together.",
    },
    "held": {
        "label": "Held",
        "definition": "A guard stopped the merge. These records wait for a person.",
    },
}

CLUSTER_STATUS_QUESTION = "Why this cluster is here"

#: clusters.parquet ``status``. These say what the gate found. They are not
#: "how it was decided", so they keep their own list.
CLUSTER_STATUS: dict[str, dict] = {
    "ok": {
        "label": "Proposed",
        "definition": "The gate found nothing wrong, so this cluster is proposed as one entity.",
    },
    "conflict": {
        "label": "Conflict",
        "definition": "A reviewer has already said two of these records are not the same.",
    },
    "too_large": {
        "label": "Too large",
        "definition": (
            "This cluster holds more units than the limit. That usually means the "
            "accept line is too low, or a match key is too loose."
        ),
    },
    "weak_link": {
        "label": "Weak link",
        "definition": (
            "Two units inside this cluster scored very low against each other, so "
            "it may be a chain."
        ),
    },
    "mixed_ids": {
        "label": "Mixed earlier IDs",
        "definition": "This cluster joins records that the earlier grouping gave different IDs.",
    },
    "held_key": {
        "label": "Held by a match key",
        "definition": (
            "A guard on a match key stopped these records being put together, so "
            "they are still separate and waiting for a person."
        ),
    },
    "cross_track_ids": {
        "label": "Earlier ID spans tracks",
        "definition": (
            "An earlier ID covers a person and an organisation. The tool keeps them "
            "as two entities."
        ),
    },
    "attribute_tie": {
        "label": "Value undecided",
        "definition": (
            "Two values were equally common, so one value for the whole cluster "
            "could not be settled."
        ),
    },
}

RUN_STATUS_QUESTION = "Where this run got to"

#: runs.status.
RUN_STATUS: dict[str, dict] = {
    "pending": {
        "label": "Not started",
        "definition": "The run is set up and has not begun.",
    },
    "queued": {
        "label": "Waiting",
        "definition": "Another tool holds the run lock. This run starts when that one ends.",
    },
    "running": {
        "label": "Running",
        "definition": "The run is working through its stages now.",
    },
    "complete": {
        "label": "Finished",
        "definition": "Every stage finished and the run's files are on disk.",
    },
    "failed": {
        "label": "Failed",
        "definition": "A stage stopped with an error. The run has no entity IDs.",
    },
}

#: The terms the backend names in a definition it serves. The whole list lives
#: in `docs/GLOSSARY.md` and in `frontend/src/glossary.js`; these three are the
#: ones an API response has to be able to define on its own.
TERMS: dict[str, dict] = {
    "link": {
        "term": "link",
        "plural": "links",
        "definition": (
            "One accepted join between two records inside an entity. It carries "
            "what joined them: a match key, a score, an earlier ID or a reviewer."
        ),
    },
    "codeVersion": {
        "term": "code version",
        "plural": "code versions",
        "definition": (
            "The exact copy of this tool's code that produced a run. It is named "
            "by the commit it was built from."
        ),
    },
    "fileFingerprint": {
        "term": "file fingerprint",
        "plural": "file fingerprints",
        "definition": (
            "A short code worked out from a file's contents. Two files with the "
            "same fingerprint hold exactly the same bytes, whatever they are named."
        ),
    },
}

ANSWER_SOURCE_QUESTION = "Where this answer was read from"

#: The ``source`` on an entity provenance response.
ANSWER_SOURCE: dict[str, dict] = {
    "run": {
        "label": "From this run's files",
        "definition": (
            "Read from the run folder. Delete the run and this answer goes with "
            "it, unless the run was published."
        ),
    },
    "registry": {
        "label": "From the registry",
        "definition": (
            "Read from the registry, which keeps this after a run is deleted. "
            "Publishing writes it down."
        ),
    },
}

RULES_REPLAYED_QUESTION = "Which rules these steps used"

#: The ``source`` on a cleaning preview.
RULES_REPLAYED: dict[str, dict] = {
    "run": {
        "label": "This run's own rules",
        "definition": (
            "The frozen rules this run used, replayed on this record. They are "
            "what made the value you are looking at."
        ),
    },
    "draft": {
        "label": "The rules you are editing",
        "definition": (
            "The draft rules on this screen. Nothing has been saved or run with "
            "them yet."
        ),
    },
}

SCORER_QUESTION = "Which score decided"

#: The ``scorer`` on a run's details and on a change to the lines.
SCORER: dict[str, dict] = {
    "splink": {
        "label": "Splink score",
        "definition": (
            "The score the unsupervised engine works out from the shape of the data."
        ),
    },
    "model": {
        "label": "Model score",
        "definition": "The score the trained model works out from saved labels.",
    },
}

LINE_CHANGE_QUESTION = "What moved the lines"

#: The ``action`` on an entry in a run's history of the lines. These four
#: strings are what ``app/services/bucketing_history.py`` writes, so the keys
#: here and the stored values are the same words.
LINE_CHANGE: dict[str, dict] = {
    "scored": {
        "label": "First scored",
        "definition": (
            "The run scored its pairs and put each one in a bucket for the first time."
        ),
    },
    "re-bucketed": {
        "label": "Lines moved",
        "definition": (
            "Someone moved the accept line or the review line, so every pair was "
            "put in a bucket again."
        ),
    },
    "model applied": {
        "label": "Model applied",
        "definition": (
            "A trained model scored the same pairs, and the buckets were set on "
            "its score."
        ),
    },
    "model reverted": {
        "label": "Model taken off",
        "definition": (
            "The model was taken off this run, so the buckets went back to the "
            "Splink score."
        ),
    },
}

ANSWER_QUESTION = "The answer on this pair"

#: pair_labels.is_match. The API keeps TRUE and FALSE; the screen never does.
ANSWER: dict[str, dict] = {
    "TRUE": {
        "label": "Match",
        "definition": "These two records are the same person or the same organisation.",
    },
    "FALSE": {
        "label": "Not a match",
        "definition": "These two records are different people or different organisations.",
    },
}

TRACK_QUESTION = "Which track"

TRACK: dict[str, dict] = {
    "person": {
        "label": "People",
        "definition": "Records the track rules read as a person.",
    },
    "organisation": {
        "label": "Organisations",
        "definition": "Records the track rules read as an organisation.",
    },
}

ID_STATUS_QUESTION = ID_ORIGIN_QUESTION  # one name, kept for readability


# ---------------------------------------------------------------------------
# 5. The stages, named and never numbered
# ---------------------------------------------------------------------------
#
# `docs/DESIGN.md` D21: stages are named. Three numberings are in use and any
# renumbering breaks the other two, so the key and the label are the handle.
# ``dedupe/__init__.py`` serves this list at ``GET /api/pipeline/stages``.

STAGES: tuple[dict, ...] = (
    {
        "key": "load",
        "label": "Load",
        "description": "Read the input file into one row per record (records_raw.parquet).",
    },
    {
        "key": "clean",
        "label": "Clean",
        "description": (
            "Give each record a track and run that track's cleaning steps, "
            "writing a new column beside each raw one (records.parquet)."
        ),
    },
    {
        "key": "derive",
        "label": "Derived columns",
        "description": (
            "Set the derived columns from their ordered rules, so a category the "
            "source often gets wrong reads the same way everywhere. This runs "
            "inside the clean stage, after the cleaning steps."
        ),
    },
    {
        "key": "exact",
        "label": "Match keys",
        "description": (
            "Put together the records that agree on a match key, hold back the "
            "groups a guard stops, and measure the result against the earlier "
            "grouping (exact_groups.parquet)."
        ),
    },
    {
        "key": "score",
        "label": "Score pairs",
        "description": (
            "Compare the units the match keys left — one per exact group, one per "
            "other record — with Splink, and put every pair in a bucket: "
            "Accepted, For review or Rejected (pairs.parquet)."
        ),
    },
    {
        "key": "model",
        "label": "Model score",
        "description": (
            "Score the same pairs again with the trained model, where one is "
            "active. A graded model may set the buckets; a new model only "
            "re-orders the review queue (pairs.parquet, gbt_score)."
        ),
    },
    {
        "key": "cluster",
        "label": "Cluster",
        "description": (
            "Join the accepted pairs into clusters, and hold back for review the "
            "ones that look like a chain, are too large, or would merge records "
            "the earlier grouping kept apart (clusters.parquet)."
        ),
    },
    {
        "key": "entities",
        "label": "Entity IDs",
        "description": (
            "Give every record an entity ID, keeping the ones the registry "
            "already holds, and settle the entity-level values "
            "(entities.parquet)."
        ),
    },
)

#: The stages that run as their own step, in order. ``derive`` and ``model``
#: are shown to a reader but are not separate steps in the runner.
PIPELINE_STAGE_KEYS = ("load", "clean", "exact", "score", "cluster", "entities")

STAGE_BY_KEY: dict[str, dict] = {stage["key"]: stage for stage in STAGES}


# ---------------------------------------------------------------------------
# 6. The tuples the rest of the backend validates against
# ---------------------------------------------------------------------------
#
# One definition each. Modules that used to hold their own copy import these.

BUCKETS = tuple(BUCKET)
DECIDED_BY = ("score", "import", "human", "model", "veto")
ENTITY_BASES = ("single", "exact_key", "score", "veto", "import", "human")
EDGE_SOURCES = ("score", "model", "exact_key", "veto", "import", "human")
LABEL_PROVENANCES = ("manual", "bulk_range", "llm", "import", "cluster_merge",
                     "cluster_split")
ATTRIBUTE_BASES = tuple(VALUE_BASIS)
ID_STATUSES = tuple(ID_ORIGIN)
AGREEMENTS = tuple(AGREEMENT)
IMPORT_AGREEMENTS = tuple(IMPORT_AGREEMENT)
EXACT_GROUP_STATUSES = tuple(EXACT_GROUP_STATUS)
CLUSTER_STATUSES = tuple(CLUSTER_STATUS)
RUN_STATUSES = tuple(RUN_STATUS)
ANSWERS = tuple(ANSWER)
TRACKS = tuple(TRACK)
ANSWER_SOURCES = tuple(ANSWER_SOURCE)
RULES_REPLAYED_SOURCES = tuple(RULES_REPLAYED)
SCORERS = tuple(SCORER)
LINE_CHANGES = tuple(LINE_CHANGE)


# ---------------------------------------------------------------------------
# 7. The precedence D22 settles
# ---------------------------------------------------------------------------
#
# `docs/DESIGN.md` D22 and `docs/RULESET.md`: Earlier grouping beats Score. The
# old order in ``stage_4_cluster.py`` had it the other way round, so a record
# whose path held both an earlier-grouping edge and a score edge reported
# "Score". One list now, derived from the ordered provenance list, so the two
# cannot drift apart again.

BASIS_ORDER: dict[str, int] = {
    "single": provenance_rank("alone") - 1,      # 0
    "exact_key": provenance_rank("matchKey") - 1,  # 1
    "score": provenance_rank("score") - 1,       # 2
    "model": provenance_rank("score") - 1,       # 2 — the model is a scorer
    "veto": provenance_rank("veto") - 1,         # 3
    "import": provenance_rank("earlier") - 1,    # 4
    "human": provenance_rank("reviewer") - 1,    # 5
}


# ---------------------------------------------------------------------------
# 8. What GET /api/vocabulary serves
# ---------------------------------------------------------------------------


def _values(table: dict[str, dict], field: str | None = None) -> list[dict]:
    """One field's values, each with its label, definition and provenance key."""
    out = []
    for value, meta in table.items():
        entry = {"value": value, "label": meta["label"], "definition": meta["definition"]}
        if field:
            mapped = provenance_for(field, value)
            if mapped:
                entry["provenance"] = mapped["key"]
                entry["provenance_label"] = mapped["label"]
                if mapped.get("detail"):
                    entry["detail"] = mapped["detail"]
        out.append(entry)
    return out


def _provenance_field(field: str, values: tuple[str, ...]) -> list[dict]:
    """A field whose values are only an answer to "how it was decided"."""
    out = []
    for value in values:
        mapped = provenance_for(field, value)
        if not mapped:
            continue
        entry = {
            "value": value,
            "label": mapped["label"],
            "definition": mapped["definition"],
            "provenance": mapped["key"],
        }
        if mapped.get("detail"):
            entry["detail"] = mapped["detail"]
        out.append(entry)
    return out


def as_dict() -> dict:
    """The whole vocabulary, as one JSON-safe document."""
    return {
        "provenance": {
            "question": PROVENANCE_QUESTION,
            "precedence": PROVENANCE_PRECEDENCE,
            "ordered": [dict(entry) for entry in PROVENANCE],
            "outside": dict(SUGGESTED),
            "map": {
                field: {value: dict(hit) for value, hit in table.items()}
                for field, table in PROVENANCE_MAP.items()
            },
        },
        "fields": {
            "decided_by": {
                "question": PROVENANCE_QUESTION,
                "definition": "Which authority put this pair in its bucket.",
                "values": _provenance_field("decided_by", DECIDED_BY),
            },
            "entity_basis": {
                "question": PROVENANCE_QUESTION,
                "definition": "Why this record sits in this entity.",
                "values": _provenance_field("entity_basis", ENTITY_BASES),
            },
            "edge_source": {
                "question": PROVENANCE_QUESTION,
                "definition": "What joined these two units.",
                "values": _provenance_field("edge_source", EDGE_SOURCES),
            },
            "provenance": {
                "question": PROVENANCE_QUESTION,
                "definition": "How this saved answer was reached.",
                "values": _provenance_field("provenance", LABEL_PROVENANCES),
            },
            "attribute_basis": {
                "question": VALUE_BASIS_QUESTION,
                "definition": "How this entity settled on one value for a column.",
                "values": _values(VALUE_BASIS),
            },
            "id_status": {
                "question": ID_ORIGIN_QUESTION,
                "definition": "Where this entity's ID came from.",
                "values": _values(ID_ORIGIN),
            },
            "agreement": {
                "question": AGREEMENT_QUESTION,
                "definition": "How an exact group compares with the earlier grouping.",
                "values": _values(AGREEMENT),
            },
            "import_agreement": {
                "question": IMPORT_AGREEMENT_QUESTION,
                "definition": "How one accepted pair compares with the earlier grouping.",
                "values": _values(IMPORT_AGREEMENT),
            },
            "bucket": {
                "question": BUCKET_QUESTION,
                "definition": "Where a pair landed once every rule had its say.",
                "values": _values(BUCKET),
            },
            "score_bucket": {
                "question": BUCKET_QUESTION,
                "definition": "Where the score alone would have put the pair.",
                "values": _values(BUCKET),
            },
            "exact_group_status": {
                "question": EXACT_GROUP_STATUS_QUESTION,
                "definition": "Whether a match key's group was merged or held.",
                "values": _values(EXACT_GROUP_STATUS),
            },
            "cluster_status": {
                "question": CLUSTER_STATUS_QUESTION,
                "definition": "What the gate found when it looked at this cluster.",
                "values": _values(CLUSTER_STATUS),
            },
            "run_status": {
                "question": RUN_STATUS_QUESTION,
                "definition": "Where a run got to.",
                "values": _values(RUN_STATUS),
            },
            "is_match": {
                "question": ANSWER_QUESTION,
                "definition": "The two answers a reviewer can save on a pair.",
                "values": _values(ANSWER),
            },
            "track": {
                "question": TRACK_QUESTION,
                "definition": "Which track a record is matched inside.",
                "values": _values(TRACK),
            },
            "answer_source": {
                "question": ANSWER_SOURCE_QUESTION,
                "definition": "Where a provenance answer was read from.",
                "values": _values(ANSWER_SOURCE),
            },
            "rules_replayed": {
                "question": RULES_REPLAYED_QUESTION,
                "definition": "Which rules a cleaning preview replayed.",
                "values": _values(RULES_REPLAYED),
            },
            "scorer": {
                "question": SCORER_QUESTION,
                "definition": "Which score decided a pair.",
                "values": _values(SCORER),
            },
            "line_change": {
                "question": LINE_CHANGE_QUESTION,
                "definition": "What last moved the lines that set the buckets.",
                "values": _values(LINE_CHANGE),
            },
        },
        "terms": {key: dict(entry) for key, entry in TERMS.items()},
        "stages": [dict(stage) for stage in STAGES],
    }


def labels_for(field: str) -> dict[str, str]:
    """``{value: label}`` for one field name, for the exports and the docs."""
    served = as_dict()["fields"].get(field)
    if not served:
        return {}
    return {entry["value"]: entry["label"] for entry in served["values"]}


def label_for(field: str, value, fallback: str = "") -> str:
    """One value's label, or *fallback* when nothing is known about it."""
    if value is None or value == "":
        return fallback
    return labels_for(field).get(str(value), fallback or str(value))


# ---------------------------------------------------------------------------
# 9. Saying what went wrong, without an internal name
# ---------------------------------------------------------------------------
#
# Validation used to answer a researcher with ``fallback must be one of keep,
# blank, error`` (`docs/BACKEND_STRINGS.md` §3). The field name and the list of
# values are the only handle support has, so they stay — but they arrive inside
# a sentence that says what the setting is for.

SETTING_NAMES: dict[str, str] = {
    "action": "what a veto rule does to a pair",
    "applies_when": "when a match key is allowed to apply",
    "column": "which column this value belongs to",
    "default_track": "the track a record falls into when no rule matches it",
    "fallback": "what happens to a value the lookup table does not list",
    "is_match": "the answer on a pair",
    "kind": "the kind of decision",
    "on_guard_fail": "what happens when a guard stops a group",
    "on_oversize": "what happens when a blocking rule makes too many pairs",
    "op": "what a cleaning step does",
    "position": "where in the value to look",
    "scope": "what a lookup replaces",
    "splink_function": "how a column is compared",
    "track": "which track this applies to",
}


def choice_error(field: str, value, options, what: str | None = None) -> str:
    """One sentence for a value that is not one of a fixed set.

    Names the setting in words, says what was given, and lists what is allowed.
    The allowed values are the ones the file holds, because the reader has to
    type one of them back.
    """
    what = what or SETTING_NAMES.get(field, field.replace("_", " "))
    allowed = ", ".join(str(option) for option in options) or "(nothing)"
    given = "nothing" if value in (None, "") else repr(value)
    return (f"{given} does not say {what}. Choose one of: {allowed}.")

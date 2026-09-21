# backend/tests/test_vocabulary.py
"""One vocabulary — and a check that the two copies of it agree.

The backend's copy is ``app/vocabulary.py``. The frontend's is
``frontend/src/glossary.js``. They are written in different languages, so they
cannot import each other; this file reads the JavaScript and compares the two
word for word. If someone changes a label in one place and not the other, this
test says which label and which file.

The JavaScript is read with a tolerant regular expression, not a parser. The
file is hand-written and formatted by Prettier, so the shapes it looks for are
stable. If that ever stops being true the comparison moves to
``backend/scripts/check_vocabulary.py`` and runs as a documented script.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import vocabulary

GLOSSARY_JS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "glossary.js"


# ---------------------------------------------------------------------------
# Reading the JavaScript
# ---------------------------------------------------------------------------


def _block(source: str, name: str) -> str:
    """The text of one ``export const NAME = …;`` declaration.

    The declaration ends at the first ``];`` or ``};`` that starts a line, which
    is how Prettier closes a top-level array or object.
    """
    start = source.find(f"export const {name} =")
    if start < 0:
        pytest.skip(f"{name} is not exported from glossary.js")
    end = re.search(r"^\s*[\]}];", source[start:], re.MULTILINE)
    assert end, f"could not find the end of {name} in glossary.js"
    return source[start:start + end.end()]


def _ordered_entries(block: str) -> dict[str, dict]:
    """``{key: {label, definition}}`` from a list of ``{key, order, label, …}``."""
    found = {}
    pattern = re.compile(
        r'key:\s*"(?P<key>[^"]+)".*?label:\s*"(?P<label>[^"]+)".*?'
        r'definition:\s*(?P<definition>"(?:[^"\\]|\\.)*"(?:\s*\+\s*"(?:[^"\\]|\\.)*")*)',
        re.DOTALL,
    )
    for match in pattern.finditer(block):
        found[match.group("key")] = {
            "label": match.group("label"),
            "definition": _join_strings(match.group("definition")),
        }
    return found


def _keyed_entries(block: str) -> dict[str, dict]:
    """``{value: {label, definition}}`` from an object of ``value: {label, …}``."""
    found = {}
    pattern = re.compile(
        r'^\s{2}(?P<value>[A-Za-z_][A-Za-z0-9_]*):\s*\{\s*'
        r'label:\s*"(?P<label>[^"]+)",\s*'
        r'(?:tag:\s*"[^"]*",\s*)?'
        r'definition:\s*(?P<definition>"(?:[^"\\]|\\.)*"(?:\s*\+\s*\n?\s*"(?:[^"\\]|\\.)*")*)',
        re.MULTILINE,
    )
    for match in pattern.finditer(block):
        found[match.group("value")] = {
            "label": match.group("label"),
            "definition": _join_strings(match.group("definition")),
        }
    return found


def _join_strings(literal: str) -> str:
    """Turn ``"a " + "b"`` into ``a b`` and unescape what JSON escapes."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', literal)
    return "".join(part.replace('\\"', '"').replace("\\\\", "\\") for part in parts)


def _provenance_map(block: str) -> dict[str, dict[str, dict]]:
    """``{field: {value: {key, detail}}}`` from ``PROVENANCE_MAP``."""
    fields: dict[str, dict[str, dict]] = {}
    field_pattern = re.compile(
        r'^\s{2}(?P<field>[A-Za-z_][A-Za-z0-9_]*):\s*\{(?P<body>.*?)^\s{2}\},',
        re.DOTALL | re.MULTILINE,
    )
    inline_pattern = re.compile(
        r'^\s{2}(?P<field>[A-Za-z_][A-Za-z0-9_]*):\s*\{(?P<body>[^\n]*?)\},\s*$',
        re.MULTILINE,
    )
    entry_pattern = re.compile(
        r'(?P<value>[A-Za-z_][A-Za-z0-9_]*|"\*"):\s*\{\s*key:\s*"(?P<key>[^"]+)"'
        r'(?:\s*,\s*detail:\s*"(?P<detail>[^"]*)")?\s*\}'
    )
    for pattern in (field_pattern, inline_pattern):
        for match in pattern.finditer(block):
            body = match.group("body")
            entries = {}
            for entry in entry_pattern.finditer(body):
                value = entry.group("value").strip('"')
                entries[value] = {
                    "key": entry.group("key"),
                    "detail": entry.group("detail"),
                }
            if entries:
                fields.setdefault(match.group("field"), {}).update(entries)
    return fields


def _quoted_entries(block: str) -> dict[str, dict]:
    """The same as ``_keyed_entries`` for a key the JS has to quote.

    ``re-bucketed`` and ``model applied`` are not valid bare property names, so
    Prettier writes them as strings and the other reader skips them.
    """
    found = {}
    pattern = re.compile(
        r'^\s{2}"(?P<value>[^"]+)":\s*\{\s*'
        r'label:\s*"(?P<label>[^"]+)",\s*'
        r'(?:tag:\s*"[^"]*",\s*)?'
        r'definition:\s*(?P<definition>"(?:[^"\\]|\\.)*"(?:\s*\+\s*\n?\s*"(?:[^"\\]|\\.)*")*)',
        re.MULTILINE,
    )
    for match in pattern.finditer(block):
        found[match.group("value")] = {
            "label": match.group("label"),
            "definition": _join_strings(match.group("definition")),
        }
    return found


def _term(source: str, key: str) -> dict:
    """One entry of the ``TERMS`` list, by its key."""
    pattern = re.compile(
        rf'key:\s*"{re.escape(key)}",\s*'
        r'term:\s*"(?P<term>[^"]+)",\s*'
        r'plural:\s*"(?P<plural>[^"]+)",\s*'
        r'definition:\s*(?P<definition>"(?:[^"\\]|\\.)*"(?:\s*\+\s*\n?\s*"(?:[^"\\]|\\.)*")*)',
        re.DOTALL,
    )
    match = pattern.search(source)
    assert match, f"no term {key!r} in glossary.js"
    return {
        "term": match.group("term"),
        "plural": match.group("plural"),
        "definition": _join_strings(match.group("definition")),
    }


@pytest.fixture(scope="module")
def js() -> str:
    if not GLOSSARY_JS.is_file():
        pytest.skip(f"no frontend glossary at {GLOSSARY_JS}")
    return GLOSSARY_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The reader works
# ---------------------------------------------------------------------------


def test_the_reader_finds_something_in_every_block_it_needs(js):
    """A silent regex failure would turn every comparison below into a pass."""
    assert len(_ordered_entries(_block(js, "PROVENANCE"))) == 6
    assert len(_keyed_entries(_block(js, "VALUE_BASIS"))) >= 5
    assert len(_keyed_entries(_block(js, "ID_ORIGIN"))) >= 4
    assert len(_keyed_entries(_block(js, "AGREEMENT"))) >= 4
    assert len(_provenance_map(_block(js, "PROVENANCE_MAP"))) >= 4


# ---------------------------------------------------------------------------
# The two copies agree
# ---------------------------------------------------------------------------


def test_the_ordered_provenance_list_matches_the_frontend(js):
    theirs = _ordered_entries(_block(js, "PROVENANCE"))
    ours = {entry["key"]: entry for entry in vocabulary.PROVENANCE}

    assert list(theirs) == list(ours), "the six are in a different order"
    for key, entry in theirs.items():
        assert entry["label"] == ours[key]["label"], f"label for {key}"
        assert entry["definition"] == ours[key]["definition"], f"definition for {key}"


def test_suggested_matches_the_frontend(js):
    theirs = _ordered_entries(_block(js, "SUGGESTED"))
    assert theirs["suggested"]["label"] == vocabulary.SUGGESTED["label"]
    assert theirs["suggested"]["definition"] == vocabulary.SUGGESTED["definition"]


@pytest.mark.parametrize(
    "export_name,ours",
    [
        ("VALUE_BASIS", vocabulary.VALUE_BASIS),
        ("ID_ORIGIN", vocabulary.ID_ORIGIN),
        ("AGREEMENT", vocabulary.AGREEMENT),
    ],
)
def test_the_other_three_questions_match_the_frontend(js, export_name, ours):
    theirs = _keyed_entries(_block(js, export_name))
    missing = set(theirs) - set(ours)
    assert not missing, f"{export_name}: the backend has no entry for {sorted(missing)}"
    for value, entry in theirs.items():
        assert entry["label"] == ours[value]["label"], f"{export_name}.{value} label"
        assert entry["definition"] == ours[value]["definition"], (
            f"{export_name}.{value} definition"
        )


def test_the_questions_themselves_match_the_frontend(js):
    for export_name, ours in [
        ("PROVENANCE_QUESTION", vocabulary.PROVENANCE_QUESTION),
        ("VALUE_BASIS_QUESTION", vocabulary.VALUE_BASIS_QUESTION),
        ("ID_ORIGIN_QUESTION", vocabulary.ID_ORIGIN_QUESTION),
        ("AGREEMENT_QUESTION", vocabulary.AGREEMENT_QUESTION),
    ]:
        match = re.search(rf'export const {export_name} = "([^"]+)"', js)
        assert match, f"{export_name} is not a plain string in glossary.js"
        assert match.group(1) == ours, export_name


def test_every_stored_value_maps_to_the_same_label_in_both(js):
    theirs = _provenance_map(_block(js, "PROVENANCE_MAP"))
    for field, entries in theirs.items():
        assert field in vocabulary.PROVENANCE_MAP, f"the backend maps no field {field}"
        ours = vocabulary.PROVENANCE_MAP[field]
        for value, hit in entries.items():
            assert value in ours, f"{field}.{value} is mapped on screen and not here"
            assert hit["key"] == ours[value]["key"], f"{field}.{value} maps elsewhere"
            assert hit["detail"] == ours[value].get("detail"), f"{field}.{value} detail"


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------


def test_earlier_grouping_beats_score(js=None):
    """`docs/DESIGN.md` D22. The clash B2 settles, in one assertion."""
    assert vocabulary.provenance_rank("earlier") > vocabulary.provenance_rank("score")
    assert vocabulary.BASIS_ORDER["import"] > vocabulary.BASIS_ORDER["score"]
    assert vocabulary.BASIS_ORDER["human"] == max(vocabulary.BASIS_ORDER.values())
    assert vocabulary.BASIS_ORDER["single"] == min(vocabulary.BASIS_ORDER.values())


def test_a_suggestion_never_ranks():
    assert vocabulary.provenance_rank("suggested") == 0
    assert vocabulary.SUGGESTED["order"] is None


def test_every_field_value_has_a_label_and_a_definition():
    served = vocabulary.as_dict()
    for field, block in served["fields"].items():
        assert block["values"], f"{field} serves no values"
        for entry in block["values"]:
            assert entry["label"], f"{field}.{entry['value']} has no label"
            assert entry["definition"].endswith("."), (
                f"{field}.{entry['value']} definition is not a sentence"
            )


def test_the_tuples_the_backend_validates_against_come_from_here():
    from app.services import clusters_reader, entities_reader, exact_groups_reader
    from app.services import pair_labels, pairs_reader

    assert entities_reader.BASES is vocabulary.ENTITY_BASES
    assert entities_reader.ID_STATUSES is vocabulary.ID_STATUSES
    assert pairs_reader.BUCKETS is vocabulary.BUCKETS
    assert pairs_reader.DECIDED_BY is vocabulary.DECIDED_BY

    from app.rules import linkage, vetoes

    assert linkage.BUCKETS is vocabulary.BUCKETS
    assert linkage.DECIDED_BY is vocabulary.DECIDED_BY
    assert set(vetoes.OPERATORS) == set(vocabulary.VETO_OPS)
    assert clusters_reader.STATUSES is vocabulary.CLUSTER_STATUSES
    assert exact_groups_reader.AGREEMENTS is vocabulary.AGREEMENTS
    assert pair_labels.PROVENANCES is vocabulary.LABEL_PROVENANCES


def test_the_second_agreements_tuple_has_its_own_name():
    """B19: two lists shared one name and answered different questions."""
    from app.pipeline.dedupe import score_eval
    from app.rules import keys_eval

    assert not hasattr(score_eval, "AGREEMENTS")
    assert score_eval.IMPORT_AGREEMENTS == ("agrees", "disagrees", "unknown")
    assert keys_eval.AGREEMENTS == ("consistent", "conflict", "extends", "new")


def test_the_stage_list_names_every_stage_a_reader_is_shown():
    keys = [stage["key"] for stage in vocabulary.STAGES]
    assert "model" in keys, "How it works shows a model stage; the list must too"
    assert "derive" in keys, "How it works shows derived columns; the list must too"
    for stage in vocabulary.STAGES:
        assert not any(char.isdigit() for char in stage["label"]), (
            f"stages are named, never numbered: {stage['label']!r}"
        )


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_the_endpoint_needs_no_login(monkeypatch, tmp_path):
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/vocabulary")
    assert response.status_code == 200
    body = response.json()
    assert body["provenance"]["question"] == "How it was decided"
    assert [entry["label"] for entry in body["provenance"]["ordered"]] == [
        "On its own", "Match key", "Score", "Veto rule", "Earlier grouping", "Reviewer",
    ]
    assert body["fields"]["entity_basis"]["values"]
    assert body["stages"]


# ---------------------------------------------------------------------------
# The four value tables the frontend added, and the three terms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "export_name,ours",
    [
        ("ANSWER", vocabulary.ANSWER),
        ("ANSWER_SOURCE", vocabulary.ANSWER_SOURCE),
        ("RULES_REPLAYED", vocabulary.RULES_REPLAYED),
        ("SCORER", vocabulary.SCORER),
        ("LINE_CHANGE", vocabulary.LINE_CHANGE),
    ],
)
def test_the_four_newer_tables_match_the_frontend(js, export_name, ours):
    theirs = _keyed_entries(_block(js, export_name))
    missing = set(theirs) - set(ours)
    assert not missing, f"{export_name}: the backend has no entry for {sorted(missing)}"
    for value, entry in theirs.items():
        assert entry["label"] == ours[value]["label"], f"{export_name}.{value} label"
        assert entry["definition"] == ours[value]["definition"], (
            f"{export_name}.{value} definition"
        )


def test_a_quoted_key_matches_too(js):
    """`re-bucketed` and `model applied` are quoted in the JS, not bare names."""
    theirs = _quoted_entries(_block(js, "LINE_CHANGE"))
    for value in ("re-bucketed", "model applied", "model reverted"):
        assert value in theirs, f"the reader missed {value!r}"
        assert theirs[value]["label"] == vocabulary.LINE_CHANGE[value]["label"]
        assert theirs[value]["definition"] == vocabulary.LINE_CHANGE[value]["definition"]


def test_the_newer_questions_match_the_frontend(js):
    for export_name, ours in [
        ("ANSWER_QUESTION", vocabulary.ANSWER_QUESTION),
        ("ANSWER_SOURCE_QUESTION", vocabulary.ANSWER_SOURCE_QUESTION),
        ("RULES_REPLAYED_QUESTION", vocabulary.RULES_REPLAYED_QUESTION),
        ("SCORER_QUESTION", vocabulary.SCORER_QUESTION),
        ("LINE_CHANGE_QUESTION", vocabulary.LINE_CHANGE_QUESTION),
    ]:
        match = re.search(rf'export const {export_name} = "([^"]+)"', js)
        assert match, f"{export_name} is not a plain string in glossary.js"
        assert match.group(1) == ours, export_name


@pytest.mark.parametrize("key", ["link", "codeVersion", "fileFingerprint"])
def test_the_three_shared_terms_match_the_frontend(js, key):
    theirs = _term(js, key)
    ours = vocabulary.TERMS[key]
    assert theirs["term"] == ours["term"]
    assert theirs["plural"] == ours["plural"]
    assert theirs["definition"] == ours["definition"]


def test_the_line_change_keys_are_the_words_the_backend_writes():
    """The stored `action` strings and the labels must be one list, or the
    screen would meet a value it has no word for."""
    from app.services import bucketing_history

    assert set(bucketing_history.ACTIONS) == set(vocabulary.LINE_CHANGE)


def test_the_endpoint_serves_the_newer_tables_and_the_terms():
    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/vocabulary").json()
    for field in ("answer_source", "rules_replayed", "scorer", "line_change",
                  "is_match"):
        assert body["fields"][field]["values"], field
    assert {entry["value"] for entry in body["fields"]["line_change"]["values"]} == {
        "scored", "re-bucketed", "model applied", "model reverted"}
    assert sorted(body["terms"]) == ["codeVersion", "fileFingerprint", "link"]
    assert body["terms"]["link"]["term"] == "link"

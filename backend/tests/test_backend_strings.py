"""No retired term reaches the screen from the backend — items B15 to B17.

`frontend/scripts/check-terms.mjs` does this for the frontend. The backend
sends words to the screen too: the stage list, the default rule descriptions,
the veto reasons, the guard reasons, the pair explanation and every error
message. This file is that check.

The list of retired words is `docs/GLOSSARY.md`'s. Only *rendered* text is
checked: a field name, a database value and a code comment may all say
``exact_key``, and none of them reaches a reader.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app import vocabulary
from app.services.exact_groups_reader import guard_text
from app.services.pairs_reader import plain_column, plain_level_label

DEFAULTS = Path(__file__).resolve().parents[1] / "app" / "profiles" / "defaults"

#: Words `GLOSSARY.md` retires, as they would appear in prose. Each is matched
#: case-insensitively as a whole phrase.
RETIRED = [
    "exact key", "exact keys",
    "existing labels", "imported labels",
    "merged group", "merged groups",
    "earlier manual work",
    "cold start",
    "auto-accept", "auto accept",
    "candidate floor",
    "withheld cluster",
    "match probability",
    "GBT score",
    "term frequency",
    "training block",
    "bits",
    "held out",
    "verdict",
]

#: The fields of a ruleset or a linkage settings file that render verbatim on
#: the Config tabs. A key starting with ``_`` is a note the UI does not show.
RENDERED_FIELDS = ("description", "reason", "name")


def _rendered_strings(node, trail="", out=None):
    """Every string the Config tabs print, with the path it came from."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).startswith("_"):
                continue  # a note, not shown
            path = f"{trail}.{key}"
            if key in RENDERED_FIELDS and isinstance(value, str):
                out.append((path, value))
            else:
                _rendered_strings(value, path, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _rendered_strings(value, f"{trail}[{index}]", out)
    return out


def _offenders(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in RETIRED if word in lowered]


# ---------------------------------------------------------------------------
# B15 — the stage list
# ---------------------------------------------------------------------------


def test_the_stage_list_carries_no_retired_term():
    from app.pipeline.dedupe import STAGES

    for stage in STAGES:
        for field in ("label", "description"):
            found = _offenders(stage[field])
            assert not found, f"{stage['key']}.{field} says {found}"


def test_the_stage_list_is_the_vocabularys_list():
    from app.pipeline.dedupe import STAGES

    assert [s["key"] for s in STAGES] == [s["key"] for s in vocabulary.STAGES]


# ---------------------------------------------------------------------------
# B16 — the default rule text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(DEFAULTS.glob("*/*.json")), ids=str)
def test_no_default_config_text_carries_a_retired_term(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    for where, text in _rendered_strings(document):
        found = _offenders(text)
        if found:
            problems.append(f"{where} says {found}: {text[:90]}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("path", sorted(DEFAULTS.glob("*/*.json")), ids=str)
def test_a_dense_measurement_lives_in_a_note_the_ui_does_not_show(path):
    """A description says what a rule does. The figures behind it go in
    ``_measured``, which the Config tabs do not print."""
    document = json.loads(path.read_text(encoding="utf-8"))
    long_ones = [
        (where, text) for where, text in _rendered_strings(document)
        if len(text) > 400
    ]
    assert not long_ones, f"too long to read on a tab: {[w for w, _ in long_ones]}"


def test_the_measurements_were_kept_and_not_deleted():
    """Moving a paragraph out of sight must not lose it."""
    psc = json.loads((DEFAULTS / "psc" / "linkage_settings.json").read_text("utf-8"))
    rules = psc["tracks"]["person"]["blocking_rules"]
    measured = [rule.get("_measured") for rule in rules if rule.get("_measured")]
    assert any("37,467" in text for text in measured), (
        "the surname-change measurement must still be on the record"
    )


# ---------------------------------------------------------------------------
# B17 — errors and guards
# ---------------------------------------------------------------------------


def test_a_bad_choice_is_a_sentence_that_names_the_setting():
    message = vocabulary.choice_error("fallback", "wrong", ("keep", "blank", "error"))
    assert message.startswith("'wrong' does not say")
    assert "what happens to a value the lookup table does not list" in message
    # The allowed values stay, because the reader has to type one of them back.
    assert "keep, blank, error" in message


def test_a_missing_choice_says_nothing_rather_than_none():
    assert vocabulary.choice_error("track", None, ("person",)).startswith("nothing")


def test_the_answer_is_named_in_the_reviewers_words():
    from app.services import pair_labels

    with pytest.raises(pair_labels.LabelError) as caught:
        pair_labels.normalise_verdict("MAYBE")
    assert "Match or Not a match" in str(caught.value)


@pytest.mark.parametrize("guard,expected", [
    ("max_group_size:12>10", "12 records together"),
    ("max_distinct:name_core=7>3", "7 different values of core name"),
    ("require_any_equal:postcode_clean", "do not all agree on postcode"),
])
def test_a_guard_reason_gains_a_sentence_beside_it(guard, expected):
    text = guard_text(guard)
    assert expected in text
    assert "_" not in text, f"a cleaned column name reached the screen: {text}"


def test_a_group_that_passed_has_no_guard_sentence():
    assert guard_text(None) is None
    assert guard_text("") is None


@pytest.mark.parametrize("column,engine,expected", [
    ("forename_canon", "Exact match on forename_canon", "Same standard forename"),
    ("dob_year_clean", "Equal dob_year_clean", "Same birth year"),
    ("dob_year_clean", "dob_year_clean within 1", "Birth year within 1 year"),
    ("title", "title is NULL", "Title is missing on at least one side"),
    ("name_core", "Jaro-Winkler distance of name_core >= 0.92",
     "Core name is a close spelling"),
])
def test_a_pair_explanation_never_prints_a_cleaned_column_name(column, engine,
                                                               expected):
    label = plain_level_label(column, engine)
    assert label == expected
    assert column not in label


def test_all_other_comparisons_says_what_it_means():
    assert plain_level_label("surname", "All other comparisons") == (
        "Surname: neither side is close enough to count"
    )


@pytest.mark.parametrize("column,expected", [
    ("dob_year_clean", "birth year"),
    ("forename_canon", "standard forename"),
    ("name_tokens_sorted", "name words"),
    ("postcode_district", "postcode district"),
    # Two columns must never read the same, or the screen would say one thing
    # about two pieces of evidence.
    ("surname", "surname"),
    ("surname_metaphone", "surname sound"),
])
def test_a_cleaned_column_reads_as_a_phrase(column, expected):
    assert plain_column(column) == expected


# ---------------------------------------------------------------------------
# The reviewer is never the literal word "user"
# ---------------------------------------------------------------------------


def test_nobody_is_ever_called_user():
    from app import auth

    assert auth.current_user(None) == auth.UNKNOWN_USER
    assert auth.current_user("not-a-signed-cookie") == auth.UNKNOWN_USER
    assert auth.UNKNOWN_USER != "user"


def test_a_cookie_that_literally_says_user_is_not_believed(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "_unsign", lambda token, max_age=None: {"name": "user"})
    assert auth.current_user("anything") == auth.UNKNOWN_USER
    monkeypatch.setattr(auth, "_unsign", lambda token, max_age=None: {"name": "Tom"})
    assert auth.current_user("anything") == "Tom"


# ---------------------------------------------------------------------------
# The training report
# ---------------------------------------------------------------------------


def test_the_training_report_says_new_model_and_not_cold_start():
    from app.model import train

    assert "cold start" not in train.KNOWN_LIMIT.lower()
    assert "imported labels" not in train.KNOWN_LIMIT.lower()
    assert "earlier grouping" in train.KNOWN_LIMIT.lower()

    source = __import__("inspect").getsource(train._warnings)
    assert "This is a new model" in source
    # ModelPanel reads the number out of that sentence with /(\d+) are needed/.
    assert "are needed" in source

# backend/tests/test_rules_conditions.py
"""The track-rule condition operators."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.rules import conditions

TOKEN_LISTS = {
    "titles": {"tokens": ["MR", "MRS", "DR", "REV", "REVD", "HON", "RT HON", "THE RT HON"]},
    "legal_forms": {"tokens": ["LTD", "AND CO", "CO"]},
    "org_words": {"tokens": ["CLUB", "ASSOCIATION"]},
    "empty": {"tokens": []},
}


def _mask(column, **condition):
    return list(conditions.evaluate(condition, pd.Series(column, dtype="object"), TOKEN_LISTS))


# ---------------------------------------------------------------------------
# Whole-value operators
# ---------------------------------------------------------------------------


def test_equals_is_case_insensitive_and_trims():
    values = ["Individual", "individual", "  INDIVIDUAL  ", "Company", None]
    assert _mask(values, op="equals", value="individual") == [True, True, True, False, False]
    assert _mask(values, op="not_equals", value="individual") == [False, False, False, True, True]


def test_in_and_not_in():
    values = ["Other", "Impermissible Donor", "Company", None]
    wanted = ["Impermissible Donor", "Other"]
    assert _mask(values, op="in", values=wanted) == [True, True, False, False]
    assert _mask(values, op="not_in", values=wanted) == [False, False, True, True]


def test_null_operators_treat_blank_as_missing():
    values = ["Smith", "", "   ", None]
    assert _mask(values, op="is_null") == [False, True, True, True]
    assert _mask(values, op="not_null") == [True, False, False, False]


def test_matches_is_a_case_insensitive_search():
    values = ["Acme Ltd", "acme limited", "Smith", None]
    assert _mask(values, op="matches", pattern=r"^acme") == [True, True, False, False]


def test_matches_rejects_a_broken_pattern():
    with pytest.raises(conditions.ConditionError, match="Invalid regular expression"):
        _mask(["x"], op="matches", pattern="(unclosed")


def test_an_unknown_operator_is_refused():
    with pytest.raises(conditions.ConditionError, match="Unknown condition operator"):
        _mask(["x"], op="sounds_like", value="y")


def test_an_unknown_token_list_is_refused():
    with pytest.raises(conditions.ConditionError, match="Unknown token list 'nope'"):
        _mask(["x"], op="contains_token", lists=["nope"])


# ---------------------------------------------------------------------------
# Token operators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("Mr A Smith", True),
    ("MR A SMITH", True),
    ("Dr. J Patel", True),           # a full stop does not hide the token
    ("  Mrs B Brown", True),
    ("Drummond", False),             # starts with 'Dr' but is not the title
    ("Mrs", True),                   # the whole value is the token
    ("A Smith Dr", False),           # not at the start
    ("Mister Smith", False),
    (None, False),
])
def test_starts_with_token(name, expected):
    assert _mask([name], op="starts_with_token", lists=["titles"]) == [expected]


@pytest.mark.parametrize("name,expected", [
    ("Acme Ltd", True),
    ("Acme ltd.", True),
    ("Smith and Co", True),          # multi-word token
    ("Ltd Acme", False),
    ("Acme Limited", False),         # LIMITED is not in this list
    ("Coconut", False),              # 'CO' is not a whole word here
    (None, False),
])
def test_ends_with_token(name, expected):
    assert _mask([name], op="ends_with_token", lists=["legal_forms"]) == [expected]


@pytest.mark.parametrize("name,expected", [
    ("Barnet Conservative Association", True),
    ("West End Club, Soho", True),
    ("Acme Ltd Trading", True),
    ("Associations of things", False),   # a longer word is not the token
    ("Clubhouse", False),
    (None, False),
])
def test_contains_token(name, expected):
    assert _mask([name], op="contains_token",
                 lists=["legal_forms", "org_words"]) == [expected]


def test_a_multi_word_token_matches_across_whitespace():
    values = ["Rt Hon Jane Doe", "The Rt Hon Jane Doe", "Rt   Hon Jane Doe", "Hon Jane Doe"]
    assert _mask(values, op="starts_with_token", lists=["titles"]) == [True, True, True, True]


def test_a_multi_word_token_beats_its_shorter_cousin():
    """'RT HON' must win over 'HON', and 'REVD' over 'REV' — longest first."""
    pattern = conditions.token_regex(["HON", "RT HON", "REV", "REVD"], "leading")
    assert pattern.match("RT HON JANE").group(0) == "RT HON"
    assert pattern.match("REVD SMITH").group(0) == "REVD"


def test_several_lists_are_one_alternation():
    values = ["Acme Ltd", "Barnet Association", "Jane Doe"]
    assert _mask(values, op="contains_token",
                 lists=["legal_forms", "org_words"]) == [True, True, False]


def test_an_empty_token_list_matches_nothing():
    assert _mask(["Anything"], op="contains_token", lists=["empty"]) == [False]


def test_token_operators_run_once_per_distinct_value(monkeypatch):
    """The regex is the expensive part, so it must not run per row."""
    pattern = conditions.token_regex(["LTD"], "anywhere")
    calls = []
    real = pattern.search

    class Counting:
        def search(self, value):
            calls.append(value)
            return real(value)

    text = pd.Series(["Acme Ltd", "Acme Ltd", "Smith", "Acme Ltd", None], dtype="object")
    conditions._regex_mask(conditions._as_text(text), Counting())
    assert sorted(calls) == ["Acme Ltd", "Smith"]


def test_columns_read_lists_each_column_once_in_order():
    rule = {"when": [
        {"column": "donor_status", "op": "equals", "value": "Other"},
        {"column": "name", "op": "contains_token", "lists": ["legal_forms"]},
        {"column": "donor_status", "op": "not_equals", "value": "Company"},
    ]}
    assert conditions.columns_read(rule) == ["donor_status", "name"]

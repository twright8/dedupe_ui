# backend/tests/test_rules_functions.py
"""The fixed function library, value by value."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.rules import functions


def _run(name: str, values: list, **args):
    """Run one library function over *values* the way the engine does."""
    spec = functions.REGISTRY[name]
    series = pd.Series(values, dtype="object")
    return functions.map_distinct(series, lambda distinct: spec.run(distinct, **args))


# ---------------------------------------------------------------------------
# parse_person_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("JOHN SMITH", ("JOHN", None, "SMITH", "J")),
    ("JOHN ALAN SMITH", ("JOHN", "ALAN", "SMITH", "J")),
    ("JOHN A B SMITH", ("JOHN", "A B", "SMITH", "J")),
    # One token is a surname, not a forename: bare surnames are common here.
    ("SMITH", (None, None, "SMITH", None)),
    ("SMITH, JOHN", ("JOHN", None, "SMITH", "J")),
    ("SMITH, JOHN ALAN", ("JOHN", "ALAN", "SMITH", "J")),
    ("VAN DER BERG, JOHN", ("JOHN", None, "VAN DER BERG", "J")),
    ("SMITH,", (None, None, "SMITH", None)),
    ("  SPACED   OUT  ", ("SPACED", None, "OUT", "S")),
    ("", (None, None, None, None)),
])
def test_parse_person_name(name, expected):
    parsed = functions.parse_person_name_value(name)
    assert (parsed["forename"], parsed["middle_names"],
            parsed["surname"], parsed["forename_initial"]) == expected


def test_parse_person_name_writes_four_columns_and_keeps_nulls_null():
    out = _run("parse_person_name", ["JOHN SMITH", None, "SMITH"])
    assert list(out.columns) == ["forename", "middle_names", "surname", "forename_initial"]
    assert list(out["surname"]) == ["SMITH", None, "SMITH"]
    assert list(out["forename"]) == ["JOHN", None, None]


# ---------------------------------------------------------------------------
# Postcodes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("le11fb", "LE1 1FB"),
    ("LE1 1FB", "LE1 1FB"),
    ("  sw1a  1aa ", "SW1A 1AA"),      # no space, extra space, either way
    ("EC1A1BB", "EC1A 1BB"),
    ("e14 5ab", "E14 5AB"),
    ("SW1A-1AA", "SW1A 1AA"),          # punctuation is not part of a postcode
    ("75008", None),                   # a French postcode is not a UK shape
    ("NOT A POSTCODE", None),
    ("LE1", None),
    ("", None),
])
def test_normalise_postcode(raw, expected):
    assert functions.normalise_postcode_value(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("LE1 1FB", "LE1"),
    ("SW1A 1AA", "SW1A"),
    ("E14 5AB", "E14"),
    ("LE11FB", None),                  # needs the space, so run it after cleaning
    ("", None),
])
def test_postcode_district(raw, expected):
    assert functions.postcode_district_value(raw) == expected


def test_postcode_chain_keeps_nulls_null():
    clean = _run("normalise_postcode", ["le11fb", None, "nonsense"])
    assert list(clean) == ["LE1 1FB", None, None]
    district = _run("postcode_district", list(clean))
    assert list(district) == ["LE1", None, None]


# ---------------------------------------------------------------------------
# Company numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("04250076", "04250076"),
    ("4250076", "04250076"),           # the pair that must become one value
    ("04250076 ?", "04250076"),        # the clerk's "unsure" flag
    (" 123 456 ", "00123456"),
    ("sc123456", "SC123456"),
    ("OC-314414", "OC314414"),
    ("123456789", "123456789"),        # already longer than 8: left alone
    ("AB", None),                      # no digit
    ("LIMITED", None),
    ("7", None),                       # under two characters
    ("-", None),
    ("", None),
])
def test_normalise_company_number(raw, expected):
    assert functions.normalise_company_number_value(raw) == expected


def test_the_two_aq_networks_numbers_become_one():
    out = _run("normalise_company_number", ["04250076", "4250076"])
    assert out.nunique() == 1


# ---------------------------------------------------------------------------
# Phonetic keys and token helpers
# ---------------------------------------------------------------------------


def test_metaphone_and_soundex_agree_on_names_that_sound_alike():
    assert (functions.metaphone_value("SMITH") == functions.metaphone_value("SMYTH"))
    assert (functions.soundex_value("SMITH") == functions.soundex_value("SMYTHE"))


def test_phonetic_keys_of_nothing_are_null():
    assert functions.metaphone_value("") is None
    assert functions.soundex_value("") is None
    assert list(_run("metaphone", [None, "SMITH"]))[0] is None


@pytest.mark.parametrize("raw,expected", [
    ("SMITH AND SON", "AND SMITH SON"),
    ("SON AND SMITH", "AND SMITH SON"),
    ("SMITH AND SMITH", "AND SMITH"),   # distinct tokens only
    ("SMITH", "SMITH"),
    ("", None),
])
def test_sorted_tokens(raw, expected):
    assert functions.sorted_tokens_value(raw) == expected


@pytest.mark.parametrize("raw,first,last,initials", [
    ("JOHN A SMITH", "JOHN", "SMITH", "JAS"),
    ("SMITH", "SMITH", "SMITH", "S"),
    ("  padded  name ", "padded", "name", "pn"),
    ("", None, None, None),
])
def test_token_helpers(raw, first, last, initials):
    assert functions.first_token_value(raw) == first
    assert functions.last_token_value(raw) == last
    assert functions.initials_value(raw) == initials


# ---------------------------------------------------------------------------
# The performance rule
# ---------------------------------------------------------------------------


def test_a_function_runs_once_per_distinct_value(monkeypatch):
    """RULESET.md: every function runs on the DISTINCT values of its source.
    Without this a 16-million-row PSC run is not practical."""
    calls = []
    real = functions.normalise_company_number_value
    monkeypatch.setattr(
        functions, "normalise_company_number_value",
        lambda value: (calls.append(value), real(value))[1],
    )

    values = ["04250076", "04250076", "SC123456", "04250076", None, None, "SC123456"]
    out = _run("normalise_company_number", values)

    assert sorted(calls) == ["04250076", "SC123456"]
    assert list(out) == ["04250076", "04250076", "SC123456", "04250076",
                         None, None, "SC123456"]


def test_map_distinct_survives_an_all_null_column():
    out = _run("metaphone", [None, None])
    assert list(out) == [None, None]
    parsed = _run("parse_person_name", [None, None])
    assert list(parsed.columns) == ["forename", "middle_names", "surname", "forename_initial"]
    assert parsed["surname"].isna().all()


def test_map_distinct_handles_a_numeric_source_column():
    out = _run("first_token", [2015, 2016, 2015])
    assert list(out) == ["2015", "2016", "2015"]


# ---------------------------------------------------------------------------
# The library as the UI sees it
# ---------------------------------------------------------------------------


def test_every_function_declares_what_it_writes():
    for spec in functions.library():
        assert spec["outputs"], f"{spec['name']} declares no outputs"
        assert spec["name"] in functions.REGISTRY


def test_every_example_is_true():
    """A worked example the UI shows must be what the function really does."""
    for spec in functions._SPECS:
        out = functions.map_distinct(
            pd.Series([spec.example["input"]], dtype="object"), spec.run
        )
        if spec.multi_output:
            produced = {c: out[c].iloc[0] for c in out.columns}
        else:
            produced = out.iloc[0]
        assert produced == spec.example["output"], spec.name

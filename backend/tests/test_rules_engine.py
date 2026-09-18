# backend/tests/test_rules_engine.py
"""The ruleset engine: every op, validation, track assignment, and the trace."""

import copy
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset, small_ruleset

from app.profiles.donations import RAW_COLUMNS
from app.rules import engine, functions

TOKEN_LISTS = {
    "titles": {"tokens": ["MR", "MRS", "DR", "RT HON"]},
    "legal_forms": {"tokens": ["LTD", "LIMITED", "CO", "AND CO"]},
    "null_values": {"tokens": ["N/A", "NONE", "-"]},
}

LOOKUPS = {
    "nicknames": {"fallback": "passthrough",
                  "rows": [{"raw": "BILL", "canonical": "WILLIAM"},
                           {"raw": "TOM", "canonical": "THOMAS"}]},
    "strict": {"fallback": "error", "rows": [{"raw": "A", "canonical": "ALPHA"}]},
    "drop": {"fallback": "null", "rows": [{"raw": "A", "canonical": "ALPHA"}]},
}


def _apply(values, step, **overrides):
    """Run one step over a source column called 'src'."""
    ruleset = {"token_lists": TOKEN_LISTS, "lookups": LOOKUPS,
               "cleaning": {"person": [{**step, "source": "src"}]}}
    ruleset.update(overrides)
    frame = pd.DataFrame({"src": pd.Series(values, dtype="object")})
    return engine.apply_cleaning(frame, ruleset, "person")


def _out(values, step, column="out"):
    return list(_apply(values, step)[column])


# ---------------------------------------------------------------------------
# Text ops
# ---------------------------------------------------------------------------


def test_copy_and_the_text_ops():
    assert _out(["Acme"], {"id": "s", "op": "copy", "target": "out"}) == ["Acme"]
    assert _out(["Acme"], {"id": "s", "op": "upper", "target": "out"}) == ["ACME"]
    assert _out(["Acme"], {"id": "s", "op": "lower", "target": "out"}) == ["acme"]
    assert _out(["  Acme  "], {"id": "s", "op": "trim", "target": "out"}) == ["Acme"]
    assert _out(["A   B  C"], {"id": "s", "op": "collapse_spaces", "target": "out"}) == ["A B C"]
    assert _out(["José Ávila"], {"id": "s", "op": "accent_fold", "target": "out"}) == ["Jose Avila"]


def test_text_ops_leave_nulls_and_blanks_alone():
    assert _out([None, "", "   "], {"id": "s", "op": "upper", "target": "out"}) == [None, None, None]


def test_strip_punctuation_keeps_what_it_is_told_to():
    step = {"id": "s", "op": "strip_punctuation", "target": "out", "keep": " -'"}
    assert _out(["O'BRIEN-SMITH, JR."], step) == ["O'BRIEN-SMITH JR"]
    plain = {"id": "s", "op": "strip_punctuation", "target": "out"}
    assert _out(["A.C.M.E. (UK) LTD"], plain) == ["ACME UK LTD"]
    assert _out(["A_B"], plain) == ["AB"]          # the underscore is punctuation too
    assert _out(["£$%"], plain) == [None]          # nothing left is null


def test_regex_replace_is_re_sub():
    step = {"id": "s", "op": "regex_replace", "target": "out",
            "pattern": "&", "replacement": " AND "}
    assert _out(["SMITH&SON & CO"], step) == ["SMITH AND SON  AND  CO"]


def test_regex_replace_reports_a_broken_pattern():
    step = {"id": "s", "op": "regex_replace", "target": "out",
            "pattern": "(unclosed", "replacement": ""}
    with pytest.raises(engine.RulesetError, match="Invalid regular expression"):
        _apply(["X"], step)


# ---------------------------------------------------------------------------
# strip_tokens
# ---------------------------------------------------------------------------


def _strip(values, **step):
    base = {"id": "s", "op": "strip_tokens", "target": "out", "lists": ["titles"],
            "position": "leading"}
    return _apply(values, {**base, **step})


def test_strip_tokens_leading_repeats_and_keeps_what_it_removed():
    out = _strip(["MR DR JOHN SMITH"], keep_as="kept")
    assert list(out["out"]) == ["JOHN SMITH"]
    assert list(out["kept"]) == ["MR DR"]


def test_strip_tokens_can_be_told_not_to_repeat():
    out = _strip(["MR DR JOHN SMITH"], repeat=False, keep_as="kept")
    assert list(out["out"]) == ["DR JOHN SMITH"]
    assert list(out["kept"]) == ["MR"]


def test_strip_tokens_trailing_keeps_the_removed_words_in_order():
    out = _strip(["SMITH AND SON AND CO LTD"], lists=["legal_forms"],
                 position="trailing", keep_as="kept")
    assert list(out["out"]) == ["SMITH AND SON"]
    assert list(out["kept"]) == ["AND CO LTD"]


def test_strip_tokens_prefers_the_longer_multi_word_token():
    out = _strip(["RT HON JANE DOE"], keep_as="kept")
    assert list(out["out"]) == ["JANE DOE"]
    assert list(out["kept"]) == ["RT HON"]


def test_strip_tokens_anywhere():
    out = _strip(["ACME LTD TRADING CO GROUP"], lists=["legal_forms"], position="anywhere")
    assert list(out["out"]) == ["ACME TRADING GROUP"]


def test_strip_tokens_never_removes_the_last_token():
    """A donor really called 'Ltd' would otherwise clean away to nothing."""
    assert list(_strip(["LTD"], lists=["legal_forms"], position="trailing")["out"]) == ["LTD"]
    assert list(_strip(["MR"])["out"]) == ["MR"]


def test_strip_tokens_may_be_allowed_to_empty_a_value():
    out = _strip(["MR"], keep_one=False, keep_as="kept")
    assert list(out["out"]) == [None]
    assert list(out["kept"]) == ["MR"]


def test_strip_tokens_leaves_a_clean_name_and_an_empty_keep_column():
    out = _strip(["JANE DOE"], keep_as="kept")
    assert list(out["out"]) == ["JANE DOE"]
    assert list(out["kept"]) == [None]


def test_strip_tokens_rejects_an_unknown_position():
    with pytest.raises(engine.RulesetError, match="Unknown position"):
        _strip(["MR SMITH"], position="sideways")


# ---------------------------------------------------------------------------
# nullify and lookup
# ---------------------------------------------------------------------------


def test_nullify_matches_the_whole_value_only():
    step = {"id": "s", "op": "nullify", "target": "out", "lists": ["null_values"]}
    assert _out(["N/A", "none", " - ", "NONE OF THE ABOVE", None], step) == [
        None, None, None, "NONE OF THE ABOVE", None,
    ]


def test_lookup_on_the_whole_value():
    step = {"id": "s", "op": "lookup", "target": "out", "table": "nicknames", "scope": "value"}
    assert _out(["BILL", "bill", "SUSAN", None], step) == [
        "WILLIAM", "WILLIAM", "SUSAN", None,
    ]


def test_lookup_on_each_token():
    step = {"id": "s", "op": "lookup", "target": "out", "table": "nicknames", "scope": "tokens"}
    assert _out(["BILL TOM SMITH"], step) == ["WILLIAM THOMAS SMITH"]


def test_a_null_fallback_drops_what_it_cannot_map():
    step = {"id": "s", "op": "lookup", "target": "out", "table": "drop", "scope": "value"}
    assert _out(["A", "B"], step) == ["ALPHA", None]


def test_an_error_fallback_names_every_unmapped_value():
    step = {"id": "s", "op": "lookup", "target": "out", "table": "strict", "scope": "value"}
    with pytest.raises(engine.UnmappedLookupValuesError) as caught:
        _apply(["A", "B", "C", "B"], step)
    assert caught.value.table == "strict"
    assert caught.value.values == ["B", "C"]      # distinct, sorted
    assert "Add them on the Config screen" in str(caught.value)


def test_lookup_rejects_an_unknown_table_or_scope():
    with pytest.raises(engine.RulesetError, match="Unknown lookup 'nope'"):
        _apply(["A"], {"id": "s", "op": "lookup", "target": "out", "table": "nope"})
    with pytest.raises(engine.RulesetError, match="Unknown lookup scope"):
        _apply(["A"], {"id": "s", "op": "lookup", "target": "out",
                       "table": "nicknames", "scope": "letters"})


# ---------------------------------------------------------------------------
# function ops
# ---------------------------------------------------------------------------


def test_a_single_output_function_writes_the_target():
    step = {"id": "s", "op": "function", "name": "metaphone", "target": "out"}
    assert _out(["SMITH"], step) == [functions.metaphone_value("SMITH")]


def test_a_multi_output_function_writes_its_fixed_columns():
    frame = _apply(["JOHN SMITH"], {"id": "s", "op": "function", "name": "parse_person_name"})
    assert frame["forename"].iloc[0] == "JOHN"
    assert frame["surname"].iloc[0] == "SMITH"


def test_an_unknown_function_is_refused():
    with pytest.raises(engine.RulesetError, match="Unknown function 'wibble'"):
        _apply(["X"], {"id": "s", "op": "function", "name": "wibble", "target": "out"})


def test_an_unknown_op_is_refused():
    with pytest.raises(engine.RulesetError, match="Unknown op 'explode'"):
        _apply(["X"], {"id": "s", "op": "explode", "target": "out"})


# ---------------------------------------------------------------------------
# The distinct-value rule, at op level
# ---------------------------------------------------------------------------


def test_an_op_runs_once_per_distinct_source_value(monkeypatch):
    seen = []
    real = engine._op_upper

    def counting(values, step, ruleset):
        seen.append(list(values))
        return real(values, step, ruleset)

    monkeypatch.setitem(engine.OPS, "upper", counting)
    out = _apply(["a", "a", "b", None, "a"], {"id": "s", "op": "upper", "target": "out"})
    assert seen == [["a", "b"]]
    assert list(out["out"]) == ["A", "A", "B", None, "A"]


# ---------------------------------------------------------------------------
# Chained steps
# ---------------------------------------------------------------------------


def test_a_later_step_reads_an_earlier_target():
    ruleset = {
        "token_lists": TOKEN_LISTS, "lookups": LOOKUPS,
        "cleaning": {"person": [
            {"id": "a", "op": "upper", "source": "name", "target": "name_clean"},
            {"id": "b", "op": "strip_tokens", "source": "name_clean", "target": "name_clean",
             "lists": ["titles"], "position": "leading"},
            {"id": "c", "op": "function", "name": "initials",
             "source": "name_clean", "target": "name_initials"},
        ]},
    }
    frame = pd.DataFrame({"name": ["mr john smith"]})
    out = engine.apply_cleaning(frame, ruleset, "person")
    assert out["name_clean"].iloc[0] == "JOHN SMITH"
    assert out["name_initials"].iloc[0] == "JS"


# ---------------------------------------------------------------------------
# Track assignment
# ---------------------------------------------------------------------------


def _records(rows):
    frame = pd.DataFrame(rows)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame


DONORS = _records([
    {"record_id": "1", "name": "Alice Smith", "donor_status": "Individual"},
    {"record_id": "2", "name": "Acme Ltd", "donor_status": "Company"},
    {"record_id": "3", "name": "Mr A Smith", "donor_status": "Impermissible Donor"},
    {"record_id": "4", "name": "West End Club", "donor_status": "Other"},
    {"record_id": "5", "name": "A Smith", "donor_status": "Impermissible Donor"},
    {"record_id": "6", "name": "Something Unclassifiable", "donor_status": "Other"},
])


def test_the_default_ruleset_sorts_the_donors_as_intended():
    tracks = engine.assign_tracks(DONORS, default_ruleset())
    assert list(tracks) == [
        "person", "organisation", "person", "organisation", "person", "organisation",
    ]


def test_the_first_matching_rule_wins_and_hits_are_counted_once():
    tracks, rules = engine.assign_tracks_detailed(DONORS, default_ruleset())
    hits = {rule["id"]: rule["hits"] for rule in rules}
    assert hits == {"t1": 1, "t2": 1, "t3": 1, "t4": 1, "default": 2}
    assert sum(hits.values()) == len(DONORS)
    assert rules[-1]["id"] == "default"


def test_a_rule_naming_a_column_the_frame_lacks_simply_never_fires():
    ruleset = small_ruleset()
    ruleset["track_rules"][0]["when"][0]["column"] = "not_here"
    tracks = engine.assign_tracks(DONORS.drop(columns=["not_here"], errors="ignore"), ruleset)
    assert set(tracks) == {"organisation"}


def test_the_rule_columns_are_reported_for_the_preview():
    _, rules = engine.assign_tracks_detailed(DONORS, default_ruleset())
    by_id = {rule["id"]: rule for rule in rules}
    assert by_id["t1"]["columns"] == ["donor_status"]
    assert by_id["t2"]["columns"] == ["donor_status", "name"]


# ---------------------------------------------------------------------------
# available_columns
# ---------------------------------------------------------------------------


def test_available_columns_lists_raw_then_targets_in_creation_order():
    columns = engine.available_columns(default_ruleset(), "person", RAW_COLUMNS)
    assert columns["raw"] == RAW_COLUMNS
    assert columns["all"][:len(RAW_COLUMNS)] == RAW_COLUMNS
    assert columns["targets"][:2] == ["name_clean", "title"]
    assert "surname_metaphone" in columns["targets"]
    parse = next(s for s in columns["steps"] if s["step_id"] == "p8")
    assert parse["targets"] == ["forename", "middle_names", "surname", "forename_initial"]


# ---------------------------------------------------------------------------
# The trace — the same steps, with the working shown
# ---------------------------------------------------------------------------


def test_the_trace_and_the_pipeline_agree_on_the_same_input():
    """A preview that disagrees with a run is worse than no preview."""
    ruleset = default_ruleset()
    frame = _records([
        {"record_id": "1", "name": "Mr John Smith MP", "donor_status": "Individual"},
        {"record_id": "2", "name": "Dr. José O'Brien", "donor_status": "Individual"},
    ])
    cleaned = engine.apply_cleaning(frame, ruleset, "person")
    samples = engine.trace_rows(frame, ruleset, "person")

    for position, sample in enumerate(samples):
        for column, value in sample["output"].items():
            expected = cleaned[column].iloc[position]
            expected = None if pd.isna(expected) else expected
            assert value == expected, column


def test_the_trace_reports_each_step():
    ruleset = default_ruleset()
    frame = _records([{"record_id": "1", "name": "Mr John Smith MP",
                       "donor_status": "Individual"}])
    sample = engine.trace_rows(frame, ruleset, "person")[0]

    ids = [step["id"] for step in sample["steps"]]
    assert ids == [s["id"] for s in ruleset["cleaning"]["person"]]

    first = sample["steps"][0]
    assert first == {
        "id": "p1", "op": "upper", "description": "Upper-case the name",
        "source": "name", "before": "Mr John Smith MP",
        "outputs": {"name_clean": "MR JOHN SMITH MP"},
        "changed": True, "error": None,
    }
    titles = next(s for s in sample["steps"] if s["id"] == "p6")
    assert titles["outputs"] == {"name_clean": "JOHN SMITH MP", "title": "MR"}
    assert titles["changed"] is True

    assert sample["input"]["name"] == "Mr John Smith MP"
    assert sample["input"]["record_id"] == "1"
    assert sample["output"]["surname"] == "SMITH"


def test_a_step_that_changes_nothing_says_so():
    ruleset = default_ruleset()
    frame = _records([{"record_id": "1", "name": "JOHN SMITH", "donor_status": "Individual"}])
    sample = engine.trace_rows(frame, ruleset, "person")[0]
    assert next(s for s in sample["steps"] if s["id"] == "p1")["changed"] is False
    assert next(s for s in sample["steps"] if s["id"] == "p6")["changed"] is False


def test_a_broken_draft_rule_is_reported_on_its_own_step():
    """Mid-edit, a bad regex must land on the step, not blow up the preview."""
    ruleset = default_ruleset()
    ruleset["cleaning"]["person"][2] = {
        "id": "p3", "description": "", "op": "regex_replace", "source": "name_clean",
        "target": "name_clean", "pattern": "(unclosed", "replacement": "",
    }
    frame = _records([{"record_id": "1", "name": "Mr John Smith", "donor_status": "Individual"}])
    sample = engine.trace_rows(frame, ruleset, "person")[0]

    broken = next(s for s in sample["steps"] if s["id"] == "p3")
    assert "Invalid regular expression" in broken["error"]
    assert broken["changed"] is False
    # The steps after it still ran.
    assert sample["output"]["surname"] == "SMITH"


def test_apply_cleaning_still_raises_on_a_broken_rule():
    """A run must fail loudly where a preview forgives."""
    ruleset = default_ruleset()
    ruleset["cleaning"]["person"][2]["pattern"] = "(unclosed"
    ruleset["cleaning"]["person"][2]["op"] = "regex_replace"
    ruleset["cleaning"]["person"][2]["replacement"] = ""
    frame = _records([{"record_id": "1", "name": "Mr John Smith", "donor_status": "Individual"}])
    with pytest.raises(engine.RulesetError):
        engine.apply_cleaning(frame, ruleset, "person")


def test_an_unmapped_lookup_stops_a_preview_too():
    """A missing nickname row is a data problem the user must fix, not a typo
    in the rule they are editing — so it is not swallowed as a step error."""
    ruleset = default_ruleset()
    ruleset["lookups"]["nicknames"]["fallback"] = "error"
    ruleset["lookups"]["nicknames"]["rows"] = []
    frame = _records([{"record_id": "1", "name": "John Smith", "donor_status": "Individual"}])
    with pytest.raises(engine.UnmappedLookupValuesError):
        engine.trace_rows(frame, ruleset, "person")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _errors(edit):
    ruleset = small_ruleset()
    edit(ruleset)
    return engine.validate_ruleset(ruleset, RAW_COLUMNS)


def _first(edit):
    errors = _errors(edit)
    assert errors, "expected at least one error"
    return errors[0]


def test_the_shipped_default_is_valid():
    assert engine.validate_ruleset(default_ruleset(), RAW_COLUMNS) == []


def test_a_valid_small_ruleset_has_no_errors():
    assert engine.validate_ruleset(small_ruleset(), RAW_COLUMNS) == []


def test_something_that_is_not_an_object_is_refused():
    assert engine.validate_ruleset(["not", "a", "ruleset"], RAW_COLUMNS) == [
        {"path": "", "message": "A ruleset must be an object"}
    ]


def test_unknown_op():
    def edit(r):
        r["cleaning"]["person"][0]["op"] = "explode"
    assert _first(edit) == {"path": "cleaning.person[0].op", "message": "Unknown op 'explode'"}


def test_unknown_function():
    def edit(r):
        r["cleaning"]["person"][0].update({"op": "function", "name": "wibble"})
    assert _first(edit)["path"] == "cleaning.person[0].name"


def test_unknown_token_list_in_a_step_and_in_a_rule():
    def step(r):
        r["cleaning"]["person"][0].update({"op": "nullify", "lists": ["nope"]})
    assert _first(step) == {"path": "cleaning.person[0].lists",
                            "message": "Unknown token list 'nope'"}

    def rule(r):
        r["track_rules"][0]["when"][0] = {
            "column": "name", "op": "contains_token", "lists": ["nope"]}
    assert _first(rule)["path"] == "track_rules[0].when[0].lists"


def test_unknown_lookup():
    def edit(r):
        r["cleaning"]["person"][0].update({"op": "lookup", "table": "nope"})
    assert _first(edit) == {"path": "cleaning.person[0].table",
                            "message": "Unknown lookup 'nope'"}


def test_invalid_regex_in_a_step_and_in_a_condition():
    def step(r):
        r["cleaning"]["person"][0].update(
            {"op": "regex_replace", "pattern": "(unclosed", "replacement": ""})
    assert _first(step)["path"] == "cleaning.person[0].pattern"

    def condition(r):
        r["track_rules"][0]["when"][0] = {
            "column": "name", "op": "matches", "pattern": "(unclosed"}
    assert _first(condition)["path"] == "track_rules[0].when[0].pattern"


def test_a_source_that_no_earlier_step_produced():
    def edit(r):
        r["cleaning"]["person"][0]["source"] = "name_core"
    error = _first(edit)
    assert error["path"] == "cleaning.person[0].source"
    assert "neither a raw column nor written by an earlier step" in error["message"]


def test_a_source_produced_by_an_earlier_step_is_fine():
    def edit(r):
        r["cleaning"]["person"].append({
            "id": "p2", "op": "lower", "source": "name_clean", "target": "name_lower"})
    assert _errors(edit) == []


def test_a_step_may_not_overwrite_a_raw_column():
    def edit(r):
        r["cleaning"]["person"][0]["target"] = "donor_status"
    error = _first(edit)
    assert error["path"] == "cleaning.person[0].target"
    assert "may not be overwritten" in error["message"]


def test_a_multi_output_function_may_not_overwrite_a_raw_column():
    def edit(r):
        r["cleaning"]["person"][0] = {
            "id": "p1", "op": "function", "name": "parse_person_name", "source": "name"}
        r["cleaning"]["person"].append({
            "id": "p2", "op": "strip_tokens", "source": "surname", "target": "surname",
            "lists": ["titles"], "position": "leading", "keep_as": "name"})
    paths = [e["path"] for e in _errors(edit)]
    assert "cleaning.person[1].target" in paths


def test_duplicate_ids_in_every_collection():
    def steps(r):
        r["cleaning"]["person"].append(dict(r["cleaning"]["person"][0]))
    assert _first(steps) == {"path": "cleaning.person[1].id",
                             "message": "Duplicate step id 'p1'"}

    def rules(r):
        r["track_rules"].append(copy.deepcopy(r["track_rules"][0]))
    assert _first(rules)["message"] == "Duplicate rule id 'r1'"

    def keys(r):
        key = {"id": "k1", "track": "person", "columns": ["name_clean"]}
        r["match_keys"] = [key, dict(key)]
    assert _first(keys)["message"] == "Duplicate match key id 'k1'"


def test_a_missing_id():
    def edit(r):
        del r["cleaning"]["person"][0]["id"]
    assert _first(edit) == {"path": "cleaning.person[0].id", "message": "A step needs an id"}


def test_unknown_track_in_a_rule_in_cleaning_and_in_a_key():
    def rule(r):
        r["track_rules"][0]["track"] = "alien"
    assert _first(rule)["path"] == "track_rules[0].track"

    def cleaning(r):
        r["cleaning"]["alien"] = []
    assert _first(cleaning)["path"] == "cleaning.alien"

    def key(r):
        r["match_keys"] = [{"id": "k1", "track": "alien", "columns": ["name_clean"]}]
    assert _first(key)["path"] == "match_keys[0].track"


def test_a_bad_default_track():
    def edit(r):
        r["default_track"] = "banana"
    assert _first(edit)["path"] == "default_track"


def test_bad_position_scope_and_fallback_values():
    def position(r):
        r["cleaning"]["person"][0].update(
            {"op": "strip_tokens", "lists": ["titles"], "position": "sideways"})
    assert _first(position)["path"] == "cleaning.person[0].position"

    def scope(r):
        r["lookups"]["x"] = {"fallback": "passthrough", "rows": []}
        r["cleaning"]["person"][0].update({"op": "lookup", "table": "x", "scope": "letters"})
    assert _first(scope)["path"] == "cleaning.person[0].scope"

    def fallback(r):
        r["lookups"]["x"] = {"fallback": "shrug", "rows": []}
    assert _first(fallback)["path"] == "lookups.x.fallback"


def test_bad_applies_when_and_on_guard_fail():
    def applies(r):
        r["match_keys"] = [{"id": "k1", "track": "person", "columns": ["name_clean"],
                            "applies_when": "sometimes"}]
    assert _first(applies)["path"] == "match_keys[0].applies_when"

    def guard(r):
        r["match_keys"] = [{"id": "k1", "track": "person", "columns": ["name_clean"],
                            "on_guard_fail": "panic"}]
    assert _first(guard)["path"] == "match_keys[0].on_guard_fail"


def test_a_match_key_naming_a_column_the_track_never_makes():
    def edit(r):
        r["match_keys"] = [{"id": "k1", "track": "person", "columns": ["postcode_clean"]}]
    error = _first(edit)
    assert error["path"] == "match_keys[0].columns"
    assert "not a column of the person track" in error["message"]


def test_a_match_key_guard_naming_an_unknown_column_or_list():
    def edit(r):
        r["match_keys"] = [{
            "id": "k1", "track": "person", "columns": ["name_clean"],
            "guards": {"blocklists": ["nope"],
                       "max_distinct": {"column": "name_core", "count": 3},
                       "require_any_equal": ["postcode_clean"]},
        }]
    paths = [e["path"] for e in _errors(edit)]
    assert paths == [
        "match_keys[0].guards.blocklists",
        "match_keys[0].guards.max_distinct.column",
        "match_keys[0].guards.require_any_equal",
    ]


def test_a_condition_reading_a_column_that_is_not_raw():
    def edit(r):
        r["track_rules"][0]["when"][0]["column"] = "name_clean"
    error = _first(edit)
    assert error["path"] == "track_rules[0].when[0].column"
    assert "not a column of the input records" in error["message"]


def test_a_condition_missing_its_argument():
    def missing_values(r):
        r["track_rules"][0]["when"][0] = {"column": "name", "op": "in"}
    assert _first(missing_values)["path"] == "track_rules[0].when[0].values"

    def missing_value(r):
        r["track_rules"][0]["when"][0] = {"column": "name", "op": "equals"}
    assert _first(missing_value)["path"] == "track_rules[0].when[0].value"


def test_malformed_token_lists_lookups_and_vetoes():
    def tokens(r):
        r["token_lists"]["titles"]["tokens"] = "MR"
    assert _first(tokens)["path"] == "token_lists.titles.tokens"

    def rows(r):
        r["lookups"]["x"] = {"fallback": "passthrough", "rows": [{"raw": "A"}]}
    assert _first(rows)["path"] == "lookups.x.rows[0]"

    def vetoes(r):
        r["vetoes"] = {"nope": True}
    assert _first(vetoes)["path"] == "vetoes"


def test_every_problem_is_reported_at_once():
    """A user editing a form wants the whole list, not the first mistake."""
    def edit(r):
        r["default_track"] = "banana"
        r["cleaning"]["person"][0]["op"] = "explode"
        r["track_rules"][0]["track"] = "alien"
    paths = [e["path"] for e in _errors(edit)]
    assert paths == ["track_rules[0].track", "default_track", "cleaning.person[0].op"]

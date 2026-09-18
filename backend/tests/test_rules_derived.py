# backend/tests/test_rules_derived.py
"""Derived columns (D8a, stage 1): the operator, the rules, and validation."""

import copy
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset, small_ruleset

from app.profiles.donations import RAW_COLUMNS
from app.rules import conditions, engine

OVERRIDABLE = ["Company", "Limited Liability Partnership",
               "Unincorporated Association", "Other", "Trust"]

# Statuses D8a says a company-number rule must never touch.
PROTECTED = ["Registered Political Party", "Trade Union", "Friendly Society",
             "Public Fund", "Individual"]


# ---------------------------------------------------------------------------
# The starts_with operator
# ---------------------------------------------------------------------------


def _mask(column, **condition):
    return list(conditions.evaluate(condition, pd.Series(column, dtype="object"), {}))


def test_starts_with_is_case_insensitive_and_null_is_false():
    values = ["OC314414", "oc314414", "SC123456", "04250076", "", None, "  oc1  "]
    assert _mask(values, op="starts_with", values=["OC"]) == [
        True, True, False, False, False, False, True,
    ]


def test_starts_with_accepts_several_prefixes():
    values = ["OC1", "SO2", "NC3", "NI4"]
    assert _mask(values, op="starts_with", values=["OC", "SO", "NC"]) == [
        True, True, True, False,
    ]


def test_starts_with_matches_a_prefix_not_a_whole_word():
    """The point of the operator: 'NI' is not a word of 'NI654321'."""
    assert _mask(["NI654321"], op="starts_with", values=["NI"]) == [True]
    assert _mask(["NI654321"], op="equals", value="NI") == [False]


def test_starts_with_no_prefixes_matches_nothing():
    assert _mask(["OC1"], op="starts_with", values=[]) == [False]
    assert _mask(["OC1"], op="starts_with", values=["  "]) == [False]


def test_starts_with_runs_once_per_distinct_value(monkeypatch):
    seen = []
    real = conditions._prefix_mask

    def counting(text, prefixes):
        seen.append(list(text[text.notna()].drop_duplicates()))
        return real(text, prefixes)

    monkeypatch.setattr(conditions, "_prefix_mask", counting)
    _mask(["OC1", "OC1", "SC2", None, "OC1"], op="starts_with", values=["OC"])
    assert seen == [["OC1", "SC2"]]


def test_starts_with_is_valid_in_a_track_rule():
    ruleset = small_ruleset()
    ruleset["track_rules"][0]["when"] = [
        {"column": "company_number", "op": "starts_with", "values": ["OC"]},
    ]
    assert engine.validate_ruleset(ruleset, RAW_COLUMNS) == []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _records(rows):
    frame = pd.DataFrame(rows)
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame


def _with_derived(ruleset, *columns):
    ruleset = copy.deepcopy(ruleset)
    ruleset["derived_columns"] = list(columns)
    return ruleset


def _column(**overrides):
    column = {
        "id": "d1", "target": "status_std", "description": "",
        "default_from": "donor_status", "tracks": ["organisation"],
        "rules": [
            {"id": "d1r1", "description": "LLP prefixes",
             "when": [{"column": "company_number_clean", "op": "starts_with",
                       "values": ["OC", "SO", "NC"]},
                      {"column": "donor_status", "op": "in", "values": OVERRIDABLE}],
             "value": "Limited Liability Partnership"},
            {"id": "d1r2", "description": "Digits are a company",
             "when": [{"column": "company_number_clean", "op": "matches", "pattern": "^[0-9]"},
                      {"column": "donor_status", "op": "in", "values": OVERRIDABLE}],
             "value": "Company"},
        ],
    }
    column.update(overrides)
    return column


ROWS = [
    {"record_id": "1", "name": "Acme LLP", "donor_status": "Company",
     "company_number": "OC314414"},
    {"record_id": "2", "name": "Acme Ltd", "donor_status": "Unincorporated Association",
     "company_number": "4250076"},
    {"record_id": "3", "name": "Barnet Association", "donor_status": "Unincorporated Association"},
    {"record_id": "4", "name": "Labour Party", "donor_status": "Registered Political Party",
     "company_number": "OC999999"},
    {"record_id": "5", "name": "Mr John Smith", "donor_status": "Individual",
     "company_number": "4250076"},
]


def _derive(rows=None, ruleset=None):
    from app.pipeline.dedupe.stage_1_clean import clean_records_detailed

    return clean_records_detailed(
        _records(rows or ROWS), ruleset or _with_derived(default_ruleset(), _column())
    )


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------


def test_the_first_matching_rule_sets_the_value():
    frame, _ = _derive()
    by_id = frame.set_index("record_id")
    assert by_id.loc["1", "status_std"] == "Limited Liability Partnership"
    assert by_id.loc["1", "status_std_rule"] == "d1r1"
    assert by_id.loc["2", "status_std"] == "Company"
    assert by_id.loc["2", "status_std_rule"] == "d1r2"


def test_a_record_no_rule_catches_takes_the_default_and_no_rule_id():
    frame, _ = _derive()
    by_id = frame.set_index("record_id")
    assert by_id.loc["3", "status_std"] == "Unincorporated Association"
    assert by_id.loc["3", "status_std_rule"] is None


def test_rules_are_tried_in_order():
    """Swapping two rules changes which one claims a record."""
    column = _column()
    column["rules"] = list(reversed(column["rules"]))
    rows = [{"record_id": "1", "name": "x", "donor_status": "Company",
             "company_number": "OC314414"}]
    frame, _ = _derive(rows, _with_derived(default_ruleset(), column))
    # The digit rule cannot claim OC314414, so the LLP rule still does.
    assert frame.iloc[0]["status_std_rule"] == "d1r1"

    frame, _ = _derive(
        [{"record_id": "1", "name": "x", "donor_status": "Company", "company_number": "4250076"}],
        _with_derived(default_ruleset(), column),
    )
    assert frame.iloc[0]["status_std_rule"] == "d1r2"


def test_tracks_limit_which_records_the_rules_see():
    """A person with a company number keeps their status: the column is scoped
    to organisations, and D8a protects Individual anyway."""
    frame, _ = _derive()
    person = frame.set_index("record_id").loc["5"]
    assert person["track"] == "person"
    assert person["status_std"] == "Individual"
    assert person["status_std_rule"] is None


def test_no_tracks_means_every_track():
    frame, _ = _derive(ruleset=_with_derived(default_ruleset(), _column(tracks=[])))
    person = frame.set_index("record_id").loc["5"]
    # The rule's own donor_status condition is what protects Individual now.
    assert person["status_std"] == "Individual"

    open_column = _column(tracks=None, rules=[{
        "id": "r", "description": "", "value": "Everyone",
        "when": [{"column": "name", "op": "not_null"}],
    }])
    frame, _ = _derive(ruleset=_with_derived(default_ruleset(), open_column))
    assert set(frame["status_std"]) == {"Everyone"}


def test_a_protected_status_is_never_changed():
    frame, _ = _derive()
    party = frame.set_index("record_id").loc["4"]
    assert party["status_std"] == "Registered Political Party"
    assert party["status_std_rule"] is None


def test_the_default_from_column_supplies_the_untouched_value():
    column = _column(default_from="name", rules=[])
    frame, _ = _derive(ruleset=_with_derived(default_ruleset(), column))
    assert list(frame["status_std"]) == [r["name"] for r in ROWS]


def test_a_missing_default_from_column_leaves_nulls():
    column = _column(default_from="not_a_column", rules=[])
    frame, _ = _derive(ruleset=_with_derived(default_ruleset(), column))
    assert frame["status_std"].isna().all()


def test_derived_columns_run_in_order_and_may_read_an_earlier_target():
    first = _column()
    second = {
        "id": "d2", "target": "status_group", "description": "",
        "default_from": "status_std", "tracks": ["organisation"],
        "rules": [{"id": "d2r1", "description": "LLPs and companies are incorporated",
                   "when": [{"column": "status_std", "op": "in",
                             "values": ["Company", "Limited Liability Partnership"]}],
                   "value": "Incorporated"}],
    }
    frame, reports = _derive(ruleset=_with_derived(default_ruleset(), first, second))
    by_id = frame.set_index("record_id")
    assert by_id.loc["1", "status_group"] == "Incorporated"
    assert by_id.loc["2", "status_group"] == "Incorporated"
    assert by_id.loc["3", "status_group"] == "Unincorporated Association"
    assert [r["target"] for r in reports] == ["status_std", "status_group"]


def test_a_rule_naming_a_column_the_frame_lacks_fires_for_nobody():
    column = _column(rules=[{"id": "r", "description": "", "value": "X",
                             "when": [{"column": "not_here", "op": "not_null"}]}])
    frame, reports = _derive(ruleset=_with_derived(default_ruleset(), column))
    assert reports[0]["rules"][0]["hits"] == 0
    assert frame["status_std_rule"].isna().all()


def test_an_empty_value_after_a_rule_becomes_null():
    column = _column(rules=[{"id": "r", "description": "", "value": "   ",
                             "when": [{"column": "name", "op": "not_null"}]}])
    frame, _ = _derive(ruleset=_with_derived(default_ruleset(), column))
    organisations = frame[frame["track"] == "organisation"]
    assert organisations["status_std"].isna().all()


def test_a_ruleset_with_no_derived_section_is_simply_left_alone():
    ruleset = copy.deepcopy(default_ruleset())
    del ruleset["derived_columns"]
    frame, reports = _derive(ruleset=ruleset)
    assert reports == []
    assert not [c for c in frame.columns if c.endswith("_rule")]


# ---------------------------------------------------------------------------
# The report the preview is built from
# ---------------------------------------------------------------------------


def test_hits_cover_every_record_exactly_once():
    _, reports = _derive()
    report = reports[0]
    hits = {rule["id"]: rule["hits"] for rule in report["rules"]}
    assert hits == {"d1r1": 1, "d1r2": 1, "default": 3}
    assert sum(hits.values()) == report["total"] == len(ROWS)
    assert report["rules"][-1]["id"] == "default"


def test_changed_counts_only_the_records_whose_value_moved():
    _, reports = _derive()
    report = reports[0]
    # Record 1 Company -> LLP and record 2 Unincorporated Association -> Company.
    assert report["changed"] == 2


def test_a_rule_that_sets_the_value_a_record_already_had_is_a_hit_not_a_change():
    rows = [{"record_id": "1", "name": "x", "donor_status": "Company",
             "company_number": "4250076"}]
    _, reports = _derive(rows)
    report = reports[0]
    assert report["rules"][1]["hits"] == 1
    assert report["changed"] == 0


def test_transitions_add_up_to_changed_and_come_largest_first():
    rows = ROWS + [
        {"record_id": f"a{i}", "name": "x", "donor_status": "Unincorporated Association",
         "company_number": "4250076"} for i in range(4)
    ]
    _, reports = _derive(rows)
    report = reports[0]
    moves = engine.transitions(report)
    assert sum(t["count"] for t in moves) == report["changed"]
    assert [t["count"] for t in moves] == sorted((t["count"] for t in moves), reverse=True)
    assert moves[0] == {"from": "Unincorporated Association", "to": "Company", "count": 5}
    assert {"from": "Company", "to": "Limited Liability Partnership", "count": 1} in moves


def test_transitions_of_an_unchanged_column_are_empty():
    column = _column(rules=[])
    _, reports = _derive(ruleset=_with_derived(default_ruleset(), column))
    assert engine.transitions(reports[0]) == []
    assert reports[0]["changed"] == 0


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def test_available_columns_lists_derived_targets_after_the_cleaning_ones():
    ruleset = _with_derived(default_ruleset(), _column())
    columns = engine.available_columns(ruleset, "organisation", RAW_COLUMNS)
    assert columns["all"][-1] == "status_std"
    assert columns["all"].index("status_std") > columns["all"].index("name_core")
    assert columns["derived"] == [{"derived_id": "d1", "targets": ["status_std", "status_std_rule"]}]
    # `steps` stays the cleaning steps, so the Config screen does not read a
    # derived column as a clash with itself.
    assert all("status_std" not in s["targets"] for s in columns["steps"])


def test_a_derived_column_is_absent_from_a_track_it_does_not_apply_to():
    ruleset = _with_derived(default_ruleset(), _column())
    person = engine.available_columns(ruleset, "person", RAW_COLUMNS)
    assert "status_std" not in person["all"]
    assert person["derived"] == []


def test_a_match_key_may_name_a_derived_column():
    ruleset = _with_derived(default_ruleset(), _column())
    ruleset["match_keys"].append({
        "id": "kd", "name": "Status", "track": "organisation", "tier": 3,
        "columns": ["name_core", "status_std"],
    })
    assert engine.validate_ruleset(ruleset, RAW_COLUMNS) == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _errors(*columns, edit=None):
    ruleset = _with_derived(default_ruleset(), *columns)
    if edit:
        edit(ruleset)
    return engine.validate_ruleset(ruleset, RAW_COLUMNS)


def _first(*columns, edit=None):
    errors = _errors(*columns, edit=edit)
    assert errors, "expected at least one error"
    return errors[0]


def test_the_shipped_default_is_valid():
    assert engine.validate_ruleset(default_ruleset(), RAW_COLUMNS) == []


def test_a_good_derived_column_has_no_errors():
    assert _errors(_column()) == []


def test_derived_columns_must_be_a_list():
    assert _first(edit=lambda r: r.update(derived_columns={"d1": {}})) == {
        "path": "derived_columns", "message": "derived_columns must be a list",
    }


def test_a_target_must_be_snake_case():
    assert _first(_column(target="Donor Status"))["path"] == "derived_columns[0].target"
    assert _first(_column(target="_leading"))["path"] == "derived_columns[0].target"
    assert _errors(_column(target="donor_status_2")) == []


def test_a_target_may_not_be_missing():
    assert _first(_column(target=None)) == {
        "path": "derived_columns[0].target", "message": "A derived column needs a target",
    }


def test_a_target_may_not_be_a_raw_column():
    error = _first(_column(target="donor_status"))
    assert error["path"] == "derived_columns[0].target"
    assert "is a raw column" in error["message"]


def test_a_target_may_not_be_a_cleaning_target():
    error = _first(_column(target="name_core"))
    assert "already written by a cleaning step" in error["message"]


def test_a_target_may_not_be_another_derived_target():
    error = _first(_column(), _column(id="d2"))
    assert error["path"] == "derived_columns[1].target"
    assert "already written by another derived column" in error["message"]


def test_a_target_may_not_end_with_the_rule_suffix():
    error = _first(_column(target="status_rule"))
    assert error["path"] == "derived_columns[0].target"
    assert "may not end with '_rule'" in error["message"]


def test_default_from_must_exist_and_is_required():
    assert _first(_column(default_from=None)) == {
        "path": "derived_columns[0].default_from",
        "message": "A derived column needs a default_from column",
    }
    error = _first(_column(default_from="nope"))
    assert error["path"] == "derived_columns[0].default_from"
    assert "not a column of the organisation track" in error["message"]


def test_default_from_must_exist_for_every_track_the_column_applies_to():
    """name_core is an organisation cleaning target; a person never has one."""
    error = _first(_column(default_from="name_core", tracks=["person", "organisation"]))
    assert error["path"] == "derived_columns[0].default_from"
    assert "person track" in error["message"]
    assert _errors(_column(default_from="name_core")) == []


def test_a_condition_column_must_exist_for_every_applicable_track():
    column = _column(tracks=["person", "organisation"])
    error = _first(column)
    assert error["path"] == "derived_columns[0].rules[0].when[0].column"
    assert "'company_number_clean' is not a column of the person track" in error["message"]


def test_a_condition_may_read_an_earlier_derived_target():
    second = {
        "id": "d2", "target": "status_group", "description": "",
        "default_from": "status_std", "tracks": ["organisation"],
        "rules": [{"id": "d2r1", "description": "", "value": "Incorporated",
                   "when": [{"column": "status_std", "op": "not_null"}]}],
    }
    assert _errors(_column(), second) == []
    # ...but not a later one.
    error = _first(second, _column())
    assert error["path"] == "derived_columns[0].default_from"


def test_unique_ids_for_columns_and_for_rules():
    assert _first(_column(), _column(id="d1", target="other"))["message"] == \
        "Duplicate derived column id 'd1'"

    column = _column()
    column["rules"][1]["id"] = "d1r1"
    assert _first(column) == {
        "path": "derived_columns[0].rules[1].id", "message": "Duplicate rule id 'd1r1'",
    }


def test_a_rule_needs_an_id_a_value_and_a_condition():
    column = _column(rules=[{"description": "", "when": [], "value": ""}])
    paths = [e["path"] for e in _errors(column)]
    assert paths == [
        "derived_columns[0].rules[0].id",
        "derived_columns[0].rules[0].value",
        "derived_columns[0].rules[0].when",
    ]


def test_a_value_may_not_be_blank_or_a_non_string():
    for value in ("", "   ", None, 3):
        column = _column(rules=[{"id": "r", "description": "", "value": value,
                                 "when": [{"column": "name", "op": "not_null"}]}])
        assert _first(column) == {
            "path": "derived_columns[0].rules[0].value",
            "message": "A rule needs a value to set",
        }


def test_an_unknown_track():
    error = _first(_column(tracks=["alien"]))
    assert error["path"] == "derived_columns[0].tracks"
    assert "Unknown track 'alien'" in error["message"]
    assert _first(_column(tracks="organisation"))["path"] == "derived_columns[0].tracks"


def test_a_broken_condition_inside_a_derived_rule():
    column = _column(rules=[{"id": "r", "description": "", "value": "X", "when": [
        {"column": "name_clean", "op": "matches", "pattern": "(unclosed"},
    ]}])
    assert _first(column)["path"] == "derived_columns[0].rules[0].when[0].pattern"

    column = _column(rules=[{"id": "r", "description": "", "value": "X", "when": [
        {"column": "name_clean", "op": "sounds_like", "value": "x"},
    ]}])
    assert _first(column)["path"] == "derived_columns[0].rules[0].when[0].op"


def test_a_column_that_is_not_an_object():
    assert _first(edit=lambda r: r.update(derived_columns=["nope"])) == {
        "path": "derived_columns[0]", "message": "A derived column must be an object",
    }


# ---------------------------------------------------------------------------
# The donations default, on hand-made rows
# ---------------------------------------------------------------------------


DEFAULT_ROWS = [
    ("Company", "OC314414", "Limited Liability Partnership", "d1r1"),
    ("Company", "SO301234", "Limited Liability Partnership", "d1r1"),
    ("Other", "NC001234", "Limited Liability Partnership", "d1r1"),
    ("Company", "IP00123R", "Friendly Society", "d1r2"),
    ("Unincorporated Association", "SP00456", "Friendly Society", "d1r2"),
    ("Unincorporated Association", "SC123456", "Company", "d1r3"),
    ("Other", "NI654321", "Company", "d1r3"),
    ("Unincorporated Association", "04250076", "Company", "d1r4"),
    ("Trust", "ZZ123456", "Company", "d1r5"),
    # Protected statuses keep theirs, company number or not.
    ("Registered Political Party", "OC314414", "Registered Political Party", None),
    ("Trade Union", "04250076", "Trade Union", None),
    ("Friendly Society", "04250076", "Friendly Society", None),
    ("Public Fund", "SC123456", "Public Fund", None),
    # No company number, nothing to go on.
    ("Unincorporated Association", None, "Unincorporated Association", None),
]


@pytest.mark.parametrize("status,number,expected,rule", DEFAULT_ROWS)
def test_the_donations_default_standardises_the_status(status, number, expected, rule):
    rows = [{"record_id": "1", "name": "A Donor Ltd", "donor_status": status,
             "company_number": number}]
    frame, _ = _derive(rows, default_ruleset())
    row = frame.iloc[0]
    assert row["track"] == "organisation"
    assert row["donor_status_std"] == expected
    assert row["donor_status_std_rule"] == rule


def test_the_donations_default_never_touches_an_individual():
    rows = [{"record_id": "1", "name": "Mr John Smith", "donor_status": "Individual",
             "company_number": "04250076"}]
    frame, _ = _derive(rows, default_ruleset())
    assert frame.iloc[0]["donor_status_std"] == "Individual"
    assert frame.iloc[0]["donor_status_std_rule"] is None


def test_every_company_number_rule_is_limited_to_the_overridable_set():
    """The guard D8a turns on: only five statuses may be overridden."""
    for status in PROTECTED:
        rows = [{"record_id": "1", "name": "X", "donor_status": status,
                 "company_number": number}
                for number in ("OC314414", "IP00123R", "SC123456", "04250076")]
        for row in rows:
            frame, _ = _derive([row], default_ruleset())
            assert frame.iloc[0]["donor_status_std"] == status, (status, row)

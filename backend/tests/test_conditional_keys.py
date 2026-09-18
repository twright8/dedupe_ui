# backend/tests/test_conditional_keys.py
"""Match keys with a `when` condition (D13c), and the evidence focus."""

import copy
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from tests.rulesets import default_ruleset

from app.profiles import get_profile
from app.profiles.base import EVIDENCE_FOCUS_OPS, EvidenceFocus, evidence_focus_id
from app.profiles.donations import EVIDENCE_FOCUS, RAW_COLUMNS
from app.rules import engine, keys


def records(*rows) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    if "track" not in frame.columns:
        frame["track"] = "organisation"
    return frame


def a_ruleset(*match_keys) -> dict:
    return {
        "token_lists": {"placeholders": {"tokens": ["00000000"]}},
        "match_keys": list(match_keys),
    }


def a_key(**overrides) -> dict:
    key = {"id": "k1", "name": "Name", "track": "organisation", "tier": 1,
           "columns": ["name_core"], "allow_null": False, "applies_when": "always"}
    key.update(overrides)
    return key


UNIONS = records(
    {"record_id": "1", "name_core": "UNISON", "donor_status_std": "Trade Union"},
    {"record_id": "2", "name_core": "UNISON", "donor_status_std": "Trade Union"},
    {"record_id": "3", "name_core": "ACME", "donor_status_std": "Company"},
    {"record_id": "4", "name_core": "ACME", "donor_status_std": "Company"},
)

UNION_WHEN = [{"column": "donor_status_std", "op": "equals", "value": "Trade Union"}]


# ---------------------------------------------------------------------------
# The condition decides who is eligible
# ---------------------------------------------------------------------------


def test_a_key_with_no_condition_still_groups_everyone():
    groups, stats = keys.apply_match_keys(UNIONS, a_ruleset(a_key()))
    assert groups["group_id"].nunique() == 2
    assert stats["keys"][0]["excluded_by_condition"] == 0


def test_a_condition_limits_the_key_to_one_kind_of_record():
    groups, stats = keys.apply_match_keys(UNIONS, a_ruleset(a_key(when=UNION_WHEN)))
    assert set(groups["record_id"]) == {"1", "2"}
    assert groups["group_id"].nunique() == 1

    reported = stats["keys"][0]
    assert reported["eligible_records"] == 2
    assert reported["excluded_by_condition"] == 2
    assert reported["records"] == 2


def test_excluded_by_condition_counts_the_key_s_own_track_only():
    frame = records(
        {"record_id": "1", "name_core": "UNISON", "donor_status_std": "Trade Union",
         "track": "organisation"},
        {"record_id": "2", "name_core": "UNISON", "donor_status_std": "Trade Union",
         "track": "organisation"},
        {"record_id": "3", "name_core": "ACME", "donor_status_std": "Company",
         "track": "organisation"},
        {"record_id": "4", "name_core": "ANN LEE", "donor_status_std": "Individual",
         "track": "person"},
    )
    _, stats = keys.apply_match_keys(frame, a_ruleset(a_key(when=UNION_WHEN)))
    # The person is not this key's business; only the company was excluded.
    assert stats["keys"][0]["excluded_by_condition"] == 1


def test_several_conditions_are_anded():
    frame = records(
        {"record_id": "1", "name_core": "UNISON", "donor_status_std": "Trade Union",
         "postcode_clean": "SW1A 1AA"},
        {"record_id": "2", "name_core": "UNISON", "donor_status_std": "Trade Union",
         "postcode_clean": None},
        {"record_id": "3", "name_core": "UNISON", "donor_status_std": "Trade Union",
         "postcode_clean": "SW1A 1AA"},
    )
    key = a_key(when=UNION_WHEN + [{"column": "postcode_clean", "op": "not_null"}])
    groups, stats = keys.apply_match_keys(frame, a_ruleset(key))
    assert set(groups["record_id"]) == {"1", "3"}
    assert stats["keys"][0]["eligible_records"] == 2


def test_a_condition_may_read_any_operator_the_rules_use():
    frame = records(
        {"record_id": "1", "name_core": "A", "company_number_clean": "OC314414"},
        {"record_id": "2", "name_core": "A", "company_number_clean": "OC999999"},
        {"record_id": "3", "name_core": "A", "company_number_clean": "04250076"},
    )
    key = a_key(when=[{"column": "company_number_clean", "op": "starts_with",
                       "values": ["OC"]}])
    groups, _ = keys.apply_match_keys(frame, a_ruleset(key))
    assert set(groups["record_id"]) == {"1", "2"}


def test_a_condition_naming_a_missing_column_makes_the_key_fire_for_nobody():
    key = a_key(when=[{"column": "not_here", "op": "not_null"}])
    groups, stats = keys.apply_match_keys(UNIONS, a_ruleset(key))
    assert len(groups) == 0
    assert stats["keys"][0]["eligible_records"] == 0
    assert stats["keys"][0]["excluded_by_condition"] == 4


def test_an_empty_condition_list_holds_for_everybody():
    groups, stats = keys.apply_match_keys(UNIONS, a_ruleset(a_key(when=[])))
    assert groups["group_id"].nunique() == 2
    assert stats["keys"][0]["excluded_by_condition"] == 0


# ---------------------------------------------------------------------------
# no_earlier_key and conditions together
# ---------------------------------------------------------------------------


def test_a_record_an_earlier_condition_excluded_is_not_covered_by_that_key():
    """The point of the rule: an earlier key that never applied to a record must
    not stop a later `no_earlier_key` key from seeing it."""
    frame = records(
        {"record_id": "1", "name_core": "ACME", "postcode_clean": "SW1A 1AA",
         "donor_status_std": "Company"},
        {"record_id": "2", "name_core": "ACME", "postcode_clean": "SW1A 1AA",
         "donor_status_std": "Company"},
    )
    first = a_key(id="k1", tier=1, columns=["postcode_clean"], when=UNION_WHEN)
    second = a_key(id="k2", tier=2, columns=["name_core"], applies_when="no_earlier_key")

    groups, stats = keys.apply_match_keys(frame, a_ruleset(first, second))
    by_id = {k["id"]: k for k in stats["keys"]}
    assert by_id["k1"]["eligible_records"] == 0
    assert by_id["k2"]["eligible_records"] == 2
    assert groups["group_id"].nunique() == 1
    assert set(groups["key_ids"]) == {"k2"}


def test_a_record_an_earlier_key_did_cover_is_still_skipped():
    frame = records(
        {"record_id": "1", "name_core": "ACME", "postcode_clean": "SW1A 1AA",
         "donor_status_std": "Company"},
        {"record_id": "2", "name_core": "ACME", "postcode_clean": "SW1A 1AA",
         "donor_status_std": "Company"},
    )
    company = [{"column": "donor_status_std", "op": "equals", "value": "Company"}]
    first = a_key(id="k1", tier=1, columns=["postcode_clean"], when=company)
    second = a_key(id="k2", tier=2, columns=["name_core"], applies_when="no_earlier_key")

    _, stats = keys.apply_match_keys(frame, a_ruleset(first, second))
    by_id = {k["id"]: k for k in stats["keys"]}
    assert by_id["k1"]["eligible_records"] == 2
    assert by_id["k2"]["eligible_records"] == 0


# ---------------------------------------------------------------------------
# Guards still apply on top of a condition
# ---------------------------------------------------------------------------


def test_a_conditional_key_still_holds_a_group_over_its_size_guard():
    rows = [{"record_id": str(i), "name_core": "UNISON", "donor_status_std": "Trade Union"}
            for i in range(5)]
    key = a_key(when=UNION_WHEN, guards={"max_group_size": 3}, on_guard_fail="review")
    groups, stats = keys.apply_match_keys(records(*rows), a_ruleset(key))
    assert set(groups["status"]) == {"held"}
    assert stats["keys"][0]["held_records"] == 5
    assert groups.iloc[0]["guard"] == "max_group_size:5>3"


def test_the_condition_runs_before_the_blocklist_and_the_null_check():
    frame = records(
        {"record_id": "1", "name_core": None, "donor_status_std": "Trade Union"},
        {"record_id": "2", "name_core": "UNISON", "donor_status_std": "Company"},
    )
    _, stats = keys.apply_match_keys(UNIONS, a_ruleset(a_key(when=UNION_WHEN)))
    assert stats["keys"][0]["excluded_by_condition"] == 2
    # A null key column and an excluded record are counted apart.
    _, stats = keys.apply_match_keys(frame, a_ruleset(a_key(when=UNION_WHEN)))
    assert stats["keys"][0]["excluded_by_condition"] == 1
    assert stats["keys"][0]["eligible_records"] == 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _errors(**key_overrides):
    ruleset = copy.deepcopy(default_ruleset())
    ruleset["match_keys"].append(a_key(id="kx", tier=9, **key_overrides))
    return engine.validate_ruleset(ruleset, RAW_COLUMNS)


def test_the_shipped_default_is_valid():
    assert engine.validate_ruleset(default_ruleset(), RAW_COLUMNS) == []


def test_a_good_condition_has_no_errors():
    assert _errors(when=UNION_WHEN) == []
    assert _errors(when=[{"column": "postcode", "op": "not_null"}]) == []


def test_a_condition_column_must_exist_for_the_key_s_track():
    errors = _errors(when=[{"column": "surname_metaphone", "op": "not_null"}])
    assert errors[0]["path"] == "match_keys[4].when[0].column"
    assert "not a column of the organisation track" in errors[0]["message"]


def test_a_condition_may_read_a_derived_target():
    assert _errors(when=[{"column": "donor_status_std", "op": "not_null"}]) == []


def test_an_unknown_operator_or_token_list_in_a_condition():
    errors = _errors(when=[{"column": "name_core", "op": "sounds_like", "value": "x"}])
    assert errors[0]["path"] == "match_keys[4].when[0].op"

    errors = _errors(when=[{"column": "name_core", "op": "contains_token",
                            "lists": ["nope"]}])
    assert errors[0]["path"] == "match_keys[4].when[0].lists"


def test_a_broken_regex_in_a_condition():
    errors = _errors(when=[{"column": "name_core", "op": "matches", "pattern": "(unclosed"}])
    assert errors[0]["path"] == "match_keys[4].when[0].pattern"


def test_when_must_be_a_list():
    errors = _errors(when={"column": "name_core"})
    assert errors[0] == {"path": "match_keys[4].when",
                         "message": "when must be a list of conditions"}


def test_the_path_names_the_condition_that_is_wrong():
    errors = _errors(when=[
        {"column": "name_core", "op": "not_null"},
        {"column": "nope", "op": "not_null"},
    ])
    assert errors[0]["path"] == "match_keys[4].when[1].column"


# ---------------------------------------------------------------------------
# The donations trade-union key
# ---------------------------------------------------------------------------


def test_the_trade_union_key_is_in_the_defaults():
    key = next(k for k in default_ruleset()["match_keys"] if k["id"] == "k4")
    assert key["track"] == "organisation"
    assert key["columns"] == ["name_core"]
    assert key["when"] == [
        {"column": "donor_status_std", "op": "equals", "value": "Trade Union"}
    ]
    assert key["guards"]["max_group_size"] == 200
    assert key["on_guard_fail"] == "review"
    assert key["allow_null"] is False
    # After the company-number key, so a union with a number is grouped on it first.
    company = next(k for k in default_ruleset()["match_keys"] if k["id"] == "k1")
    assert key["tier"] > company["tier"]


def test_the_trade_union_key_merges_unions_and_leaves_companies_alone():
    from app.pipeline.dedupe.stage_1_clean import clean_records

    frame = pd.DataFrame([
        {"record_id": "1", "name": "UNISON", "donor_status": "Trade Union"},
        {"record_id": "2", "name": "Unison", "donor_status": "Trade Union"},
        # Same name, not a union: nothing here says these two are one body.
        {"record_id": "3", "name": "Acme Holdings", "donor_status": "Unincorporated Association"},
        {"record_id": "4", "name": "Acme Holdings", "donor_status": "Unincorporated Association"},
    ])
    for column in RAW_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    cleaned = clean_records(frame[RAW_COLUMNS], default_ruleset())
    groups, stats = keys.apply_match_keys(cleaned, default_ruleset())

    k4 = next(k for k in stats["keys"] if k["id"] == "k4")
    assert k4["eligible_records"] == 2
    assert k4["excluded_by_condition"] == 2
    merged = groups[groups["status"] == "merged"]
    assert set(merged["record_id"]) == {"1", "2"}


# ---------------------------------------------------------------------------
# Evidence focus
# ---------------------------------------------------------------------------


def test_the_profile_serves_its_evidence_focus():
    focuses = get_profile().as_dict()["evidence_focus"]
    assert focuses
    first = focuses[0]
    assert set(first) == {"id", "label", "when", "record_columns", "event_columns"}
    assert first["id"] == "individual"
    assert focuses[-1]["when"] == []          # the catch-all comes last


def test_every_focus_uses_only_operators_a_browser_can_evaluate():
    """The review screen picks the focus itself, so the conditions must be
    trivial in JavaScript."""
    for focus in EVIDENCE_FOCUS:
        for condition in focus.when:
            assert condition["op"] in EVIDENCE_FOCUS_OPS


def test_every_focus_names_real_columns():
    profile = get_profile()
    record_keys = {c.key for c in profile.display_columns}
    event_keys = {c.key for c in profile.event_columns}
    for focus in EVIDENCE_FOCUS:
        assert set(focus.record_columns) <= record_keys, focus.id
        assert set(focus.event_columns) <= event_keys, focus.id


@pytest.mark.parametrize("status,expected", [
    ("Individual", "individual"),
    ("Public Fund", "public_fund"),
    ("Company", "incorporated"),
    ("Limited Liability Partnership", "incorporated"),
    ("Friendly Society", "incorporated"),
    ("Building Society", "incorporated"),
    ("Trade Union", "trade_union"),
    ("Unincorporated Association", "other"),
    ("Trust", "other"),
    ("Other", "other"),
    ("Impermissible Donor", "other"),
])
def test_the_focus_follows_the_standardised_status(status, expected):
    assert get_profile().evidence_focus_for({"donor_status_std": status}) == expected


def test_the_raw_status_is_the_fallback():
    """A unit from a run made before the derived column existed still gets the
    right focus."""
    profile = get_profile()
    assert profile.evidence_focus_for({"donor_status": "Trade Union"}) == "trade_union"
    # The standardised status wins when both are there.
    assert profile.evidence_focus_for(
        {"donor_status_std": "Company", "donor_status": "Trade Union"}
    ) == "incorporated"


def test_a_row_with_nothing_to_go_on_gets_the_catch_all():
    profile = get_profile()
    assert profile.evidence_focus_for({}) == "other"
    assert profile.evidence_focus_for({"donor_status_std": None}) == "other"
    assert profile.evidence_focus_for({"donor_status_std": "  "}) == "other"


def test_the_focus_says_what_a_reviewer_checks():
    by_id = {}
    for focus in EVIDENCE_FOCUS:
        by_id.setdefault(focus.id, focus)

    assert by_id["individual"].event_columns == [
        "recipient", "unit", "value", "date", "donation_type"]
    assert by_id["public_fund"].event_columns == ["nature", "value", "date"]
    assert by_id["incorporated"].record_columns == ["company_number", "postcode", "name"]
    # A union needs no donation history at all.
    assert by_id["trade_union"].record_columns == ["donor_status", "name"]
    assert by_id["trade_union"].event_columns == []
    assert by_id["other"].record_columns == ["postcode", "name"]


def test_the_focus_reads_a_pandas_row_as_well_as_a_dict():
    row = pd.Series({"donor_status_std": "Trade Union", "name": "UNISON"})
    assert get_profile().evidence_focus_for(row) == "trade_union"


def test_the_helper_works_on_any_list_of_focuses():
    focuses = [
        EvidenceFocus("oc", "OC", [{"column": "n", "op": "starts_with", "values": ["OC"]}],
                      ["n"], []),
        EvidenceFocus("rest", "Rest", [], ["n"], []),
    ]
    assert evidence_focus_id(focuses, {"n": "oc314414"}) == "oc"
    assert evidence_focus_id(focuses, {"n": "04250076"}) == "rest"
    assert evidence_focus_id([], {"n": "x"}) is None


def test_an_operator_a_browser_cannot_evaluate_is_refused():
    focuses = [EvidenceFocus("x", "X", [{"column": "n", "op": "matches",
                                         "pattern": "^a"}], [], [])]
    with pytest.raises(ValueError, match="may not use 'matches'"):
        evidence_focus_id(focuses, {"n": "abc"})


def test_a_profile_with_no_focus_returns_none():
    from app.profiles.base import Profile

    profile = Profile(key="bare")
    assert profile.as_dict()["evidence_focus"] == []
    assert profile.evidence_focus_for({"anything": "x"}) is None

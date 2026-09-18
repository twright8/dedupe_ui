"""Vetoes: the operators, the precedence order, and where they are applied.

The contract is the "Vetoes" section of ``docs/RULESET.md``. A veto is a rule
about a pair that stops the scorer accepting something a person never would, and
the order it sits in — score or model, then veto, then imported label, then
human label — is the whole point of it.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.pipeline.dedupe import label_overlay, score_eval
from app.pipeline.dedupe.stage_3_score import apply_overlays, counts_from, finalise_pairs
from app.pipeline.dedupe.stage_4_cluster import accepted_edges
from app.rules import engine, linkage, vetoes
from tests.rulesets import small_ruleset


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def units_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column, default in (
        ("track", "person"), ("name", None), ("existing_entity_id", None),
        ("held_group_id", None), ("unit_size", 1),
    ):
        if column not in frame.columns:
            frame[column] = default
    frame["unit_id"] = frame["unit_id"].astype(str)
    return frame


def pairs_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column, default in (("track", "person"), ("match_weight", 0.0)):
        if column not in frame.columns:
            frame[column] = default
    frame["unit_id_l"] = frame["unit_id_l"].astype(str)
    frame["unit_id_r"] = frame["unit_id_r"].astype(str)
    return frame


def veto(veto_id="v1", track="person", action="reject", reason=None, **when):
    return {"id": veto_id, "track": track, "description": "",
            "when": [when], "action": action, "reason": reason}


def ruleset_with(*vetoes_, token_lists=None):
    rules = {"vetoes": list(vetoes_)}
    if token_lists:
        rules["token_lists"] = token_lists
    return rules


def mask_for(condition: dict, left, right, ruleset=None) -> list[bool]:
    """One condition's answer over two aligned columns of values."""
    return list(vetoes.condition_mask(
        condition,
        pd.Series(left, dtype="object"),
        pd.Series(right, dtype="object"),
        ruleset or {},
    ))


# ---------------------------------------------------------------------------
# The operators
# ---------------------------------------------------------------------------


def test_differs_needs_both_sides_and_ignores_case_and_spacing():
    assert mask_for(
        {"column": "c", "op": "differs"},
        ["ANN", "ann", " ANN ", "ANN", None, None, ""],
        ["BOB", "ANN", "ANN", None, "BOB", None, "BOB"],
    ) == [True, False, False, False, False, False, False]


def test_abs_diff_gt_reads_numbers_out_of_text():
    # dob_year_clean is a string of digits out of the cleaning engine.
    condition = {"column": "c", "op": "abs_diff_gt", "value": 1}
    assert mask_for(
        condition,
        ["1958", "1980", "1980", "1980", None, "not a year", "1980"],
        ["1995", "1981", "1982", "1980", "1980", "1980", None],
    ) == [True, False, True, False, False, False, False]


def test_abs_diff_gte_includes_the_limit_itself():
    assert mask_for({"column": "c", "op": "abs_diff_gte", "value": 2},
                    ["1980", "1980"], ["1982", "1981"]) == [True, False]


def test_abs_diff_reads_real_numbers_as_well_as_text():
    assert vetoes.condition_mask(
        {"column": "c", "op": "abs_diff_gt", "value": 1},
        pd.Series([1958.0, 1980.0, np.nan]),
        pd.Series([1995.0, 1980.0, 1980.0]),
        {},
    ).tolist() == [True, False, False]


def test_similarity_lt_is_jaro_winkler_and_null_is_false():
    condition = {"column": "c", "op": "similarity_lt", "value": 0.7}
    assert mask_for(condition,
                    ["SABELO", "SARAH", "JOHN", None],
                    ["JANE", "SARA", "JOHN", "JANE"]) == [True, False, False, False]


def test_both_in_and_differ_needs_both_values_on_the_list():
    lists = {"gendered": {"description": "", "tokens": ["MR", "MRS", "MISS"]}}
    condition = {"column": "c", "op": "both_in_and_differ", "lists": ["gendered"]}
    assert mask_for(condition,
                    ["MR", "MR", "DR", "MR", None, "mrs"],
                    ["MRS", "MR", "MRS", "DR", "MRS", "MR"],
                    {"token_lists": lists}) == [True, False, False, False, False, True]


def test_no_overlap_reads_the_bar_joined_sets():
    condition = {"column": "c", "op": "no_overlap"}
    assert mask_for(condition,
                    ["A | B", "A | B", "A", None, "A | B"],
                    ["C | D", "B | C", "A", "A", None]) == [True, False, False,
                                                            False, False]


def test_an_unknown_operator_is_refused_rather_than_ignored():
    with pytest.raises(vetoes.VetoError):
        mask_for({"column": "c", "op": "sounds_wrong"}, ["A"], ["B"])


def test_an_unknown_token_list_is_refused():
    with pytest.raises(vetoes.VetoError):
        mask_for({"column": "c", "op": "both_in_and_differ", "lists": ["nope"]},
                 ["MR"], ["MRS"], {"token_lists": {}})


def test_every_condition_in_a_veto_must_hold():
    """The PSC sibling veto: the forenames differ AND are not near-spellings."""
    units = units_frame([
        {"unit_id": "1", "forename_canon": "SABELO"},
        {"unit_id": "2", "forename_canon": "JANE"},
        {"unit_id": "3", "forename_canon": "SARAH"},
        {"unit_id": "4", "forename_canon": "SARA"},
    ])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 1.0},
        {"unit_id_l": "3", "unit_id_r": "4", "match_probability": 1.0},
    ])
    rules = ruleset_with({
        "id": "v2", "track": "person", "action": "review",
        "when": [{"column": "forename_canon", "op": "differs"},
                 {"column": "forename_canon", "op": "similarity_lt", "value": 0.7}],
        "reason": "Forenames {left} and {right}",
    })
    out = vetoes.apply_to_buckets(pairs, units, rules,
                                  np.array(["accept", "accept"], dtype=object))
    assert list(out["bucket"]) == ["review", "accept"]
    assert list(out["vetoed_by"]) == ["v2", None]
    assert out["veto_reason"][0] == "Forenames SABELO and JANE"


def test_a_veto_only_touches_its_own_track():
    units = units_frame([
        {"unit_id": "1", "c": "A", "track": "person"},
        {"unit_id": "2", "c": "B", "track": "person"},
        {"unit_id": "3", "c": "A", "track": "organisation"},
        {"unit_id": "4", "c": "B", "track": "organisation"},
    ])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 1.0,
         "track": "person"},
        {"unit_id_l": "3", "unit_id_r": "4", "match_probability": 1.0,
         "track": "organisation"},
    ])
    rules = ruleset_with(veto(track="organisation", column="c", op="differs"))
    out = vetoes.apply_to_buckets(pairs, units, rules,
                                  np.array(["accept", "accept"], dtype=object))
    assert list(out["bucket"]) == ["accept", "reject"]


def test_a_column_the_units_do_not_carry_vetoes_nobody():
    units = units_frame([{"unit_id": "1"}, {"unit_id": "2"}])
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 1.0}])
    rules = ruleset_with(veto(column="not_a_column", op="differs"))
    out = vetoes.apply_to_buckets(pairs, units, rules,
                                  np.array(["accept"], dtype=object))
    assert list(out["bucket"]) == ["accept"]


def test_the_strongest_action_wins_when_two_vetoes_hit():
    units = units_frame([
        {"unit_id": "1", "a": "X", "b": "1980"},
        {"unit_id": "2", "a": "Y", "b": "1995"},
    ])
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 1.0}])
    rules = ruleset_with(
        veto("soft", action="review", column="a", op="differs"),
        veto("hard", action="reject", column="b", op="abs_diff_gt", value=1),
    )
    out = vetoes.apply_to_buckets(pairs, units, rules,
                                  np.array(["accept"], dtype=object))
    assert list(out["bucket"]) == ["reject"]
    assert list(out["vetoed_by"]) == ["hard"]


def test_a_review_veto_caps_an_accept_and_leaves_a_reject_alone():
    units = units_frame([
        {"unit_id": "1", "a": "X"}, {"unit_id": "2", "a": "Y"},
        {"unit_id": "3", "a": "Z"},
    ])
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.99},
        {"unit_id_l": "1", "unit_id_r": "3", "match_probability": 0.02},
    ])
    rules = ruleset_with(veto(action="review", column="a", op="differs"))
    out = vetoes.apply_to_buckets(pairs, units, rules,
                                  np.array(["accept", "reject"], dtype=object))
    assert list(out["bucket"]) == ["review", "reject"]


# ---------------------------------------------------------------------------
# Precedence: score or model, then veto, then import, then human
# ---------------------------------------------------------------------------


PRECEDENCE_UNITS = units_frame([
    {"unit_id": "1", "dob_year_clean": "1958", "existing_entity_id": "E1"},
    {"unit_id": "2", "dob_year_clean": "1995", "existing_entity_id": "E1"},
    {"unit_id": "3", "dob_year_clean": "1995", "existing_entity_id": None},
])

YEAR_VETO = ruleset_with(veto(column="dob_year_clean", op="abs_diff_gt", value=1,
                              reason="Born {left} and {right}"))


def test_a_veto_beats_the_score():
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "3",
                          "match_probability": 1.0}])
    out = apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO)
    assert out["score_bucket"][0] == "accept"      # the pre-overlay bucket stays
    assert out["bucket"][0] == "reject"
    assert out["decided_by"][0] == "veto"
    assert out["vetoed_by"][0] == "v1"
    assert out["veto_reason"][0] == "Born 1958 and 1995"


def test_an_imported_label_beats_a_veto_and_the_clash_is_flagged():
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 1.0}])
    out = apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO)
    assert out["bucket"][0] == "accept"
    assert out["decided_by"][0] == "import"
    assert out["vetoed_by"][0] == "v1"
    assert bool(out["veto_conflicts_import"][0]) is True


def test_a_human_label_beats_the_import_that_beat_the_veto():
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                          "match_probability": 1.0}])
    out = apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO)
    applied = pd.DataFrame({"unit_id_l": ["1"], "unit_id_r": ["2"],
                            "is_match": ["FALSE"]})
    overlaid = label_overlay.apply_to_pairs(out, applied)
    assert overlaid["bucket"][0] == "reject"
    assert overlaid["decided_by"][0] == "human"


def test_a_veto_beats_a_graded_model():
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "3",
                          "match_probability": 0.10, "gbt_score": 0.99}])
    out = apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92,
                         model_lines={"person": (0.4, 0.8)}, ruleset=YEAR_VETO)
    assert out["score_bucket"][0] == "accept"      # the model accepted it
    assert out["bucket"][0] == "reject"
    assert out["decided_by"][0] == "veto"


def test_a_run_with_no_vetoes_carries_the_columns_and_nothing_else_moves():
    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "3",
                          "match_probability": 1.0}])
    out = apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92, ruleset={})
    assert out["bucket"][0] == "accept"
    assert out["decided_by"][0] == "score"
    assert out["vetoed_by"][0] is None
    assert bool(out["veto_conflicts_import"][0]) is False


# ---------------------------------------------------------------------------
# Every path that moves a bucket applies them again
# ---------------------------------------------------------------------------


def test_stripping_the_overlays_takes_the_veto_columns_with_them():
    from app.pipeline.dedupe.stage_3_score import _strip_overlays

    pairs = pairs_frame([{"unit_id_l": "1", "unit_id_r": "3",
                          "match_probability": 1.0}])
    out = finalise_pairs(
        apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO),
        PRECEDENCE_UNITS,
    )
    stripped = _strip_overlays(out)
    for column in vetoes.VETO_COLUMNS:
        assert column not in stripped.columns

    # And re-applying them puts the same answer back.
    again = apply_overlays(stripped, PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO)
    assert again["bucket"][0] == "reject"
    assert again["vetoed_by"][0] == "v1"


def test_re_bucketing_a_run_applies_the_vetoes_again(tmp_path):
    from app.pipeline.dedupe.stage_3_score import rebucket

    run_dir = tmp_path / "run"
    (run_dir / "config").mkdir(parents=True)
    records = pd.DataFrame({"record_id": ["1", "3"], "track": ["person"] * 2,
                            "name": ["A", "B"], "existing_entity_id": [None, None]})
    members = pd.DataFrame({"unit_id": ["1", "3"], "record_id": ["1", "3"]})
    groups = pd.DataFrame(columns=["record_id", "group_id", "track", "status",
                                   "key_ids", "guard"])
    pairs = finalise_pairs(
        apply_overlays(pairs_frame([{"unit_id_l": "1", "unit_id_r": "3",
                                     "match_probability": 1.0}]),
                       PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO),
        PRECEDENCE_UNITS,
    )
    PRECEDENCE_UNITS.to_parquet(run_dir / "units.parquet", index=False)
    members.to_parquet(run_dir / "unit_members.parquet", index=False)
    records.to_parquet(run_dir / "records.parquet", index=False)
    groups.to_parquet(run_dir / "exact_groups.parquet", index=False)
    pairs.to_parquet(run_dir / "pairs.parquet", index=False)
    ruleset = {**small_ruleset(), **YEAR_VETO}
    (run_dir / "config" / "ruleset.json").write_text(json.dumps(ruleset),
                                                     encoding="utf-8")
    (run_dir / "config" / "linkage_settings.json").write_text("{}", encoding="utf-8")

    counts = rebucket(str(run_dir), threshold_high=0.5, threshold_review=0.2)
    after = pd.read_parquet(run_dir / "pairs.parquet")
    assert after["bucket"][0] == "reject"
    assert after["vetoed_by"][0] == "v1"
    assert counts["pairs_vetoed"] == 1
    assert counts["pairs_vetoed_from_accept"] == 1


def test_a_vetoed_pair_is_never_a_cluster_edge():
    pairs = finalise_pairs(
        apply_overlays(pairs_frame([{"unit_id_l": "1", "unit_id_r": "3",
                                     "match_probability": 1.0}]),
                       PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO),
        PRECEDENCE_UNITS,
    )
    assert len(accepted_edges(pairs, None)) == 0


def test_an_imported_accept_a_veto_clashes_with_is_still_an_edge():
    """A veto never overrides an earlier real grouping (LINKAGE.md, D11)."""
    pairs = finalise_pairs(
        apply_overlays(pairs_frame([{"unit_id_l": "1", "unit_id_r": "2",
                                     "match_probability": 1.0}]),
                       PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO),
        PRECEDENCE_UNITS,
    )
    edges = accepted_edges(pairs, None)
    assert list(edges["source"]) == ["import"]


# ---------------------------------------------------------------------------
# Counts and the evaluation
# ---------------------------------------------------------------------------


def _three_pair_run():
    pairs = pairs_frame([
        {"unit_id_l": "1", "unit_id_r": "3", "match_probability": 1.0},
        {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 1.0},
        {"unit_id_l": "2", "unit_id_r": "3", "match_probability": 0.10},
    ])
    return finalise_pairs(
        apply_overlays(pairs, PRECEDENCE_UNITS, 0.5, 0.92, ruleset=YEAR_VETO),
        PRECEDENCE_UNITS,
    )


def test_the_three_run_counts_add_up():
    counts = vetoes.counts_from(_three_pair_run())
    # 1|3 vetoed from an accept; 1|2 vetoed but the import overlay accepted it;
    # 2|3 agrees on the year, so no veto.
    assert counts == {"pairs_vetoed": 2, "pairs_vetoed_from_accept": 2,
                      "veto_conflicts_import": 1}


def test_the_evaluation_reports_the_run_with_and_without_its_vetoes():
    pairs = _three_pair_run()
    records = pd.DataFrame({"record_id": ["1", "2", "3"], "track": ["person"] * 3,
                            "existing_entity_id": ["E1", "E1", None]})
    members = pd.DataFrame({"unit_id": ["1", "2", "3"],
                            "record_id": ["1", "2", "3"]})
    groups = pd.DataFrame(columns=["record_id", "group_id", "track", "status",
                                   "key_ids", "guard"])
    evaluation = score_eval.evaluate(records, groups, PRECEDENCE_UNITS, members,
                                     pairs, thresholds={"high": 0.92})
    # With the veto, only the imported accept joins anything: two entities.
    assert evaluation["entities_after"] == 2
    # Without it, 1|3 joins as well and everything is one entity.
    assert evaluation["without_vetoes"]["entities_after"] == 1
    assert evaluation["vetoes"]["vetoed"] == 2
    assert evaluation["vetoes"]["by_veto"]["v1"] == {"pairs": 2, "from_accept": 2}
    # score_only follows the vetoes; the set beside it does not.
    assert evaluation["score_only"]["entities_after"] == 3
    assert evaluation["without_vetoes"]["score_only"]["entities_after"] == 1


def test_counts_from_carries_the_veto_keys_into_the_run():
    pairs = _three_pair_run()
    counts = counts_from(PRECEDENCE_UNITS, pairs,
                         {"entities_after": 2, "pair_precision": None,
                          "pair_recall": None, "with_human": {}})
    assert counts["pairs_vetoed"] == 2
    assert counts["pairs_vetoed_from_accept"] == 2
    assert counts["veto_conflicts_import"] == 1


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------


def test_the_preview_counts_hits_and_leads_with_the_ones_it_would_stop():
    pairs = _three_pair_run()
    report = vetoes.report(pairs, PRECEDENCE_UNITS, YEAR_VETO)
    assert len(report) == 1
    entry = report[0]
    assert entry["id"] == "v1"
    assert entry["action"] == "reject"
    assert entry["pairs_hit"] == 2
    # Both hits would otherwise have been accepted: one by the score, one by the
    # import overlay.
    assert entry["accepted_pairs_hit"] == 2
    assert {e["pair_id"] for e in entry["examples"]} == {"1|3", "1|2"}
    example = next(e for e in entry["examples"] if e["pair_id"] == "1|3")
    assert (example["left_value"], example["right_value"]) == ("1958", "1995")
    assert example["reason"] == "Born 1958 and 1995"
    assert example["score"] == 1.0


def test_a_veto_nothing_hits_reports_zero_rather_than_disappearing():
    pairs = _three_pair_run()
    rules = ruleset_with(veto(column="dob_year_clean", op="abs_diff_gt", value=99))
    report = vetoes.report(pairs, PRECEDENCE_UNITS, rules)
    assert report[0]["pairs_hit"] == 0
    assert report[0]["accepted_pairs_hit"] == 0
    assert report[0]["examples"] == []


def test_the_preview_reads_only_the_columns_the_vetoes_name():
    assert vetoes.columns_needed(YEAR_VETO) == ["dob_year_clean"]
    assert vetoes.columns_needed(YEAR_VETO, track="organisation") == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


RAW_COLUMNS = ["record_id", "name", "donor_status"]


def _errors(*vetoes_) -> list[dict]:
    ruleset = {**small_ruleset(), "vetoes": list(vetoes_)}
    return engine.validate_ruleset(ruleset, RAW_COLUMNS)


def _paths(errors) -> list[str]:
    return [e["path"] for e in errors]


def test_a_good_veto_validates():
    assert _errors(veto(column="name_clean", op="differs")) == []


def test_a_veto_on_a_column_the_track_does_not_have_is_refused():
    errors = _errors(veto(column="nope", op="differs"))
    assert _paths(errors) == ["vetoes[0].when[0].column"]
    assert "person track" in errors[0]["message"]


def test_a_veto_needs_a_track_an_action_an_id_and_a_condition():
    errors = _errors({"id": None, "track": "people", "action": "accept",
                      "when": []})
    assert set(_paths(errors)) == {"vetoes[0].id", "vetoes[0].track",
                                   "vetoes[0].action", "vetoes[0].when"}


def test_duplicate_veto_ids_are_refused():
    errors = _errors(veto("v1", column="name_clean", op="differs"),
                     veto("v1", column="name_clean", op="differs"))
    assert _paths(errors) == ["vetoes[1].id"]


def test_an_unknown_operator_names_its_own_path():
    errors = _errors(veto(column="name_clean", op="is_roughly"))
    assert _paths(errors) == ["vetoes[0].when[0].op"]


@pytest.mark.parametrize("condition,path", [
    ({"column": "name_clean", "op": "abs_diff_gt"}, "vetoes[0].when[0].value"),
    ({"column": "name_clean", "op": "abs_diff_gt", "value": "two"},
     "vetoes[0].when[0].value"),
    ({"column": "name_clean", "op": "abs_diff_gt", "value": -1},
     "vetoes[0].when[0].value"),
    ({"column": "name_clean", "op": "similarity_lt", "value": 1.4},
     "vetoes[0].when[0].value"),
    ({"column": "name_clean", "op": "both_in_and_differ"},
     "vetoes[0].when[0].lists"),
    ({"column": "name_clean", "op": "both_in_and_differ", "lists": ["nope"]},
     "vetoes[0].when[0].lists"),
])
def test_each_operator_argument_is_checked(condition, path):
    errors = _errors({"id": "v1", "track": "person", "action": "reject",
                      "when": [condition]})
    assert _paths(errors) == [path]


def test_a_token_list_the_ruleset_has_is_accepted():
    assert _errors({"id": "v1", "track": "person", "action": "reject",
                    "when": [{"column": "name_clean", "op": "both_in_and_differ",
                              "lists": ["titles"]}]}) == []


def test_vetoes_must_be_a_list():
    ruleset = {**small_ruleset(), "vetoes": {"v1": {}}}
    errors = engine.validate_ruleset(ruleset, RAW_COLUMNS)
    assert _paths(errors) == ["vetoes"]


# ---------------------------------------------------------------------------
# custom.NumericDifferenceAtThresholds
# ---------------------------------------------------------------------------


def numeric_spec(thresholds=(0, 1), column="dob_year_clean", **extra):
    return {"id": "c1", "column": column, "term_frequency": False,
            "splink_function": linkage.NUMERIC_DIFFERENCE,
            "splink_args": {"thresholds": list(thresholds)}, **extra}


def test_the_numeric_comparison_makes_a_null_level_a_level_per_threshold_and_an_else():
    built = linkage.build_comparison(numeric_spec([0, 1, 2]))
    levels = built.get_comparison("duckdb").as_dict()["comparison_levels"]
    assert [lv["label_for_charts"] for lv in levels] == [
        "dob_year_clean is NULL",
        "Equal dob_year_clean",
        "dob_year_clean within 1",
        "dob_year_clean within 2",
        "All other",
    ]
    assert levels[0]["is_null_level"] is True
    assert levels[1]["sql_condition"] == \
        'ABS("dob_year_clean_l" - "dob_year_clean_r") <= 0'
    assert levels[-1]["sql_condition"] == "ELSE"


def test_the_explanation_renders_those_levels_in_plain_words(tmp_path):
    from app.services import pairs_reader

    model = {"comparisons": [
        linkage.build_comparison(numeric_spec([0, 1])).get_comparison("duckdb").as_dict()
    ]}
    for comparison in model["comparisons"]:
        for index, level in enumerate(comparison["comparison_levels"]):
            level["m_probability"] = 0.5
            level["u_probability"] = 0.25 if index else None
    (tmp_path / "splink_model_person.json").write_text(json.dumps(model),
                                                       encoding="utf-8")
    levels = pairs_reader.comparison_levels(str(tmp_path), "person")
    assert levels["dob_year_clean"][2]["label"] == "Equal dob_year_clean"
    assert levels["dob_year_clean"][1]["label"] == "dob_year_clean within 1"
    assert levels["dob_year_clean"][0]["label"] == "All other"
    assert levels["dob_year_clean"][-1]["label"] == "dob_year_clean is NULL"


def _linkage_errors(spec) -> list[str]:
    settings = {"tracks": {"person": {"blocking_rules": [], "comparisons": [spec],
                                      "max_pairs": 100}}}
    errors = linkage.validate_linkage_settings(settings, small_ruleset(), RAW_COLUMNS)
    return [e["path"] for e in errors]


def test_the_numeric_comparison_needs_ascending_non_negative_thresholds():
    assert _linkage_errors(numeric_spec([0, 1], column="name_clean")) == []
    assert _linkage_errors(numeric_spec([], column="name_clean")) == \
        ["linkage_settings.tracks.person.comparisons[0].splink_args.thresholds"]
    assert _linkage_errors(numeric_spec([1, 0], column="name_clean")) == \
        ["linkage_settings.tracks.person.comparisons[0].splink_args.thresholds[1]"]
    assert _linkage_errors(numeric_spec([-1], column="name_clean")) == \
        ["linkage_settings.tracks.person.comparisons[0].splink_args.thresholds[0]"]
    assert _linkage_errors(numeric_spec(["a"], column="name_clean")) == \
        ["linkage_settings.tracks.person.comparisons[0].splink_args.thresholds[0]"]


def test_the_numeric_comparison_refuses_a_term_frequency_adjustment():
    assert _linkage_errors(numeric_spec([0, 1], column="name_clean", term_frequency=True)) == \
        ["linkage_settings.tracks.person.comparisons[0].term_frequency"]


def test_the_scoring_frame_casts_the_numeric_columns():
    from app.pipeline.dedupe.stage_3_score import _splink_frame

    config = {"blocking_rules": [], "comparisons": [numeric_spec()]}
    rows = pd.DataFrame({"unit_id": ["1", "2", "3"],
                         "dob_year_clean": ["1980", None, "not a year"]})
    frame = _splink_frame(rows, config)
    assert frame["dob_year_clean"].tolist()[0] == 1980.0
    assert frame["dob_year_clean"].isna().tolist() == [False, True, True]


# ---------------------------------------------------------------------------
# What the two profiles ship
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["donations", "psc"])
def test_the_shipped_defaults_validate_clean_with_no_warnings(profile):
    from pathlib import Path

    from app.profiles import get_profile

    raw = list(get_profile(profile).raw_columns)
    base = Path(__file__).parent.parent / "app" / "profiles" / "defaults" / profile
    ruleset = json.loads((base / "ruleset.json").read_text(encoding="utf-8"))
    settings = json.loads((base / "linkage_settings.json").read_text(encoding="utf-8"))
    assert engine.validate_ruleset(ruleset, raw) == []
    assert linkage.validate_linkage_settings(settings, ruleset, raw) == []
    assert linkage.linkage_warnings(settings, ruleset) == []
    assert ruleset["vetoes"], "both profiles ship vetoes now"


# ---------------------------------------------------------------------------
# POST /api/config/preview-vetoes
# ---------------------------------------------------------------------------


PREVIEW_RUN = "run_veto_preview"


@pytest.fixture
def preview_client(db_path, tmp_path, monkeypatch):
    import app.auth as _auth_mod
    import app.main as _main_mod
    from app.db import write_db
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    run_dir = data_dir / "runs" / PREVIEW_RUN
    run_dir.mkdir(parents=True)

    units = units_frame([
        {"unit_id": "1", "name": "Acme Ltd", "company_number_clean": "00000001",
         "track": "organisation"},
        {"unit_id": "2", "name": "Acme Limited", "company_number_clean": "00000002",
         "track": "organisation"},
        {"unit_id": "3", "name": "Acme Holdings", "company_number_clean": None,
         "track": "organisation"},
    ])
    pairs = finalise_pairs(
        apply_overlays(pairs_frame([
            {"unit_id_l": "1", "unit_id_r": "2", "match_probability": 0.99,
             "track": "organisation"},
            {"unit_id_l": "1", "unit_id_r": "3", "match_probability": 0.10,
             "track": "organisation"},
        ]), units, 0.5, 0.92, ruleset={}),
        units,
    )
    units.to_parquet(run_dir / "units.parquet", index=False)
    pairs.to_parquet(run_dir / "pairs.parquet", index=False)

    monkeypatch.setattr(_main_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        _auth_mod, "_unsign", lambda token, max_age=None: {"authenticated": True}
    )
    write_db(db_path, "INSERT INTO runs (id, status) VALUES (?, ?)",
             (PREVIEW_RUN, "complete"))
    return TestClient(_main_mod.app, cookies={"session": "fake"})


def _draft_with(*vetoes_):
    from tests.rulesets import default_ruleset

    return {**default_ruleset(), "vetoes": list(vetoes_)}


def test_the_preview_endpoint_reports_hits_and_the_accepted_ones(preview_client):
    draft = _draft_with(veto("dv2", track="organisation", action="review",
                             reason="Company numbers {left} and {right}",
                             column="company_number_clean", op="differs"))
    response = preview_client.post("/api/config/preview-vetoes",
                                   json={"ruleset": draft, "run_id": PREVIEW_RUN})
    assert response.status_code == 200
    body = response.json()
    assert body["pairs_total"] == 2
    assert len(body["vetoes"]) == 1
    entry = body["vetoes"][0]
    assert (entry["id"], entry["action"]) == ("dv2", "review")
    assert entry["pairs_hit"] == 1
    assert entry["accepted_pairs_hit"] == 1
    assert entry["examples"][0]["pair_id"] == "1|2"
    assert entry["examples"][0]["left_name"] == "Acme Ltd"
    assert entry["examples"][0]["reason"] == "Company numbers 00000001 and 00000002"


def test_the_preview_endpoint_refuses_an_invalid_draft(preview_client):
    draft = _draft_with({"id": "bad", "track": "person", "action": "accept",
                         "when": [{"column": "nope", "op": "differs"}]})
    response = preview_client.post("/api/config/preview-vetoes",
                                   json={"ruleset": draft, "run_id": PREVIEW_RUN})
    assert response.status_code == 422
    paths = [e["path"] for e in response.json()["detail"]["errors"]]
    assert "vetoes[0].action" in paths and "vetoes[0].when[0].column" in paths


def test_the_preview_endpoint_404s_on_a_run_with_no_pairs(preview_client):
    response = preview_client.post("/api/config/preview-vetoes",
                                   json={"ruleset": _draft_with(), "run_id": "nope"})
    assert response.status_code == 404


def test_the_preview_endpoint_refuses_a_run_over_the_pair_cap(preview_client,
                                                              monkeypatch):
    import app.routers.config as config_router

    monkeypatch.setattr(config_router, "PREVIEW_MAX_RECORDS", 1)
    response = preview_client.post("/api/config/preview-vetoes",
                                   json={"ruleset": _draft_with(), "run_id": PREVIEW_RUN})
    assert response.status_code == 400
    assert "scored pairs" in response.json()["detail"]


def test_a_reason_trims_a_float_year_but_never_a_padded_number():
    assert vetoes._render("1958.0") == "1958"
    assert vetoes._render(1958.0) == "1958"
    assert vetoes._render("00000001") == "00000001"
    assert vetoes._render("SC570493") == "SC570493"
    assert vetoes._render("0.5") == "0.5"
    assert vetoes._render(None) == ""

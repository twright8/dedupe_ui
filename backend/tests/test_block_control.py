# backend/tests/test_block_control.py
"""The hot-key blocking control: `max_block_size`, `on_oversize`, `refine_with`,
`drop_above`.

A blocking rule puts units into blocks and a block of n units makes n(n-1)/2
pairs, so a few very common keys carry most of the workload. The control refines
an oversized block by also requiring extra columns to agree, and drops one that
is still too big.

Two things are checked everywhere here. First, the control is generated SQL, so
the pairs it removes are never made: the tests count what the generated SQL
produces, not what a filter over pairs would leave. Second, a rule carrying none
of the four keys behaves exactly as it did before the control existed. That is
the donations no-op guarantee and it is asserted directly.
"""

import itertools
import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.profiles.donations import RAW_COLUMNS
from app import vocabulary
from app.rules import linkage
from tests.rulesets import default_linkage_settings, default_ruleset

DEFAULTS = Path(__file__).resolve().parent.parent / "app" / "profiles" / "defaults"


@pytest.fixture
def con():
    connection = duckdb.connect()
    connection.execute("set memory_limit='1GB'")
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# A fixture with one hot key and several ordinary ones
# ---------------------------------------------------------------------------


def fixture_units() -> pd.DataFrame:
    """Units whose `surname` blocks are 30, 8, 3, 2 and 1 unit big.

    The block of 30 is the hot key. Inside it the `forename_initial` values are
    deliberately lopsided — 18 A, 9 B, 3 C — so refining it leaves one sub-block
    over 10 and two under, which is what `drop_above` has to act on.
    """
    rows = []
    initials = ["A"] * 18 + ["B"] * 9 + ["C"] * 3
    for index, initial in enumerate(initials):
        rows.append({"surname": "SMITH", "forename_initial": initial,
                     "postcode": f"P{index % 4}"})
    for index in range(8):
        rows.append({"surname": "JONES", "forename_initial": "ABCDEFGH"[index],
                     "postcode": "P0"})
    for index in range(3):
        rows.append({"surname": "PATEL", "forename_initial": "A",
                     "postcode": "P1"})
    for index in range(2):
        rows.append({"surname": "OKAFOR", "forename_initial": "B",
                     "postcode": "P2"})
    rows.append({"surname": "SOLO", "forename_initial": "Z", "postcode": "P3"})
    # A hot block whose refine column is null: `l.f = r.f` is false for a null,
    # so those units make no pair once the block is refined.
    for index in range(12):
        rows.append({"surname": "NONAME", "forename_initial": None,
                     "postcode": "P0"})
    # A null key never blocks; two of them must not become a block of their own.
    rows.append({"surname": None, "forename_initial": "Q", "postcode": "P0"})
    rows.append({"surname": None, "forename_initial": "Q", "postcode": "P0"})
    frame = pd.DataFrame(rows)
    frame.insert(0, "unit_id", [str(i) for i in range(len(frame))])
    frame["track"] = "person"
    return frame


def brute_force_pairs(frame: pd.DataFrame, key: str, control: dict | None) -> int:
    """Every pair the rule `l.<key> = r.<key>` makes, counted one pair at a time.

    Plain Python, no SQL and no group arithmetic, so it agrees with the counting
    function only if the counting function is right.
    """
    rows = frame.to_dict("records")
    sizes = {}
    for row in rows:
        if row[key] is not None and not pd.isna(row[key]):
            sizes[row[key]] = sizes.get(row[key], 0) + 1
    refined_sizes = {}
    if control and control["refine_with"]:
        for row in rows:
            if row[key] is None or pd.isna(row[key]):
                continue
            values = [row[c] for c in control["refine_with"]]
            if any(v is None or pd.isna(v) for v in values):
                continue
            refined_sizes[(row[key],) + tuple(values)] = \
                refined_sizes.get((row[key],) + tuple(values), 0) + 1

    total = 0
    for left, right in itertools.combinations(rows, 2):
        value = left[key]
        if value is None or pd.isna(value) or value != right[key]:
            continue
        if control:
            oversized = sizes[value] > control["max_block_size"]
            if oversized:
                if control["on_oversize"] == "drop":
                    continue
                values = [left[c] for c in control["refine_with"]]
                # SQL's `l.f = r.f` is never true for a null, on either side.
                if any(v is None or pd.isna(v) for v in values):
                    continue
                if any(left[c] != right[c] for c in control["refine_with"]):
                    continue
                if control["drop_above"] and \
                        refined_sizes[(value,) + tuple(values)] > control["drop_above"]:
                    continue
        total += 1
    return total


def pairs_from_sql(con, frame: pd.DataFrame, sql: str) -> int:
    """The pairs the generated SQL really makes, by running it as a self-join.

    Only the tests materialise pairs. The point of the control is that the
    pipeline never does.
    """
    con.register("_fixture", frame)
    return int(con.execute(
        f"select count(*) from _fixture l join _fixture r "
        f"on ({sql}) and l.unit_id < r.unit_id"
    ).fetchone()[0])


def rule(**extra) -> dict:
    base = {"id": "b1", "description": "same surname",
            "sql": "l.surname = r.surname"}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Reading a rule
# ---------------------------------------------------------------------------


def test_block_shape_separates_keys_filters_and_inequalities():
    shape = linkage.block_shape(
        "l.name_fingerprint = r.name_fingerprint AND l.name_fingerprint <> ''"
    )
    assert shape["keys"] == ["l.name_fingerprint"]
    assert shape["filters"] == ["l.name_fingerprint <> ''"]
    assert shape["not_equal"] == [] and shape["residual"] == []

    # An expression on both sides is a key too, not only a bare column.
    shape = linkage.block_shape(
        "substr(l.surname_clean, 1, 3) = substr(r.surname_clean, 1, 3) "
        "AND l.forename_initial = r.forename_initial"
    )
    assert shape["keys"] == ["substr(l.surname_clean, 1, 3)", "l.forename_initial"]

    shape = linkage.block_shape(
        "l.forename_metaphone = r.forename_metaphone "
        "AND l.surname_metaphone <> r.surname_metaphone"
    )
    assert shape["keys"] == ["l.forename_metaphone"]
    assert shape["not_equal"] == ["l.surname_metaphone"]

    # A condition counting cannot apply is kept whole and reported as residual.
    shape = linkage.block_shape(
        "l.a = r.a AND (l.dob_year_clean IS NULL OR r.dob_year_clean IS NULL)"
    )
    assert shape["keys"] == ["l.a"]
    assert shape["residual"] == ["(l.dob_year_clean IS NULL OR r.dob_year_clean IS NULL)"]

    # A top-level OR has no one block key, so nothing is taken apart.
    assert linkage.block_shape("l.a = r.a OR l.b = r.b")["keys"] == []


def test_the_control_defaults_refine_when_it_can_and_drop_when_it_cannot():
    assert linkage.block_control(rule(max_block_size=10,
                                      refine_with=["forename_initial"])) == {
        "max_block_size": 10, "on_oversize": "refine",
        "refine_with": ["forename_initial"], "drop_above": None,
    }
    assert linkage.block_control(rule(max_block_size=10))["on_oversize"] == "drop"
    # Asking to drop throws refine_with away rather than half-applying it.
    control = linkage.block_control(
        rule(max_block_size=10, on_oversize="drop",
             refine_with=["forename_initial"], drop_above=50)
    )
    assert control["on_oversize"] == "drop"
    assert control["refine_with"] == []
    assert control["drop_above"] is None


def test_a_rule_with_none_of_the_keys_has_no_control():
    assert linkage.block_control(rule()) is None
    assert linkage.block_control("l.surname = r.surname") is None


# ---------------------------------------------------------------------------
# The generated SQL
# ---------------------------------------------------------------------------


def test_refine_generates_sql_that_never_makes_the_pair(con):
    frame = fixture_units()
    sql = linkage.controlled_sql(
        con, frame, rule(max_block_size=10, refine_with=["forename_initial"]),
        "person",
    )
    # The rule's own SQL is still there, the hot key is named as a literal, and
    # the refinement is an OR beside it — one predicate, no pair filter.
    assert "l.surname = r.surname" in sql
    assert "'SMITH'" in sql
    assert "l.forename_initial = r.forename_initial" in sql
    assert "NOT IN" in sql
    # JONES has 8 units, inside the limit, so it is not named.
    assert "'JONES'" not in sql


def test_drop_generates_sql_that_removes_the_hot_key_entirely(con):
    frame = fixture_units()
    sql = linkage.controlled_sql(
        con, frame, rule(max_block_size=10, on_oversize="drop"), "person"
    )
    # NONAME has 12 units and is oversized too, so both hot keys are named.
    assert "NOT IN ('NONAME', 'SMITH')" in sql
    assert "forename_initial" not in sql


def test_refine_and_drop_together_name_both_sets(con):
    frame = fixture_units()
    sql = linkage.controlled_sql(
        con, frame,
        rule(max_block_size=10, refine_with=["forename_initial"], drop_above=10),
        "person",
    )
    # SMITH is oversized; refined by initial it splits 18 / 9 / 3, and only the
    # sub-block of 18 is still over 10, so exactly that one is named as dropped.
    assert "'SMITH'" in sql
    assert "SMITH\x1fA" in sql
    assert "SMITH\x1fB" not in sql


def test_a_rule_with_no_control_comes_back_byte_for_byte(con):
    # The donations no-op guarantee. Nothing about a rule that does not ask for
    # the control may change, including the SQL string itself.
    frame = fixture_units()
    plain = rule()
    assert linkage.controlled_sql(con, frame, plain, "person") == plain["sql"]
    assert linkage.controlled_sql(con, frame, "l.surname = r.surname", "person") \
        == "l.surname = r.surname"
    assert linkage.control_description(plain) == ""


def test_a_control_no_block_trips_leaves_the_sql_alone(con):
    frame = fixture_units()
    sql = linkage.controlled_sql(
        con, frame, rule(max_block_size=1000, refine_with=["forename_initial"]),
        "person",
    )
    assert sql == "l.surname = r.surname"


def test_too_many_hot_keys_is_refused_rather_than_inlined(con, monkeypatch):
    monkeypatch.setattr(linkage, "MAX_INLINE_KEYS", 2)
    frame = fixture_units()
    with pytest.raises(ValueError) as caught:
        linkage.controlled_sql(
            con, frame, rule(max_block_size=1, on_oversize="drop"), "person"
        )
    assert "too coarse" in str(caught.value)


# ---------------------------------------------------------------------------
# The counting function, against a pair counted one at a time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [
    {},
    {"max_block_size": 10, "refine_with": ["forename_initial"]},
    {"max_block_size": 10, "on_oversize": "drop"},
    {"max_block_size": 10, "refine_with": ["forename_initial"], "drop_above": 10},
    {"max_block_size": 5, "refine_with": ["forename_initial", "postcode"],
     "drop_above": 6},
    {"max_block_size": 2, "refine_with": ["forename_initial"], "drop_above": 2},
])
def test_price_rule_agrees_with_a_brute_force_pair_count(con, extra):
    frame = fixture_units()
    spec = rule(**extra)
    priced = linkage.price_rule(con, frame, spec, "person")
    expected = brute_force_pairs(frame, "surname", linkage.block_control(spec))
    assert priced["pairs"] == expected
    # And the generated SQL makes exactly the pairs the counting function priced.
    assert pairs_from_sql(
        con, frame, linkage.controlled_sql(con, frame, spec, "person")
    ) == expected


def test_price_rule_reports_what_the_control_did(con):
    frame = fixture_units()
    priced = linkage.price_rule(
        con, frame,
        rule(max_block_size=10, refine_with=["forename_initial"], drop_above=10),
        "person",
    )
    # 30 SMITH + 12 NONAME + 8 JONES + 3 PATEL + 2 OKAFOR + 1 SOLO. The two
    # null surnames are not a block: a null key never blocks.
    assert priced["blocks_before"] == 6
    assert priced["pairs_before"] == 435 + 66 + 28 + 3 + 1
    assert priced["oversized_blocks"] == 2
    # SMITH refines to 18 / 9 / 3; the 18 is still over 10 and is dropped.
    assert priced["units_dropped"] == 18
    # NONAME cannot be refined at all — every forename initial in it is null.
    assert priced["units_unrefinable"] == 12
    assert priced["pairs"] == 28 + 3 + 1 + 36 + 3
    assert priced["blocks"] == 4 + 2
    assert priced["exact"] is True


def test_price_rule_counts_an_inequality_exactly(con):
    # The pb5 shape: same forename sound, different surname sound. Group
    # arithmetic alone would over-count it, so the sub-blocks are subtracted.
    frame = pd.DataFrame({
        "unit_id": [str(i) for i in range(8)],
        "track": ["person"] * 8,
        "forename_metaphone": ["JN"] * 8,
        # The last two are null: `l.x <> r.x` is null, not true, for a null, so
        # they pair with nobody — not even with each other.
        "surname_metaphone": ["SM", "SM", "SM", "PTL", "PTL", "OKF", None, None],
    })
    sql = ("l.forename_metaphone = r.forename_metaphone "
           "AND l.surname_metaphone <> r.surname_metaphone")
    priced = linkage.price_rule(con, frame, sql, "person")
    # 15 pairs among the six that have a surname sound, less 3 + 1 inside the
    # same sound.
    assert priced["pairs_before"] == 11
    assert priced["pairs"] == 11
    assert pairs_from_sql(con, frame, sql) == 11


def test_a_condition_counting_cannot_apply_is_reported_as_an_upper_bound(con):
    frame = fixture_units()
    priced = linkage.price_rule(
        con, frame,
        "l.surname = r.surname AND (l.postcode IS NULL OR r.postcode IS NULL)",
        "person",
    )
    assert priced["exact"] is False
    assert priced["residual"] == ["(l.postcode IS NULL OR r.postcode IS NULL)"]


def test_price_rule_reads_units_from_a_parquet_path(con, tmp_path):
    path = tmp_path / "units.parquet"
    fixture_units().to_parquet(path)
    priced = linkage.price_rule(con, str(path), rule(), "person")
    assert priced["pairs"] == 435 + 66 + 28 + 3 + 1


def test_price_rule_keeps_the_tracks_apart(con):
    frame = fixture_units()
    frame.loc[frame["surname"] == "SMITH", "track"] = "organisation"
    assert linkage.price_rule(con, frame, rule(), "person")["pairs"] == 98
    assert linkage.price_rule(con, frame, rule(), "organisation")["pairs"] == 435


# ---------------------------------------------------------------------------
# The friendly description
# ---------------------------------------------------------------------------


def test_the_description_reads_as_english_and_names_the_real_numbers():
    assert linkage.control_description(
        rule(max_block_size=60, refine_with=["forename_initial"], drop_above=200)
    ) == ("Blocks of more than 60 records are compared only when the forename "
          "initial also matches; blocks of more than 200 are skipped.")
    assert linkage.control_description(
        rule(max_block_size=60, refine_with=["forename_initial"])
    ) == ("Blocks of more than 60 records are compared only when the forename "
          "initial also matches.")
    assert linkage.control_description(rule(max_block_size=200)) == \
        "Blocks of more than 200 records are skipped."
    assert linkage.control_description(
        rule(max_block_size=60, refine_with=["forename_initial", "dob_year_clean"])
    ) == ("Blocks of more than 60 records are compared only when the forename "
          "initial and the dob year also match.")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check(settings) -> list[dict]:
    return linkage.validate_linkage_settings(settings, default_ruleset(), RAW_COLUMNS)


def _with_rule(**extra) -> dict:
    settings = default_linkage_settings()
    person = settings["tracks"]["person"]
    person["blocking_rules"] = [rule(**extra)]
    return settings


def _messages(errors, fragment: str) -> list[str]:
    return [e["message"] for e in errors if fragment in e["path"]]


@pytest.mark.parametrize("value", [0, -1, -60, "60", 60.5, True, None])
def test_a_bad_max_block_size_is_refused(value):
    errors = _check(_with_rule(max_block_size=value))
    assert _messages(errors, "max_block_size")


def test_an_unknown_on_oversize_is_refused():
    errors = _check(_with_rule(max_block_size=60, on_oversize="shrink"))
    assert _messages(errors, "on_oversize") == \
        [vocabulary.choice_error("on_oversize", "shrink", linkage.ON_OVERSIZE)]


@pytest.mark.parametrize("value", ["forename_initial", [], [1], ["a", 2], None])
def test_a_refine_with_that_is_not_a_list_of_strings_is_refused(value):
    errors = _check(_with_rule(max_block_size=60, refine_with=value))
    assert _messages(errors, "refine_with") == \
        ["refine_with must be a non-empty list of column names"]


def test_a_refine_with_naming_a_column_the_track_does_not_have_is_refused():
    errors = _check(_with_rule(max_block_size=60, refine_with=["shoe_size"]))
    assert _messages(errors, "refine_with") == \
        ["'shoe_size' is not a column of the person track"]


def test_refine_without_refine_with_is_refused():
    errors = _check(_with_rule(max_block_size=60, on_oversize="refine"))
    assert _messages(errors, "refine_with")


def test_a_drop_above_below_max_block_size_is_refused():
    errors = _check(_with_rule(max_block_size=60, refine_with=["forename_initial"],
                               drop_above=30))
    assert _messages(errors, "drop_above") == [
        "drop_above (30) must be at or above max_block_size (60): a block is "
        "refined first and only then dropped"
    ]
    assert not _check(_with_rule(max_block_size=60,
                                 refine_with=["forename_initial"], drop_above=60))


def test_a_control_key_without_max_block_size_is_refused():
    errors = _check(_with_rule(refine_with=["forename_initial"]))
    assert _messages(errors, "refine_with")


def test_a_rule_with_no_block_key_cannot_take_a_control():
    settings = default_linkage_settings()
    settings["tracks"]["person"]["blocking_rules"] = [
        {"id": "b1", "description": "", "sql": "l.surname <> r.surname",
         "max_block_size": 60}
    ]
    errors = _check(settings)
    assert _messages(errors, "max_block_size")


def test_a_good_control_validates():
    assert _check(_with_rule(max_block_size=60, on_oversize="refine",
                             refine_with=["forename_initial"],
                             drop_above=200)) == []


# ---------------------------------------------------------------------------
# The shipped defaults, which ask for none of this
# ---------------------------------------------------------------------------


def test_the_donations_settings_carry_no_control_and_still_validate():
    """The no-op guarantee: donations blocks exactly as it did before this existed."""
    settings = json.loads(
        (DEFAULTS / "donations" / "linkage_settings.json").read_text(encoding="utf-8")
    )
    for track, config in settings["tracks"].items():
        for spec in linkage.blocking_rules(config):
            assert linkage.block_control(spec) is None
            assert linkage.control_description(spec) == ""
            assert set(spec) == {"id", "description", "sql"}
        for entry in linkage.em_entries(config):
            assert linkage.block_control(entry) is None


def test_every_psc_route_carries_the_control_the_full_snapshot_needs():
    """PSC turns it on, and `PSC_HANDOVER.md` section 104 says what it measured.

    Uncontrolled, the six person routes make 1,901,943,106 pairs over the full
    snapshot's 11,205,785 person units, and the first EM training rule makes
    582,535,879 on its own. Nothing runs without this.
    """
    settings = json.loads(
        (DEFAULTS / "psc" / "linkage_settings.json").read_text(encoding="utf-8")
    )
    for track, config in settings["tracks"].items():
        for spec in linkage.blocking_rules(config):
            control = linkage.block_control(spec)
            assert control is not None, f"{track}/{spec['id']}"
            assert control["on_oversize"] == "refine"
            assert control["refine_with"], f"{track}/{spec['id']}"
            # The whole point: a route may not refine on a column its own key
            # already fixes, or it splits nothing.
            fixed = linkage.columns_held_equal(spec["sql"])
            assert not (set(control["refine_with"]) & fixed), \
                f"{track}/{spec['id']} refines on a column its key already fixes"
        for entry in linkage.em_entries(config):
            control = linkage.block_control(entry)
            assert control is not None
            assert control["on_oversize"] == "drop"


def test_blocking_rules_carries_the_control_through_untouched():
    config = {"blocking_rules": [
        {"id": "b1", "sql": "l.surname = r.surname", "max_block_size": 60,
         "refine_with": ["forename_initial"], "drop_above": 200},
        {"id": "b2", "sql": "l.postcode = r.postcode"},
    ]}
    first, second = linkage.blocking_rules(config)
    assert first["max_block_size"] == 60
    assert first["refine_with"] == ["forename_initial"]
    assert first["drop_above"] == 200
    assert set(second) == {"id", "description", "sql"}


# ---------------------------------------------------------------------------
# The control on an EM training rule
# ---------------------------------------------------------------------------
# An EM rule is priced against the same `max_pairs` and nothing downstream
# trims it, so a coarse one is more dangerous than a coarse prediction rule.
# PSC's person em1 — "both name sounds agree" — makes 582,535,879 training
# pairs on the full snapshot against 94,743,605 for the whole of prediction,
# and 43,054,672 with `drop` over 60. See PSC_HANDOVER.md, section 104.


def test_an_em_rule_may_be_written_as_text_or_as_an_object():
    config = {"em_blocking_rules": [
        "l.surname = r.surname",
        {"sql": "l.postcode = r.postcode", "max_block_size": 60,
         "on_oversize": "drop"},
    ]}
    entries = linkage.em_entries(config)
    assert [e["sql"] for e in entries] == ["l.surname = r.surname",
                                           "l.postcode = r.postcode"]
    assert linkage.block_control(entries[0]) is None
    assert linkage.block_control(entries[1]) == {
        "max_block_size": 60, "on_oversize": "drop",
        "refine_with": [], "drop_above": None,
    }
    # The text form is what every existing caller reads, and it is unchanged.
    assert linkage.em_rules(config) == ["l.surname = r.surname",
                                        "l.postcode = r.postcode"]


def test_an_em_rule_with_no_rules_still_falls_back_to_the_blocking_rules():
    config = {"blocking_rules": [{"id": "b1", "description": "",
                                  "sql": "l.surname = r.surname"}]}
    assert [e["sql"] for e in linkage.em_entries(config)] == ["l.surname = r.surname"]


def test_an_em_rule_object_with_no_sql_is_refused():
    settings = default_linkage_settings()
    settings["tracks"]["person"]["em_blocking_rules"] = [{"max_block_size": 60}]
    assert _messages(_check(settings), "em_blocking_rules") == [
        "An EM blocking rule must be SQL text, or an object with a 'sql' key"
    ]


def test_an_em_rule_control_is_validated_like_any_other():
    settings = default_linkage_settings()
    settings["tracks"]["person"]["em_blocking_rules"] = [
        {"sql": "l.surname_metaphone = r.surname_metaphone",
         "max_block_size": 0, "on_oversize": "drop"}
    ]
    assert _messages(_check(settings), "max_block_size")

    settings["tracks"]["person"]["em_blocking_rules"] = [
        {"sql": "l.surname_metaphone = r.surname_metaphone",
         "max_block_size": 60, "on_oversize": "drop"}
    ]
    assert _messages(_check(settings), "em_blocking_rules") == []


def test_an_em_rule_object_still_counts_towards_the_untrainable_guard():
    """The guard reads the rule's columns, whichever form it is written in."""
    settings = default_linkage_settings()
    person = settings["tracks"]["person"]
    column = person["comparisons"][0]["column"]
    person["em_blocking_rules"] = [
        {"sql": f"l.{column} = r.{column}", "max_block_size": 60,
         "on_oversize": "drop"},
    ]
    assert linkage.untrainable_comparisons(person) == [0]
    assert any("em_blocking_rule" in w["message"]
               for w in linkage.linkage_warnings(settings))


def test_the_em_control_removes_the_hot_block_from_the_training_set(tmp_path):
    """The pairs a dropped block would have made are never built."""
    import duckdb as _duckdb

    rows = [{"unit_id": f"u{n}", "track": "person",
             "surname_metaphone": "HOT" if n < 40 else f"S{n}",
             "forename_metaphone": "HOT" if n < 40 else f"F{n}"}
            for n in range(60)]
    units = pd.DataFrame(rows)
    sql = ("l.surname_metaphone = r.surname_metaphone AND "
           "l.forename_metaphone = r.forename_metaphone")
    con = _duckdb.connect()
    try:
        plain = linkage.price_rule(con, units, {"sql": sql}, "person")
        dropped = linkage.price_rule(
            con, units,
            {"sql": sql, "max_block_size": 20, "on_oversize": "drop"}, "person")
    finally:
        con.close()
    assert plain["pairs"] == 40 * 39 // 2
    assert dropped["pairs"] == 0
    assert dropped["units_dropped"] == 40


def test_the_budget_prices_an_em_rule_after_its_control(tmp_path):
    """A budget that prices the uncontrolled rule is not a budget."""
    from app.pipeline.dedupe import stage_3_score

    units = pd.DataFrame([
        {"unit_id": f"u{n}", "track": "person",
         "surname_metaphone": "HOT" if n < 40 else f"S{n}",
         "forename_metaphone": "HOT" if n < 40 else f"F{n}",
         "surname_clean": f"S{n}"}
        for n in range(60)
    ])
    sql = ("l.surname_metaphone = r.surname_metaphone AND "
           "l.forename_metaphone = r.forename_metaphone")
    settings = {"tracks": {"person": {
        "blocking_rules": [{"id": "b1", "description": "",
                            "sql": "l.surname_clean = r.surname_clean"}],
        "em_blocking_rules": [{"sql": sql, "max_block_size": 20,
                               "on_oversize": "drop"}],
        "max_pairs": 1000,
    }}}
    db_api = stage_3_score._db_api(tmp_path)
    report, failure = stage_3_score.blocking_budget_report(units, settings, db_api)
    em = report["tracks"]["person"]["em_rules"][0]

    assert em["pairs"] == 0, "the hot block never reaches the training set"
    assert em["control"]["on_oversize"] == "drop"
    assert em["sql"] == sql, "the report shows the rule as it was written"
    assert em["sql_after_control_bytes"] > len(sql), "and the size of what ran"
    assert report["tracks"]["person"]["em_over_budget"] is False
    assert failure is None


def test_the_budget_prices_a_refined_rule_with_price_rule(tmp_path):
    """Splink cannot count a refined rule, so the budget counts it itself.

    The control generates ``… AND (key NOT IN (…) OR refine columns agree)``,
    and that ``OR`` stops Splink's blocking analyser seeing the equi-join: it
    falls back to the cartesian bound and refuses. On the full snapshot's
    593,640 organisation units it asked for a limit above 3.524e+11, which is
    593,640 squared over two. The number here has to be the rule's real cost,
    not the square of the unit count, and it has to be priced from the rule as
    written rather than from the SQL the control already generated.
    """
    from app.pipeline.dedupe import stage_3_score

    units = pd.DataFrame([
        {"unit_id": f"u{n}", "track": "person",
         "surname_metaphone": "HOT" if n < 40 else f"S{n}",
         "forename_initial": "A" if n % 2 else "B"}
        for n in range(60)
    ])
    rule = {"id": "b1", "description": "",
            "sql": "l.surname_metaphone = r.surname_metaphone",
            "max_block_size": 20, "on_oversize": "refine",
            "refine_with": ["forename_initial"], "drop_above": 60}
    settings = {"tracks": {"person": {"blocking_rules": [rule],
                                      "max_pairs": 10_000_000}}}
    con = duckdb.connect()
    try:
        expected = linkage.price_rule(con, units, rule, "person")["pairs"]
    finally:
        con.close()

    report, failure = stage_3_score.blocking_budget_report(
        units, settings, stage_3_score._db_api(tmp_path))
    priced = report["tracks"]["person"]["rules"][0]

    assert expected == 2 * (20 * 19 // 2), "the hot block splits on the initial"
    assert priced["pairs"] == expected
    assert priced["pairs"] < len(units) ** 2, "not the cartesian bound"
    assert failure is None


def test_a_rule_splink_will_not_count_is_priced_rather_than_lost(tmp_path):
    """Splink refuses more rules than the refined ones, and a run must survive it.

    PSC's pb5 blocks on the forename sound and the date of birth and then
    filters on `l.surname_metaphone <> r.surname_metaphone`. Splink counts the
    block before the filter, and at 11.2 million person units that is over its
    own `max_rows_limit`, so it returns the string "exceeded max_rows_limit"
    where an integer belongs. That killed a full-scale stage 3 in the budget
    check, before anything was scored.
    """
    from app.pipeline.dedupe import stage_3_score

    units = pd.DataFrame([
        {"unit_id": f"u{n}", "track": "person", "forename_metaphone": "F",
         "surname_metaphone": f"S{n % 3}"} for n in range(30)
    ])
    rule = {"id": "b1", "description": "",
            "sql": ("l.forename_metaphone = r.forename_metaphone AND "
                    "l.surname_metaphone <> r.surname_metaphone")}

    class Refuses:
        """Stands in for Splink when it will not give a number."""

    def refuse(*args, **kwargs):
        raise ValueError("invalid literal for int() with base 10: "
                         "'exceeded max_rows_limit, see warning'")

    real = stage_3_score.count_blocking_pairs
    stage_3_score.count_blocking_pairs = refuse
    try:
        pairs = stage_3_score.count_rule_pairs(units, rule, rule["sql"],
                                               "person", Refuses(), tmp_path)
    finally:
        stage_3_score.count_blocking_pairs = real

    con = duckdb.connect()
    try:
        expected = linkage.price_rule(con, units, rule, "person")["pairs"]
    finally:
        con.close()
    assert pairs == expected == 30 * 29 // 2 - 3 * (10 * 9 // 2)

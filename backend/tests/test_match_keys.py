"""The match-key engine: eligibility, guards, splitting, and the union across keys."""

import os
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.rules import engine, keys
from tests.rulesets import default_ruleset, small_ruleset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def records(*rows, track="person") -> pd.DataFrame:
    """A records frame from ``(record_id, **columns)`` dicts, one track by default."""
    frame = pd.DataFrame(list(rows))
    if "track" not in frame.columns:
        frame["track"] = track
    return frame


def a_key(**overrides) -> dict:
    key = {
        "id": "k1", "name": "Key one", "track": "person", "tier": 1,
        "columns": ["name_clean"], "allow_null": False,
        "applies_when": "always", "guards": {}, "on_guard_fail": "review",
    }
    key.update(overrides)
    return key


def a_ruleset(*match_keys, token_lists=None) -> dict:
    return {"token_lists": token_lists or {}, "match_keys": list(match_keys)}


def membership(groups: pd.DataFrame, status=keys.MERGED) -> dict[str, set[str]]:
    """``{group_id: {record_id, ...}}`` for one status."""
    part = groups[groups["status"] == status]
    return {
        group_id: set(rows["record_id"])
        for group_id, rows in part.groupby("group_id")
    }


def sets(groups: pd.DataFrame, status=keys.MERGED) -> list[set[str]]:
    return sorted(membership(groups, status).values(), key=sorted)


def key_stats(stats: dict, key_id: str) -> dict:
    return next(s for s in stats["keys"] if s["id"] == key_id)


# ---------------------------------------------------------------------------
# Grouping and nulls
# ---------------------------------------------------------------------------


class TestGrouping:
    def test_equal_values_form_a_group_and_a_lone_value_does_not(self):
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE"},
            {"record_id": "2", "name_clean": "ANN LEE"},
            {"record_id": "3", "name_clean": "BOB RAY"},
        )
        groups, stats = keys.apply_match_keys(frame, a_ruleset(a_key()))

        assert sets(groups) == [{"1", "2"}]
        assert stats["overall"]["merged_groups"] == 1
        assert stats["overall"]["merged_records"] == 2
        # Three records, two of them merged into one: two entities left.
        assert stats["overall"]["entities_after"] == 2

    def test_a_null_key_column_makes_a_singleton(self):
        frame = records(
            {"record_id": "1", "name_clean": None},
            {"record_id": "2", "name_clean": None},
            {"record_id": "3", "name_clean": "ANN LEE"},
        )
        groups, stats = keys.apply_match_keys(frame, a_ruleset(a_key()))

        assert len(groups) == 0
        assert key_stats(stats, "k1")["eligible_records"] == 1

    def test_a_blank_counts_as_missing(self):
        frame = records(
            {"record_id": "1", "name_clean": "  "},
            {"record_id": "2", "name_clean": ""},
        )
        groups, _ = keys.apply_match_keys(frame, a_ruleset(a_key()))
        assert len(groups) == 0

    def test_allow_null_lets_two_nulls_match(self):
        frame = records(
            {"record_id": "1", "name_clean": None},
            {"record_id": "2", "name_clean": None},
            {"record_id": "3", "name_clean": "ANN LEE"},
        )
        groups, stats = keys.apply_match_keys(
            frame, a_ruleset(a_key(allow_null=True))
        )

        assert sets(groups) == [{"1", "2"}]
        assert key_stats(stats, "k1")["eligible_records"] == 3

    def test_every_key_column_must_agree(self):
        frame = records(
            {"record_id": "1", "name_clean": "ACME", "postcode_clean": "LE1 1FB"},
            {"record_id": "2", "name_clean": "ACME", "postcode_clean": "LE1 1FB"},
            {"record_id": "3", "name_clean": "ACME", "postcode_clean": "M1 1AA"},
        )
        groups, _ = keys.apply_match_keys(
            frame, a_ruleset(a_key(columns=["name_clean", "postcode_clean"]))
        )
        assert sets(groups) == [{"1", "2"}]

    def test_matching_is_case_insensitive(self):
        frame = records(
            {"record_id": "1", "name_clean": "ann lee"},
            {"record_id": "2", "name_clean": "ANN LEE"},
        )
        groups, _ = keys.apply_match_keys(frame, a_ruleset(a_key()))
        assert sets(groups) == [{"1", "2"}]

    def test_a_key_only_sees_its_own_track(self):
        frame = pd.DataFrame([
            {"record_id": "1", "name_clean": "ACME", "track": "person"},
            {"record_id": "2", "name_clean": "ACME", "track": "organisation"},
        ])
        groups, _ = keys.apply_match_keys(frame, a_ruleset(a_key()))
        assert len(groups) == 0


# ---------------------------------------------------------------------------
# Blocklists
# ---------------------------------------------------------------------------


class TestBlocklists:
    def _ruleset(self):
        return a_ruleset(
            a_key(columns=["company_number"],
                  guards={"blocklists": ["placeholders"]}),
            token_lists={"placeholders": {"tokens": ["00000000", "12345678"]}},
        )

    def test_a_blocked_value_never_groups(self):
        frame = records(
            {"record_id": "1", "company_number": "00000000"},
            {"record_id": "2", "company_number": "00000000"},
            {"record_id": "3", "company_number": "01234567"},
            {"record_id": "4", "company_number": "01234567"},
        )
        groups, stats = keys.apply_match_keys(frame, self._ruleset())

        assert sets(groups) == [{"3", "4"}]
        assert key_stats(stats, "k1")["eligible_records"] == 2

    def test_distinct_blocked_values_are_counted(self):
        frame = records(
            {"record_id": "1", "company_number": "00000000"},
            {"record_id": "2", "company_number": "00000000"},
            {"record_id": "3", "company_number": "12345678"},
        )
        _, stats = keys.apply_match_keys(frame, self._ruleset())
        assert key_stats(stats, "k1")["blocked_values"] == 2

    def test_a_blocklist_is_matched_case_insensitively(self):
        frame = records(
            {"record_id": "1", "company_number": "sc000000"},
            {"record_id": "2", "company_number": "SC000000"},
        )
        ruleset = a_ruleset(
            a_key(columns=["company_number"], guards={"blocklists": ["placeholders"]}),
            token_lists={"placeholders": {"tokens": ["SC000000"]}},
        )
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert len(groups) == 0


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------


class TestTiers:
    def _frame(self):
        return records(
            # 1 and 2 share a number, so a no_earlier_key tier-2 key skips them.
            {"record_id": "1", "company_number": "111", "name_clean": "ACME"},
            {"record_id": "2", "company_number": "111", "name_clean": "ACME"},
            {"record_id": "3", "company_number": None, "name_clean": "ACME"},
            {"record_id": "4", "company_number": None, "name_clean": "ACME"},
        )

    def test_no_earlier_key_skips_records_an_earlier_tier_could_use(self):
        ruleset = a_ruleset(
            a_key(id="k1", tier=1, columns=["company_number"]),
            a_key(id="k2", tier=2, columns=["name_clean"], applies_when="no_earlier_key"),
        )
        groups, stats = keys.apply_match_keys(self._frame(), ruleset)

        assert sets(groups) == [{"1", "2"}, {"3", "4"}]
        assert key_stats(stats, "k2")["eligible_records"] == 2

    def test_always_takes_every_record_and_unites_the_two_keys(self):
        ruleset = a_ruleset(
            a_key(id="k1", tier=1, columns=["company_number"]),
            a_key(id="k2", tier=2, columns=["name_clean"], applies_when="always"),
        )
        groups, stats = keys.apply_match_keys(self._frame(), ruleset)

        assert sets(groups) == [{"1", "2", "3", "4"}]
        assert key_stats(stats, "k2")["eligible_records"] == 4

    def test_keys_of_the_same_tier_are_not_earlier_than_each_other(self):
        frame = records(
            {"record_id": "1", "a": "x", "b": "y"},
            {"record_id": "2", "a": "x", "b": "y"},
        )
        ruleset = a_ruleset(
            a_key(id="k1", tier=1, columns=["a"]),
            a_key(id="k2", tier=1, columns=["b"], applies_when="no_earlier_key"),
        )
        _, stats = keys.apply_match_keys(frame, ruleset)
        assert key_stats(stats, "k2")["eligible_records"] == 2

    def test_tier_order_beats_document_order(self):
        """A tier-1 key written second still runs first, so no_earlier_key sees it."""
        frame = self._frame()
        ruleset = a_ruleset(
            a_key(id="late", tier=2, columns=["name_clean"], applies_when="no_earlier_key"),
            a_key(id="early", tier=1, columns=["company_number"]),
        )
        _, stats = keys.apply_match_keys(frame, ruleset)
        assert [s["id"] for s in stats["keys"]] == ["early", "late"]
        assert key_stats(stats, "late")["eligible_records"] == 2

    def test_a_record_blocked_out_of_an_earlier_key_reaches_the_next_tier(self):
        frame = records(
            {"record_id": "1", "company_number": "00000000", "name_clean": "ACME"},
            {"record_id": "2", "company_number": "00000000", "name_clean": "ACME"},
        )
        ruleset = a_ruleset(
            a_key(id="k1", tier=1, columns=["company_number"],
                  guards={"blocklists": ["placeholders"]}),
            a_key(id="k2", tier=2, columns=["name_clean"], applies_when="no_earlier_key"),
            token_lists={"placeholders": {"tokens": ["00000000"]}},
        )
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert sets(groups) == [{"1", "2"}]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_max_group_size_holds_the_group_for_review(self):
        frame = records(*[
            {"record_id": str(i), "name_clean": "ANN LEE"} for i in range(1, 5)
        ])
        groups, stats = keys.apply_match_keys(
            frame, a_ruleset(a_key(guards={"max_group_size": 3}))
        )

        assert sets(groups, keys.MERGED) == []
        assert sets(groups, keys.HELD) == [{"1", "2", "3", "4"}]
        assert groups["guard"].dropna().unique().tolist() == ["max_group_size:4>3"]
        assert key_stats(stats, "k1")["held_groups"] == 1
        assert key_stats(stats, "k1")["held_records"] == 4

    def test_a_group_at_the_limit_still_merges(self):
        frame = records(*[
            {"record_id": str(i), "name_clean": "ANN LEE"} for i in range(1, 4)
        ])
        groups, _ = keys.apply_match_keys(
            frame, a_ruleset(a_key(guards={"max_group_size": 3}))
        )
        assert sets(groups) == [{"1", "2", "3"}]

    def test_max_distinct_counts_distinct_non_null_values(self):
        frame = records(
            {"record_id": "1", "company_number": "111", "name_core": "ACME"},
            {"record_id": "2", "company_number": "111", "name_core": "BETA"},
            {"record_id": "3", "company_number": "111", "name_core": "GAMMA"},
            {"record_id": "4", "company_number": "111", "name_core": None},
        )
        ruleset = a_ruleset(a_key(
            columns=["company_number"],
            guards={"max_distinct": {"column": "name_core", "count": 2}},
        ))
        groups, _ = keys.apply_match_keys(frame, ruleset)

        assert sets(groups, keys.HELD) == [{"1", "2", "3", "4"}]
        assert groups["guard"].dropna().unique().tolist() == ["max_distinct:name_core=3>2"]

    def test_max_distinct_lets_a_group_inside_the_limit_through(self):
        frame = records(
            {"record_id": "1", "company_number": "111", "name_core": "ACME"},
            {"record_id": "2", "company_number": "111", "name_core": "ACME LTD"},
        )
        ruleset = a_ruleset(a_key(
            columns=["company_number"],
            guards={"max_distinct": {"column": "name_core", "count": 2}},
        ))
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert sets(groups) == [{"1", "2"}]

    def test_size_is_checked_before_distinctness(self):
        """RULESET.md fixes the order, so the reason a reviewer reads is stable."""
        frame = records(*[
            {"record_id": str(i), "company_number": "111", "name_core": f"NAME {i}"}
            for i in range(1, 5)
        ])
        ruleset = a_ruleset(a_key(
            columns=["company_number"],
            guards={"max_group_size": 3, "max_distinct": {"column": "name_core", "count": 2}},
        ))
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert groups["guard"].dropna().unique().tolist() == ["max_group_size:4>3"]

    def test_skip_drops_the_group_silently(self):
        frame = records(*[
            {"record_id": str(i), "name_clean": "ANN LEE"} for i in range(1, 5)
        ])
        groups, stats = keys.apply_match_keys(
            frame, a_ruleset(a_key(guards={"max_group_size": 3}, on_guard_fail="skip"))
        )

        assert len(groups) == 0
        assert key_stats(stats, "k1")["held_groups"] == 0

    def test_a_held_group_is_never_united_with_a_merged_one(self):
        frame = records(
            # k1 holds 1..4 on size; k2 merges 1 and 2 on their postcode.
            {"record_id": "1", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "2", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "3", "name_clean": "ANN LEE", "postcode_clean": None},
            {"record_id": "4", "name_clean": "ANN LEE", "postcode_clean": None},
        )
        ruleset = a_ruleset(
            a_key(id="k1", tier=1, guards={"max_group_size": 3}),
            a_key(id="k2", tier=2, columns=["postcode_clean"]),
        )
        groups, stats = keys.apply_match_keys(frame, ruleset)

        assert sets(groups, keys.MERGED) == [{"1", "2"}]
        assert sets(groups, keys.HELD) == [{"1", "2", "3", "4"}]
        # 1 and 2 appear twice — once merged, once held — and that is allowed.
        assert stats["overall"]["merged_records"] == 2
        assert stats["overall"]["held_records"] == 4


# ---------------------------------------------------------------------------
# require_any_equal
# ---------------------------------------------------------------------------


class TestRequireAnyEqual:
    def _ruleset(self, columns=("postcode_clean",)):
        return a_ruleset(a_key(guards={"require_any_equal": list(columns)}))

    def test_the_group_becomes_its_connected_parts(self):
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "2", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "3", "name_clean": "ANN LEE", "postcode_clean": "M1 1AA"},
            {"record_id": "4", "name_clean": "ANN LEE", "postcode_clean": "M1 1AA"},
        )
        groups, _ = keys.apply_match_keys(frame, self._ruleset())
        assert sets(groups) == [{"1", "2"}, {"3", "4"}]

    def test_a_part_of_one_is_left_unmerged(self):
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "2", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "3", "name_clean": "ANN LEE", "postcode_clean": "M1 1AA"},
        )
        groups, stats = keys.apply_match_keys(frame, self._ruleset())
        assert sets(groups) == [{"1", "2"}]
        assert key_stats(stats, "k1")["records"] == 2

    def test_a_null_corroborator_joins_nobody(self):
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE", "postcode_clean": None},
            {"record_id": "2", "name_clean": "ANN LEE", "postcode_clean": None},
        )
        groups, _ = keys.apply_match_keys(frame, self._ruleset())
        assert len(groups) == 0

    def test_a_chain_across_two_columns_stays_one_part(self):
        """A joins B on the postcode, B joins C on the name: one part, not two."""
        frame = records(
            {"record_id": "A", "name_clean": "ANN LEE",
             "postcode_clean": "LE1 1FB", "all_names": "A LEE"},
            {"record_id": "B", "name_clean": "ANN LEE",
             "postcode_clean": "LE1 1FB", "all_names": "ANNIE LEE"},
            {"record_id": "C", "name_clean": "ANN LEE",
             "postcode_clean": "M1 1AA", "all_names": "ANNIE LEE"},
            {"record_id": "D", "name_clean": "ANN LEE",
             "postcode_clean": "G1 1AA", "all_names": "ANNE LEE"},
        )
        groups, _ = keys.apply_match_keys(
            frame, self._ruleset(("postcode_clean", "all_names"))
        )
        assert sets(groups) == [{"A", "B", "C"}]

    def test_the_split_never_crosses_a_group(self):
        """Two records may share a postcode and still not share a key value."""
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "2", "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"},
            {"record_id": "3", "name_clean": "BOB RAY", "postcode_clean": "LE1 1FB"},
            {"record_id": "4", "name_clean": "BOB RAY", "postcode_clean": "LE1 1FB"},
        )
        groups, _ = keys.apply_match_keys(frame, self._ruleset())
        assert sets(groups) == [{"1", "2"}, {"3", "4"}]

    def test_the_guards_run_before_the_split(self):
        frame = records(*[
            {"record_id": str(i), "name_clean": "ANN LEE", "postcode_clean": "LE1 1FB"}
            for i in range(1, 6)
        ])
        ruleset = a_ruleset(a_key(guards={
            "max_group_size": 3, "require_any_equal": ["postcode_clean"],
        }))
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert sets(groups, keys.HELD) == [{"1", "2", "3", "4", "5"}]
        assert sets(groups, keys.MERGED) == []


# ---------------------------------------------------------------------------
# Uniting keys, and the ids
# ---------------------------------------------------------------------------


class TestUnionAcrossKeys:
    def test_two_keys_sharing_a_record_make_one_group(self):
        frame = records(
            {"record_id": "1", "a": "x", "b": None},
            {"record_id": "2", "a": "x", "b": "y"},
            {"record_id": "3", "a": None, "b": "y"},
        )
        ruleset = a_ruleset(
            a_key(id="k1", columns=["a"]),
            a_key(id="k2", columns=["b"]),
        )
        groups, stats = keys.apply_match_keys(frame, ruleset)

        assert sets(groups) == [{"1", "2", "3"}]
        assert groups["key_ids"].unique().tolist() == ["k1|k2"]
        # Each key still reports what it alone merged.
        assert key_stats(stats, "k1")["groups"] == 1
        assert key_stats(stats, "k2")["groups"] == 1
        assert stats["overall"]["merged_groups"] == 1

    def test_a_record_two_keys_both_merged_gets_one_row(self):
        """"A record appears at most once as merged" — the pair counts depend on it."""
        frame = records(
            {"record_id": "1", "a": "x", "b": "y"},
            {"record_id": "2", "a": "x", "b": "y"},
        )
        ruleset = a_ruleset(a_key(id="k1", columns=["a"]), a_key(id="k2", columns=["b"]))
        groups, _ = keys.apply_match_keys(frame, ruleset)

        assert len(groups) == 2
        assert groups["record_id"].tolist() == ["1", "2"]

    def test_key_ids_follow_document_order(self):
        frame = records(
            {"record_id": "1", "a": "x", "b": "y"},
            {"record_id": "2", "a": "x", "b": "y"},
        )
        ruleset = a_ruleset(
            a_key(id="second", tier=2, columns=["b"]),
            a_key(id="first", tier=1, columns=["a"]),
        )
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert groups["key_ids"].unique().tolist() == ["second|first"]

    def test_tracks_never_mix(self):
        frame = pd.DataFrame([
            {"record_id": "1", "a": "x", "track": "person"},
            {"record_id": "2", "a": "x", "track": "person"},
            {"record_id": "3", "a": "x", "track": "organisation"},
            {"record_id": "4", "a": "x", "track": "organisation"},
        ])
        ruleset = a_ruleset(
            a_key(id="p", track="person", columns=["a"]),
            a_key(id="o", track="organisation", columns=["a"]),
        )
        groups, _ = keys.apply_match_keys(frame, ruleset)
        assert sets(groups) == [{"1", "2"}, {"3", "4"}]


class TestGroupIds:
    def test_a_merged_id_is_the_smallest_member_id_as_a_string(self):
        frame = records(
            {"record_id": "10", "name_clean": "ANN LEE"},
            {"record_id": "2", "name_clean": "ANN LEE"},
        )
        groups, _ = keys.apply_match_keys(frame, a_ruleset(a_key()))
        # "10" sorts before "2" as text, and record ids compare as text.
        assert groups["group_id"].unique().tolist() == ["X-10"]

    def test_a_held_id_names_the_key_that_held_it(self):
        frame = records(*[
            {"record_id": str(i), "name_clean": "ANN LEE"} for i in range(1, 5)
        ])
        groups, _ = keys.apply_match_keys(
            frame, a_ruleset(a_key(id="kx", guards={"max_group_size": 3}))
        )
        assert groups["group_id"].unique().tolist() == ["H-kx-1"]

    def test_the_same_membership_gives_the_same_id_whatever_the_row_order(self):
        rows = [
            {"record_id": "5", "name_clean": "ANN LEE"},
            {"record_id": "9", "name_clean": "ANN LEE"},
            {"record_id": "7", "name_clean": "BOB RAY"},
            {"record_id": "3", "name_clean": "BOB RAY"},
        ]
        ruleset = a_ruleset(a_key())
        first, _ = keys.apply_match_keys(records(*rows), ruleset)
        second, _ = keys.apply_match_keys(records(*reversed(rows)), ruleset)

        assert membership(first) == membership(second) == {
            "X-5": {"5", "9"}, "X-3": {"3", "7"},
        }

    def test_the_output_columns_are_the_contract(self):
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE"},
            {"record_id": "2", "name_clean": "ANN LEE"},
        )
        groups, _ = keys.apply_match_keys(frame, a_ruleset(a_key()))
        assert list(groups.columns) == list(keys.GROUP_COLUMNS)
        assert groups["guard"].isna().all()
        assert set(groups["track"]) == {"person"}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_every_field_is_reported_for_every_key(self):
        frame = records(
            {"record_id": "1", "name_clean": "ANN LEE"},
            {"record_id": "2", "name_clean": "ANN LEE"},
        )
        _, stats = keys.apply_match_keys(frame, a_ruleset(a_key()))
        assert set(stats["keys"][0]) == {
            "id", "name", "track", "tier", "eligible_records", "groups", "records",
            "held_groups", "held_records", "blocked_values", "excluded_by_condition",
        }
        assert set(stats["overall"]) >= {
            "merged_groups", "merged_records", "held_groups", "held_records",
            "entities_after",
        }

    def test_entities_after_counts_the_records_a_merge_removed(self):
        frame = records(*(
            [{"record_id": str(i), "name_clean": "ANN LEE"} for i in range(1, 4)]
            + [{"record_id": "9", "name_clean": "BOB RAY"}]
        ))
        _, stats = keys.apply_match_keys(frame, a_ruleset(a_key()))
        # Four records, one group of three: 4 - (3 - 1) = 2.
        assert stats["overall"]["entities_after"] == 2

    def test_an_empty_ruleset_leaves_every_record_its_own_entity(self):
        frame = records({"record_id": "1", "name_clean": "ANN LEE"})
        groups, stats = keys.apply_match_keys(frame, a_ruleset())
        assert len(groups) == 0
        assert stats["keys"] == []
        assert stats["overall"]["entities_after"] == 1

    def test_the_donations_defaults_run_on_a_cleaned_frame(self):
        from app.pipeline.dedupe.stage_1_clean import clean_records

        raw = pd.DataFrame([
            {"record_id": "1", "name": "Mr John Smith", "donor_status": "Individual",
             "postcode": None, "company_number": None},
            {"record_id": "2", "name": "JOHN SMITH", "donor_status": "Individual",
             "postcode": None, "company_number": None},
            {"record_id": "3", "name": "Acme Ltd", "donor_status": "Company",
             "postcode": "le1 1fb", "company_number": "1234567"},
            {"record_id": "4", "name": "Acme Limited", "donor_status": "Company",
             "postcode": "LE1 1FB", "company_number": "01234567"},
        ])
        cleaned = clean_records(raw, default_ruleset())
        groups, _ = keys.apply_match_keys(cleaned, default_ruleset())
        assert sets(groups) == [{"1", "2"}, {"3", "4"}]


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------


def test_two_hundred_thousand_records_group_in_seconds():
    """The guard against accidental pairwise code.

    200,000 records, 20,000 names, one 2,000-member group and a corroborator
    split on top. A pairwise implementation would need two million comparisons
    for the big group alone and would not finish in the budget.
    """
    size = 200_000
    position = np.arange(size)
    name = np.where(
        position < 2_000, "CROWDED NAME",
        np.char.add("NAME ", (position % 20_000).astype(str)),
    )
    frame = pd.DataFrame({
        "record_id": position.astype(str),
        "track": "person",
        "name_clean": name,
        # Three postcodes per name group, so every group really does split.
        "postcode_clean": np.char.add("PC", ((position // 20_000) % 3).astype(str)),
    })
    ruleset = a_ruleset(a_key(guards={"require_any_equal": ["postcode_clean"]}))

    started = time.time()
    groups, stats = keys.apply_match_keys(frame, ruleset)
    elapsed = time.time() - started

    assert stats["overall"]["merged_records"] > 100_000
    assert len(groups) == stats["overall"]["merged_records"]
    assert elapsed < 20, f"took {elapsed:.1f}s — is something walking pairs?"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


RAW = ["record_id", "name", "donor_status"]


def _with_keys(*match_keys) -> dict:
    ruleset = small_ruleset()
    ruleset["match_keys"] = list(match_keys)
    return ruleset


def _paths(errors) -> list[str]:
    return [e["path"] for e in errors]


class TestValidation:
    def test_the_shipped_defaults_are_valid(self):
        from app.profiles import get_profile

        assert engine.validate_ruleset(default_ruleset(), get_profile().raw_columns) == []

    def test_a_key_column_must_exist_for_that_track(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(columns=["name_clean"], track="person")), RAW
        )
        assert errors == []

        errors = engine.validate_ruleset(
            _with_keys(a_key(columns=["postcode_clean"], track="person")), RAW
        )
        assert "match_keys[0].columns" in _paths(errors)

    def test_a_key_needs_at_least_one_column(self):
        errors = engine.validate_ruleset(_with_keys(a_key(columns=[])), RAW)
        assert "match_keys[0].columns" in _paths(errors)

    def test_ids_must_be_unique(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(id="same"), a_key(id="same")), RAW
        )
        assert "match_keys[1].id" in _paths(errors)

    @pytest.mark.parametrize("tier", [0, -1, "1", 1.5, True, None])
    def test_tier_must_be_a_positive_whole_number(self, tier):
        errors = engine.validate_ruleset(_with_keys(a_key(tier=tier)), RAW)
        assert "match_keys[0].tier" in _paths(errors)

    def test_a_missing_tier_defaults_to_one(self):
        key = a_key()
        del key["tier"]
        assert engine.validate_ruleset(_with_keys(key), RAW) == []

    def test_a_blocklist_must_name_a_token_list(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(guards={"blocklists": ["nope"]})), RAW
        )
        assert "match_keys[0].guards.blocklists" in _paths(errors)

    def test_blocklists_must_be_a_list_of_names(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(guards={"blocklists": "titles"})), RAW
        )
        assert "match_keys[0].guards.blocklists" in _paths(errors)

    def test_max_distinct_names_a_column_of_the_track(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(guards={"max_distinct": {"column": "nope", "count": 3}})), RAW
        )
        assert "match_keys[0].guards.max_distinct.column" in _paths(errors)

    def test_max_distinct_needs_a_whole_count(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(guards={"max_distinct": {"column": "name_clean", "count": 0}})),
            RAW,
        )
        assert "match_keys[0].guards.max_distinct.count" in _paths(errors)

    def test_max_group_size_must_be_two_or_more(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(guards={"max_group_size": 1})), RAW
        )
        assert "match_keys[0].guards.max_group_size" in _paths(errors)

    def test_require_any_equal_names_columns_of_the_track(self):
        errors = engine.validate_ruleset(
            _with_keys(a_key(guards={"require_any_equal": ["name_clean", "nope"]})), RAW
        )
        assert _paths(errors) == ["match_keys[0].guards.require_any_equal"]

    def test_a_cleaning_target_counts_as_a_column(self):
        """The keys are checked against raw columns plus what the rules write."""
        ruleset = _with_keys(a_key(columns=["name_clean"]))
        assert engine.validate_ruleset(ruleset, RAW) == []
        # Drop the step that writes it and the key stops being valid.
        ruleset["cleaning"]["person"] = []
        assert "match_keys[0].columns" in _paths(engine.validate_ruleset(ruleset, RAW))

"""Scoring the match keys against the labels a previous review already made.

Every figure here is hand-counted in the test's own comment, because the point
of the module is that n·(n−1)/2 over group sizes gives the same answer a
pairwise loop would.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.rules import keys, keys_eval


def records(*rows, track="person") -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    if "track" not in frame.columns:
        frame["track"] = track
    if "existing_entity_id" not in frame.columns:
        frame["existing_entity_id"] = None
    return frame


def groups(*parts, status=keys.MERGED) -> pd.DataFrame:
    """``groups(["1", "2"], ["3", "4"])`` — one merged group per list."""
    rows = []
    for index, members in enumerate(parts):
        for record_id in members:
            rows.append({
                "record_id": record_id, "group_id": f"X-{index}",
                "track": "person", "status": status, "key_ids": "k1", "guard": None,
            })
    return pd.DataFrame(rows, columns=list(keys.GROUP_COLUMNS))


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


class TestAgreement:
    def _frame(self):
        return records(
            # consistent: two members, both already entity 7
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
            # conflict: two different old ids meet
            {"record_id": "3", "existing_entity_id": "8"},
            {"record_id": "4", "existing_entity_id": "9"},
            # extends: one old id plus an unreviewed record
            {"record_id": "5", "existing_entity_id": "10"},
            {"record_id": "6", "existing_entity_id": None},
            # new: nobody has ever reviewed either
            {"record_id": "7", "existing_entity_id": None},
            {"record_id": "8", "existing_entity_id": None},
        )

    def test_each_group_is_classified(self):
        result = keys_eval.group_agreement(
            self._frame(),
            groups(["1", "2"], ["3", "4"], ["5", "6"], ["7", "8"]),
        )
        assert dict(zip(result["group_id"], result["agreement"])) == {
            "X-0": "consistent", "X-1": "conflict", "X-2": "extends", "X-3": "new",
        }

    def test_agreeing_labels_plus_an_unreviewed_member_is_extends(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
            {"record_id": "3", "existing_entity_id": None},
        )
        result = keys_eval.group_agreement(frame, groups(["1", "2", "3"]))
        assert result["agreement"].tolist() == ["extends"]

    def test_a_conflict_stays_a_conflict_even_with_unreviewed_members(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "8"},
            {"record_id": "3", "existing_entity_id": None},
        )
        result = keys_eval.group_agreement(frame, groups(["1", "2", "3"]))
        assert result["agreement"].tolist() == ["conflict"]

    def test_a_blank_entity_id_means_never_reviewed(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "  "},
            {"record_id": "2", "existing_entity_id": ""},
        )
        result = keys_eval.group_agreement(frame, groups(["1", "2"]))
        assert result["agreement"].tolist() == ["new"]

    def test_held_groups_are_not_classified(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
        )
        held = groups(["1", "2"], status=keys.HELD)
        assert len(keys_eval.group_agreement(frame, held)) == 0


# ---------------------------------------------------------------------------
# Precision and recall
# ---------------------------------------------------------------------------


class TestPairScores:
    def test_precision_counts_only_labelled_pairs_inside_merged_groups(self):
        """Group of 4: ids 7, 7, 8 and one unreviewed record.

        Labelled pairs: 3 labelled members give 3·2/2 = 3 pairs.
        Agreeing pairs: the two 7s give 1. So precision is 1/3.
        """
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
            {"record_id": "3", "existing_entity_id": "8"},
            {"record_id": "4", "existing_entity_id": None},
        )
        result = keys_eval.evaluate(frame, groups(["1", "2", "3", "4"]))

        assert result["labelled_pairs"] == 3
        assert result["labelled_pairs_agreeing"] == 1
        assert result["pair_precision"] == round(1 / 3, 6)

    def test_a_clean_merge_scores_one(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
            {"record_id": "3", "existing_entity_id": "7"},
        )
        result = keys_eval.evaluate(frame, groups(["1", "2", "3"]))
        assert result["labelled_pairs"] == 3
        assert result["pair_precision"] == 1.0

    def test_recall_measures_the_manual_merges_a_key_finds(self):
        """Entity 7 has four records, entity 8 has two: 6 + 1 = 7 manual pairs.

        The keys merge three of entity 7 (3 pairs) and split entity 8 across two
        groups (0 pairs). Recall is 3/7.
        """
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
            {"record_id": "3", "existing_entity_id": "7"},
            {"record_id": "4", "existing_entity_id": "7"},
            {"record_id": "5", "existing_entity_id": "8"},
            {"record_id": "6", "existing_entity_id": "8"},
        )
        result = keys_eval.evaluate(frame, groups(["1", "2", "3"], ["5", "9"]))

        assert result["manual_pairs"] == 7
        assert result["manual_pairs_found"] == 3
        assert result["pair_recall"] == round(3 / 7, 6)

    def test_a_record_left_out_of_every_group_contributes_no_recall(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "7"},
        )
        result = keys_eval.evaluate(frame, groups())
        assert result["manual_pairs"] == 1
        assert result["pair_recall"] == 0.0

    def test_nothing_to_score_reports_null_rather_than_zero(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": None},
            {"record_id": "2", "existing_entity_id": None},
        )
        result = keys_eval.evaluate(frame, groups(["1", "2"]))
        assert result["pair_precision"] is None
        assert result["pair_recall"] is None

    def test_held_groups_score_nothing(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": "8"},
        )
        result = keys_eval.evaluate(frame, groups(["1", "2"], status=keys.HELD))
        assert result["labelled_pairs"] == 0
        assert result["pair_precision"] is None

    def test_a_big_group_is_counted_from_its_size(self):
        """1,000 members of one entity: 499,500 pairs, counted without walking them."""
        frame = records(*[
            {"record_id": str(i), "existing_entity_id": "7"} for i in range(1_000)
        ])
        result = keys_eval.evaluate(frame, groups([str(i) for i in range(1_000)]))
        assert result["labelled_pairs"] == 499_500
        assert result["pair_precision"] == 1.0
        assert result["pair_recall"] == 1.0


# ---------------------------------------------------------------------------
# Counts and tracks
# ---------------------------------------------------------------------------


class TestCounts:
    def test_the_headline_counts(self):
        frame = records(
            {"record_id": "1", "existing_entity_id": "7"},
            {"record_id": "2", "existing_entity_id": None},
            {"record_id": "3", "existing_entity_id": None},
            {"record_id": "4", "existing_entity_id": "8"},
            {"record_id": "5", "existing_entity_id": "9"},
        )
        result = keys_eval.evaluate(frame, groups(["1", "2", "3"], ["4", "5"]))

        assert result["by_agreement"] == {
            "consistent": 0, "conflict": 1, "extends": 1, "new": 0,
        }
        assert result["conflicts"] == 1
        # Records 2 and 3 are newly attached to entity 7.
        assert result["records_attached"] == 2
        assert result["labelled_records"] == 3

    def test_scores_are_reported_per_track(self):
        frame = pd.DataFrame([
            {"record_id": "1", "track": "person", "existing_entity_id": "7"},
            {"record_id": "2", "track": "person", "existing_entity_id": "7"},
            {"record_id": "3", "track": "organisation", "existing_entity_id": "8"},
            {"record_id": "4", "track": "organisation", "existing_entity_id": "9"},
        ])
        result = keys_eval.evaluate(frame, groups(["1", "2"], ["3", "4"]))

        assert result["by_track"]["person"]["pair_precision"] == 1.0
        assert result["by_track"]["person"]["pair_recall"] == 1.0
        assert result["by_track"]["organisation"]["pair_precision"] == 0.0
        # Two organisations with different ids were never a manual pair.
        assert result["by_track"]["organisation"]["manual_pairs"] == 0
        assert result["by_track"]["organisation"]["pair_recall"] is None

    def test_no_groups_at_all_still_answers(self):
        frame = records({"record_id": "1", "existing_entity_id": "7"})
        result = keys_eval.evaluate(frame, groups())
        assert result["by_agreement"] == {
            "consistent": 0, "conflict": 0, "extends": 0, "new": 0,
        }
        assert result["records_attached"] == 0

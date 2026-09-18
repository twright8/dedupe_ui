# backend/tests/test_model_dataset.py
"""Training rows, weights, folds and the cold-start rule.

`docs/MODEL.md`'s training table, checked line by line: who trains, at what
weight, who is held back, and that a fold never splits a donor.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.model import dataset
from app.model import settings as model_settings

SETTINGS = model_settings.get(None)


def _pairs(rows):
    frame = pd.DataFrame(rows, columns=["unit_id_l", "unit_id_r", "decided_by",
                                        "import_disagrees"])
    frame["track"] = "person"
    return frame


def _members(mapping):
    return pd.DataFrame({"record_id": list(mapping), "unit_id": list(mapping.values())})


def _labels(rows):
    columns = ["record_id_a", "record_id_b", "track", "is_match", "provenance",
               "held_out"]
    return pd.DataFrame(rows, columns=columns)


def _identity_members(pairs):
    ids = sorted(set(pairs["unit_id_l"]) | set(pairs["unit_id_r"]))
    return _members({i: i for i in ids})


# ---------------------------------------------------------------------------
# Where the rows come from
# ---------------------------------------------------------------------------


def test_imported_agreements_are_positives_and_disagreements_are_negatives():
    pairs = _pairs([
        ("a", "b", "import", False),
        ("c", "d", "score", True),
        ("e", "f", "score", False),
    ])
    built = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                          "person", SETTINGS)
    by_row = dict(zip(built.index, zip(built.y, built.source)))
    assert by_row[0] == (1, "import_agree")
    assert by_row[1] == (0, "import_disagree")
    # A pair nothing decided is not a training row at all.
    assert 2 not in by_row


def test_the_weights_follow_the_settings():
    pairs = _pairs([("a", "b", "import", False), ("c", "d", "score", True)])
    built = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                          "person", SETTINGS)
    weights = dict(zip(built.source, built.weight))
    assert weights["import_agree"] == SETTINGS["import_agree_weight"] == 0.25
    assert weights["import_disagree"] == SETTINGS["import_disagree_weight"] == 0.10


def test_a_human_label_trains_at_full_weight_and_beats_the_import_overlay():
    pairs = _pairs([("a", "b", "import", False)])
    labels = _labels([("a", "b", "person", "FALSE", "manual", 0)])
    built = dataset.build(pairs, labels, _identity_members(pairs), "person", SETTINGS)
    assert built.n_rows == 1
    assert built.y.tolist() == [0]
    assert built.source.tolist() == ["human"]
    assert built.weight.tolist() == [1.0]
    assert built.n_human == 1


def test_a_group_decision_counts_as_a_human_label():
    pairs = _pairs([("a", "b", "score", False), ("c", "d", "score", False)])
    labels = _labels([
        ("a", "b", "person", "TRUE", "cluster_merge", 0),
        ("c", "d", "person", "FALSE", "cluster_split", 0),
    ])
    built = dataset.build(pairs, labels, _identity_members(pairs), "person", SETTINGS)
    assert sorted(built.source.tolist()) == ["decision", "decision"]
    assert built.n_human == 2
    assert built.is_human.all()


def test_a_held_out_label_never_trains():
    pairs = _pairs([("a", "b", "score", False), ("c", "d", "score", False)])
    labels = _labels([
        ("a", "b", "person", "TRUE", "manual", 0),
        ("c", "d", "person", "FALSE", "manual", 1),
    ])
    built = dataset.build(pairs, labels, _identity_members(pairs), "person", SETTINGS)
    assert built.n_rows == 1
    assert built.test_index.tolist() == [1]
    assert built.test_y.tolist() == [0]
    held_out_row = [c for c in built.counts if c["held_out"] == 1][0]
    assert held_out_row["rows"] == 1
    assert held_out_row["weight"] is None


def test_a_label_whose_records_now_sit_in_one_unit_is_already_satisfied():
    pairs = _pairs([("u1", "u2", "score", False)])
    members = _members({"r1": "u1", "r2": "u1", "r3": "u2"})
    labels = _labels([("r1", "r2", "person", "TRUE", "manual", 0)])
    built = dataset.build(pairs, labels, members, "person", SETTINGS)
    assert built.n_rows == 0


def test_a_label_naming_an_unknown_record_is_skipped():
    pairs = _pairs([("a", "b", "score", False)])
    labels = _labels([("a", "zz", "person", "TRUE", "manual", 0)])
    built = dataset.build(pairs, labels, _identity_members(pairs), "person", SETTINGS)
    assert built.n_rows == 0


def test_a_label_on_the_other_track_is_left_alone():
    pairs = _pairs([("a", "b", "score", False)])
    labels = _labels([("a", "b", "organisation", "TRUE", "manual", 0)])
    built = dataset.build(pairs, labels, _identity_members(pairs), "person", SETTINGS)
    assert built.n_rows == 0


def test_a_label_is_re_pointed_onto_whichever_units_hold_its_records():
    pairs = _pairs([("u1", "u2", "score", False)])
    members = _members({"r1": "u1", "r9": "u2"})
    labels = _labels([("r9", "r1", "person", "TRUE", "manual", 0)])
    built = dataset.build(pairs, labels, members, "person", SETTINGS)
    assert built.index.tolist() == [0]
    assert built.y.tolist() == [1]


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def test_the_imported_positives_are_capped_and_the_cap_is_reported():
    pairs = _pairs([(f"a{i}", f"b{i}", "import", False) for i in range(50)])
    settings = {**SETTINGS, "import_agree_max_rows": 10}
    built = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                          "person", settings)
    assert built.n_rows == 10
    assert built.sampling["import_agree_available"] == 50
    assert built.sampling["import_agree_kept"] == 10
    assert built.sampling["cap_applied"] == "import_agree_max_rows"
    assert [c for c in built.counts if c["source"] == "import_agree"][0]["capped"]


def test_the_per_human_cap_only_bites_once_someone_has_labelled():
    pairs = _pairs([(f"a{i}", f"b{i}", "import", False) for i in range(50)]
                   + [("h1", "h2", "score", False)])
    settings = {**SETTINGS, "import_agree_per_human_label": 3}
    labels = _labels([("h1", "h2", "person", "TRUE", "manual", 0)])
    without = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                            "person", settings)
    with_human = dataset.build(pairs, labels, _identity_members(pairs),
                               "person", settings)
    assert without.sampling["cap_applied"] is None
    assert with_human.sampling["cap_applied"] == "import_agree_per_human_label"
    assert with_human.sampling["import_agree_kept"] == 3


def test_the_sample_is_the_same_every_time():
    pairs = _pairs([(f"a{i}", f"b{i}", "import", False) for i in range(50)])
    settings = {**SETTINGS, "import_agree_max_rows": 10}
    members = _identity_members(pairs)
    first = dataset.build(pairs, pd.DataFrame(), members, "person", settings)
    second = dataset.build(pairs, pd.DataFrame(), members, "person", settings)
    assert first.index.tolist() == second.index.tolist()


# ---------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------


def test_a_unit_is_never_on_both_sides_of_a_fold():
    # Three chains, each a run of pairs sharing units, plus loners.
    rows = []
    for chain in range(3):
        for step in range(6):
            rows.append((f"c{chain}_{step}", f"c{chain}_{step + 1}", "import", False))
    for lone in range(12):
        rows.append((f"x{lone}", f"y{lone}", "import", False))
    pairs = _pairs(rows)
    built = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                          "person", SETTINGS)

    left = pairs["unit_id_l"].to_numpy()[built.index]
    right = pairs["unit_id_r"].to_numpy()[built.index]
    folds_of_unit: dict = {}
    for unit, fold in list(zip(left, built.fold)) + list(zip(right, built.fold)):
        folds_of_unit.setdefault(unit, set()).add(int(fold))
    assert all(len(folds) == 1 for folds in folds_of_unit.values())


def test_every_pair_of_one_chain_lands_in_one_fold():
    rows = [(f"c{step}", f"c{step + 1}", "import", False) for step in range(8)]
    rows += [(f"x{lone}", f"y{lone}", "import", False) for lone in range(20)]
    pairs = _pairs(rows)
    built = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                          "person", SETTINGS)
    chain_rows = [i for i, row in enumerate(built.index) if row < 8]
    assert len({int(built.fold[i]) for i in chain_rows}) == 1


def test_the_folds_stay_roughly_even():
    pairs = _pairs([(f"a{i}", f"b{i}", "import", False) for i in range(40)])
    built = dataset.build(pairs, pd.DataFrame(), _identity_members(pairs),
                          "person", SETTINGS)
    sizes = np.bincount(built.fold, minlength=4)
    assert sizes.tolist() == [10, 10, 10, 10]


def test_components_are_found_over_units_not_pairs():
    left = np.array(["a", "b", "c", "x"])
    right = np.array(["b", "c", "d", "y"])
    groups = dataset.components(left, right)
    assert len(set(groups[:3])) == 1
    assert groups[3] != groups[0]


def test_assign_folds_puts_the_biggest_group_first():
    group = np.array([0] * 10 + [1] * 5 + [2] * 5)
    folds = dataset.assign_folds(group, 2)
    # The big group takes one fold on its own; the two small ones share the other.
    assert len(set(folds[:10])) == 1
    assert set(folds[10:]) == {1 - folds[0]}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_missing_settings_fall_back_to_the_defaults():
    assert model_settings.get(None) == model_settings.DEFAULTS
    assert model_settings.get({})["target_precision"] == 0.99
    assert model_settings.get({"model": {"target_precision": 0.95}})["target_precision"] \
        == 0.95


def test_a_bad_setting_is_caught_by_the_validator():
    assert model_settings.validate({"model": {"target_precision": 2}})
    assert model_settings.validate({"model": {"n_folds": 0}})
    assert model_settings.validate({"model": {"nonsense": 1}})
    assert model_settings.validate({"model": "yes"})
    assert model_settings.validate({"model": {"target_precision": 0.99}}) == []
    assert model_settings.validate({}) == []

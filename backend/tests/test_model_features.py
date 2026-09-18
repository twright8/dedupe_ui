# backend/tests/test_model_features.py
"""Feature values on hand-built pairs, one assertion per feature.

Every feature in `docs/MODEL.md` is exercised at least once, with a value that
can be worked out by hand, plus the null case: a feature whose inputs are
missing must come back null, never zero. A zero means "they disagree" and a null
means "nobody knows", and a tree learns very different things from the two.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.model import features as feature_lib
from app.model import references as reference_lib
from app.profiles import donations_features
from app.profiles.donations import DonationsProfile


def _unit(unit_id, **values):
    row = {
        "unit_id": unit_id, "unit_size": 1, "parties": None, "units": None,
        "first_year": None, "last_year": None, "n_donations": None,
        "median_value": None, "modal_value": None, "share_round_1000": None,
        "top_values": None, "name_clean": None, "forename": None,
        "forename_canon": None, "middle_names": None, "surname": None,
        "title": None, "post_nominals": None, "name_core": None,
        "postcode_clean": None, "postcode_district": None,
        "company_number_clean": None, "legal_form": None, "donor_status_std": None,
        "donor_status": None,
    }
    row.update(values)
    return row


def _units(*rows):
    return pd.DataFrame(list(rows))


def _pairs(*ids):
    return pd.DataFrame({"unit_id_l": [a for a, _ in ids],
                         "unit_id_r": [b for _, b in ids]})


def _build(pairs, units, track="person", events=None, references=None):
    return donations_features.build(pairs, units, events=events,
                                    references=references, track=track)


def _name_table(rows):
    return pd.DataFrame(rows, columns=["kind", "forename", "surname", "n"])


# ---------------------------------------------------------------------------
# Person: the name group
# ---------------------------------------------------------------------------


def test_name_features_on_a_pair_that_agrees():
    units = _units(
        _unit("a", name_clean="JOHN A SMITH", forename="JOHN", forename_canon="JOHN",
              middle_names="A", surname="SMITH", title="MR", post_nominals="MP OBE"),
        _unit("b", name_clean="JOHN A SMITH", forename="JOHN", forename_canon="JOHN",
              middle_names="ALAN", surname="SMITH", title="MR", post_nominals="OBE"),
    )
    out = _build(_pairs(("a", "b")), units)
    assert out.loc[0, "name_jaro_winkler"] == 1.0
    assert out.loc[0, "forename_exact"] == 1.0
    assert out.loc[0, "forename_canon_equal"] == 1.0
    assert out.loc[0, "middle_initial_agrees"] == 1.0
    assert out.loc[0, "middle_initial_conflicts"] == 0.0
    assert out.loc[0, "title_gender_conflict"] == 0.0
    assert out.loc[0, "post_nominals_agree"] == 1.0
    assert out.loc[0, "forename_initial_of_other"] == 0.0


def test_name_features_on_a_pair_that_conflicts():
    units = _units(
        _unit("a", name_clean="JOHN B SMITH", forename="JOHN", forename_canon="JOHN",
              middle_names="B", surname="SMITH", title="MR", post_nominals="MP"),
        _unit("b", name_clean="JOAN C SMYTH", forename="JOAN", forename_canon="JOAN",
              middle_names="C", surname="SMYTH", title="MRS", post_nominals="OBE"),
    )
    out = _build(_pairs(("a", "b")), units)
    assert 0.0 < out.loc[0, "name_jaro_winkler"] < 1.0
    assert out.loc[0, "forename_exact"] == 0.0
    assert out.loc[0, "middle_initial_agrees"] == 0.0
    assert out.loc[0, "middle_initial_conflicts"] == 1.0
    assert out.loc[0, "title_gender_conflict"] == 1.0
    assert out.loc[0, "post_nominals_agree"] == 0.0


def test_a_forename_that_is_an_initial_of_the_other():
    units = _units(
        _unit("a", forename="J", forename_canon="J", name_clean="J SMITH", surname="SMITH"),
        _unit("b", forename="JOHN", forename_canon="JOHN", name_clean="JOHN SMITH",
              surname="SMITH"),
    )
    out = _build(_pairs(("a", "b")), units)
    assert out.loc[0, "forename_initial_of_other"] == 1.0
    assert out.loc[0, "forename_exact"] == 0.0


def test_a_nickname_agrees_once_it_is_mapped():
    units = _units(
        _unit("a", forename="BOB", forename_canon="ROBERT", surname="SMITH"),
        _unit("b", forename="ROBERT", forename_canon="ROBERT", surname="SMITH"),
    )
    out = _build(_pairs(("a", "b")), units)
    assert out.loc[0, "forename_exact"] == 0.0
    assert out.loc[0, "forename_canon_equal"] == 1.0


def test_a_title_with_no_gender_makes_no_conflict():
    units = _units(
        _unit("a", title="DR", surname="SMITH"),
        _unit("b", title="MRS", surname="SMITH"),
    )
    out = _build(_pairs(("a", "b")), units)
    assert pd.isna(out.loc[0, "title_gender_conflict"])


@pytest.mark.parametrize("column", [
    "name_jaro_winkler", "forename_exact", "forename_initial_of_other",
    "forename_canon_equal", "middle_initial_agrees", "middle_initial_conflicts",
    "title_gender_conflict", "post_nominals_agree",
])
def test_a_missing_name_part_is_null_not_zero(column):
    units = _units(_unit("a"), _unit("b"))
    out = _build(_pairs(("a", "b")), units)
    assert pd.isna(out.loc[0, column])


# ---------------------------------------------------------------------------
# Person: rarity (D12)
# ---------------------------------------------------------------------------


def test_rarity_uses_the_commoner_of_the_two_spellings():
    table = _name_table([
        ("surname", None, "SMITH", 999),
        ("surname", None, "SMYTH", 9),
        ("full", "JOHN", "SMITH", 99),
        ("full", "JOHN", "SMYTH", 4),
    ])
    units = _units(
        _unit("a", forename="JOHN", surname="SMITH"),
        _unit("b", forename="JOHN", surname="SMYTH"),
    )
    out = _build(_pairs(("a", "b")), units,
                 references={reference_lib.UK_NAME_FREQUENCIES: table})
    assert out.loc[0, "surname_log_frequency"] == pytest.approx(np.log10(1000))
    assert out.loc[0, "full_name_log_frequency"] == pytest.approx(np.log10(100))


def test_a_name_absent_from_the_table_reads_as_the_rarest_there_is():
    table = _name_table([("surname", None, "SMITH", 999)])
    units = _units(_unit("a", forename="X", surname="FOTHERGILL"),
                   _unit("b", forename="X", surname="FOTHERGILL"))
    out = _build(_pairs(("a", "b")), units,
                 references={reference_lib.UK_NAME_FREQUENCIES: table})
    assert out.loc[0, "surname_log_frequency"] == 0.0


def test_rarity_is_null_without_the_reference_table():
    units = _units(_unit("a", forename="JOHN", surname="SMITH"),
                   _unit("b", forename="JOHN", surname="SMITH"))
    out = _build(_pairs(("a", "b")), units, references=None)
    assert pd.isna(out.loc[0, "surname_log_frequency"])
    assert pd.isna(out.loc[0, "full_name_log_frequency"])


def test_rarity_is_null_when_a_surname_is_missing():
    table = _name_table([("surname", None, "SMITH", 999)])
    units = _units(_unit("a", forename="JOHN", surname="SMITH"), _unit("b"))
    out = _build(_pairs(("a", "b")), units,
                 references={reference_lib.UK_NAME_FREQUENCIES: table})
    assert pd.isna(out.loc[0, "surname_log_frequency"])


# ---------------------------------------------------------------------------
# Recipients, timing, amounts, size
# ---------------------------------------------------------------------------


def test_recipient_features():
    units = _units(
        _unit("a", parties="Labour | Greens", units="Camden"),
        _unit("b", parties="Labour", units="Camden | Islington"),
        _unit("c", parties="Greens", units="Leeds"),
    )
    out = _build(_pairs(("a", "b"), ("b", "c")), units)
    # a has {Labour, Greens}, b has {Labour}: one shared out of two.
    assert out.loc[0, "recipient_jaccard"] == pytest.approx(0.5)
    assert out.loc[0, "same_local_unit"] == 1.0
    # a gave to two parties, so "each gave to one party and not the same one" is false.
    assert out.loc[0, "single_party_conflict"] == 0.0
    assert out.loc[1, "recipient_jaccard"] == 0.0
    assert out.loc[1, "same_local_unit"] == 0.0
    assert out.loc[1, "single_party_conflict"] == 1.0


def test_recipient_features_are_null_when_a_side_has_none():
    units = _units(_unit("a", parties="Labour"), _unit("b"))
    out = _build(_pairs(("a", "b")), units)
    assert pd.isna(out.loc[0, "recipient_jaccard"])
    assert pd.isna(out.loc[0, "same_local_unit"])
    assert pd.isna(out.loc[0, "single_party_conflict"])


def test_timing_features():
    units = _units(
        _unit("a", first_year=2001, last_year=2005),
        _unit("b", first_year=2004, last_year=2008),
        _unit("c", first_year=2015, last_year=2016),
    )
    out = _build(_pairs(("a", "b"), ("a", "c")), units)
    assert out.loc[0, "year_gap"] == 0.0
    assert out.loc[0, "years_overlap"] == 1.0
    assert out.loc[1, "year_gap"] == 10.0
    assert out.loc[1, "years_overlap"] == 0.0


def test_timing_features_are_null_without_a_dated_donation():
    units = _units(_unit("a", first_year=2001, last_year=2005), _unit("b"))
    out = _build(_pairs(("a", "b")), units)
    assert pd.isna(out.loc[0, "year_gap"])
    assert pd.isna(out.loc[0, "years_overlap"])


def test_amount_features_from_the_summary_columns():
    units = _units(
        _unit("a", median_value=10000.0, modal_value=10000.0, share_round_1000=1.0,
              top_values="10,000 | 5,000", n_donations=4),
        _unit("b", median_value=5000.0, modal_value=10000.0, share_round_1000=0.5,
              top_values="10,000 | 2,500", n_donations=2),
        _unit("c", median_value=37.5, modal_value=37.5, share_round_1000=0.0,
              top_values="37.50", n_donations=1),
    )
    out = _build(_pairs(("a", "b"), ("a", "c")), units)
    assert out.loc[0, "median_value_ratio"] == pytest.approx(0.5)
    assert out.loc[0, "modal_value_equal"] == 1.0
    assert out.loc[0, "shared_exact_amounts"] == 1.0
    assert out.loc[0, "round_share_difference"] == pytest.approx(0.5)
    assert out.loc[0, "both_single_donation"] == 0.0
    assert out.loc[1, "modal_value_equal"] == 0.0
    assert out.loc[1, "shared_exact_amounts"] == 0.0


def test_both_single_donation():
    units = _units(_unit("a", n_donations=1), _unit("b", n_donations=1),
                   _unit("c", n_donations=9))
    out = _build(_pairs(("a", "b"), ("a", "c")), units)
    assert out.loc[0, "both_single_donation"] == 1.0
    assert out.loc[1, "both_single_donation"] == 0.0


def test_amount_distribution_distance_comes_from_the_donations():
    units = _units(_unit("a"), _unit("b"), _unit("c"))
    events = pd.DataFrame({
        "unit_id": ["a"] * 4 + ["b"] * 4 + ["c"] * 4,
        "value": [100, 100, 100, 100] + [100, 100, 100, 100] + [10000] * 4,
    })
    out = _build(_pairs(("a", "b"), ("a", "c")), units, events=events)
    assert out.loc[0, "amount_distribution_distance"] == pytest.approx(0.0)
    # log10(10001) - log10(101) is about 2, at every quantile.
    assert out.loc[1, "amount_distribution_distance"] == pytest.approx(2.0, abs=0.01)


def test_amount_distribution_distance_is_null_without_donations():
    units = _units(_unit("a"), _unit("b"))
    events = pd.DataFrame({"unit_id": ["a"], "value": [100.0]})
    out = _build(_pairs(("a", "b")), units, events=events)
    assert pd.isna(out.loc[0, "amount_distribution_distance"])


def test_size_features_are_symmetric():
    units = _units(_unit("a", unit_size=1), _unit("b", unit_size=99))
    out = _build(_pairs(("a", "b")), units)
    assert out.loc[0, "log_unit_size_min"] == pytest.approx(np.log10(2))
    assert out.loc[0, "log_unit_size_max"] == pytest.approx(np.log10(100))


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


def test_organisation_identifier_features():
    units = _units(
        _unit("a", name_core="ACME HOLDINGS", postcode_clean="LE1 1FB",
              postcode_district="LE1", company_number_clean="00001234",
              legal_form="LTD", donor_status_std="Company"),
        _unit("b", name_core="ACME HOLDINGS", postcode_clean="LE1 1FB",
              postcode_district="LE1", company_number_clean="00001234",
              legal_form="LTD", donor_status_std="Company"),
        _unit("c", name_core="ACME HOLDINGS", postcode_clean="M1 1AA",
              postcode_district="M1", company_number_clean="00009999",
              legal_form="PLC", donor_status_std="LLP"),
        _unit("d", name_core="BETA CLUB"),
    )
    out = _build(_pairs(("a", "b"), ("a", "c"), ("a", "d")), units,
                 track="organisation")
    assert out.loc[0, "name_core_jaro_winkler"] == 1.0
    assert out.loc[0, "name_core_token_jaccard"] == 1.0
    assert out.loc[0, "postcode_equal"] == 1.0
    assert out.loc[0, "postcode_district_equal"] == 1.0
    assert out.loc[0, "company_number_equal"] == 1.0
    assert out.loc[0, "company_number_conflicts"] == 0.0
    assert out.loc[0, "company_number_one_sided"] == 0.0
    assert out.loc[0, "legal_form_equal"] == 1.0
    assert out.loc[0, "donor_status_std_equal"] == 1.0

    assert out.loc[1, "company_number_equal"] == 0.0
    assert out.loc[1, "company_number_conflicts"] == 1.0
    assert out.loc[1, "postcode_equal"] == 0.0
    assert out.loc[1, "legal_form_equal"] == 0.0
    assert out.loc[1, "donor_status_std_equal"] == 0.0

    assert out.loc[2, "company_number_one_sided"] == 1.0
    assert pd.isna(out.loc[2, "company_number_equal"])
    assert out.loc[2, "name_core_token_jaccard"] == 0.0


def test_organisation_tfidf_prefers_the_rare_word():
    units = _units(
        _unit("a", name_core="ZYTHUM CONSERVATIVE ASSOCIATION"),
        _unit("b", name_core="ZYTHUM CONSERVATIVE ASSOCIATION"),
        _unit("c", name_core="WESTMINSTER CONSERVATIVE ASSOCIATION"),
        _unit("d", name_core="HARLOW CONSERVATIVE ASSOCIATION"),
        _unit("e", name_core="BARNET CONSERVATIVE ASSOCIATION"),
    )
    pairs = _pairs(("a", "b"), ("a", "c"), ("c", "d"), ("d", "e"))
    out = _build(pairs, units, track="organisation")
    assert out.loc[0, "name_tfidf_cosine"] == pytest.approx(1.0)
    # Sharing only the common words scores lower than sharing the rare one too.
    assert out.loc[1, "name_tfidf_cosine"] < out.loc[0, "name_tfidf_cosine"]


# ---------------------------------------------------------------------------
# What reviewers check, by kind of donor (D13c)
# ---------------------------------------------------------------------------


def test_nature_overlap_compares_what_the_donations_were_for():
    units = _units(_unit("a"), _unit("b"), _unit("c"))
    events = pd.DataFrame({
        "unit_id": ["a", "a", "b", "b", "c"],
        "nature": ["Premises", "Staff costs", "Premises", "Travel",
                   "Auction prizes"],
        "value": [1.0] * 5,
    })
    out = _build(_pairs(("a", "b"), ("a", "c")), units, track="organisation",
                 events=events)
    # a is {Premises, Staff costs}, b is {Premises, Travel}: one shared of three.
    assert out.loc[0, "nature_overlap"] == pytest.approx(1 / 3)
    assert out.loc[1, "nature_overlap"] == 0.0


def test_nature_overlap_ignores_repeats_and_blanks():
    units = _units(_unit("a"), _unit("b"))
    events = pd.DataFrame({
        "unit_id": ["a", "a", "a", "b", "b"],
        "nature": ["Premises", "Premises", None, "Premises", ""],
        "value": [1.0] * 5,
    })
    out = _build(_pairs(("a", "b")), units, track="organisation", events=events)
    assert out.loc[0, "nature_overlap"] == 1.0


def test_nature_overlap_is_null_when_a_side_records_none():
    units = _units(_unit("a"), _unit("b"))
    events = pd.DataFrame({"unit_id": ["a"], "nature": ["Premises"], "value": [1.0]})
    out = _build(_pairs(("a", "b")), units, track="organisation", events=events)
    assert pd.isna(out.loc[0, "nature_overlap"])
    # And null everywhere when the run has no evidence rows at all.
    out = _build(_pairs(("a", "b")), units, track="organisation")
    assert pd.isna(out.loc[0, "nature_overlap"])


def test_nature_overlap_is_an_organisation_feature_only():
    assert "nature_overlap" not in {f.name for f in donations_features.metadata("person")}
    assert "nature_overlap" in {
        f.name for f in donations_features.metadata("organisation")}


@pytest.mark.parametrize("status,kind", [
    ("Individual", "individual"),
    ("Public Fund", "public fund"),
    ("Company", "company or registered body"),
    ("Limited Liability Partnership", "company or registered body"),
    ("Friendly Society", "company or registered body"),
    ("Building Society", "company or registered body"),
    ("Trade Union", "trade union"),
    ("Unincorporated Association", "association, trust or other"),
    ("Trust", "association, trust or other"),
    ("Other", "association, trust or other"),
    ("Impermissible Donor", "association, trust or other"),
    ("Registered Political Party", "registered political party"),
    ("something new", "unknown"),
    (None, "unknown"),
])
def test_every_donor_status_maps_to_a_d13c_kind(status, kind):
    assert donations_features.kind_of(status) == kind


def test_status_kind_is_the_shared_kind_or_says_they_differ():
    units = _units(
        _unit("a", donor_status_std="Company"),
        _unit("b", donor_status_std="Limited Liability Partnership"),
        _unit("c", donor_status_std="Trade Union"),
        _unit("d"),
    )
    out = _build(_pairs(("a", "b"), ("a", "c"), ("a", "d"), ("c", "d")),
                 units, track="organisation")
    kinds = donations_features.STATUS_KINDS
    # A company and an LLP are one kind: both are judged on the registration
    # number and the postcode (D13c).
    assert kinds[int(out.loc[0, "status_kind"])] == "company or registered body"
    assert kinds[int(out.loc[1, "status_kind"])] == "kinds differ"
    assert kinds[int(out.loc[2, "status_kind"])] == "kinds differ"
    assert kinds[int(out.loc[3, "status_kind"])] == "kinds differ"


def test_two_units_with_no_status_are_one_unknown_kind():
    out = _build(_pairs(("a", "b")), _units(_unit("a"), _unit("b")),
                 track="organisation")
    assert donations_features.STATUS_KINDS[int(out.loc[0, "status_kind"])] == "unknown"


def test_status_kind_falls_back_to_the_raw_status():
    units = _units(_unit("a", donor_status="Trade Union"),
                   _unit("b", donor_status="Trade Union"))
    out = _build(_pairs(("a", "b")), units, track="organisation")
    assert donations_features.STATUS_KINDS[int(out.loc[0, "status_kind"])] \
        == "trade union"


def test_the_standardised_status_beats_the_raw_one():
    units = _units(
        _unit("a", donor_status="Unincorporated Association",
              donor_status_std="Company"),
        _unit("b", donor_status="Company", donor_status_std="Company"),
    )
    out = _build(_pairs(("a", "b")), units, track="organisation")
    assert donations_features.STATUS_KINDS[int(out.loc[0, "status_kind"])] \
        == "company or registered body"


def test_status_kind_is_on_both_tracks_and_never_null():
    for track in ("person", "organisation"):
        assert "status_kind" in {f.name for f in donations_features.metadata(track)}
    units = _units(_unit("a"), _unit("b", donor_status_std="Individual"))
    out = _build(_pairs(("a", "b")), units)
    assert out["status_kind"].notna().all()


def test_status_kind_is_a_category_with_a_stable_code_list():
    feature = [f for f in donations_features.metadata("organisation")
               if f.name == "status_kind"][0]
    assert feature.is_categorical
    # A category may not carry a monotone constraint; LightGBM refuses one.
    assert feature.monotone == 0
    assert feature.categories == donations_features.STATUS_KINDS
    assert feature.category_label(5) == "trade union"
    assert feature.category_label(99) is None
    assert feature.category_label(None) is None
    # The code list travels with the model, so an older version keeps its own.
    assert feature.as_dict()["categories"] == list(donations_features.STATUS_KINDS)
    assert feature_lib.categorical_names(
        donations_features.metadata("organisation")) == ["status_kind"]
    # Everything else is an ordinary number.
    assert [f.name for f in donations_features.metadata("person")
            if f.is_categorical] == ["status_kind"]


def test_the_standard_status_equality_feature_is_still_there():
    """`status_kind` groups statuses; `donor_status_std_equal` keeps the exact
    comparison, so a company and an LLP can still be told apart."""
    names = {f.name for f in donations_features.metadata("organisation")}
    assert {"status_kind", "donor_status_std_equal"} <= names


def test_organisation_name_features_are_null_without_a_name():
    units = _units(_unit("a", name_core="ACME"), _unit("b"))
    out = _build(_pairs(("a", "b")), units, track="organisation")
    assert pd.isna(out.loc[0, "name_core_jaro_winkler"])
    assert pd.isna(out.loc[0, "name_core_token_jaccard"])
    assert pd.isna(out.loc[0, "name_tfidf_cosine"])


# ---------------------------------------------------------------------------
# The generic half, and assembly
# ---------------------------------------------------------------------------


def test_generic_features_are_the_match_weight_and_the_gammas():
    pairs = pd.DataFrame({
        "unit_id_l": ["a"], "unit_id_r": ["b"],
        "match_weight": [3.5], "gamma_surname": [3.0], "gamma_forename_canon": [1.0],
        "match_probability": [0.9],
    })
    meta = feature_lib.generic_metadata(pairs)
    names = [f.name for f in meta]
    assert names == ["match_weight", "gamma_forename_canon", "gamma_surname"]
    assert all(f.source == "generic" for f in meta)
    assert meta[0].monotone == 1
    frame = feature_lib.generic_features(pairs)
    assert frame.loc[0, "match_weight"] == 3.5
    assert "match_probability" not in frame.columns


def test_a_gamma_with_no_value_in_this_track_is_dropped():
    pairs = pd.DataFrame({
        "unit_id_l": ["a"], "unit_id_r": ["b"], "match_weight": [1.0],
        "gamma_surname": [3.0], "gamma_postcode_clean": [None],
    })
    units = _units(_unit("a", surname="SMITH"), _unit("b", surname="SMITH"))
    frame, meta = feature_lib.build(pairs, units, "person",
                                    profile=DonationsProfile())
    names = [f.name for f in meta]
    assert "gamma_surname" in names
    assert "gamma_postcode_clean" not in names
    assert list(frame.columns) == names


def test_build_puts_the_columns_in_the_metadata_order():
    pairs = pd.DataFrame({"unit_id_l": ["a"], "unit_id_r": ["b"],
                          "match_weight": [1.0], "gamma_surname": [3.0]})
    units = _units(_unit("a", surname="SMITH", name_clean="A SMITH"),
                   _unit("b", surname="SMITH", name_clean="B SMITH"))
    frame, meta = feature_lib.build(pairs, units, "person", profile=DonationsProfile())
    assert list(frame.columns) == [f.name for f in meta]
    assert frame.dtypes.unique().tolist() == [np.dtype("float64")]


def test_every_feature_has_a_label_a_group_and_a_legal_constraint():
    for track in ("person", "organisation"):
        for feature in donations_features.metadata(track):
            assert feature.label and feature.label[0].isupper()
            assert feature.group in feature_lib.GROUP_LABELS
            assert feature.monotone in (-1, 0, 1)


def test_the_documented_feature_groups_are_all_present():
    person = {f.group for f in donations_features.metadata("person")}
    assert person == {"name", "rarity", "recipients", "timing", "amounts", "kind",
                      "size"}
    organisation = {f.group for f in donations_features.metadata("organisation")}
    assert organisation == {"name", "identifiers", "address", "recipients",
                            "nature", "timing", "amounts", "kind", "size"}


def test_an_empty_pairs_frame_still_gives_every_column():
    units = _units(_unit("a"), _unit("b"))
    out = _build(_pairs(), units)
    assert list(out.columns) == [f.name for f in donations_features.metadata("person")]
    assert len(out) == 0


# ---------------------------------------------------------------------------
# Putting a value into words
# ---------------------------------------------------------------------------


def _feature(name, track="organisation"):
    everything = feature_lib.metadata(["match_weight", "gamma_surname"], track,
                                      DonationsProfile())
    return [f for f in everything if f.name == name][0]


def _render(name, value, track="organisation", gamma_levels=None):
    return _feature(name, track).render_value(value, gamma_levels)


@pytest.mark.parametrize("name,value,words,track", [
    ("name_core_jaro_winkler", 0.8666, "0.87", "organisation"),
    ("forename_exact", 1.0, "yes", "person"),
    ("forename_exact", 0.0, "no", "person"),
    ("shared_exact_amounts", 3.0, "3", "person"),
    ("round_share_difference", 0.5, "50%", "person"),
    ("year_gap", 0.0, "no gap", "person"),
    ("year_gap", 1.0, "1 year", "person"),
    ("year_gap", 10.0, "10 years", "person"),
    ("log_unit_size_max", np.log10(2), "1 record", "person"),
    ("log_unit_size_max", np.log10(100), "99 records", "person"),
    ("status_kind", 5.0, "trade union", "organisation"),
    ("match_weight", 3.4776, "3.48 bits", "person"),
    ("amount_distribution_distance", 0.0, "the same", "person"),
    ("amount_distribution_distance", 2.0, "about 100 times apart", "person"),
])
def test_a_value_reads_as_words_not_as_a_number(name, value, words, track):
    assert _render(name, value, track) == words


def test_rarity_reads_back_as_people_in_the_uk():
    assert _render("surname_log_frequency", np.log10(52090), "person") \
        == "about 52,089 in the UK"
    assert _render("surname_log_frequency", 0.0, "person") \
        == "not in the UK name table"


def test_a_missing_value_says_there_is_nothing_to_compare():
    assert _render("name_core_jaro_winkler", None) == feature_lib.NOTHING_TO_COMPARE
    assert _render("forename_exact", np.nan, "person") == feature_lib.NOTHING_TO_COMPARE


def test_a_gamma_reads_as_the_splink_comparison_level():
    levels = {"surname": {2: {"label": "Jaro-Winkler >= 0.92"}}}
    assert _render("gamma_surname", 2.0, "person", levels) == "Jaro-Winkler >= 0.92"
    # The null level says so in words, whatever the model file holds.
    assert _render("gamma_surname", -1.0, "person", levels) == "one side has no value"
    # An unlabelled level still reads as something.
    assert _render("gamma_surname", 7.0, "person", levels) == "level 7"
    assert _render("gamma_surname", 2.0, "person", None) == "level 2"


def test_every_feature_declares_how_to_render_itself():
    for track in ("person", "organisation"):
        for feature in feature_lib.metadata(["match_weight", "gamma_surname"],
                                            track, DonationsProfile()):
            assert feature.render in feature_lib.RENDERINGS, feature.name
            # And every one of them produces words, never a blank.
            assert feature.render_value(1.0), feature.name
            assert feature.render_value(None) == feature_lib.NOTHING_TO_COMPARE

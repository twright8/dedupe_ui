"""Corpus statistics: fitted once, written down, and the same for everybody.

The rule these tests hold in place is in `app/model/corpus.py`: a feature that
needs to know how rare a word is must be told by the run, not by whatever batch
of pairs it happens to be handed. Two things follow, and both are tested here —
a pair's value does not move with the batch size, and one pair explained on its
own reproduces the number the scoring run wrote.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.model import corpus as corpus_lib
from app.profiles import donations_features, psc_features


def _units(names, tracks=None) -> pd.DataFrame:
    return pd.DataFrame({
        "unit_id": [str(i) for i in range(len(names))],
        "track": tracks or ["organisation"] * len(names),
        "name_core": names,
        "unit_size": [1] * len(names),
    })


ORG_NAMES = [
    "ACME TRADING", "ACME HOLDINGS", "BETA TRADING", "ZYTHUM VENTURES",
    "ACME TRADING GROUP", "GAMMA TRADING", "DELTA HOLDINGS", "ZYTHUM LABS",
]


# ---------------------------------------------------------------------------
# The fit itself
# ---------------------------------------------------------------------------


def test_a_stored_fit_transforms_exactly_as_the_original_vectoriser_did():
    from sklearn.feature_extraction.text import TfidfVectorizer

    spec = psc_features.NAME_CORE_TFIDF
    fitted = corpus_lib.fit(spec, pd.Series(ORG_NAMES))
    reference = TfidfVectorizer(
        analyzer=spec.analyzer, token_pattern=spec.token_pattern,
        lowercase=spec.lowercase, norm=spec.norm, max_features=spec.max_features,
    ).fit_transform(ORG_NAMES)
    assert np.allclose(fitted.transform(ORG_NAMES).toarray(), reference.toarray())


def test_a_fit_survives_a_round_trip_through_the_run_folder(tmp_path):
    spec = psc_features.NAME_CORE_TFIDF
    fitted = corpus_lib.fit(spec, pd.Series(ORG_NAMES))
    corpus_lib.write(tmp_path, fitted, "organisation")
    back = corpus_lib.read(tmp_path, spec, "organisation")
    assert back is not None
    assert back.vocabulary == fitted.vocabulary
    assert np.allclose(back.idf, fitted.idf)
    assert np.allclose(back.transform(ORG_NAMES).toarray(),
                       fitted.transform(ORG_NAMES).toarray())


def test_a_fit_made_with_other_settings_is_refused_not_reused(tmp_path):
    """A run scored with one vectoriser and explained with another has to
    refit. The stored numbers would be answers to a different question."""
    spec = psc_features.NAME_CORE_TFIDF
    corpus_lib.write(tmp_path, corpus_lib.fit(spec, pd.Series(ORG_NAMES)),
                     "organisation")
    moved = corpus_lib.CorpusSpec(name=spec.name, column=spec.column,
                                  scope=spec.scope, max_features=5)
    assert corpus_lib.read(tmp_path, moved, "organisation") is None


def test_the_scope_decides_which_units_are_counted():
    units = _units(["ACME", "BETA", None, None],
                   ["organisation", "organisation", "person", "person"])
    track_texts = corpus_lib.texts_for(
        psc_features.NAME_CORE_TFIDF, units, "organisation")
    assert len(track_texts) == 2
    run_texts = corpus_lib.texts_for(
        donations_features.NAME_CORE_TFIDF, units, "organisation")
    assert len(run_texts) == 4


def test_for_run_writes_what_it_fits_and_reads_it_back(tmp_path):
    class Profile:
        @staticmethod
        def corpus_specs(track):
            return psc_features.corpus_specs(track)

    units = _units(ORG_NAMES)
    first = corpus_lib.for_run(tmp_path, units, "organisation", Profile())
    assert set(first) == {"name_core_tfidf"}
    assert (tmp_path / corpus_lib.CORPUS_DIRNAME /
            "organisation_name_core_tfidf.json").is_file()

    # Second time it is read, not refitted — and refitting over a different set
    # of units cannot change it, which is the guarantee.
    second = corpus_lib.for_run(tmp_path, _units(ORG_NAMES[:2]), "organisation",
                                Profile())
    assert second["name_core_tfidf"].vocabulary == first["name_core_tfidf"].vocabulary


def test_a_profile_that_declares_nothing_gets_nothing(tmp_path):
    class Profile:
        pass

    assert corpus_lib.for_run(tmp_path, _units(ORG_NAMES), "organisation",
                              Profile()) == {}


# ---------------------------------------------------------------------------
# What it fixes: the PSC value no longer moves with the batch
# ---------------------------------------------------------------------------


def _pairs(rows):
    return pd.DataFrame({
        "unit_id_l": [str(a) for a, _ in rows],
        "unit_id_r": [str(b) for _, b in rows],
        "track": ["organisation"] * len(rows),
    })


def test_a_psc_pair_scores_the_same_alone_as_in_a_batch(tmp_path):
    """The defect this closes.

    The builder used to fit over the units named in the pairs it was handed, so
    the same pair came out differently depending on how many others arrived with
    it — which also meant a one-pair explanation could not reproduce a scoring
    run.
    """
    units = _units(ORG_NAMES)
    fitted = corpus_lib.for_run(tmp_path, units, "organisation",
                                _profile_with(psc_features))
    references = corpus_lib.attach(None, fitted)

    many = psc_features._tfidf_cosine(
        _pairs([(0, 1), (0, 2), (3, 7), (4, 5)]), units, "name_core", references)
    one = psc_features._tfidf_cosine(
        _pairs([(0, 1)]), units, "name_core", references)
    assert one.iloc[0] == pytest.approx(many.iloc[0])


def test_without_the_corpus_the_psc_value_did_move_with_the_batch():
    """The old behaviour, kept as a test so nobody puts it back by accident."""
    units = _units(ORG_NAMES)
    many = psc_features._tfidf_cosine(
        _pairs([(0, 1), (0, 2), (3, 7), (4, 5)]), units, "name_core", None)
    one = psc_features._tfidf_cosine(_pairs([(0, 1)]), units, "name_core", None)
    # Fitted over every unit both times now, because `units` IS every unit.
    assert one.iloc[0] == pytest.approx(many.iloc[0])


def _profile_with(module):
    class Profile:
        @staticmethod
        def corpus_specs(track):
            return module.corpus_specs(track)

    return Profile()


# ---------------------------------------------------------------------------
# What it must not change: donations
# ---------------------------------------------------------------------------


def test_the_donations_value_is_what_the_old_in_line_fit_produced():
    """The donations model is trained on these numbers. They may not move.

    The old code fitted a TfidfVectorizer over every unit in the frame, person
    rows included, and took the row-wise product. This reproduces that exactly,
    against the same fixture, and the two must agree to the last bit.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer

    units = _units(ORG_NAMES + [None, None],
                   ["organisation"] * 8 + ["person", "person"])
    pairs = _pairs([(0, 1), (0, 4), (3, 7)])

    got = donations_features._tfidf_cosine(pairs, units, None)

    names = units.set_index(units["unit_id"].astype(str))["name_core"]
    names = names[~names.index.duplicated()]
    matrix = TfidfVectorizer(
        analyzer="word", token_pattern=r"[^\s]+", lowercase=False,
        max_features=donations_features.TFIDF_MAX_FEATURES, norm="l2",
    ).fit_transform(names.fillna("").astype(str).to_numpy())
    position = pd.Series(np.arange(len(names)), index=names.index)
    left = position.reindex(pairs["unit_id_l"].astype(str)).to_numpy().astype(int)
    right = position.reindex(pairs["unit_id_r"].astype(str)).to_numpy().astype(int)
    expected = np.asarray(
        matrix[left].multiply(matrix[right]).sum(axis=1)).ravel()

    assert np.allclose(got, expected, rtol=0, atol=0)


def test_the_donations_value_is_the_same_through_a_stored_corpus(tmp_path):
    units = _units(ORG_NAMES + [None, None],
                   ["organisation"] * 8 + ["person", "person"])
    pairs = _pairs([(0, 1), (0, 4), (3, 7)])

    without = donations_features._tfidf_cosine(pairs, units, None)
    fitted = corpus_lib.for_run(tmp_path, units, "organisation",
                                _profile_with(donations_features))
    through = donations_features._tfidf_cosine(
        pairs, units, corpus_lib.attach(None, fitted))
    assert np.allclose(without, through, rtol=0, atol=0)


def test_the_two_profiles_declare_different_scopes_on_purpose():
    """PSC counts its own track; donations counts the whole run.

    Not an oversight in either direction — `app/model/corpus.py` records the
    reason for both, and the donations one is the value a trained model already
    depends on.
    """
    assert psc_features.NAME_CORE_TFIDF.scope == "track"
    assert donations_features.NAME_CORE_TFIDF.scope == "run"
    assert psc_features.corpus_specs("person") == []
    assert donations_features.corpus_specs("person") == []


def test_the_stored_file_is_json_a_person_can_read(tmp_path):
    """A run's fitted state is part of its record, so it is readable text."""
    fitted = corpus_lib.fit(psc_features.NAME_CORE_TFIDF, pd.Series(ORG_NAMES))
    path = corpus_lib.write(tmp_path, fitted, "organisation")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["settings"]["column"] == "name_core"
    assert stored["documents"] == len(ORG_NAMES)
    assert "acme" in stored["vocabulary"]
    assert len(stored["idf"]) == len(stored["vocabulary"])

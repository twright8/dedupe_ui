# backend/app/profiles/psc_features.py
"""The PSC profile's pair features (`docs/MODEL.md`).

Two feature sets. People are settled by name, date of birth, nationality,
address and — the signal donations does not have — the companies the two sides
control. Organisations are settled by the registration number, the country it
was registered in, and the six discriminators deduping used as score vetoes.

Everything is built from whole frames with pandas and numpy joins. A pair never
touches Python: the company sets are reduced to one row per unit before any pair
is looked at, and the co-controller signal is a set join capped at a size that
cannot blow up on a formation agent with ten thousand companies.

Rarity comes from the same ``uk_name_frequencies`` table the donations profile
declares. That table was built FROM the PSC register, so for this profile it is
an in-dataset frequency: it says how common a name is among UK company
controllers, which is exactly the population being matched, but it cannot be
read as independent outside evidence the way it can for donations. A very rare
name here means "rare among PSCs", and two records sharing one are no more
surprising than the register itself makes them.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from app.model import corpus as corpus_lib
from app.model import references as refs
from app.model.features import Feature

SEPARATOR = " | "

# A bare Companies House number is the strongest identifier there is.
GB_NUMBER_RE = re.compile(r"^(\d{8}|[A-Z]{2}\d{6})$")

# How many companies of one unit the co-controller join will consider. A
# formation agent controls tens of thousands; pairing them all would be
# quadratic for no signal, and the feature saturates long before this.
MAX_COMPANIES_PER_UNIT = 200

REFERENCES = [
    refs.Reference(
        key=refs.UK_NAME_FREQUENCIES,
        label="UK name frequencies",
        description=(
            "Forename and surname counts over about 7.5 million UK individuals from "
            "the PSC register. For THIS profile the table is built from the same "
            "population it is scoring, so it measures how common a name is among "
            "company controllers, not how common it is in the country. Treat it as "
            "an in-dataset frequency: useful for ranking, not independent evidence."
        ),
        features=("surname_log_frequency", "full_name_log_frequency"),
    )
]


# ---------------------------------------------------------------------------
# What each feature means
# ---------------------------------------------------------------------------

_NAME = "Name"
_DOB = "Date of birth"
_WHO = "Who they are"
_WHERE = "Where they are"
_COMPANIES = "Companies"
_SIZE = "How much there is"
_ORG_NAME = "Name"
_ORG_ID = "Registration"
_ORG_DISCRIMINATOR = "Tells them apart"

PERSON_FEATURES = [
    Feature("surname_similarity", "Surname similarity", _NAME, 1,
            null_when="one side has no surname", render="similarity"),
    Feature("forename_similarity", "Forename similarity (after nicknames)", _NAME, 1,
            null_when="one side has no forename", render="similarity"),
    Feature("middle_similarity", "Middle name similarity", _NAME, 0,
            null_when="one side has no middle name", render="similarity"),
    Feature("middle_conflict", "Middle names disagree", _NAME, -1,
            null_when="one side has no middle name", render="flag"),
    Feature("name_fingerprint_equal", "Same name letters, any order", _NAME, 1,
            render="flag"),
    Feature("surname_log_frequency", "How common the shared surname is", _NAME, -1,
            null_when="the surnames differ, or the name table is not built",
            render="log_people"),
    Feature("full_name_log_frequency", "How common the shared full name is", _NAME, -1,
            null_when="the names differ, or the name table is not built",
            render="log_people"),

    Feature("dob_month_equal", "Same birth month", _DOB, 1,
            null_when="one side has no date of birth", render="flag"),
    Feature("dob_year_gap", "Years between the birth years", _DOB, -1,
            null_when="one side has no birth year", render="years"),
    Feature("dob_missing", "One side has no date of birth", _DOB, 0, render="flag"),

    Feature("nationality_equal", "Same nationality", _WHO, 1,
            null_when="one side has no nationality", render="flag"),
    Feature("residence_equal", "Same country of residence", _WHO, 1,
            null_when="one side has no country of residence", render="flag"),

    Feature("postcode_equal", "Same postcode", _WHERE, 1,
            null_when="one side has no postcode", render="flag"),
    Feature("postcode_district_equal", "Same postcode district", _WHERE, 1,
            null_when="one side has no postcode", render="flag"),
    Feature("address_similarity", "First address line similarity", _WHERE, 1,
            null_when="one side has no address line", render="similarity"),

    Feature("same_company", "Both control the same company", _COMPANIES, 1,
            render="flag"),
    Feature("shared_companies", "How many companies both control", _COMPANIES, 1,
            render="count"),
    Feature("shared_co_controller", "They share a co-controller", _COMPANIES, 1,
            render="flag"),
    Feature("notified_gap_days", "Days between the two notifications", _COMPANIES, 0,
            null_when="one side has no notification date", render="number"),

    Feature("unit_size_min", "Records on the smaller side", _SIZE, 0, render="count"),
    Feature("unit_size_max", "Records on the larger side", _SIZE, 0, render="count"),
]

ORGANISATION_FEATURES = [
    Feature("name_core_similarity", "Core name similarity", _ORG_NAME, 1,
            null_when="one side has no name", render="similarity"),
    Feature("name_tfidf_cosine", "Name word overlap, rare words weighted", _ORG_NAME, 1,
            render="similarity"),
    Feature("name_fingerprint_equal", "Same name letters, any order", _ORG_NAME, 1,
            render="flag"),

    Feature("regnum_equal", "Same registration number", _ORG_ID, 1,
            null_when="one side has no registration number", render="flag"),
    Feature("regnum_conflict", "Registration numbers differ", _ORG_ID, -1,
            null_when="one side has no registration number", render="flag"),
    Feature("regnum_one_sided", "Only one side has a registration number", _ORG_ID, 0,
            render="flag"),
    Feature("country_equal", "Same country of registration", _ORG_ID, 1,
            null_when="one side has no country", render="flag"),

    Feature("legal_form_equal", "Same legal form", _ORG_DISCRIMINATOR, 1,
            null_when="one side has no legal form", render="flag"),
    # The six discriminators deduping used to cap a score at 0.49. As features
    # they keep the evidence and let the model weigh it, which is what D9 asked
    # for once a supervised layer existed.
    Feature("legal_form_differs", "Legal forms differ", _ORG_DISCRIMINATOR, -1,
            null_when="one side has no legal form", render="flag"),
    Feature("numeric_suffix_differs", "Numbered differently (Fund II vs Fund III)",
            _ORG_DISCRIMINATOR, -1,
            null_when="one side carries no number", render="flag"),
    Feature("subject_differs", "Different subject (Ministry of X vs Y)",
            _ORG_DISCRIMINATOR, -1,
            null_when="neither side names a subject", render="flag"),
    Feature("country_differs", "Registered in different countries",
            _ORG_DISCRIMINATOR, -1,
            null_when="one side has no country", render="flag"),
    Feature("holdings_asymmetry", "One side says HOLDINGS and the other does not",
            _ORG_DISCRIMINATOR, -1,
            null_when="the base names are not near-identical", render="flag"),
    Feature("house_number_differs", "Different house number", _ORG_DISCRIMINATOR, -1,
            null_when="one side has no house number", render="flag"),

    Feature("postcode_equal", "Same postcode", _WHERE, 1,
            null_when="one side has no postcode", render="flag"),
    Feature("postcode_district_equal", "Same postcode district", _WHERE, 1,
            null_when="one side has no postcode", render="flag"),

    Feature("same_company", "Both control the same company", _COMPANIES, 1,
            render="flag"),
    Feature("shared_companies", "How many companies both control", _COMPANIES, 1,
            render="count"),

    Feature("unit_size_min", "Records on the smaller side", _SIZE, 0, render="count"),
    Feature("unit_size_max", "Records on the larger side", _SIZE, 0, render="count"),
]

FEATURES_BY_TRACK = {
    "person": PERSON_FEATURES,
    "organisation": ORGANISATION_FEATURES,
}


def metadata(track: str) -> list[Feature]:
    return list(FEATURES_BY_TRACK.get(track, []))


# ---------------------------------------------------------------------------
# Small vectorised helpers
# ---------------------------------------------------------------------------


def _text(series: pd.Series) -> pd.Series:
    out = series.astype("object").where(series.notna(), None)
    return out.map(lambda v: None if v is None else str(v).strip().upper() or None)


def _side(units: pd.DataFrame, pairs: pd.DataFrame, column: str, side: str) -> pd.Series:
    """One unit column lined up with *pairs*, for the left or right side."""
    if column not in units.columns:
        return pd.Series([None] * len(pairs), index=pairs.index, dtype="object")
    lookup = units.set_index(units["unit_id"].astype(str))[column]
    keys = pairs[f"unit_id_{side}"].astype(str)
    return pd.Series(keys.map(lookup).to_numpy(), index=pairs.index)


def _both(left: pd.Series, right: pd.Series) -> pd.Series:
    return left.notna() & right.notna()


def _equal(left: pd.Series, right: pd.Series) -> pd.Series:
    """1.0 where both sides are present and equal, 0.0 where they differ, else null."""
    known = _both(left, right)
    out = pd.Series(np.nan, index=left.index, dtype="float64")
    out[known] = (left[known] == right[known]).astype(float)
    return out


def _differs(left: pd.Series, right: pd.Series) -> pd.Series:
    """The mirror of ``_equal`` — a discriminator, so 1.0 means "tells them apart"."""
    known = _both(left, right)
    out = pd.Series(np.nan, index=left.index, dtype="float64")
    out[known] = (left[known] != right[known]).astype(float)
    return out


def _jaro_winkler(left: pd.Series, right: pd.Series) -> pd.Series:
    """Similarity over the DISTINCT pairs of values, mapped back."""
    import jellyfish

    known = _both(left, right)
    out = pd.Series(np.nan, index=left.index, dtype="float64")
    if not known.any():
        return out
    keys = pd.Series(list(zip(left[known], right[known])), index=left[known].index)
    distinct = keys.drop_duplicates()
    scored = {
        pair: jellyfish.jaro_winkler_similarity(pair[0], pair[1]) for pair in distinct
    }
    out[known] = keys.map(scored).to_numpy()
    return out


def _fingerprint(series: pd.Series) -> pd.Series:
    """Order- and duplicate-invariant name key, as deduping's name_fingerprint."""
    junk = re.compile(r"[^A-Z0-9 ]")

    def one(value):
        if value is None:
            return None
        tokens = junk.sub(" ", str(value).upper()).split()
        return "".join(sorted(set(tokens))) or None

    present = series[series.notna()].drop_duplicates()
    mapping = {v: one(v) for v in present}
    return series.map(mapping)


def _sets_from(units: pd.DataFrame, column: str) -> dict:
    """``{unit_id: frozenset(values)}`` from a separator-joined unit column."""
    if column not in units.columns:
        return {}
    sets = {}
    for unit_id, value in zip(units["unit_id"].astype(str), units[column]):
        if not isinstance(value, str) or not value:
            continue
        parts = [p.strip() for p in value.split(SEPARATOR) if p.strip()]
        if parts:
            sets[unit_id] = frozenset(parts[:MAX_COMPANIES_PER_UNIT])
    return sets


def _company_sets(units: pd.DataFrame, events: pd.DataFrame | None) -> dict:
    """``{unit_id: frozenset(company numbers)}``.

    Built from the evidence rows when the run has them, because a unit is
    several PSC statements pooled and each names its own company. Falls back to
    the unit's own ``company_number``, which is the representative's one.
    """
    if events is not None and len(events) and "unit_id" in events.columns \
            and "company_number" in events.columns:
        frame = events[["unit_id", "company_number"]].dropna()
        frame = frame.astype(str)
        grouped = frame.groupby("unit_id", sort=False)["company_number"]
        return {
            unit_id: frozenset(list(values)[:MAX_COMPANIES_PER_UNIT])
            for unit_id, values in grouped
        }
    if "company_number" in units.columns:
        return {
            str(unit_id): frozenset({str(value)})
            for unit_id, value in zip(units["unit_id"], units["company_number"])
            if isinstance(value, str) and value
        }
    return {}


def _shared_counts(pairs: pd.DataFrame, sets: dict) -> pd.Series:
    """How many companies both sides of each pair control."""
    left = pairs["unit_id_l"].astype(str)
    right = pairs["unit_id_r"].astype(str)
    empty = frozenset()
    counts = [
        len(sets.get(a, empty) & sets.get(b, empty))
        for a, b in zip(left.to_numpy(), right.to_numpy())
    ]
    return pd.Series(counts, index=pairs.index, dtype="float64")


def _co_controller(pairs: pd.DataFrame, sets: dict) -> pd.Series:
    """Do the two sides each share a company with some third unit?

    Built as company -> units once, so the answer is two set lookups per pair
    rather than a scan of the register.
    """
    by_company: dict = {}
    for unit_id, companies in sets.items():
        for company in companies:
            by_company.setdefault(company, set()).add(unit_id)

    neighbours: dict = {}
    for unit_id, companies in sets.items():
        found: set = set()
        for company in companies:
            found |= by_company.get(company, set())
            if len(found) > MAX_COMPANIES_PER_UNIT:
                break
        found.discard(unit_id)
        neighbours[unit_id] = found

    left = pairs["unit_id_l"].astype(str).to_numpy()
    right = pairs["unit_id_r"].astype(str).to_numpy()
    empty: set = set()
    shared = [
        1.0 if (neighbours.get(a, empty) & neighbours.get(b, empty)) else 0.0
        for a, b in zip(left, right)
    ]
    return pd.Series(shared, index=pairs.index, dtype="float64")


def _log_frequency(names: pd.Series, table: pd.DataFrame | None,
                   name_column: str, count_column: str) -> pd.Series:
    """log10 of how often a name appears in the reference table."""
    out = pd.Series(np.nan, index=names.index, dtype="float64")
    if table is None or not len(table) or name_column not in table.columns:
        return out
    counts = table.set_index(table[name_column].astype(str).str.upper())[count_column]
    counts = counts[~counts.index.duplicated()]
    mapped = names.map(counts)
    return np.log10(pd.to_numeric(mapped, errors="coerce").clip(lower=1.0))


def _house_number(series: pd.Series) -> pd.Series:
    """The leading house number of an address line, as deduping's parser reads it."""
    pattern = re.compile(r"^([0-9]+[A-Z]?)\b")

    def one(value):
        if value is None:
            return None
        match = pattern.match(str(value).upper().strip())
        return match.group(1) if match else None

    present = series[series.notna()].drop_duplicates()
    return series.map({v: one(v) for v in present})


#: The corpus statistics this builder needs, declared rather than fitted on the
#: fly. ``app/model/corpus.py`` explains why. The scope is the track:
#: ``name_core`` is an organisation column and the sample's 449,397 person units
#: carry none, so counting them in would add that many empty documents and push
#: every IDF towards the same value.
NAME_CORE_TFIDF = corpus_lib.CorpusSpec(
    name="name_core_tfidf", column="name_core", scope="track",
    max_features=20000, analyzer="word", token_pattern=r"[A-Za-z0-9]+",
    lowercase=True, norm="l2",
)

CORPUS_SPECS = {"organisation": [NAME_CORE_TFIDF]}


def corpus_specs(track: str) -> list:
    """What this builder needs fitted over the units before it can score."""
    return list(CORPUS_SPECS.get(track, []))


def _tfidf_cosine(pairs: pd.DataFrame, units: pd.DataFrame, column: str,
                  references: dict | None = None) -> pd.Series:
    """Cosine of the two names in a TF-IDF space fitted over every unit.

    Every unit of the track — not the units this call happens to have been
    handed. That is the point. It used to fit over the units named in *pairs*,
    so one pair scored differently depending on how many others it arrived
    with, and the per-pair explanation could not reproduce a single number the
    scoring run had written down.

    The fitted vocabulary and IDF arrive on *references*, put there by
    ``corpus.for_run`` from what the run stored. With nothing there — an old
    run, a test — they are fitted here over the units given, which is the same
    answer whenever those are all of them.
    """
    out = pd.Series(np.nan, index=pairs.index, dtype="float64")
    if column not in units.columns:
        return out
    try:
        import sklearn.feature_extraction.text  # noqa: F401
    except ImportError:
        return out

    spec = NAME_CORE_TFIDF
    fitted = corpus_lib.from_references(references, spec.name)
    if fitted is None:
        texts = corpus_lib.texts_for(spec, units, "organisation")
        if not len(texts) or not texts.fillna("").astype(str).str.strip().any():
            return out
        fitted = corpus_lib.fit(spec, texts)
    if not fitted.vocabulary:
        return out

    # Only the units this call is about are transformed. The statistics belong
    # to the run; the vectors belong to this batch.
    wanted = pd.unique(
        pd.concat([pairs["unit_id_l"], pairs["unit_id_r"]]).astype(str)
    )
    subset = units[units["unit_id"].astype(str).isin(set(wanted))]
    if not len(subset):
        return out
    matrix = fitted.transform(subset[column].fillna("").astype(str))
    position = {uid: i for i, uid in enumerate(subset["unit_id"].astype(str))}
    left = pairs["unit_id_l"].astype(str).map(position)
    right = pairs["unit_id_r"].astype(str).map(position)
    known = left.notna() & right.notna()
    if not known.any():
        return out
    rows = matrix[left[known].astype(int).to_numpy()]
    cols = matrix[right[known].astype(int).to_numpy()]
    out[known] = np.asarray(rows.multiply(cols).sum(axis=1)).ravel()
    return out


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def build(pairs: pd.DataFrame, units: pd.DataFrame,
          events: pd.DataFrame | None = None, references: dict | None = None,
          track: str = "person") -> pd.DataFrame:
    """One feature row per pair, in *pairs*' own order and index."""
    wanted = metadata(track)
    if not len(pairs):
        return pd.DataFrame(
            {f.name: pd.Series(dtype="float64") for f in wanted}, index=pairs.index
        )

    def col(name, side):
        return _text(_side(units, pairs, name, side))

    out = pd.DataFrame(index=pairs.index)
    sets = _company_sets(units, events)
    shared = _shared_counts(pairs, sets)

    if track == "person":
        surname_l, surname_r = col("surname_clean", "l"), col("surname_clean", "r")
        if surname_l.isna().all():
            surname_l, surname_r = col("surname", "l"), col("surname", "r")
        fore_l, fore_r = col("forename_canon", "l"), col("forename_canon", "r")
        middle_l, middle_r = col("middle_clean", "l"), col("middle_clean", "r")
        if middle_l.isna().all():
            middle_l, middle_r = col("middle_name", "l"), col("middle_name", "r")

        out["surname_similarity"] = _jaro_winkler(surname_l, surname_r)
        out["forename_similarity"] = _jaro_winkler(fore_l, fore_r)
        out["middle_similarity"] = _jaro_winkler(middle_l, middle_r)
        out["middle_conflict"] = _differs(middle_l, middle_r)

        full_l = (fore_l.fillna("") + " " + surname_l.fillna("")).str.strip()
        full_r = (fore_r.fillna("") + " " + surname_r.fillna("")).str.strip()
        out["name_fingerprint_equal"] = (
            (_fingerprint(full_l) == _fingerprint(full_r)) & full_l.ne("")
        ).astype(float)

        table = (references or {}).get(refs.UK_NAME_FREQUENCIES)
        same_surname = out["surname_similarity"] >= 1.0
        surname_freq = _log_frequency(surname_l, table, "name", "count")
        out["surname_log_frequency"] = surname_freq.where(same_surname)
        same_full = same_surname & (out["forename_similarity"] >= 1.0)
        out["full_name_log_frequency"] = _log_frequency(
            full_l, table, "name", "count"
        ).where(same_full)

        year_l = pd.to_numeric(_side(units, pairs, "dob_year", "l"), errors="coerce")
        year_r = pd.to_numeric(_side(units, pairs, "dob_year", "r"), errors="coerce")
        month_l = pd.to_numeric(_side(units, pairs, "dob_month", "l"), errors="coerce")
        month_r = pd.to_numeric(_side(units, pairs, "dob_month", "r"), errors="coerce")
        out["dob_month_equal"] = _equal(month_l, month_r)
        out["dob_year_gap"] = (year_l - year_r).abs()
        out["dob_missing"] = (year_l.isna() | year_r.isna()).astype(float)

        out["nationality_equal"] = _equal(col("nationality_norm", "l"),
                                          col("nationality_norm", "r"))
        if out["nationality_equal"].isna().all():
            out["nationality_equal"] = _equal(col("nationality", "l"),
                                              col("nationality", "r"))
        out["residence_equal"] = _equal(col("residence_norm", "l"),
                                        col("residence_norm", "r"))
        if out["residence_equal"].isna().all():
            out["residence_equal"] = _equal(col("country_of_residence", "l"),
                                            col("country_of_residence", "r"))

        out["postcode_equal"] = _equal(col("postcode_clean", "l"),
                                       col("postcode_clean", "r"))
        out["postcode_district_equal"] = _equal(col("postcode_district", "l"),
                                                col("postcode_district", "r"))
        out["address_similarity"] = _jaro_winkler(col("address_line_1", "l"),
                                                  col("address_line_1", "r"))

        out["same_company"] = (shared > 0).astype(float)
        out["shared_companies"] = shared
        out["shared_co_controller"] = _co_controller(pairs, sets)
        notified_l = pd.to_datetime(_side(units, pairs, "notified_on", "l"),
                                    errors="coerce")
        notified_r = pd.to_datetime(_side(units, pairs, "notified_on", "r"),
                                    errors="coerce")
        out["notified_gap_days"] = (notified_l - notified_r).dt.days.abs()

    else:
        core_l, core_r = col("name_core", "l"), col("name_core", "r")
        out["name_core_similarity"] = _jaro_winkler(core_l, core_r)
        out["name_tfidf_cosine"] = _tfidf_cosine(pairs, units, "name_core",
                                                 references)
        out["name_fingerprint_equal"] = (
            (_fingerprint(core_l) == _fingerprint(core_r)) & core_l.notna()
        ).astype(float)

        regnum_l, regnum_r = col("regnum_clean", "l"), col("regnum_clean", "r")
        if regnum_l.isna().all():
            regnum_l, regnum_r = col("registration_number", "l"), col("registration_number", "r")
        out["regnum_equal"] = _equal(regnum_l, regnum_r)
        out["regnum_conflict"] = _differs(regnum_l, regnum_r)
        out["regnum_one_sided"] = (regnum_l.notna() ^ regnum_r.notna()).astype(float)

        country_l, country_r = col("country_canonical", "l"), col("country_canonical", "r")
        if country_l.isna().all():
            country_l, country_r = col("country_registered", "l"), col("country_registered", "r")
        out["country_equal"] = _equal(country_l, country_r)
        out["country_differs"] = _differs(country_l, country_r)

        form_l, form_r = col("legal_form_clean", "l"), col("legal_form_clean", "r")
        if form_l.isna().all():
            form_l, form_r = col("legal_form", "l"), col("legal_form", "r")
        out["legal_form_equal"] = _equal(form_l, form_r)
        out["legal_form_differs"] = _differs(form_l, form_r)

        out["numeric_suffix_differs"] = _differs(col("numeric_suffix", "l"),
                                                 col("numeric_suffix", "r"))
        out["subject_differs"] = _differs(col("subject_phrase", "l"),
                                          col("subject_phrase", "r"))

        # deduping fires this one only on a near-identical base name, because
        # "X HOLDINGS" and "Y HOLDINGS" tell you nothing.
        holdings_l = col("name_clean", "l").fillna("").str.contains(r"\bHOLDINGS?\b")
        holdings_r = col("name_clean", "r").fillna("").str.contains(r"\bHOLDINGS?\b")
        near = out["name_core_similarity"] > 0.95
        asymmetric = (holdings_l != holdings_r).astype(float)
        out["holdings_asymmetry"] = asymmetric.where(near)

        out["house_number_differs"] = _differs(
            _house_number(col("address_line_1", "l")),
            _house_number(col("address_line_1", "r")),
        )

        out["postcode_equal"] = _equal(col("postcode_clean", "l"),
                                       col("postcode_clean", "r"))
        out["postcode_district_equal"] = _equal(col("postcode_district", "l"),
                                                col("postcode_district", "r"))
        out["same_company"] = (shared > 0).astype(float)
        out["shared_companies"] = shared

    size_l = pd.to_numeric(_side(units, pairs, "unit_size", "l"), errors="coerce")
    size_r = pd.to_numeric(_side(units, pairs, "unit_size", "r"), errors="coerce")
    out["unit_size_min"] = np.minimum(size_l, size_r)
    out["unit_size_max"] = np.maximum(size_l, size_r)

    # A feature the frame could not build is null, never missing: the model
    # reads a column that is not there as null anyway, and this keeps the
    # explanation screen honest about which ones ran.
    for feature in wanted:
        if feature.name not in out.columns:
            out[feature.name] = np.nan
    return out[[f.name for f in wanted]].astype("float64")

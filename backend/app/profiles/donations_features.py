# backend/app/profiles/donations_features.py
"""The donations profile's pair features (`docs/MODEL.md`).

Two feature sets, one per track, plus four groups both tracks share: who the
money went to, when it was given, how big the gifts were, and how many records
each side pools.

Everything is built in DuckDB from whole frames. A pair never touches Python:
name similarity is DuckDB's `jaro_winkler_similarity`, set overlaps are list
operations over split strings, and the shape of a donor's giving is reduced to
five numbers per **unit** before any pair is looked at, so a 20-fold bigger pairs
file costs a bigger join and not 20 times the work. The one exception is the
organisation TF-IDF cosine, which is a sparse matrix product over the units in
the pairs file — still no loop.

What each feature means in plain words lives in `PERSON_FEATURES` and
`ORGANISATION_FEATURES`, which the model panel and the per-pair explanation both
read.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.model.features import Feature
from app.model import corpus as corpus_lib
from app.model import references as refs

# The donations profile joins multi-valued cells with this; `units.parquet`
# carries them the same way.
SEPARATOR = " | "

# Quantiles of a unit's donation sizes. Five points describe "always £10,000"
# against "a scatter of odd amounts" well enough to compare two donors, and are
# cheap enough to compute for every unit in one pass.
AMOUNT_QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)

# Titles that carry a gender, for the conflict feature. A title outside this map
# (DR, PROF, CLLR, LORD-as-a-peerage) says nothing about gender, so it makes no
# conflict either way.
TITLE_GENDER = {
    "MR": "M", "SIR": "M", "LORD": "M", "BARON": "M", "VISCOUNT": "M", "EARL": "M",
    "MRS": "F", "MISS": "F", "MS": "F", "MX": None, "LADY": "F", "DAME": "F",
    "BARONESS": "F",
}

# How many organisation name tokens the TF-IDF space keeps. Donations
# organisation names are short; 20,000 tokens covers every word in the sheet
# several times over and keeps the matrix small.
TFIDF_MAX_FEATURES = 20000

#: The corpus statistics the organisation name feature needs, declared rather
#: than fitted on the fly (`app/model/corpus.py`).
#:
#: The scope is the whole RUN, not the organisation track, because that is what
#: this builder has always done — it is handed the whole units frame and fits
#: over all of it, person units included, which on the real donations run adds
#: 13,310 empty documents to 9,065 real ones and lifts every IDF. Narrowing it
#: to the track would be an improvement and would move every stored feature
#: value in the trained model, so it is a change to make deliberately with a
#: retrain, not a side effect of moving the fit. `docs/MODEL.md` records it.
NAME_CORE_TFIDF = corpus_lib.CorpusSpec(
    name="name_core_tfidf", column="name_core", scope="run",
    max_features=TFIDF_MAX_FEATURES, analyzer="word",
    token_pattern=r"[^\s]+", lowercase=False, norm="l2",
)

CORPUS_SPECS = {"organisation": [NAME_CORE_TFIDF]}


def corpus_specs(track: str) -> list:
    """What this builder needs fitted over the units before it can score."""
    return list(CORPUS_SPECS.get(track, []))


# ---------------------------------------------------------------------------
# Kind of donor (D13c)
# ---------------------------------------------------------------------------

# Reviewers do not judge every donor the same way (D13c, from Steve Goodrich via
# Tom): a name settles a trade union and settles nothing about a company; a
# public fund is read on the nature of the donation; a company on its number and
# its postcode. The model cannot learn any of that while every organisation
# looks alike to it, so the kind of donor is a feature — a **category**, not a
# number, because there is no order in which a trade union sits between a
# company and a trust.
#
# The code is the index into this tuple, and it is part of the model's contract:
# every version writes the list into its own `features.json`, so re-ordering
# this one cannot silently re-map an older version's codes.
STATUS_KINDS = (
    "kinds differ",                   # 0 — the two sides are not the same kind
    "unknown",                        # 1
    "individual",                     # 2
    "public fund",                    # 3
    "company or registered body",     # 4
    "trade union",                    # 5
    "association, trust or other",    # 6
    "registered political party",     # 7
)
_KIND_CODE = {name: index for index, name in enumerate(STATUS_KINDS)}
MIXED_KIND = _KIND_CODE["kinds differ"]
UNKNOWN_KIND = _KIND_CODE["unknown"]

# D13c's evidence groups, written as the donor statuses this sheet carries. A
# company, an LLP, a friendly society and a building society are all judged on a
# registration number and a postcode, so they are one kind. An unincorporated
# association, a trust and "other" are all judged on postcode, amount and date,
# so they are another. A registered political party is in neither of D13c's
# lists, so it gets its own code rather than being quietly folded into one.
STATUS_TO_KIND = {
    "individual": "individual",
    "public fund": "public fund",
    "company": "company or registered body",
    "limited liability partnership": "company or registered body",
    "llp": "company or registered body",
    "friendly society": "company or registered body",
    "building society": "company or registered body",
    "trade union": "trade union",
    "unincorporated association": "association, trust or other",
    "trust": "association, trust or other",
    "other": "association, trust or other",
    "impermissible donor": "association, trust or other",
    "registered political party": "registered political party",
}


def kind_of(status) -> str:
    """The D13c kind one donor status belongs to, in plain words."""
    text = str(status or "").strip().lower()
    return STATUS_TO_KIND.get(text, "unknown")


def kind_code(status) -> int:
    return _KIND_CODE[kind_of(status)]


# ---------------------------------------------------------------------------
# Feature metadata
# ---------------------------------------------------------------------------

_RECIPIENT_FEATURES = [
    Feature("recipient_jaccard", "Share of recipients in common", "recipients", 1,
            null_when="either side has no recipient recorded", render="similarity"),
    Feature("same_local_unit", "Gave through the same local unit", "recipients", 1,
            null_when="either side has no local unit recorded", render="flag"),
    Feature("single_party_conflict", "Each gave to one party, and not the same one",
            "recipients", -1, null_when="either side has no recipient recorded", render="flag"),
]

_TIMING_FEATURES = [
    Feature("year_gap", "Years between the two giving periods", "timing", -1,
            null_when="either side has no dated donation", render="years"),
    Feature("years_overlap", "The two giving periods overlap", "timing", 1,
            null_when="either side has no dated donation", render="flag"),
]

_AMOUNT_FEATURES = [
    Feature("median_value_ratio", "Typical gift, smaller over larger", "amounts", 1,
            null_when="either side has no median gift", render="similarity"),
    Feature("modal_value_equal", "Same most common gift", "amounts", 1,
            null_when="either side has no most common gift", render="flag"),
    Feature("shared_exact_amounts", "Exact amounts both of them gave", "amounts", 1,
            null_when="either side has no recorded amounts", render="count"),
    Feature("round_share_difference", "Gap in how often they give round thousands",
            "amounts", -1, null_when="either side has no recorded amounts", render="share"),
    Feature("amount_distribution_distance", "Distance between their gift-size patterns",
            "amounts", -1, null_when="either side has no individual donations",
            render="log_ratio"),
    Feature("both_single_donation", "Both gave only once", "amounts", 0, render="flag"),
]

_SIZE_FEATURES = [
    Feature("log_unit_size_min", "Records on the smaller side (log)", "size", 0, render="log_records"),
    Feature("log_unit_size_max", "Records on the larger side (log)", "size", 0, render="log_records"),
]

# The kind of donor, on both tracks (D13c). It sits in a group of its own rather
# than under `identifiers`, because the ablation table is exactly where "does
# knowing the kind of donor help" gets answered, and burying it inside another
# group would hide the answer.
_KIND_FEATURE = Feature(
    "status_kind", "Kind of donor", "kind", 0,
    null_when=None, categories=STATUS_KINDS, render="category",
)

PERSON_FEATURES: list[Feature] = [
    Feature("name_jaro_winkler", "Whole name similarity", "name", 1,
            null_when="either side has no cleaned name", render="similarity"),
    Feature("forename_exact", "Same forename", "name", 1,
            null_when="either side has no forename", render="flag"),
    Feature("forename_initial_of_other", "One forename is the other's initial", "name", 1,
            null_when="either side has no forename", render="flag"),
    Feature("forename_canon_equal", "Same forename once nicknames are mapped", "name", 1,
            null_when="either side has no forename", render="flag"),
    Feature("middle_initial_agrees", "Middle initials agree", "name", 1,
            null_when="either side has no middle name", render="flag"),
    Feature("middle_initial_conflicts", "Middle initials disagree", "name", -1,
            null_when="either side has no middle name", render="flag"),
    Feature("title_gender_conflict", "Titles point to different genders", "name", -1,
            null_when="either side has no title that carries a gender", render="flag"),
    Feature("post_nominals_agree", "Share a post-nominal", "name", 1,
            null_when="either side has no post-nominals", render="flag"),
    Feature("surname_log_frequency", "How common the surname is in the UK", "rarity", -1,
            null_when="either side has no surname, or the UK name table is missing", render="log_people"),
    Feature("full_name_log_frequency", "How common the whole name is in the UK",
            "rarity", -1,
            null_when="either side is missing a name part, or the UK name table is missing", render="log_people"),
    *_RECIPIENT_FEATURES,
    *_TIMING_FEATURES,
    *_AMOUNT_FEATURES,
    _KIND_FEATURE,
    *_SIZE_FEATURES,
]

ORGANISATION_FEATURES: list[Feature] = [
    Feature("name_core_jaro_winkler", "Name similarity, legal form set aside", "name", 1,
            null_when="either side has no cleaned name", render="similarity"),
    Feature("name_core_token_jaccard", "Share of name words in common", "name", 1,
            null_when="either side has no cleaned name", render="similarity"),
    Feature("name_tfidf_cosine", "Name similarity weighted by how rare each word is",
            "name", 1, null_when="either side has no cleaned name", render="similarity"),
    Feature("postcode_equal", "Same postcode", "address", 1,
            null_when="either side has no usable postcode", render="flag"),
    Feature("postcode_district_equal", "Same postcode district", "address", 1,
            null_when="either side has no usable postcode", render="flag"),
    Feature("company_number_equal", "Same company number", "identifiers", 1,
            null_when="either side has no company number", render="flag"),
    Feature("company_number_conflicts", "Different company numbers", "identifiers", -1,
            null_when="either side has no company number", render="flag"),
    Feature("company_number_one_sided", "Only one side has a company number",
            "identifiers", 0, render="flag"),
    Feature("legal_form_equal", "Same legal form", "identifiers", 1,
            null_when="either side has no legal form in its name", render="flag"),
    Feature("donor_status_std_equal", "Same standardised donor status", "identifiers", 1,
            null_when="either side has no standardised status", render="flag"),
    *_RECIPIENT_FEATURES,
    Feature("nature_overlap", "Share of donation natures in common", "nature", 1,
            null_when="either side has no donation with a recorded nature", render="similarity"),
    *_TIMING_FEATURES,
    *_AMOUNT_FEATURES,
    _KIND_FEATURE,
    *_SIZE_FEATURES,
]

FEATURES_BY_TRACK = {
    "person": PERSON_FEATURES,
    "organisation": ORGANISATION_FEATURES,
}

REFERENCES = [
    refs.Reference(
        key=refs.UK_NAME_FREQUENCIES,
        label="UK name frequencies",
        description=(
            "Forename and surname counts over about 7.5 million UK individuals "
            "from the PSC register. Built by backend/scripts/build_name_frequencies.py. "
            "Rarity has to come from outside the donations sheet (D12): inside it, "
            "a repeat donor gets a fresh DonorId, so a frequent name usually means "
            "one frequent donor."
        ),
        features=("surname_log_frequency", "full_name_log_frequency"),
    )
]


def metadata(track: str) -> list[Feature]:
    return list(FEATURES_BY_TRACK.get(track, []))


# ---------------------------------------------------------------------------
# Inputs the SQL needs
# ---------------------------------------------------------------------------

_SHARED_UNIT_COLUMNS = (
    "unit_id", "unit_size", "parties", "units", "first_year", "last_year",
    "n_donations", "median_value", "modal_value", "share_round_1000", "top_values",
    # The kind of donor is a feature on both tracks (D13c). The standardised
    # status is the one to read; the raw one is the fallback for a run made
    # before the derived-column rules existed.
    "donor_status_std", "donor_status",
)
_PERSON_UNIT_COLUMNS = (
    "name_clean", "forename", "forename_canon", "middle_names", "surname",
    "title", "post_nominals",
)
_ORGANISATION_UNIT_COLUMNS = (
    "name_core", "postcode_clean", "postcode_district", "company_number_clean",
    "legal_form",
)


def _unit_frame(units: pd.DataFrame, track: str) -> pd.DataFrame:
    """The unit columns this track's features read, as plain types DuckDB likes.

    Missing columns are added as nulls, so a cut-down run — a profile that never
    produced `donor_status_std`, say — builds the same feature row with that one
    feature blank instead of failing.
    """
    wanted = list(_SHARED_UNIT_COLUMNS)
    wanted += list(_PERSON_UNIT_COLUMNS if track == "person" else _ORGANISATION_UNIT_COLUMNS)
    out = pd.DataFrame(index=range(len(units)))
    for column in wanted:
        if column in units.columns:
            values = units[column].reset_index(drop=True)
        else:
            values = pd.Series([None] * len(units), dtype="object")
        out[column] = values
    out["unit_id"] = out["unit_id"].astype(str)
    for column in ("unit_size", "first_year", "last_year", "n_donations",
                   "median_value", "modal_value", "share_round_1000"):
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("float64")
    for column in out.columns:
        if out[column].dtype == "object":
            out[column] = out[column].astype("object").where(out[column].notna(), None)
    return out


def _quantile_frame(events: pd.DataFrame | None) -> pd.DataFrame:
    """Five points on each unit's gift-size curve, on a log scale.

    A log scale because £500 against £1,000 is the same kind of difference as
    £50,000 against £100,000, and a reviewer reads it that way. Built once per
    unit; the pair features then only subtract.
    """
    columns = ["unit_id"] + [f"q{i + 1}" for i in range(len(AMOUNT_QUANTILES))]
    empty = pd.DataFrame({c: pd.Series(dtype="object" if c == "unit_id" else "float64")
                          for c in columns})
    if events is None or not len(events) or "unit_id" not in events.columns:
        return empty
    values = pd.to_numeric(events["value"], errors="coerce")
    keep = values.notna() & (values > 0)
    if not keep.any():
        return empty

    import duckdb

    frame = pd.DataFrame({
        "unit_id": events.loc[keep, "unit_id"].astype(str).to_numpy(),
        "value": values[keep].to_numpy(),
    })
    con = duckdb.connect()
    con.register("event_rows", frame)
    picks = ", ".join(
        f"quantiles[{i + 1}] AS q{i + 1}" for i in range(len(AMOUNT_QUANTILES))
    )
    out = con.execute(
        f"""SELECT unit_id, {picks} FROM (
                SELECT unit_id,
                       quantile_cont(log10(value + 1), {list(AMOUNT_QUANTILES)}) AS quantiles
                FROM event_rows GROUP BY unit_id)"""
    ).fetch_df()
    con.close()
    return out[columns]


def _nature_frame(events: pd.DataFrame | None) -> pd.DataFrame:
    """The distinct natures of each unit's donations, as one list per unit.

    D13c: a public fund is judged on what the donation was *for* — Short Money,
    a policy development grant, premises — far more than on its name. Two units
    that both give premises and staff costs are a different proposition from one
    that gives premises and one that gives auction prizes.

    Built once per unit, so the pair feature is a list intersection and nothing
    walks an event.
    """
    empty = pd.DataFrame({"unit_id": pd.Series(dtype="object"),
                          "natures": pd.Series(dtype="object")})
    if events is None or not len(events) or "unit_id" not in events.columns \
            or "nature" not in events.columns:
        return empty
    values = events["nature"].astype("object")
    keep = values.notna() & (values.astype(str).str.strip() != "")
    if not keep.any():
        return empty

    import duckdb

    frame = pd.DataFrame({
        "unit_id": events.loc[keep, "unit_id"].astype(str).to_numpy(),
        "nature": values[keep].astype(str).to_numpy(),
    })
    con = duckdb.connect()
    con.register("nature_rows", frame)
    out = con.execute(
        """SELECT unit_id, list_distinct(list(nature)) AS natures
           FROM nature_rows GROUP BY unit_id"""
    ).fetch_df()
    con.close()
    return out


def _tfidf_cosine(pairs: pd.DataFrame, units: pd.DataFrame,
                  references: dict | None = None) -> np.ndarray:
    """Cosine similarity of the two names in TF-IDF space, one number per pair.

    The word weights are fitted over **every unit in the frame**, not over the
    units this batch happens to name. That is what makes the feature stable: the
    value for one pair has to be the same whether it was built with a million
    others or on its own for a per-pair explanation. It also makes CLUB and
    LIMITED cheap and ZYTHUM expensive against the whole sheet, which is the
    point of the feature (`MODEL.md`).

    The similarity itself is one sparse element-wise product and a row sum, so
    no pair is visited in Python.
    """
    if not len(pairs):
        return np.zeros(0, dtype="float64")

    names = units.set_index(units["unit_id"].astype(str))["name_core"] \
        if "name_core" in units.columns else pd.Series(dtype="object")
    names = names[~names.index.duplicated()]
    known = names.notna() & (names.astype(str).str.strip() != "")
    left = pairs["unit_id_l"].astype(str).to_numpy()
    right = pairs["unit_id_r"].astype(str).to_numpy()
    if not known.any():
        return np.full(len(pairs), np.nan)

    fitted = corpus_lib.from_references(references, NAME_CORE_TFIDF.name)
    if fitted is None:
        fitted = corpus_lib.fit(NAME_CORE_TFIDF, names)
    matrix = fitted.transform(names.fillna("").astype(str).to_numpy())
    position = pd.Series(np.arange(len(names)), index=names.index)
    li = position.reindex(left).to_numpy()
    ri = position.reindex(right).to_numpy()
    present = ~(pd.isna(li) | pd.isna(ri))
    cosine = np.full(len(pairs), np.nan)
    if present.any():
        rows_l = li[present].astype(int)
        rows_r = ri[present].astype(int)
        values = np.asarray(matrix[rows_l].multiply(matrix[rows_r]).sum(axis=1)).ravel()
        usable = known.to_numpy()[rows_l] & known.to_numpy()[rows_r]
        values = np.where(usable, values, np.nan)
        cosine[present] = values
    return cosine.astype("float64")


def _name_frequency_frames(references: dict | None):
    """``(surname counts, full-name counts, whether the table is there at all)``."""
    table = (references or {}).get(refs.UK_NAME_FREQUENCIES)
    forenames, surnames, full = refs.name_frequency_parts(table)
    return surnames, full, table is not None and bool(len(table))


# ---------------------------------------------------------------------------
# The SQL
# ---------------------------------------------------------------------------

# Splitting a multi-valued cell, guarding the empty string that str_split leaves
# behind on a blank.
def _tokens(expression: str, separator: str = SEPARATOR) -> str:
    return f"list_distinct(str_split({expression}, '{separator}'))"


_SHARED_SQL = f"""
    -- recipients
    CASE WHEN l.parties IS NULL OR r.parties IS NULL THEN NULL ELSE
        len(list_intersect({_tokens('l.parties')}, {_tokens('r.parties')}))::DOUBLE
        / nullif(len(list_distinct(list_concat({_tokens('l.parties')},
                                               {_tokens('r.parties')}))), 0)
    END AS recipient_jaccard,
    CASE WHEN l.units IS NULL OR r.units IS NULL THEN NULL
         WHEN len(list_intersect({_tokens('l.units')}, {_tokens('r.units')})) > 0 THEN 1.0
         ELSE 0.0 END AS same_local_unit,
    CASE WHEN l.parties IS NULL OR r.parties IS NULL THEN NULL
         WHEN len({_tokens('l.parties')}) = 1 AND len({_tokens('r.parties')}) = 1
              AND l.parties <> r.parties THEN 1.0
         ELSE 0.0 END AS single_party_conflict,

    -- timing
    CASE WHEN l.first_year IS NULL OR r.first_year IS NULL
              OR l.last_year IS NULL OR r.last_year IS NULL THEN NULL
         ELSE greatest(0.0, greatest(l.first_year, r.first_year)
                            - least(l.last_year, r.last_year)) END AS year_gap,
    CASE WHEN l.first_year IS NULL OR r.first_year IS NULL
              OR l.last_year IS NULL OR r.last_year IS NULL THEN NULL
         WHEN greatest(l.first_year, r.first_year)
              <= least(l.last_year, r.last_year) THEN 1.0
         ELSE 0.0 END AS years_overlap,

    -- amounts (D13a)
    CASE WHEN l.median_value IS NULL OR r.median_value IS NULL
              OR l.median_value <= 0 OR r.median_value <= 0 THEN NULL
         ELSE least(l.median_value, r.median_value)
              / greatest(l.median_value, r.median_value) END AS median_value_ratio,
    CASE WHEN l.modal_value IS NULL OR r.modal_value IS NULL THEN NULL
         WHEN l.modal_value = r.modal_value THEN 1.0 ELSE 0.0 END AS modal_value_equal,
    CASE WHEN l.top_values IS NULL OR r.top_values IS NULL THEN NULL
         ELSE len(list_intersect({_tokens('l.top_values')},
                                 {_tokens('r.top_values')}))::DOUBLE
    END AS shared_exact_amounts,
    CASE WHEN l.share_round_1000 IS NULL OR r.share_round_1000 IS NULL THEN NULL
         ELSE abs(l.share_round_1000 - r.share_round_1000) END AS round_share_difference,
    CASE WHEN ql.q1 IS NULL OR qr.q1 IS NULL THEN NULL
         ELSE (abs(ql.q1 - qr.q1) + abs(ql.q2 - qr.q2) + abs(ql.q3 - qr.q3)
               + abs(ql.q4 - qr.q4) + abs(ql.q5 - qr.q5)) / 5.0
    END AS amount_distribution_distance,
    CASE WHEN l.n_donations IS NULL OR r.n_donations IS NULL THEN NULL
         WHEN l.n_donations = 1 AND r.n_donations = 1 THEN 1.0
         ELSE 0.0 END AS both_single_donation,

    -- kind of donor (D13c). A category, not a number: the code of the kind both
    -- sides share, or "kinds differ" when they do not. The standardised status
    -- decides it, falling back to the raw one.
    CASE WHEN coalesce(kl.code, {UNKNOWN_KIND}) = coalesce(kr.code, {UNKNOWN_KIND})
         THEN coalesce(kl.code, {UNKNOWN_KIND})::DOUBLE
         ELSE {MIXED_KIND}::DOUBLE END AS status_kind,

    -- size
    log10(1 + least(l.unit_size, r.unit_size)) AS log_unit_size_min,
    log10(1 + greatest(l.unit_size, r.unit_size)) AS log_unit_size_max
"""

# Both tracks join the status-kind lookup; only organisations join the natures.
_KIND_JOINS = """
    LEFT JOIN status_kind_map kl
           ON lower(trim(coalesce(l.donor_status_std, l.donor_status))) = kl.status
    LEFT JOIN status_kind_map kr
           ON lower(trim(coalesce(r.donor_status_std, r.donor_status))) = kr.status
"""

# `{rarity}` is filled in at build time: the rarity block below, or the null one,
# depending on whether the reference table is there. Everything else is fixed.
_PERSON_SQL = """
    CASE WHEN l.name_clean IS NULL OR r.name_clean IS NULL THEN NULL
         ELSE jaro_winkler_similarity(l.name_clean, r.name_clean)
    END AS name_jaro_winkler,
    CASE WHEN l.forename IS NULL OR r.forename IS NULL THEN NULL
         WHEN l.forename = r.forename THEN 1.0 ELSE 0.0 END AS forename_exact,
    CASE WHEN l.forename IS NULL OR r.forename IS NULL THEN NULL
         WHEN (length(l.forename) = 1 OR length(r.forename) = 1)
              AND substr(l.forename, 1, 1) = substr(r.forename, 1, 1) THEN 1.0
         ELSE 0.0 END AS forename_initial_of_other,
    CASE WHEN l.forename_canon IS NULL OR r.forename_canon IS NULL THEN NULL
         WHEN l.forename_canon = r.forename_canon THEN 1.0
         ELSE 0.0 END AS forename_canon_equal,
    CASE WHEN l.middle_names IS NULL OR r.middle_names IS NULL THEN NULL
         WHEN substr(l.middle_names, 1, 1) = substr(r.middle_names, 1, 1) THEN 1.0
         ELSE 0.0 END AS middle_initial_agrees,
    CASE WHEN l.middle_names IS NULL OR r.middle_names IS NULL THEN NULL
         WHEN substr(l.middle_names, 1, 1) <> substr(r.middle_names, 1, 1) THEN 1.0
         ELSE 0.0 END AS middle_initial_conflicts,
    CASE WHEN gl.gender IS NULL OR gr.gender IS NULL THEN NULL
         WHEN gl.gender <> gr.gender THEN 1.0 ELSE 0.0 END AS title_gender_conflict,
    CASE WHEN l.post_nominals IS NULL OR r.post_nominals IS NULL THEN NULL
         WHEN len(list_intersect(str_split(l.post_nominals, ' '),
                                 str_split(r.post_nominals, ' '))) > 0 THEN 1.0
         ELSE 0.0 END AS post_nominals_agree,
{rarity}
""" + _SHARED_SQL

# Rarity (D12). The commoner of the two spellings governs how easily two
# different people could share this evidence, so it is the one that counts. A
# name the table has never seen counts zero, which reads as the rarest there is —
# that is a real answer, and quite different from the answer below.
_RARITY_SQL = """
    CASE WHEN l.surname IS NULL OR r.surname IS NULL THEN NULL
         ELSE log10(1 + greatest(coalesce(sl.n, 0), coalesce(sr.n, 0)))
    END AS surname_log_frequency,
    CASE WHEN l.surname IS NULL OR r.surname IS NULL
              OR l.forename IS NULL OR r.forename IS NULL THEN NULL
         ELSE log10(1 + greatest(coalesce(fl.n, 0), coalesce(fr.n, 0)))
    END AS full_name_log_frequency,
"""

# With no reference table there is nothing to be rare against, so both features
# are null rather than zero (`MODEL.md`). The training report then says which
# table was missing, and the model treats the column as "no evidence" rather
# than "the rarest name in Britain".
_RARITY_ABSENT_SQL = """
    CAST(NULL AS DOUBLE) AS surname_log_frequency,
    CAST(NULL AS DOUBLE) AS full_name_log_frequency,
"""

_ORGANISATION_SQL = f"""
    CASE WHEN l.name_core IS NULL OR r.name_core IS NULL THEN NULL
         ELSE jaro_winkler_similarity(l.name_core, r.name_core)
    END AS name_core_jaro_winkler,
    CASE WHEN l.name_core IS NULL OR r.name_core IS NULL THEN NULL
         ELSE len(list_intersect({_tokens('l.name_core', ' ')},
                                 {_tokens('r.name_core', ' ')}))::DOUBLE
              / nullif(len(list_distinct(list_concat({_tokens('l.name_core', ' ')},
                                                     {_tokens('r.name_core', ' ')}))), 0)
    END AS name_core_token_jaccard,
    CASE WHEN l.postcode_clean IS NULL OR r.postcode_clean IS NULL THEN NULL
         WHEN l.postcode_clean = r.postcode_clean THEN 1.0 ELSE 0.0 END AS postcode_equal,
    CASE WHEN l.postcode_district IS NULL OR r.postcode_district IS NULL THEN NULL
         WHEN l.postcode_district = r.postcode_district THEN 1.0
         ELSE 0.0 END AS postcode_district_equal,
    CASE WHEN l.company_number_clean IS NULL OR r.company_number_clean IS NULL THEN NULL
         WHEN l.company_number_clean = r.company_number_clean THEN 1.0
         ELSE 0.0 END AS company_number_equal,
    CASE WHEN l.company_number_clean IS NULL OR r.company_number_clean IS NULL THEN NULL
         WHEN l.company_number_clean <> r.company_number_clean THEN 1.0
         ELSE 0.0 END AS company_number_conflicts,
    CASE WHEN (l.company_number_clean IS NULL) <> (r.company_number_clean IS NULL)
         THEN 1.0 ELSE 0.0 END AS company_number_one_sided,
    CASE WHEN l.legal_form IS NULL OR r.legal_form IS NULL THEN NULL
         WHEN l.legal_form = r.legal_form THEN 1.0 ELSE 0.0 END AS legal_form_equal,
    CASE WHEN l.donor_status_std IS NULL OR r.donor_status_std IS NULL THEN NULL
         WHEN l.donor_status_std = r.donor_status_std THEN 1.0
         ELSE 0.0 END AS donor_status_std_equal,

    -- nature of donation (D13c)
    -- Cast both sides: a run with no usable nature rows registers an EMPTY
    -- pandas object column, which carries no type at all, so DuckDB gives it a
    -- scalar one and `list_distinct` refuses to bind. The cast makes the empty
    -- case a list of nothing rather than a binder error, and is a no-op when
    -- there are real rows.
    CASE WHEN nl.natures IS NULL OR nr.natures IS NULL THEN NULL
         ELSE len(list_intersect(CAST(nl.natures AS VARCHAR[]),
                                 CAST(nr.natures AS VARCHAR[])))::DOUBLE
              / nullif(len(list_distinct(list_concat(
                    CAST(nl.natures AS VARCHAR[]),
                    CAST(nr.natures AS VARCHAR[])))), 0)
    END AS nature_overlap,
{_SHARED_SQL}
"""

_NATURE_JOINS = """
    LEFT JOIN nature_rows nl ON p.unit_id_l = nl.unit_id
    LEFT JOIN nature_rows nr ON p.unit_id_r = nr.unit_id
"""

_PERSON_JOINS = """
    LEFT JOIN title_gender gl ON l.title = gl.title
    LEFT JOIN title_gender gr ON r.title = gr.title
    LEFT JOIN surname_frequency sl ON l.surname = sl.name
    LEFT JOIN surname_frequency sr ON r.surname = sr.name
    LEFT JOIN full_name_frequency fl
           ON l.forename = fl.forename AND l.surname = fl.surname
    LEFT JOIN full_name_frequency fr
           ON r.forename = fr.forename AND r.surname = fr.surname
"""


def build(
    pairs: pd.DataFrame,
    units: pd.DataFrame,
    events: pd.DataFrame | None = None,
    references: dict | None = None,
    track: str = "person",
) -> pd.DataFrame:
    """One feature row per pair, in *pairs*' own order and index."""
    wanted = metadata(track)
    if not len(pairs):
        return pd.DataFrame({f.name: pd.Series(dtype="float64") for f in wanted},
                            index=pairs.index)

    import duckdb

    keys = pd.DataFrame({
        "_row": np.arange(len(pairs)),
        "unit_id_l": pairs["unit_id_l"].astype(str).to_numpy(),
        "unit_id_r": pairs["unit_id_r"].astype(str).to_numpy(),
    })
    unit_rows = _unit_frame(units, track)
    quantile_rows = _quantile_frame(events)
    nature_rows = _nature_frame(events)
    kinds = pd.DataFrame([{"status": status, "code": _KIND_CODE[kind]}
                          for status, kind in STATUS_TO_KIND.items()])
    surnames, full_names, have_names = _name_frequency_frames(references)
    gender = pd.DataFrame(
        [{"title": t, "gender": g} for t, g in TITLE_GENDER.items() if g]
    )

    con = duckdb.connect()
    try:
        con.register("pair_rows", keys)
        con.register("unit_rows", unit_rows)
        con.register("quantile_rows", quantile_rows)
        con.register("nature_rows", nature_rows)
        con.register("status_kind_map", kinds)
        con.register("surname_frequency", surnames)
        con.register("full_name_frequency", full_names)
        con.register("title_gender", gender)
        if track == "person":
            columns = _PERSON_SQL.format(
                rarity=_RARITY_SQL if have_names else _RARITY_ABSENT_SQL
            )
            joins = _PERSON_JOINS + _KIND_JOINS
        else:
            columns = _ORGANISATION_SQL
            joins = _NATURE_JOINS + _KIND_JOINS
        out = con.execute(
            f"""SELECT p._row AS _row, {columns}
                FROM pair_rows p
                LEFT JOIN unit_rows l ON p.unit_id_l = l.unit_id
                LEFT JOIN unit_rows r ON p.unit_id_r = r.unit_id
                LEFT JOIN quantile_rows ql ON p.unit_id_l = ql.unit_id
                LEFT JOIN quantile_rows qr ON p.unit_id_r = qr.unit_id
                {joins}
                ORDER BY p._row"""
        ).fetch_df()
    finally:
        con.close()

    out = out.sort_values("_row").drop(columns=["_row"]).reset_index(drop=True)
    if track == "organisation":
        out["name_tfidf_cosine"] = _tfidf_cosine(pairs, units, references)

    frame = pd.DataFrame(index=pairs.index)
    for feature in wanted:
        values = out[feature.name].to_numpy() if feature.name in out.columns else np.nan
        frame[feature.name] = pd.Series(values, index=pairs.index, dtype="float64")
    return frame

# backend/app/model/features.py
"""The feature contract, and the features shared code builds for every profile.

`MODEL.md` splits a feature row in two. The **generic** half comes from
`pairs.parquet` and is the same in every profile: Splink's match weight and one
agreement level per comparison. The **profile** half comes from the profile's
own builder, which knows what a donation is.

A feature is described by `Feature`, never by a bare column name, because three
screens need more than the name: the model panel groups features, the per-pair
explanation prints them in plain words, and the trainer hands LightGBM one
monotone constraint per feature in the right order.

Nothing here loops over pairs. The generic features are column reads; the
profile builders use DuckDB joins and array operations.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Every group a feature may belong to, in the order the model panel shows them,
# with the words the panel prints. A profile may not invent a group: a reviewer
# reading two profiles' reports should meet the same headings.
GROUP_LABELS = {
    "splink": "Splink",
    "name": "Name",
    "rarity": "Name rarity",
    "recipients": "Recipients",
    "timing": "Timing",
    "amounts": "Amounts",
    "identifiers": "Identifiers",
    "address": "Address",
    "nature": "Nature of donation",
    "kind": "Kind of donor",
    "size": "Size",
}

GENERIC_GROUP = "splink"

# The column Splink writes its log-odds score into, and the prefix of its
# per-comparison agreement levels.
MATCH_WEIGHT_COLUMN = "match_weight"
GAMMA_PREFIX = "gamma_"


@dataclass(frozen=True)
class Feature:
    """One column of the feature row, and everything three screens need about it.

    ``monotone`` is handed straight to LightGBM: ``1`` means the feature may only
    push the score up, ``-1`` only down, ``0`` leaves it free. A constraint is
    worth setting only where the direction is a fact about the world — a rarer
    surname cannot be evidence *against* a match — because a wrong one costs
    accuracy silently.

    ``null_when`` is plain words for the one question a reviewer asks of a blank
    cell: why is there nothing here?

    ``categories`` makes the feature a **category** rather than a number:
    ``(code, words)`` pairs, in code order, and the built column holds the code.
    LightGBM is told which columns these are and splits them by set membership
    rather than by ``<=``, which is the only sound way to handle "kind of donor"
    — there is no order in which a trade union sits between a company and a
    trust. The list is stored with the model, so a later edit to it cannot
    silently re-map an older version's codes. A category may not carry a
    monotone constraint, and LightGBM refuses one, so ``monotone`` stays 0.

    ``render`` says how to put the value into words. A bare number beside a bar
    tells a reviewer nothing: 0.87 means one thing for a name similarity and
    another for the log of a count, and 1 means "yes" for a flag and "one
    record" for a count. The kinds are in ``RENDERINGS``.
    """

    name: str
    label: str
    group: str
    monotone: int = 0
    source: str = "profile"  # 'generic' | 'profile'
    null_when: str | None = None
    categories: tuple[str, ...] | None = None
    render: str = "number"

    @property
    def is_categorical(self) -> bool:
        return bool(self.categories)

    def category_label(self, code) -> str | None:
        """The words for one code, for a per-pair explanation."""
        if not self.categories or code is None:
            return None
        try:
            index = int(code)
        except (TypeError, ValueError):
            return None
        return self.categories[index] if 0 <= index < len(self.categories) else None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "group": self.group,
            "monotone": int(self.monotone),
            "source": self.source,
            "null_when": self.null_when,
            "categories": list(self.categories) if self.categories else None,
            "render": self.render,
        }

    def render_value(self, value, gamma_levels: dict | None = None) -> str:
        """This pair's value for this feature, in words a reviewer can read."""
        return render_value(self.as_dict(), value, gamma_levels)


# ---------------------------------------------------------------------------
# Putting a value into words
# ---------------------------------------------------------------------------

#: What each ``render`` kind means. The per-pair explanation puts one of these
#: beside every bar, because the number on its own is not readable: a reviewer
#: asked to judge "Whole name similarity: 0.87" can, and "log_unit_size_max:
#: 1.9957" is noise.
RENDERINGS = {
    "number": "two decimal places",
    "bits": "two decimal places and the word bits",
    "similarity": "0 to 1, two decimal places",
    "share": "0 to 1, as a percentage",
    "flag": "yes or no",
    "count": "a whole number",
    "years": "a number of years, or 'no gap'",
    "log_records": "back to a number of records",
    "log_people": "back to a number of people in the UK name table",
    "log_ratio": "back to how many times apart the two sides are",
    "category": "the words for the code, from `categories`",
    "gamma": "the Splink comparison level's own label",
}

# What a null reads as. The feature's own `null_when` says *why*; this says that.
NOTHING_TO_COMPARE = "nothing to compare"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def render_value(feature: dict, value, gamma_levels: dict | None = None) -> str:
    """One feature's value for one pair, in plain words.

    *feature* is a feature's metadata as ``Feature.as_dict`` returns it, so this
    works off a **stored** feature list and an old model is always read the way
    it was written. *gamma_levels* is ``{column: {gamma: {label, ...}}}`` from
    the run's saved Splink model, which is what turns ``gamma_surname = 2`` into
    "Jaro-Winkler ≥ 0.92".
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return NOTHING_TO_COMPARE
    kind = feature.get("render") or "number"
    number = float(value)

    if kind == "category":
        categories = feature.get("categories") or []
        index = int(number)
        return categories[index] if 0 <= index < len(categories) else f"code {index}"
    if kind == "gamma":
        return _gamma_words(feature.get("name", ""), number, gamma_levels)
    if kind == "flag":
        return "yes" if number else "no"
    if kind == "count":
        return f"{int(round(number)):,}"
    if kind == "years":
        years = int(round(number))
        return "no gap" if years == 0 else _plural(years, "year")
    if kind == "share":
        return f"{number:.0%}"
    if kind == "bits":
        return f"{number:.2f} bits"
    if kind == "log_records":
        return _plural(int(round(10 ** number - 1)), "record")
    if kind == "log_people":
        people = int(round(10 ** number - 1))
        return "not in the UK name table" if people <= 0 \
            else f"about {people:,} in the UK"
    if kind == "log_ratio":
        times = 10 ** number
        return "the same" if times < 1.05 else f"about {times:,.0f} times apart"
    return f"{number:.2f}"


def _gamma_words(column: str, value: float, gamma_levels: dict | None) -> str:
    """A Splink agreement level as the label the trained model gives it."""
    gamma = int(value)
    if gamma < 0:
        return "one side has no value"
    levels = (gamma_levels or {}).get(column[len(GAMMA_PREFIX):]) or {}
    level = levels.get(gamma) or levels.get(str(gamma)) or {}
    return level.get("label") or f"level {gamma}"


def categorical_names(features: list[Feature]) -> list[str]:
    """The feature names LightGBM must treat as categories, in column order."""
    return [f.name for f in features if f.is_categorical]


def group_summary(features: list[Feature]) -> list[dict]:
    """``[{key, label, n_features}]`` in ``GROUP_LABELS`` order, groups in use only."""
    counts: dict[str, int] = {}
    for feature in features:
        counts[feature.group] = counts.get(feature.group, 0) + 1
    ordered = [g for g in GROUP_LABELS if g in counts]
    ordered += [g for g in counts if g not in GROUP_LABELS]
    return [
        {"key": g, "label": GROUP_LABELS.get(g, g.replace("_", " ").title()),
         "n_features": counts[g]}
        for g in ordered
    ]


# ---------------------------------------------------------------------------
# The generic half
# ---------------------------------------------------------------------------


def gamma_columns(pairs: pd.DataFrame) -> list[str]:
    return sorted(c for c in pairs.columns if c.startswith(GAMMA_PREFIX))


def _gamma_label(column: str) -> str:
    """'gamma_forename_canon' -> 'Forename agreement level'."""
    words = column[len(GAMMA_PREFIX):].replace("_", " ")
    for suffix in (" clean", " canon", " core"):
        if words.endswith(suffix):
            words = words[: -len(suffix)]
    return f"{words[:1].upper()}{words[1:]} agreement level"


def generic_metadata(pairs: pd.DataFrame) -> list[Feature]:
    """The generic features of a pairs frame.

    A gamma is Splink's ordinal agreement level for one comparison: ``-1`` means
    a side was null, ``0`` the lowest level, and up from there. It is left
    unconstrained because that ordering is not monotone — null sits below
    "disagrees" by number but means "no evidence", not "evidence against".
    """
    features = [
        Feature(
            name=MATCH_WEIGHT_COLUMN,
            label="Splink match weight",
            group=GENERIC_GROUP,
            monotone=1,
            source="generic",
            null_when="the pair was forced in by a label and Splink never scored it",
            render="bits",
        )
    ]
    features += [
        Feature(name=c, label=_gamma_label(c), group=GENERIC_GROUP, monotone=0,
                source="generic", render="gamma")
        for c in gamma_columns(pairs)
    ]
    return features


def generic_features(pairs: pd.DataFrame) -> pd.DataFrame:
    """The generic feature columns, as floats, indexed like *pairs*."""
    out = pd.DataFrame(index=pairs.index)
    for feature in generic_metadata(pairs):
        if feature.name in pairs.columns:
            out[feature.name] = pd.to_numeric(pairs[feature.name], errors="coerce")
        else:
            out[feature.name] = np.nan
    return out.astype("float64")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build(
    pairs: pd.DataFrame,
    units: pd.DataFrame,
    track: str,
    events: pd.DataFrame | None = None,
    references: dict | None = None,
    profile=None,
) -> tuple[pd.DataFrame, list[Feature]]:
    """``(X, metadata)`` for one track's pairs.

    *pairs* is one track's rows of `pairs.parquet`. *units* is `units.parquet`,
    whole — the builder picks what it needs. *events* is the run's evidence rows
    carrying a ``unit_id`` column, or None. *references* is what
    ``references.load`` returned.

    The column order of *X* is the metadata order, and the trainer stores that
    order with the model, so a later scoring run cannot line the columns up
    wrongly.
    """
    if profile is None:
        from app.profiles import get_profile

        profile = get_profile()

    meta = generic_metadata(pairs)
    frame = generic_features(pairs)

    # `pairs.parquet` carries every track's gammas side by side, so a person
    # pair has an empty `gamma_postcode_clean` and an organisation pair an empty
    # `gamma_surname`. A gamma with no value anywhere in this track is a comparison
    # the config never set up for it, not a data gap, so it is dropped rather than
    # carried as a column of nothing. A profile feature that comes out all null —
    # rarity with no reference table — is kept, because the report has to be able
    # to say it went blank and the feature list must not move with the data.
    if len(frame):
        empty = [f.name for f in meta if not frame[f.name].notna().any()]
        if empty:
            meta = [f for f in meta if f.name not in empty]
            frame = frame.drop(columns=empty)

    profile_meta = list(profile.pair_feature_metadata(track))
    if profile_meta:
        built = profile.build_pair_features(
            pairs, units, events=events, references=references, track=track
        )
        if built is None:
            built = pd.DataFrame(index=pairs.index)
        built = built.reindex(index=pairs.index)
        for feature in profile_meta:
            column = built[feature.name] if feature.name in built.columns else np.nan
            frame[feature.name] = pd.to_numeric(
                pd.Series(column, index=pairs.index), errors="coerce"
            )
        meta += profile_meta

    frame = frame[[f.name for f in meta]].astype("float64")
    return frame, meta


def metadata(pairs_columns, track: str, profile=None) -> list[Feature]:
    """The metadata alone, without building anything.

    ``GET /api/model/{track}/features`` answers from this, so the panel can
    describe a model that has not been trained yet. *pairs_columns* is any
    iterable of column names — the gamma features follow the config's
    comparisons, so they cannot be known without it.
    """
    if profile is None:
        from app.profiles import get_profile

        profile = get_profile()
    stub = pd.DataFrame(columns=list(pairs_columns))
    return generic_metadata(stub) + list(profile.pair_feature_metadata(track))

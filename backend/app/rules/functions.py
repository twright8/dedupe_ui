# backend/app/rules/functions.py
"""The fixed function library a cleaning step may call.

A ruleset names a function; it never supplies one. The registry below is the
whole list, and ``GET /api/config/functions`` serves it straight from here, so
the forms on the Config screen and the engine can never drift apart.

Every function receives the DISTINCT non-null values of its source column as a
pandas Series and returns either a Series (one output, written to the step's
target) or a DataFrame whose columns are the function's fixed outputs. The
engine maps the result back onto the full frame, so a 16-million-row run pays
for each distinct value exactly once.

The postcode, company-number and nickname logic is ported from
``deduping/backend/pipeline/normalize.py``, which has its own tests pinning the
behaviour. Where RULESET.md states something different (an unrecognisable
postcode is null, not passed through) RULESET.md wins.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

import jellyfish
import pandas as pd


def accent_fold(text: str) -> str:
    """Strip accents, so LÖWE and LOWE are one spelling.

    This lived in the two-dataset tool's ``standardise`` module, which is gone.
    It is a cleaning function like every other one here.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Distinct-value execution — the performance rule from RULESET.md
# ---------------------------------------------------------------------------


def map_distinct(series: pd.Series, fn: Callable[[pd.Series], Any]):
    """Run *fn* over the distinct non-null values of *series* and map back.

    *fn* takes a Series of distinct values and returns a Series (one output) or
    a DataFrame (several), positionally aligned with its input. Nulls never
    reach *fn* and stay null in the result — "a null input stays null unless a
    step says otherwise".
    """
    present = series[series.notna()]
    distinct = present.drop_duplicates()

    if distinct.empty:
        # Nothing to do, but the caller still needs a correctly shaped result.
        result = fn(distinct)
        if isinstance(result, pd.DataFrame):
            return pd.DataFrame(
                {c: pd.Series([None] * len(series), index=series.index, dtype="object")
                 for c in result.columns}
            )
        return pd.Series([None] * len(series), index=series.index, dtype="object")

    result = fn(distinct)
    keys = distinct.to_numpy()

    def _back(values) -> pd.Series:
        lookup = pd.Series(list(values), index=keys)
        lookup = lookup[~lookup.index.duplicated()]
        mapped = series.map(lookup).astype("object")
        # A missing value comes back as None, never NaN: the rest of the engine,
        # the parquet and the JSON all read better for it.
        return mapped.where(mapped.notna(), None)

    if isinstance(result, pd.DataFrame):
        return pd.DataFrame({c: _back(result[c].to_numpy()) for c in result.columns})
    return _back(result.to_numpy())


def _text(values: pd.Series) -> pd.Series:
    """Distinct values as plain strings — a source column may be numeric."""
    return values.astype(str)


# ---------------------------------------------------------------------------
# Person names
# ---------------------------------------------------------------------------

PERSON_NAME_OUTPUTS = ("forename", "middle_names", "surname", "forename_initial")


def parse_person_name_value(value) -> dict:
    """Split one cleaned, title-free person name into its parts.

    A single token is a surname, not a forename: the donations sheet is full of
    bare surnames, and guessing them as forenames would block on the wrong half
    of the name. "SURNAME, FORENAME MIDDLE" is recognised by the comma.
    """
    text = " ".join(str(value).split())
    empty = {key: None for key in PERSON_NAME_OUTPUTS}
    if not text:
        return empty

    if "," in text:
        left, _, right = text.partition(",")
        surname_parts = left.split()
        given = right.split()
    else:
        parts = text.split()
        if len(parts) == 1:
            surname_parts, given = parts, []
        else:
            surname_parts, given = parts[-1:], parts[:-1]

    surname = " ".join(surname_parts) or None
    forename = given[0] if given else None
    middle = " ".join(given[1:]) or None
    return {
        "forename": forename,
        "middle_names": middle,
        "surname": surname,
        "forename_initial": forename[0] if forename else None,
    }


def _fn_parse_person_name(values: pd.Series) -> pd.DataFrame:
    parsed = [parse_person_name_value(v) for v in _text(values)]
    return pd.DataFrame(parsed, columns=list(PERSON_NAME_OUTPUTS))


# ---------------------------------------------------------------------------
# Postcodes  (ported from deduping normalize.normalize_postcode / postcode_district)
# ---------------------------------------------------------------------------

_UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9R][0-9A-Z]?)([0-9][A-Z]{2})$")
_POSTCODE_DISTRICT_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)\s")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def normalise_postcode_value(value) -> str | None:
    """'le11fb' to 'LE1 1FB'. Anything that is not a UK postcode shape is null.

    deduping passed non-UK postcodes through in compact form. Here they are
    dropped: this column feeds exact-match keys, and a half-cleaned foreign
    postcode there merges records it should not.
    """
    compact = _NON_ALNUM_RE.sub("", str(value).upper())
    if not 5 <= len(compact) <= 7:
        return None
    match = _UK_POSTCODE_RE.match(compact)
    return f"{match.group(1)} {match.group(2)}" if match else None


def postcode_district_value(value) -> str | None:
    """'LE1 1FB' to 'LE1'. Needs the space-separated form, so run it on the
    output of normalise_postcode, never on a raw postcode."""
    match = _POSTCODE_DISTRICT_RE.match(str(value).upper().strip())
    return match.group(1) if match else None


def _fn_normalise_postcode(values: pd.Series) -> pd.Series:
    return _text(values).map(normalise_postcode_value)


def _fn_postcode_district(values: pd.Series) -> pd.Series:
    return _text(values).map(postcode_district_value)


# ---------------------------------------------------------------------------
# Company numbers  (deduping normalize_regnum + gb_padded_company_number)
# ---------------------------------------------------------------------------


def normalise_company_number_value(value) -> str | None:
    """Strip non-alphanumerics, upper-case, left-pad a pure number to 8 digits.

    Companies House publishes the same company as '4250076' and '04250076';
    padding makes both one value. Fewer than two characters, or no digit at
    all, is junk rather than a number, and becomes null.
    """
    cleaned = _NON_ALNUM_RE.sub("", str(value).upper())
    if len(cleaned) < 2:
        return None
    if not any(character.isdigit() for character in cleaned):
        return None
    return cleaned.zfill(8) if cleaned.isdigit() else cleaned


def _fn_normalise_company_number(values: pd.Series) -> pd.Series:
    return _text(values).map(normalise_company_number_value)


# ---------------------------------------------------------------------------
# Phonetic keys and token helpers
# ---------------------------------------------------------------------------


def metaphone_value(value) -> str | None:
    return jellyfish.metaphone(str(value)) or None


def soundex_value(value) -> str | None:
    return jellyfish.soundex(str(value)) or None


def sorted_tokens_value(value) -> str | None:
    """Distinct tokens, sorted, joined by a space. 'SMITH AND SON AND SMITH'
    and 'SON AND SMITH' both become 'AND SMITH SON'."""
    return " ".join(sorted(set(str(value).split()))) or None


def first_token_value(value) -> str | None:
    tokens = str(value).split()
    return tokens[0] if tokens else None


def last_token_value(value) -> str | None:
    tokens = str(value).split()
    return tokens[-1] if tokens else None


def initials_value(value) -> str | None:
    return "".join(token[0] for token in str(value).split()) or None


def _series_fn(scalar_name: str) -> Callable[[pd.Series], pd.Series]:
    """Wrap a scalar function so it runs once per distinct value.

    The global is looked up at call time, which is what lets a test swap in a
    counting wrapper and prove the once-per-distinct-value rule holds.
    """

    def run(values: pd.Series) -> pd.Series:
        return _text(values).map(globals()[scalar_name])

    return run


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunctionSpec:
    """One library function, plus everything the Config screen needs to show it."""

    name: str
    description: str
    # The columns the function writes. ["target"] means "the step's target";
    # anything else is a fixed set of column names the step does not choose.
    outputs: list[str]
    example: dict
    run: Callable[..., Any]
    args: list[dict] = field(default_factory=list)

    @property
    def multi_output(self) -> bool:
        return self.outputs != ["target"]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "outputs": list(self.outputs),
            "args": [dict(a) for a in self.args],
            "example": dict(self.example),
        }


_SPECS = [
    FunctionSpec(
        name="parse_person_name",
        description=(
            "Split a cleaned, title-free person name into its parts. One token is "
            "taken as a surname. 'SURNAME, FORENAME' is recognised by the comma."
        ),
        outputs=list(PERSON_NAME_OUTPUTS),
        example={
            "input": "JOHN A SMITH",
            "output": {
                "forename": "JOHN",
                "middle_names": "A",
                "surname": "SMITH",
                "forename_initial": "J",
            },
        },
        run=_fn_parse_person_name,
    ),
    FunctionSpec(
        name="normalise_postcode",
        description=(
            "Reformat a UK postcode as 'OUTWARD INWARD'. Anything that is not a "
            "UK postcode shape becomes null."
        ),
        outputs=["target"],
        example={"input": "le11fb", "output": "LE1 1FB"},
        run=_fn_normalise_postcode,
    ),
    FunctionSpec(
        name="postcode_district",
        description=(
            "The outward code of a normalised postcode. Run it on the output of "
            "normalise_postcode — it needs the space."
        ),
        outputs=["target"],
        example={"input": "LE1 1FB", "output": "LE1"},
        run=_fn_postcode_district,
    ),
    FunctionSpec(
        name="normalise_company_number",
        description=(
            "Strip non-alphanumerics, upper-case, and left-pad a pure number to 8 "
            "digits. Fewer than two characters, or no digit, becomes null."
        ),
        outputs=["target"],
        example={"input": "4250076", "output": "04250076"},
        run=_fn_normalise_company_number,
    ),
    FunctionSpec(
        name="metaphone",
        description="Metaphone phonetic key, for matching names that sound alike.",
        outputs=["target"],
        example={"input": "SMYTHE", "output": metaphone_value("SMYTHE")},
        run=_series_fn("metaphone_value"),
    ),
    FunctionSpec(
        name="soundex",
        description="Soundex phonetic key. Coarser than metaphone.",
        outputs=["target"],
        example={"input": "SMYTHE", "output": soundex_value("SMYTHE")},
        run=_series_fn("soundex_value"),
    ),
    FunctionSpec(
        name="sorted_tokens",
        description=(
            "Distinct tokens, sorted, joined by a space. Matches names whose words "
            "are the same but in a different order."
        ),
        outputs=["target"],
        example={"input": "TULLOCH A J", "output": "A J TULLOCH"},
        run=_series_fn("sorted_tokens_value"),
    ),
    FunctionSpec(
        name="first_token",
        description="The first whitespace-separated token.",
        outputs=["target"],
        example={"input": "JOHN A SMITH", "output": "JOHN"},
        run=_series_fn("first_token_value"),
    ),
    FunctionSpec(
        name="last_token",
        description="The last whitespace-separated token.",
        outputs=["target"],
        example={"input": "JOHN A SMITH", "output": "SMITH"},
        run=_series_fn("last_token_value"),
    ),
    FunctionSpec(
        name="initials",
        description="The first letter of every token, joined.",
        outputs=["target"],
        example={"input": "JOHN A SMITH", "output": "JAS"},
        run=_series_fn("initials_value"),
    ),
]

REGISTRY: dict[str, FunctionSpec] = {spec.name: spec for spec in _SPECS}


def get(name: str) -> FunctionSpec | None:
    return REGISTRY.get(name)


def library() -> list[dict]:
    """The function library as GET /api/config/functions returns it."""
    return [spec.as_dict() for spec in _SPECS]

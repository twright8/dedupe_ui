# backend/app/rules/vetoes.py
"""Vetoes — the fourth rule type of `DESIGN.md` D8, described in `RULESET.md`.

A veto is a rule about a **pair**. It stops the scorer accepting a pair a person
would never accept, whatever the score says: two people whose birth years are 37
years apart are not one person, even when the name and the postcode agree.

Everything here works on two aligned Series — the LEFT unit's values and the
RIGHT unit's values of one column — and returns a boolean mask. A condition is
false when either side is null, so missing data never triggers a veto. Nothing
loops over pairs in Python: PSC will have a hundred million of them.

The same functions serve the pipeline and `POST /api/config/preview-vetoes`, so
a preview can never promise something a run does not deliver.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from app import vocabulary

TRACK_KEYS = ("person", "organisation")
ACTIONS = ("review", "reject")

# How strong each action is. A pair more than one veto hits takes the strongest.
ACTION_RANK = {"review": 1, "reject": 2}

# op -> the argument key it requires, or None when it takes none.
OPERATORS: dict[str, str | None] = {
    "differs": None,
    "abs_diff_gt": "value",
    "abs_diff_gte": "value",
    "similarity_lt": "value",
    "both_in_and_differ": "lists",
    "no_overlap": None,
}

# Operators whose `value` is a number.
NUMERIC_OPS = ("abs_diff_gt", "abs_diff_gte", "similarity_lt")

# How a set column is written in a representative row: " | "-joined, as
# `units.py` joins the members' existing entity ids.
SET_SEPARATOR = " | "

# The columns a vetoed pair carries. `_strip_overlays` drops them and
# `apply_overlays` writes them again, so every path that changes a bucket
# re-applies the vetoes.
VETO_COLUMNS = ("vetoed_by", "veto_reason", "veto_conflicts_import")


class VetoError(ValueError):
    """A veto that cannot be run — an unknown op, list or action."""


# ---------------------------------------------------------------------------
# Reading the document — user data, so never assume a key is there
# ---------------------------------------------------------------------------


def vetoes(ruleset: dict) -> list[dict]:
    """The ruleset's vetoes, in document order."""
    value = (ruleset or {}).get("vetoes")
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def for_track(ruleset: dict, track: str) -> list[dict]:
    return [v for v in vetoes(ruleset) if v.get("track") == track]


def conditions(veto: dict) -> list[dict]:
    value = (veto or {}).get("when")
    if not isinstance(value, list):
        return []
    return [c for c in value if isinstance(c, dict)]


def referenced_columns(veto: dict) -> list[str]:
    """The columns one veto's conditions read, in order, no repeats."""
    seen: list[str] = []
    for condition in conditions(veto):
        column = condition.get("column")
        if isinstance(column, str) and column and column not in seen:
            seen.append(column)
    return seen


def columns_needed(ruleset: dict, track: str | None = None) -> list[str]:
    """Every unit column the vetoes name — the projection to read, and no more."""
    seen: list[str] = []
    for veto in vetoes(ruleset):
        if track is not None and veto.get("track") != track:
            continue
        for column in referenced_columns(veto):
            if column not in seen:
                seen.append(column)
    return seen


def tracks_used(ruleset: dict) -> list[str]:
    return [t for t in TRACK_KEYS if for_track(ruleset, t)]


# ---------------------------------------------------------------------------
# Value shaping — the same "case-insensitive, blank is missing" rule the
# cleaning engine uses, done over the DISTINCT values of a column
# ---------------------------------------------------------------------------


def _as_text(series: pd.Series) -> pd.Series:
    """Trimmed, upper-cased strings with blanks as null.

    Built from the distinct values, so a column of a hundred million pairs costs
    one pass over its vocabulary rather than one conversion per row.
    """
    present = series[series.notna()]
    distinct = present.drop_duplicates()
    mapping = {value: (str(value).strip().upper() or None) for value in distinct}
    mapped = series.map(mapping).astype("object")
    return mapped.where(mapped.notna(), None)


def _as_number(series: pd.Series) -> pd.Series:
    """Numbers, whatever the column's dtype. Text that is not a number is null.

    `dob_year_clean` arrives as text out of the cleaning engine, so a numeric
    veto has to cast rather than assume.
    """
    return pd.to_numeric(series, errors="coerce")


def _both_present(left: pd.Series, right: pd.Series) -> np.ndarray:
    return (left.notna() & right.notna()).to_numpy()


def _distinct_pairs(left: pd.Series, right: pd.Series, known: np.ndarray,
                    score) -> np.ndarray:
    """*score* applied to the distinct (left, right) value pairs, mapped back."""
    out = np.full(len(left), np.nan, dtype="float64")
    if not known.any():
        return out
    lefts = left.to_numpy()[known]
    rights = right.to_numpy()[known]
    keys = pd.Series(list(zip(lefts, rights)))
    scored = {pair: score(pair[0], pair[1]) for pair in keys.drop_duplicates()}
    out[known] = keys.map(scored).to_numpy(dtype="float64")
    return out


def _token_set(ruleset: dict, names) -> set[str]:
    lists = (ruleset or {}).get("token_lists") or {}
    tokens: set[str] = set()
    for name in names or []:
        entry = lists.get(name)
        if entry is None:
            raise VetoError(f"Unknown token list '{name}'")
        for token in entry.get("tokens") or []:
            tokens.add(" ".join(str(token).split()).upper())
    return tokens


def _sets(series: pd.Series) -> pd.Series:
    """A " | "-joined column as frozensets, over the distinct values."""
    present = series[series.notna()]
    distinct = present.drop_duplicates()
    mapping = {
        value: frozenset(
            part.strip().upper()
            for part in str(value).split(SET_SEPARATOR)
            if part.strip()
        )
        for value in distinct
    }
    mapped = series.map(mapping).astype("object")
    return mapped.where(mapped.notna(), None)


# ---------------------------------------------------------------------------
# The operators
# ---------------------------------------------------------------------------


def condition_mask(condition: dict, left: pd.Series, right: pd.Series,
                   ruleset: dict) -> np.ndarray:
    """A boolean mask: does this condition hold for each pair?

    *left* and *right* are one column's values on the two sides, aligned. Null on
    either side is always False.
    """
    op = condition.get("op")
    if op not in OPERATORS:
        raise VetoError(f"Unknown veto operator '{op}'")

    if op in ("abs_diff_gt", "abs_diff_gte"):
        left_n, right_n = _as_number(left), _as_number(right)
        known = _both_present(left_n, right_n)
        try:
            limit = float(condition.get("value"))
        except (TypeError, ValueError):
            raise VetoError(f"{op} needs a number in `value`")
        gap = np.abs(left_n.to_numpy(dtype="float64")
                     - right_n.to_numpy(dtype="float64"))
        with np.errstate(invalid="ignore"):
            hit = gap > limit if op == "abs_diff_gt" else gap >= limit
        return known & np.nan_to_num(hit, nan=False).astype(bool)

    if op == "no_overlap":
        left_s, right_s = _sets(left), _sets(right)
        known = _both_present(left_s, right_s)
        out = np.zeros(len(left), dtype=bool)
        if not known.any():
            return out
        scored = _distinct_pairs(
            left_s, right_s, known,
            lambda a, b: 0.0 if (a & b) else 1.0,
        )
        return known & (scored == 1.0)

    text_left, text_right = _as_text(left), _as_text(right)
    known = _both_present(text_left, text_right)
    differ = np.zeros(len(left), dtype=bool)
    differ[known] = (text_left.to_numpy()[known] != text_right.to_numpy()[known])

    if op == "differs":
        return known & differ

    if op == "similarity_lt":
        import jellyfish

        try:
            limit = float(condition.get("value"))
        except (TypeError, ValueError):
            raise VetoError("similarity_lt needs a number in `value`")
        scored = _distinct_pairs(
            text_left, text_right, known, jellyfish.jaro_winkler_similarity
        )
        with np.errstate(invalid="ignore"):
            below = scored < limit
        return known & np.nan_to_num(below, nan=False).astype(bool)

    # both_in_and_differ
    tokens = _token_set(ruleset, condition.get("lists"))
    if not tokens:
        return np.zeros(len(left), dtype=bool)
    in_left = np.zeros(len(left), dtype=bool)
    in_right = np.zeros(len(left), dtype=bool)
    in_left[known] = np.isin(text_left.to_numpy()[known], list(tokens))
    in_right[known] = np.isin(text_right.to_numpy()[known], list(tokens))
    return known & differ & in_left & in_right


# ---------------------------------------------------------------------------
# One veto over a pair frame
# ---------------------------------------------------------------------------


class SideValues:
    """The two sides' values of whichever columns the vetoes name.

    Built once per call from a PROJECTION of the units — only the columns the
    vetoes read — so a 63-column units file costs three or four of them.
    """

    def __init__(self, pairs: pd.DataFrame, units: pd.DataFrame, columns):
        self._pairs = pairs
        self._cache: dict[str, tuple[pd.Series, pd.Series]] = {}
        wanted = [c for c in columns if c in units.columns]
        if len(units) and "unit_id" in units.columns:
            index = units["unit_id"].astype(str)
            self._lookup = units[wanted].set_index(index.to_numpy())
            self._lookup = self._lookup[~self._lookup.index.duplicated(keep="first")]
        else:
            self._lookup = pd.DataFrame(columns=wanted)
        self._columns = set(wanted)

    def get(self, column: str) -> tuple[pd.Series, pd.Series]:
        if column in self._cache:
            return self._cache[column]
        if column not in self._columns:
            # A ruleset may legally name a column the data does not carry, and
            # a condition on a column nobody has is false for every pair —
            # exactly as the key engine treats a missing column.
            empty = pd.Series([None] * len(self._pairs), index=self._pairs.index,
                              dtype="object")
            pair = (empty, empty)
        else:
            values = self._lookup[column]
            left = self._pairs["unit_id_l"].astype(str).map(values)
            right = self._pairs["unit_id_r"].astype(str).map(values)
            pair = (left, right)
        self._cache[column] = pair
        return pair


def veto_mask(veto: dict, sides: SideValues, ruleset: dict,
              track_mask: np.ndarray) -> np.ndarray:
    """Which pairs one veto hits. Every condition in `when` must hold."""
    when = conditions(veto)
    if not when:
        return np.zeros(len(track_mask), dtype=bool)
    hit = track_mask.copy()
    for condition in when:
        if not hit.any():
            return hit
        left, right = sides.get(condition.get("column"))
        hit = hit & condition_mask(condition, left, right, ruleset)
    return hit


# A number that arrived as text with nothing but zeros after the point.
_TRAILING_POINT_ZERO = re.compile(r"^(-?\d+)\.0+$")


def _render(value) -> str:
    """One value as a reviewer should read it.

    `nullify_outside_range` writes a birth year as the text "1958.0", because it
    goes through `to_numeric` on the way, and "Born 1958.0 and 1995.0" is not
    what a review screen should say. Only that exact shape is trimmed: a
    company number is left alone, leading zeros and all, because "00000001" and
    "1" are different numbers to a reader of Companies House.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    match = _TRAILING_POINT_ZERO.match(text)
    return match.group(1) if match else text


def _reasons(veto: dict, sides: SideValues, hit: np.ndarray,
             index: pd.Index) -> np.ndarray:
    """The reason text per hit pair, `{left}` and `{right}` filled in.

    The values come from the FIRST condition's column — the one the veto is
    about. Formatted over the distinct value pairs, so a million hits cost as
    many format calls as there are distinct birth-year pairs.
    """
    template = veto.get("reason")
    out = np.full(len(hit), None, dtype="object")
    if not hit.any():
        return out
    if not isinstance(template, str) or not template:
        out[hit] = veto.get("description") or veto.get("id")
        return out

    columns = referenced_columns(veto)
    if not columns:
        out[hit] = template
        return out
    left, right = sides.get(columns[0])
    lefts = left.to_numpy()[hit]
    rights = right.to_numpy()[hit]
    keys = pd.Series(list(zip(lefts, rights)), index=index[hit])
    rendered = {}
    for pair in keys.drop_duplicates():
        try:
            rendered[pair] = template.format(left=_render(pair[0]),
                                             right=_render(pair[1]))
        except (IndexError, KeyError, ValueError):
            # A template with an unknown placeholder must not lose the veto.
            rendered[pair] = template
    out[hit] = keys.map(rendered).to_numpy()
    return out


# ---------------------------------------------------------------------------
# The whole set over a pair frame
# ---------------------------------------------------------------------------


def hits(pairs: pd.DataFrame, units: pd.DataFrame, ruleset: dict) -> list[dict]:
    """One entry per veto: ``{veto, mask}``, in document order.

    Only the columns the vetoes name are read out of *units*, and only the rows
    of each veto's own track are considered.
    """
    result: list[dict] = []
    if not len(pairs):
        return result
    active = vetoes(ruleset)
    if not active:
        return result

    sides = SideValues(pairs, units, columns_needed(ruleset))
    track = pairs["track"].astype(str).to_numpy() if "track" in pairs.columns \
        else np.full(len(pairs), None)
    for veto in active:
        mask = veto_mask(veto, sides, ruleset,
                         (track == str(veto.get("track"))))
        result.append({"veto": veto, "mask": mask, "sides": sides})
    return result


def apply_to_buckets(pairs: pd.DataFrame, units: pd.DataFrame, ruleset: dict,
                     bucket: np.ndarray) -> dict:
    """Lay the vetoes over *bucket*, the buckets the score or the model gave.

    Returns ``{bucket, vetoed_by, veto_reason, any}``. A `review` veto caps the
    pair at review — it can never be auto-accepted — and a `reject` veto puts it
    in reject. When more than one veto hits, the strongest action applies
    (reject beats review) and ``vetoed_by`` names the first veto with it.
    """
    bucket = np.asarray(bucket, dtype=object).copy()
    vetoed_by = np.full(len(bucket), None, dtype="object")
    reason = np.full(len(bucket), None, dtype="object")
    rank = np.zeros(len(bucket), dtype="int8")

    for entry in hits(pairs, units, ruleset):
        veto, mask = entry["veto"], entry["mask"]
        if not mask.any():
            continue
        action = veto.get("action")
        strength = ACTION_RANK.get(action, 0)
        if not strength:
            raise VetoError(f"Unknown veto action '{action}'")
        stronger = mask & (rank < strength)
        if stronger.any():
            rank[stronger] = strength
            vetoed_by[stronger] = veto.get("id")
            text = _reasons(veto, entry["sides"], stronger, pairs.index)
            reason[stronger] = text[stronger]

    capped = rank == ACTION_RANK["review"]
    bucket[capped & (bucket == "accept")] = "review"
    bucket[rank == ACTION_RANK["reject"]] = "reject"
    return {
        "bucket": bucket,
        "vetoed_by": vetoed_by,
        "veto_reason": reason,
        "any": rank > 0,
    }


def bucket_without_vetoes(pairs: pd.DataFrame) -> np.ndarray:
    """What each pair's bucket would be if no veto had run.

    The score bucket, with the import overlay back on top of it. This is what
    ``accepted_pairs_hit`` and the ``without_vetoes`` figure set count against:
    "pairs the run would otherwise have accepted".
    """
    if not len(pairs):
        return np.empty(0, dtype=object)
    bucket = pairs["score_bucket"].to_numpy(dtype=object).copy()
    if "decided_by" in pairs.columns:
        agreed = (pairs["decided_by"].astype(str) == "import").to_numpy()
        conflict = pairs["veto_conflicts_import"].fillna(False).to_numpy() \
            if "veto_conflicts_import" in pairs.columns \
            else np.zeros(len(pairs), dtype=bool)
        bucket[agreed | conflict] = "accept"
    return bucket


def counts_from(pairs: pd.DataFrame) -> dict:
    """The three run counts the vetoes own."""
    if not len(pairs) or "vetoed_by" not in pairs.columns:
        return {"pairs_vetoed": 0, "pairs_vetoed_from_accept": 0,
                "veto_conflicts_import": 0}
    vetoed = pairs["vetoed_by"].notna().to_numpy()
    would_accept = bucket_without_vetoes(pairs) == "accept"
    conflicts = pairs["veto_conflicts_import"].fillna(False).to_numpy() \
        if "veto_conflicts_import" in pairs.columns \
        else np.zeros(len(pairs), dtype=bool)
    return {
        "pairs_vetoed": int(vetoed.sum()),
        "pairs_vetoed_from_accept": int((vetoed & would_accept).sum()),
        "veto_conflicts_import": int(conflicts.sum()),
    }


def report(pairs: pd.DataFrame, units: pd.DataFrame, ruleset: dict,
           max_examples: int = 10, name_column: str = "name") -> list[dict]:
    """Per veto: how many pairs it hits, how many of those would be accepted,
    and a handful of examples. This is what `preview-vetoes` serves."""
    active = vetoes(ruleset)
    if not active:
        return []

    would_accept = (bucket_without_vetoes(pairs) == "accept") if len(pairs) \
        else np.empty(0, dtype=bool)
    names = None
    if len(units) and name_column in units.columns and "unit_id" in units.columns:
        names = pd.Series(units[name_column].to_numpy(),
                          index=units["unit_id"].astype(str).to_numpy())
        names = names[~names.index.duplicated(keep="first")]

    out = []
    for entry in hits(pairs, units, ruleset) if len(pairs) else []:
        veto, mask, sides = entry["veto"], entry["mask"], entry["sides"]
        columns = referenced_columns(veto)
        examples = []
        if mask.any():
            reason = _reasons(veto, sides, mask, pairs.index)
            chosen = np.flatnonzero(mask & would_accept)
            if len(chosen) < max_examples:
                rest = np.flatnonzero(mask & ~would_accept)
                chosen = np.concatenate([chosen, rest])
            chosen = chosen[:max_examples]
            left_values, right_values = sides.get(columns[0]) if columns else (None, None)
            for position in chosen:
                row = pairs.iloc[position]
                left_id = str(row["unit_id_l"])
                right_id = str(row["unit_id_r"])
                score = row.get("match_probability")
                examples.append({
                    "pair_id": f"{left_id}|{right_id}",
                    "left_name": None if names is None else _plain(names.get(left_id)),
                    "right_name": None if names is None else _plain(names.get(right_id)),
                    "left_value": _plain(None if left_values is None
                                         else left_values.iloc[position]),
                    "right_value": _plain(None if right_values is None
                                          else right_values.iloc[position]),
                    "score": _plain(score),
                    "reason": reason[position],
                })
        out.append({
            "id": veto.get("id"),
            "track": veto.get("track"),
            "description": veto.get("description") or "",
            "action": veto.get("action"),
            "columns": columns,
            "pairs_hit": int(mask.sum()),
            "accepted_pairs_hit": int((mask & would_accept).sum()),
            "examples": examples,
        })
    return out


def _plain(value):
    """A JSON-safe scalar: pandas and numpy nulls become None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _error(errors: list[dict], path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def validate(ruleset: dict, per_track: dict, errors: list[dict]) -> None:
    """Every problem with the ruleset's vetoes, appended to *errors*.

    *per_track* is ``{track: [column names]}`` — the raw columns plus that
    track's cleaning and derived targets, which is what a unit row carries.
    """
    raw = (ruleset or {}).get("vetoes", [])
    if raw is None:
        return
    if not isinstance(raw, list):
        _error(errors, "vetoes", "vetoes must be a list")
        return

    token_lists = (ruleset or {}).get("token_lists") or {}
    seen: set[str] = set()
    for index, veto in enumerate(raw):
        path = f"vetoes[{index}]"
        if not isinstance(veto, dict):
            _error(errors, path, "A veto must be an object")
            continue

        veto_id = veto.get("id")
        if not veto_id:
            _error(errors, f"{path}.id", "A veto needs an id")
        elif veto_id in seen:
            _error(errors, f"{path}.id", f"Duplicate veto id '{veto_id}'")
        else:
            seen.add(veto_id)

        track = veto.get("track")
        known = None
        if track not in TRACK_KEYS:
            _error(errors, f"{path}.track", f"Unknown track '{track}'")
        else:
            known = per_track.get(track)

        action = veto.get("action")
        if action not in ACTIONS:
            _error(errors, f"{path}.action",
                   vocabulary.choice_error("action", veto.get("action"), ACTIONS))

        reason = veto.get("reason")
        if reason is not None and not isinstance(reason, str):
            _error(errors, f"{path}.reason", "reason must be text")

        when = veto.get("when")
        if not isinstance(when, list) or not when:
            _error(errors, f"{path}.when", "A veto needs at least one condition")
            continue
        for position, condition in enumerate(when):
            _check_condition(condition, f"{path}.when[{position}]", track, known,
                             token_lists, errors)


def _check_condition(condition, path: str, track, known, token_lists,
                     errors: list[dict]) -> None:
    if not isinstance(condition, dict):
        _error(errors, path, "A condition must be an object")
        return

    column = condition.get("column")
    if not isinstance(column, str) or not column:
        _error(errors, f"{path}.column", "A condition needs a column")
    elif known is not None and column not in known:
        _error(errors, f"{path}.column",
               f"'{column}' is not a column of the {track} track")

    op = condition.get("op")
    if op not in OPERATORS:
        _error(errors, f"{path}.op",
               vocabulary.choice_error("op", condition.get("op"), sorted(OPERATORS)))
        return

    argument = OPERATORS[op]
    if argument == "value":
        value = condition.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _error(errors, f"{path}.value", f"{op} needs a number in `value`")
        elif op in ("abs_diff_gt", "abs_diff_gte") and float(value) < 0:
            _error(errors, f"{path}.value",
                   f"{op} needs a difference of zero or more")
        elif op == "similarity_lt" and not 0 <= float(value) <= 1:
            _error(errors, f"{path}.value",
                   "similarity_lt needs a similarity between 0 and 1")
    elif argument == "lists":
        names = condition.get("lists")
        if not isinstance(names, list) or not names \
                or any(not isinstance(n, str) for n in names):
            _error(errors, f"{path}.lists",
                   f"{op} needs a list of token list names")
        else:
            for name in names:
                if name not in token_lists:
                    _error(errors, f"{path}.lists", f"Unknown token list '{name}'")

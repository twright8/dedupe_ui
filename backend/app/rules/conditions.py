# backend/app/rules/conditions.py
"""The condition operators a track rule may use.

A condition reads one raw column and returns a boolean mask over the frame.
Matching is case-insensitive throughout. Token operators match whole words, so
'Drummond' does not start with the title 'DR', and they accept multi-word
tokens such as 'RT HON'.

Every regular expression runs over the DISTINCT values of the column and the
answers are mapped back, for the same reason cleaning steps do.
"""

import re

import pandas as pd

# Operators that take a `lists` argument naming one or more token lists.
TOKEN_OPS = ("starts_with_token", "ends_with_token", "contains_token")

# op -> the argument key it requires, or None when it takes no argument.
OPERATORS: dict[str, str | None] = {
    "equals": "value",
    "not_equals": "value",
    "in": "values",
    "not_in": "values",
    "is_null": None,
    "not_null": None,
    "starts_with_token": "lists",
    "ends_with_token": "lists",
    "contains_token": "lists",
    "starts_with": "values",
    "matches": "pattern",
}


class ConditionError(ValueError):
    """A condition that cannot be run — an unknown op, list or bad regex."""


# ---------------------------------------------------------------------------
# Token patterns
# ---------------------------------------------------------------------------


def _token_pattern(token: str) -> str:
    """One token as a regex fragment, with word boundaries that survive
    punctuation. Inner spaces match any run of whitespace."""
    words = [re.escape(word) for word in token.split()]
    core = r"\s+".join(words)
    left = r"(?<![0-9A-Za-z])" if token[:1].isalnum() else ""
    right = r"(?![0-9A-Za-z])" if token[-1:].isalnum() else ""
    return f"{left}{core}{right}"


def token_regex(tokens, position: str) -> re.Pattern | None:
    """Compile *tokens* into one alternation anchored for *position*.

    Longest first, so 'RT HON' wins over 'HON' and 'REVD' over 'REV'.
    """
    cleaned = [" ".join(str(t).split()) for t in tokens]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        return None
    ordered = sorted(set(cleaned), key=lambda t: (-len(t), t))
    alternation = "|".join(_token_pattern(t) for t in ordered)
    if position == "leading":
        pattern = rf"^\s*(?:{alternation})"
    elif position == "trailing":
        # A trailing full stop or comma is punctuation, not part of the name.
        pattern = rf"(?:{alternation})[.,\s]*$"
    else:
        pattern = rf"(?:{alternation})"
    return re.compile(pattern, re.IGNORECASE)


_POSITION_FOR_OP = {
    "starts_with_token": "leading",
    "ends_with_token": "trailing",
    "contains_token": "anywhere",
}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _as_text(series: pd.Series) -> pd.Series:
    """Object-dtype strings, trimmed, with blanks treated as null.

    Built from the distinct values, so a column of 16 million records costs one
    pass over its vocabulary rather than one conversion per row.
    """
    distinct = series[series.notna()].drop_duplicates()
    cleaned = {value: (str(value).strip() or None) for value in distinct}
    mapped = series.map(cleaned).astype("object")
    return mapped.where(mapped.notna(), None)


def _regex_mask(text: pd.Series, pattern: re.Pattern) -> pd.Series:
    present = text[text.notna()]
    distinct = present.drop_duplicates().tolist()
    hits = {value: bool(pattern.search(value)) for value in distinct}
    return text.map(hits).fillna(False).astype(bool)


def _upper_mask(text: pd.Series, wanted) -> pd.Series:
    targets = {str(w).strip().upper() for w in wanted}
    present = text[text.notna()]
    distinct = present.drop_duplicates().tolist()
    hits = {value: value.strip().upper() in targets for value in distinct}
    return text.map(hits).fillna(False).astype(bool)


def _prefix_mask(text: pd.Series, prefixes: tuple[str, ...]) -> pd.Series:
    present = text[text.notna()]
    distinct = present.drop_duplicates().tolist()
    hits = {value: value.strip().upper().startswith(prefixes) for value in distinct}
    return text.map(hits).fillna(False).astype(bool)


def _tokens_for(condition: dict, token_lists: dict) -> list[str]:
    tokens: list[str] = []
    for name in condition.get("lists") or []:
        entry = token_lists.get(name)
        if entry is None:
            raise ConditionError(f"Unknown token list '{name}'")
        tokens.extend(entry.get("tokens") or [])
    return tokens


def evaluate(condition: dict, series: pd.Series, token_lists: dict) -> pd.Series:
    """Return a boolean mask over *series* for one condition."""
    op = condition.get("op")
    if op not in OPERATORS:
        raise ConditionError(f"Unknown condition operator '{op}'")

    text = _as_text(series)

    if op == "is_null":
        return text.isna()
    if op == "not_null":
        return text.notna()

    if op in ("equals", "not_equals"):
        mask = _upper_mask(text, [condition.get("value", "")])
        return ~mask if op == "not_equals" else mask

    if op in ("in", "not_in"):
        mask = _upper_mask(text, condition.get("values") or [])
        return ~mask if op == "not_in" else mask

    if op == "starts_with":
        # The company-number prefixes (OC, SC, NI...) are the point of this one:
        # a whole-word token operator would never see them, because the prefix is
        # not a word of its own.
        prefixes = tuple(
            str(value).strip().upper()
            for value in (condition.get("values") or [])
            if str(value).strip()
        )
        if not prefixes:
            return pd.Series(False, index=series.index)
        return _prefix_mask(text, prefixes)

    if op == "matches":
        try:
            pattern = re.compile(condition.get("pattern") or "", re.IGNORECASE)
        except re.error as exc:
            raise ConditionError(f"Invalid regular expression: {exc}") from exc
        return _regex_mask(text, pattern)

    pattern = token_regex(_tokens_for(condition, token_lists), _POSITION_FOR_OP[op])
    if pattern is None:
        return pd.Series(False, index=series.index)
    return _regex_mask(text, pattern)


def columns_read(rule: dict) -> list[str]:
    """The columns one rule's conditions look at, in order, no repeats."""
    seen: list[str] = []
    for condition in rule.get("when") or []:
        column = condition.get("column")
        if column and column not in seen:
            seen.append(column)
    return seen

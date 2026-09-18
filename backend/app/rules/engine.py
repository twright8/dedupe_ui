# backend/app/rules/engine.py
"""Validate a ruleset, assign tracks, and run the cleaning steps.

This is the only place a ruleset is executed. Stage 1 and the two Config-screen
previews all call the functions here, so a preview cannot quietly disagree with
a run: ``apply_cleaning`` and ``trace_cleaning`` are the same loop over the same
step functions, with the trace switched on.

Every op runs on the DISTINCT values of its source column and the results are
mapped back (``functions.map_distinct``). Nothing walks the frame row by row.
"""

import re

import pandas as pd

from app.pipeline.standardise import accent_fold
from app.rules import conditions, functions

TRACK_KEYS = ("person", "organisation")
POSITIONS = ("leading", "trailing", "anywhere")
SCOPES = ("value", "tokens")
FALLBACKS = ("passthrough", "null", "error")
APPLIES_WHEN = ("always", "no_earlier_key")
ON_GUARD_FAIL = ("review", "skip")

TEXT_OPS = ("upper", "lower", "trim", "collapse_spaces", "accent_fold")

# A derived column also writes "<target>_rule", so a target may not end this way
# or the two would collide.
RULE_COLUMN_SUFFIX = "_rule"

# A target is a column name, so it is held to the shape the cleaning targets use.
COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class UnmappedLookupValuesError(RuntimeError):
    """A lookup with ``fallback: "error"`` met values it cannot map.

    Carries the table and the distinct unmapped values so the run's failure can
    offer the one-click "add these rows" fix instead of leaving the user stuck.
    """

    def __init__(self, table: str, values: list[str]):
        self.table = table
        self.values = list(values)
        shown = ", ".join(self.values[:20])
        super().__init__(
            f"Lookup '{table}' has no row for {len(self.values)} value(s): {shown}. "
            "Add them on the Config screen and rerun."
        )


class RulesetError(ValueError):
    """A ruleset that cannot be run. Validation should have caught it first."""


# ---------------------------------------------------------------------------
# Small readers — a ruleset is user data, so never assume a key is there
# ---------------------------------------------------------------------------


def _token_lists(ruleset: dict) -> dict:
    value = ruleset.get("token_lists")
    return value if isinstance(value, dict) else {}


def _lookups(ruleset: dict) -> dict:
    value = ruleset.get("lookups")
    return value if isinstance(value, dict) else {}


def _track_rules(ruleset: dict) -> list[dict]:
    value = ruleset.get("track_rules")
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def cleaning_steps(ruleset: dict, track: str) -> list[dict]:
    cleaning = ruleset.get("cleaning")
    if not isinstance(cleaning, dict):
        return []
    steps = cleaning.get(track)
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def derived_columns(ruleset: dict) -> list[dict]:
    """The ruleset's derived columns, in document order.

    A ruleset saved before derived columns existed simply has none, so the
    section is read as empty rather than forcing a re-seed.
    """
    value = ruleset.get("derived_columns")
    return [d for d in value if isinstance(d, dict)] if isinstance(value, list) else []


def derived_tracks(column: dict) -> list[str]:
    """The tracks a derived column applies to. Omitted or empty means all."""
    tracks = column.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return list(TRACK_KEYS)
    return [t for t in tracks if isinstance(t, str)]


def derived_targets(column: dict) -> list[str]:
    """The columns one derived column writes: its target and its rule column."""
    target = column.get("target")
    return [target, f"{target}{RULE_COLUMN_SUFFIX}"] if target else []


def match_keys(ruleset: dict) -> list[dict]:
    value = ruleset.get("match_keys")
    return [k for k in value if isinstance(k, dict)] if isinstance(value, list) else []


def _tokens_from(ruleset: dict, names) -> list[str]:
    tokens: list[str] = []
    lists = _token_lists(ruleset)
    for name in names or []:
        entry = lists.get(name)
        if entry is None:
            raise RulesetError(f"Unknown token list '{name}'")
        tokens.extend(entry.get("tokens") or [])
    return tokens


def _token_set(tokens) -> tuple[set[str], int]:
    """Upper-cased, space-normalised tokens plus the longest word count."""
    cleaned = {" ".join(str(t).split()).upper() for t in tokens}
    cleaned.discard("")
    longest = max((len(t.split()) for t in cleaned), default=1)
    return cleaned, longest


# ---------------------------------------------------------------------------
# Ops — each takes the DISTINCT non-null source values and returns a Series
# (written to the step's target) or a DataFrame keyed on final column names.
# ---------------------------------------------------------------------------


def _text(values: pd.Series) -> pd.Series:
    return values.astype(str)


def _op_copy(values, step, ruleset):
    return values


def _op_upper(values, step, ruleset):
    return _text(values).str.upper()


def _op_lower(values, step, ruleset):
    return _text(values).str.lower()


def _op_trim(values, step, ruleset):
    return _text(values).str.strip()


def _op_collapse_spaces(values, step, ruleset):
    return _text(values).str.replace(r"\s+", " ", regex=True).str.strip()


def _op_accent_fold(values, step, ruleset):
    return _text(values).map(accent_fold)


def _op_strip_punctuation(values, step, ruleset):
    keep = step.get("keep", " ")
    keep = " " if keep is None else str(keep)
    pattern = re.compile(rf"[^\w{re.escape(keep)}]", re.UNICODE)
    out = _text(values).str.replace(pattern, "", regex=True)
    if "_" not in keep:
        # \w includes the underscore; nothing here wants to keep it by default.
        out = out.str.replace("_", "", regex=False)
    return out


def _op_regex_replace(values, step, ruleset):
    pattern = step.get("pattern") or ""
    replacement = step.get("replacement", "")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise RulesetError(f"Invalid regular expression: {exc}") from exc
    return _text(values).str.replace(compiled, replacement, regex=True)


def _split_tokens(text: str, token_set: set[str], longest: int, position: str,
                  repeat: bool, keep_one: bool) -> tuple[str, list[str]]:
    """Remove list tokens at *position*; return what is left and what went."""
    parts = text.split()
    removed: list[str] = []

    if position == "anywhere":
        index = 0
        while index < len(parts):
            matched = 0
            for size in range(min(longest, len(parts) - index), 0, -1):
                if " ".join(parts[index:index + size]).upper() in token_set:
                    matched = size
                    break
            if matched and not (keep_one and len(parts) - matched < 1):
                removed.append(" ".join(parts[index:index + matched]))
                del parts[index:index + matched]
                if not repeat:
                    break
            else:
                index += 1
        return " ".join(parts), removed

    leading = position == "leading"
    while parts:
        matched = 0
        for size in range(min(longest, len(parts)), 0, -1):
            candidate = parts[:size] if leading else parts[len(parts) - size:]
            if " ".join(candidate).upper() in token_set:
                matched = size
                break
        if not matched:
            break
        if keep_one and len(parts) - matched < 1:
            break
        if leading:
            removed.append(" ".join(parts[:matched]))
            parts = parts[matched:]
        else:
            removed.insert(0, " ".join(parts[len(parts) - matched:]))
            parts = parts[: len(parts) - matched]
        if not repeat:
            break

    return " ".join(parts), removed


def _op_strip_tokens(values, step, ruleset):
    token_set, longest = _token_set(_tokens_from(ruleset, step.get("lists")))
    position = step.get("position", "leading")
    if position not in POSITIONS:
        raise RulesetError(f"Unknown position '{position}'")
    repeat = bool(step.get("repeat", True))
    keep_one = bool(step.get("keep_one", True))

    kept: list[str] = []
    stripped: list[str] = []
    for text in _text(values):
        remaining, removed = _split_tokens(
            text, token_set, longest, position, repeat, keep_one
        )
        stripped.append(remaining)
        kept.append(" ".join(removed))

    target = step.get("target")
    frame = pd.DataFrame({target: stripped})
    keep_as = step.get("keep_as")
    if keep_as:
        frame[keep_as] = kept
    return frame


def _op_nullify(values, step, ruleset):
    token_set, _ = _token_set(_tokens_from(ruleset, step.get("lists")))
    return _text(values).map(
        lambda text: None if " ".join(text.split()).upper() in token_set else text
    )


def _op_lookup(values, step, ruleset):
    name = step.get("table")
    table = _lookups(ruleset).get(name)
    if table is None:
        raise RulesetError(f"Unknown lookup '{name}'")
    scope = step.get("scope", "value")
    if scope not in SCOPES:
        raise RulesetError(f"Unknown lookup scope '{scope}'")
    fallback = table.get("fallback", "passthrough")
    if fallback not in FALLBACKS:
        raise RulesetError(f"Unknown lookup fallback '{fallback}'")

    mapping = {}
    for row in table.get("rows") or []:
        raw = " ".join(str(row.get("raw", "")).split()).upper()
        if raw:
            mapping[raw] = row.get("canonical")

    unmapped: list[str] = []

    def _one(value: str):
        key = " ".join(value.split()).upper()
        if key in mapping:
            return mapping[key]
        if fallback == "error":
            unmapped.append(value)
            return value
        return None if fallback == "null" else value

    if scope == "value":
        out = _text(values).map(_one)
    else:
        def _tokens(text: str):
            parts = [_one(token) for token in text.split()]
            return " ".join(p for p in parts if p) or None

        out = _text(values).map(_tokens)

    if unmapped:
        raise UnmappedLookupValuesError(name, sorted(dict.fromkeys(unmapped)))
    return out


def _op_function(values, step, ruleset):
    name = step.get("name")
    spec = functions.get(name)
    if spec is None:
        raise RulesetError(f"Unknown function '{name}'")
    args = {a["name"]: step[a["name"]] for a in spec.args if a["name"] in step}
    result = spec.run(values, **args)
    if spec.multi_output:
        return result
    target = step.get("target")
    return result.rename(target) if isinstance(result, pd.Series) else result


OPS = {
    "copy": _op_copy,
    "upper": _op_upper,
    "lower": _op_lower,
    "trim": _op_trim,
    "collapse_spaces": _op_collapse_spaces,
    "accent_fold": _op_accent_fold,
    "strip_punctuation": _op_strip_punctuation,
    "regex_replace": _op_regex_replace,
    "strip_tokens": _op_strip_tokens,
    "nullify": _op_nullify,
    "lookup": _op_lookup,
    "function": _op_function,
}


def step_targets(step: dict) -> list[str]:
    """Every column one step writes, in the order the UI should show them."""
    if step.get("op") == "function":
        spec = functions.get(step.get("name"))
        if spec is not None and spec.multi_output:
            return list(spec.outputs)
    written = [step.get("target")] if step.get("target") else []
    if step.get("op") == "strip_tokens" and step.get("keep_as"):
        written.append(step["keep_as"])
    return [c for c in written if c]


# ---------------------------------------------------------------------------
# Running the steps
# ---------------------------------------------------------------------------


def _blank_to_null(series: pd.Series) -> pd.Series:
    """"An empty string after a step becomes null" (RULESET.md), and a missing
    value is always None rather than NaN."""
    out = series.astype("object")
    out = out.where(out.notna(), None)
    blank = out.astype("string").str.strip().eq("").fillna(False)
    return out.where(~blank, None)


def _run_step(step: dict, ruleset: dict, frame: pd.DataFrame) -> dict[str, pd.Series]:
    """Run one step against *frame* and return the columns it writes."""
    op = step.get("op")
    handler = OPS.get(op)
    if handler is None:
        raise RulesetError(f"Unknown op '{op}'")
    source = step.get("source")
    if source not in frame.columns:
        raise RulesetError(f"Unknown source column '{source}'")

    result = functions.map_distinct(
        frame[source], lambda distinct: handler(distinct, step, ruleset)
    )
    if isinstance(result, pd.Series):
        outputs = {step.get("target"): result}
    else:
        outputs = {name: result[name] for name in result.columns}
    return {name: _blank_to_null(series) for name, series in outputs.items() if name}


def _same(left, right) -> bool:
    left_null = left is None or (isinstance(left, float) and pd.isna(left))
    right_null = right is None or (isinstance(right, float) and pd.isna(right))
    if left_null or right_null:
        return left_null and right_null
    return left == right


def _run_cleaning(df: pd.DataFrame, ruleset: dict, track: str, trace: bool):
    """The one cleaning loop. ``trace`` only adds bookkeeping — never a branch
    in what the steps do, so a preview and a run cannot disagree."""
    frame = df.copy()
    steps_trace: list[dict] = []

    for step in cleaning_steps(ruleset, track):
        source = step.get("source")
        before = frame[source] if source in frame.columns else None
        error = None
        outputs: dict[str, pd.Series] = {}
        try:
            outputs = _run_step(step, ruleset, frame)
        except UnmappedLookupValuesError:
            raise  # a run-stopping data problem, never a draft-rule mistake
        except Exception as exc:  # a bad regex in a draft must not 500
            if not trace:
                raise
            error = str(exc)

        for name, series in outputs.items():
            frame[name] = series

        if trace:
            steps_trace.append({
                "id": step.get("id"),
                "op": step.get("op"),
                "description": step.get("description", ""),
                "source": source,
                "target": step.get("target"),
                "before": before,
                "outputs": outputs,
                "error": error,
            })

    return frame, steps_trace


def apply_cleaning(df: pd.DataFrame, ruleset: dict, track: str) -> pd.DataFrame:
    """Run one track's cleaning steps over *df*.

    Returns *df* plus every column the steps write. Raw columns are never
    overwritten — validation rejects a step that tries.
    """
    return _run_cleaning(df, ruleset, track, trace=False)[0]


def trace_cleaning(df: pd.DataFrame, ruleset: dict, track: str):
    """``apply_cleaning`` with each step's before / outputs kept for the preview.

    Returns ``(frame, steps)`` where each step carries ``before`` and
    ``outputs`` as Series aligned to *df*, plus an error string when a draft
    rule could not run.
    """
    return _run_cleaning(df, ruleset, track, trace=True)


def trace_rows(df: pd.DataFrame, ruleset: dict, track: str) -> list[dict]:
    """One preview sample per row of *df*: the steps with before / outputs /
    changed, and the final value of every target."""
    frame, steps = trace_cleaning(df, ruleset, track)
    targets = available_columns(ruleset, track, list(df.columns))["targets"]
    used = [c for c in _sources_read(ruleset, track) if c in df.columns]

    samples = []
    for position, index in enumerate(df.index):
        inputs = {column: _cell(df.at[index, column]) for column in used}
        # The record id is not a rule input, but it is how a reviewer finds the
        # row again. Typed-in samples have none, so it is only added when real.
        if "record_id" in df.columns and _cell(df.at[index, "record_id"]) is not None:
            inputs["record_id"] = _cell(df.at[index, "record_id"])

        rows = []
        for step in steps:
            before = _cell(step["before"].iloc[position]) if step["before"] is not None else None
            outputs = {
                name: _cell(series.iloc[position])
                for name, series in step["outputs"].items()
            }
            rows.append({
                "id": step["id"],
                "op": step["op"],
                "description": step["description"],
                "source": step["source"],
                "before": before,
                "outputs": outputs,
                "changed": _step_changed(step, before, outputs),
                "error": step["error"],
            })

        samples.append({
            "input": inputs,
            "steps": rows,
            "output": {c: _cell(frame.at[index, c]) for c in targets if c in frame.columns},
        })
    return samples


def _step_changed(step: dict, before, outputs: dict) -> bool:
    """Did this step alter the value flowing through it?

    Measured on the target, whether or not the target is the source: an
    already-upper-case name copied to name_clean has not been changed by the
    upper step, and saying otherwise would light up the whole preview.
    """
    if step["error"] or not outputs:
        return False
    target = step.get("target")
    if target in outputs:
        return not _same(before, outputs[target])
    # A multi-output function has no target; it did something if it wrote one.
    return any(value is not None for value in outputs.values())


def _cell(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    return value


def _sources_read(ruleset: dict, track: str) -> list[str]:
    seen: list[str] = []
    for step in cleaning_steps(ruleset, track):
        source = step.get("source")
        if source and source not in seen:
            seen.append(source)
    return seen


# ---------------------------------------------------------------------------
# Track assignment
# ---------------------------------------------------------------------------


def assign_tracks_detailed(df: pd.DataFrame, ruleset: dict):
    """``(tracks, rules)`` where *rules* carries each rule's mask and hit count.

    First rule whose conditions all hold wins, so a rule's hits are the records
    it actually decided — not the records it merely matched.
    """
    default_track = ruleset.get("default_track") or "organisation"
    tracks = pd.Series([None] * len(df), index=df.index, dtype="object")
    undecided = pd.Series(True, index=df.index)
    token_lists = _token_lists(ruleset)
    reported: list[dict] = []

    for rule in _track_rules(ruleset):
        matches = pd.Series(True, index=df.index)
        for condition in rule.get("when") or []:
            column = condition.get("column")
            if column not in df.columns:
                matches = pd.Series(False, index=df.index)
                break
            matches &= conditions.evaluate(condition, df[column], token_lists)

        decided = matches & undecided
        tracks = tracks.where(~decided, rule.get("track"))
        undecided &= ~decided
        reported.append({
            "id": rule.get("id"),
            "description": rule.get("description", ""),
            "track": rule.get("track"),
            "columns": conditions.columns_read(rule),
            "hits": int(decided.sum()),
            "mask": decided,
        })

    tracks = tracks.where(~undecided, default_track)
    reported.append({
        "id": "default",
        "description": f"Everything else is an {default_track}",
        "track": default_track,
        "columns": [],
        "hits": int(undecided.sum()),
        "mask": undecided,
    })
    return tracks, reported


def assign_tracks(df: pd.DataFrame, ruleset: dict) -> pd.Series:
    """The track for every record, as a Series aligned to *df*."""
    return assign_tracks_detailed(df, ruleset)[0]


# ---------------------------------------------------------------------------
# Derived columns (D8a, stage 1)
# ---------------------------------------------------------------------------


def _rule_mask(rule: dict, frame: pd.DataFrame, token_lists: dict) -> pd.Series:
    """The records one ordered rule's conditions all hold for.

    A condition naming a column the frame lacks makes the rule fire for nobody,
    which is what a track rule already does.
    """
    matches = pd.Series(True, index=frame.index)
    for condition in rule.get("when") or []:
        column = condition.get("column")
        if column not in frame.columns:
            return pd.Series(False, index=frame.index)
        matches &= conditions.evaluate(condition, frame[column], token_lists)
    return matches


def _same_values(left: pd.Series, right: pd.Series) -> pd.Series:
    """Elementwise equality that counts two missing values as the same."""
    both_null = left.isna() & right.isna()
    return both_null | (left == right)


def _derive(df: pd.DataFrame, ruleset: dict):
    """The one derived-column loop. ``(frame, reports)``.

    Reports carry each rule's mask and hit count, plus the before and after
    values, so the preview and the pipeline read the same run of the rules.
    """
    frame = df.copy()
    token_lists = _token_lists(ruleset)
    reports: list[dict] = []

    for column in derived_columns(ruleset):
        target = column.get("target")
        if not target:
            continue

        source = column.get("default_from")
        if source in frame.columns:
            base = frame[source].astype("object")
            base = base.where(base.notna(), None)
        else:
            base = pd.Series([None] * len(frame), index=frame.index, dtype="object")

        tracks = derived_tracks(column)
        if "track" in frame.columns:
            in_scope = frame["track"].isin(tracks)
        else:
            in_scope = pd.Series(True, index=frame.index)

        values = base.copy()
        rule_ids = pd.Series([None] * len(frame), index=frame.index, dtype="object")
        undecided = in_scope.copy()
        rules: list[dict] = []

        for rule in column.get("rules") or []:
            decided = _rule_mask(rule, frame, token_lists) & undecided
            values = values.where(~decided, rule.get("value"))
            rule_ids = rule_ids.where(~decided, rule.get("id"))
            undecided &= ~decided
            rules.append({
                "id": rule.get("id"),
                "description": rule.get("description", ""),
                "value": rule.get("value"),
                "columns": conditions.columns_read(rule),
                "hits": int(decided.sum()),
                "mask": decided,
            })

        # Out-of-scope records are part of the default: they keep the
        # default_from value too, so the hits still add up to the record count.
        untouched = rule_ids.isna()
        rules.append({
            "id": "default",
            "description": f"Everything else keeps its {source} value",
            "value": None,
            "columns": [],
            "hits": int(untouched.sum()),
            "mask": untouched,
        })

        values = _blank_to_null(values)
        frame[target] = values
        frame[f"{target}{RULE_COLUMN_SUFFIX}"] = rule_ids

        changed = ~_same_values(base, values)
        reports.append({
            "id": column.get("id"),
            "target": target,
            "description": column.get("description", ""),
            "default_from": source,
            "tracks": tracks,
            "total": int(len(frame)),
            "changed": int(changed.sum()),
            "before": base,
            "after": values,
            "changed_mask": changed,
            "rules": rules,
        })

    return frame, reports


def apply_derived_columns(df: pd.DataFrame, ruleset: dict) -> pd.DataFrame:
    """Run the derived columns over an already-cleaned frame.

    Returns *df* plus each column's target and its ``<target>_rule``. Columns
    run in document order, so a later one may read an earlier target.
    """
    return _derive(df, ruleset)[0]


def derived_detailed(df: pd.DataFrame, ruleset: dict):
    """``apply_derived_columns`` with the per-rule bookkeeping the preview needs."""
    return _derive(df, ruleset)


def transitions(report: dict, limit: int | None = None) -> list[dict]:
    """``[{from, to, count}]`` for one derived column's report, largest first.

    Counted with a groupby over the changed rows; nothing walks a record.
    """
    changed = report["changed_mask"]
    if not changed.any():
        return []
    pairs = pd.DataFrame({
        "from": report["before"][changed].to_numpy(),
        "to": report["after"][changed].to_numpy(),
    })
    counted = (
        pairs.groupby(["from", "to"], dropna=False, sort=False)
        .size()
        .rename("count")
        .reset_index()
        .sort_values(["count", "from", "to"], ascending=[False, True, True],
                     kind="mergesort")
    )
    if limit is not None:
        counted = counted.head(limit)
    return [
        {
            "from": _cell(row["from"]),
            "to": _cell(row["to"]),
            "count": int(row["count"]),
        }
        for _, row in counted.iterrows()
    ]


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def available_columns(ruleset: dict, track: str, raw_columns) -> dict:
    """What a step in *track* may read, and what each step writes.

    ``all`` is everything a later step, a match key or a Splink rule may name:
    the raw columns, then every cleaning target, then every derived target that
    applies to this track — the order they come into existence.

    ``steps`` stays the cleaning steps alone. The Config screen builds its
    "columns that already exist" list from it, and a derived column must not
    count as a clash with itself.
    """
    raw = list(raw_columns)
    steps = []
    targets: list[str] = []
    for step in cleaning_steps(ruleset, track):
        written = step_targets(step)
        steps.append({"step_id": step.get("id"), "targets": written})
        for column in written:
            if column not in targets and column not in raw:
                targets.append(column)

    derived = []
    derived_names: list[str] = []
    for column in derived_columns(ruleset):
        if track not in derived_tracks(column):
            continue
        written = derived_targets(column)
        derived.append({"derived_id": column.get("id"), "targets": written})
        # The rule column records which rule decided the value. It is not
        # something to block or compare on, so it stays out of `all`.
        name = column.get("target")
        if name and name not in derived_names and name not in targets and name not in raw:
            derived_names.append(name)

    return {
        "raw": raw,
        "steps": steps,
        "derived": derived,
        "targets": targets,
        "derived_targets": derived_names,
        "all": raw + targets + derived_names,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _error(errors: list[dict], path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def _check_token_lists(ruleset: dict, errors: list[dict]) -> None:
    lists = ruleset.get("token_lists", {})
    if not isinstance(lists, dict):
        _error(errors, "token_lists", "token_lists must be an object")
        return
    for name, entry in lists.items():
        path = f"token_lists.{name}"
        if not isinstance(entry, dict):
            _error(errors, path, "A token list must be an object with a tokens list")
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, list) or any(not isinstance(t, str) for t in tokens):
            _error(errors, f"{path}.tokens", "tokens must be a list of strings")


def _check_lookups(ruleset: dict, errors: list[dict]) -> None:
    lookups = ruleset.get("lookups", {})
    if not isinstance(lookups, dict):
        _error(errors, "lookups", "lookups must be an object")
        return
    for name, entry in lookups.items():
        path = f"lookups.{name}"
        if not isinstance(entry, dict):
            _error(errors, path, "A lookup must be an object with rows and a fallback")
            continue
        fallback = entry.get("fallback", "passthrough")
        if fallback not in FALLBACKS:
            _error(errors, f"{path}.fallback",
                   f"fallback must be one of {', '.join(FALLBACKS)}")
        rows = entry.get("rows")
        if not isinstance(rows, list):
            _error(errors, f"{path}.rows", "rows must be a list")
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or "raw" not in row or "canonical" not in row:
                _error(errors, f"{path}.rows[{index}]",
                       "Each row needs a raw and a canonical value")


def _check_condition(condition, path: str, ruleset: dict, check_column, errors) -> None:
    """One condition. *check_column* says whether the column it reads exists,
    which differs between a track rule (raw columns only, because tracks are
    decided before cleaning) and a derived-column rule (anything by then)."""
    if not isinstance(condition, dict):
        _error(errors, path, "A condition must be an object")
        return
    column = condition.get("column")
    if not column:
        _error(errors, f"{path}.column", "A condition needs a column")
    else:
        check_column(column, f"{path}.column")

    op = condition.get("op")
    if op not in conditions.OPERATORS:
        _error(errors, f"{path}.op", f"Unknown condition operator '{op}'")
        return

    required = conditions.OPERATORS[op]
    if required == "lists":
        names = condition.get("lists")
        if not isinstance(names, list) or not names:
            _error(errors, f"{path}.lists", "This operator needs one or more token lists")
        else:
            for name in names:
                if name not in _token_lists(ruleset):
                    _error(errors, f"{path}.lists", f"Unknown token list '{name}'")
    elif required == "values":
        if not isinstance(condition.get("values"), list):
            _error(errors, f"{path}.values", "This operator needs a list of values")
    elif required == "pattern":
        try:
            re.compile(condition.get("pattern") or "")
        except re.error as exc:
            _error(errors, f"{path}.pattern", f"Invalid regular expression: {exc}")
    elif required == "value":
        if condition.get("value") is None:
            _error(errors, f"{path}.value", "This operator needs a value")


def _check_track_rules(ruleset: dict, raw_columns, errors: list[dict]) -> None:
    rules = ruleset.get("track_rules", [])
    if not isinstance(rules, list):
        _error(errors, "track_rules", "track_rules must be a list")
        return
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        path = f"track_rules[{index}]"
        if not isinstance(rule, dict):
            _error(errors, path, "A track rule must be an object")
            continue
        rule_id = rule.get("id")
        if not rule_id:
            _error(errors, f"{path}.id", "A track rule needs an id")
        elif rule_id in seen:
            _error(errors, f"{path}.id", f"Duplicate rule id '{rule_id}'")
        else:
            seen.add(rule_id)
        if rule.get("track") not in TRACK_KEYS:
            _error(errors, f"{path}.track",
                   f"track must be one of {', '.join(TRACK_KEYS)}")
        when = rule.get("when")
        if not isinstance(when, list) or not when:
            _error(errors, f"{path}.when", "A track rule needs at least one condition")
            continue
        for position, condition in enumerate(when):
            _check_condition(condition, f"{path}.when[{position}]",
                             ruleset, _raw_column_check(raw_columns, errors), errors)


def _raw_column_check(raw_columns, errors):
    """Track rules run before cleaning, so they may only read raw columns."""
    def check(column: str, path: str) -> None:
        if column not in raw_columns:
            _error(errors, path, f"'{column}' is not a column of the input records")
    return check


def _check_step(step, path: str, ruleset: dict, raw_columns, known, errors) -> list[str]:
    """Validate one cleaning step and return the columns it adds to *known*."""
    if not isinstance(step, dict):
        _error(errors, path, "A cleaning step must be an object")
        return []

    op = step.get("op")
    if op not in OPS:
        _error(errors, f"{path}.op", f"Unknown op '{op}'")
        return []

    source = step.get("source")
    if not source:
        _error(errors, f"{path}.source", "A step needs a source column")
    elif source not in known:
        _error(errors, f"{path}.source",
               f"'{source}' is neither a raw column nor written by an earlier step")

    spec = None
    if op == "function":
        spec = functions.get(step.get("name"))
        if spec is None:
            _error(errors, f"{path}.name", f"Unknown function '{step.get('name')}'")
            return []

    needs_target = not (spec is not None and spec.multi_output)
    target = step.get("target")
    if needs_target and not target:
        _error(errors, f"{path}.target", "A step needs a target column")

    written = step_targets(step)
    for column in written:
        if column in raw_columns:
            _error(errors, f"{path}.target",
                   f"'{column}' is a raw column and may not be overwritten")

    if op == "strip_tokens":
        position = step.get("position", "leading")
        if position not in POSITIONS:
            _error(errors, f"{path}.position",
                   f"position must be one of {', '.join(POSITIONS)}")
        _check_lists(step, path, ruleset, errors)
    elif op == "nullify":
        _check_lists(step, path, ruleset, errors)
    elif op == "regex_replace":
        try:
            re.compile(step.get("pattern") or "")
        except re.error as exc:
            _error(errors, f"{path}.pattern", f"Invalid regular expression: {exc}")
    elif op == "lookup":
        name = step.get("table")
        if name not in _lookups(ruleset):
            _error(errors, f"{path}.table", f"Unknown lookup '{name}'")
        scope = step.get("scope", "value")
        if scope not in SCOPES:
            _error(errors, f"{path}.scope", f"scope must be one of {', '.join(SCOPES)}")

    return written


def _check_lists(step: dict, path: str, ruleset: dict, errors: list[dict]) -> None:
    names = step.get("lists")
    if not isinstance(names, list) or not names:
        _error(errors, f"{path}.lists", "This op needs one or more token lists")
        return
    for name in names:
        if name not in _token_lists(ruleset):
            _error(errors, f"{path}.lists", f"Unknown token list '{name}'")


def _check_cleaning(ruleset: dict, raw_columns, errors: list[dict]) -> dict[str, list[str]]:
    """Validate every track's steps. Returns the columns each track ends with."""
    per_track: dict[str, list[str]] = {}
    cleaning = ruleset.get("cleaning", {})
    if not isinstance(cleaning, dict):
        _error(errors, "cleaning", "cleaning must be an object keyed on track")
        return {track: list(raw_columns) for track in TRACK_KEYS}

    for track in cleaning:
        if track not in TRACK_KEYS:
            _error(errors, f"cleaning.{track}",
                   f"Unknown track '{track}' — expected {', '.join(TRACK_KEYS)}")

    for track in TRACK_KEYS:
        known = list(raw_columns)
        steps = cleaning.get(track, [])
        if not isinstance(steps, list):
            _error(errors, f"cleaning.{track}", "A track's cleaning must be a list of steps")
            per_track[track] = known
            continue
        seen: set[str] = set()
        for index, step in enumerate(steps):
            path = f"cleaning.{track}[{index}]"
            if isinstance(step, dict):
                step_id = step.get("id")
                if not step_id:
                    _error(errors, f"{path}.id", "A step needs an id")
                elif step_id in seen:
                    _error(errors, f"{path}.id", f"Duplicate step id '{step_id}'")
                else:
                    seen.add(step_id)
            for column in _check_step(step, path, ruleset, raw_columns, known, errors):
                if column not in known:
                    known.append(column)
        per_track[track] = known
    return per_track


def _check_derived_columns(ruleset: dict, raw_columns, per_track: dict,
                           errors: list[dict]) -> dict[str, list[str]]:
    """Validate the derived columns and return each track's columns with them added.

    Derived columns run after cleaning and in document order, so what a rule may
    read grows as the list is walked — and differs per track, because a column
    scoped to organisations never exists for a person.
    """
    known = {track: list(columns) for track, columns in per_track.items()}
    section = ruleset.get("derived_columns", [])
    if not isinstance(section, list):
        _error(errors, "derived_columns", "derived_columns must be a list")
        return known

    cleaning_targets = {c for columns in per_track.values() for c in columns}
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()

    for index, column in enumerate(section):
        path = f"derived_columns[{index}]"
        if not isinstance(column, dict):
            _error(errors, path, "A derived column must be an object")
            continue

        column_id = column.get("id")
        if not column_id:
            _error(errors, f"{path}.id", "A derived column needs an id")
        elif column_id in seen_ids:
            _error(errors, f"{path}.id", f"Duplicate derived column id '{column_id}'")
        else:
            seen_ids.add(column_id)

        tracks = column.get("tracks")
        if tracks is not None and not isinstance(tracks, list):
            _error(errors, f"{path}.tracks", "tracks must be a list of track keys")
            tracks = None
        applies = list(TRACK_KEYS)
        if isinstance(tracks, list) and tracks:
            unknown = [t for t in tracks if t not in TRACK_KEYS]
            if unknown:
                _error(errors, f"{path}.tracks",
                       f"Unknown track '{unknown[0]}' — expected {', '.join(TRACK_KEYS)}")
            applies = [t for t in tracks if t in TRACK_KEYS]

        target = _check_derived_target(column, path, raw_columns, cleaning_targets,
                                       seen_targets, errors)

        source = column.get("default_from")
        if not source:
            _error(errors, f"{path}.default_from", "A derived column needs a default_from column")
        else:
            missing = [t for t in applies if source not in known.get(t, [])]
            if missing:
                _error(errors, f"{path}.default_from",
                       f"'{source}' is not a column of the {' or '.join(missing)} track")

        _check_derived_rules(column, path, ruleset, known, applies, errors)

        if target:
            seen_targets.add(target)
            for track in applies:
                if target not in known.setdefault(track, []):
                    known[track].append(target)

    return known


def _check_derived_target(column, path, raw_columns, cleaning_targets, seen_targets, errors):
    """A derived target must be a new column of its own. Returns it, or None."""
    target = column.get("target")
    if not target:
        _error(errors, f"{path}.target", "A derived column needs a target")
        return None
    if not COLUMN_NAME_RE.match(str(target)):
        _error(errors, f"{path}.target",
               "target must be lower case letters, digits and underscores, starting with a letter")
        return None
    if target.endswith(RULE_COLUMN_SUFFIX):
        _error(errors, f"{path}.target",
               f"target may not end with '{RULE_COLUMN_SUFFIX}' — "
               "that name is taken by the column recording which rule decided the value")
        return None
    if target in raw_columns:
        _error(errors, f"{path}.target", f"'{target}' is a raw column and may not be overwritten")
        return None
    if target in cleaning_targets:
        _error(errors, f"{path}.target", f"'{target}' is already written by a cleaning step")
        return None
    if target in seen_targets:
        _error(errors, f"{path}.target", f"'{target}' is already written by another derived column")
        return None
    return target


def _check_derived_rules(column, path, ruleset, known, applies, errors) -> None:
    rules = column.get("rules", [])
    if not isinstance(rules, list):
        _error(errors, f"{path}.rules", "rules must be a list")
        return

    def check(name: str, at: str) -> None:
        missing = [t for t in applies if name not in known.get(t, [])]
        if missing:
            _error(errors, at, f"'{name}' is not a column of the {' or '.join(missing)} track")

    seen: set[str] = set()
    for index, rule in enumerate(rules):
        rule_path = f"{path}.rules[{index}]"
        if not isinstance(rule, dict):
            _error(errors, rule_path, "A rule must be an object")
            continue

        rule_id = rule.get("id")
        if not rule_id:
            _error(errors, f"{rule_path}.id", "A rule needs an id")
        elif rule_id in seen:
            _error(errors, f"{rule_path}.id", f"Duplicate rule id '{rule_id}'")
        else:
            seen.add(rule_id)

        value = rule.get("value")
        if not isinstance(value, str) or not value.strip():
            _error(errors, f"{rule_path}.value", "A rule needs a value to set")

        when = rule.get("when")
        if not isinstance(when, list) or not when:
            _error(errors, f"{rule_path}.when", "A rule needs at least one condition")
            continue
        for position, condition in enumerate(when):
            _check_condition(condition, f"{rule_path}.when[{position}]", ruleset, check, errors)


def _check_match_keys(ruleset: dict, per_track: dict, errors: list[dict]) -> None:
    keys = ruleset.get("match_keys", [])
    if not isinstance(keys, list):
        _error(errors, "match_keys", "match_keys must be a list")
        return
    seen: set[str] = set()
    for index, key in enumerate(keys):
        path = f"match_keys[{index}]"
        if not isinstance(key, dict):
            _error(errors, path, "A match key must be an object")
            continue
        key_id = key.get("id")
        if not key_id:
            _error(errors, f"{path}.id", "A match key needs an id")
        elif key_id in seen:
            _error(errors, f"{path}.id", f"Duplicate match key id '{key_id}'")
        else:
            seen.add(key_id)

        track = key.get("track")
        if track not in TRACK_KEYS:
            _error(errors, f"{path}.track", f"Unknown track '{track}'")
            known = None
        else:
            known = per_track.get(track)

        columns = key.get("columns")
        if not isinstance(columns, list) or not columns:
            _error(errors, f"{path}.columns", "A match key needs at least one column")
        elif known is not None:
            for column in columns:
                if column not in known:
                    _error(errors, f"{path}.columns",
                           f"'{column}' is not a column of the {track} track")

        tier = key.get("tier", 1)
        if isinstance(tier, bool) or not isinstance(tier, int) or tier < 1:
            _error(errors, f"{path}.tier", "tier must be a whole number of 1 or more")

        applies_when = key.get("applies_when", "always")
        if applies_when not in APPLIES_WHEN:
            _error(errors, f"{path}.applies_when",
                   f"applies_when must be one of {', '.join(APPLIES_WHEN)}")
        on_fail = key.get("on_guard_fail", "review")
        if on_fail not in ON_GUARD_FAIL:
            _error(errors, f"{path}.on_guard_fail",
                   f"on_guard_fail must be one of {', '.join(ON_GUARD_FAIL)}")

        guards = key.get("guards", {})
        if not isinstance(guards, dict):
            _error(errors, f"{path}.guards", "guards must be an object")
            continue
        _check_key_guards(guards, path, track, known, ruleset, errors)


def _check_key_guards(guards, path, track, known, ruleset, errors) -> None:
    """The guard block of one match key. Columns are knowable by now — the
    cleaning steps have been read — so a guard naming a column that track never
    produces is caught here rather than at run time."""
    blocklists = guards.get("blocklists", [])
    if blocklists is None:
        blocklists = []
    if not isinstance(blocklists, list) or any(not isinstance(n, str) for n in blocklists):
        _error(errors, f"{path}.guards.blocklists", "blocklists must be a list of token list names")
    else:
        for name in blocklists:
            if name not in _token_lists(ruleset):
                _error(errors, f"{path}.guards.blocklists", f"Unknown token list '{name}'")

    size = guards.get("max_group_size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 2):
        _error(errors, f"{path}.guards.max_group_size",
               "max_group_size must be a whole number of 2 or more")

    max_distinct = guards.get("max_distinct")
    if max_distinct is not None:
        if not isinstance(max_distinct, dict):
            _error(errors, f"{path}.guards.max_distinct",
                   "max_distinct must be an object with a column and a count")
        else:
            column = max_distinct.get("column")
            if not column:
                _error(errors, f"{path}.guards.max_distinct.column",
                       "max_distinct needs a column")
            elif known is not None and column not in known:
                _error(errors, f"{path}.guards.max_distinct.column",
                       f"'{column}' is not a column of the {track} track")
            count = max_distinct.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                _error(errors, f"{path}.guards.max_distinct.count",
                       "max_distinct count must be a whole number of 1 or more")

    corroborators = guards.get("require_any_equal", [])
    if corroborators is None:
        corroborators = []
    if not isinstance(corroborators, list) or any(not isinstance(n, str) for n in corroborators):
        _error(errors, f"{path}.guards.require_any_equal",
               "require_any_equal must be a list of column names")
    else:
        for name in corroborators:
            if known is not None and name not in known:
                _error(errors, f"{path}.guards.require_any_equal",
                       f"'{name}' is not a column of the {track} track")


def validate_ruleset(ruleset, raw_columns) -> list[dict]:
    """Every problem with *ruleset*, as ``[{path, message}]``. Empty means good.

    *raw_columns* is the profile's record columns — the only columns a track
    rule may read and the only ones a step may not overwrite.
    """
    errors: list[dict] = []
    if not isinstance(ruleset, dict):
        return [{"path": "", "message": "A ruleset must be an object"}]

    raw = list(raw_columns)
    _check_token_lists(ruleset, errors)
    _check_lookups(ruleset, errors)
    _check_track_rules(ruleset, raw, errors)

    if ruleset.get("default_track") not in TRACK_KEYS:
        _error(errors, "default_track",
               f"default_track must be one of {', '.join(TRACK_KEYS)}")

    per_track = _check_cleaning(ruleset, raw, errors)
    # A match key runs on the frame stage 1 wrote, so it may name a derived
    # column as well as a cleaning target.
    with_derived = _check_derived_columns(ruleset, raw, per_track, errors)
    _check_match_keys(ruleset, with_derived, errors)

    if not isinstance(ruleset.get("vetoes", []), list):
        _error(errors, "vetoes", "vetoes must be a list")

    return errors

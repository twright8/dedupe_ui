# backend/app/rules/linkage.py
"""``linkage_settings``: read it, build Splink objects from it, and validate it.

The settings document is described in ``docs/LINKAGE.md``. It sits beside the
ruleset in every config version and says, per track, how to block, what to
compare and what the budget is.

Everything that turns a user's JSON into a Splink object lives here, so the
scoring stage and the Config screen's validation agree on what is allowed. A
``splink_function`` outside the allow-list never reaches Splink.
"""

import re

TRACK_KEYS = ("person", "organisation")

# The fixed allow-list from LINKAGE.md. The UI offers these and nothing else,
# and anything else is a validation error rather than a run-time crash.
SPLINK_FUNCTIONS = (
    "cl.ExactMatch",
    "cl.JaroWinklerAtThresholds",
    "cl.JaroAtThresholds",
    "cl.LevenshteinAtThresholds",
    "cl.DamerauLevenshteinAtThresholds",
    "cl.JaccardAtThresholds",
    "cl.NameComparison",
    "cl.ForenameSurnameComparison",
    "cl.PostcodeComparison",
    "cl.ArrayIntersectAtSizes",
)

DEFAULT_CANDIDATE = 0.05
DEFAULT_REVIEW = 0.50
DEFAULT_HIGH = 0.92
DEFAULT_EM_ITERATIONS = 20
DEFAULT_MAX_PAIRS = 20_000_000
# Assumed recall of the deterministic rules, used only when
# probability_two_random_records_match is null and Splink has to estimate it.
DEFAULT_DETERMINISTIC_RECALL = 0.8

BUCKETS = ("accept", "review", "reject")
DECIDED_BY = ("score", "import", "human")

_SIMPLE_EQ_RE = re.compile(r"^l\.(\w+)\s*=\s*r\.(\w+)$")
_SIDE_COLUMN_RE = re.compile(r"\b[lr]\.(\w+)")


# ---------------------------------------------------------------------------
# Reading the document — user data, so never assume a key is there
# ---------------------------------------------------------------------------


def tracks(settings: dict) -> dict:
    value = (settings or {}).get("tracks")
    return value if isinstance(value, dict) else {}


def track_settings(settings: dict, track: str) -> dict:
    value = tracks(settings).get(track)
    return value if isinstance(value, dict) else {}


def blocking_rules(track_config: dict) -> list[dict]:
    """The track's blocking rules, each ``{id, description, sql}``."""
    rules = track_config.get("blocking_rules")
    if not isinstance(rules, list):
        return []
    out = []
    for index, rule in enumerate(rules):
        if isinstance(rule, str):
            out.append({"id": f"b{index + 1}", "description": "", "sql": rule})
        elif isinstance(rule, dict) and rule.get("sql"):
            out.append({
                "id": rule.get("id") or f"b{index + 1}",
                "description": rule.get("description") or "",
                "sql": str(rule["sql"]),
            })
    return out


def comparisons(track_config: dict) -> list[dict]:
    value = track_config.get("comparisons")
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def em_rules(track_config: dict) -> list[str]:
    """The EM training rules, falling back to the blocking rules when absent."""
    value = track_config.get("em_blocking_rules")
    if isinstance(value, list) and value:
        return [str(v) for v in value if isinstance(v, str) and v.strip()]
    return [rule["sql"] for rule in blocking_rules(track_config)]


def max_pairs(track_config: dict) -> int:
    value = track_config.get("max_pairs", DEFAULT_MAX_PAIRS)
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PAIRS


def thresholds(settings: dict) -> tuple[float, float, float]:
    """``(candidate, review, high)`` — the three lines, in order."""
    settings = settings or {}
    return (
        float(settings.get("match_probability_threshold_candidate", DEFAULT_CANDIDATE)),
        float(settings.get("match_probability_threshold_review", DEFAULT_REVIEW)),
        float(settings.get("match_probability_threshold_high", DEFAULT_HIGH)),
    )


def sql_columns(sql: str) -> set[str]:
    """Every column a blocking rule names on either side."""
    return set(_SIDE_COLUMN_RE.findall(sql or ""))


# ---------------------------------------------------------------------------
# Building Splink objects
# ---------------------------------------------------------------------------


def build_blocking_rule(sql: str):
    """A Splink blocking rule from one SQL string.

    A plain ``l.col = r.col`` becomes ``block_on(col)``, which Splink can plan
    better than raw SQL. Anything else is passed through verbatim.
    """
    from splink import block_on
    from splink.blocking_rule_library import CustomRule

    match = _SIMPLE_EQ_RE.match((sql or "").strip())
    if match and match.group(1) == match.group(2):
        return block_on(match.group(1))
    return CustomRule(sql)


def build_comparison(spec: dict):
    """A Splink comparison from one ``comparisons`` entry."""
    import splink.comparison_library as cl

    name = spec.get("splink_function")
    if name not in SPLINK_FUNCTIONS:
        raise ValueError(f"'{name}' is not an allowed splink_function")
    factory = getattr(cl, name.split(".", 1)[1])
    args = spec.get("splink_args") or {}
    comparison = factory(spec["column"], **args)
    if spec.get("term_frequency"):
        comparison = comparison.configure(term_frequency_adjustments=True)
    return comparison


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _error(errors: list[dict], path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def _check_thresholds(settings: dict, errors: list[dict]) -> None:
    lines = []
    for key, default in (
        ("match_probability_threshold_candidate", DEFAULT_CANDIDATE),
        ("match_probability_threshold_review", DEFAULT_REVIEW),
        ("match_probability_threshold_high", DEFAULT_HIGH),
    ):
        value = settings.get(key, default)
        path = f"linkage_settings.{key}"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _error(errors, path, f"{key} must be a number between 0 and 1")
            lines.append(None)
            continue
        if not 0 <= float(value) <= 1:
            _error(errors, path, f"{key} must be between 0 and 1")
            lines.append(None)
            continue
        lines.append(float(value))

    candidate, review, high = lines
    if candidate is not None and review is not None and candidate > review:
        _error(errors, "linkage_settings.match_probability_threshold_review",
               "The review threshold must be at or above the candidate threshold")
    if review is not None and high is not None and review > high:
        _error(errors, "linkage_settings.match_probability_threshold_high",
               "The high threshold must be at or above the review threshold")


def _check_track(track: str, config: dict, known: set[str], errors: list[dict]) -> None:
    base = f"linkage_settings.tracks.{track}"
    if not isinstance(config, dict):
        _error(errors, base, "A track's settings must be an object")
        return

    raw_rules = config.get("blocking_rules")
    if raw_rules is not None and not isinstance(raw_rules, list):
        _error(errors, f"{base}.blocking_rules", "blocking_rules must be a list")
    else:
        for index, rule in enumerate(raw_rules or []):
            path = f"{base}.blocking_rules[{index}]"
            if isinstance(rule, str):
                sql = rule
            elif isinstance(rule, dict):
                sql = rule.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    _error(errors, f"{path}.sql", "A blocking rule needs SQL")
                    continue
            else:
                _error(errors, path, "A blocking rule must be SQL text or an object")
                continue
            for column in sorted(sql_columns(sql)):
                if column not in known:
                    _error(errors, f"{path}.sql",
                           f"'{column}' is not a column of the {track} track")

    raw_comparisons = config.get("comparisons")
    if raw_comparisons is not None and not isinstance(raw_comparisons, list):
        _error(errors, f"{base}.comparisons", "comparisons must be a list")
    else:
        for index, spec in enumerate(raw_comparisons or []):
            path = f"{base}.comparisons[{index}]"
            if not isinstance(spec, dict):
                _error(errors, path, "A comparison must be an object")
                continue
            function = spec.get("splink_function")
            if function not in SPLINK_FUNCTIONS:
                _error(errors, f"{path}.splink_function",
                       f"splink_function must be one of {', '.join(SPLINK_FUNCTIONS)}")
            column = spec.get("column")
            if not isinstance(column, str) or not column:
                _error(errors, f"{path}.column", "A comparison needs a column")
            elif column not in known:
                _error(errors, f"{path}.column",
                       f"'{column}' is not a column of the {track} track")
            args = spec.get("splink_args")
            if args is not None and not isinstance(args, dict):
                _error(errors, f"{path}.splink_args", "splink_args must be an object")
            elif isinstance(args, dict):
                # A two-column comparison names its second column in the args.
                for key, value in args.items():
                    if key.endswith("_col_name") and isinstance(value, str) \
                            and value not in known:
                        _error(errors, f"{path}.splink_args.{key}",
                               f"'{value}' is not a column of the {track} track")

    em = config.get("em_blocking_rules")
    if em is not None and not isinstance(em, list):
        _error(errors, f"{base}.em_blocking_rules", "em_blocking_rules must be a list")
    elif isinstance(em, list):
        for index, rule in enumerate(em):
            path = f"{base}.em_blocking_rules[{index}]"
            if not isinstance(rule, str) or not rule.strip():
                _error(errors, path, "An EM blocking rule must be SQL text")
                continue
            for column in sorted(sql_columns(rule)):
                if column not in known:
                    _error(errors, path,
                           f"'{column}' is not a column of the {track} track")

    budget = config.get("max_pairs", DEFAULT_MAX_PAIRS)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        _error(errors, f"{base}.max_pairs", "max_pairs must be a whole number above zero")


def validate_linkage_settings(settings, ruleset: dict, raw_columns) -> list[dict]:
    """Every problem with *settings*, as ``[{path, message}]``. Empty means good.

    The columns a track may name come from the ruleset: the profile's raw
    columns plus everything that track's cleaning steps write. A blocking rule
    over a column no rule produces would fail deep inside Splink, so it is
    caught here instead.
    """
    from app.rules import engine

    errors: list[dict] = []
    if settings is None:
        return errors
    if not isinstance(settings, dict):
        return [{"path": "linkage_settings", "message": "linkage_settings must be an object"}]

    _check_thresholds(settings, errors)

    raw = list(raw_columns)
    by_track = tracks(settings)
    if settings.get("tracks") is not None and not isinstance(settings.get("tracks"), dict):
        _error(errors, "linkage_settings.tracks", "tracks must be an object")
        return errors

    for track, config in by_track.items():
        if track not in TRACK_KEYS:
            _error(errors, f"linkage_settings.tracks.{track}",
                   f"Unknown track '{track}'")
            continue
        known = set(engine.available_columns(ruleset, track, raw)["all"])
        # Every unit row also carries these, so a rule may block or compare on them.
        known.update({"unit_id", "unit_size", "held_group_id", "n_existing_ids"})
        _check_track(track, config, known, errors)

    iterations = settings.get("em_iterations", DEFAULT_EM_ITERATIONS)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        _error(errors, "linkage_settings.em_iterations",
               "em_iterations must be a whole number of 1 or more")

    prior = settings.get("probability_two_random_records_match")
    if prior is not None:
        if isinstance(prior, bool) or not isinstance(prior, (int, float)) \
                or not 0 < float(prior) < 1:
            _error(errors, "linkage_settings.probability_two_random_records_match",
                   "probability_two_random_records_match must be null or between 0 and 1")

    return errors

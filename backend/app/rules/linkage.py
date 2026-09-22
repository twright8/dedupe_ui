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

from app import vocabulary

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

# Comparisons this tool builds rather than taking from Splink's library
# (docs/LINKAGE.md). `custom.NumericDifferenceAtThresholds` makes one level per
# threshold in ascending order plus "all other", so a large gap between two
# numbers — two birth years decades apart — learns its own weight instead of
# being averaged in with off-by-one typos.
NUMERIC_DIFFERENCE = "custom.NumericDifferenceAtThresholds"
CUSTOM_FUNCTIONS = (NUMERIC_DIFFERENCE,)

# Everything a `splink_function` may be. The UI offers these and nothing else.
COMPARISON_FUNCTIONS = SPLINK_FUNCTIONS + CUSTOM_FUNCTIONS

DEFAULT_CANDIDATE = 0.05
DEFAULT_REVIEW = 0.50
DEFAULT_HIGH = 0.92
# The accept line a track sets for itself, over the one line every track
# starts from. `{"person": 0.96}` (docs/LINKAGE.md).
HIGH_BY_TRACK_KEY = "match_probability_threshold_high_by_track"
DEFAULT_EM_ITERATIONS = 20
DEFAULT_MAX_PAIRS = 20_000_000
# Assumed recall of the deterministic rules, used only when
# probability_two_random_records_match is null and Splink has to estimate it.
DEFAULT_DETERMINISTIC_RECALL = 0.8

# The stage 4 gate (docs/ENTITIES.md). A cluster held together by a pair below
# the floor may be a chain; one over the unit cap is a runaway attractor; one
# carrying more than max_existing_ids earlier ids would merge groups the manual
# work deliberately kept apart.
DEFAULT_CLUSTER_FLOOR = 0.20
DEFAULT_MAX_CLUSTER_UNITS = 200
DEFAULT_MAX_EXISTING_IDS = 1

# One definition each, in `app/vocabulary.py`, so a label and a validation
# message can never name a value this module does not know.
BUCKETS = vocabulary.BUCKETS
DECIDED_BY = vocabulary.DECIDED_BY

# The hot-key blocking control (docs/LINKAGE.md). A blocking rule may carry
# these four optional keys; a rule without them blocks exactly as it always did.
BLOCK_CONTROL_KEYS = ("max_block_size", "on_oversize", "refine_with", "drop_above")
ON_OVERSIZE = ("refine", "drop")

# The oversized key values are inlined into the generated SQL as a literal list,
# so the list has to stay short. A rule that puts more keys than this over the
# limit does not have a hot-key problem; it is a coarse rule, and refining a few
# thousand separate key values one by one is not what this control is for.
#: How many oversized key values the control will inline into a rule's SQL.
#: The keys have nowhere else to live — a Splink blocking rule is a predicate
#: over ``l.`` and ``r.`` columns — so this is the cap on how coarse a rule the
#: control can fix. The PSC person routes need 11,725 to 91,047 of them at
#: ``max_block_size: 20`` on the full snapshot, which generates 4.8 MB of SQL
#: across the six rules and takes under a second a rule to build. It is
#: deliberately not unlimited: a rule needing millions of keys is a rule to
#: rewrite, not to patch.
MAX_INLINE_KEYS = 100_000

# Joins the parts of a composite blocking key into one string. Unit separator:
# it cannot appear in a cleaned value, so two different keys cannot collide.
_KEY_SEPARATOR = "\x1f"

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
            normalised = {
                "id": rule.get("id") or f"b{index + 1}",
                "description": rule.get("description") or "",
                "sql": str(rule["sql"]),
            }
            # The hot-key control's keys ride along untouched when the rule has
            # them. A rule without them keeps exactly the shape it always had.
            for key in BLOCK_CONTROL_KEYS:
                if key in rule:
                    normalised[key] = rule[key]
            out.append(normalised)
    return out


def comparisons(track_config: dict) -> list[dict]:
    value = track_config.get("comparisons")
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def em_entries(track_config: dict) -> list[dict]:
    """The EM training rules as dicts, whether written as SQL text or as one.

    An entry may carry the same four hot-key keys a prediction rule may
    (``docs/LINKAGE.md``). It has to be able to: an EM rule is priced against
    the same ``max_pairs`` and nothing downstream trims it, and PSC's person
    rule "both name sounds agree" makes **582,535,879** training pairs on the
    full snapshot against 94,743,605 for the whole of prediction. The blocks
    that do it are the placeholder-name blocks of section 19 of
    ``PSC_HANDOVER.md``, which is what the control is for.
    """
    value = track_config.get("em_blocking_rules")
    if isinstance(value, list) and value:
        entries = []
        for item in value:
            rule = as_rule(item)
            if str(rule.get("sql") or "").strip():
                entries.append(rule)
        return entries
    return [dict(rule) for rule in blocking_rules(track_config)]


def em_rules(track_config: dict) -> list[str]:
    """The EM training rules' SQL, before any hot-key control."""
    return [str(rule["sql"]) for rule in em_entries(track_config)]


def max_pairs(track_config: dict) -> int:
    value = track_config.get("max_pairs", DEFAULT_MAX_PAIRS)
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PAIRS


def thresholds(settings: dict) -> tuple[float, float, float]:
    """``(candidate, review, high)`` — the three lines, in order.

    The third is the accept line every track starts from. A track that names
    its own line in ``match_probability_threshold_high_by_track`` overrides it;
    ``high_by_track`` and ``accept_line`` are what read that, and this function
    is unchanged so that a caller with no pair in front of it — the budget
    check, the candidate floor — still gets one number.
    """
    settings = settings or {}
    return (
        float(settings.get("match_probability_threshold_candidate", DEFAULT_CANDIDATE)),
        float(settings.get("match_probability_threshold_review", DEFAULT_REVIEW)),
        float(settings.get("match_probability_threshold_high", DEFAULT_HIGH)),
    )


def high_by_track(settings: dict) -> dict[str, float]:
    """``{track: accept line}`` — only the tracks that set one of their own.

    The two tracks want different lines. Measured on the donations sheet, the
    person track needs 0.96 to buy back the precision the surname fix cost and
    pays 0.0009 of recall for it, while the organisation track pays 0.0054 of
    recall at the same line for a precision gain nobody asked for
    (``donations_surname_em_2026-09-22.md``). One number cannot serve both.

    A track missing from here uses ``match_probability_threshold_high``, so an
    empty result means the tool behaves exactly as it did before this existed.
    A malformed entry is dropped rather than guessed at;
    ``validate_linkage_settings`` is what tells the user about it.
    """
    raw = (settings or {}).get(HIGH_BY_TRACK_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for track, value in raw.items():
        if track not in TRACK_KEYS:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not 0 <= float(value) <= 1:
            continue
        out[str(track)] = float(value)
    return out


def accept_line(settings: dict, track: str | None = None) -> float:
    """One track's accept line: its own, or the line every track starts from."""
    lines = high_by_track(settings)
    if track is not None and track in lines:
        return lines[track]
    return thresholds(settings)[2]


def accept_lines(settings: dict, track_keys=None) -> dict[str, float]:
    """The accept line of every track named, the default filled in.

    This is what the New run form shows one slider per, and what a run records
    as the lines it used. *track_keys* defaults to the tracks the settings
    document actually describes, so a profile with one track gets one line.
    """
    if track_keys is None:
        named = [t for t in TRACK_KEYS if t in tracks(settings)]
        track_keys = named or list(TRACK_KEYS)
    default = thresholds(settings)[2]
    lines = high_by_track(settings)
    return {track: lines.get(track, default) for track in track_keys}


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


# ---------------------------------------------------------------------------
# The hot-key blocking control
#
# A blocking rule puts units into blocks, and a block of n units makes
# n(n-1)/2 pairs, so a handful of very common keys can carry most of the
# workload. On the PSC sample, 30 blocks out of 356,138 carry 38% of every pair
# route pb3 makes. It gets worse with scale, because a hot block's pairs grow
# with the square of the data while a selective block's grow linearly.
#
# The control refines an oversized block by also requiring extra columns to
# agree, and drops one that is still too big. Everything here works on counts
# and on SQL text. No pair is ever made in order to be thrown away.
# ---------------------------------------------------------------------------


_L_REF_RE = re.compile(r"\bl\.(\w+)", re.IGNORECASE)
_R_REF_RE = re.compile(r"\br\.(\w+)", re.IGNORECASE)


def _norm(text: str) -> str:
    """One SQL fragment flattened so two spellings of it compare equal."""
    return " ".join((text or "").split()).lower()


def _retarget(expr: str, prefix: str) -> str:
    """An ``l.``-side fragment rewritten to another side, or to no side at all.

    ``prefix`` is ``"r."`` for the right-hand side and ``""`` for a query over
    the units table itself, where the columns carry no alias.
    """
    return _L_REF_RE.sub(lambda m: f"{prefix}{m.group(1)}", expr)


def _scan(text: str):
    """Walk *text* yielding ``(index, char, depth)`` outside string literals."""
    depth, quote, index = 0, None, 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        yield index, char, depth
        index += 1


def _word_at(text: str, index: int, word: str) -> bool:
    """True when *word* starts at *index* and is not part of a longer word."""
    end = index + len(word)
    if text[index:end].upper() != word:
        return False
    before = text[index - 1] if index else " "
    after = text[end] if end < len(text) else " "
    return not (before.isalnum() or before == "_") and \
        not (after.isalnum() or after == "_")


def _split_conjuncts(sql: str) -> list[str] | None:
    """The top-level ``AND`` parts of *sql*, or None when a top-level ``OR`` runs.

    A rule mixing ``AND`` and ``OR`` at the top level does not have one blocking
    key, so it is not taken apart at all.
    """
    text = sql or ""
    parts, start, skip_to = [], 0, 0
    for index, char, depth in _scan(text):
        if index < skip_to or depth != 0 or not char.isalpha():
            continue
        if _word_at(text, index, "OR"):
            return None
        if _word_at(text, index, "AND"):
            parts.append(text[start:index])
            start = index + 3
            skip_to = start
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _split_on(text: str, operator: str) -> tuple[str, str] | None:
    """*text* cut at its first top-level *operator*, or None when it has none."""
    for index, char, depth in _scan(text):
        if depth != 0 or not text.startswith(operator, index):
            continue
        if operator == "=":
            if index and text[index - 1] in "<>!=":
                continue
            if index + 1 < len(text) and text[index + 1] == "=":
                continue
        return text[:index].strip(), text[index + len(operator):].strip()
    return None


def _sides(text: str) -> str:
    """Which of ``l.`` and ``r.`` a fragment names: ``l``, ``r``, ``both``, ``none``."""
    left = bool(_L_REF_RE.search(text))
    right = bool(_R_REF_RE.search(text))
    if left and right:
        return "both"
    if left:
        return "l"
    if right:
        return "r"
    return "none"


def block_shape(sql: str) -> dict:
    """What a blocking rule blocks on, read out of its SQL.

    Returns ``{"keys", "filters", "not_equal", "residual"}``. All four hold
    ``l.``-side SQL fragments.

    - ``keys`` — the expressions the rule forces to agree, such as
      ``l.surname_metaphone`` or ``substr(l.surname_clean, 1, 3)``. Together
      they are the block's key.
    - ``filters`` — the conditions that name one side only, such as
      ``l.name_fingerprint <> ''``. Both units of a pair satisfy them, so they
      are a filter on the units.
    - ``not_equal`` — the expressions the rule forces to *differ*, written
      ``l.x <> r.x``.
    - ``residual`` — anything else, verbatim. Counting cannot apply these, so a
      rule that has any is priced as an upper bound.
    """
    shape = {"keys": [], "filters": [], "not_equal": [], "residual": []}
    conjuncts = _split_conjuncts(sql)
    if conjuncts is None:
        shape["residual"].append((sql or "").strip())
        return shape
    for conjunct in conjuncts:
        body = conjunct
        while body.startswith("(") and body.endswith(")") and \
                _split_conjuncts(body[1:-1]) is not None and \
                len(_split_conjuncts(body[1:-1]) or []) == 1:
            body = body[1:-1].strip()
        where = _sides(body)
        if where in ("l", "r"):
            shape["filters"].append(_R_REF_RE.sub(lambda m: f"l.{m.group(1)}", body))
            continue
        equality = _split_on(body, "=")
        if equality and where == "both":
            left, right = equality
            if _norm(_retarget(left, "r.")) == _norm(right):
                shape["keys"].append(left)
                continue
        unequal = _split_on(body, "<>")
        if unequal and where == "both":
            left, right = unequal
            if _norm(_retarget(left, "r.")) == _norm(right):
                shape["not_equal"].append(left)
                continue
        shape["residual"].append(body)
    return shape


def block_control(rule) -> dict | None:
    """The hot-key control on one blocking rule, or None when it has none.

    Returns ``{"max_block_size", "on_oversize", "refine_with", "drop_above"}``
    with the defaults filled in: ``on_oversize`` is ``"refine"`` when
    ``refine_with`` names columns and ``"drop"`` when it does not, and
    ``drop_above`` is None when the rule does not set one.

    Bad values are read as leniently as the rest of this module reads user
    data — validation is what refuses them, not this.
    """
    if isinstance(rule, str) or not isinstance(rule, dict):
        return None
    size = rule.get("max_block_size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    refine = rule.get("refine_with")
    columns = [c for c in refine if isinstance(c, str) and c] \
        if isinstance(refine, list) else []
    mode = rule.get("on_oversize")
    if mode not in ON_OVERSIZE:
        mode = "refine" if columns else "drop"
    if mode == "refine" and not columns:
        mode = "drop"
    above = rule.get("drop_above")
    if isinstance(above, bool) or not isinstance(above, int) or above <= 0 \
            or mode == "drop":
        # In drop mode every oversized block already goes, so there is nothing
        # left for drop_above to decide.
        above = None
    return {
        "max_block_size": size,
        "on_oversize": mode,
        "refine_with": columns if mode == "refine" else [],
        "drop_above": above,
    }


def _column_words(column: str) -> str:
    """A column name as plain words: ``dob_year_clean`` reads "dob year"."""
    name = column[:-6] if column.endswith("_clean") else column
    return name.replace("_", " ").strip() or column


def _join_words(words: list[str]) -> str:
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def control_description(rule) -> str:
    """One plain sentence saying what the control does, for the UI.

    "Blocks of more than 60 records are compared only when the forename initial
    also matches; blocks of more than 200 are skipped."
    """
    control = block_control(rule)
    if control is None:
        return ""
    size = control["max_block_size"]
    if control["on_oversize"] == "drop":
        return f"Blocks of more than {size:,} records are skipped."
    columns = [f"the {_column_words(c)}" for c in control["refine_with"]]
    verb = "also matches" if len(columns) == 1 else "also match"
    sentence = (
        f"Blocks of more than {size:,} records are compared only when "
        f"{_join_words(columns)} {verb}"
    )
    if control["drop_above"]:
        return (f"{sentence}; blocks of more than "
                f"{control['drop_above']:,} are skipped.")
    return f"{sentence}."


# ---------------------------------------------------------------------------
# Counting and generating SQL — DuckDB group arithmetic, never a pair
# ---------------------------------------------------------------------------


def as_rule(rule) -> dict:
    """One blocking rule as a dict, whether it arrived as SQL text or a dict."""
    if isinstance(rule, str):
        return {"id": "", "description": "", "sql": rule}
    return rule if isinstance(rule, dict) else {"id": "", "description": "", "sql": ""}


def _units_source(con, units) -> str:
    """*units* as something a ``FROM`` clause can name.

    Takes a path to a parquet file, the name of a table or view already in
    *con*, a pandas frame, or a DuckDB relation. A frame or a relation is
    registered on *con* under a fixed name, which is replaced on each call.
    """
    if isinstance(units, str):
        lowered = units.lower()
        if lowered.endswith((".parquet", ".pq")) or "*" in units:
            return "read_parquet('" + units.replace("'", "''") + "')"
        return units
    con.register("_linkage_units", units)
    return "_linkage_units"


def _has_column(con, source: str, column: str) -> bool:
    cursor = con.execute(f"select * from {source} limit 0")
    return column in [d[0] for d in cursor.description]


def _key_expr(parts: list[str], prefix: str) -> str:
    """The block key of one side as a single string expression.

    Every part is cast to text and a null becomes the empty string, so the value
    this builds is the same value the literal list holds. A null key never
    blocks anyway — ``l.k = r.k`` is false when either side is null — so the
    substitution cannot invent a pair.
    """
    pieces = [f"coalesce(cast({_retarget(part, prefix)} as varchar), '')"
              for part in parts]
    joiner = f" || chr({ord(_KEY_SEPARATOR)}) || "
    return pieces[0] if len(pieces) == 1 else joiner.join(pieces)


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _where(shape: dict, track: str | None, has_track: bool, extra=()) -> str:
    """The rows of the units table this rule could ever block.

    A null key is left out because ``l.k = r.k`` is false when either side is
    null, so a unit with a null key never blocks with anything. The same is true
    of a null ``refine_with`` value, which is why *extra* is filtered the same
    way: a unit in an oversized block with nothing to refine on makes no pair.
    """
    clauses = []
    if track and has_track:
        clauses.append(f"track = {_literal(track)}")
    # `l.x <> r.x` is null, not true, when either side is null, so a unit with a
    # null there makes no pair on this rule either and is left out the same way.
    for part in list(shape["keys"]) + list(extra) + list(shape["not_equal"]):
        clauses.append(f"({_retarget(part, '')}) is not null")
    for part in shape["filters"]:
        clauses.append(f"({_retarget(part, '')})")
    return " and ".join(clauses) if clauses else "true"


def _block_sql(shape: dict, source: str, track: str | None, has_track: bool,
               extra=(), tag: str = "") -> tuple[str, list[str]]:
    """``(CTEs, key names)`` for a chain ending in ``blk<tag>(key..., n, c)``.

    ``n`` is the units in the block. ``c`` is the pairs an ``l.x <> r.x``
    condition removes from it, which is the pairs of its sub-blocks by ``x``.
    Both come from ``count(*)``. No pair is ever built.

    *extra* adds columns to the key, which is how a refined block is counted.
    """
    parts = list(shape["keys"]) + list(extra)
    group = [f"({_retarget(part, '')}) as k{i}" for i, part in enumerate(parts)]
    names = [f"k{i}" for i in range(len(group))]
    if not names:                       # a rule with no key is one big block
        group, names = ["true as k0"], ["k0"]
    where = _where(shape, track, has_track, extra)
    # Prefixed names, because the units may themselves be a table or view the
    # caller named, and a CTE that shadows it would silently read the wrong one.
    units, sub, blk = f"_hk_u{tag}", f"_hk_sub{tag}", f"_hk_blk{tag}"
    if shape["not_equal"]:
        unequal = [f"({_retarget(part, '')}) as x{i}"
                   for i, part in enumerate(shape["not_equal"])]
        keys_and_x = ", ".join(names + [f"x{i}" for i in range(len(unequal))])
        ctes = (
            f"{units} as (select {', '.join(group + unequal)} "
            f"from {source} where {where}), "
            f"{sub} as (select {keys_and_x}, count(*) as m "
            f"from {units} group by all), "
            f"{blk} as (select {', '.join(names)}, sum(m) as n, "
            f"sum(m * (m - 1) // 2) as c from {sub} group by all)"
        )
    else:
        ctes = (
            f"{units} as (select {', '.join(group)} from {source} where {where}), "
            f"{blk} as (select {', '.join(names)}, count(*) as n, 0 as c "
            f"from {units} group by all)"
        )
    return ctes, names


def _one(con, sql: str) -> tuple:
    row = con.execute(sql).fetchone()
    return tuple(0 if value is None else int(value) for value in row)


def price_rule(con, units, rule, track: str | None = None) -> dict:
    """What one blocking rule costs after its hot-key control, by counting only.

    *con* is a DuckDB connection. *units* is a parquet path, the name of a table
    or view on *con*, a pandas frame or a DuckDB relation — one row per unit.
    *rule* is a blocking rule: SQL text, or the dict ``blocking_rules()``
    returns, which may carry ``max_block_size``, ``on_oversize``,
    ``refine_with`` and ``drop_above``. *track* filters the units when they
    carry a ``track`` column.

    Returns::

        {"pairs":            pairs the rule makes after the control,
         "blocks":           blocks it makes after the control,
         "oversized_blocks": blocks over max_block_size, before the control,
         "pairs_before":     pairs it would make with no control,
         "units_dropped":    units in blocks the control drops entirely,
         "units_unrefinable": units in an oversized block whose refine_with
                             value is null, which make no pair either,
         "blocks_before":    blocks it makes with no control,
         "exact":            False when the SQL holds a condition counting
                             cannot apply, which makes every count an upper
                             bound,
         "residual":         those conditions, as text}

    Nothing here builds a pair and nothing here touches Splink. Every number is
    ``sum(n * (n - 1) / 2)`` over a ``GROUP BY``, so a rule that would make a
    hundred million pairs is priced in one pass over the units.
    """
    rule = as_rule(rule)
    shape = block_shape(rule.get("sql") or "")
    control = block_control(rule)
    if control is not None and not shape["keys"]:
        # Nothing to be oversized about: the rule has no block key. Validation
        # refuses this shape; pricing simply reports the rule as it stands.
        control = None
    source = _units_source(con, units)
    has_track = bool(track) and _has_column(con, source, "track")
    base, keys = _block_sql(shape, source, track, has_track)

    limit = control["max_block_size"] if control else None
    filter_kept = "" if limit is None else f" filter (where n <= {limit})"
    filter_big = "false" if limit is None else f"n > {limit}"
    totals = _one(con, f"""
        with {base}
        select count(*),
               coalesce(sum(n * (n - 1) // 2 - c), 0),
               count(*){filter_kept},
               coalesce(sum(n * (n - 1) // 2 - c){filter_kept}, 0),
               count(*) filter (where {filter_big}),
               coalesce(sum(n) filter (where {filter_big}), 0)
        from _hk_blk
    """)
    blocks_before, pairs_before, kept_blocks, kept_pairs, oversized, big_units = totals

    result = {
        "pairs": pairs_before,
        "blocks": blocks_before,
        "oversized_blocks": oversized,
        "pairs_before": pairs_before,
        "blocks_before": blocks_before,
        "units_dropped": 0,
        "units_unrefinable": 0,
        "exact": not shape["residual"],
        "residual": list(shape["residual"]),
    }
    if control is None:
        return result

    if control["on_oversize"] == "drop":
        result.update({"pairs": kept_pairs, "blocks": kept_blocks,
                       "units_dropped": big_units})
        return result

    # Refine: every oversized block is re-blocked on the extra columns, and what
    # is still too big after that goes. `blk2` groups by key plus refine_with,
    # so a refined block is just another GROUP BY — still no pair.
    refined, _ = _block_sql(shape, source, track, has_track,
                            extra=[f"l.{c}" for c in control["refine_with"]],
                            tag="2")
    above = control["drop_above"]
    keep = "true" if above is None else f"n <= {above}"
    drop = "false" if above is None else f"n > {above}"
    joined = " and ".join(f"_hk_blk2.{key} is not distinct from _hk_big.{key}"
                          for key in keys)
    refined_blocks, refined_pairs, dropped_units, refined_units = _one(con, f"""
        with {base}, {refined},
             _hk_big as (select {', '.join(keys)} from _hk_blk where {filter_big})
        select count(*) filter (where {keep}),
               coalesce(sum(n * (n - 1) // 2 - c) filter (where {keep}), 0),
               coalesce(sum(n) filter (where {drop}), 0),
               coalesce(sum(n), 0)
        from _hk_blk2 join _hk_big on {joined}
    """)
    result.update({
        "pairs": kept_pairs + refined_pairs,
        "blocks": kept_blocks + refined_blocks,
        "units_dropped": dropped_units,
        # A unit in an oversized block with a null refine_with value has nothing
        # to be refined on, so it makes no pair on this rule either.
        "units_unrefinable": big_units - refined_units,
    })
    return result


def oversized_keys(con, units, rule, track: str | None = None) -> dict:
    """The key values the control acts on: ``{"oversized": [...], "dropped": [...]}``.

    ``oversized`` holds every block key with more units than ``max_block_size``.
    ``dropped`` holds the refined keys — key plus ``refine_with`` — that are
    still over ``drop_above`` after refining, and is empty without one. Both are
    strings: a composite key is its parts joined by the unit separator, which is
    exactly what the generated SQL compares against.
    """
    rule = as_rule(rule)
    control = block_control(rule)
    if control is None:
        return {"oversized": [], "dropped": []}
    shape = block_shape(rule.get("sql") or "")
    if not shape["keys"]:
        return {"oversized": [], "dropped": []}
    source = _units_source(con, units)
    has_track = bool(track) and _has_column(con, source, "track")
    where = _where(shape, track, has_track)
    key = _key_expr(shape["keys"], "")
    size = control["max_block_size"]

    big = [row[0] for row in con.execute(
        f"select {key} as k from {source} where {where} "
        f"group by 1 having count(*) > {size}"
    ).fetchall()]
    dropped = []
    if control["on_oversize"] == "refine" and control["drop_above"]:
        extra = [f"l.{c}" for c in control["refine_with"]]
        combined = _key_expr(list(shape["keys"]) + extra, "")
        # A block is oversized on its whole size, but a null refine value never
        # agrees with itself, so those units make no pair and must not swell the
        # refined group that drop_above then measures.
        refinable = _where(shape, track, has_track, extra)
        dropped = [row[0] for row in con.execute(
            f"with _hk_big as (select {key} as k from {source} where {where} "
            f"group by 1 having count(*) > {size}), "
            f"_hk_u as (select {key} as k, {combined} as ck from {source} "
            f"where {refinable}) "
            f"select ck from _hk_u where k in (select k from _hk_big) "
            f"group by 1 having count(*) > {control['drop_above']}"
        ).fetchall()]
    return {"oversized": big, "dropped": dropped}


def controlled_sql(con, units, rule, track: str | None = None) -> str:
    """The rule's SQL with its hot-key control built in, ready for Splink.

    The control is generated SQL, not a filter over pairs: a pair inside an
    oversized block is never made unless the ``refine_with`` columns agree, and
    is never made at all when the refined block is still over ``drop_above``.

    The shape is the rule's own SQL with two extra conditions on the ``l.``
    side, which is enough because the rule already holds the key equal::

        <the rule> AND (<key> NOT IN (<the oversized keys>)
                        OR (l.forename_initial = r.forename_initial))
                   AND <key || refine> NOT IN (<the keys still too big>)

    The oversized keys are inlined as literals because a Splink blocking rule is
    a predicate over ``l.`` and ``r.`` columns and has nowhere else to read a set
    from. They are measured from *units*, so pass the same frame Splink will
    score. A rule with no control, or one whose blocks are all inside the limit,
    comes back untouched.
    """
    rule = as_rule(rule)
    sql = rule.get("sql") or ""
    control = block_control(rule)
    if control is None:
        return sql
    shape = block_shape(sql)
    if not shape["keys"]:
        return sql
    sets = oversized_keys(con, units, rule, track)
    if not sets["oversized"]:
        return sql
    if len(sets["oversized"]) > MAX_INLINE_KEYS:
        raise ValueError(
            f"Blocking rule '{rule.get('id') or sql}' has "
            f"{len(sets['oversized']):,} blocks over max_block_size "
            f"({control['max_block_size']:,}), more than the {MAX_INLINE_KEYS:,} "
            "this control can carry. The rule is too coarse to fix one key at a "
            "time — tighten the rule itself, or raise max_block_size."
        )

    key = _key_expr(shape["keys"], "l.")
    values = ", ".join(_literal(v) for v in sorted(sets["oversized"]))
    clauses = [f"({sql})"]
    if control["on_oversize"] == "drop":
        clauses.append(f"{key} NOT IN ({values})")
    else:
        agree = " AND ".join(f"l.{c} = r.{c}" for c in control["refine_with"])
        clauses.append(f"({key} NOT IN ({values}) OR ({agree}))")
        if sets["dropped"]:
            combined = _key_expr(
                list(shape["keys"]) + [f"l.{c}" for c in control["refine_with"]],
                "l.",
            )
            gone = ", ".join(_literal(v) for v in sorted(sets["dropped"]))
            clauses.append(f"{combined} NOT IN ({gone})")
    return " AND ".join(clauses)


def numeric_thresholds(spec: dict) -> list:
    """The ascending thresholds of a ``custom.NumericDifferenceAtThresholds``.

    The numbers come back as they were written, so a whole number stays whole
    and the generated SQL reads ``<= 1`` rather than ``<= 1.0``.
    """
    raw = (spec.get("splink_args") or {}).get("thresholds")
    if not isinstance(raw, list):
        return []
    return [value for value in raw
            if not isinstance(value, bool) and isinstance(value, (int, float))]


def numeric_columns(track_config: dict) -> list[str]:
    """The columns a numeric-difference comparison reads.

    Cleaning writes text — ``dob_year_clean`` is a string of digits out of
    ``nullify_outside_range`` — and ``ABS(l - r)`` needs numbers, so the scoring
    frame casts exactly these columns and nothing else. ``units.parquet`` keeps
    the text, so the review screen and the vetoes still see what was filed.
    """
    out = []
    for spec in comparisons(track_config):
        if spec.get("splink_function") in CUSTOM_FUNCTIONS:
            column = spec.get("column")
            if isinstance(column, str) and column and column not in out:
                out.append(column)
    return out


def build_numeric_difference(spec: dict):
    """``custom.NumericDifferenceAtThresholds`` as a Splink ``CustomComparison``.

    Null level first, then one level per threshold in ascending order, then
    "all other". A threshold of 0 is an exact match and is labelled as one, so
    the per-pair explanation reads in plain words.
    """
    import splink.comparison_level_library as cll
    import splink.comparison_library as cl

    column = spec["column"]
    thresholds = numeric_thresholds(spec)
    if not thresholds:
        raise ValueError(f"'{NUMERIC_DIFFERENCE}' needs a thresholds list")

    levels = [cll.NullLevel(column)]
    for threshold in thresholds:
        if threshold == 0:
            label = f"Equal {column}"
        elif float(threshold).is_integer():
            label = f"{column} within {int(threshold)}"
        else:
            label = f"{column} within {threshold}"
        levels.append(
            cll.AbsoluteDifferenceLevel(column, threshold).configure(
                label_for_charts=label
            )
        )
    levels.append(cll.ElseLevel().configure(label_for_charts="All other"))
    return cl.CustomComparison(
        comparison_levels=levels,
        output_column_name=column,
        comparison_description=spec.get("description")
        or f"Numeric difference on {column}",
    )


def build_comparison(spec: dict):
    """A Splink comparison from one ``comparisons`` entry."""
    import splink.comparison_library as cl

    name = spec.get("splink_function")
    if name not in COMPARISON_FUNCTIONS:
        raise ValueError(f"'{name}' is not an allowed splink_function")
    if name in CUSTOM_FUNCTIONS:
        # Term frequency is refused by validation, so nothing to configure.
        return build_numeric_difference(spec)
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
    _check_high_by_track(settings, review, errors)


def _check_high_by_track(settings: dict, review: float | None,
                         errors: list[dict]) -> None:
    """The per-track accept lines: a known track, a score, above the review line.

    The review line is one line for every track, so a track's own accept line
    has to clear it just as the shared accept line does.
    """
    raw = settings.get(HIGH_BY_TRACK_KEY)
    if raw is None:
        return
    path = f"linkage_settings.{HIGH_BY_TRACK_KEY}"
    if not isinstance(raw, dict):
        _error(errors, path,
               f"{HIGH_BY_TRACK_KEY} must be an object keyed by track")
        return
    for track, value in raw.items():
        at = f"{path}.{track}"
        if track not in TRACK_KEYS:
            _error(errors, at, vocabulary.choice_error("track", track, TRACK_KEYS))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _error(errors, at, "A track's accept line must be a number between 0 and 1")
            continue
        if not 0 <= float(value) <= 1:
            _error(errors, at, "A track's accept line must be between 0 and 1")
            continue
        if review is not None and float(value) < review:
            _error(errors, at,
                   "A track's accept line must be at or above the review threshold")


def _check_numeric_difference(spec: dict, path: str, errors: list[dict]) -> None:
    """``thresholds``: a non-empty ascending list of numbers, zero or more.

    Term frequency is refused. The levels here are gaps between two numbers, not
    values; down-weighting a common birth year is exactly the mistake that let a
    37-year gap score 1.0 in the first PSC sample run.
    """
    args = spec.get("splink_args")
    if args is not None and not isinstance(args, dict):
        return  # already reported as "splink_args must be an object"
    raw = (args or {}).get("thresholds")
    if not isinstance(raw, list) or not raw:
        _error(errors, f"{path}.splink_args.thresholds",
               f"{NUMERIC_DIFFERENCE} needs a non-empty list of thresholds")
    else:
        previous = None
        bad = False
        for index, value in enumerate(raw):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _error(errors, f"{path}.splink_args.thresholds[{index}]",
                       "A threshold must be a number")
                bad = True
                continue
            if float(value) < 0:
                _error(errors, f"{path}.splink_args.thresholds[{index}]",
                       "A threshold must be zero or more")
                bad = True
                continue
            if previous is not None and float(value) <= previous:
                _error(errors, f"{path}.splink_args.thresholds[{index}]",
                       "Thresholds must be in ascending order")
                bad = True
            previous = float(value)
        if bad:
            return
    if spec.get("term_frequency"):
        _error(errors, f"{path}.term_frequency",
               f"{NUMERIC_DIFFERENCE} cannot use a term-frequency adjustment: "
               "its levels are gaps between numbers, not values")


def _check_block_control(rule: dict, sql: str, path: str, track: str,
                         known: set[str], errors: list[dict]) -> None:
    """The four hot-key keys on one blocking rule (docs/LINKAGE.md).

    A rule carrying none of them is left alone, which is the whole point: the
    control is opt-in and a settings document written before it existed is
    still valid and still blocks the same way.
    """
    present = [key for key in BLOCK_CONTROL_KEYS if key in rule]
    if not present:
        return

    size = rule.get("max_block_size")
    has_size = "max_block_size" in rule
    if has_size:
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            _error(errors, f"{path}.max_block_size",
                   "max_block_size must be a whole number above zero")
            size = None
    else:
        size = None
        _error(errors, f"{path}.{present[0]}",
               f"'{present[0]}' needs a max_block_size to say which blocks are "
               "oversized")

    mode = rule.get("on_oversize")
    if "on_oversize" in rule and mode not in ON_OVERSIZE:
        _error(errors, f"{path}.on_oversize",
               vocabulary.choice_error("on_oversize", mode, ON_OVERSIZE))
        mode = None

    refine = rule.get("refine_with")
    columns = None
    if "refine_with" in rule:
        if not isinstance(refine, list) or not refine \
                or not all(isinstance(c, str) and c for c in refine):
            _error(errors, f"{path}.refine_with",
                   "refine_with must be a non-empty list of column names")
        else:
            columns = refine
            for column in refine:
                if column not in known:
                    _error(errors, f"{path}.refine_with",
                           f"'{column}' is not a column of the {track} track")
    if mode == "refine" and columns is None and "refine_with" not in rule:
        _error(errors, f"{path}.refine_with",
               "on_oversize 'refine' needs refine_with to say which columns an "
               "oversized block is refined by")

    if "drop_above" in rule:
        above = rule.get("drop_above")
        if isinstance(above, bool) or not isinstance(above, int) or above <= 0:
            _error(errors, f"{path}.drop_above",
                   "drop_above must be a whole number above zero")
        elif size is not None and above < size:
            _error(errors, f"{path}.drop_above",
                   f"drop_above ({above:,}) must be at or above max_block_size "
                   f"({size:,}): a block is refined first and only then dropped")

    if size is not None and not block_shape(sql)["keys"]:
        _error(errors, f"{path}.max_block_size",
               "A rule with no 'l.column = r.column' has no blocks to size, so "
               "it cannot take a hot-key control")


def gate_key(columns) -> str:
    """The name one limit's count comes back under, as ``n_distinct_<key>``.

    A limit on one column keeps that column's own name, which is what the
    cluster screen and ``docs/ENTITIES.md`` have always read. A limit that
    counts several columns as one value joins them with ``+``, so
    ``dob_year_clean+dob_month_clean`` is one full birth date.
    """
    return "+".join(columns)


def gate_limit(entry) -> dict | None:
    """One limit, normalised to ``{"columns": [...], "count": int, "key": str}``.

    ``{"column": "surname_clean", "count": 3}`` is one column. ``{"columns":
    ["dob_year_clean", "dob_month_clean"], "count": 1}`` counts the two
    together, so a cluster showing 1985-03 and 1985-07 shows two values and not
    one. A malformed entry is dropped rather than guessed at.
    """
    if not isinstance(entry, dict):
        return None
    raw = entry.get("columns")
    if isinstance(raw, str):
        raw = [raw]
    if raw is None:
        column = entry.get("column")
        raw = [column] if isinstance(column, str) and column else None
    if not isinstance(raw, list) or not raw:
        return None
    columns = [c for c in raw if isinstance(c, str) and c]
    if len(columns) != len(raw):
        return None
    count = entry.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    return {"columns": columns, "count": count, "key": gate_key(columns)}


def _gate_group(entry) -> list[dict] | None:
    """One clause of a track's gate: a single limit, or an ``all`` of them.

    A group holds the cluster only when EVERY limit in it is over its count. A
    half-understood conjunction would hold more clusters than its author meant,
    which is the dangerous direction, so a group with one bad member is dropped
    whole.
    """
    if isinstance(entry, dict) and "all" in entry:
        raw = entry.get("all")
        if not isinstance(raw, list) or not raw:
            return None
        group = [gate_limit(member) for member in raw]
        return group if all(group) else None
    limit = gate_limit(entry)
    return [limit] if limit else None


def max_distinct_values(settings: dict) -> dict:
    """``{track: [group, ...]}`` — the stage 4 gate, as groups of limits.

    A cluster is held when ANY group holds, and a group holds when EVERY limit
    in it is over its count. A plain entry becomes a group of one, so the
    any-of behaviour this setting has always had is unchanged.

    A track names more than one clause because a runaway cluster does not always
    run away on the same column: the PSC person track needs the surname AND the
    forename, and gating only the surname leaves every "<different forename>
    Singh" cluster standing.

    The conjunction is for the opposite problem — a column that is wrong on its
    own. On the full PSC run a limit on postcode districts alone is wrong 14
    times in 20, because one person's companies have many registered offices
    (``psc_person_evidence_2026-09-22.md``, section 2). Paired with "more than
    one full birth date" it stops being a guess: 3,596 entities and 45,151
    records, and it catches the two known bad "Mohammed Imran" entities.

    A malformed entry is dropped rather than guessed at; `validate_linkage_settings`
    is what tells the user about it.
    """
    raw = (settings or {}).get("max_distinct_values")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[list[dict]]] = {}
    for track, spec in raw.items():
        if track not in TRACK_KEYS:
            continue
        groups = []
        for entry in (spec if isinstance(spec, list) else [spec]):
            group = _gate_group(entry)
            if group:
                groups.append(group)
        if groups:
            out[track] = groups
    return out


def _check_max_distinct_values(settings: dict, errors: list[dict]) -> None:
    raw = (settings or {}).get("max_distinct_values")
    if raw is None:
        return
    path = "linkage_settings.max_distinct_values"
    if not isinstance(raw, dict):
        _error(errors, path, "max_distinct_values must be an object keyed by track")
        return
    for track, spec in raw.items():
        if track not in TRACK_KEYS:
            _error(errors, f"{path}.{track}",
                   vocabulary.choice_error("track", track, TRACK_KEYS))
            continue
        entries = spec if isinstance(spec, list) else [spec]
        if not entries or not all(isinstance(e, dict) for e in entries):
            _error(errors, f"{path}.{track}",
                   "Each track needs a column and a count, or a list of them")
            continue
        for index, entry in enumerate(entries):
            at = f"{path}.{track}" + (f"[{index}]" if isinstance(spec, list) else "")
            if "all" in entry:
                _check_gate_conjunction(entry, at, errors)
                continue
            _check_gate_limit(entry, at, errors)


def _check_gate_limit(entry: dict, at: str, errors: list[dict]) -> None:
    """One limit: the column or columns it counts, and the count itself."""
    raw = entry.get("columns")
    if raw is None:
        column = entry.get("column")
        if not isinstance(column, str) or not column:
            _error(errors, f"{at}.column",
                   "max_distinct_values needs a column to count the values of")
    elif isinstance(raw, str):
        if not raw:
            _error(errors, f"{at}.columns",
                   "max_distinct_values needs a column to count the values of")
    elif not isinstance(raw, list) or not raw \
            or not all(isinstance(c, str) and c for c in raw):
        _error(errors, f"{at}.columns",
               "columns must be a list of one or more column names, counted "
               "together as one value")
    count = entry.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        _error(errors, f"{at}.count",
               "count must be a whole number of 1 or more")


def _check_gate_conjunction(entry: dict, at: str, errors: list[dict]) -> None:
    """An ``all`` clause: a list of limits, every one of which must be over."""
    raw = entry.get("all")
    if not isinstance(raw, list) or not raw:
        _error(errors, f"{at}.all",
               "'all' must be a list of limits, and the cluster is held only "
               "when every one of them is over its count")
        return
    for index, member in enumerate(raw):
        if not isinstance(member, dict):
            _error(errors, f"{at}.all[{index}]",
                   "Each limit under 'all' needs a column and a count")
            continue
        if "all" in member:
            _error(errors, f"{at}.all[{index}]",
                   "'all' cannot hold another 'all'")
            continue
        _check_gate_limit(member, f"{at}.all[{index}]", errors)


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
            if isinstance(rule, dict):
                _check_block_control(rule, sql, path, track, known, errors)

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
            if function not in COMPARISON_FUNCTIONS:
                _error(errors, f"{path}.splink_function",
                       vocabulary.choice_error("splink_function", function,
                                               COMPARISON_FUNCTIONS))
            elif function == NUMERIC_DIFFERENCE:
                _check_numeric_difference(spec, path, errors)
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
            if isinstance(rule, str):
                sql = rule
            elif isinstance(rule, dict):
                sql = rule.get("sql")
            else:
                sql = None
            if not isinstance(sql, str) or not sql.strip():
                _error(errors, path,
                       "An EM blocking rule must be SQL text, or an object with "
                       "a 'sql' key")
                continue
            for column in sorted(sql_columns(sql)):
                if column not in known:
                    _error(errors, path,
                           f"'{column}' is not a column of the {track} track")
            if isinstance(rule, dict):
                _check_block_control(rule, sql, path, track, known, errors)

    budget = config.get("max_pairs", DEFAULT_MAX_PAIRS)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        _error(errors, f"{base}.max_pairs", "max_pairs must be a whole number above zero")


# Every `l.col = r.col` a rule holds equal. The same parser the blocking rules
# already use, so a rule this reads is a rule Splink will read the same way.
_EQUALITY_RE = re.compile(r"\bl\.(\w+)\s*=\s*r\.(\w+)\b")


def columns_held_equal(sql: str) -> set[str]:
    """The columns one blocking rule forces to agree on both sides."""
    return {
        left for left, right in _EQUALITY_RE.findall(sql or "") if left == right
    }


def untrainable_comparisons(config: dict) -> list[int]:
    """Indexes of comparisons no EM training rule ever lets vary.

    Splink cannot estimate a comparison whose column every training rule holds
    equal: there is no disagreement inside the training block to learn from, so
    the level comes back with no m probability and contributes **nothing** to
    the score. It is silent — the run finishes, the comparison is listed, and
    it counts for zero. The PSC person track shipped this way and accepted
    pairs 37 birth-years apart.
    """
    rules = config.get("em_blocking_rules")
    if not isinstance(rules, list) or not rules:
        return []
    held = [columns_held_equal(str(as_rule(rule).get("sql") or ""))
            for rule in rules
            if isinstance(rule, (str, dict)) and str(as_rule(rule).get("sql") or "").strip()]
    if not held:
        return []
    comparisons = config.get("comparisons")
    if not isinstance(comparisons, list):
        return []
    stuck = []
    for index, comparison in enumerate(comparisons):
        if not isinstance(comparison, dict):
            continue
        column = comparison.get("column")
        if column and all(column in fixed for fixed in held):
            stuck.append(index)
    return stuck


def linkage_warnings(settings, ruleset: dict = None) -> list[dict]:
    """Things that are legal but will not do what the user meant.

    Warnings never block a save. They are the difference between a run that is
    wrong and a run that is wrong in a way nobody notices.
    """
    warnings: list[dict] = []
    if not isinstance(settings, dict):
        return warnings
    for track in TRACK_KEYS:
        config = track_settings(settings, track)
        if not config:
            continue
        for index in untrainable_comparisons(config):
            comparison = config["comparisons"][index]
            warnings.append({
                "path": f"linkage_settings.tracks.{track}.comparisons[{index}]",
                "message": (
                    "No training rule lets this comparison vary, so the model cannot "
                    "learn its weight and it will count for nothing. Add an "
                    f"em_blocking_rule that does not hold '{comparison.get('column')}' "
                    "equal."
                ),
            })
    return warnings


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
    _check_max_distinct_values(settings, errors)

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

    floor = settings.get("cluster_floor", DEFAULT_CLUSTER_FLOOR)
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) \
            or not 0 <= float(floor) <= 1:
        _error(errors, "linkage_settings.cluster_floor",
               "cluster_floor must be a number between 0 and 1")
    for key, default, least in (
        ("max_cluster_units", DEFAULT_MAX_CLUSTER_UNITS, 2),
        ("max_existing_ids", DEFAULT_MAX_EXISTING_IDS, 1),
    ):
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < least:
            _error(errors, f"linkage_settings.{key}",
                   f"{key} must be a whole number of {least} or more")

    prior = settings.get("probability_two_random_records_match")
    if prior is not None:
        if isinstance(prior, bool) or not isinstance(prior, (int, float)) \
                or not 0 < float(prior) < 1:
            _error(errors, "linkage_settings.probability_two_random_records_match",
                   "probability_two_random_records_match must be null or between 0 and 1")

    # The GBT's own numbers sit under a `model` key beside the Splink ones
    # (docs/MODEL.md). Every one has a default, so a config version with no such
    # key is valid; a key with a bad value in it is not.
    from app.model import settings as model_settings

    errors.extend(model_settings.validate(settings))

    return errors

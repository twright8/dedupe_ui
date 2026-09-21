# backend/app/rules/keys.py
"""Apply a ruleset's match keys — the deterministic half of the matcher.

RULESET.md's "Match keys" section is the contract. One pass per track: the
keys run in tier order, each key groups the records that are equal on every
key column, the guards decide whether a group is merged or held for a human,
and the merged groups of every key are united into one set of entities.

Nothing here walks a pair. Every join is a group-by followed by "union each
member of this bucket with the bucket's first member", which is linear in the
number of records. A PSC run has 16 million of them, and the donations sheet
has trade-union groups with over a thousand members; a pairwise loop over
either is not an option.
"""

import numpy as np
import pandas as pd

from app import vocabulary
from app.rules import engine, functions

MERGED = "merged"
HELD = "held"
STATUSES = vocabulary.EXACT_GROUP_STATUSES

# The columns stage 2 writes. A record appears at most once as merged and may
# also appear in any number of held groups.
GROUP_COLUMNS = ("record_id", "group_id", "track", "status", "key_ids", "guard")

MERGED_PREFIX = "X-"
HELD_PREFIX = "H-"


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------


class UnionFind:
    """Array union-find over integer codes, with path halving and union by rank.

    It is only ever asked to join the members of a bucket to that bucket's first
    member, so the work is linear in the number of records rather than in the
    number of pairs.
    """

    def __init__(self, size: int):
        self._parent = np.arange(max(size, 1), dtype=np.int64)
        self._rank = np.zeros(max(size, 1), dtype=np.int8)

    def find(self, x: int) -> int:
        parent = self._parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return int(x)

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def join_buckets(self, members: np.ndarray, buckets: np.ndarray) -> None:
        """Union every member of each bucket with that bucket's first member."""
        if len(members) == 0:
            return
        order = np.argsort(buckets, kind="stable")
        members = np.asarray(members)[order]
        buckets = np.asarray(buckets)[order]
        starts = np.flatnonzero(np.r_[True, buckets[1:] != buckets[:-1]])
        lengths = np.diff(np.r_[starts, len(buckets)])
        anchors = np.repeat(members[starts], lengths)
        for anchor, member in zip(anchors.tolist(), members.tolist()):
            if anchor != member:
                self.union(anchor, member)

    def roots(self, size: int) -> np.ndarray:
        return np.fromiter((self.find(i) for i in range(size)), dtype=np.int64, count=size)


# ---------------------------------------------------------------------------
# One comparable form for a key value
# ---------------------------------------------------------------------------


def _as_key_text(value):
    """Trimmed, upper-cased text; a blank is missing, not a value.

    "Matching is case-insensitive everywhere" (RULESET.md). A whole number that
    parquet hands back as 2001.0 must not become a different key from 2001.
    """
    if isinstance(value, float):
        if not np.isfinite(value):
            return None
        if float(value).is_integer():
            value = int(value)
    text = str(value).strip().upper()
    return text or None


def normalise(series: pd.Series) -> pd.Series:
    """*series* as comparable key text, with nulls and blanks both None."""
    return functions.map_distinct(series, lambda distinct: distinct.map(_as_key_text))


# ---------------------------------------------------------------------------
# Reading one key
# ---------------------------------------------------------------------------


def _guards(key: dict) -> dict:
    guards = key.get("guards")
    return guards if isinstance(guards, dict) else {}


def _key_columns(key: dict) -> list[str]:
    columns = key.get("columns")
    return [c for c in columns if c] if isinstance(columns, list) else []


def _corroborators(key: dict) -> list[str]:
    values = _guards(key).get("require_any_equal")
    return [c for c in values if c] if isinstance(values, list) else []


def _max_distinct(key: dict) -> tuple[str | None, int | None]:
    spec = _guards(key).get("max_distinct")
    if not isinstance(spec, dict):
        return None, None
    column = spec.get("column")
    count = spec.get("count")
    if not column or not isinstance(count, int) or isinstance(count, bool):
        return None, None
    return column, count


def _max_group_size(key: dict) -> int | None:
    value = _guards(key).get("max_group_size")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def referenced_columns(ruleset: dict) -> dict[str, set[str]]:
    """Every column the match keys read, per track — what stage 2 must normalise."""
    wanted: dict[str, set[str]] = {track: set() for track in engine.TRACK_KEYS}
    for key in engine.match_keys(ruleset):
        track = key.get("track")
        if track not in wanted:
            continue
        wanted[track].update(_key_columns(key))
        wanted[track].update(_corroborators(key))
        column, _ = _max_distinct(key)
        if column:
            wanted[track].add(column)
    return wanted


def _ordered_keys(ruleset: dict) -> list[dict]:
    """The keys of one ruleset in run order: by tier, ties in document order."""
    keys = list(engine.match_keys(ruleset))
    order = []
    for position, key in enumerate(keys):
        tier = key.get("tier", 1)
        tier = tier if isinstance(tier, int) and not isinstance(tier, bool) else 1
        order.append((tier, position, key))
    order.sort(key=lambda item: (item[0], item[1]))
    return [{"tier": tier, "position": position, "key": key} for tier, position, key in order]


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def apply_match_keys(records: pd.DataFrame, ruleset: dict) -> tuple[pd.DataFrame, dict]:
    """Group *records* under the ruleset's match keys.

    Returns ``(groups, stats)``. *groups* has one row per record per group it
    belongs to, with the columns in ``GROUP_COLUMNS``. *stats* carries one entry
    per key plus an ``overall`` block.
    """
    if "record_id" not in records.columns:
        raise engine.RulesetError("Match keys need a record_id column")
    track_values = (
        records["track"] if "track" in records.columns
        else pd.Series([None] * len(records), index=records.index)
    )

    total = len(records)
    record_ids = records["record_id"].astype(str).to_numpy()
    tracks = track_values.astype("object").to_numpy()

    # Normalise once, over the distinct values, every column any key reads.
    wanted: set[str] = set()
    for columns in referenced_columns(ruleset).values():
        wanted.update(columns)
    normalised = {
        column: normalise(records[column]).to_numpy()
        for column in sorted(wanted)
        if column in records.columns
    }

    ordered = _ordered_keys(ruleset)
    _attach_token_lists(ordered, ruleset)
    _attach_conditions(ordered, ruleset, records)
    stats_by_key: dict[int, dict] = {}
    merged_positions: list[np.ndarray] = []
    merged_key_positions: list[np.ndarray] = []
    held_rows: list[dict] = []
    union = UnionFind(total)

    for track in engine.TRACK_KEYS:
        in_track = tracks == track
        eligible_before = np.zeros(total, dtype=bool)
        tier_eligible = np.zeros(total, dtype=bool)
        current_tier = None

        for entry in ordered:
            key = entry["key"]
            if key.get("track") != track:
                continue
            if current_tier is not None and entry["tier"] != current_tier:
                eligible_before |= tier_eligible
                tier_eligible = np.zeros(total, dtype=bool)
            current_tier = entry["tier"]

            result = _run_key(
                key, entry, in_track, eligible_before, normalised, record_ids, union
            )
            tier_eligible |= result["eligible"]
            stats_by_key[entry["position"]] = result["stats"]
            if len(result["merged_positions"]):
                merged_positions.append(result["merged_positions"])
                merged_key_positions.append(
                    np.full(len(result["merged_positions"]), entry["position"], dtype=np.int64)
                )
            held_rows.extend(result["held"])

    groups = _build_frame(
        total, record_ids, tracks, ordered, union,
        merged_positions, merged_key_positions, held_rows,
    )
    stats = _stats(ordered, stats_by_key, groups, total)
    return groups, stats


def _run_key(key, entry, in_track, eligible_before, normalised, record_ids, union) -> dict:
    """One key: who is eligible, which groups pass the guards, what they merge."""
    total = len(record_ids)
    columns = _key_columns(key)
    allow_null = bool(key.get("allow_null", False))

    # A conditional key applies to one kind of record only (D13c). The condition
    # is settled before anything else, so a record it excludes is not eligible —
    # and therefore does not count as "covered by an earlier key" either.
    condition = entry.get("condition")
    excluded_by_condition = int((in_track & ~condition).sum()) if condition is not None else 0
    eligible = in_track & condition if condition is not None else in_track.copy()
    blocked_values: set[str] = set()

    missing = [c for c in columns if c not in normalised]
    if missing or not columns:
        # Validation rejects this; a draft preview must still answer.
        return _empty_key_result(
            key, entry, np.zeros(total, dtype=bool), 0, excluded_by_condition
        )

    blocklist = _blocklist_tokens(key, entry)
    for column in columns:
        values = normalised[column]
        present = pd.notna(values)
        if not allow_null:
            eligible &= present
        if blocklist:
            hit = present & pd.Series(values).isin(blocklist).to_numpy()
            blocked_values.update(values[hit & eligible].tolist())
            eligible &= ~hit

    if key.get("applies_when", "always") == "no_earlier_key":
        eligible &= ~eligible_before

    positions = np.flatnonzero(eligible)
    if len(positions) == 0:
        return _empty_key_result(
            key, entry, eligible, len(blocked_values), excluded_by_condition
        )

    frame = pd.DataFrame({c: normalised[c][positions] for c in columns})
    group_codes = frame.groupby(list(columns), sort=False, dropna=False).ngroup().to_numpy()
    sizes = np.bincount(group_codes)
    big_enough = sizes[group_codes] >= 2

    # ---- guards, in the order RULESET.md states ----
    failed = np.zeros(len(sizes), dtype=bool)
    reason = np.array([None] * len(sizes), dtype=object)

    limit = _max_group_size(key)
    if limit is not None:
        over = (sizes > limit) & (sizes >= 2)
        failed |= over
        for code in np.flatnonzero(over):
            reason[code] = f"max_group_size:{sizes[code]}>{limit}"

    md_column, md_count = _max_distinct(key)
    if md_column and md_column in normalised:
        values = pd.Series(normalised[md_column][positions])
        distinct = values.groupby(group_codes, sort=False).nunique()
        counted = np.zeros(len(sizes), dtype=np.int64)
        counted[distinct.index.to_numpy()] = distinct.to_numpy()
        over = (counted > md_count) & (sizes >= 2) & ~failed
        failed |= over
        for code in np.flatnonzero(over):
            reason[code] = f"max_distinct:{md_column}={counted[code]}>{md_count}"

    held_codes = np.flatnonzero(failed & (sizes >= 2))
    passing = big_enough & ~failed[group_codes]

    # ---- held groups ----
    held = []
    if len(held_codes) and key.get("on_guard_fail", "review") == "review":
        held_mask = failed[group_codes] & big_enough
        held_positions = positions[held_mask]
        held_group_codes = group_codes[held_mask]
        for code, members in _by_code(held_group_codes, held_positions):
            held.append({
                "key_id": key.get("id"),
                "guard": reason[code],
                "positions": members,
            })

    # ---- passing groups, split by require_any_equal ----
    merged_positions = np.empty(0, dtype=np.int64)
    merged_groups = 0
    if passing.any():
        pass_positions = positions[passing]
        pass_codes = group_codes[passing]
        corroborators = [c for c in _corroborators(key) if c in normalised]
        if corroborators:
            part_codes = _connected_parts(
                pass_codes, [normalised[c][pass_positions] for c in corroborators]
            )
        else:
            part_codes = pass_codes

        keep = _in_parts_of_two_or_more(part_codes)
        merged_positions = pass_positions[keep]
        part_codes = part_codes[keep]
        merged_groups = len(np.unique(part_codes)) if len(part_codes) else 0
        union.join_buckets(merged_positions, part_codes)

    return {
        "eligible": eligible,
        "merged_positions": merged_positions,
        "held": held,
        "stats": {
            "id": key.get("id"),
            "name": key.get("name") or key.get("id"),
            "track": key.get("track"),
            "tier": entry["tier"],
            "eligible_records": int(len(positions)),
            "groups": int(merged_groups),
            "records": int(len(merged_positions)),
            "held_groups": len(held),
            "held_records": int(sum(len(h["positions"]) for h in held)),
            "blocked_values": len(blocked_values),
            "excluded_by_condition": excluded_by_condition,
        },
    }


def _empty_key_result(key, entry, eligible, blocked_values, excluded_by_condition=0) -> dict:
    return {
        "eligible": eligible,
        "merged_positions": np.empty(0, dtype=np.int64),
        "held": [],
        "stats": {
            "id": key.get("id"),
            "name": key.get("name") or key.get("id"),
            "track": key.get("track"),
            "tier": entry["tier"],
            "eligible_records": int(eligible.sum()),
            "groups": 0,
            "records": 0,
            "held_groups": 0,
            "held_records": 0,
            "blocked_values": blocked_values,
            "excluded_by_condition": excluded_by_condition,
        },
    }


def _blocklist_tokens(key: dict, entry: dict) -> set[str]:
    names = _guards(key).get("blocklists")
    if not isinstance(names, list):
        return set()
    tokens: set[str] = set()
    for name in names:
        entries = entry.get("token_lists", {}).get(name)
        if entries:
            tokens.update(entries)
    return tokens


def _by_code(codes: np.ndarray, members: np.ndarray):
    """``(code, members)`` per distinct code, without a per-row Python loop."""
    if len(codes) == 0:
        return
    order = np.argsort(codes, kind="stable")
    codes = codes[order]
    members = members[order]
    starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
    bounds = np.r_[starts, len(codes)]
    for index, start in enumerate(starts):
        yield int(codes[start]), members[start:bounds[index + 1]]


def _connected_parts(group_codes: np.ndarray, corroborator_values: list[np.ndarray]) -> np.ndarray:
    """Split each group into the parts joined by a shared corroborator value.

    Driven by a group-by per corroborator column, so a 1,000-member group costs
    1,000 unions rather than half a million comparisons.
    """
    n = len(group_codes)
    uf = UnionFind(n)
    local = np.arange(n, dtype=np.int64)
    for values in corroborator_values:
        present = pd.notna(values)
        if not present.any():
            continue
        vcodes = pd.factorize(pd.Series(values[present]))[0].astype(np.int64)
        span = int(vcodes.max()) + 1 if len(vcodes) else 1
        buckets = group_codes[present].astype(np.int64) * span + vcodes
        uf.join_buckets(local[present], buckets)
    return uf.roots(n)


def _in_parts_of_two_or_more(part_codes: np.ndarray) -> np.ndarray:
    if len(part_codes) == 0:
        return np.zeros(0, dtype=bool)
    codes, inverse = np.unique(part_codes, return_inverse=True)
    return np.bincount(inverse)[inverse] >= 2


# ---------------------------------------------------------------------------
# Building the output frame
# ---------------------------------------------------------------------------


def _group_id(prefix: str, record_ids: np.ndarray, members: np.ndarray) -> str:
    """Deterministic and stable for the same membership: the smallest member id.

    Record ids compare as strings, so the id does not depend on whether this
    profile's ids happen to be numeric.
    """
    return prefix + min(str(record_ids[member]) for member in members.tolist())


def _build_frame(total, record_ids, tracks, ordered, union,
                 merged_positions, merged_key_positions, held_rows) -> pd.DataFrame:
    rows: list[dict] = []
    key_ids_by_position = {entry["position"]: entry["key"].get("id") for entry in ordered}

    if merged_positions:
        contributed = np.concatenate(merged_positions)
        key_positions = np.concatenate(merged_key_positions)
        roots_of_contributed = np.fromiter(
            (union.find(int(p)) for p in contributed),
            dtype=np.int64, count=len(contributed),
        )
        # Which keys built each united group, in document order.
        pairs = pd.DataFrame(
            {"root": roots_of_contributed, "key_position": key_positions}
        ).drop_duplicates()
        # A record two keys both merged appears twice above. It is one member of
        # one group, so it gets exactly one row in the output.
        positions, first = np.unique(contributed, return_index=True)
        roots = roots_of_contributed[first]
        contributing = (
            pairs.sort_values(["root", "key_position"])
            .groupby("root")["key_position"]
            .apply(lambda values: "|".join(
                str(key_ids_by_position[int(v)]) for v in values
            ))
        )
        for root, members in _by_code(roots, positions):
            group_id = _group_id(MERGED_PREFIX, record_ids, members)
            key_ids = contributing.loc[root]
            for member in members.tolist():
                rows.append({
                    "record_id": record_ids[member],
                    "group_id": group_id,
                    "track": tracks[member],
                    "status": MERGED,
                    "key_ids": key_ids,
                    "guard": None,
                })

    for held in held_rows:
        members = held["positions"]
        group_id = _group_id(f"{HELD_PREFIX}{held['key_id']}-", record_ids, members)
        for member in members.tolist():
            rows.append({
                "record_id": record_ids[member],
                "group_id": group_id,
                "track": tracks[member],
                "status": HELD,
                "key_ids": str(held["key_id"]),
                "guard": held["guard"],
            })

    frame = pd.DataFrame(rows, columns=list(GROUP_COLUMNS))
    if len(frame):
        frame = frame.sort_values(["status", "group_id", "record_id"], kind="mergesort")
    return frame.reset_index(drop=True)


def _stats(ordered, stats_by_key, groups: pd.DataFrame, total: int) -> dict:
    # Run order: by tier, ties in document order — the order the Config screen
    # lists them and the order a reviewer reasons about them in. A key naming a
    # track that does not exist never ran; validation rejects it, but a draft
    # preview must still answer rather than raise.
    per_key = [
        stats_by_key[entry["position"]] for entry in ordered
        if entry["position"] in stats_by_key
    ]

    merged = groups[groups["status"] == MERGED] if len(groups) else groups
    held = groups[groups["status"] == HELD] if len(groups) else groups
    merged_groups = int(merged["group_id"].nunique()) if len(merged) else 0
    merged_records = int(merged["record_id"].nunique()) if len(merged) else 0

    return {
        "keys": per_key,
        "overall": {
            "records": int(total),
            "merged_groups": merged_groups,
            "merged_records": merged_records,
            "held_groups": int(held["group_id"].nunique()) if len(held) else 0,
            "held_records": int(held["record_id"].nunique()) if len(held) else 0,
            "entities_after": int(total - (merged_records - merged_groups)),
        },
    }


# ---------------------------------------------------------------------------
# Token lists reach a key through its ordered entry
# ---------------------------------------------------------------------------


def _attach_conditions(ordered: list[dict], ruleset: dict, records: pd.DataFrame) -> None:
    """Evaluate each key's ``when`` once, over the whole frame.

    The conditions run on the cleaned records, so a key may test a raw column, a
    cleaning target or a derived one. ``engine.when_mask`` is the same evaluator
    the track and derived-column rules use; there is no second implementation.
    """
    token_lists = ruleset.get("token_lists")
    token_lists = token_lists if isinstance(token_lists, dict) else {}
    for item in ordered:
        when = item["key"].get("when")
        if not isinstance(when, list) or not when:
            item["condition"] = None
            continue
        item["condition"] = engine.when_mask(when, records, token_lists).to_numpy()


def _attach_token_lists(ordered: list[dict], ruleset: dict) -> None:
    lists = ruleset.get("token_lists")
    lists = lists if isinstance(lists, dict) else {}
    resolved = {
        name: {
            text for text in (
                _as_key_text(token) for token in (entry.get("tokens") or [])
            ) if text
        }
        for name, entry in lists.items()
        if isinstance(entry, dict)
    }
    for item in ordered:
        item["token_lists"] = resolved

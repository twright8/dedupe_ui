#!/usr/bin/env python3
# backend/scripts/build_name_frequencies.py
"""Build the UK name-frequency reference table (D12) from the PSC individuals.

`DESIGN.md` D12: name rarity must come from **outside** the donations sheet. The
Electoral Commission hands a repeat donor a fresh `DonorId`, so a name that
looks common inside the sheet is usually one busy donor, not a common name. The
PSC individuals are a large, independent sample of UK personal names — about 7.5
million of them — so they answer "how common is this name" without asking the
sheet about itself.

The table this writes is one file:

| `kind` | `forename` | `surname` | `n` |
|---|---|---|---|
| `forename` | the cleaned forename | null | people with it |
| `surname` | null | the cleaned surname | people with it |
| `full` | the cleaned forename | the cleaned surname | people with both |
| `total_forename` | null | null | people with any cleaned forename |
| `total_surname` | null | null | people with any cleaned surname |
| `total_full` | null | null | people with both |
| `total_records` | null | null | rows in the source |

The totals are in the same file so a reader never has to know a second number to
turn a count into a rate.

**The cleaning is not re-implemented here.** It is the donations profile's own
person name-cleaning steps, read out of the ruleset and run through
``app.rules.engine``: upper-case, fold accents, drop punctuation but keep the
space and the hyphen, collapse spaces, and treat a null spelling as missing. If
someone edits those rules, rebuilding the table follows them. Only the text
steps that write ``name_clean`` are taken: stripping titles and splitting the
name into parts make no sense on a name part that is already split.

Run it:

    DATA_DIR=/tmp/scratch .venv/bin/python scripts/build_name_frequencies.py

It writes ``DATA_DIR/references/uk_name_frequencies.parquet`` and a small
``.json`` beside it saying where the numbers came from. ``--also DIR`` writes a
second copy, which is how the checked-in table under ``backend/data/references``
gets made.

Nearly all the work happens inside DuckDB: two small maps of distinct raw value
to cleaned value go in, and DuckDB does the joins and the counting over the
parquet. Nothing walks 7.5 million rows in Python.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules import engine
from app.services import run_manifest  # noqa: E402

DEFAULT_SOURCE = (
    "/home/tomwright/PycharmProjects/deduping/backend/data/source/pscs_individual.parquet"
)

REFERENCE_KEY = "uk_name_frequencies"
REFERENCES_DIRNAME = "references"
TABLE_FILENAME = f"{REFERENCE_KEY}.parquet"
META_FILENAME = f"{REFERENCE_KEY}.json"

# The source's name parts before its own cleaning touched them, so this script's
# cleaning is the only cleaning applied and it matches the donations sheet's.
FORENAME_COLUMN = "forename_raw"
SURNAME_COLUMN = "surname_raw"

# Cleaning ops that make sense on a name part that is already split out. The
# person track's later steps (strip titles, strip post-nominals, parse the name)
# are about a whole name and are skipped.
PART_SAFE_OPS = frozenset({
    "copy", "upper", "lower", "trim", "collapse_spaces", "accent_fold",
    "strip_punctuation", "regex_replace", "nullify",
})

# The column the person track's text steps write. A step that writes anything
# else is building a different thing (a metaphone key, a token sort) and is not
# part of "clean the name".
NAME_CLEAN_COLUMN = "name_clean"

COLUMNS = ["kind", "forename", "surname", "n"]


# ---------------------------------------------------------------------------
# Cleaning, borrowed from the profile's own rules
# ---------------------------------------------------------------------------


def default_ruleset(profile_key: str = "donations") -> dict:
    path = (
        Path(__file__).resolve().parents[1]
        / "app" / "profiles" / "defaults" / profile_key / "ruleset.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def name_part_steps(ruleset: dict, track: str = "person") -> list[dict]:
    """The cleaning steps that turn a raw name into the cleaned one.

    Everything up to and including the last text step that writes
    ``name_clean``. Order is the ruleset's order, because cleaning is a pipeline
    and folding accents after dropping punctuation is not the same thing.
    """
    steps = []
    for step in engine.cleaning_steps(ruleset, track):
        if step.get("op") not in PART_SAFE_OPS:
            continue
        if step.get("target") != NAME_CLEAN_COLUMN:
            continue
        steps.append(step)
    return steps


def clean_values(values: pd.Series, steps: list[dict], ruleset: dict) -> pd.Series:
    """Run *steps* over a Series of distinct raw values.

    Each step is the engine's own op, called exactly as the cleaning stage calls
    it, so this cannot drift from the pipeline. A value that ends up blank comes
    back as ``None``: a blank is a missing name, not a name spelled "".
    """
    out = values.astype("object")
    for step in steps:
        out = engine.OPS[step["op"]](out, step, ruleset)
    out = out.astype("object")
    blank = out.isna() | (out.astype(str).str.strip() == "")
    return out.where(~blank, None)


def cleaning_map(con, source: str, column: str, steps: list[dict],
                 ruleset: dict) -> pd.DataFrame:
    """``raw -> clean`` for every distinct non-null value of *column*.

    260,000 forenames and 642,000 surnames, so cleaning runs once per distinct
    spelling rather than once per person.
    """
    raw = con.execute(
        f'SELECT DISTINCT "{column}" AS raw FROM read_parquet(?) '
        f'WHERE "{column}" IS NOT NULL',
        [source],
    ).fetch_df()
    raw["clean"] = clean_values(raw["raw"], steps, ruleset)
    return raw.dropna(subset=["clean"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Counting, done by DuckDB
# ---------------------------------------------------------------------------


def build_table(source: str, steps: list[dict], ruleset: dict,
                memory_limit: str = "4GB") -> tuple[pd.DataFrame, dict]:
    """``(the reference table, build stats)`` for the parquet at *source*."""
    import duckdb

    started = time.perf_counter()
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")

    total_records = con.execute(
        "SELECT count(*) FROM read_parquet(?)", [source]
    ).fetchone()[0]

    forenames = cleaning_map(con, source, FORENAME_COLUMN, steps, ruleset)
    surnames = cleaning_map(con, source, SURNAME_COLUMN, steps, ruleset)
    con.register("forename_map", forenames)
    con.register("surname_map", surnames)
    con.execute("CREATE TEMP TABLE src AS SELECT * FROM read_parquet(?)", [source])

    forename_counts = con.execute(
        """SELECT f.clean AS forename, count(*) AS n
           FROM src JOIN forename_map f ON src.forename_raw = f.raw
           GROUP BY 1"""
    ).fetch_df()
    surname_counts = con.execute(
        """SELECT s.clean AS surname, count(*) AS n
           FROM src JOIN surname_map s ON src.surname_raw = s.raw
           GROUP BY 1"""
    ).fetch_df()
    full_counts = con.execute(
        """SELECT f.clean AS forename, s.clean AS surname, count(*) AS n
           FROM src
           JOIN forename_map f ON src.forename_raw = f.raw
           JOIN surname_map s ON src.surname_raw = s.raw
           GROUP BY 1, 2"""
    ).fetch_df()
    con.close()

    parts = [
        forename_counts.assign(kind="forename", surname=None),
        surname_counts.assign(kind="surname", forename=None),
        full_counts.assign(kind="full"),
    ]
    totals = pd.DataFrame([
        {"kind": "total_forename", "forename": None, "surname": None,
         "n": int(forename_counts["n"].sum())},
        {"kind": "total_surname", "forename": None, "surname": None,
         "n": int(surname_counts["n"].sum())},
        {"kind": "total_full", "forename": None, "surname": None,
         "n": int(full_counts["n"].sum())},
        {"kind": "total_records", "forename": None, "surname": None,
         "n": int(total_records)},
    ])
    table = pd.concat([p[COLUMNS] for p in parts] + [totals[COLUMNS]],
                      ignore_index=True)
    table["n"] = table["n"].astype("int64")
    for column in ("kind", "forename", "surname"):
        table[column] = table[column].astype("object")

    stats = {
        "source_rows": int(total_records),
        "distinct_forenames_raw": int(len(forenames)),
        "distinct_surnames_raw": int(len(surnames)),
        "forename_rows": int(len(forename_counts)),
        "surname_rows": int(len(surname_counts)),
        "full_rows": int(len(full_counts)),
        "rows": int(len(table)),
        "build_secs": round(time.perf_counter() - started, 2),
    }
    return table, stats


def write_table(table: pd.DataFrame, meta: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TABLE_FILENAME
    table.to_parquet(path, index=False)
    (directory / META_FILENAME).write_text(
        json.dumps({**meta, "bytes": path.stat().st_size}, indent=2),
        encoding="utf-8",
    )
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="the PSC individuals parquet")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"),
                        help="writes <data-dir>/references/ (default: $DATA_DIR)")
    parser.add_argument("--also", action="append", default=[],
                        help="another directory to write the same table into")
    parser.add_argument("--profile", default="donations",
                        help="whose person cleaning rules to reuse")
    parser.add_argument("--memory-limit", default="4GB")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        parser.error(f"No such source parquet: {source}")

    ruleset = default_ruleset(args.profile)
    steps = name_part_steps(ruleset)
    if not steps:
        parser.error(
            f"The {args.profile} profile's person track has no name cleaning steps"
        )

    table, stats = build_table(str(source), steps, ruleset, args.memory_limit)
    meta = {
        "key": REFERENCE_KEY,
        "source": str(source),
        # The source is overwritten in place from time to time, so its path is
        # not an identity. The hash is (docs/TERMINOLOGY_AUDIT.md, gap 10).
        "source_sha256": run_manifest.sha256_of(source),
        "profile": args.profile,
        "cleaning_steps": [
            {"id": s.get("id"), "op": s.get("op"), "description": s.get("description")}
            for s in steps
        ],
        "built_at": datetime.now(timezone.utc).isoformat(),
        **stats,
    }

    targets = [Path(args.data_dir) / REFERENCES_DIRNAME]
    targets += [Path(d) for d in args.also]
    for directory in targets:
        path = write_table(table, meta, directory)
        print(f"wrote {path} ({len(table):,} rows)")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

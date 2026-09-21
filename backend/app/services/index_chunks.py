# backend/app/services/index_chunks.py
"""Write a big group-by to parquet without a hash table nobody can spill.

`docs/PSC_HANDOVER.md` section 104 records the general lesson: `memory_limit`
does not bound a DuckDB query whose plan pins memory the buffer manager cannot
evict. A hash aggregate whose output carries a `list()` column is the other
shape that does it — the lists are built in the hash table and stay there until
the group is closed, so a group-by that returns 8.5 million rows with five
names on each fails at any limit a server would set. It fails at 9.3 GB; the
server has 6.

The fix is to do the same group-by in pieces. The groups are split by a hash of
their key, each piece is written to its own file, and the pieces are then
concatenated by a plain scan, which streams. The answer is the same rows: a
group falls in exactly one piece, because every row of it hashes the same way.

The caller materialises whatever the group-by reads FIRST, so the pieces scan a
narrow table rather than re-running a join once per piece.
"""

import os
from pathlib import Path

#: Pieces per index. Eight is enough for the full PSC snapshot — 8.5 million
#: entities in pieces of about a million — and costs eight scans of a table
#: that is already narrow.
CHUNKS_ENV = "INDEX_CHUNKS"
DEFAULT_CHUNKS = 8


def chunks_wanted(chunks: int | None = None) -> int:
    if chunks is not None:
        return max(1, int(chunks))
    try:
        return max(1, int(os.environ.get(CHUNKS_ENV, DEFAULT_CHUNKS)))
    except ValueError:
        return DEFAULT_CHUNKS


def _literal(path) -> str:
    text = os.fspath(path)
    return "'" + text.replace("'", "''") + "'"


def write(con, out: Path, aggregate, key: str, chunks: int | None = None,
          work: Path | None = None, prefix: str = "index",
          params: list | None = None) -> Path:
    """Run *aggregate* in pieces and leave the whole answer at *out*.

    *aggregate* takes a WHERE fragment — ``""`` or ``" WHERE ..."`` — and gives
    back the group-by SQL with it in place, before the GROUP BY.
    """
    out = Path(out)
    work = Path(work) if work is not None else out.parent
    work.mkdir(parents=True, exist_ok=True)
    count = chunks_wanted(chunks)
    parts: list[Path] = []
    try:
        for index in range(count):
            part = work / f"{prefix}.part{index}.parquet"
            where = "" if count == 1 else \
                f' WHERE abs(hash("{key}")) % {count} = {index}'
            con.execute(f"COPY ({aggregate(where)}) TO {_literal(part)} "
                        f"(FORMAT PARQUET)",  # the pieces are thrown away
                        list(params or []))
            parts.append(part)
        listed = ", ".join(_literal(part) for part in parts)
        temporary = out.with_suffix(".building.parquet")
        con.execute(
            f"COPY (SELECT * FROM read_parquet([{listed}])) "
            f"TO {_literal(temporary)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        temporary.replace(out)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    return out

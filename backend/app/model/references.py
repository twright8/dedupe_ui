# backend/app/model/references.py
"""Reference tables: outside data a profile declares, loaded from `DATA_DIR`.

`MODEL.md` D12: name rarity must come from outside the dataset being deduped.
The donations sheet gives a repeat donor a new `DonorId`, so a name that repeats
inside it is usually one busy donor, not a common name. A reference table is
that outside answer, built once by a script in `backend/scripts/` and read here.

A profile declares what it wants through its ``references`` attribute. The
loader returns what is there and ``None`` for what is not — a missing table is
never an error. The features that needed it go null, the training report says
which table was missing, and the run carries on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

REFERENCES_DIRNAME = "references"


@dataclass(frozen=True)
class Reference:
    """One outside table, and what depends on it."""

    key: str
    label: str
    description: str = ""
    # The features that go null when the table is missing.
    features: tuple[str, ...] = field(default_factory=tuple)

    @property
    def filename(self) -> str:
        return f"{self.key}.parquet"

    def as_dict(self) -> dict:
        """The wire shape (docs/MODEL_API.md).

        ``name`` rather than ``key``, and ``affects`` rather than ``features``,
        because this list is read by a person looking at a warning: the question
        it answers is "which table is missing, and what does that cost me".
        """
        return {
            "name": self.key,
            "label": self.label,
            "description": self.description,
            "affects": list(self.features),
        }


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "data"))


def references_dir() -> Path:
    return data_dir() / REFERENCES_DIRNAME


def path_for(key: str) -> Path:
    return references_dir() / f"{key}.parquet"


def meta_for(key: str) -> dict | None:
    """The build metadata the script wrote beside the table, or None."""
    path = references_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_one(key: str) -> pd.DataFrame | None:
    """One reference table, or None when it is not built.

    An unreadable file is the same as a missing one. A half-built table would
    poison every rarity feature, and the report already has a place to say the
    table is absent.
    """
    path = path_for(key)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001 — a corrupt file must not fail a run
        return None


def declared(profile=None) -> list[Reference]:
    if profile is None:
        from app.profiles import get_profile

        profile = get_profile()
    return list(getattr(profile, "references", []) or [])


def load(profile=None) -> dict[str, pd.DataFrame | None]:
    """Every table the profile declares, by key, with None for the missing ones.

    Callers index this dict rather than testing for a key, so a feature builder
    reads ``references.get("uk_name_frequencies")`` and gets None either way.
    """
    return {ref.key: load_one(ref.key) for ref in declared(profile)}


def status(profile=None, loaded: dict | None = None) -> list[dict]:
    """What the training report prints: which tables are there, and how big.

    This is the one place that says whether a reference table is present. A
    second "missing tables" list beside it would be the same fact twice, and two
    places to keep in step.
    """
    out = []
    for ref in declared(profile):
        frame = loaded.get(ref.key) if loaded is not None else load_one(ref.key)
        meta = meta_for(ref.key) or {}
        out.append({
            **ref.as_dict(),
            "present": frame is not None,
            "rows": int(len(frame)) if frame is not None else None,
            "path": f"{REFERENCES_DIRNAME}/{ref.filename}",
            "built_at": meta.get("built_at"),
        })
    return out


def missing(profile=None, loaded: dict | None = None) -> list[str]:
    """The names of the tables that are not built. For warnings, not the wire."""
    return [r["name"] for r in status(profile, loaded) if not r["present"]]


# ---------------------------------------------------------------------------
# The UK name frequency table
# ---------------------------------------------------------------------------

UK_NAME_FREQUENCIES = "uk_name_frequencies"

#: What `scripts/build_name_frequencies.py` writes. `kind` is one of `forename`,
#: `surname`, `full`, or a `total_` row whose name columns are null.
NAME_FREQUENCY_COLUMNS = ("kind", "forename", "surname", "n")


def name_frequency_parts(table: pd.DataFrame | None) -> tuple:
    """``(forename counts, surname counts, full-name counts)`` from the table.

    Three small frames, each a name and an ``n``, ready to join onto a pair
    frame. Returns three empty frames when the table is missing, so the caller
    joins the same way either way.
    """
    empty_one = pd.DataFrame({"name": pd.Series(dtype="object"),
                              "n": pd.Series(dtype="int64")})
    empty_full = pd.DataFrame({"forename": pd.Series(dtype="object"),
                               "surname": pd.Series(dtype="object"),
                               "n": pd.Series(dtype="int64")})
    if table is None or not len(table):
        return empty_one.copy(), empty_one.copy(), empty_full

    forenames = table.loc[table["kind"] == "forename", ["forename", "n"]] \
        .rename(columns={"forename": "name"}).reset_index(drop=True)
    surnames = table.loc[table["kind"] == "surname", ["surname", "n"]] \
        .rename(columns={"surname": "name"}).reset_index(drop=True)
    full = table.loc[table["kind"] == "full", ["forename", "surname", "n"]] \
        .reset_index(drop=True)
    return forenames, surnames, full

# backend/app/profiles/base.py
"""Profile base class — everything one dataset needs, in one place.

The app runs as one instance per profile. Shared code (the runs API, the records
API, the review screen) must never name a dataset: it reads the shared columns
below and takes everything else from ``display_columns``.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# Columns every profile's records frame MUST carry. Shared code reads only these
# by name; all other columns are profile-defined and described by display_columns.
#   record_id          str, unique within a run
#   track              'person' | 'organisation'
#   name               str, the display name
#   review_state       'labelled' | 'unreviewed'
#   existing_entity_id str or null — the entity ID a previous review already gave
SHARED_COLUMNS = ("record_id", "track", "name", "review_state", "existing_entity_id")

# Display column types the frontend knows how to render.
DISPLAY_TYPES = ("text", "number", "money", "year", "list")

TRACK_KEYS = ("person", "organisation")


@dataclass(frozen=True)
class InputSpec:
    """The one input file a run takes."""

    label: str
    extensions: list[str]  # lower-case, with the dot, e.g. ['.xlsx', '.csv']
    help: str

    def as_dict(self) -> dict:
        return {"label": self.label, "extensions": list(self.extensions), "help": self.help}


@dataclass(frozen=True)
class Track:
    key: str  # 'person' | 'organisation'
    label: str

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label}


@dataclass(frozen=True)
class DisplayColumn:
    key: str
    label: str
    type: str = "text"  # one of DISPLAY_TYPES

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type}


# Every profile has exactly these two tracks (design decision D5). A profile may
# relabel them but may not add or remove one.
DEFAULT_TRACKS = [
    Track(key="person", label="People"),
    Track(key="organisation", label="Organisations"),
]


@dataclass
class Profile:
    """One dataset's configuration. Subclasses fill the fields and load_records."""

    key: str = ""
    title: str = ""
    subtitle: str = ""
    input: InputSpec = field(
        default_factory=lambda: InputSpec(label="Input file", extensions=[".csv"], help="")
    )
    tracks: list[Track] = field(default_factory=lambda: list(DEFAULT_TRACKS))
    display_columns: list[DisplayColumn] = field(default_factory=list)
    priority_columns: list[str] = field(default_factory=list)

    def load_records(self, input_path: Path) -> tuple[pd.DataFrame, dict]:
        """Read the input file and return (records frame, load stats).

        The frame carries one row per record, with at least SHARED_COLUMNS.
        The stats dict is stored as the run's counts.
        """
        raise NotImplementedError(
            f"Profile '{self.key}' cannot load records yet."
        )

    def as_dict(self) -> dict:
        """The profile as the /api/profile endpoint returns it (minus base_path)."""
        return {
            "key": self.key,
            "title": self.title,
            "subtitle": self.subtitle,
            "input": self.input.as_dict(),
            "tracks": [t.as_dict() for t in self.tracks],
            "display_columns": [c.as_dict() for c in self.display_columns],
            "priority_columns": list(self.priority_columns),
        }


def validate_records(records: pd.DataFrame) -> None:
    """Raise if a profile's records frame is missing a shared column or has
    duplicate record_ids. Cheap, and it catches a broken loader at load time
    rather than three screens later."""
    missing = [c for c in SHARED_COLUMNS if c not in records.columns]
    if missing:
        raise ValueError(
            f"Records frame is missing required column(s): {', '.join(missing)}"
        )
    if records["record_id"].duplicated().any():
        duplicated = records.loc[records["record_id"].duplicated(), "record_id"].head(3).tolist()
        raise ValueError(f"record_id must be unique — repeated: {duplicated}")
    bad_tracks = sorted(set(records["track"].dropna().unique()) - set(TRACK_KEYS))
    if bad_tracks:
        raise ValueError(f"track must be one of {TRACK_KEYS} — found: {bad_tracks}")

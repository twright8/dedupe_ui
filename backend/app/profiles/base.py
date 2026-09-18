# backend/app/profiles/base.py
"""Profile base class — everything one dataset needs, in one place.

The app runs as one instance per profile. Shared code (the runs API, the records
API, the review screen) must never name a dataset: it reads the shared columns
below and takes everything else from ``display_columns``.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# Columns every profile's RAW records frame MUST carry (stage 0). Shared code
# reads only these by name; all other columns are profile-defined and described
# by display_columns.
#   record_id          str, unique within a run
#   name               str, the display name
#   review_state       'labelled' | 'unreviewed'
#   existing_entity_id str or null — the entity ID a previous review already gave
# `track` is NOT here: the ruleset decides it, and stage 1 adds it.
SHARED_COLUMNS = ("record_id", "name", "review_state", "existing_entity_id")

# What the cleaned frame carries on top of the raw one. Stage 1 writes it.
TRACK_COLUMN = "track"

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


@dataclass(frozen=True)
class EvidenceFocus:
    """What a reviewer actually checks for one kind of record (D13c).

    ``when`` is a list of conditions in the ruleset's own shape, ANDed, read
    against one record or unit. The focuses are tried in order and the first
    whose conditions hold wins, exactly as a track rule does — so the last one
    should have an empty ``when`` and act as the fallback.

    Only operators a browser can evaluate in a line of JavaScript belong here
    (``equals``, ``in``, ``is_null``, ``not_null``, ``starts_with``): the review
    screen may have to pick the focus client-side from ``/api/profile``.
    """

    id: str
    label: str
    when: list[dict] = field(default_factory=list)
    record_columns: list[str] = field(default_factory=list)
    event_columns: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "when": [dict(c) for c in self.when],
            "record_columns": list(self.record_columns),
            "event_columns": list(self.event_columns),
        }


# The condition operators an evidence focus may use. Deliberately the subset a
# browser can evaluate without the backend.
EVIDENCE_FOCUS_OPS = ("equals", "in", "is_null", "not_null", "starts_with")


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
    # Every column the loader produces, in frame order. A ruleset may read these
    # and may not overwrite them, so the Config screen needs the list before any
    # run exists.
    raw_columns: list[str] = field(default_factory=list)
    # The evidence rows a profile may supply (D13b): the donations behind a
    # donor, the companies behind a PSC. Empty when there are none.
    event_columns: list[DisplayColumn] = field(default_factory=list)
    # Columns settled once per entity rather than per record (D8a, stage 2 of
    # the two). Donations: the standardised donor status.
    consensus_columns: list[str] = field(default_factory=list)
    # What a reviewer checks, by kind of record (D13c). Tried in order; the last
    # entry should be the catch-all. Empty when every record is judged the same.
    evidence_focus: list[EvidenceFocus] = field(default_factory=list)
    # Outside tables the model's features need (`app.model.references.Reference`),
    # such as the UK name frequencies the rarity features read (D12). Empty when
    # the profile needs none. A declared table that is not built is not an error:
    # its features go null and the training report says so.
    references: list = field(default_factory=list)
    # Only the default mint uses this; a profile with an ID convention of its
    # own ignores it.
    _entity_counter: int = 0

    def load_records(self, input_path: Path) -> tuple[pd.DataFrame, dict]:
        """Read the input file and return (records frame, load stats).

        The frame carries one row per record, with at least SHARED_COLUMNS.
        The stats dict is stored as the run's counts.
        """
        raise NotImplementedError(
            f"Profile '{self.key}' cannot load records yet."
        )

    def load_events(self, input_path: Path) -> pd.DataFrame | None:
        """The evidence rows behind the records, or None when there are none.

        One row per underlying event, keyed on ``record_id`` and carrying the
        columns ``event_columns`` describes. Stage 0 writes it to
        ``events.parquet``, and a pair or a group opens it beside the records.

        A profile whose input is slow to read should read it once and serve both
        this and ``load_records`` from the same parse.
        """
        return None

    def mint_entity_ids(self, members: pd.DataFrame) -> pd.Series:
        """Fresh entity IDs for the proposals that belong to no registry entity.

        *members* is every such proposal's record rows in one frame, carrying
        ``entity_key``. The answer is a Series indexed by ``entity_key``. It is
        a whole frame rather than one entity at a time because PSC will have
        millions of them.

        The default is a counter, which suits a profile with no ID convention.
        Donations overrides it: its IDs have to line up with the numbers the
        earlier manual work already handed out (D15).
        """
        keys = members["entity_key"].drop_duplicates().sort_values()
        start = self._entity_counter
        self._entity_counter += len(keys)
        return pd.Series(
            [f"E{start + n + 1:08d}" for n in range(len(keys))], index=keys.to_numpy()
        )

    def choose_survivors(self, claims: pd.DataFrame) -> pd.Series:
        """Which ID wins where a proposed entity spans several registry ones.

        *claims* is long form: ``entity_key`` and ``registry_entity``, several
        rows per key. The answer is a Series indexed by ``entity_key``. The
        others retire as aliases when the run is published, so the choice has to
        be stable — the same set must always give one answer.
        """
        return claims.groupby("entity_key", sort=False)["registry_entity"].min()

    def export(self, run_dir: Path, scope: str, fmt: str, context: dict) -> Path:
        """Write this profile's export file and return its path.

        *context* carries what the file needs beyond the run's own parquet
        files: the aliases, the run row and its counts. A profile with no
        export of its own raises, and the runs API falls back to a plain
        records CSV.
        """
        raise NotImplementedError(f"Profile '{self.key}' has no export of its own.")

    def aggregate_unit_columns(
        self, members: pd.DataFrame, events: pd.DataFrame | None = None
    ) -> pd.DataFrame | None:
        """Per-unit columns a modal vote over the members would get wrong.

        ``units.py`` takes the most frequent member value for every column,
        which is right for a name and wrong for a median. A profile with such
        columns recomputes them here, indexed by ``unit_id``, and the result
        replaces what the representative row worked out.

        ``members`` is the record rows with a ``unit_id`` column. ``events`` is
        the evidence rows with the same, when the run has any. Both are whole
        frames, so the work has to stay vectorised.
        """
        return None

    # -- the model's features (docs/MODEL.md) --------------------------------

    def pair_feature_metadata(self, track: str) -> list:
        """What this profile's feature builder produces for *track*.

        A list of ``app.model.features.Feature``: the column name, a label in
        plain words, the group it belongs to, and its monotone constraint. It is
        a description, not a build, so ``GET /api/model/{track}/features`` can
        answer before anything is trained.

        A profile with no features of its own returns an empty list, and the
        model trains on the generic Splink features alone.
        """
        return []

    def build_pair_features(
        self,
        pairs: pd.DataFrame,
        units: pd.DataFrame,
        events: pd.DataFrame | None = None,
        references: dict | None = None,
        track: str = "person",
    ) -> pd.DataFrame | None:
        """One feature row per pair, in *pairs*' own order and index.

        *pairs* is one track's rows of `pairs.parquet`, *units* is the whole of
        `units.parquet`, *events* is the evidence rows carrying a ``unit_id``
        column (or None), and *references* is what
        ``app.model.references.load`` returned, keyed by reference key with None
        for a table that is not built.

        The builder receives whole frames and must stay vectorised: it never
        loops over pairs. A column the metadata names and the frame omits is
        read as null, so a feature can be dropped from a run without breaking
        the model.
        """
        return None

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
            "event_columns": [c.as_dict() for c in self.event_columns],
            "consensus_columns": list(self.consensus_columns),
            "evidence_focus": [f.as_dict() for f in self.evidence_focus],
        }

    def evidence_focus_for(self, row) -> str | None:
        """The id of the first focus whose conditions hold for one record or unit.

        *row* is anything with ``.get`` or ``[]`` — a dict, a pandas Series, a
        reader's item. Returns None when the profile declares no focuses, or
        when none matches (which a catch-all entry should prevent).
        """
        return evidence_focus_id(self.evidence_focus, row)


# ---------------------------------------------------------------------------
# Picking an evidence focus for one row
# ---------------------------------------------------------------------------


def _cell(row, column: str):
    """One column of a dict, a Series or a reader's item, as text or None."""
    try:
        value = row.get(column) if hasattr(row, "get") else row[column]
    except (KeyError, IndexError, TypeError):
        return None
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _condition_holds(condition: dict, row) -> bool:
    """One condition against one row, in the same words as the ruleset engine.

    Deliberately the small subset in ``EVIDENCE_FOCUS_OPS``: whatever runs here
    must also run in the browser, because the review screen may pick the focus
    without asking the server.
    """
    op = condition.get("op")
    value = _cell(row, condition.get("column"))

    if op == "is_null":
        return value is None
    if op == "not_null":
        return value is not None
    if value is None:
        return False
    if op == "equals":
        return value.upper() == str(condition.get("value", "")).strip().upper()
    if op == "in":
        wanted = {str(v).strip().upper() for v in (condition.get("values") or [])}
        return value.upper() in wanted
    if op == "starts_with":
        prefixes = tuple(
            str(v).strip().upper() for v in (condition.get("values") or []) if str(v).strip()
        )
        return bool(prefixes) and value.upper().startswith(prefixes)
    raise ValueError(
        f"An evidence focus may not use '{op}' — "
        f"allowed: {', '.join(EVIDENCE_FOCUS_OPS)}"
    )


def evidence_focus_id(focuses, row) -> str | None:
    """The id of the first focus in *focuses* whose conditions all hold for *row*."""
    for focus in focuses or []:
        if all(_condition_holds(c, row) for c in focus.when):
            return focus.id
    return None


def validate_records(records: pd.DataFrame) -> None:
    """Raise if a profile's records frame is missing a shared column or has
    duplicate record_ids. Cheap, and it catches a broken loader at load time
    rather than three screens later.

    ``track`` is checked only when it is present: stage 0 has none, stage 1 adds it.
    """
    missing = [c for c in SHARED_COLUMNS if c not in records.columns]
    if missing:
        raise ValueError(
            f"Records frame is missing required column(s): {', '.join(missing)}"
        )
    if records["record_id"].duplicated().any():
        duplicated = records.loc[records["record_id"].duplicated(), "record_id"].head(3).tolist()
        raise ValueError(f"record_id must be unique — repeated: {duplicated}")
    if TRACK_COLUMN in records.columns:
        bad_tracks = sorted(set(records[TRACK_COLUMN].dropna().unique()) - set(TRACK_KEYS))
        if bad_tracks:
            raise ValueError(f"track must be one of {TRACK_KEYS} — found: {bad_tracks}")

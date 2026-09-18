# backend/app/rules/__init__.py
"""The ruleset engine.

One ruleset is one JSON document (``docs/RULESET.md``). This package holds the
only implementation of it: the pipeline and every preview call the functions
here, so a preview can never disagree with a run.

  functions.py   the fixed function library, with the metadata the UI lists
  conditions.py  the track-rule condition operators
  engine.py      validation, track assignment, cleaning, and the preview trace
"""

from app.rules.engine import (  # noqa: F401  (re-exported for convenience)
    UnmappedLookupValuesError,
    apply_cleaning,
    assign_tracks,
    available_columns,
    trace_cleaning,
    validate_ruleset,
)

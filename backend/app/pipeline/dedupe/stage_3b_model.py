# backend/app/pipeline/dedupe/stage_3b_model.py
"""Stage 3b — score the pairs with the track's active model (`docs/MODEL.md`).

Splink finds the candidates and gives every pair a prior score. When a track has
an active model, this stage adds `gbt_score` beside `match_probability`, and
when that model is **graded** the buckets are worked out from it instead, with
`decided_by` reading `model`.

Three rules keep the two scores honest about each other.

* `match_probability` is never touched. Both numbers stay on the pair, so a
  reviewer, the "most useful to label" sort and `score_eval` can all compare
  them.
* The overlays are unchanged. An imported agreement still accepts a pair, a
  human label still wins, and the model changes neither — it only moves where
  the score-alone line falls.
* A model that is not graded writes its score and decides nothing. That is the
  cold-start rule: it re-orders the review queue and no more.

The collapse guard is carried over from `roe_ui` (`CONCERNS_LOG` R). A model
trained on few or nearly separable labels goes bimodal, which empties the review
band and bins every uncertain pair without a human ever seeing it. If applying
one does that, the run goes back to the Splink score and records a warning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from app.model import features as feature_lib
from app.model import references as reference_lib
from app.model import store

logger = logging.getLogger(__name__)

MODEL_SCORE_COLUMN = "gbt_score"
MODEL_STATE_FILENAME = "model_state.json"

# What `decided_by` says when the model's own score put the pair in its bucket.
DECIDED_BY_MODEL = "model"

# Below this many distinct scores the model has collapsed to a near two-valued
# dial. `roe_ui` saw exactly two (0.001 and 0.999).
MIN_DISTINCT_SCORES = 4
# A review band that falls below this share of what it was has collapsed.
REVIEW_BAND_FLOOR = 0.05


@dataclass(frozen=True)
class TrackModel:
    """The active model of one track, and where it puts its lines."""

    track: str
    version: int
    graded: bool
    accept: float | None
    reject: float | None

    @property
    def decides(self) -> bool:
        """Whether this model may set buckets. A cold-start model may not."""
        return bool(self.graded and self.accept is not None)

    @property
    def lines(self) -> tuple[float, float]:
        """``(review, high)`` for ``bucket_of``.

        No reject line means nothing is rejected, so the review floor sits below
        every score. That is `MODEL.md`'s rule: with too few test labels to reach
        the target precision, everything the model scores goes to review.
        """
        return (float(self.reject) if self.reject is not None else -np.inf,
                float(self.accept))

    def as_dict(self) -> dict:
        return {"track": self.track, "version": self.version, "graded": self.graded,
                "accept": self.accept, "reject": self.reject, "decides": self.decides}


def active_models(tracks=("person", "organisation")) -> dict[str, TrackModel]:
    """The active model of every track that has one."""
    out = {}
    for track in tracks:
        summary = store.active_summary(track)
        if summary is None:
            continue
        out[track] = TrackModel(
            track=track, version=int(summary["version"]),
            graded=bool(summary.get("graded")),
            accept=summary.get("accept"), reject=summary.get("reject"),
        )
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_track(pairs: pd.DataFrame, units: pd.DataFrame, track: str, version: int,
                events: pd.DataFrame | None = None, profile=None) -> np.ndarray:
    """Calibrated model scores for one track's pairs, in the frame's own order.

    The feature frame is rebuilt from the stored feature list rather than from
    the profile's current metadata, so a version trained before a feature was
    added still scores with exactly the columns it learnt on. A column that is
    no longer produced comes back as null, which the booster already knows how
    to handle.
    """
    stored = store.load_features(track, version)
    booster = store.load_booster(track, version)
    if booster is None or not stored:
        raise FileNotFoundError(f"No usable model v{version} for track {track}")
    if not len(pairs):
        return np.zeros(0, dtype="float64")

    references = reference_lib.load(profile)
    built, _meta = feature_lib.build(pairs, units, track, events=events,
                                     references=references, profile=profile)
    columns = [f["name"] for f in stored]
    X = built.reindex(columns=columns).astype("float64")
    raw = booster.predict(X.to_numpy())
    return store.apply_calibration(raw, store.load_calibration(track, version))


def score_pairs(pairs: pd.DataFrame, units: pd.DataFrame,
                models: dict[str, TrackModel], events: pd.DataFrame | None = None,
                profile=None) -> tuple[pd.DataFrame, dict[str, TrackModel]]:
    """Add `gbt_score` for every track that has an active model.

    Returns the pairs and the models that actually scored something — a track
    whose model cannot be loaded is dropped with a log line rather than failing
    the run, because a broken model file must not cost a night's scoring.
    """
    pairs = pairs.copy()
    if MODEL_SCORE_COLUMN not in pairs.columns:
        pairs[MODEL_SCORE_COLUMN] = np.nan
    pairs[MODEL_SCORE_COLUMN] = pd.to_numeric(pairs[MODEL_SCORE_COLUMN],
                                              errors="coerce").astype("float64")
    used: dict[str, TrackModel] = {}
    for track, model in models.items():
        mask = (pairs["track"] == track).to_numpy() if "track" in pairs.columns \
            else np.ones(len(pairs), dtype=bool)
        if not mask.any():
            continue
        subset = pairs[mask].reset_index(drop=True)
        try:
            scores = score_track(subset, units, track, model.version,
                                 events=events, profile=profile)
        except Exception:  # noqa: BLE001 — a broken version must not fail the run
            logger.exception("Could not score the %s track with model v%s",
                             track, model.version)
            continue
        pairs.loc[mask, MODEL_SCORE_COLUMN] = scores
        used[track] = model
    return pairs, used


def deciding_lines(models: dict[str, TrackModel]) -> dict[str, tuple[float, float]]:
    """``{track: (review, high)}`` for the models allowed to set buckets."""
    return {track: model.lines for track, model in models.items() if model.decides}


# ---------------------------------------------------------------------------
# The collapse guard
# ---------------------------------------------------------------------------


def distinct_scores(pairs: pd.DataFrame) -> int:
    if MODEL_SCORE_COLUMN not in pairs.columns or not len(pairs):
        return 0
    values = pd.to_numeric(pairs[MODEL_SCORE_COLUMN], errors="coerce").dropna()
    return int(values.round(6).nunique()) if len(values) else 0


def collapse_reason(review_before: int, review_after: int, n_distinct: int) -> str | None:
    """Why applying this model must be refused, or None.

    Two failures, both `roe_ui`'s:

    * the score is near two-valued, which catches a first apply even when the
      Splink review band was already empty;
    * a review band that had pairs in it has fallen to nothing or nearly nothing.
    """
    if 0 < n_distinct < MIN_DISTINCT_SCORES:
        return (f"the model collapsed to a near two-valued score "
                f"({n_distinct} distinct value(s) across every scored pair)")
    if review_before > 0:
        floor = max(1, round(REVIEW_BAND_FLOOR * review_before))
        if review_after < floor:
            return (f"the review band fell from {review_before:,} pairs to "
                    f"{review_after:,} (below the {floor:,}-pair safety floor)")
    return None


# ---------------------------------------------------------------------------
# The run's model state
# ---------------------------------------------------------------------------


def write_state(run_dir, models: dict[str, TrackModel], warning: str | None = None,
                applied: bool = True) -> dict:
    """Record which model scored this run, for the counts and the pairs API."""
    state = {
        "applied": bool(applied and models),
        "warning": warning,
        "tracks": {track: model.as_dict() for track, model in models.items()},
    }
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / MODEL_STATE_FILENAME).write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )
    return state


def read_state(run_dir) -> dict:
    path = Path(run_dir) / MODEL_STATE_FILENAME
    if not path.is_file():
        return {"applied": False, "warning": None, "tracks": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"applied": False, "warning": None, "tracks": {}}


def clear_state(run_dir) -> None:
    path = Path(run_dir) / MODEL_STATE_FILENAME
    if path.is_file():
        path.unlink()


def counts_from_state(run_dir) -> dict:
    """The model counts `MODEL_API.md` names, in the pipeline's snake_case.

    The lines are recorded on the **run**, not looked up from the model store,
    because activating a newer version must not change what a finished run says
    it was decided by. A screen drawing the accept line on a histogram reads
    this, so the line it draws is the one the pairs were actually bucketed on.
    """
    state = read_state(run_dir)
    tracks = state.get("tracks") or {}
    deciding = [t for t in tracks.values() if t.get("decides")]
    return {
        "model_active": bool(state.get("applied") and tracks),
        "model_version": {track: info["version"] for track, info in tracks.items()},
        "model_accept_line": {track: info.get("accept") for track, info in tracks.items()},
        "model_reject_line": {track: info.get("reject") for track, info in tracks.items()},
        # Graded only when every track that scored is graded: a half-graded run
        # buckets on two different dials, and calling that "graded" would be a lie.
        "model_graded": bool(tracks) and len(deciding) == len(tracks),
        "model_warning": state.get("warning"),
    }


def models_from_state(run_dir) -> dict[str, TrackModel]:
    """The state back as models, for re-bucketing without re-reading the store."""
    out = {}
    for track, info in (read_state(run_dir).get("tracks") or {}).items():
        out[track] = TrackModel(track=track, version=int(info["version"]),
                                graded=bool(info.get("graded")),
                                accept=info.get("accept"), reject=info.get("reject"))
    return out

# backend/app/model/store.py
"""Where a trained model lives, and which version is active.

`MODEL.md`: one model per track, versions immutable under
``DATA_DIR/models/<track>/versions/<n>/``, at most one active per track.

A version folder holds five files and is never written twice:

| File | Holds |
|---|---|
| `model.txt` | the LightGBM booster, in its own text format |
| `calibration.json` | the Platt lookup that turns a raw score into a probability |
| `features.json` | the ordered feature list, with each feature's metadata |
| `report.json` | the whole training report the model panel prints |
| `meta.json` | version, track, when, the seed, the run and config it came from |

Immutability is the point. An active version has to survive a later retrain
unchanged, or "this run was scored by version 2" stops meaning anything. Writing
goes to a temporary folder and renames it into place, so a crash halfway through
leaves no half-version behind.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MODEL_FILENAME = "model.txt"
CALIBRATION_FILENAME = "calibration.json"
FEATURES_FILENAME = "features.json"
REPORT_FILENAME = "report.json"
META_FILENAME = "meta.json"
ACTIVE_FILENAME = "active.json"

TRACKS = ("person", "organisation")

# The summary fields the version list carries. Never the whole report: a panel
# listing ten versions should not download ten reports.
# Which reference tables were there is NOT here: the report's `references` list
# already says, and one fact in two places is one fact to keep in step.
SUMMARY_FIELDS = ("version", "track", "trained_at", "graded", "cold_start", "seed",
                  "run_id", "config_version", "note", "n_features", "n_train_rows",
                  "n_human_labels", "auc", "average_precision", "accept", "reject")


class ModelStoreError(RuntimeError):
    """Something is wrong with the model folder, in words fit to show a user."""


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "data"))


def models_dir() -> Path:
    return data_dir() / "models"


def track_dir(track: str) -> Path:
    return models_dir() / track


def versions_dir(track: str) -> Path:
    return track_dir(track) / "versions"


def version_dir(track: str, version: int) -> Path:
    return versions_dir(track) / str(int(version))


def list_versions(track: str) -> list[int]:
    directory = versions_dir(track)
    if not directory.is_dir():
        return []
    return sorted(int(p.name) for p in directory.iterdir()
                  if p.is_dir() and p.name.isdigit())


def latest_version(track: str) -> int | None:
    versions = list_versions(track)
    return versions[-1] if versions else None


def next_version(track: str) -> int:
    return (latest_version(track) or 0) + 1


def exists(track: str, version: int) -> bool:
    return (version_dir(track, version) / MODEL_FILENAME).exists()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save(track: str, booster, calibration: dict, features: list[dict],
         report: dict, meta: dict) -> int:
    """Write a new immutable version and return its number."""
    version = next_version(track)
    final = version_dir(track, version)
    if final.exists():
        raise ModelStoreError(
            f"Version {version} of the {track} model already exists — refusing to "
            "overwrite a trained version."
        )
    staging = final.with_name(f".{version}.writing")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        booster.save_model(str(staging / MODEL_FILENAME))
        stamped = {
            **meta,
            "version": version,
            "track": track,
            "trained_at": meta.get("trained_at")
            or datetime.now(timezone.utc).isoformat(),
        }
        for name, payload in (
            (CALIBRATION_FILENAME, calibration),
            (FEATURES_FILENAME, features),
            (REPORT_FILENAME, {**report, "version": version}),
            (META_FILENAME, stamped),
        ):
            (staging / name).write_text(json.dumps(payload, indent=2, default=_plain),
                                        encoding="utf-8")
        staging.rename(final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return version


def _plain(value):
    """numpy types out of json.dumps' way."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"{type(value)!r} is not JSON serialisable")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_meta(track: str, version: int) -> dict | None:
    return _read_json(version_dir(track, version) / META_FILENAME)


def load_report(track: str, version: int) -> dict | None:
    return _read_json(version_dir(track, version) / REPORT_FILENAME)


def load_features(track: str, version: int) -> list | None:
    return _read_json(version_dir(track, version) / FEATURES_FILENAME)


def load_calibration(track: str, version: int) -> dict | None:
    return _read_json(version_dir(track, version) / CALIBRATION_FILENAME)


def load_booster(track: str, version: int):
    path = version_dir(track, version) / MODEL_FILENAME
    if not path.exists():
        return None
    import lightgbm as lgb

    return lgb.Booster(model_file=str(path))


def summary(track: str, version: int) -> dict | None:
    """One version as the version list shows it, plus whether it is active."""
    meta = load_meta(track, version)
    if meta is None:
        return None
    out = {field: meta.get(field) for field in SUMMARY_FIELDS}
    out["version"] = int(version)
    out["track"] = track
    out["active"] = get_active(track) == int(version)
    return out


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def apply_calibration(scores, calibration: dict | None) -> np.ndarray:
    """Raw booster probabilities through the saved Platt lookup.

    An absent or unusable calibration is the identity, which is honest: the
    score is then the booster's own, and the report says the method was
    ``identity``.
    """
    values = np.asarray(scores, dtype=float)
    if not calibration:
        return values
    x = np.asarray(calibration.get("x_grid") or [], dtype=float)
    y = np.asarray(calibration.get("y_grid") or [], dtype=float)
    if len(x) < 2 or len(x) != len(y):
        return values
    return np.interp(values, x, y)


# ---------------------------------------------------------------------------
# Which version is active
# ---------------------------------------------------------------------------


def active_file(track: str) -> Path:
    return track_dir(track) / ACTIVE_FILENAME


def get_active(track: str) -> int | None:
    payload = _read_json(active_file(track))
    if not payload:
        return None
    try:
        version = int(payload["version"])
    except (KeyError, TypeError, ValueError):
        return None
    return version if exists(track, version) else None


def set_active(track: str, version: int) -> None:
    if not exists(track, version):
        raise ModelStoreError(f"No version {version} for track {track}")
    path = active_file(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": int(version),
                    "activated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def clear_active(track: str) -> int | None:
    previous = get_active(track)
    path = active_file(track)
    if path.exists():
        path.unlink()
    return previous


def active_summary(track: str) -> dict | None:
    version = get_active(track)
    return summary(track, version) if version is not None else None


def can_auto_accept(track: str) -> bool:
    """Whether the active model is allowed to decide a pair.

    Graded, and with an accept line the frozen test set actually supported.
    A cold-start model is active but decides nothing (`MODEL.md`).
    """
    active = active_summary(track)
    return bool(active and active.get("graded") and active.get("accept") is not None)

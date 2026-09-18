"""Persistence + loading for the global GBT re-scoring model.

The model is cross-run/global: it is trained on the global label store and
applied to every run. The "latest" artifacts live under ``<DATA_DIR>/models/``:

- ``gbt_model.txt``           LightGBM booster (native text format)
- ``gbt_feature_cols.json``   ordered feature column list (schema guard)
- ``gbt_calibration.json``    Platt (logistic) calibration lookup (x_grid/y_grid)
- ``gbt_metrics.json``        last training metrics (auc, n_labels, version, ...)

Every train also snapshots those four artifacts, immutably, under
``models/versions/<N>/`` (monotonic integer ``N``), so an older *active* version
survives a later retrain. ``models/active.json`` records which version is active —
the one fresh runs auto-apply. Training never auto-activates.

The bare (un-versioned) helpers read/write the "latest" artifacts exactly as
before; pass ``version=N`` to read a specific immutable snapshot.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

_MODEL_NAME = "gbt_model.txt"
_FEATURES_NAME = "gbt_feature_cols.json"
_CALIB_NAME = "gbt_calibration.json"
_METRICS_NAME = "gbt_metrics.json"


def model_dir() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    d = data_dir / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def versions_dir() -> Path:
    d = model_dir() / "versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def version_dir(version: int) -> Path:
    return versions_dir() / str(version)


def _base_dir(version: Optional[int]) -> Path:
    """Artifact directory for *version* (an immutable snapshot) or the 'latest' models dir."""
    return version_dir(version) if version is not None else model_dir()


def model_file(version: Optional[int] = None) -> Path:
    return _base_dir(version) / _MODEL_NAME


def feature_cols_file(version: Optional[int] = None) -> Path:
    return _base_dir(version) / _FEATURES_NAME


def calibration_file(version: Optional[int] = None) -> Path:
    return _base_dir(version) / _CALIB_NAME


def metrics_file(version: Optional[int] = None) -> Path:
    return _base_dir(version) / _METRICS_NAME


def model_exists(version: Optional[int] = None) -> bool:
    return model_file(version).exists() and feature_cols_file(version).exists()


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------


def _next_version() -> int:
    nums = [int(p.name) for p in versions_dir().iterdir() if p.is_dir() and p.name.isdigit()]
    return max(nums, default=0) + 1


def list_versions() -> list[int]:
    return sorted(int(p.name) for p in versions_dir().iterdir() if p.is_dir() and p.name.isdigit())


def latest_version() -> Optional[int]:
    """Version stamped into the current 'latest' metrics (the most recent train)."""
    v = (load_metrics() or {}).get("version")
    return int(v) if v is not None else None


def _snapshot_version(version: int) -> None:
    """Copy the just-written 'latest' artifacts into an immutable ``versions/<N>/`` dir."""
    dest = version_dir(version)
    dest.mkdir(parents=True, exist_ok=True)
    for name in (_MODEL_NAME, _FEATURES_NAME, _CALIB_NAME, _METRICS_NAME):
        src = model_dir() / name
        if src.exists():
            shutil.copy2(src, dest / name)


# ---------------------------------------------------------------------------
# Active-model state (models/active.json)
# ---------------------------------------------------------------------------


def active_file() -> Path:
    return model_dir() / "active.json"


def get_active_version() -> Optional[int]:
    """The activated model version fresh runs auto-apply, or None if none is active."""
    p = active_file()
    if not p.exists():
        return None
    try:
        return int(json.loads(p.read_text(encoding="utf-8"))["version"])
    except Exception:
        return None


def set_active_version(version: int) -> None:
    active_file().write_text(
        json.dumps({"version": int(version),
                    "activated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def clear_active_version() -> None:
    p = active_file()
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Metrics + artifacts
# ---------------------------------------------------------------------------


def save_metrics(metrics: dict) -> None:
    """Persist training metrics AND snapshot a new immutable model version.

    Called as ``train()``'s final step (so gbt_train needs no edit): by now the
    booster, feature-cols and calibration files are already on disk. We stamp a
    monotonic ``version`` + ``trained_at`` into *metrics* (mutated in place, so the
    caller's returned dict carries them too), write them as the 'latest' metrics,
    then archive all four artifacts under ``models/versions/<version>/``.
    """
    version = _next_version()
    metrics["version"] = version
    metrics["trained_at"] = datetime.now(timezone.utc).isoformat()
    metrics_file().write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    _snapshot_version(version)


def load_metrics(version: Optional[int] = None) -> Optional[dict]:
    p = metrics_file(version)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_feature_cols(version: Optional[int] = None) -> Optional[list]:
    p = feature_cols_file(version)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_booster(version: Optional[int] = None):
    """Load the LightGBM booster for *version* (or latest), or None if absent."""
    mf = model_file(version)
    if not mf.exists():
        return None
    import lightgbm as lgb

    return lgb.Booster(model_file=str(mf))


def save_calibration(x_grid: list, y_grid: list, meta: dict | None = None) -> None:
    payload = {"x_grid": list(x_grid), "y_grid": list(y_grid)}
    if meta:
        payload.update(meta)
    calibration_file().write_text(json.dumps(payload), encoding="utf-8")


def apply_calibration(scores, version: Optional[int] = None) -> np.ndarray:
    """Map raw GBT scores through the saved Platt (logistic) calibration (identity if absent)."""
    scores = np.asarray(scores, dtype=float)
    p = calibration_file(version)
    if not p.exists():
        return scores
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        x = np.asarray(data["x_grid"], dtype=float)
        y = np.asarray(data["y_grid"], dtype=float)
        if len(x) < 2:
            return scores
        return np.interp(scores, x, y)
    except Exception:
        return scores

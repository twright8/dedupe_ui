# backend/app/profiles/__init__.py
"""Profile registry. One instance of this app serves exactly one profile."""

import os

from app.profiles.base import (  # noqa: F401  (re-exported for convenience)
    SHARED_COLUMNS,
    DisplayColumn,
    InputSpec,
    Profile,
    Track,
    validate_records,
)
from app.profiles.donations import DonationsProfile
from app.profiles.psc import PscProfile

DEFAULT_PROFILE_KEY = "donations"

_PROFILE_CLASSES = {
    "donations": DonationsProfile,
    "psc": PscProfile,
}

_instances: dict[str, Profile] = {}


def get_profile(key: str | None = None) -> Profile:
    """Return the profile named by *key*, or by the PROFILE env var.

    Reads the environment at call time so tests can switch profile. An unknown
    key raises — main.py builds the app from the profile, so a typo in the
    service file fails at startup rather than half way through a run.
    """
    name = (key or os.environ.get("PROFILE") or DEFAULT_PROFILE_KEY).strip().lower()
    if name not in _PROFILE_CLASSES:
        known = ", ".join(sorted(_PROFILE_CLASSES))
        raise RuntimeError(
            f"Unknown PROFILE '{name}'. Set PROFILE to one of: {known}."
        )
    if name not in _instances:
        _instances[name] = _PROFILE_CLASSES[name]()
    return _instances[name]


def profile_keys() -> list[str]:
    return sorted(_PROFILE_CLASSES)

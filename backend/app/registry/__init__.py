# backend/app/registry/__init__.py
"""The durable entity registry.

Entity IDs outlive runs (`DESIGN.md` D14). A run only ever writes a proposal;
publishing a run is what moves the registry, and it moves in one transaction so
a half-written registry can never exist.

``store`` is the only module that reads or writes the three tables.
"""

from app.registry.store import (  # noqa: F401
    RegistryError,
    active_entities,
    alias_rows,
    current_members,
    entity_detail,
    latest_publication,
    publication,
    publish,
    resolve,
)

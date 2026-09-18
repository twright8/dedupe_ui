# backend/app/services/audit_logger.py
"""Append-only audit event logger."""

import json
from datetime import datetime, timezone
from typing import Any

from app.db import write_db

_VALID_KINDS = {"run", "label", "config", "threshold", "upload", "export",
                "model", "publish"}


def log_event(
    db_path: str,
    user: str,
    kind: str,
    description: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert one row into audit_log. Returns the new row id.

    Args:
        db_path:     Path to the SQLite database file.
        user:        Display name of the acting user.
        kind:        Event category — one of run, label, config, threshold, upload, export.
        description: Human-readable summary of the event.
        metadata:    Optional dict of extra context; stored as JSON.

    Returns:
        lastrowid of the inserted row.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Invalid audit kind {kind!r}. Must be one of: {sorted(_VALID_KINDS)}")

    timestamp = datetime.now(timezone.utc).isoformat()
    metadata_json = json.dumps(metadata) if metadata is not None else None

    return write_db(
        db_path,
        """INSERT INTO audit_log (timestamp, user_name, kind, description, metadata_json)
           VALUES (?, ?, ?, ?, ?)""",
        (timestamp, user, kind, description, metadata_json),
    )

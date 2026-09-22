#!/usr/bin/env python
"""Give a used instance the profile's current default linkage settings.

The app seeds config version 1 from the profile defaults only when the
database has no version at all. Once a tool has been used, a better default
shipped with the code changes nothing on that instance: every run keeps
reading the version the user has. This script closes that gap for the
operator. It saves a NEW config version whose linkage settings are the
profile's defaults and whose ruleset is the current one, unchanged. Nothing is
overwritten, and the version history shows what happened and when.

    PROFILE=donations python scripts/adopt_linkage_defaults.py \
        --db /var/lib/dedupe_ui/donations/linkage.db

It does nothing when the instance has never been used (the app will seed it on
start) or when the current version already holds the defaults. It refuses to
save settings that do not validate against the current ruleset, and it says
what it would change with ``--dry-run``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.profiles import get_profile  # noqa: E402
from app.rules import engine, linkage  # noqa: E402
from app.services import config_manager  # noqa: E402

DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "app" / "profiles" / "defaults"


def default_settings(profile_key: str) -> dict:
    path = DEFAULTS_DIR / profile_key / "linkage_settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def describe_change(before: dict, after: dict) -> list[str]:
    """Plain lines saying what differs, top level and per track."""
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key == "tracks" or key.startswith("_"):
            continue
        if before.get(key) != after.get(key):
            lines.append(f"{key}: {json.dumps(before.get(key))} -> {json.dumps(after.get(key))}")
    b_tracks, a_tracks = before.get("tracks") or {}, after.get("tracks") or {}
    for track in sorted(set(b_tracks) | set(a_tracks)):
        b, a = b_tracks.get(track) or {}, a_tracks.get(track) or {}
        for key in sorted(set(b) | set(a)):
            if key.startswith("_"):
                continue
            if b.get(key) != a.get(key):
                lines.append(f"tracks.{track}.{key}: changed")
    return lines


def adopt(db_path: str, profile_key: str, created_by: str = "system",
          dry_run: bool = False, out=sys.stdout) -> int | None:
    """Save the new version and return its number, or None when nothing was saved."""
    init_db(db_path)
    current = config_manager.get_current(db_path)
    if current is None or not current.get("ruleset"):
        print("Never used: no config version to update. The app seeds one on start.", file=out)
        return None
    ruleset = current["ruleset"]
    before = json.loads(current.get("linkage_settings") or "{}")
    after = default_settings(profile_key)
    if before == after:
        print(f"Version {current['version']} already holds the profile defaults. Nothing to do.",
              file=out)
        return None
    raw = list(get_profile(profile_key).raw_columns)
    errors = engine.validate_ruleset(ruleset, raw)
    errors = errors + linkage.validate_linkage_settings(after, ruleset, raw)
    if errors:
        print("The defaults do not validate against the current ruleset. Nothing saved.", file=out)
        for error in errors:
            print(f"  {error}", file=out)
        return None
    changes = describe_change(before, after)
    for line in changes:
        print(f"  {line}", file=out)
    if dry_run:
        print(f"Dry run: would save version {current['version'] + 1}.", file=out)
        return None
    note = ("Linkage settings replaced with the profile defaults shipped with the code "
            f"(scripts/adopt_linkage_defaults.py). Rules unchanged from version {current['version']}.")
    version = config_manager.save_version(db_path, created_by=created_by, note=note,
                                          ruleset=ruleset, linkage_settings=after)
    print(f"Saved config version {version}.", file=out)
    return version


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="the instance's linkage.db")
    parser.add_argument("--profile", default=os.environ.get("PROFILE"),
                        help="profile key; defaults to $PROFILE")
    parser.add_argument("--by", default="system", help="who to record as the author")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.profile:
        parser.error("--profile or $PROFILE is required")
    adopt(args.db, args.profile, created_by=args.by, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

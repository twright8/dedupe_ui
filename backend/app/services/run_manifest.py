# backend/app/services/run_manifest.py
"""What produced this run — written once, at the start, and finished at the end.

A published merge is defended against two things: the rules that were in force,
and the code that read them. A config version pins the first. Nothing pinned
the second, and nothing pinned the input file either: the ``runs`` table held a
filename and no hash, no size and no row count, so a file swapped under the same
name left no trace (`docs/TERMINOLOGY_AUDIT.md`, gap 3).

This module writes ``run_manifest.json`` into the run folder and copies the few
fields a list view needs onto the ``runs`` row. It holds:

* the input file — name, size, sha256, row count, when it was uploaded
* the code version — the git commit when there is a ``.git``, else the
  ``APP_VERSION`` environment variable, else ``"unknown"``
* the config version, and the three lines in force: accept, review, and the
  lowest score kept
* per track, the scorer that decided and, where a model did, its version, its
  training date and how many labels it learnt from
* each reference table's name, row count, build time and file hash
* the library versions of pandas, duckdb, splink and lightgbm
* when the run started and ended, and who started it

Nothing here fails a run. A manifest that cannot be written is logged and the
run carries on: losing the provenance of a run is bad, and losing the run is
worse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "run_manifest.json"

#: Read in 8 MB pieces. A PSC snapshot is tens of gigabytes and must never be
#: held in memory to be hashed.
HASH_CHUNK = 8 * 1024 * 1024

#: The libraries whose version changes an answer. Anything else is noise.
LIBRARIES = ("pandas", "duckdb", "splink", "lightgbm", "numpy", "pyarrow")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The code version
# ---------------------------------------------------------------------------


def code_version(root: Path | None = None) -> str:
    """The commit this code is, without shelling out.

    The server deploys by ``git checkout``, so ``.git/HEAD`` is there and
    naming it is enough. ``HEAD`` is either a ref to follow or a commit id
    already. When there is no ``.git`` — a container built from a tarball —
    ``APP_VERSION`` answers instead, and failing that the honest word
    ``unknown``.
    """
    root = Path(root) if root else Path(__file__).resolve().parents[3]
    git_dir = root / ".git"
    try:
        if git_dir.is_file():
            # A worktree: .git is a file naming the real directory.
            pointer = git_dir.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_dir = Path(pointer.split(":", 1)[1].strip())
        if git_dir.is_dir():
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref:"):
                ref = head.split(":", 1)[1].strip()
                ref_path = git_dir / ref
                if ref_path.is_file():
                    return ref_path.read_text(encoding="utf-8").strip()
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        if line.endswith(f" {ref}"):
                            return line.split(" ", 1)[0].strip()
            elif head:
                return head
    except OSError:
        logger.debug("Could not read a git commit from %s", git_dir)
    return os.environ.get("APP_VERSION") or "unknown"


def library_versions() -> dict[str, str]:
    """``{library: version}`` for the libraries whose version changes an answer."""
    from importlib import metadata

    found = {}
    for name in LIBRARIES:
        try:
            found[name] = metadata.version(name)
        except Exception:  # not installed, or no metadata — say so plainly
            found[name] = "not installed"
    return found


# ---------------------------------------------------------------------------
# The input file
# ---------------------------------------------------------------------------


def sha256_of(path: str | Path) -> str:
    """The file's sha256, read in pieces so a huge file is never held whole."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(HASH_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def input_facts(path: str | Path, uploaded_at: str | None = None) -> dict:
    """Name, size, sha256 and upload time for the file a run was given."""
    path = Path(path)
    facts = {
        "filename": path.name,
        "size_bytes": None,
        "sha256": None,
        "row_count": None,
        "uploaded_at": uploaded_at,
    }
    try:
        facts["size_bytes"] = path.stat().st_size
        facts["sha256"] = sha256_of(path)
    except OSError:
        logger.warning("Could not read the input file %s for the manifest", path)
    return facts


# ---------------------------------------------------------------------------
# The reference tables and the models
# ---------------------------------------------------------------------------


def _plain(key: str) -> str:
    """A file key as a sentence, for when a reference declares no label."""
    text = str(key).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else str(key)


def reference_facts(profile=None) -> list[dict]:
    """Each reference table's name, row count, build time and file hash.

    The table is hashed here rather than trusted from the sidecar, because the
    file is overwritten in place and a stale sidecar would be worse than none
    (`docs/TERMINOLOGY_AUDIT.md`, gap 10). ``source_sha256`` is the hash of the
    file it was built from, which the build script records.
    """
    from app.model import references as reference_lib

    out = []
    try:
        declared = reference_lib.status(profile)
    except Exception:
        logger.debug("Could not read the reference tables for the manifest")
        return out

    for entry in declared:
        key = entry.get("key") or entry.get("name")
        fact = {
            "key": key,
            # `label` is what a person is shown; `name` is the file's key.
            "label": entry.get("label") or _plain(key),
            "name": entry.get("name") or key,
            "present": entry.get("present"),
            "rows": entry.get("rows"),
            "built_at": entry.get("built_at"),
            "path": entry.get("path"),
            "sha256": None,
            "source": None,
            "source_sha256": None,
        }
        try:
            meta = reference_lib.meta_for(key) or {}
            fact["source"] = meta.get("source")
            fact["source_sha256"] = meta.get("source_sha256")
            path = reference_lib.path_for(key)
            if Path(path).is_file():
                fact["sha256"] = sha256_of(path)
        except Exception:
            logger.debug("No reference metadata for %s", key)
        out.append(fact)
    return out


def scorer_facts(run_dir: str | Path, settings: dict | None = None) -> dict:
    """Per track: which scorer decided, and the model version behind it.

    Reads the run's own ``model_state.json``, which the apply-model step writes,
    so a later rescore cannot change what this run says.
    """
    from app.model import store as model_store

    state_path = Path(run_dir) / "model_state.json"
    state: dict = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    tracks = state.get("tracks") if isinstance(state.get("tracks"), dict) else state

    out: dict[str, dict] = {}
    for track, detail in (tracks or {}).items():
        if not isinstance(detail, dict):
            continue
        version = detail.get("version")
        entry = {
            "scorer": "model" if version is not None else "splink",
            "model_version": version,
            "graded": detail.get("graded"),
            "accept": detail.get("accept"),
            "reject": detail.get("reject"),
            "trained_at": None,
            "n_train_rows": None,
            "n_human_labels": None,
        }
        if version is not None:
            try:
                meta = model_store.load_meta(track, int(version)) or {}
                entry["trained_at"] = meta.get("trained_at")
                entry["n_train_rows"] = meta.get("n_train_rows")
                entry["n_human_labels"] = meta.get("n_human_labels")
            except Exception:
                logger.debug("No model metadata for %s v%s", track, version)
        out[str(track)] = entry
    return out


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------


def build(
    run_id: str,
    run_dir: str | Path,
    config_version: int | None,
    input_path: str | Path | None,
    thresholds: dict,
    triggered_by: str = "",
    uploaded_at: str | None = None,
    started_at: str | None = None,
) -> dict:
    """Everything known when a run starts."""
    return {
        "run_id": run_id,
        "started_at": started_at or now(),
        "finished_at": None,
        "triggered_by": triggered_by or "",
        "code_version": code_version(),
        "config_version": config_version,
        "thresholds": {
            "accept_line": thresholds.get("accept_line"),
            "review_line": thresholds.get("review_line"),
            "lowest_score_kept": thresholds.get("lowest_score_kept"),
        },
        "input": input_facts(input_path, uploaded_at) if input_path else {},
        "libraries": library_versions(),
        "references": reference_facts(),
        "scorers": {},
    }


def write(run_dir: str | Path, manifest: dict) -> Path | None:
    """Write the manifest. A failure is logged and never fails the run."""
    path = Path(run_dir) / MANIFEST_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return path
    except OSError:
        logger.exception("Could not write %s", path)
        return None


def read(run_dir: str | Path) -> dict:
    """The manifest, or an empty answer when the run has none."""
    path = Path(run_dir) / MANIFEST_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def update(run_dir: str | Path, **fields) -> dict:
    """Merge *fields* into the run's manifest and write it back."""
    manifest = read(run_dir)
    if not manifest:
        manifest = {"run_id": Path(run_dir).name}
    manifest.update(fields)
    write(run_dir, manifest)
    return manifest


def finish(run_dir: str | Path, counts: dict | None = None,
           row_count: int | None = None) -> dict:
    """Close the manifest when the run ends: the end time and the row count."""
    manifest = read(run_dir)
    if not manifest:
        manifest = {"run_id": Path(run_dir).name}
    manifest["finished_at"] = now()
    if row_count is not None:
        manifest.setdefault("input", {})["row_count"] = int(row_count)
    manifest["scorers"] = scorer_facts(run_dir)
    if counts:
        manifest["counts"] = counts
    write(run_dir, manifest)
    return manifest

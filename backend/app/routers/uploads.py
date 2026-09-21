# backend/app/routers/uploads.py
"""Chunked file upload router.

Clients split large files into chunks and POST each chunk individually.
When all chunks for an upload session have arrived the server reassembles them
into a single file under DATA_DIR/uploads/.
"""
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from app.auth import current_user
from app.db import query_db, write_db
from app.services import run_manifest
from app.services.audit_logger import log_event
from app.services.upload_handler import reassemble_chunks, safe_filename, save_chunk, validate_zip

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _uploads_dir() -> str:
    """Resolve the uploads directory at call time.

    Reads DATA_DIR from the environment (defaulting to 'data') so that tests
    can override it without modifying the module at import time.
    """
    try:
        from app.main import DATA_DIR
        data_dir = Path(DATA_DIR)
    except Exception:
        data_dir = Path(os.environ.get("DATA_DIR", "data"))
    return str(data_dir / "uploads")


def _chunk_dir(uploads_dir: str, upload_id: str) -> Path:
    return Path(uploads_dir) / upload_id


def _db_path() -> str:
    from app.main import DB_PATH
    return DB_PATH


def _cleanup_stale_chunks(uploads_dir: str, max_age_secs: int = 24 * 60 * 60) -> None:
    """Remove abandoned chunk directories older than max_age_secs."""
    base = Path(uploads_dir).resolve()
    if not base.exists():
        return
    now = time.time()
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            child.resolve().relative_to(base)
        except ValueError:
            continue
        try:
            if now - child.stat().st_mtime > max_age_secs:
                shutil.rmtree(child)
        except OSError:
            continue


@router.post("")
async def upload_chunk(
    chunk: UploadFile,
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    user_name: str = Depends(current_user),
):
    """Receive a single chunk and, if it completes the upload, reassemble.

    Form fields:
        chunk (file):    Raw binary chunk data.
        upload_id (str): Unique identifier for this upload session.
        chunk_index (int): Zero-based index of this chunk.
        total_chunks (int): Total number of chunks in this upload.
        filename (str):  Desired filename for the reassembled file.

    Returns:
        {"status": "chunk_received", "chunk_index": N}  — more chunks expected.
        {"status": "complete", "filename": "...", "path": "..."}  — all done.
    """
    uploads_dir = _uploads_dir()
    Path(uploads_dir).mkdir(parents=True, exist_ok=True)
    # Only clean stale chunks on the first chunk of a session, not every chunk
    if chunk_index == 0:
        _cleanup_stale_chunks(uploads_dir)
    db_path = _db_path()
    stored_filename = safe_filename(filename)
    data = await chunk.read()

    existing = query_db(db_path, "SELECT * FROM upload_sessions WHERE upload_id = ?", (upload_id,))
    if existing and existing[0]["status"] == "complete":
        raise HTTPException(status_code=409, detail="Upload session already complete")
    if existing and existing[0]["total_chunks"] != total_chunks:
        raise HTTPException(status_code=400, detail="total_chunks changed for upload session")
    if not existing:
        write_db(
            db_path,
            """INSERT INTO upload_sessions
               (upload_id, filename, stored_filename, total_chunks, chunks_received, size_bytes, status)
               VALUES (?, ?, ?, ?, 0, 0, 'uploading')""",
            (upload_id, filename, stored_filename, total_chunks),
        )

    chunk_dir = _chunk_dir(uploads_dir, upload_id)
    is_new_chunk = not (chunk_dir / f"chunk_{chunk_index:06d}").exists()
    save_chunk(uploads_dir, upload_id, chunk_index, data)

    # Count DISTINCT chunk files actually on disk rather than a blind +1 counter. A client
    # retry that re-sends a chunk then can't double-count or trip the completion check
    # early (which would reassemble before every chunk has landed). Idempotent by design.
    received = sum(
        1 for i in range(total_chunks) if (chunk_dir / f"chunk_{i:06d}").exists()
    )
    write_db(
        db_path,
        "UPDATE upload_sessions SET chunks_received = ?, size_bytes = size_bytes + ? WHERE upload_id = ?",
        (received, len(data) if is_new_chunk else 0, upload_id),
    )

    if received >= total_chunks:
        # Single-winner reassembly: claim the session so two near-simultaneous final chunks
        # can't both reassemble and race on the output file. (All DB ops are serialised by a
        # process-wide lock, so the read-then-claim is effectively atomic here.)
        claim = query_db(db_path, "SELECT status FROM upload_sessions WHERE upload_id = ?", (upload_id,))
        if claim and claim[0]["status"] != "uploading":
            return {"status": "chunk_received", "chunk_index": chunk_index}
        write_db(db_path, "UPDATE upload_sessions SET status = 'assembling' WHERE upload_id = ?", (upload_id,))
        try:
            out_path = reassemble_chunks(
                uploads_dir, upload_id, total_chunks, stored_filename
            )
        except ValueError as exc:
            write_db(
                db_path,
                "UPDATE upload_sessions SET status = 'error', error_message = ? WHERE upload_id = ?",
                (str(exc), upload_id),
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if Path(stored_filename).suffix.lower() == ".zip" and not validate_zip(out_path):
            Path(out_path).unlink(missing_ok=True)
            write_db(
                db_path,
                "UPDATE upload_sessions SET status = 'error', error_message = ? WHERE upload_id = ?",
                ("Uploaded file is not a valid ZIP archive", upload_id),
            )
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP archive")
        # Hashed once, here, while the file is still the thing that was
        # uploaded. A run copies the hash onto its own row, so a file swapped
        # under the same name later shows up (docs/PROVENANCE.md).
        try:
            digest = run_manifest.sha256_of(out_path)
            size_bytes = Path(out_path).stat().st_size
        except OSError:
            digest, size_bytes = None, None
        write_db(
            db_path,
            """UPDATE upload_sessions
               SET status = 'complete', stored_filename = ?, sha256 = ?,
                   completed_at = datetime('now')
               WHERE upload_id = ?""",
            (Path(out_path).name, digest, upload_id),
        )
        log_event(
            db_path,
            user=user_name,
            kind="upload",
            description=f"Uploaded {filename}",
            metadata={"upload_id": upload_id, "filename": filename,
                      "stored_filename": Path(out_path).name,
                      "sha256": digest, "size_bytes": size_bytes},
        )
        return {"status": "complete", "filename": filename, "path": out_path,
                "sha256": digest, "size_bytes": size_bytes}

    return {"status": "chunk_received", "chunk_index": chunk_index}


@router.get("/{upload_id}/status")
def upload_status(upload_id: str, total_chunks: int | None = None):
    """Return how many chunks have been received for an upload session.

    Query params:
        total_chunks (int): Total expected chunks (required to compute completeness).

    Returns:
        {"upload_id": "...", "chunks_received": N, "complete": bool}
    """
    db_path = _db_path()
    rows = query_db(db_path, "SELECT * FROM upload_sessions WHERE upload_id = ?", (upload_id,))
    if rows:
        row = rows[0]
        return {
            "upload_id": upload_id,
            "filename": row["filename"],
            "stored_filename": row["stored_filename"],
            "chunks_received": row["chunks_received"],
            "total_chunks": row["total_chunks"],
            "complete": row["status"] == "complete",
            "status": row["status"],
            "error_message": row["error_message"],
        }

    if total_chunks is None:
        raise HTTPException(status_code=404, detail="Upload session not found")

    uploads_dir = _uploads_dir()
    chunk_dir = _chunk_dir(uploads_dir, upload_id)

    chunks_received = 0
    if chunk_dir.exists():
        chunks_received = sum(
            1
            for i in range(total_chunks)
            if (chunk_dir / f"chunk_{i:06d}").exists()
        )

    return {
        "upload_id": upload_id,
        "chunks_received": chunks_received,
        "complete": chunks_received == total_chunks,
    }

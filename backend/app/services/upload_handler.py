# backend/app/services/upload_handler.py
"""Chunked file upload service.

Handles writing individual chunks to disk, reassembling them in order into a
final file, and validating that a reassembled file is a valid ZIP archive.
"""
import shutil
import zipfile
from pathlib import Path
from uuid import uuid4


def safe_filename(filename: str) -> str:
    """Return a path-safe filename while preserving a useful extension."""
    name = Path(filename).name.strip().replace("\\", "_").replace("/", "_")
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
    return cleaned or f"upload_{uuid4().hex}"


def save_chunk(uploads_dir: str, upload_id: str, chunk_index: int, data: bytes) -> None:
    """Write a single chunk to disk.

    Args:
        uploads_dir: Base directory for all uploads.
        upload_id: Unique identifier for this upload session.
        chunk_index: Zero-based index of this chunk.
        data: Raw bytes for this chunk.
    """
    chunk_dir = Path(uploads_dir) / upload_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = chunk_dir / f"chunk_{chunk_index:06d}"
    chunk_path.write_bytes(data)


def reassemble_chunks(
    uploads_dir: str, upload_id: str, total_chunks: int, filename: str
) -> str:
    """Reassemble ordered chunks into a single file.

    Reads chunks 0..total_chunks-1 in order, concatenates their bytes into
    ``{uploads_dir}/{filename}``, then deletes the chunk directory.

    Args:
        uploads_dir: Base directory for all uploads.
        upload_id: Unique identifier for this upload session.
        total_chunks: Expected number of chunks.
        filename: Filename to write the reassembled file to.

    Returns:
        Absolute path to the reassembled file as a string.

    Raises:
        ValueError: If any expected chunk is missing from disk.
    """
    chunk_dir = Path(uploads_dir) / upload_id

    # Validate all chunks are present before writing anything
    missing = []
    for i in range(total_chunks):
        chunk_path = chunk_dir / f"chunk_{i:06d}"
        if not chunk_path.exists():
            missing.append(i)
    if missing:
        raise ValueError(
            f"Missing chunks for upload '{upload_id}': {missing}"
        )

    out_path = Path(uploads_dir) / safe_filename(filename)
    with out_path.open("wb") as out_fh:
        for i in range(total_chunks):
            chunk_path = chunk_dir / f"chunk_{i:06d}"
            out_fh.write(chunk_path.read_bytes())

    # Clean up chunk directory after successful reassembly
    shutil.rmtree(str(chunk_dir))

    return str(out_path)


def validate_zip(path: str) -> bool:
    """Return True if the file at *path* is a valid ZIP archive.

    Args:
        path: Filesystem path to the file to check.

    Returns:
        True if the file is a valid ZIP, False otherwise.
    """
    return zipfile.is_zipfile(path)

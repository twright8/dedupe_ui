# backend/tests/test_upload_handler.py
import os
import zipfile

import pytest

from app.services.upload_handler import save_chunk, reassemble_chunks, validate_zip


def test_save_and_reassemble_chunks(tmp_path):
    upload_id = "test_upload"
    save_chunk(str(tmp_path), upload_id, chunk_index=0, data=b"Hello ")
    save_chunk(str(tmp_path), upload_id, chunk_index=1, data=b"World!")
    out_path = reassemble_chunks(str(tmp_path), upload_id, total_chunks=2, filename="test.txt")
    assert open(out_path, "rb").read() == b"Hello World!"


def test_reassemble_out_of_order(tmp_path):
    upload_id = "test_ooo"
    save_chunk(str(tmp_path), upload_id, chunk_index=1, data=b"World!")
    save_chunk(str(tmp_path), upload_id, chunk_index=0, data=b"Hello ")
    out_path = reassemble_chunks(str(tmp_path), upload_id, total_chunks=2, filename="test.txt")
    assert open(out_path, "rb").read() == b"Hello World!"


def test_reassemble_missing_chunk_raises(tmp_path):
    upload_id = "test_missing"
    save_chunk(str(tmp_path), upload_id, chunk_index=0, data=b"Hello ")
    with pytest.raises(ValueError):
        reassemble_chunks(str(tmp_path), upload_id, total_chunks=2, filename="test.txt")


def test_validate_zip_valid(tmp_path):
    zf_path = tmp_path / "good.zip"
    with zipfile.ZipFile(str(zf_path), "w") as zf:
        zf.writestr("test.txt", "hello")
    assert validate_zip(str(zf_path)) is True


def test_validate_zip_invalid(tmp_path):
    bad = tmp_path / "notazip.zip"
    bad.write_bytes(b"not a zip file")
    assert validate_zip(str(bad)) is False


def test_chunk_dir_cleaned_after_reassembly(tmp_path):
    upload_id = "test_cleanup"
    save_chunk(str(tmp_path), upload_id, chunk_index=0, data=b"data")
    reassemble_chunks(str(tmp_path), upload_id, total_chunks=1, filename="out.bin")
    assert not os.path.exists(str(tmp_path / upload_id))

"""The bounded DuckDB connection.

A runaway query filled a 1 TB disk with spill files. The cap that stops that is
``max_temp_directory_size``; these tests check it is actually on the connections
the pipeline uses, including the one Splink opens for itself.
"""

import duckdb
import pytest

from app import duckdb_conn


def test_a_connection_carries_all_three_limits(tmp_path):
    con = duckdb_conn.connect(tmp_path / "spill")
    settings = duckdb_conn.settings_of(con)
    assert settings["temp_directory"] == str(tmp_path / "spill")
    # DuckDB echoes sizes back in its own units, so compare on magnitude.
    assert "GiB" in settings["max_temp_directory_size"]
    assert "GiB" in settings["memory_limit"]
    assert (tmp_path / "spill").is_dir()


def test_the_caps_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(duckdb_conn.MAX_TEMP_ENV, "3GB")
    monkeypatch.setenv(duckdb_conn.MEMORY_LIMIT_ENV, "1GB")
    settings = duckdb_conn.settings_of(duckdb_conn.connect(tmp_path / "spill"))
    assert settings["max_temp_directory_size"] == "2.7 GiB"   # 3 GB
    assert settings["memory_limit"] == "953.6 MiB"            # 1 GB


def test_the_defaults_are_the_documented_ones(monkeypatch):
    monkeypatch.delenv(duckdb_conn.MAX_TEMP_ENV, raising=False)
    monkeypatch.delenv(duckdb_conn.MEMORY_LIMIT_ENV, raising=False)
    assert duckdb_conn.max_temp() == "20GB"
    assert duckdb_conn.memory_limit() == "6GB"


def test_a_query_that_would_outspill_the_cap_fails_instead_of_filling_the_disk(tmp_path,
                                                                               monkeypatch):
    # The whole point. A tiny memory limit forces spilling; a tiny spill cap
    # then stops it. Without the cap this query simply succeeds, slowly, having
    # written as much as it liked.
    monkeypatch.setenv(duckdb_conn.MEMORY_LIMIT_ENV, "10MB")
    monkeypatch.setenv(duckdb_conn.MAX_TEMP_ENV, "10MB")
    con = duckdb_conn.connect(tmp_path / "spill")
    with pytest.raises(duckdb.Error) as caught:
        con.execute(
            "select a.range x, b.range y from range(400000) a, range(40) b "
            "order by x desc, y desc"
        ).fetchall()
    # DuckDB names the limit it hit, so the message tells the operator what to raise.
    assert "temp" in str(caught.value).lower()


def test_splinks_own_connection_gets_the_caps_too(tmp_path):
    # Splink opens its own DuckDB, so the limits have to be applied to a
    # connection this project did not create. Predict and EM are exactly where
    # the 53 GB spill happened.
    from app.pipeline.dedupe.stage_3_score import _db_api

    api = _db_api(tmp_path / "duckdb_tmp")
    settings = duckdb_conn.settings_of(api._con)
    assert settings["temp_directory"] == str(tmp_path / "duckdb_tmp")
    assert "GiB" in settings["max_temp_directory_size"]


def test_stale_spill_is_cleared_and_real_files_are_left_alone(tmp_path):
    spill = tmp_path / "duckdb_tmp"
    spill.mkdir()
    (spill / "duckdb_temp_storage_DEFAULT-0.tmp").write_bytes(b"x" * 2048)
    (spill / "duckdb_temp_storage_S32K-1.tmp").write_bytes(b"x" * 1024)
    keep = spill / "units.parquet"
    keep.write_text("not a spill file", encoding="utf-8")

    assert duckdb_conn.clear_spill(spill) == 3072
    assert not list(spill.glob("duckdb_temp_storage_*.tmp"))
    assert keep.is_file()
    assert duckdb_conn.clear_spill(tmp_path / "does_not_exist") == 0

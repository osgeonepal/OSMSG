"""Operator env tunes DuckDB memory/threads/spill on every connection."""

import pytest

from osmsg.db.schema import connect


def test_no_env_leaves_defaults(monkeypatch):
    for var in ("OSMSG_DUCKDB_MEMORY_LIMIT", "OSMSG_DUCKDB_THREADS", "OSMSG_DUCKDB_TEMP_DIR"):
        monkeypatch.delenv(var, raising=False)
    conn = connect(":memory:")
    assert conn.execute("SELECT current_setting('threads')").fetchone()[0] >= 1
    conn.close()


def test_threads_and_temp_dir_applied(monkeypatch, tmp_path):
    spill = tmp_path / "duckdb-tmp"
    monkeypatch.setenv("OSMSG_DUCKDB_THREADS", "2")
    monkeypatch.setenv("OSMSG_DUCKDB_TEMP_DIR", str(spill))
    monkeypatch.delenv("OSMSG_DUCKDB_MEMORY_LIMIT", raising=False)
    conn = connect(":memory:")
    assert conn.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    assert conn.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(spill)
    assert spill.is_dir()
    conn.close()


def test_valid_memory_limit_applied(monkeypatch):
    monkeypatch.setenv("OSMSG_DUCKDB_MEMORY_LIMIT", "1GB")
    conn = connect(":memory:")
    # DuckDB normalises decimal "1GB" to its binary equivalent, "953.6 MiB".
    assert "mib" in conn.execute("SELECT lower(current_setting('memory_limit'))").fetchone()[0]
    conn.close()


def test_malformed_memory_limit_fails_loud(monkeypatch):
    monkeypatch.setenv("OSMSG_DUCKDB_MEMORY_LIMIT", "lots'; DROP")
    with pytest.raises(ValueError, match="OSMSG_DUCKDB_MEMORY_LIMIT"):
        connect(":memory:")

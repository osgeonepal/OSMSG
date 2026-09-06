"""Worker tick: command assembly + state-row lookup precedence."""

import datetime as dt
import fcntl
import os
from pathlib import Path
from typing import Any

import pytest

from osmsg import _tick
from osmsg.db import connect, create_tables
from osmsg.db.schema import upsert_state
from osmsg.geofabrik import country_update_url
from osmsg.replication import SHORTCUTS


@pytest.fixture
def captured_cmd(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_call(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return 0

    monkeypatch.setattr(_tick.subprocess, "call", fake_call)
    return captured


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("OSMSG_EXTRA_ARGS", "OSMSG_BOOTSTRAP", "OSMSG_BOOTSTRAP_DAYS"):
        monkeypatch.delenv(key, raising=False)


def _seed_state(out_dir: Path, name: str, source_url: str) -> None:
    conn = connect(str(out_dir / f"{name}.duckdb"))
    try:
        create_tables(conn)
        ts = dt.datetime(2026, 5, 21, 7, 0, tzinfo=dt.UTC)
        upsert_state(conn, source_url=source_url, last_seq=100, last_ts=ts, updated_at=ts)
    finally:
        conn.close()


def test_explicit_url_with_country_resolves_state_under_explicit_url(tmp_path, monkeypatch, captured_cmd, clean_env):
    """--country + explicit --url: the state row is keyed by the explicit URL, not the country's geofabrik
    URL (previously never found there, re-bootstrapping and wiping the DuckDB every tick).
    """
    name = "nepal"
    _seed_state(tmp_path, name, SHORTCUTS["minute"])

    monkeypatch.setenv(
        "OSMSG_EXTRA_ARGS",
        f"--name {name} --output-dir {tmp_path} --country nepal --url minute",
    )

    assert _tick.main() == 0
    assert "--update" in captured_cmd["cmd"], (
        f"expected --update to be appended when state exists for the explicit URL; got {captured_cmd['cmd']}"
    )
    assert "--last" not in captured_cmd["cmd"]


def test_country_only_resolves_state_under_geofabrik_url(tmp_path, monkeypatch, captured_cmd, clean_env):
    """--country alone: state is keyed by geofabrik (pipeline derives URL from country)."""
    name = "nepal"
    _seed_state(tmp_path, name, country_update_url("nepal"))

    monkeypatch.setenv(
        "OSMSG_EXTRA_ARGS",
        f"--name {name} --output-dir {tmp_path} --country nepal",
    )

    assert _tick.main() == 0
    assert "--update" in captured_cmd["cmd"]


def test_cold_start_bootstraps_at_day(tmp_path, monkeypatch, captured_cmd, clean_env):
    """First tick (no state row) cold-starts at day granularity, not --update; --update then refines."""
    name = "stats"
    monkeypatch.setenv("OSMSG_EXTRA_ARGS", f"--name {name} --output-dir {tmp_path}")

    assert _tick.main() == 0
    cmd = captured_cmd["cmd"]
    assert "--update" not in cmd
    assert "--url" in cmd and cmd[cmd.index("--url") + 1] == "day"
    assert cmd[-2:] == ["--days", "1"]  # default cold-start window


def test_tick_watchdog_returns_nonzero_when_run_times_out(tmp_path, monkeypatch, clean_env):
    def slow_call(cmd, *args, **kwargs):
        raise _tick.subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(_tick.subprocess, "call", slow_call)
    monkeypatch.setenv("OSMSG_EXTRA_ARGS", f"--name stats --output-dir {tmp_path}")
    assert _tick.main() == 1


def test_tick_timeout_tolerates_empty_env_var(monkeypatch):
    """compose passes the var as an empty string when unset, so the timeout must fall back, not crash on
    int('')."""
    import importlib

    monkeypatch.setenv("OSMSG_TICK_TIMEOUT_SECONDS", "")
    try:
        importlib.reload(_tick)
        assert _tick._TICK_TIMEOUT_SECONDS == 1200
    finally:
        monkeypatch.delenv("OSMSG_TICK_TIMEOUT_SECONDS", raising=False)
        importlib.reload(_tick)


def test_planet_continues_seeded_source(tmp_path, monkeypatch, captured_cmd, clean_env):
    """A `--insert --seed-only` seeds the store's resume source; the planet tick must --update off that
    seed, not re-bootstrap."""
    name = "stats"
    _seed_state(tmp_path, name, SHORTCUTS["day"])
    monkeypatch.setenv("OSMSG_EXTRA_ARGS", f"--name {name} --output-dir {tmp_path}")

    assert _tick.main() == 0
    assert "--update" in captured_cmd["cmd"]
    assert "--days" not in captured_cmd["cmd"]


def test_bootstrap_days_sets_cold_start_window(tmp_path, monkeypatch, captured_cmd, clean_env):
    name = "stats"
    monkeypatch.setenv("OSMSG_EXTRA_ARGS", f"--name {name} --output-dir {tmp_path}")
    monkeypatch.setenv("OSMSG_BOOTSTRAP_DAYS", "3")

    assert _tick.main() == 0
    cmd = captured_cmd["cmd"]
    assert "--url" in cmd and cmd[cmd.index("--url") + 1] == "day"
    assert cmd[-2:] == ["--days", "3"]


def test_tick_lifecycle_cold_then_warm(tmp_path, monkeypatch, clean_env):
    """Cold tick bootstraps; the next tick (after the state row lands under the planet/minute URL) must
    switch to --update instead of re-bootstrapping under the geofabrik URL forever."""
    calls: list[list[str]] = []

    def fake_call(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(_tick.subprocess, "call", fake_call)

    name = "nepal"
    monkeypatch.setenv(
        "OSMSG_EXTRA_ARGS",
        f"--name {name} --output-dir {tmp_path} --country nepal --url minute",
    )

    assert _tick.main() == 0
    assert calls[0][-2:] == ["--days", "1"]  # cold start (explicit --url, so no day override forced)
    assert "--update" not in calls[0]

    _seed_state(tmp_path, name, SHORTCUTS["minute"])

    assert _tick.main() == 0
    assert "--update" in calls[1]
    assert "--days" not in calls[1]


def test_tick_skips_when_previous_tick_holds_lock(tmp_path, monkeypatch, clean_env):
    """Concurrent-tick guard: flock is held → exit 0 immediately, never invoke subprocess."""
    name = "nepal"
    monkeypatch.setenv("OSMSG_EXTRA_ARGS", f"--name {name} --output-dir {tmp_path}")

    call_count = 0

    def fake_call(cmd, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return 0

    monkeypatch.setattr(_tick.subprocess, "call", fake_call)

    lock_path = tmp_path / f"{name}.lock"
    holder = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert _tick.main() == 0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert call_count == 0


def test_reset_store_buffer_clears_data_keeps_state(tmp_path):
    """After a psql push the store keeps only its resume `state`; data tables are emptied so the next
    push stays a small delta instead of the whole accumulated store."""
    db_path = tmp_path / "stats.duckdb"
    conn = connect(str(db_path))
    create_tables(conn)
    conn.execute("INSERT INTO users VALUES (1, 'alice')")
    conn.execute("INSERT INTO changesets (changeset_id, uid, created_at, hashtags) VALUES (7, 1, '2026-08-01', ['#x'])")
    upsert_state(
        conn,
        source_url=SHORTCUTS["minute"],
        last_seq=123,
        last_ts=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
    )
    conn.close()

    _tick._reset_store_buffer(db_path)

    conn = connect(str(db_path))
    assert conn.execute("SELECT count(*) FROM changesets").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM changeset_stats").fetchone()[0] == 0
    row = conn.execute("SELECT source_url, last_seq FROM state").fetchone()
    assert row == (SHORTCUTS["minute"], 123)
    conn.close()


def test_store_is_dirty_detects_leftover_data(tmp_path):
    db_path = tmp_path / "stats.duckdb"
    conn = connect(str(db_path))
    create_tables(conn)
    assert _tick._store_is_dirty(db_path) is False
    conn.execute("INSERT INTO changeset_stats (changeset_id, seq_id, uid) VALUES (1, 5, 9)")
    conn.close()
    assert _tick._store_is_dirty(db_path) is True


def test_store_is_dirty_false_when_store_missing(tmp_path):
    assert _tick._store_is_dirty(tmp_path / "absent.duckdb") is False


def test_rebuild_store_from_pg_empties_data_and_reseeds_state(tmp_path, monkeypatch):
    """A dirty store is discarded and rebuilt: data tables empty, resume state taken from Postgres (not
    the store's own stale, ahead-of-PG state), so --update resumes from the last durably pushed position."""
    db_path = tmp_path / "stats.duckdb"
    conn = connect(str(db_path))
    create_tables(conn)
    conn.execute("INSERT INTO changeset_stats (changeset_id, seq_id, uid) VALUES (1, 7, 9)")
    upsert_state(
        conn,
        source_url=SHORTCUTS["minute"],
        last_seq=999,
        last_ts=dt.datetime(2026, 8, 1, 9, 0, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 8, 1, 9, 0, tzinfo=dt.UTC),
    )
    conn.close()

    ts = dt.datetime(2026, 8, 1, 8, 0, tzinfo=dt.UTC)
    pg_state = [(SHORTCUTS["minute"], 42, ts, ts)]
    monkeypatch.setattr(_tick, "_read_pg_state", lambda dsn: pg_state)
    _tick._rebuild_store_from_pg(db_path, "postgresql://ignored")

    conn = connect(str(db_path))
    assert conn.execute("SELECT count(*) FROM changeset_stats").fetchone()[0] == 0
    assert conn.execute("SELECT source_url, last_seq FROM state").fetchone() == (SHORTCUTS["minute"], 42)
    conn.close()

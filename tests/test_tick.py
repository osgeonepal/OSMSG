"""Worker tick: command assembly + state-row lookup precedence."""

from __future__ import annotations

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
    """--country + explicit --url: state row is keyed by the explicit URL (pipeline rule).

    Regression guard: previously _tick looked up state under the country's geofabrik URL,
    never found it, and re-bootstrapped every tick (wiping the DuckDB each time).
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
    """Cold tick bootstraps; the next tick (after state lands) must switch to --update.

    End-to-end guard for the bug: tick 0 bootstraps, the pipeline writes a state row
    under the planet/minute URL, tick 1 must find that row instead of looking under
    the geofabrik URL and re-bootstrapping forever.
    """
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

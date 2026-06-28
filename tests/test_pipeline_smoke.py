"""Smoke tests for the orchestration glue (no network)."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest
from platformdirs import user_cache_dir

from osmsg.db.schema import create_tables, upsert_state
from osmsg.exceptions import OsmsgError
from osmsg.pipeline import (
    RunConfig,
    _auto_switch_replication,
    _bootstrap_window_start,
    _canonical_hashtags,
    _needs_changefile_changeset_filter,
    _normalize_urls,
    _pick_replication_for_span,
    _resolve_url_starts,
)
from osmsg.replication import CHANGESETS_REPLICATION, SHORTCUTS, ChangesetReplication

PLANET_MINUTE = "https://planet.openstreetmap.org/replication/minute"


def test_normalize_urls_expands_minute_shortcut():
    cfg = RunConfig(urls=["minute"])
    _normalize_urls(cfg)
    assert cfg.urls == ["https://planet.openstreetmap.org/replication/minute"]


def test_normalize_urls_strips_trailing_slash():
    cfg = RunConfig(urls=["https://example.com/path/"])
    _normalize_urls(cfg)
    assert cfg.urls == ["https://example.com/path"]


def test_normalize_urls_dedupes():
    cfg = RunConfig(urls=["minute", "minute"])
    _normalize_urls(cfg)
    assert len(cfg.urls) == 1


def test_normalize_urls_preserves_order():
    """Order matters: cfg.urls[0] used to be the implicit resume key. Set comprehension was non-deterministic."""
    cfg = RunConfig(urls=["https://example.com/zebra", "https://example.com/alpha", "https://example.com/zebra"])
    _normalize_urls(cfg)
    assert cfg.urls == ["https://example.com/zebra", "https://example.com/alpha"]


def test_normalize_urls_country_alone_uses_geofabrik():
    """--country with no explicit --url falls through to the Geofabrik country feed."""
    cfg = RunConfig(countries=["nepal"], urls=["minute"], url_explicit=False)
    _normalize_urls(cfg)
    assert cfg.urls == ["https://download.geofabrik.de/asia/nepal-updates"]


def test_normalize_urls_explicit_url_overrides_country():
    """--url is explicit user intent; it must beat --country's default Geofabrik URL.
    --country still applies the boundary geometry filter downstream (handled elsewhere)."""
    cfg = RunConfig(countries=["nepal"], urls=["minute"], url_explicit=True)
    _normalize_urls(cfg)
    assert cfg.urls == ["https://planet.openstreetmap.org/replication/minute"]


def test_run_config_defaults_to_parquet():
    cfg = RunConfig()
    assert cfg.formats == ["parquet"]
    # Library callers get the same per-user cache dir as the CLI, not a CWD-relative path.
    assert str(cfg.cache_dir) == user_cache_dir("osmsg")


def test_canonical_hashtags_normalizes_prefix():
    """Both 'hotosm' and '#hotosm' must canonicalize to the same '#hotosm' form (regression N1)."""
    assert _canonical_hashtags(["hotosm"]) == ["#hotosm"]
    assert _canonical_hashtags(["#hotosm"]) == ["#hotosm"]
    assert _canonical_hashtags(["##hotosm"]) == ["#hotosm"]
    assert _canonical_hashtags(["mapathon", "#hotosm-project-1"]) == ["#mapathon", "#hotosm-project-1"]


def _open_db(tmp_path):
    conn = duckdb.connect(str(tmp_path / "t.duckdb"))
    create_tables(conn)
    return conn


def test_pipeline_state_last_ts_is_seq_aligned(tmp_path, monkeypatch):
    """state.last_ts is the seq_ts of last_seq so the next tick's lower-bound filter
    aligns with the seq boundary."""
    import datetime as _dt

    import osmsg.pipeline as pipeline_mod
    from osmsg.db.schema import get_state
    from osmsg.pipeline import RunConfig, run

    fake_server_ts = _dt.datetime(2026, 5, 1, 12, 0, tzinfo=_dt.UTC)
    seq_ts = _dt.datetime(2026, 5, 1, 11, 35, tzinfo=_dt.UTC)  # explicitly different from cfg.end_date

    def fake_changefile_download_urls(_start, _end, _base, *, resume_seq=None, cs_ts=None):
        return (["https://fake/101.osc.gz"], fake_server_ts, 101, 101, "u1", "u2")

    monkeypatch.setattr(pipeline_mod, "changefile_download_urls", fake_changefile_download_urls)
    monkeypatch.setattr(pipeline_mod, "_download_all", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_mod, "_process_all", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_mod.dbmod, "merge_parquet_files", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_mod, "changefile_seq_timestamp", lambda _base, _seq: seq_ts)
    monkeypatch.setattr(
        pipeline_mod, "user_stats", lambda *a, **kw: [{"uid": 1, "name": "x", "map_changes": 1, "rank": 1}]
    )
    monkeypatch.setattr(pipeline_mod, "to_parquet", lambda rows, path: path)

    cfg = RunConfig(
        name="t",
        urls=["https://fake-source"],
        url_explicit=True,
        start_date=_dt.datetime(2026, 5, 1, 11, 0, tzinfo=_dt.UTC),
        end_date=_dt.datetime(2026, 5, 1, 11, 30, tzinfo=_dt.UTC),  # explicit end != seq_ts
        output_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        formats=["parquet"],
        changeset=False,
        tag_mode="none",
        history_mode="off",  # this test exercises the live state-alignment path only
    )
    run(cfg)

    conn = duckdb.connect(str(tmp_path / "t.duckdb"))
    state = get_state(conn, "https://fake-source")
    conn.close()
    assert state is not None
    state_last_ts = state["last_ts"]
    if state_last_ts.tzinfo is None:
        state_last_ts = state_last_ts.replace(tzinfo=dt.UTC)
    else:
        state_last_ts = state_last_ts.astimezone(dt.UTC)
    assert state_last_ts == seq_ts, (
        f"state.last_ts must be seq_ts of last_seq ({seq_ts}); got {state_last_ts}. "
        "If it equals cfg.end_date instead, the next tick's lower-bound filter is mis-aligned "
        "with the seq boundary and edits in the boundary diff are silently lost."
    )


def test_resolve_url_starts_no_update_uses_cfg_start(tmp_path):
    conn = _open_db(tmp_path)
    start = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)
    cfg = RunConfig(urls=["https://x", "https://y"], start_date=start)
    starts = _resolve_url_starts(conn, cfg)
    assert starts == {"https://x": (start, None), "https://y": (start, None)}


def test_resolve_url_starts_no_update_no_start_raises(tmp_path):
    conn = _open_db(tmp_path)
    cfg = RunConfig(urls=["https://x"])
    with pytest.raises(OsmsgError, match="start_date is required"):
        _resolve_url_starts(conn, cfg)


def test_resolve_url_starts_update_reads_each_url_state_row(tmp_path):
    """Multi-URL --update must derive each URL's start from its own state row (regression N2/T1)."""
    conn = _open_db(tmp_path)
    ts_x = dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.UTC)
    ts_y = dt.datetime(2026, 4, 26, 9, 30, tzinfo=dt.UTC)
    upsert_state(conn, source_url="https://x", last_seq=1, last_ts=ts_x, updated_at=ts_x)
    upsert_state(conn, source_url="https://y", last_seq=2, last_ts=ts_y, updated_at=ts_y)
    cfg = RunConfig(urls=["https://x", "https://y"], update=True)
    starts = _resolve_url_starts(conn, cfg)
    assert starts == {"https://x": (ts_x, 2), "https://y": (ts_y, 3)}


def test_resolve_url_starts_update_resume_seq_is_last_seq_plus_one(tmp_path):
    """--update must resume at last_seq + 1, no overlap, no gap, no backward pad."""
    conn = _open_db(tmp_path)
    ts = dt.datetime(2026, 5, 1, tzinfo=dt.UTC)
    upsert_state(conn, source_url="https://planet", last_seq=12345, last_ts=ts, updated_at=ts)
    cfg = RunConfig(urls=["https://planet"], update=True)
    starts = _resolve_url_starts(conn, cfg)
    _ts, resume_seq = starts["https://planet"]
    assert resume_seq == 12346


def test_resolve_url_starts_update_missing_state_raises_per_url(tmp_path):
    """Partial state must raise: auto-bootstrap is for empty DBs only, never for a mix."""
    conn = _open_db(tmp_path)
    ts = dt.datetime(2026, 4, 25, tzinfo=dt.UTC)
    upsert_state(conn, source_url="https://x", last_seq=1, last_ts=ts, updated_at=ts)
    cfg = RunConfig(urls=["https://x", "https://y"], update=True)
    with pytest.raises(OsmsgError, match="no prior state for https://y"):
        _resolve_url_starts(conn, cfg)


def test_resolve_url_starts_update_different_url_raises_with_recovery_hint(tmp_path):
    """State for a different URL must raise: switching granularity would double-count
    via (seq_id, changeset_id). The error message names the known URL so the user can recover."""
    conn = _open_db(tmp_path)
    ts = dt.datetime(2026, 4, 25, tzinfo=dt.UTC)
    upsert_state(conn, source_url=PLANET_MINUTE, last_seq=1, last_ts=ts, updated_at=ts)
    cfg = RunConfig(urls=["https://planet.openstreetmap.org/replication/day"], update=True)
    with pytest.raises(OsmsgError) as exc:
        _resolve_url_starts(conn, cfg)
    msg = str(exc.value)
    for fragment in ("Existing state in this DuckDB is for", "minute", "different --name", "seq_id"):
        assert fragment in msg


def test_resolve_url_starts_update_empty_db_bootstraps_one_hour(tmp_path, capsys):
    """Fresh DB plus --update auto-seeds start = now - 1h instead of erroring."""
    conn = _open_db(tmp_path)
    cfg = RunConfig(urls=[PLANET_MINUTE], update=True)
    before = dt.datetime.now(dt.UTC)
    starts = _resolve_url_starts(conn, cfg)
    after = dt.datetime.now(dt.UTC)

    ts, resume_seq = starts[PLANET_MINUTE]
    assert resume_seq is None
    assert before - dt.timedelta(hours=1, seconds=1) <= ts <= after - dt.timedelta(hours=1) + dt.timedelta(seconds=1)
    assert "no prior state" in capsys.readouterr().out


def test_bootstrap_honors_osmsg_bootstrap_env(tmp_path, monkeypatch):
    """OSMSG_BOOTSTRAP=day shifts the auto-bootstrap window from 1h to 24h."""
    monkeypatch.setenv("OSMSG_BOOTSTRAP", "day")
    conn = _open_db(tmp_path)
    starts = _resolve_url_starts(conn, RunConfig(urls=[PLANET_MINUTE], update=True))
    age = dt.datetime.now(dt.UTC) - starts[PLANET_MINUTE][0]
    assert dt.timedelta(hours=23, minutes=59) < age < dt.timedelta(hours=24, minutes=1)


def test_bootstrap_honors_osmsg_bootstrap_days_env(tmp_path, monkeypatch):
    """OSMSG_BOOTSTRAP_DAYS=N wins over the preset."""
    monkeypatch.setenv("OSMSG_BOOTSTRAP_DAYS", "3")
    monkeypatch.setenv("OSMSG_BOOTSTRAP", "hour")
    conn = _open_db(tmp_path)
    starts = _resolve_url_starts(conn, RunConfig(urls=[PLANET_MINUTE], update=True))
    age = dt.datetime.now(dt.UTC) - starts[PLANET_MINUTE][0]
    assert dt.timedelta(days=2, hours=23) < age < dt.timedelta(days=3, hours=1)


def test_bootstrap_ignores_changeset_replication_state_row(tmp_path):
    """The changesets-replication state row is internal bookkeeping and must not block
    the bootstrap path: we only refuse fresh starts when *user-facing* state exists."""
    conn = _open_db(tmp_path)
    ts = dt.datetime(2026, 4, 25, tzinfo=dt.UTC)
    upsert_state(conn, source_url=CHANGESETS_REPLICATION, last_seq=999_999, last_ts=ts, updated_at=ts)
    starts = _resolve_url_starts(conn, RunConfig(urls=[PLANET_MINUTE], update=True))
    ts, resume_seq = starts[PLANET_MINUTE]
    assert resume_seq is None
    assert dt.datetime.now(dt.UTC) - ts < dt.timedelta(hours=2)


def test_bootstrap_window_start_default_is_one_hour():
    now = dt.datetime(2026, 5, 8, 12, 0, tzinfo=dt.UTC)
    assert _bootstrap_window_start(now) == now - dt.timedelta(hours=1)


def test_runconfig_default_changeset_pad_is_one_hour():
    assert RunConfig().changeset_pad_hours == 1


def test_runconfig_changeset_pad_hours_round_trips_to_replication():
    """The CLI/env value must reach ChangesetReplication unchanged."""
    cfg = RunConfig(changeset_pad_hours=24)
    assert ChangesetReplication(pad_hours=cfg.changeset_pad_hours).pad_min == 24 * 60


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"hashtags": ["#hotosm"]}, True),
        ({"boundary": "/tmp/x.geojson"}, True),
        ({"countries": ["nepal"]}, True),
        ({"countries": ["nepal", "india"]}, True),
        ({}, False),
        ({"changeset": True}, False),
    ],
)
def test_needs_changefile_changeset_filter(kwargs, expected):
    """Predicate-level guard: hashtags OR boundary OR countries must trigger the metadata
    allowlist. Omitting countries silently leaks global changefile stubs into a country DB."""
    assert _needs_changefile_changeset_filter(RunConfig(**kwargs)) is expected


@pytest.mark.parametrize(
    "span,expected",
    [
        (dt.timedelta(hours=1), "minute"),
        (dt.timedelta(hours=5, minutes=59), "minute"),
        (dt.timedelta(hours=6), "hour"),  # boundary: ≥6h flips to hour
        (dt.timedelta(days=1), "hour"),
        (dt.timedelta(days=6, hours=23), "hour"),
        (dt.timedelta(days=7), "day"),  # boundary: ≥7d flips to day
        (dt.timedelta(days=30), "day"),
    ],
)
def test_pick_replication_for_span(span, expected):
    assert _pick_replication_for_span(span) == expected


def test_auto_switch_promotes_minute_to_hour_on_long_span(capsys):
    cfg = RunConfig(urls=[SHORTCUTS["minute"]])
    _auto_switch_replication(cfg, dt.timedelta(hours=10))
    assert cfg.urls == [SHORTCUTS["hour"]]
    err = capsys.readouterr().err
    assert "auto-switching" in err
    assert "from 'minute' to 'hour'" in err


def test_auto_switch_promotes_minute_to_day_on_multi_day_span():
    cfg = RunConfig(urls=[SHORTCUTS["minute"]])
    _auto_switch_replication(cfg, dt.timedelta(days=30))
    assert cfg.urls == [SHORTCUTS["day"]]


def test_auto_switch_demotes_day_to_minute_on_short_span():
    """A user defaulting to day for a 1h window should be moved back to minute too."""
    cfg = RunConfig(urls=[SHORTCUTS["day"]])
    _auto_switch_replication(cfg, dt.timedelta(hours=1))
    assert cfg.urls == [SHORTCUTS["minute"]]


def test_auto_switch_no_op_when_already_correct(capsys):
    cfg = RunConfig(urls=[SHORTCUTS["hour"]])
    _auto_switch_replication(cfg, dt.timedelta(hours=10))
    assert cfg.urls == [SHORTCUTS["hour"]]
    assert "auto-switching" not in capsys.readouterr().err


def test_auto_switch_suppressed_by_url_explicit():
    cfg = RunConfig(urls=[SHORTCUTS["minute"]], url_explicit=True)
    _auto_switch_replication(cfg, dt.timedelta(days=30))
    assert cfg.urls == [SHORTCUTS["minute"]]


def test_auto_switch_suppressed_by_update():
    """--update must never auto-switch, cross-URL replay would double-count via (seq_id, changeset_id)."""
    cfg = RunConfig(urls=[SHORTCUTS["minute"]], update=True)
    _auto_switch_replication(cfg, dt.timedelta(days=30))
    assert cfg.urls == [SHORTCUTS["minute"]]


def test_auto_switch_suppressed_by_country():
    cfg = RunConfig(urls=[SHORTCUTS["minute"]], countries=["nepal"])
    _auto_switch_replication(cfg, dt.timedelta(days=30))
    assert cfg.urls == [SHORTCUTS["minute"]]


def test_auto_switch_suppressed_by_multi_url():
    urls = [SHORTCUTS["minute"], SHORTCUTS["hour"]]
    cfg = RunConfig(urls=list(urls))
    _auto_switch_replication(cfg, dt.timedelta(days=30))
    assert cfg.urls == urls


def test_auto_switch_skips_non_shortcut_url():
    """A custom (e.g. Geofabrik) URL must not be silently swapped for a planet shortcut."""
    custom = "https://download.geofabrik.de/asia/nepal-updates"
    cfg = RunConfig(urls=[custom])
    _auto_switch_replication(cfg, dt.timedelta(days=30))
    assert cfg.urls == [custom]

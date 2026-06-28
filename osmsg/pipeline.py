"""End-to-end orchestration: download → process → ingest → query → export."""

from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import requests
from platformdirs import user_cache_dir
from shapely.ops import unary_union

from . import db as dbmod
from . import tm
from .__version__ import __version__
from .auth import get_geofabrik_cookie
from .boundary import load_boundary
from .db.queries import attach_metadata, attach_tag_stats, daily_summary, list_changesets, user_stats
from .db.schema import get_state, upsert_state
from .exceptions import CredentialsRequiredError, NoDataFoundError, OsmsgError
from .export import summary_markdown, table_markdown, to_csv, to_json, to_parquet, to_psql
from .fetch import download_osm_file
from .geofabrik import country_geometry, country_update_url
from .history import (
    RESUME_SAFETY,
    RemoteFilters,
    WindowSplit,
    fetch_manifest,
    ingest_remote,
    seed_resume_at,
    seed_resume_state,
    split_window,
)
from .replication import (
    CHANGESETS_REPLICATION,
    SHORTCUTS,
    ChangesetReplication,
    changefile_download_urls,
    changefile_seq_timestamp,
    resolve_url,
)
from .ui import info, progress_bar, warn

UTC = dt.UTC


def _default_cache_dir() -> Path:
    return Path(user_cache_dir("osmsg"))


def _cpu_count() -> int:
    # sched_getaffinity is cgroup-aware (matters in containers); not present on macOS/Windows.
    sched = getattr(os, "sched_getaffinity", None)
    if sched is not None:
        return len(sched(0))
    return os.cpu_count() or 4


@dataclass
class RunConfig:
    name: str = "stats"
    start_date: dt.datetime | None = None
    end_date: dt.datetime | None = None
    countries: list[str] | None = None
    urls: list[str] = field(default_factory=lambda: ["https://planet.openstreetmap.org/replication/minute"])
    url_explicit: bool = False
    workers: int | None = None
    additional_tags: list[str] | None = None
    hashtags: list[str] | None = None
    length_tags: list[str] | None = None
    users_filter: list[str] | None = None
    tag_mode: str = "none"
    exact_lookup: bool = False
    changeset: bool = False
    summary: bool = False
    boundary: str | None = None
    tm_stats: bool = False
    formats: list[str] = field(default_factory=lambda: ["parquet"])
    update: bool = False
    delete_temp: bool = False
    cache_dir: Path = field(default_factory=_default_cache_dir)
    output_dir: Path = field(default_factory=lambda: Path("."))
    osm_username: str | None = None
    osm_password: str | None = None
    psql_dsn: str | None = None
    psql_bulk: bool = False
    changeset_pad_hours: int = ChangesetReplication.DEFAULT_PAD_HOURS
    history_mode: str = "auto"  # auto | off
    history_url: str = "hf://datasets/kshitijrajsharma/osmsg-history"
    insert: bool = False
    osh_file: str | None = None
    changeset_file: str | None = None
    overwrite: bool = False


def _resolve_country_urls(countries: list[str]) -> list[str]:
    return [country_update_url(region) for region in countries]


def _normalize_urls(cfg: RunConfig) -> None:
    # Explicit --url wins over --country's default Geofabrik URL; --country still
    # contributes the boundary geometry filter downstream.
    if cfg.countries and not cfg.url_explicit:
        cfg.urls = _resolve_country_urls(cfg.countries)
        return
    # Order-preserving dedupe: cfg.urls[0] is load-bearing for resume.
    cfg.urls = list(dict.fromkeys(resolve_url(u) for u in cfg.urls))


def _pick_replication_for_span(span: dt.timedelta) -> str:
    span_h = span.total_seconds() / 3600
    if span_h < 6:
        return "minute"
    if span_h < 24 * 7:
        return "hour"
    return "day"


def _auto_switch_replication(cfg: RunConfig, span: dt.timedelta) -> None:
    """Swap a single planet-shortcut --url for the cheapest one that covers `span`."""
    if cfg.url_explicit or cfg.update or cfg.countries or len(cfg.urls) != 1:
        return
    cur = cfg.urls[0]
    if cur not in SHORTCUTS.values():
        return
    target_label = _pick_replication_for_span(span)
    target_url = SHORTCUTS[target_label]
    if target_url == cur:
        return
    cur_label = next(label for label, url in SHORTCUTS.items() if url == cur)
    warn(
        f"Span is {span}; auto-switching --url from '{cur_label}' to '{target_label}' to reduce load. "
        f"Pass --url {cur_label} to keep '{cur_label}'."
    )
    cfg.urls = [target_url]


def _canonical_hashtags(hashtags: list[str]) -> list[str]:
    # Force leading '#' so 'hotosm' and '#hotosm' both match the '#hotosm' tokens in changeset comments.
    return ["#" + h.lstrip("#") for h in hashtags]


def _needs_changefile_changeset_filter(cfg: RunConfig) -> bool:
    # When any metadata-side filter is on, ChangefileHandler must drop edits whose
    # changeset_id isn't in the allowlist; otherwise stub rows for global changesets
    # pollute the changesets table.
    return bool(cfg.hashtags or cfg.boundary or cfg.countries)


def _resolve_valid_changesets(conn, cfg: RunConfig) -> set[int] | None:
    # None means "no allowlist, keep everything"; a set means "drop edits to changesets
    # not in this set". The set is whatever ChangesetHandler already filtered into the
    # changesets table earlier in the run.
    if not _needs_changefile_changeset_filter(cfg):
        return None
    return set(list_changesets(conn))


_BOOTSTRAP_PRESETS = {
    "hour": dt.timedelta(hours=1),
    "day": dt.timedelta(days=1),
    "week": dt.timedelta(days=7),
}


def _bootstrap_window_start(now: dt.datetime | None = None) -> dt.datetime:
    """Resolve the auto-bootstrap start_date for a fresh --update.

    OSMSG_BOOTSTRAP_DAYS=N wins over OSMSG_BOOTSTRAP=hour|day|week. Defaults to one hour,
    matching the worker tick in osmsg/_tick.py.
    """
    now = now or dt.datetime.now(UTC)
    days_env = os.environ.get("OSMSG_BOOTSTRAP_DAYS")
    if days_env:
        return now - dt.timedelta(days=int(days_env))
    preset = os.environ.get("OSMSG_BOOTSTRAP", "hour")
    return now - _BOOTSTRAP_PRESETS.get(preset, _BOOTSTRAP_PRESETS["hour"])


def _resolve_url_starts(conn, cfg: RunConfig) -> dict[str, tuple[dt.datetime, int | None]]:
    """Per-URL (start_ts, resume_seq); resume_seq is set only on --update."""
    if cfg.update:
        if not cfg.urls:
            raise OsmsgError("--update requires at least one source URL.")

        all_known = [r[0] for r in conn.execute("SELECT source_url FROM state").fetchall()]
        known_user_sources = [u for u in all_known if u != CHANGESETS_REPLICATION]
        per_url_state = {url: get_state(conn, url) for url in cfg.urls}
        if not known_user_sources and all(s is None for s in per_url_state.values()):
            bootstrap_start = _bootstrap_window_start()
            info(
                f"--update: no prior state, bootstrapping from {bootstrap_start.isoformat()} "
                "(set OSMSG_BOOTSTRAP=hour|day|week or OSMSG_BOOTSTRAP_DAYS=N to change)."
            )
            return {url: (bootstrap_start, None) for url in cfg.urls}
        starts: dict[str, tuple[dt.datetime, int | None]] = {}
        for url, last in per_url_state.items():
            if not last:
                hint = (
                    f" Existing state in this DuckDB is for: {', '.join(known_user_sources)}. "
                    "Re-run --update with one of those URLs, or start fresh under a different --name."
                    if known_user_sources
                    else " Run osmsg once without --update to seed state."
                )
                raise OsmsgError(
                    f"--update cannot switch replication URL: no prior state for {url}.{hint} "
                    "(Replaying the same window through a different granularity would double-count "
                    "via the changeset_stats (seq_id, changeset_id) key.)"
                )
            starts[url] = (last["last_ts"], last["last_seq"] + 1)
        return starts
    if cfg.start_date is None:
        raise OsmsgError("start_date is required. Pass --start, --last, --days, or --update with a prior run.")
    return {url: (cfg.start_date, None) for url in cfg.urls}


def _seed_history_resume(conn, cfg: RunConfig) -> None:
    """On --update against a store loaded from the published history but with no resume state yet,
    seed each source's state at the published frontier so the first update resumes there instead of
    the default bootstrap window. Makes "load the history, then --update" just work."""
    if not cfg.update or cfg.history_mode != "auto":
        return
    history_rows = conn.execute("SELECT count(*) FROM changeset_stats WHERE seq_id = 0").fetchone()
    if not history_rows or not history_rows[0]:
        return
    for url in cfg.urls:
        if get_state(conn, url) is None:
            seed_resume_state(conn, cfg.history_url, url)


_GRANULARITY_RANK = {SHORTCUTS["minute"]: 0, SHORTCUTS["hour"]: 1, SHORTCUTS["day"]: 2}


def _tracked_sources(conn) -> list[str]:
    """Planet replication sources this store already tracks (excludes the changeset-metadata stream)."""
    return [r[0] for r in conn.execute("SELECT source_url FROM state").fetchall() if r[0] != CHANGESETS_REPLICATION]


def _switch_source(conn, from_url: str, to_url: str) -> None:
    """Resume to_url at from_url's last_ts and retire from_url, so the granularities never overlap or gap."""
    state = get_state(conn, from_url)
    if state is None:
        return
    boundary = state["last_ts"]
    if seed_resume_at(conn, boundary, to_url) is None:
        raise OsmsgError(f"cannot switch to {to_url}: no replication sequence resolves at {boundary.isoformat()}.")
    conn.execute("DELETE FROM state WHERE source_url = ?", [from_url])
    info(f"--update: handed off {from_url} -> {to_url} at {boundary.isoformat()}.")


def _select_update_source(conn, cfg: RunConfig, now: dt.datetime) -> None:
    """Pick the source `--update` continues: without `--url`, continue the tracked source and auto-refine
    to a finer granularity as the backlog shrinks; with `--url`, switch to it via a clean handoff."""
    tracked = _tracked_sources(conn)
    if not tracked:
        return
    if len(tracked) > 1:
        if not cfg.url_explicit:
            cfg.urls = tracked
        return
    current = tracked[0]
    if cfg.url_explicit:
        target = cfg.urls[0]
        if target != current:
            _switch_source(conn, current, target)
        cfg.urls = [target]
        return
    last = get_state(conn, current)
    assert last is not None
    target = resolve_url(_pick_replication_for_span(now - last["last_ts"]))
    if _GRANULARITY_RANK.get(target, 0) < _GRANULARITY_RANK.get(current, 0):
        _switch_source(conn, current, target)
        cfg.urls = [target]
    else:
        cfg.urls = [current]


def _history_live_start(split: WindowSplit, frontier: dt.datetime) -> dt.datetime:
    """Where the live tail begins after a remote ingest: back up by the safety window when the query
    reached the frontier (the final month may be short), else the split boundary."""
    if split.remote_end == frontier:
        return frontier - RESUME_SAFETY
    return split.live_start


def _resolve_geom_wkt(cfg: RunConfig) -> str | None:
    """The boundary/country filter as WKT, or None when no spatial filter is set."""
    if cfg.boundary:
        return load_boundary(cfg.boundary).wkt
    if cfg.countries:
        geoms = [country_geometry(region) for region in cfg.countries]
        return (unary_union(geoms) if len(geoms) > 1 else geoms[0]).wkt
    return None


def _convert_local(cfg: RunConfig) -> tuple[str, WindowSplit]:
    """Convert the local .osh + changeset dump to parquet under the cache dir and return its base path
    plus the window split that covers it."""
    from .maintain.convert import convert

    assert cfg.osh_file is not None and cfg.changeset_file is not None
    start = cfg.start_date or dt.datetime(2005, 1, 1, tzinfo=UTC)
    assert cfg.end_date is not None
    work = cfg.cache_dir / "insert_convert"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    out = convert(cfg.osh_file, cfg.changeset_file, start, cfg.end_date, work)
    return out.as_posix(), WindowSplit(remote_start=start, remote_end=cfg.end_date, live_start=cfg.end_date)


def _run_insert(cfg: RunConfig, conn: duckdb.DuckDBPyConnection, db_path: Path) -> dict[str, Any]:
    """Populate the store from history (published parquet or a local .osh), seed resume state so a later
    --update continues, and push to Postgres when a DSN is set. No live diffs, no leaderboard export."""
    filters = RemoteFilters(
        hashtags=cfg.hashtags,
        exact_lookup=cfg.exact_lookup,
        users_filter=cfg.users_filter,
        geom_wkt=_resolve_geom_wkt(cfg),
    )
    if cfg.osh_file:
        base, split = _convert_local(cfg)
    else:
        manifest = fetch_manifest(cfg.history_url)
        if manifest is None:
            raise OsmsgError(
                "history: dataset manifest unavailable, cannot --insert from the published dataset. "
                "Pass --osh-file/--changeset-file to insert from local files, or check --history-url."
            )
        assert cfg.end_date is not None
        split = split_window(cfg.start_date or manifest.min_month, cfg.end_date, manifest)
        base = cfg.history_url

    if not split.has_remote:
        dbmod.close(conn)
        raise NoDataFoundError("Insert window has no overlap with the available history.")
    assert split.remote_end is not None

    n = ingest_remote(conn, split, filters, base)
    resume_at = split.remote_end - RESUME_SAFETY
    if cfg.url_explicit:
        seed_urls = cfg.urls
    else:
        seed_urls = [resolve_url(_pick_replication_for_span(dt.datetime.now(UTC) - split.remote_end))]
    for url in seed_urls:
        seed_resume_at(conn, resume_at, url)
    info(f"insert: {n:,} history changeset rows; resume seeded at {resume_at.astimezone(UTC).isoformat()}.")

    written: dict[str, str] = {"duckdb": str(db_path)}
    if cfg.psql_dsn:
        info(f"Pushing to PostgreSQL: {cfg.psql_dsn.split()[0]}…")
        to_psql(conn, cfg.psql_dsn, bulk_load=True)
        written["psql"] = cfg.psql_dsn
    dbmod.close(conn)
    return {"rows": n, "files": written, "rows_data": [], "summary": None, "start_seq": None, "end_seq": None}


def _query_fingerprint(cfg: RunConfig) -> str:
    """Stable hash of the query's data-affecting params, excluding output formats."""
    key = {
        "start": cfg.start_date.isoformat() if cfg.start_date else None,
        "end": cfg.end_date.isoformat() if cfg.end_date else None,
        "urls": sorted(cfg.urls),
        "countries": sorted(cfg.countries) if cfg.countries else None,
        "boundary": cfg.boundary,
        "hashtags": sorted(cfg.hashtags) if cfg.hashtags else None,
        "exact_lookup": cfg.exact_lookup,
        "users": sorted(cfg.users_filter) if cfg.users_filter else None,
        "tag_mode": cfg.tag_mode,
        "additional_tags": sorted(cfg.additional_tags) if cfg.additional_tags else None,
        "length_tags": sorted(cfg.length_tags) if cfg.length_tags else None,
        "changeset": cfg.changeset,
        "summary": cfg.summary,
        "tm_stats": cfg.tm_stats,
        "history_mode": cfg.history_mode,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def _read_fingerprint(conn: duckdb.DuckDBPyConnection) -> str | None:
    """The query fingerprint stamped on an existing store, or None if absent."""
    present = conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'osmsg_run_meta'").fetchone()
    if not present:
        return None
    row = conn.execute("SELECT fingerprint FROM osmsg_run_meta LIMIT 1").fetchone()
    return row[0] if row else None


def _store_fingerprint(conn: duckdb.DuckDBPyConnection, fingerprint: str) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS osmsg_run_meta (fingerprint VARCHAR)")
    conn.execute("DELETE FROM osmsg_run_meta")
    conn.execute("INSERT INTO osmsg_run_meta VALUES (?)", [fingerprint])


def _finalize(
    cfg: RunConfig,
    conn: duckdb.DuckDBPyConnection,
    fingerprint: str,
    *,
    start_date_utc: dt.datetime,
    end_date_utc: dt.datetime,
    start_seq: int | None,
    end_seq: int | None,
) -> dict[str, Any]:
    """Aggregate the populated tables into user stats and write the requested formats."""
    rows = user_stats(conn, top_n=None)
    if not rows:
        dbmod.close(conn)
        # Raised so the CLI can map "no new data" to exit 0.
        raise NoDataFoundError("No stats produced for the requested time range.")
    _store_fingerprint(conn, fingerprint)

    if cfg.changeset or cfg.hashtags:
        attach_metadata(conn, rows)
    if cfg.additional_tags or cfg.tag_mode != "none" or cfg.length_tags:
        attach_tag_stats(
            conn,
            rows,
            additional_tags=cfg.additional_tags,
            tag_mode=cfg.tag_mode,
            length_tags=cfg.length_tags,
        )
    if cfg.tm_stats:
        rows = tm.enrich(rows)

    out = cfg.output_dir
    written: dict[str, str] = {}
    if "parquet" in cfg.formats:
        written["parquet"] = str(to_parquet(rows, out / f"{cfg.name}.parquet"))
    if "csv" in cfg.formats:
        written["csv"] = str(to_csv(rows, out / f"{cfg.name}.csv"))
    if "json" in cfg.formats:
        written["json"] = str(to_json(rows, out / f"{cfg.name}.json"))
    if "markdown" in cfg.formats:
        md_path = out / f"{cfg.name}.md"
        table_markdown(rows, output_path=md_path)
        written["markdown"] = str(md_path)

    summary_rows: list[dict[str, Any]] | None = None
    if cfg.summary:
        summary_rows = daily_summary(
            conn,
            additional_tags=cfg.additional_tags,
            tag_mode=cfg.tag_mode,
            length_tags=cfg.length_tags,
        )
    if summary_rows:
        if "parquet" in cfg.formats:
            written["summary_parquet"] = str(to_parquet(summary_rows, out / f"{cfg.name}_summary.parquet"))
        if "csv" in cfg.formats:
            written["summary_csv"] = str(to_csv(summary_rows, out / f"{cfg.name}_summary.csv"))
        if "json" in cfg.formats:
            written["summary_json"] = str(to_json(summary_rows, out / f"{cfg.name}_summary.json"))
        if "markdown" in cfg.formats:
            summary_md_path = out / f"{cfg.name}_summary.md"
            summary_markdown(
                rows,
                output_path=summary_md_path,
                start_date=start_date_utc,
                end_date=end_date_utc,
                additional_tags=cfg.additional_tags,
                length_tags=cfg.length_tags,
                tag_mode=cfg.tag_mode,
                fname=cfg.name,
                tm_stats=cfg.tm_stats,
            )
            written["summary_md"] = str(summary_md_path)
        # psql: skipped on purpose, daily_summary is a query over the four base tables.

    if "psql" in cfg.formats:
        if not cfg.psql_dsn:
            raise OsmsgError("'psql' format requires a libpq DSN (--psql-dsn / RunConfig.psql_dsn=...).")
        info(f"Pushing to PostgreSQL: {cfg.psql_dsn.split()[0]}…")
        to_psql(conn, cfg.psql_dsn, bulk_load=cfg.psql_bulk)
        written["psql"] = cfg.psql_dsn

    dbmod.close(conn)
    return {
        "rows": len(rows),
        "files": written,
        "rows_data": rows,
        "summary": summary_rows,
        "start_seq": start_seq,
        "end_seq": end_seq,
    }


def _ensure_credentials(cfg: RunConfig) -> str | None:
    """Resolve OSM credentials and exchange them for a Geofabrik OAuth 2.0 cookie.

    Resolution order: explicit `RunConfig` fields → `OSM_USERNAME` / `OSM_PASSWORD`
    env vars → interactive prompt (only if stdin is a TTY).

    Raises `CredentialsRequiredError` if a geofabrik URL is in use but no credentials
    can be obtained non-interactively (library users running headless).
    """
    if not any("geofabrik" in u.lower() for u in cfg.urls):
        return None

    user = cfg.osm_username or os.environ.get("OSM_USERNAME")
    pw = cfg.osm_password or os.environ.get("OSM_PASSWORD")

    if not user or not pw:
        import sys as _sys

        if not _sys.stdin.isatty():
            raise CredentialsRequiredError(
                "Geofabrik URLs need OSM credentials. Set OSM_USERNAME/OSM_PASSWORD or pass "
                "RunConfig(osm_username=…, osm_password=…)."
            )
        import getpass

        user = user or input("OSM username: ").strip()
        pw = pw or getpass.getpass("OSM password: ")

    info("Authenticating with OSM (OAuth 2.0)…")
    return get_geofabrik_cookie(user, pw)


def _processing_config(cfg: RunConfig, *, parquet_dir: Path, geom_wkt: str | None) -> dict[str, Any]:
    return {
        "hashtags": cfg.hashtags,
        "additional_tags": cfg.additional_tags,
        "tag_mode": cfg.tag_mode,
        "length": cfg.length_tags,
        "exact_lookup": cfg.exact_lookup,
        "changeset_meta": cfg.changeset,
        "whitelisted_users": cfg.users_filter or [],
        "geom_filter_wkt": geom_wkt,
        "delete_temp": cfg.delete_temp,
        "cache_dir": str(cfg.cache_dir),
        "parquet_dir": str(parquet_dir),
    }


_DOWNLOAD_WORKERS = 4


def _download_all(
    urls: list[str],
    mode: str,
    workers: int,
    cookie: str | None,
    cache_dir: Path,
    label: str,
    description: str = "downloading",
) -> None:
    try:
        with (
            progress_bar(len(urls), unit=label, description=description) as advance,
            concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            for _ in pool.map(lambda u: download_osm_file(u, mode=mode, cookie=cookie, cache_dir=cache_dir), urls):
                advance()
    except requests.exceptions.RequestException as exc:
        raise OsmsgError(
            f"Network error downloading {label} after retries ({type(exc).__name__}). "
            "Re-run to resume: finished downloads are cached, so it continues from where it stopped."
        ) from exc


def _process_all(
    items: list,
    *,
    target,
    initializer,
    init_args,
    chunksize: int,
    label: str,
    workers: int,
    extra_iterables: tuple[list, ...] = (),
    description: str = "processing",
) -> None:
    with (
        progress_bar(len(items), unit=label, description=description) as advance,
        concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, initializer=initializer, initargs=init_args
        ) as pool,
    ):
        for _ in pool.map(target, items, *extra_iterables, chunksize=chunksize):
            advance()


def run(cfg: RunConfig) -> dict[str, Any]:
    """Run a full osmsg pipeline. Returns paths + counts."""
    from .workers import (
        init_changefile_worker,
        init_changeset_worker,
        process_changefile,
        process_changeset,
    )

    cfg = copy.deepcopy(cfg)

    info(f"osmsg {__version__}")
    _normalize_urls(cfg)
    if cfg.hashtags:
        cfg.hashtags = _canonical_hashtags(cfg.hashtags)

    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    cs_dir = cfg.cache_dir / "scratch_cs"
    cf_dir = cfg.cache_dir / "scratch_cf"
    # Drop scratch dirs in case a previous run crashed mid-write.
    for scratch in (cs_dir, cf_dir):
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    cookie = _ensure_credentials(cfg)

    db_path = cfg.output_dir / f"{cfg.name}.duckdb"

    if cfg.end_date is None:
        cfg.end_date = dt.datetime.now(UTC)
    fingerprint = _query_fingerprint(cfg)

    if not cfg.update and not cfg.insert and not cfg.overwrite and db_path.exists():
        existing = dbmod.connect(str(db_path))
        if _read_fingerprint(existing) == fingerprint:
            info(f"Reusing {db_path} (same query); re-exporting. Pass --overwrite to recompute.")
            start_utc = (cfg.start_date or cfg.end_date).astimezone(UTC)
            return _finalize(
                cfg,
                existing,
                fingerprint,
                start_date_utc=start_utc,
                end_date_utc=cfg.end_date.astimezone(UTC),
                start_seq=None,
                end_seq=None,
            )
        dbmod.close(existing)

    if not cfg.update and db_path.exists():
        db_path.unlink()
    conn = dbmod.connect(str(db_path))
    dbmod.create_tables(conn)
    info(f"DuckDB: {db_path}")

    if cfg.insert:
        return _run_insert(cfg, conn, db_path)

    if cfg.start_date is not None:
        _auto_switch_replication(cfg, cfg.end_date - cfg.start_date)

    if cfg.update:
        _select_update_source(conn, cfg, dt.datetime.now(UTC))
    _seed_history_resume(conn, cfg)
    url_starts = _resolve_url_starts(conn, cfg)
    if cfg.update:
        # Changeset-replication reads one planet-wide source; widest window covers every URL.
        cfg.start_date = min(ts for ts, _seq in url_starts.values())
        info(
            "--update: resuming each source from its own state row "
            f"(earliest: {cfg.start_date.astimezone(UTC).isoformat()})"
        )

    # _resolve_url_starts guarantees start_date is set (or raised); narrow for ty.
    assert cfg.start_date is not None
    if cfg.start_date >= cfg.end_date:
        raise OsmsgError("start_date >= end_date, nothing to do.")

    span = cfg.end_date - cfg.start_date
    info(f"Range: {cfg.start_date.astimezone(UTC).isoformat()} → {cfg.end_date.astimezone(UTC).isoformat()} ({span})")
    span_hours = span.total_seconds() / 3600
    # When auto-switch was suppressed (--url explicit, --update, --country, multi-URL), a long
    # span on minute replication still floods the network. Hint the user.
    if span_hours >= 72 and any(u == SHORTCUTS["minute"] for u in cfg.urls):
        warn(
            f"Range spans {span_hours:.0f}h on minute replication "
            f"(~{int(span_hours * 60):,} files). Consider --url hour or --url day."
        )

    geom_wkt = _resolve_geom_wkt(cfg)
    if (cfg.boundary or cfg.countries) and not cfg.hashtags:
        cfg.changeset = True

    # summary/tm_stats/--all read the changesets table, populate it even if user didn't ask.
    if (cfg.tm_stats or cfg.summary or cfg.tag_mode == "all") and not cfg.changeset and not cfg.hashtags:
        cfg.changeset = True

    run_live = True
    if cfg.history_mode == "auto" and not cfg.update:
        if cfg.length_tags:
            warn("history: --length is not in the published dataset; using the live path for the whole range.")
        else:
            manifest = fetch_manifest(cfg.history_url)
            if manifest is not None:
                split = split_window(cfg.start_date, cfg.end_date, manifest)
                if split.has_remote:
                    try:
                        filters = RemoteFilters(
                            hashtags=cfg.hashtags,
                            exact_lookup=cfg.exact_lookup,
                            users_filter=cfg.users_filter,
                            geom_wkt=geom_wkt,
                        )
                        n = ingest_remote(conn, split, filters, cfg.history_url)
                        live_start = _history_live_start(split, manifest.frontier)
                        tail = live_start.astimezone(UTC).isoformat()
                        info(f"history: ingested {n:,} changeset rows from remote; live tail from {tail}.")
                        cfg.start_date = live_start
                        url_starts = {u: (cfg.start_date, None) for u in cfg.urls}
                        run_live = cfg.start_date < cfg.end_date
                        if run_live:
                            _auto_switch_replication(cfg, cfg.end_date - cfg.start_date)
                    except duckdb.Error as exc:
                        for tbl in ("changeset_stats", "changesets", "users"):
                            conn.execute(f"DELETE FROM {tbl}")
                        dbmod.close(conn)
                        raise OsmsgError(
                            f"Reading the published history failed after retries ({type(exc).__name__}). "
                            "Re-run to try again, narrow the date range, or pass --no-history for the live path."
                        ) from exc

    max_workers = cfg.workers or _cpu_count()
    info(f"Workers: {max_workers}")

    # None == no filter active; empty set == filter matched nothing (drop everything).
    valid_changesets: set[int] | None = None
    start_seq: int | None = None
    end_seq: int | None = None
    # Threaded into changefile_download_urls so a tick never advances cf past cs.
    cs_frontier_ts: dt.datetime | None = None

    if run_live and (cfg.hashtags or cfg.changeset):
        cs_repl = ChangesetReplication(pad_hours=cfg.changeset_pad_hours)
        cs_state = get_state(conn, CHANGESETS_REPLICATION) if cfg.update else None
        cs_resume_seq = (cs_state["last_seq"] + 1) if cs_state else None
        urls, cs_start, cs_end = cs_repl.download_urls(cfg.start_date, cfg.end_date, resume_seq=cs_resume_seq)
        pad_note = (
            f"incremental from prior state seq {cs_state['last_seq']} (no backward pad)"
            if cs_state
            else f"first run with {cfg.changeset_pad_hours}h backward pad"
        )
        info(f"Changesets: {len(urls)} files (seq {cs_start}-{cs_end}), {pad_note}.")
        if len(urls) > 5000:
            warn(
                f"Hashtag/changeset filtering downloads the per-minute changeset stream for the live "
                f"tail ({len(urls):,} files here). This is slow over a busy network and resumes from "
                f"cache if interrupted; a shorter range or waiting for the dataset to cover more months "
                f"reduces it."
            )

        cs_frontier_ts = cs_repl.sequence_to_timestamp(cs_end)

        if urls:
            cs_dir.mkdir(parents=True, exist_ok=True)
            cs_config = _processing_config(cfg, parquet_dir=cs_dir, geom_wkt=geom_wkt)
            cs_config["window_start_utc"] = cfg.start_date.astimezone(UTC)

            _download_all(
                urls,
                "changeset",
                _DOWNLOAD_WORKERS,
                None,
                cfg.cache_dir,
                "changesets",
                description="Downloading changesets",
            )
            _process_all(
                urls,
                target=process_changeset,
                initializer=init_changeset_worker,
                init_args=(cs_config,),
                chunksize=10,
                label="changesets",
                workers=max_workers,
                description="Processing changesets",
            )
            dbmod.merge_parquet_files(conn, cs_dir, cleanup=True)
            upsert_state(
                conn,
                source_url=CHANGESETS_REPLICATION,
                last_seq=cs_end,
                last_ts=cs_frontier_ts,
                updated_at=dt.datetime.now(UTC),
            )
            info("Changeset processing complete.")

        valid_changesets = _resolve_valid_changesets(conn, cfg)

    end_date_utc = cfg.end_date.astimezone(UTC)

    for url in cfg.urls if run_live else []:
        info(f"Changefiles ← {url}")
        url_start, resume_seq = url_starts[url]
        urls, server_ts, src_start_seq, src_end_seq, _, _ = changefile_download_urls(
            url_start, cfg.end_date, url, resume_seq=resume_seq, cs_ts=cs_frontier_ts
        )
        if start_seq is None:
            start_seq = src_start_seq
        end_seq = src_end_seq
        url_start_date_utc = url_start.astimezone(UTC)

        gap = server_ts - url_start_date_utc
        info(
            f"  DB current to: {url_start_date_utc.isoformat()}  |  "
            f"server head: {server_ts.isoformat()}  |  gap: {gap}  |  files: {len(urls)}"
        )

        if not urls:
            info(f"  {url}: already up-to-date")
            if resume_seq is not None:
                # Heartbeat: bump updated_at so /health can tell "alive, idle" apart from "stuck".
                upsert_state(
                    conn,
                    source_url=url,
                    last_seq=resume_seq - 1,
                    last_ts=url_start,
                    updated_at=dt.datetime.now(UTC),
                )
            continue

        cf_dir.mkdir(parents=True, exist_ok=True)
        cf_config = _processing_config(cfg, parquet_dir=cf_dir, geom_wkt=None)
        cf_config["start_date_utc"] = url_start_date_utc

        _download_all(
            urls,
            "changefiles",
            _DOWNLOAD_WORKERS,
            cookie,
            cfg.cache_dir,
            "changefiles",
            description="Downloading changefiles",
        )
        chunksize = 10 if "minute" in url.lower() else 1
        seq_ids = list(range(src_start_seq, src_end_seq + 1))
        _process_all(
            urls,
            target=process_changefile,
            initializer=init_changefile_worker,
            init_args=(valid_changesets, cf_config),
            chunksize=chunksize,
            label="changefiles",
            workers=max_workers,
            extra_iterables=(seq_ids,),
            description="Processing changefiles",
        )
        dbmod.merge_parquet_files(conn, cf_dir, cleanup=True)
        # state.last_ts is the seq_ts of last_seq so the next tick's lower-bound filter
        # aligns with the seq boundary.
        state_last_ts = changefile_seq_timestamp(url, src_end_seq)
        upsert_state(
            conn,
            source_url=url,
            last_seq=src_end_seq,
            last_ts=state_last_ts,
            updated_at=dt.datetime.now(UTC),
        )
        lag = server_ts - state_last_ts
        info(f"  DB now current to: {state_last_ts.isoformat()}  |  lag from server: {lag}")
        info(f"Changefile processing complete: {url}")

    if cfg.delete_temp:
        # Never rmtree cfg.cache_dir itself, it may be the user's platform cache root.
        for sub in (cs_dir, cf_dir, cfg.cache_dir / "changefiles", cfg.cache_dir / "changeset"):
            if sub.exists():
                shutil.rmtree(sub, ignore_errors=True)

    history_row = conn.execute("SELECT count(*) FROM changeset_stats WHERE seq_id = 0").fetchone()
    has_history = bool(history_row and history_row[0] > 0)
    if run_live and has_history:
        dup_row = conn.execute(
            """DELETE FROM changeset_stats
               WHERE seq_id <> 0
                 AND changeset_id IN (SELECT changeset_id FROM changeset_stats WHERE seq_id = 0)"""
        ).fetchone()
        n_dupes = dup_row[0] if dup_row else 0
        if n_dupes:
            info(f"history: deduped {n_dupes:,} live rows already covered by the history layer.")

    if url_starts:
        start_date_utc = min(ts for ts, _seq in url_starts.values()).astimezone(UTC)
    else:
        start_date_utc = cfg.start_date.astimezone(UTC)

    return _finalize(
        cfg,
        conn,
        fingerprint,
        start_date_utc=start_date_utc,
        end_date_utc=end_date_utc,
        start_seq=start_seq,
        end_seq=end_seq,
    )


__all__ = ["RunConfig", "run"]

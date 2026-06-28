"""Hybrid-auto historical read: serve a window's covered months from the published parquet, leaving
the recent tail to the live diff path. Remote rows are tagged seq_id=0 and read by direct partition
path (a glob would make DuckDB list every partition over the HF API)."""

import datetime as dt
import json
import pathlib
import time
from dataclasses import dataclass

import duckdb
import requests

from .ui import info, progress_bar, warn

UTC = dt.UTC
SCHEMA_VERSION = 1
DEFAULT_HISTORY_URL = "hf://datasets/kshitijrajsharma/osmsg-history"
HISTORY_SEQ_ID = 0
MONTH_READ_ATTEMPTS = 6


@dataclass
class Manifest:
    schema_version: int
    min_month: dt.datetime
    frontier: dt.datetime


@dataclass
class WindowSplit:
    remote_start: dt.datetime | None
    remote_end: dt.datetime | None
    live_start: dt.datetime

    @property
    def has_remote(self) -> bool:
        return self.remote_start is not None and self.remote_end is not None and self.remote_start < self.remote_end


@dataclass
class RemoteFilters:
    """Exactly the run filters the remote ingest needs, so history.py does not import RunConfig."""

    hashtags: list[str] | None
    exact_lookup: bool
    users_filter: list[str] | None
    geom_wkt: str | None

    @property
    def has_metadata_filter(self) -> bool:
        return bool(self.hashtags or self.users_filter or self.geom_wkt)


def _manifest_http_url(history_url: str) -> str:
    if history_url.startswith("hf://datasets/"):
        repo = history_url[len("hf://datasets/") :]
        return f"https://huggingface.co/datasets/{repo}/resolve/main/manifest.json"
    return history_url.rstrip("/") + "/manifest.json"


def _month_start(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)


def _next_month(when: dt.datetime) -> dt.datetime:
    return (when.replace(day=1) + dt.timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def fetch_manifest(history_url: str, timeout: int = 15) -> Manifest | None:
    """Fetch and parse the dataset manifest. Returns None on any failure so the caller falls back to
    the live path; the network/parse exceptions are named because that fallback is the recovery."""
    url = _manifest_http_url(history_url)
    try:
        if url.startswith(("http://", "https://")):
            response = requests.get(url, timeout=timeout)
            if response.status_code != 200:
                warn(f"history: manifest HTTP {response.status_code} at {url}; using live path.")
                return None
            payload = response.json()
        else:
            with open(url) as handle:
                payload = json.load(handle)
    except (requests.RequestException, OSError, ValueError) as exc:
        warn(f"history: manifest unreachable ({type(exc).__name__}); using live path.")
        return None

    try:
        version = int(payload["schema_version"])
    except (KeyError, ValueError, TypeError) as exc:
        warn(f"history: malformed manifest ({type(exc).__name__}); using live path.")
        return None
    if version != SCHEMA_VERSION:
        warn(f"history: manifest schema_version {version} != {SCHEMA_VERSION}; using live path.")
        return None
    try:
        return Manifest(
            schema_version=version,
            min_month=_month_start(payload["min_month"]),
            frontier=_next_month(_month_start(payload["max_month"])),
        )
    except (KeyError, ValueError, TypeError) as exc:
        warn(f"history: malformed manifest months ({type(exc).__name__}); using live path.")
        return None


def split_window(start: dt.datetime, end: dt.datetime, manifest: Manifest) -> WindowSplit:
    """Split [start, end) at the published frontier. Remote covers the overlap with the dataset's
    complete months; the live diff path covers the rest (the recent tail)."""
    remote_start = max(start, manifest.min_month)
    remote_end = min(end, manifest.frontier)
    if remote_start >= remote_end:
        return WindowSplit(remote_start=None, remote_end=None, live_start=start)
    return WindowSplit(remote_start=remote_start, remote_end=remote_end, live_start=remote_end)


def _months(start: dt.datetime, end: dt.datetime) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) partitions overlapping [start, end)."""
    out: list[tuple[int, int]] = []
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        out.append((cursor.year, cursor.month))
        cursor = _next_month(cursor)
    return out


def _partition_list(base: str, dataset: str, months: list[tuple[int, int]]) -> str | None:
    """Direct read_parquet() over the given month partitions (local bases filtered to existing files),
    or None when none exist."""
    root = base.rstrip("/")
    remote = root.startswith(("hf://", "http://", "https://", "s3://"))
    files = [f"{root}/{dataset}/year={year}/month={month}/data.parquet" for (year, month) in months]
    if not remote:
        files = [f for f in files if pathlib.Path(f).exists()]
    if not files:
        return None
    return f"read_parquet([{', '.join(repr(f) for f in files)}])"


def _hashtag_predicate(hashtags: list[str], exact_lookup: bool) -> str:
    """SQL predicate matching the changesets `hashtags` list: whole-token with exact_lookup, else substring."""
    needles = [h.lower() for h in hashtags]
    if exact_lookup:
        terms = ", ".join(f"'{n}'" for n in needles)
        return f"len(list_filter(hashtags, x -> lower(x) IN ({terms}))) > 0"
    likes = " OR ".join(f"lower(x) LIKE '%{n.lstrip('#')}%'" for n in needles)
    return f"len(list_filter(hashtags, x -> {likes})) > 0"


def ingest_remote(
    conn: duckdb.DuckDBPyConnection,
    split: WindowSplit,
    filters: RemoteFilters,
    history_url: str,
) -> int:
    """Populate users / changesets / changeset_stats from the published parquet for the covered window.
    Returns the number of history changeset rows ingested. Filters mirror the live path exactly."""
    if split.remote_start is None or split.remote_end is None:
        return 0
    months = _months(split.remote_start, split.remote_end)
    start_iso = split.remote_start.astimezone(UTC).isoformat()
    end_iso = split.remote_end.astimezone(UTC).isoformat()
    in_window = f"created_at >= TIMESTAMPTZ '{start_iso}' AND created_at < TIMESTAMPTZ '{end_iso}'"

    conn.execute("INSTALL json; LOAD json;")
    conn.execute("INSTALL spatial; LOAD spatial;")
    if history_url.startswith(("hf://", "http://", "https://", "s3://")):
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        conn.execute("SET http_keep_alive=true; SET http_timeout=60000;")
        conn.execute("SET http_retries=20; SET http_retry_wait_ms=5000; SET http_retry_backoff=2;")

    changeset_preds = [in_window]
    if filters.hashtags:
        changeset_preds.append(_hashtag_predicate(filters.hashtags, filters.exact_lookup))
    if filters.geom_wkt:
        changeset_preds.append(
            f"ST_Intersects(ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat), ST_GeomFromText('{filters.geom_wkt}'))"
        )
    if filters.users_filter:
        names = ", ".join(f"'{u}'" for u in filters.users_filter)
        changeset_preds.append(f"uid IN (SELECT uid FROM users WHERE username IN ({names}))")
    changeset_where = " AND ".join(changeset_preds)

    stats_preds = [in_window]
    if filters.has_metadata_filter:
        stats_preds.append("changeset_id IN (SELECT changeset_id FROM changesets)")
    stats_where = " AND ".join(stats_preds)

    info(f"history: remote ingest {start_iso} -> {end_iso} ({len(months)} month partitions) from {history_url}")

    def ingest_month(month: tuple[int, int]) -> None:
        changesets_src = _partition_list(history_url, "changesets", [month])
        changefiles_src = _partition_list(history_url, "changefiles", [month])
        if changesets_src is not None:
            conn.execute(
                f"""INSERT INTO users
                    SELECT uid, any_value(username) FROM {changesets_src}
                    WHERE {in_window} AND username IS NOT NULL
                    GROUP BY uid ON CONFLICT (uid) DO NOTHING"""
            )
            conn.execute(
                f"""INSERT INTO changesets
                    SELECT changeset_id, uid, created_at, hashtags, editor,
                           CASE WHEN min_lon IS NOT NULL
                                THEN ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat) END
                    FROM {changesets_src} WHERE {changeset_where}
                    ON CONFLICT (changeset_id) DO NOTHING"""
            )
        if changefiles_src is not None:
            conn.execute(
                f"""INSERT INTO changeset_stats
                    SELECT changeset_id, {HISTORY_SEQ_ID} AS seq_id, uid,
                           nodes_created, nodes_modified, nodes_deleted,
                           ways_created, ways_modified, ways_deleted,
                           rels_created, rels_modified, rels_deleted,
                           poi_created, poi_modified, tag_stats
                    FROM {changefiles_src} WHERE {stats_where}
                    ON CONFLICT (seq_id, changeset_id) DO NOTHING"""
            )

    with progress_bar(len(months), unit="months", description="Reading history") as advance:
        for month in months:
            for attempt in range(MONTH_READ_ATTEMPTS):
                try:
                    ingest_month(month)
                    break
                except duckdb.Error as exc:
                    if attempt == MONTH_READ_ATTEMPTS - 1:
                        raise
                    warn(f"history: {month[0]}-{month[1]:02d} read failed ({type(exc).__name__}); retrying.")
                    time.sleep(min(60, 5 * 2**attempt))
            advance()

    row = conn.execute(f"SELECT count(*) FROM changeset_stats WHERE seq_id = {HISTORY_SEQ_ID}").fetchone()
    return row[0] if row else 0


RESUME_SAFETY = dt.timedelta(days=1)


def seed_resume_at(conn: duckdb.DuckDBPyConnection, resume_at: dt.datetime, replication_url: str) -> dt.datetime | None:
    """Seed `state` so `osmsg --update` resumes at `resume_at` on `replication_url`. Returns resume_at,
    or None if no sequence resolves."""
    from osmium.replication.server import ReplicationServer

    from .db.schema import upsert_state

    seq = ReplicationServer(replication_url).timestamp_to_sequence(resume_at)
    if seq is None:
        warn(f"history: could not resolve a sequence at {resume_at.isoformat()} for {replication_url}.")
        return None
    upsert_state(
        conn,
        source_url=replication_url,
        last_seq=seq,
        last_ts=resume_at,
        updated_at=dt.datetime.now(UTC),
    )
    info(f"history: seeded resume at seq {seq} ({resume_at.isoformat()}) for {replication_url}.")
    return resume_at


def seed_resume_state(conn: duckdb.DuckDBPyConnection, history_url: str, replication_url: str) -> dt.datetime | None:
    """Seed resume state at the published frontier minus the safety overlap. Returns the resume
    timestamp, or None if the manifest is unavailable."""
    manifest = fetch_manifest(history_url)
    if manifest is None:
        return None
    return seed_resume_at(conn, manifest.frontier - RESUME_SAFETY, replication_url)

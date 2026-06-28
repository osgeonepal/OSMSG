"""Typer-based CLI for osmsg.

UTC throughout, no display timezone. Outputs default to parquet (queryable from
disk by DuckDB / polars / pandas). Other formats: csv, json, markdown, psql.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from platformdirs import user_cache_dir
from typer_config.decorators import use_yaml_config

from .__version__ import __version__
from .exceptions import (
    CredentialsRequiredError,
    GeofabrikAuthError,
    NoDataFoundError,
    OsmsgError,
    UnknownRegionError,
)
from .maintain.cli import maintain_app
from .pipeline import RunConfig, run
from .ui import console, error, info, render_table, warn

load_dotenv()
UTC = dt.UTC
DEFAULT_CACHE_DIR = Path(user_cache_dir("osmsg"))

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="OpenStreetMap stats generator. Parquet-first, OAuth 2.0, UTC-only.",
)
app.add_typer(maintain_app, name="maintain")


class Period(StrEnum):
    hour = "hour"
    day = "day"
    week = "week"
    month = "month"
    year = "year"


class Format(StrEnum):
    parquet = "parquet"
    csv = "csv"
    json = "json"
    markdown = "markdown"
    psql = "psql"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"osmsg {__version__}")
        raise typer.Exit()


def _read_password_stdin() -> str:
    import sys

    pw = sys.stdin.readline().rstrip("\n")
    if not pw:
        error("--password-stdin: no password received on stdin.")
        raise typer.Exit(code=2)
    return pw


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    raise typer.BadParameter(f"unrecognized datetime: {value!r}")


def _period_range(period: Period) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now(UTC)
    if period is Period.hour:
        end = now.replace(minute=0, second=0, microsecond=0)
        return end - dt.timedelta(hours=1), end
    if period is Period.day:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return end - dt.timedelta(days=1), end
    if period is Period.week:
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=now.weekday())
        return end - dt.timedelta(days=7), end
    if period is Period.month:
        first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev = first_of_month - dt.timedelta(days=1)
        return prev.replace(day=1), first_of_month
    if period is Period.year:
        first_of_year = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        prev = first_of_year.replace(year=first_of_year.year - 1)
        return prev, first_of_year
    raise ValueError(period)


@app.callback(invoke_without_command=True)
@use_yaml_config(param_name="config", param_help="YAML config file (CLI flags override its values).")
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Print version and exit."),
    ] = None,
    name: Annotated[
        str,
        typer.Option(envvar="OSMSG_NAME", help="Output basename. Writes <name>.duckdb + selected formats."),
    ] = "stats",
    start: Annotated[str | None, typer.Option(help="ISO start (UTC). 'YYYY-MM-DD HH:MM:SS'.")] = None,
    end: Annotated[str | None, typer.Option(help="ISO end (UTC). Defaults to now.")] = None,
    last: Annotated[Period | None, typer.Option(help="Convenience: hour|day|week|month|year.")] = None,
    days: Annotated[int | None, typer.Option(help="Last N days (mutually exclusive with --last).")] = None,
    country: Annotated[
        list[str] | None,
        typer.Option(
            "--country",
            envvar="OSMSG_COUNTRY",
            help="Geofabrik region id(s); resolved live. Requires OSM credentials. Comma-separated when set via env.",
        ),
    ] = None,
    url: Annotated[
        list[str] | None,
        typer.Option(
            "--url",
            envvar="OSMSG_URL",
            help="Replication URL(s). Shortcuts: minute, hour, day. Comma-separated when set via env.",
        ),
    ] = None,
    hashtags: Annotated[
        list[str] | None,
        typer.Option("--hashtags", help="Hashtag filter (substring by default; whole-word with --exact-lookup)."),
    ] = None,
    tags: Annotated[list[str] | None, typer.Option("--tags", help="Per-key counts (e.g. building highway).")] = None,
    length: Annotated[list[str] | None, typer.Option("--length", help="Length-in-m for tag keys.")] = None,
    users: Annotated[
        list[str] | None,
        typer.Option("--users", help="Filter to OSM usernames (case-sensitive, exact match). Repeat for more."),
    ] = None,
    workers: Annotated[
        int | None,
        typer.Option(envvar="OSMSG_WORKERS", help="Parallel parse workers (default: cpu count)."),
    ] = None,
    rows: Annotated[
        int | None,
        typer.Option(help="Cap rows shown in the console table. Files always carry the full set."),
    ] = None,
    boundary: Annotated[
        str | None,
        typer.Option(
            envvar="OSMSG_BOUNDARY",
            help="Boundary filter: Geofabrik region name (e.g. 'nepal'), GeoJSON file path, or inline GeoJSON.",
        ),
    ] = None,
    formats: Annotated[
        list[Format] | None,
        typer.Option(
            "--format",
            "-f",
            envvar="OSMSG_FORMAT",
            help="One or more output formats. Comma-separated when set via env.",
        ),
    ] = None,
    summary: Annotated[bool, typer.Option(help="Also write <name>_summary.parquet + summary.md.")] = False,
    changeset: Annotated[bool, typer.Option(hidden=True)] = False,
    all_stats: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Collect all tag key=value stats and changeset metadata (hashtags, editors).",
        ),
    ] = False,
    keys_only: Annotated[bool, typer.Option("--keys", help="Collect tag key stats only (no value breakdown).")] = False,
    exact_lookup: Annotated[
        bool, typer.Option("--exact-lookup", help="Hashtag whole-word match. Only meaningful with --hashtags.")
    ] = False,
    tm_stats: Annotated[bool, typer.Option("--tm-stats", help="Attach Tasking Manager totals.")] = False,
    update: Annotated[bool, typer.Option(help="Append to existing <name>.duckdb.")] = False,
    cache_dir: Annotated[
        Path,
        typer.Option("--cache-dir", envvar="OSMSG_CACHE_DIR", help="Cache dir for downloaded OSM files."),
    ] = DEFAULT_CACHE_DIR,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            envvar="OSMSG_OUTPUT_DIR",
            help="Where to write <name>.duckdb + selected formats. Defaults to current directory.",
        ),
    ] = Path("."),
    delete_temp: Annotated[
        bool,
        typer.Option(
            "--delete-temp",
            help="Remove this run's downloaded files + scratch dirs after processing (cache_dir itself is kept).",
        ),
    ] = False,
    username: Annotated[str | None, typer.Option(help="OSM username. Else $OSM_USERNAME, then prompt.")] = None,
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read OSM password from stdin (one line). Else $OSM_PASSWORD, then prompt.",
        ),
    ] = False,
    psql_dsn: Annotated[
        str | None,
        typer.Option("--psql-dsn", envvar="OSMSG_PSQL_DSN", help="libpq DSN for --format psql."),
    ] = None,
    psql_bulk: Annotated[
        bool,
        typer.Option(
            "--psql-bulk",
            envvar="OSMSG_PSQL_BULK",
            help="Faster one-time psql load: drop secondary indexes and foreign keys during the push "
            "and rebuild them after. Use for a full history import, not for incremental --update.",
        ),
    ] = False,
    changeset_pad_hours: Annotated[
        int,
        typer.Option(
            "--changeset-pad-hours",
            envvar="OSMSG_CHANGESET_PAD_HOURS",
            help="Backward pad (hours) on first runs of changeset replication. "
            "Set to 24 to capture long-running open changesets. --update runs skip the pad.",
            min=0,
            max=48,
        ),
    ] = 1,
    history: Annotated[
        bool,
        typer.Option(
            "--history/--no-history",
            envvar="OSMSG_HISTORY",
            help="Serve covered months from the published parquet (HuggingFace) and only download the "
            "recent tail. Falls back to the live diff path if unavailable. Ignored by --update.",
        ),
    ] = True,
    history_url: Annotated[
        str,
        typer.Option(
            "--history-url",
            envvar="OSMSG_HISTORY_URL",
            help="Base URL of the published history dataset.",
        ),
    ] = "hf://datasets/kshitijrajsharma/osmsg-history",
    insert: Annotated[
        bool,
        typer.Option(
            "--insert",
            help="Load history into the store and seed resume state, then exit. No window loads the "
            "whole published history; --start/--end loads a slice. Follow with --update to catch up.",
        ),
    ] = False,
    osh_file: Annotated[
        str | None,
        typer.Option("--osh-file", help="Insert from a local .osh.pbf instead of the published dataset."),
    ] = None,
    changeset_file: Annotated[
        str | None,
        typer.Option("--changeset-file", help="Changeset dump (.osm.bz2) paired with --osh-file."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Recompute even if <name>.duckdb already holds this exact query; otherwise a rerun "
            "that only changes the output format re-exports from the existing store.",
        ),
    ] = False,
) -> None:
    """Run osmsg. With no subcommand this generates stats (or loads history with --insert)."""
    if ctx.invoked_subcommand is not None:
        return
    if formats is None:
        formats = [Format.parquet]
    if psql_dsn and Format.psql not in formats:
        formats.append(Format.psql)
    if sum(1 for x in (start, last, days) if x) > 1:
        error("--start, --last, and --days are mutually exclusive, pick one.")
        raise typer.Exit(code=2)
    if update and any(x is not None for x in (start, end, last, days)):
        error("--update resumes from prior state and runs to head; it ignores --start/--end/--last/--days.")
        raise typer.Exit(code=2)
    if insert and update:
        error("--insert and --update are mutually exclusive; insert first, then update.")
        raise typer.Exit(code=2)
    if insert and (last is not None or days is not None):
        error("--insert takes --start/--end (or no window), not --last/--days.")
        raise typer.Exit(code=2)
    if (osh_file is None) != (changeset_file is None):
        error("--osh-file and --changeset-file must be given together.")
        raise typer.Exit(code=2)
    if osh_file and not insert:
        error("--osh-file/--changeset-file are only valid with --insert.")
        raise typer.Exit(code=2)
    if psql_bulk and update:
        error("--psql-bulk is for a one-time full load (drops indexes/keys); do not use it with --update.")
        raise typer.Exit(code=2)
    if Format.psql in formats and not psql_dsn:
        error("-f psql requires --psql-dsn (libpq connection string, e.g. 'host=localhost dbname=osm user=osm').")
        raise typer.Exit(code=2)
    if tm_stats and not hashtags:
        warn("--tm-stats has no effect without --hashtags; TM enrichment keys off hashtags.")

    cfg = RunConfig(
        name=name,
        end_date=_parse_dt(end),
        countries=country,
        urls=url or ["minute"],
        url_explicit=url is not None,
        workers=workers,
        additional_tags=tags,
        hashtags=hashtags,
        length_tags=length,
        users_filter=users,
        tag_mode="all" if all_stats else ("keys" if keys_only else "none"),
        exact_lookup=exact_lookup,
        changeset=changeset,
        summary=summary,
        boundary=boundary,
        tm_stats=tm_stats,
        formats=[f.value for f in formats],
        update=update,
        cache_dir=cache_dir,
        output_dir=output_dir,
        delete_temp=delete_temp,
        osm_username=username,
        osm_password=_read_password_stdin() if password_stdin else None,
        psql_dsn=psql_dsn,
        psql_bulk=psql_bulk,
        changeset_pad_hours=changeset_pad_hours,
        history_mode="auto" if history else "off",
        history_url=history_url,
        insert=insert,
        osh_file=osh_file,
        changeset_file=changeset_file,
        overwrite=overwrite,
    )

    if last is not None:
        cfg.start_date, cfg.end_date = _period_range(last)
    elif days is not None:
        if days <= 0:
            error("--days must be > 0.")
            raise typer.Exit(code=2)
        end_dt = dt.datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        cfg.start_date, cfg.end_date = end_dt - dt.timedelta(days=days), end_dt
    elif start:
        cfg.start_date = _parse_dt(start)

    try:
        result = run(cfg)
    except UnknownRegionError as exc:
        error(f"Unknown region: {exc}")
        raise typer.Exit(code=2) from exc
    except CredentialsRequiredError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc
    except GeofabrikAuthError as exc:
        error(f"Geofabrik authentication failed: {exc}")
        raise typer.Exit(code=2) from exc
    except NoDataFoundError as exc:
        # Empty window under --update: exit 0 so cron doesn't flag a no-op as failure.
        info(str(exc))
        raise typer.Exit(code=0) from exc
    except OsmsgError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from exc

    if insert:
        info(f"insert complete: {result['rows']:,} history changeset rows loaded.")
        for label, path in (result.get("files") or {}).items():
            console.print(f"[green]✓[/green] {label}: [bold]{path}[/bold]")
        console.print("Next: [bold]osmsg --update[/bold] to catch up to now.")
        return

    rows_data = result.get("rows_data") or []
    display_n = min(rows or 20, len(rows_data))
    render_table(
        rows_data[:display_n],
        columns=(
            "rank",
            "name",
            "changesets",
            "map_changes",
            "nodes_create",
            "ways_create",
            "rels_create",
            "poi_create",
            "hashtags",
        ),
        title=f"Top users (showing {display_n} of {result['rows']})",
    )
    for label, path in (result.get("files") or {}).items():
        console.print(f"[green]✓[/green] {label}: [bold]{Path(path).name if Path(path).exists() else path}[/bold]")

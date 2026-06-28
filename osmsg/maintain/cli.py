"""`osmsg maintain` sub-app: generate, convert, and publish the history parquet datasets."""

import datetime as dt
from pathlib import Path
from typing import Annotated

import typer

from ..ui import console, error, info

UTC = dt.UTC
maintain_app = typer.Typer(add_completion=False, help="Generate and publish the history parquet datasets.")


def _parse_day(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


@maintain_app.command("month")
def month_cmd(
    ym: Annotated[str, typer.Argument(help="Finished month to append, 'YYYY-MM'.")],
    repo: Annotated[str | None, typer.Option("--repo", help="HuggingFace dataset repo id to upload to.")] = None,
    no_upload: Annotated[bool, typer.Option("--no-upload", help="Generate and export only; skip upload.")] = False,
    allow_incomplete: Annotated[
        bool, typer.Option("--allow-incomplete", help="Publish even if the month stops short of its boundary.")
    ] = False,
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Where to write the partitions.")] = Path("out"),
    work_dir: Annotated[Path, typer.Option("--work-dir", help="Scratch dir for the month's run.")] = Path("month_work"),
) -> None:
    """Build one finished month from the live day diffs and (unless --no-upload) push it to HuggingFace.
    Re-running an existing month rebuilds it complete and overwrites the published partition."""
    from ..exceptions import OsmsgError
    from .manifest import bump_manifest
    from .month import ensure_complete_month, export_month, generate_month, upload, verify_month_complete

    year, month = (int(x) for x in ym.split("-"))
    ensure_complete_month(year, month)
    db = generate_month(year, month, work_dir)
    if not allow_incomplete:
        try:
            verify_month_complete(db, year, month)
        except OsmsgError as exc:
            error(str(exc))
            raise typer.Exit(code=2) from exc
    cf, cs = export_month(db, year, month, output_dir)
    info(f"{ym}: changefiles={cf:,} changesets={cs:,} -> {output_dir}")
    if no_upload:
        console.print(f"Generated locally. Upload with: osmsg maintain publish {output_dir} --repo <repo>")
        return
    if not repo:
        raise typer.BadParameter("--repo is required unless --no-upload is set.")
    upload(repo, output_dir, year, month)
    bump_manifest(repo, ym)
    info(f"{ym}: uploaded and manifest advanced to {ym}.")


@maintain_app.command("convert")
def convert_cmd(
    osh: Annotated[str, typer.Argument(help="OSM full-history file (.osh.pbf).")],
    changesets: Annotated[str, typer.Argument(help="Changeset dump (.osm.bz2).")],
    start: Annotated[str, typer.Argument(help="Window start 'YYYY-MM-DD'.")],
    end: Annotated[str, typer.Argument(help="Window end 'YYYY-MM-DD'.")],
    work_dir: Annotated[Path, typer.Argument(help="Working directory; datasets land in <work_dir>/out.")],
    parts: Annotated[int, typer.Option("--parts", help="Split the history into N parts for parallel streaming.")] = 1,
) -> None:
    """Convert a local planet .osh history + changeset dump to the two parquet datasets."""
    from .convert import convert

    out = convert(osh, changesets, _parse_day(start), _parse_day(end), work_dir, parts=parts)
    info(f"datasets written to {out}/changefiles and {out}/changesets")


@maintain_app.command("publish")
def publish_cmd(
    out_dir: Annotated[Path, typer.Argument(help="Directory holding changefiles/ and changesets/.")],
    repo: Annotated[str | None, typer.Option("--repo", help="HuggingFace dataset repo id to upload to.")] = None,
    drop_last: Annotated[bool, typer.Option("--drop-last", help="Exclude the newest month (still partial).")] = False,
) -> None:
    """Write manifest.json spanning the covered months and (with --repo) upload it."""
    from .manifest import write_manifest

    manifest = write_manifest(out_dir, drop_last=drop_last, repo=repo)
    info(f"manifest: {manifest}")

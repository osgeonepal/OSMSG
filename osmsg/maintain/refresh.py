"""Pull the newest published rollup into a deployment's local history artifact so its frontier advances
to the latest month without a rebuild. Download and verify into a scratch dir, then swap atomically:
the parquet files first and the manifest last, so a query never sees an advanced frontier without the
data behind it."""

import datetime as dt
import os
import pathlib
import shutil

import duckdb
import requests

from ..exceptions import OsmsgError
from ..history import fetch_manifest
from ..ui import info

UTC = dt.UTC
VERIFY_TOLERANCE = dt.timedelta(days=2)
_HF_DATASETS = "https://huggingface.co/datasets"
_DOWNLOAD_CHUNK = 1 << 20

_ROLLUP_FILES = {
    "hashtag_changeset.parquet": "rollup/hashtag_changeset/data.parquet",
    "users.parquet": "rollup/users/data.parquet",
}


def _download_file(repo: str, remote: str, dest: pathlib.Path) -> pathlib.Path:
    """Stream a published file from the dataset's resolve/main URL to dest, preserving its exact bytes
    (and so its row-group layout). Raises on any non-200 so a bad pull never reaches the swap."""
    url = f"{_HF_DATASETS}/{repo}/resolve/main/{remote}"
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(_DOWNLOAD_CHUNK):
                handle.write(chunk)
    return dest


def _rollup_bounds(parquet: pathlib.Path) -> tuple[int, dt.datetime | None]:
    """Row count and latest created_at of a rollup parquet, read in one scan."""
    con = duckdb.connect()
    row = con.execute(f"SELECT count(*), max(created_at) FROM read_parquet('{parquet}')").fetchone()
    con.close()
    count, latest = row if row else (0, None)
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return count, latest


def _verify(new_rollup: pathlib.Path, frontier: dt.datetime, current_rollup: pathlib.Path) -> None:
    """Refuse to swap unless the download reaches the published frontier and did not shrink."""
    new_rows, latest = _rollup_bounds(new_rollup)
    if latest is None:
        raise OsmsgError("refreshed rollup has no rows; refusing to swap.")
    if latest < frontier - VERIFY_TOLERANCE:
        raise OsmsgError(
            f"refreshed rollup ends {latest.astimezone(UTC).isoformat()}, short of frontier "
            f"{frontier.astimezone(UTC).isoformat()}; refusing to swap."
        )
    if current_rollup.exists():
        current_rows = _rollup_bounds(current_rollup)[0]
        if new_rows < current_rows:
            raise OsmsgError(f"refreshed rollup has fewer rows ({new_rows:,} < {current_rows:,}); refusing to swap.")


def refresh_artifact(repo: str, artifact_dir: pathlib.Path) -> bool:
    """Advance artifact_dir to the newest published month, or return False when it is already current.
    Idempotent and safe to schedule; a failed pull leaves the live files untouched."""
    source = f"hf://datasets/{repo}"
    remote = fetch_manifest(source)
    if remote is None:
        raise OsmsgError(f"could not read the published manifest at {source}.")
    local = fetch_manifest(str(artifact_dir))
    if local is not None and remote.frontier <= local.frontier:
        info(f"artifact already current (frontier {local.frontier.astimezone(UTC).date()}); nothing to do.")
        return False

    artifact_dir.mkdir(parents=True, exist_ok=True)
    scratch = artifact_dir / ".refresh-tmp"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir()

    downloaded = {
        name: _download_file(repo, remote_path, scratch / name) for name, remote_path in _ROLLUP_FILES.items()
    }
    manifest = _download_file(repo, "manifest.json", scratch / "manifest.json")
    _verify(downloaded["hashtag_changeset.parquet"], remote.frontier, artifact_dir / "hashtag_changeset.parquet")
    for name, path in downloaded.items():
        os.replace(path, artifact_dir / name)
    os.replace(manifest, artifact_dir / "manifest.json")
    shutil.rmtree(scratch)

    info(f"artifact advanced to frontier {remote.frontier.astimezone(UTC).date()}.")
    return True

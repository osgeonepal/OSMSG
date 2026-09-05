"""Download + decompress OSM replication files. Cache-friendly: skips downloads
when the decompressed file is already on disk."""

import gzip
import os
import shutil
from pathlib import Path

from ._http import session

DEFAULT_CACHE_DIR = Path("temp")


def file_path_for(url: str, mode: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    parts = url.split("/")
    return cache_dir / mode / parts[-4] / f"{parts[-3]}_{parts[-2]}_{parts[-1]}"


def download_osm_file(
    url: str, mode: str = "changefiles", cookie: str | None = None, cache_dir: Path = DEFAULT_CACHE_DIR
) -> Path:
    """Stream `.osc.gz` / `.osm.gz` to disk, decompress, return path. Cached final path short-circuits."""
    raw_path = file_path_for(url, mode, cache_dir).with_suffix("")

    if raw_path.exists():
        return raw_path

    raw_path.parent.mkdir(parents=True, exist_ok=True)

    partial = raw_path.with_suffix(raw_path.suffix + ".partial")
    if partial.exists():
        partial.unlink()

    headers = {"Cookie": cookie} if cookie and "geofabrik" in url.lower() else None
    with session.get(url, headers=headers, stream=True) as r:
        r.raise_for_status()
        # decode_content=True unwraps any transport gzip so our GzipFile only sees file-level framing.
        r.raw.decode_content = True
        try:
            with gzip.GzipFile(fileobj=r.raw) as src, partial.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    os.replace(partial, raw_path)
    return raw_path

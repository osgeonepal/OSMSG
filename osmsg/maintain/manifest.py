"""Read, write, and advance the dataset manifest.json (the covered month range) that drives osmsg's
hybrid history read."""

import json
import pathlib
import subprocess
import tempfile

import requests

SCHEMA_VERSION = 1


def covered_months(out_dir: pathlib.Path) -> list[tuple[int, int]]:
    """Sorted (year, month) partitions present under out_dir/changesets."""
    months = set()
    for partition in (out_dir / "changesets").glob("year=*/month=*"):
        year = int(partition.parent.name.split("=")[1])
        month = int(partition.name.split("=")[1])
        months.add((year, month))
    return sorted(months)


def _upload(repo: str, path: pathlib.Path) -> None:
    cmd = ["uvx", "--from", "huggingface_hub", "hf", "upload", repo, str(path), "manifest.json"]
    subprocess.run([*cmd, "--repo-type", "dataset"], check=True)


def write_manifest(out_dir: pathlib.Path, *, drop_last: bool = False, repo: str | None = None) -> dict:
    """Write out_dir/manifest.json spanning the covered partitions. drop_last excludes the newest month
    when it is still partial. Uploads when repo is given. Returns the manifest dict."""
    months = covered_months(out_dir)
    if not months:
        raise ValueError(f"no partitions under {out_dir}")
    if drop_last:
        months = months[:-1]
    (y0, m0), (y1, m1) = months[0], months[-1]
    manifest = {"schema_version": SCHEMA_VERSION, "min_month": f"{y0:04d}-{m0:02d}", "max_month": f"{y1:04d}-{m1:02d}"}
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    if repo:
        _upload(repo, path)
    return manifest


def bump_manifest(repo: str, new_month: str) -> dict:
    """Advance the published manifest's max_month to new_month (forward only) and re-upload it."""
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/manifest.json"
    resp = requests.get(url, timeout=15)
    manifest = (
        resp.json()
        if resp.status_code == 200
        else {"schema_version": SCHEMA_VERSION, "min_month": new_month, "max_month": new_month}
    )
    if new_month <= str(manifest["max_month"]):
        return manifest
    manifest["max_month"] = new_month
    manifest["schema_version"] = SCHEMA_VERSION
    path = pathlib.Path(tempfile.mkdtemp()) / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    _upload(repo, path)
    return manifest

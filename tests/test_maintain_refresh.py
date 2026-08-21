"""maintain refresh: advance the local history artifact from the published dataset, atomically and only
when the download verifiably reaches the frontier."""

import datetime as dt
import json

import duckdb
import pytest

from osmsg.exceptions import OsmsgError
from osmsg.history import Manifest
from osmsg.maintain import refresh

UTC = dt.UTC


def _month_start(year, month):
    return dt.datetime(year, month, 1, tzinfo=UTC)


def _write_rollup(path, latest, rows):
    con = duckdb.connect()
    con.execute("CREATE TABLE t (changeset_id BIGINT, created_at TIMESTAMPTZ)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, latest) for i in range(rows)])
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    con.close()


def _fake_download(latest, manifest_dict):
    """Stand in for huggingface_hub.hf_hub_download: materialize each requested file under into_dir."""

    def _download(repo, remote, into_dir):
        dest = into_dir / remote
        dest.parent.mkdir(parents=True, exist_ok=True)
        if remote.endswith("manifest.json"):
            dest.write_text(json.dumps(manifest_dict))
        elif "hashtag_changeset" in remote:
            _write_rollup(dest, latest, rows=10)
        else:
            _write_rollup(dest, latest, rows=1)
        return dest

    return _download


def _patch_manifests(monkeypatch, artifact_dir, remote_frontier, local_frontier):
    manifests = {
        "hf://datasets/test/repo": Manifest(1, _month_start(2005, 4), remote_frontier),
    }
    if local_frontier is not None:
        manifests[str(artifact_dir)] = Manifest(1, _month_start(2005, 4), local_frontier)
    monkeypatch.setattr(refresh, "fetch_manifest", lambda url, timeout=15: manifests.get(url))


def test_noop_when_already_current(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _patch_manifests(monkeypatch, artifact, remote_frontier=_month_start(2026, 8), local_frontier=_month_start(2026, 8))

    def _boom(*args, **kwargs):
        raise AssertionError("must not download when already current")

    monkeypatch.setattr(refresh, "_download", _boom)
    assert refresh.refresh_artifact("test/repo", artifact) is False


def test_happy_path_advances_and_swaps(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_rollup(artifact / "hashtag_changeset.parquet", _month_start(2026, 6), rows=5)
    (artifact / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "min_month": "2005-04", "max_month": "2026-06"})
    )
    new_manifest = {"schema_version": 1, "min_month": "2005-04", "max_month": "2026-07"}
    _patch_manifests(monkeypatch, artifact, remote_frontier=_month_start(2026, 8), local_frontier=_month_start(2026, 7))
    monkeypatch.setattr(
        refresh, "_download", _fake_download(dt.datetime(2026, 7, 31, 23, 59, tzinfo=UTC), new_manifest)
    )

    assert refresh.refresh_artifact("test/repo", artifact) is True
    assert refresh._rollup_bounds(artifact / "hashtag_changeset.parquet")[1] == dt.datetime(
        2026, 7, 31, 23, 59, tzinfo=UTC
    )
    assert (artifact / "users.parquet").exists()
    assert json.loads((artifact / "manifest.json").read_text())["max_month"] == "2026-07"
    assert not (artifact / ".refresh-tmp").exists()


def test_short_rollup_is_rejected_and_live_files_untouched(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_rollup(artifact / "hashtag_changeset.parquet", _month_start(2026, 6), rows=5)
    _patch_manifests(monkeypatch, artifact, remote_frontier=_month_start(2026, 8), local_frontier=_month_start(2026, 7))
    monkeypatch.setattr(
        refresh, "_download", _fake_download(dt.datetime(2026, 6, 15, tzinfo=UTC), {"max_month": "2026-07"})
    )

    with pytest.raises(OsmsgError, match="short of frontier"):
        refresh.refresh_artifact("test/repo", artifact)
    assert refresh._rollup_bounds(artifact / "hashtag_changeset.parquet")[1] == _month_start(2026, 6)


def test_shrunk_rollup_is_rejected(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_rollup(artifact / "hashtag_changeset.parquet", _month_start(2026, 6), rows=50)
    _patch_manifests(monkeypatch, artifact, remote_frontier=_month_start(2026, 8), local_frontier=_month_start(2026, 7))
    monkeypatch.setattr(
        refresh, "_download", _fake_download(dt.datetime(2026, 7, 31, tzinfo=UTC), {"max_month": "2026-07"})
    )

    with pytest.raises(OsmsgError, match="fewer rows"):
        refresh.refresh_artifact("test/repo", artifact)

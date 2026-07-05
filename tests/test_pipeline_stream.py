"""_stream_download_process streams in overlapping windows: full coverage, seq alignment,
download-before-process ordering, and a bounded on-disk footprint (delete-as-you-go)."""

import pathlib

import pytest
import requests

from osmsg import pipeline
from osmsg.exceptions import OsmsgError

_RESULTS = ""
_CACHE = ""


def _init(results_dir: str, cache_dir: str) -> None:
    global _RESULTS, _CACHE
    _RESULTS, _CACHE = results_dir, cache_dir


def _probe(url: str, seq_id: int | None = None) -> None:
    base = url.rsplit("/", 1)[-1]
    raw = pathlib.Path(_CACHE) / base
    existed = raw.exists()
    on_disk = sum(1 for f in pathlib.Path(_CACHE).iterdir() if f.is_file())
    key = seq_id if seq_id is not None else base
    (pathlib.Path(_RESULTS) / f"r_{key}").write_text(f"{url}|existed={existed}|on_disk={on_disk}")
    if existed:
        raw.unlink()  # mimic the worker's per-file erase


def _fake_download(url: str, mode: str = "", cookie: str | None = None, cache_dir=None) -> pathlib.Path:
    path = pathlib.Path(cache_dir) / url.rsplit("/", 1)[-1]
    path.write_text("x")
    return path


def _run(urls, extra, window, workers, cache, results) -> None:
    pipeline._stream_download_process(
        urls,
        mode="changefiles",
        cookie=None,
        cache_dir=pathlib.Path(cache),
        window=window,
        target=_probe,
        initializer=_init,
        init_args=(str(results), str(cache)),
        chunksize=1,
        workers=workers,
        label="files",
        description="files",
        extra_iterable=extra,
    )


def test_streams_covered_aligned_and_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "download_osm_file", _fake_download)
    cache, results = tmp_path / "cache", tmp_path / "results"
    cache.mkdir()
    results.mkdir()

    count, window = 23, 4
    urls = [f"https://x/{i:03d}.osc.gz" for i in range(count)]
    seqs = list(range(1000, 1000 + count))
    _run(urls, seqs, window, 3, cache, results)

    peak = 0
    for url, seq in zip(urls, seqs):
        body = (results / f"r_{seq}").read_text()
        assert body.startswith(url + "|"), f"seq {seq} misaligned: {body}"
        assert "existed=True" in body, f"processed before download: {body}"
        peak = max(peak, int(body.split("on_disk=")[1]))

    assert len(list(results.iterdir())) == count, "every file must be processed exactly once"
    assert peak <= 2 * window, f"footprint {peak} exceeds one prefetch window ahead ({2 * window})"
    assert not list(cache.iterdir()), "raw files must be erased as they are processed"


def test_streams_without_extra_iterable(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "download_osm_file", _fake_download)
    cache, results = tmp_path / "cache", tmp_path / "results"
    cache.mkdir()
    results.mkdir()
    urls = [f"https://x/{i:03d}.osm.gz" for i in range(15)]
    _run(urls, None, 100, 2, cache, results)
    assert len(list(results.iterdir())) == 15


def test_single_window(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "download_osm_file", _fake_download)
    cache, results = tmp_path / "cache", tmp_path / "results"
    cache.mkdir()
    results.mkdir()
    urls = [f"https://x/{i:03d}.osc.gz" for i in range(3)]
    _run(urls, [0, 1, 2], 8, 2, cache, results)
    assert len(list(results.iterdir())) == 3


def test_network_error_is_wrapped(monkeypatch, tmp_path):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectTimeout("down")

    monkeypatch.setattr(pipeline, "download_osm_file", boom)
    with pytest.raises(OsmsgError, match="Re-run to resume"):
        _run(["https://x/1.osc.gz"], [1], 4, 1, tmp_path / "cache", tmp_path)


def test_stream_window_sizes_by_file_weight():
    assert pipeline._stream_window("https://planet/replication/day") == 4
    assert pipeline._stream_window("https://planet/replication/hour") == 24
    assert pipeline._stream_window("https://planet/replication/minute") == 100
    assert pipeline._stream_window("https://planet/replication/changesets/") == 100

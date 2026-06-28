"""Streaming download + atomic-rename behaviour."""

from __future__ import annotations

import gzip
import io
from pathlib import Path

import pytest

from osmsg import fetch


class _StreamingResponse:
    def __init__(self, body: bytes) -> None:
        self.raw = io.BytesIO(body)
        self.raw.decode_content = False  # set by fetch; tracked here for parity

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.raw.close()
        return False

    def raise_for_status(self):
        return None


@pytest.fixture
def fake_session(monkeypatch):
    by_url: dict[str, bytes] = {}

    def install(url: str, raw_body: bytes) -> None:
        by_url[url] = raw_body

    def fake_get(url: str, headers=None, stream: bool = False, **kwargs):  # noqa: ARG001
        if url not in by_url:
            raise AssertionError(f"unexpected URL fetched: {url}")
        return _StreamingResponse(by_url[url])

    monkeypatch.setattr(fetch.session, "get", fake_get)
    return install


def _make_gz(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(payload)
    return buf.getvalue()


def test_streaming_download_writes_decompressed_bytes(tmp_path: Path, fake_session):
    url = "https://example.com/replication/000/000/001.osc.gz"
    payload = b"<osmChange/>\n"
    fake_session(url, _make_gz(payload))

    out = fetch.download_osm_file(url, mode="changefiles", cache_dir=tmp_path)
    assert out.exists()
    assert out.read_bytes() == payload
    # Final file is at the canonical path with no .gz suffix; no .partial residue.
    assert not out.with_suffix(out.suffix + ".partial").exists()


def test_existing_partial_is_cleaned_then_redownloaded(tmp_path: Path, fake_session):
    """An interrupted prior run leaves a .partial file; next run must not reuse it."""
    url = "https://example.com/replication/000/000/002.osc.gz"
    payload = b"hello world\n"
    fake_session(url, _make_gz(payload))

    raw_path = fetch.file_path_for(url, "changefiles", tmp_path).with_suffix("")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    partial = raw_path.with_suffix(raw_path.suffix + ".partial")
    partial.write_bytes(b"corrupted leftover")

    out = fetch.download_osm_file(url, mode="changefiles", cache_dir=tmp_path)
    assert out.read_bytes() == payload
    assert not partial.exists()


def test_existing_decompressed_file_short_circuits(tmp_path: Path, fake_session):
    url = "https://example.com/replication/000/000/003.osc.gz"
    raw_path = fetch.file_path_for(url, "changefiles", tmp_path).with_suffix("")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"already cached")
    # Note: fake_session install NOT called for this URL, fake_get would AssertionError if hit.

    out = fetch.download_osm_file(url, mode="changefiles", cache_dir=tmp_path)
    assert out.read_bytes() == b"already cached"


def test_download_failure_leaves_no_partial(tmp_path: Path, monkeypatch):
    """A mid-decompress error must not leave a half-written .partial that a future run reuses as final."""
    url = "https://example.com/replication/000/000/004.osc.gz"
    raw_path = fetch.file_path_for(url, "changefiles", tmp_path).with_suffix("")

    class _BrokenResponse:
        def __init__(self):
            # Not valid gzip, GzipFile will raise on read.
            self.raw = io.BytesIO(b"not gzip data")
            self.raw.decode_content = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

    monkeypatch.setattr(fetch.session, "get", lambda *a, **kw: _BrokenResponse())

    with pytest.raises((OSError, EOFError, gzip.BadGzipFile)):
        fetch.download_osm_file(url, mode="changefiles", cache_dir=tmp_path)

    partial = raw_path.with_suffix(raw_path.suffix + ".partial")
    assert not partial.exists()
    assert not raw_path.exists()

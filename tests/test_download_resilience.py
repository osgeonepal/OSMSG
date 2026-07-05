"""Downloads stay polite and a network failure becomes a clear, resumable error."""

import pytest
import requests

from osmsg import pipeline
from osmsg.exceptions import OsmsgError


def test_download_concurrency_is_capped():
    assert pipeline._DOWNLOAD_WORKERS <= 4


def test_stream_download_wraps_network_error(monkeypatch, tmp_path):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectTimeout("planet timed out")

    monkeypatch.setattr(pipeline, "download_osm_file", boom)
    with pytest.raises(OsmsgError, match="Re-run to resume"):
        pipeline._stream_download_process(
            ["https://planet.openstreetmap.org/replication/changesets/007/035/882.osm.gz"],
            mode="changeset",
            cookie=None,
            cache_dir=tmp_path,
            window=250,
            target=int,
            initializer=None,
            init_args=(),
            chunksize=1,
            workers=1,
            label="changesets",
            description="Changesets",
        )

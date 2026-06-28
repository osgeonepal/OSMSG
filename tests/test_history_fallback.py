"""A failed remote history read fails loud instead of attempting an infeasible full live crawl."""

import datetime as dt

import duckdb
import pytest

from osmsg import pipeline
from osmsg.exceptions import OsmsgError
from osmsg.pipeline import RunConfig, run
from tests.test_history import _build_dataset

UTC = dt.UTC


def test_remote_ingest_failure_fails_loud(tmp_path, monkeypatch):
    _build_dataset(tmp_path)

    def boom(*args, **kwargs):
        raise duckdb.IOException("remote down")

    monkeypatch.setattr(pipeline, "ingest_remote", boom)
    with pytest.raises(OsmsgError, match="Reading the published history failed"):
        run(
            RunConfig(
                name="x",
                history_url=str(tmp_path),
                start_date=dt.datetime(2024, 1, 1, tzinfo=UTC),
                end_date=dt.datetime(2024, 1, 31, tzinfo=UTC),
                output_dir=tmp_path / "o",
            )
        )

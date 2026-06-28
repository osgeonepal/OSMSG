"""Store reuse: a rerun that only changes the output format re-exports from <name>.duckdb instead of
refetching; any query-param change or --overwrite recomputes. Fingerprint stamped per query."""

import datetime as dt

import pytest

from osmsg import pipeline
from osmsg.pipeline import RunConfig, _query_fingerprint, run
from tests.test_history import _build_dataset

UTC = dt.UTC
START = dt.datetime(2024, 1, 1, tzinfo=UTC)
END = dt.datetime(2024, 1, 31, tzinfo=UTC)


def _cfg(tmp_path, **over):
    base = dict(
        name="r",
        history_url=str(tmp_path),
        start_date=START,
        end_date=END,
        output_dir=tmp_path / "store",
        formats=["parquet"],
    )
    base.update(over)
    return RunConfig(**base)


def _block_refetch(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("refetched instead of reusing")

    monkeypatch.setattr(pipeline, "ingest_remote", boom)


def test_fingerprint_ignores_format_but_tracks_params():
    a = RunConfig(start_date=START, end_date=END, formats=["parquet"])
    b = RunConfig(start_date=START, end_date=END, formats=["csv", "json"])
    assert _query_fingerprint(a) == _query_fingerprint(b)
    assert _query_fingerprint(a) != _query_fingerprint(RunConfig(start_date=START, end_date=END, hashtags=["#x"]))
    assert _query_fingerprint(a) != _query_fingerprint(
        RunConfig(start_date=START, end_date=dt.datetime(2024, 1, 20, tzinfo=UTC))
    )


def test_rerun_new_format_reuses_without_refetch(tmp_path, monkeypatch):
    _build_dataset(tmp_path)
    first = run(_cfg(tmp_path))
    assert first["rows"] > 0
    _block_refetch(monkeypatch)
    second = run(_cfg(tmp_path, formats=["csv"]))
    assert (tmp_path / "store" / "r.csv").exists()
    assert second["rows"] == first["rows"]


def test_overwrite_forces_recompute(tmp_path, monkeypatch):
    _build_dataset(tmp_path)
    run(_cfg(tmp_path))
    _block_refetch(monkeypatch)
    with pytest.raises(AssertionError, match="refetched"):
        run(_cfg(tmp_path, formats=["csv"], overwrite=True))


def test_param_change_recomputes(tmp_path, monkeypatch):
    _build_dataset(tmp_path)
    run(_cfg(tmp_path))
    _block_refetch(monkeypatch)
    with pytest.raises(AssertionError, match="refetched"):
        run(_cfg(tmp_path, end_date=dt.datetime(2024, 1, 20, tzinfo=UTC)))

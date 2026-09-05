import datetime as dt
from pathlib import Path

import pytest

from osmsg import workers
from osmsg.fetch import file_path_for

_URL = "https://planet.osm.org/replication/minute/000/001/234.osc.gz"


def _cfg(cache_dir: Path, parquet_dir: Path) -> dict:
    return {
        "cache_dir": str(cache_dir),
        "parquet_dir": str(parquet_dir),
        "start_date_utc": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "whitelisted_users": None,
        "tag_mode": "none",
    }


def test_changefile_parse_failure_raises_and_writes_nothing(tmp_path):
    """A corrupt diff must raise so the tick aborts before resume advances; it must not flush a partial
    (undercounted) batch and mark the sequence processed."""
    cache_dir, parquet_dir = tmp_path / "cache", tmp_path / "parquet"
    raw_path = file_path_for(_URL, "changefiles", cache_dir).with_suffix("")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text("not valid osm xml <<<")
    parquet_dir.mkdir()

    workers.init_changefile_worker(None, _cfg(cache_dir, parquet_dir))
    with pytest.raises(RuntimeError):
        workers.process_changefile(_URL, 1)
    assert list(parquet_dir.glob("*.parquet")) == []

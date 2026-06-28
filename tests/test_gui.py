"""GUI config mapping: form fields to RunConfig, with validation. No window is opened."""

import datetime as dt

import pytest

from osmsg.exceptions import OsmsgError
from osmsg.gui import ABOUT_LINKS, PRESETS, build_config, preset_range

UTC = dt.UTC
NOW = dt.datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


def test_build_config_maps_fields(tmp_path):
    cfg = build_config(
        {
            "name": "mh",
            "start": "2024-01-01",
            "end": "2024-02-01",
            "hashtags": "2024_MH_ECU, hotosm",
            "tags": "building,highway",
            "all_tags": True,
            "summary": True,
            "parquet": True,
            "csv": True,
        },
        str(tmp_path),
    )
    assert cfg.name == "mh"
    assert cfg.start_date == dt.datetime(2024, 1, 1, tzinfo=UTC)
    assert cfg.end_date == dt.datetime(2024, 2, 1, tzinfo=UTC)
    assert cfg.hashtags == ["2024_MH_ECU", "hotosm"]
    assert cfg.additional_tags == ["building", "highway"]
    assert cfg.tag_mode == "all"
    assert cfg.summary is True
    assert cfg.formats == ["parquet", "csv"]


def test_build_config_blank_end_is_none(tmp_path):
    cfg = build_config({"start": "2024-01-01", "parquet": True}, str(tmp_path))
    assert cfg.end_date is None
    assert cfg.tag_mode == "none"


def test_build_config_parses_workers(tmp_path):
    cfg = build_config({"start": "2024-01-01", "parquet": True, "workers": "8"}, str(tmp_path))
    assert cfg.workers == 8


def test_build_config_blank_workers_is_none(tmp_path):
    cfg = build_config({"start": "2024-01-01", "parquet": True, "workers": ""}, str(tmp_path))
    assert cfg.workers is None


def test_build_config_rejects_bad_workers(tmp_path):
    with pytest.raises(OsmsgError, match="Workers"):
        build_config({"start": "2024-01-01", "parquet": True, "workers": "0"}, str(tmp_path))


def test_build_config_requires_start(tmp_path):
    with pytest.raises(OsmsgError, match="Start date"):
        build_config({"parquet": True}, str(tmp_path))


def test_build_config_requires_format(tmp_path):
    with pytest.raises(OsmsgError, match="format"):
        build_config({"start": "2024-01-01"}, str(tmp_path))


def test_build_config_rejects_bad_date(tmp_path):
    with pytest.raises(OsmsgError, match="date"):
        build_config({"start": "01/01/2024", "parquet": True}, str(tmp_path))


def test_preset_range_rolling_windows():
    assert preset_range("Last hour", NOW) == (NOW - dt.timedelta(hours=1), NOW)
    assert preset_range("Last day", NOW) == (NOW - dt.timedelta(days=1), NOW)
    assert preset_range("Last month", NOW) == (NOW - dt.timedelta(days=30), NOW)
    assert preset_range("All time", NOW) == (dt.datetime(2005, 1, 1, tzinfo=UTC), NOW)


def test_every_preset_resolves():
    for name in PRESETS:
        start, end = preset_range(name, NOW)
        assert start < end


def test_about_links():
    urls = {url for _label, url in ABOUT_LINKS}
    assert "https://github.com/osgeonepal/osmsg" in urls
    assert "https://github.com/osgeonepal/osmsg/issues" in urls
    assert "https://github.com/sponsors/kshitijrajsharma" in urls

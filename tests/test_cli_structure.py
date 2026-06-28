"""CLI structure: the root still runs without a subcommand after the callback conversion, the maintain
subcommands are registered, and --insert / --psql-dsn build the expected RunConfig."""

import click
import pytest
from typer.testing import CliRunner

import osmsg.cli as cli
from osmsg.cli import app

runner = CliRunner()

_FAKE_RESULT = {"rows": 0, "files": {}, "rows_data": [], "summary": None, "start_seq": None, "end_seq": None}


@pytest.fixture
def captured_cfg(monkeypatch):
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return _FAKE_RESULT

    monkeypatch.setattr(cli, "run", fake_run)
    return captured


def test_root_runs_without_subcommand(captured_cfg):
    result = runner.invoke(app, ["--last", "hour"])
    assert result.exit_code == 0
    assert captured_cfg["cfg"].start_date is not None


def test_psql_dsn_implies_psql_format(captured_cfg):
    result = runner.invoke(app, ["--last", "hour", "--psql-dsn", "host=localhost dbname=osm"])
    assert result.exit_code == 0
    cfg = captured_cfg["cfg"]
    assert "psql" in cfg.formats and cfg.psql_dsn == "host=localhost dbname=osm"


def test_insert_flag_builds_insert_config(captured_cfg):
    result = runner.invoke(app, ["--insert"])
    assert result.exit_code == 0
    assert captured_cfg["cfg"].insert is True


def test_insert_rejects_update():
    assert runner.invoke(app, ["--insert", "--update"]).exit_code == 2


def test_osh_file_requires_changeset_file():
    assert runner.invoke(app, ["--insert", "--osh-file", "x.osh.pbf"]).exit_code == 2


def test_osh_file_requires_insert():
    assert runner.invoke(app, ["--osh-file", "x.osh.pbf", "--changeset-file", "c.osm.bz2"]).exit_code == 2


def test_psql_bulk_rejects_update():
    assert runner.invoke(app, ["--update", "--psql-bulk"]).exit_code == 2


def test_maintain_subcommands_present():
    result = runner.invoke(app, ["maintain", "--help"])
    assert result.exit_code == 0
    plain = click.unstyle(result.stdout)
    for command in ("month", "convert", "publish"):
        assert command in plain

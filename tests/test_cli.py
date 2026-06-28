"""typer CliRunner sanity tests for major flag combinations.

These don't exercise the OSM network, they verify the parser wiring, period
resolution, mutual-exclusion checks, and error paths.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from osmsg.__version__ import __version__
from osmsg.cli import app

runner = CliRunner()


def test_version_flag_prints_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_core_options():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    plain = click.unstyle(result.stdout)
    for fragment in ("--last", "--country", "--format", "--update", "--psql-dsn"):
        assert fragment in plain


@pytest.mark.parametrize(
    "args",
    [
        ["--last", "hour", "--days", "3"],
        ["--start", "2026-04-01", "--last", "hour"],
        ["--start", "2026-04-01", "--days", "3"],
    ],
)
def test_time_range_flags_are_mutually_exclusive(args):
    result = runner.invoke(app, args)
    assert result.exit_code == 2


def test_changeset_flag_is_hidden_in_help():
    """The bare --changeset toggle is internal (set automatically when needed). Sibling
    flags that legitimately start with --changeset- (e.g. --changeset-pad-hours) are
    user-facing and may appear; only the bare toggle must stay hidden."""
    result = runner.invoke(app, ["--help"])
    plain = click.unstyle(result.stdout)
    # Match the bare flag with a trailing space or end-of-arg, not its --changeset-* siblings.
    assert not re.search(r"--changeset(\s|,|$)", plain), "bare --changeset toggle leaked into --help"


def test_password_flag_no_longer_accepted():
    result = runner.invoke(app, ["--password", "secret", "--last", "hour"])
    assert result.exit_code != 0


def test_yaml_config_is_loaded(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: yaml_check\nlast: decade\n")
    result = runner.invoke(app, ["--config", str(cfg)])
    assert result.exit_code == 2  # invalid `last` value from yaml is caught by typer's enum validation


def test_cli_overrides_yaml(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: from_yaml\nlast: hour\n")
    # --last decade is invalid; if CLI override works, it preempts the yaml value and we see the enum error.
    result = runner.invoke(app, ["--config", str(cfg), "--last", "decade"])
    assert result.exit_code == 2


def test_negative_days_rejected():
    result = runner.invoke(app, ["--days", "0"])
    assert result.exit_code == 2


def test_no_dates_no_update_errors_out():
    result = runner.invoke(app, [])
    # Without --start / --last / --days / --update, pipeline raises SystemExit.
    assert result.exit_code != 0


def test_update_rejects_explicit_window():
    """--update resumes from state and runs to head; explicit window flags must error out
    so a desync between state seq and the requested timestamp can't reintroduce stat loss."""
    for extra in (["--end", "2026-01-01"], ["--start", "2026-01-01"], ["--last", "hour"], ["--days", "1"]):
        result = runner.invoke(app, ["--update", *extra])
        assert result.exit_code == 2, f"--update {extra} should be rejected; got {result.exit_code}"
        plain = click.unstyle(result.stderr) if result.stderr else click.unstyle(result.stdout)
        assert "update" in plain.lower(), f"error message must mention --update; got: {plain}"


def test_invalid_period_value_rejected():
    result = runner.invoke(app, ["--last", "decade"])
    assert result.exit_code == 2


def test_psql_format_without_dsn_fails_fast():
    """-f psql without --psql-dsn must fail at parse time, not 30 minutes into processing."""
    result = runner.invoke(app, ["-f", "psql", "--last", "hour"])
    assert result.exit_code == 2
    plain = click.unstyle(result.stderr) if result.stderr else click.unstyle(result.stdout)
    assert "psql" in plain.lower() and "--psql-dsn" in plain


def test_password_stdin_with_empty_input_rejected():
    result = runner.invoke(app, ["--password-stdin", "--last", "hour"], input="\n")
    assert result.exit_code == 2


def test_hashtags_help_string_is_present():
    """Regression: --hashtags used to ship without a help string."""
    result = runner.invoke(app, ["--help"])
    plain = click.unstyle(result.stdout)
    assert "--hashtags" in plain
    # The help text mentions either substring or exact-lookup behaviour.
    assert "hashtag" in plain.lower()


@pytest.fixture
def stub_run(monkeypatch):
    captured: dict = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return {
            "rows": 0,
            "files": {},
            "rows_data": [],
            "summary": None,
            "start_seq": None,
            "end_seq": None,
        }

    import osmsg.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run", fake_run)
    return captured


@pytest.mark.parametrize(
    "args",
    [
        ["--tags", "building"],
        ["--tags", "building", "--tags", "highway"],
        ["--length", "highway"],
        ["--length", "highway", "--length", "waterway"],
        ["--users", "alice"],
        ["--users", "alice", "--users", "bob"],
        ["--hashtags", "mapathon"],
        ["--hashtags", "mapathon", "--exact-lookup"],
        ["--keys"],
        ["--all"],
        ["--workers", "2"],
        ["--rows", "5"],
        ["--name", "myrun"],
        ["--country", "nepal"],
        ["--country", "nepal", "--country", "india"],
        ["--url", "minute"],
        ["--url", "https://example.com/replication/minute"],
        ["-f", "csv"],
        ["-f", "json"],
        ["-f", "markdown"],
        ["-f", "parquet", "-f", "csv", "-f", "json"],
        ["--summary"],
        ["--tm-stats", "--hashtags", "hotosm-project-1"],
        ["--delete-temp"],
        ["--username", "alice"],
    ],
)
def test_cli_arg_parser_accepts(args, tmp_path, stub_run):
    full = [*args, "--last", "hour", "--cache-dir", str(tmp_path)]
    result = runner.invoke(app, full)
    assert result.exit_code == 0, f"args {args} failed: stdout={result.stdout!r} stderr={result.stderr!r}"


def test_cli_passes_repeated_tags_through_to_runconfig(stub_run, tmp_path):
    runner.invoke(app, ["--last", "hour", "--tags", "building", "--tags", "highway", "--cache-dir", str(tmp_path)])
    cfg = stub_run["cfg"]
    assert cfg.additional_tags == ["building", "highway"]


def test_cli_passes_users_filter_through(stub_run, tmp_path):
    runner.invoke(app, ["--last", "hour", "--users", "alice", "--users", "bob", "--cache-dir", str(tmp_path)])
    cfg = stub_run["cfg"]
    assert cfg.users_filter == ["alice", "bob"]


def test_cli_passes_country_through(stub_run, tmp_path):
    runner.invoke(app, ["--last", "hour", "--country", "nepal", "--cache-dir", str(tmp_path)])
    cfg = stub_run["cfg"]
    assert cfg.countries == ["nepal"]


def test_cli_format_default_is_parquet(stub_run, tmp_path):
    runner.invoke(app, ["--last", "hour", "--cache-dir", str(tmp_path)])
    cfg = stub_run["cfg"]
    assert cfg.formats == ["parquet"]


def test_cli_multi_format_parsed(stub_run, tmp_path):
    runner.invoke(app, ["--last", "hour", "-f", "csv", "-f", "json", "-f", "markdown", "--cache-dir", str(tmp_path)])
    cfg = stub_run["cfg"]
    assert set(cfg.formats) == {"csv", "json", "markdown"}


def test_cli_start_end_dates_parsed(stub_run, tmp_path):
    runner.invoke(
        app,
        [
            "--start",
            "2026-04-01 00:00:00",
            "--end",
            "2026-04-08 00:00:00",
            "--cache-dir",
            str(tmp_path),
        ],
    )
    cfg = stub_run["cfg"]
    assert cfg.start_date is not None and cfg.start_date.year == 2026
    assert cfg.end_date is not None and cfg.end_date.day == 8


def test_cli_days_resolved_to_window(stub_run, tmp_path):
    runner.invoke(app, ["--days", "3", "--cache-dir", str(tmp_path)])
    cfg = stub_run["cfg"]
    delta = cfg.end_date - cfg.start_date
    assert delta.days == 3


def test_yaml_config_with_repeated_options(stub_run, tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"name: yaml_repeats\nlast: hour\ntags: [building, highway]\nusers: [alice]\ncache_dir: {tmp_path}\n"
    )
    result = runner.invoke(app, ["--config", str(cfg)])
    assert result.exit_code == 0
    parsed = stub_run["cfg"]
    assert parsed.additional_tags == ["building", "highway"]
    assert parsed.users_filter == ["alice"]
    assert parsed.name == "yaml_repeats"

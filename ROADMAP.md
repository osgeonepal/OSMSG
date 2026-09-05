# Roadmap

osmsg (OpenStreetMap Stats Generator) turns OSM history into per-user, per-hashtag statistics. It started
as a small command-line tool to monitor mapathons at the user level and has grown into a CLI, a Python
library, an API, and a hosted leaderboard.

## Background

- 2022: development started, focused on user-level hashtag statistics for mapathons.
- 2024: development paused.
- 2026: development resumed, supported by an OpenStreetMap Foundation microgrant, with contributors from
  OSGeo Nepal and OSM Nepal.

## Shipped

- Hashtag user-stats leaderboard aggregation (summary, leaderboard, tags, editors).
- Cloud-native history: the full OSM history is published as time-partitioned parquet on Hugging Face and
  read remotely, so past windows are served without re-downloading diffs.
- Hybrid history read: covered months come from the published dataset, the recent tail from live diffs,
  merged at the frontier.
- Two-minute replication with automatic granularity handoff (day to hour to minute) as the backlog shrinks.
- Storage choices: DuckDB and Postgres, plus parquet, csv, json, and markdown exports.
- API (v2) with time windows, multi-hashtag union, trending hashtags, per-editor stats, and a map endpoint.
- Web leaderboard UI and a Windows desktop application.
- Exact-match hashtag search alongside prefix search.
- Monthly dataset maintenance (`osmsg maintain`) run from GitHub Actions.

## Planned

- Reliability hardening: fail loud on unparsable diffs instead of writing partial counts, make `/health`
  report the database state, and normalize timezone-less API query parameters to UTC.
- Packaging safeguard: a CI check that installs the built wheel and imports every module before publish.
- Distribution: publish to conda-forge and a Homebrew tap.
- Documentation: a generic "deploy on your own infrastructure" guide, so the self-hosting path does not
  assume the reference deployment's layout.
- Compare hashtags in the UI and a live tracking view.
- Performance work for the largest hashtags.

## Get involved

- Star the repository and open issues or pull requests.
- Join the OSGeo Nepal community.
- Contact: info@osgeonepal.org.

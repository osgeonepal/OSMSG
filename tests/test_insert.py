"""--insert loads history into the store (published dataset dir or a local .osh) and seeds resume
state so a later --update continues. Seeding the replication sequence needs the network, so it is
stubbed here; the timestamp-to-sequence path itself is covered in test_history."""

import datetime as dt

import duckdb

from osmsg import pipeline
from osmsg.pipeline import RunConfig, run
from osmsg.replication import SHORTCUTS
from tests.test_history import _build_dataset
from tests.test_maintain_convert import CHANGESET_DUMP, _build_history

UTC = dt.UTC


def _record_seeds(monkeypatch):
    calls = []

    def fake_seed(conn, resume_at, url):
        calls.append((resume_at, url))
        return resume_at

    monkeypatch.setattr(pipeline, "seed_resume_at", fake_seed)
    return calls


def test_insert_remote_populates_store_and_seeds(tmp_path, monkeypatch):
    calls = _record_seeds(monkeypatch)
    _build_dataset(tmp_path)
    out = tmp_path / "store"
    result = run(RunConfig(name="ins", insert=True, history_url=str(tmp_path), output_dir=out, urls=["minute"]))

    assert result["rows"] == 3
    db = duckdb.connect(str(out / "ins.duckdb"), read_only=True)
    assert db.execute("SELECT count(*) FROM changeset_stats WHERE seq_id=0").fetchone()[0] == 3
    assert calls and calls[0][0] == dt.datetime(2024, 1, 31, tzinfo=UTC)
    assert calls[0][1] == SHORTCUTS["day"]


def test_insert_remote_slice_resumes_at_window_end(tmp_path, monkeypatch):
    calls = _record_seeds(monkeypatch)
    _build_dataset(tmp_path)
    out = tmp_path / "store"
    run(
        RunConfig(
            name="ins",
            insert=True,
            history_url=str(tmp_path),
            output_dir=out,
            urls=["minute"],
            start_date=dt.datetime(2024, 1, 1, tzinfo=UTC),
            end_date=dt.datetime(2024, 1, 16, tzinfo=UTC),
        )
    )
    db = duckdb.connect(str(out / "ins.duckdb"), read_only=True)
    assert db.execute("SELECT count(*) FROM changeset_stats WHERE seq_id=0").fetchone()[0] == 2
    assert calls[0][0] == dt.datetime(2024, 1, 15, tzinfo=UTC)


def test_insert_local_files_populates_store_and_seeds(tmp_path, monkeypatch):
    calls = _record_seeds(monkeypatch)
    osh = tmp_path / "h.osh.pbf"
    dump = tmp_path / "c.osm"
    _build_history(str(osh))
    dump.write_text(CHANGESET_DUMP)
    out = tmp_path / "store"
    result = run(
        RunConfig(
            name="ins",
            insert=True,
            osh_file=str(osh),
            changeset_file=str(dump),
            start_date=dt.datetime(2021, 1, 1, tzinfo=UTC),
            end_date=dt.datetime(2025, 1, 1, tzinfo=UTC),
            output_dir=out,
            cache_dir=tmp_path / "cache",
        )
    )

    assert result["rows"] == 3
    db = duckdb.connect(str(out / "ins.duckdb"), read_only=True)
    assert db.execute("SELECT count(*) FROM changeset_stats WHERE seq_id=0").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM changesets").fetchone()[0] == 3
    assert calls[0][0] == dt.datetime(2024, 12, 31, tzinfo=UTC)

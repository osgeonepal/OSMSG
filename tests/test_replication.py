"""ChangesetReplication URL math: backward-pad behavior and the resume-seq fast path.

The pad covers still-open changesets opened before window start whose first edits
land inside the window. OSM caps changeset open time at 24h, so 24h is the maximum
useful pad. Default is 1h to keep first bootstraps cheap; --update runs skip the
pad entirely once they have prior state.
"""

from __future__ import annotations

import datetime as dt

import pytest

from osmsg.replication import ChangesetReplication


def _make_repl(monkeypatch, pad_hours: int | None = None):
    """Stub the network: 1 sequence == 1 minute, anchored at a fixed cur_seq/last_run."""
    cur_seq = 1_000_000
    last_run = dt.datetime(2026, 4, 27, 22, 0, tzinfo=dt.UTC)
    r = ChangesetReplication() if pad_hours is None else ChangesetReplication(pad_hours=pad_hours)

    def fake_state():
        return cur_seq, last_run

    def fake_seq_to_ts(seq):
        return last_run + dt.timedelta(minutes=(seq - cur_seq))

    monkeypatch.setattr(r, "_state", fake_state)
    monkeypatch.setattr(r, "sequence_to_timestamp", fake_seq_to_ts)
    return r, cur_seq, last_run


@pytest.fixture
def repl(monkeypatch):
    return _make_repl(monkeypatch)


def _backward_pad(repl_tuple, start, end):
    """Run download_urls and return how far back of `start` the first seq lands."""
    r, cur_seq, last_run = repl_tuple
    _, start_seq, _ = r.download_urls(start, end)
    return start - (last_run + dt.timedelta(minutes=(start_seq - cur_seq)))


def test_default_pad_is_one_hour(repl):
    pad = _backward_pad(
        repl,
        dt.datetime(2026, 4, 27, 21, 4, tzinfo=dt.UTC),
        dt.datetime(2026, 4, 27, 21, 54, tzinfo=dt.UTC),
    )
    assert dt.timedelta(hours=1) <= pad < dt.timedelta(hours=2)


def test_pad_hours_24_extends_backward_to_full_24h(monkeypatch):
    """Opt-in 24h pad for first runs that must capture every long-running open changeset."""
    repl_tuple = _make_repl(monkeypatch, pad_hours=24)
    pad = _backward_pad(
        repl_tuple,
        dt.datetime(2026, 4, 27, 21, 4, tzinfo=dt.UTC),
        dt.datetime(2026, 4, 27, 21, 54, tzinfo=dt.UTC),
    )
    assert pad >= dt.timedelta(hours=24)


def test_download_urls_caps_end_at_cur_seq(repl):
    """end_date past server head clamps to cur_seq instead of requesting non-existent files."""
    r, cur_seq, _ = repl
    _, _, end_seq = r.download_urls(
        dt.datetime(2026, 4, 27, 21, 0, tzinfo=dt.UTC),
        dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
    )
    assert end_seq <= cur_seq


def test_resume_seq_skips_backward_pad(monkeypatch):
    """--update fast path: prior state already covers history, so the pad is redundant
    even when pad_hours=24 is configured."""
    r, cur_seq, _ = _make_repl(monkeypatch, pad_hours=24)
    last_seq = cur_seq - 30
    urls, start_seq, end_seq = r.download_urls(
        dt.datetime(2026, 4, 27, 21, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 4, 27, 21, 30, tzinfo=dt.UTC),
        resume_seq=last_seq + 1,
    )
    assert start_seq == last_seq + 1
    assert len(urls) == end_seq - start_seq + 1
    assert len(urls) < 60


@pytest.fixture
def changefile_repl(monkeypatch):
    """Offline ReplicationServer stub; 1 sequence == 1 minute."""
    from osmsg import replication as _repl_mod

    cur_seq = 5_000_000
    cur_ts = dt.datetime(2026, 5, 7, 22, 0, tzinfo=dt.UTC)

    def fake_seq_to_ts(_state_url):
        # The state_url encodes the seq; here we just bind to cur_ts/cur_seq via a closure
        # over the call sequence. Simpler: read the seq from the URL pattern.
        import re

        m = re.search(r"(\d{3})/(\d{3})/(\d{3})", _state_url)
        if not m:
            return cur_ts
        seq = int(m.group(1)) * 1_000_000 + int(m.group(2)) * 1_000 + int(m.group(3))
        return cur_ts + dt.timedelta(minutes=(seq - cur_seq))

    monkeypatch.setattr(_repl_mod, "seq_to_timestamp", fake_seq_to_ts)

    class FakeReplicationServer:
        def __init__(self, _base_url):
            pass

        def timestamp_to_sequence(self, ts):
            # floor division to match osmium's "seq whose state timestamp <= ts" semantics
            return cur_seq + int((ts - cur_ts).total_seconds() // 60)

        def get_state_url(self, seq):
            a, b, c = seq // 1_000_000, (seq // 1_000) % 1_000, seq % 1_000
            return f"https://planet.openstreetmap.org/replication/minute/{a:03d}/{b:03d}/{c:03d}.state.txt"

        def get_diff_url(self, seq):
            return self.get_state_url(seq).replace(".state.txt", ".osc.gz")

        def get_state_info(self):
            return cur_seq, cur_ts

    monkeypatch.setattr(_repl_mod, "ReplicationServer", FakeReplicationServer)
    return cur_seq, cur_ts


def test_changefile_download_urls_resume_seq_skips_backward_pad(changefile_repl):
    """resume_seq must be used exactly, no 60-minute backward pad."""
    from osmsg.replication import changefile_download_urls

    cur_seq, cur_ts = changefile_repl
    last_seq = cur_seq - 30  # tick processed up to 30 minutes ago
    end = cur_ts

    urls, _server_ts, start_seq, _end_seq, _, _ = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=30),
        end_date=end,
        base_url="https://planet.openstreetmap.org/replication/minute",
        resume_seq=last_seq + 1,
    )

    assert start_seq == last_seq + 1
    # Without resume_seq, the backward pad would have produced ~60 extra URLs.
    # With resume_seq, we get only the genuinely new diffs from last_seq+1 onward.
    assert len(urls) <= 60  # very loose upper bound; the point is "no 60 backpad"


def test_changefile_download_urls_no_resume_seq_pads_backward(changefile_repl):
    """First-run path (no --update) keeps the 60-minute backward pad on minute replication."""
    from osmsg.replication import changefile_download_urls

    cur_seq, cur_ts = changefile_repl

    # 30 seconds offset from a seq boundary so timestamp_to_sequence rounds down
    # and the backward-pad branch actually runs (it requires start_date > seq_ts).
    _, _, start_seq, _, _, _ = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=10) + dt.timedelta(seconds=30),
        end_date=cur_ts,
        base_url="https://planet.openstreetmap.org/replication/minute",
    )
    expected_unpadded = cur_seq - 10
    assert start_seq <= expected_unpadded - 50, (
        f"expected backward pad of ~60, got start_seq={start_seq} (unpadded would be {expected_unpadded})"
    )


def test_changefile_forward_pad_is_exactly_one_when_end_not_aligned(changefile_repl):
    """+1 fires when end_date falls strictly inside a diff's state_ts range, so the diff
    holding edits in (state_ts(end_seq), end_date] is included."""
    from osmsg.replication import changefile_download_urls

    cur_seq, cur_ts = changefile_repl
    # 30s past a seq boundary so end_seq's state_ts < end_date.
    end_date = cur_ts - dt.timedelta(minutes=30) + dt.timedelta(seconds=30)

    _urls, _server_ts, _start, last_seq, _, _ = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=60),
        end_date=end_date,
        base_url="https://planet.openstreetmap.org/replication/minute",
    )
    # In the stub: 1 seq == 1 minute. timestamp_to_sequence floors to cur_seq - 30; +1.
    assert last_seq == cur_seq - 29, f"expected end_seq + 1 = {cur_seq - 29}, got {last_seq}"


def test_changefile_forward_pad_zero_when_end_aligned(changefile_repl):
    """When end_date == state_ts(end_seq) exactly, no +1: the diff at end_seq already
    covers every edit up to end_date."""
    from osmsg.replication import changefile_download_urls

    cur_seq, cur_ts = changefile_repl
    end_date = cur_ts - dt.timedelta(minutes=30)  # exactly on a seq boundary

    _, _, _, last_seq, _, _ = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=60),
        end_date=end_date,
        base_url="https://planet.openstreetmap.org/replication/minute",
    )
    assert last_seq == cur_seq - 30, f"expected end_seq exactly = {cur_seq - 30}, got {last_seq}"


def test_changefile_forward_pad_clamps_at_server_head(changefile_repl):
    """When end_seq + 1 would exceed the server head, last_seq clamps to server_seq:
    state.last_seq can never advance beyond what the server actually published."""
    from osmsg.replication import changefile_download_urls

    cur_seq, cur_ts = changefile_repl

    _, _, _, last_seq, _, _ = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=10),
        end_date=cur_ts,  # +1 would overshoot, so clamp
        base_url="https://planet.openstreetmap.org/replication/minute",
    )
    assert last_seq == cur_seq


# cs_ts sync gate: keeps changefile from outpacing changeset replication on --update ticks.
# Without it, a tick can fetch changefile minute-diff N+1 before changeset minute-diff N+1
# is published, dropping (seq, cs) rows whose parent isn't in `changesets` yet (FK-orphan
# stubs that only resolve next tick once the changeset side catches up).


def _resume_seq_call(changefile_repl, **kwargs):
    from osmsg.replication import changefile_download_urls

    cur_seq, cur_ts = changefile_repl
    defaults = dict(
        start_date=cur_ts - dt.timedelta(minutes=10),
        end_date=cur_ts,
        base_url="https://planet.openstreetmap.org/replication/minute",
        resume_seq=cur_seq - 10,
    )
    defaults.update(kwargs)
    return changefile_download_urls(**defaults)


def test_cs_ts_no_cap_when_cs_ahead_of_end_date(changefile_repl):
    """cs_ts >= end_date: changeset replication already covers the requested window;
    no need to hold the changefile worker back."""
    _, cur_ts = changefile_repl
    with_gate = _resume_seq_call(changefile_repl, cs_ts=cur_ts + dt.timedelta(minutes=5))
    no_gate = _resume_seq_call(changefile_repl)
    assert with_gate[3] == no_gate[3]


def test_cs_ts_no_cap_when_cs_equals_end_date(changefile_repl):
    """Strict `>`: cs_ts == end_date means changesets cover the boundary diff already."""
    _, cur_ts = changefile_repl
    with_gate = _resume_seq_call(changefile_repl, cs_ts=cur_ts)
    no_gate = _resume_seq_call(changefile_repl)
    assert with_gate[3] == no_gate[3]


def test_cs_ts_caps_last_seq_when_end_date_ahead_of_cs(changefile_repl):
    """end_date > cs_ts: the changefile worker would emit (seq, cs) rows whose changesets
    aren't in `changesets` yet. Decrement by one so cf trails cs by at least a minute."""
    _, cur_ts = changefile_repl
    with_gate = _resume_seq_call(changefile_repl, cs_ts=cur_ts - dt.timedelta(minutes=5))
    no_gate = _resume_seq_call(changefile_repl)
    assert with_gate[3] == no_gate[3] - 1


def test_cs_ts_cap_only_fires_on_update_resume(changefile_repl):
    """resume_seq=None means one-shot/bootstrap: respect the user's --end exactly,
    don't hold the window back over an internal sync detail."""
    from osmsg.replication import changefile_download_urls

    _, cur_ts = changefile_repl
    with_cs_ts = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=10),
        end_date=cur_ts,
        base_url="https://planet.openstreetmap.org/replication/minute",
        cs_ts=cur_ts - dt.timedelta(minutes=5),
    )
    without_cs_ts = changefile_download_urls(
        start_date=cur_ts - dt.timedelta(minutes=10),
        end_date=cur_ts,
        base_url="https://planet.openstreetmap.org/replication/minute",
    )
    assert with_cs_ts[3] == without_cs_ts[3]


def test_changeset_seq_clamps_before_replication(monkeypatch):
    cr = ChangesetReplication()
    monkeypatch.setattr(cr, "_state", lambda: (7_000_000, dt.datetime(2026, 6, 25, tzinfo=dt.UTC)))
    assert cr.timestamp_to_sequence(dt.datetime(2005, 1, 1, tzinfo=dt.UTC)) == 1
    assert cr.timestamp_to_sequence(dt.datetime(2026, 6, 24, tzinfo=dt.UTC)) <= 7_000_000

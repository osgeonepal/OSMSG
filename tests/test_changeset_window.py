"""The changeset handler drops changesets closed at/before its window start. That window must track the
changeset stream's own resume position, not the (leading) changefile/minute position, or a changeset whose
element edits are already ingested has its metadata dropped and stays a NULL-metadata stub forever."""

import datetime as dt

from osmsg.handlers import ChangesetHandler

CLOSED = dt.datetime(2026, 8, 1, 15, 56, tzinfo=dt.UTC)


def _cfg(window_start: dt.datetime) -> dict:
    return {
        "changeset_meta": True,
        "hashtags": None,
        "exact_lookup": False,
        "whitelisted_users": None,
        "geom_filter_wkt": None,
        "window_start_utc": window_start,
    }


class _Bounds:
    def valid(self) -> bool:
        return False


class _Changeset:
    id = 100
    open = False
    closed_at = CLOSED
    created_at = CLOSED
    uid = 42
    user = "mapper"
    tags = {"hashtags": "#osgeonepal", "comment": ""}
    bounds = _Bounds()


def test_kept_when_window_at_changeset_stream_position():
    h = ChangesetHandler(_cfg(CLOSED - dt.timedelta(minutes=6)))
    h.changeset(_Changeset())
    assert 100 in h.changesets


def test_dropped_when_window_at_leading_minute_position():
    h = ChangesetHandler(_cfg(CLOSED + dt.timedelta(minutes=16)))
    h.changeset(_Changeset())
    assert 100 not in h.changesets

"""Load-shed and rate-limit keying for the API."""

import queue
import time
from types import SimpleNamespace

import duckdb
import pytest
from litestar.exceptions import HTTPException, TooManyRequestsException

from api import duck
from api.app import _client_identifier, app, rate_limit_config


class _FakeCon:
    def interrupt(self):
        pass

    def close(self):
        pass


def _stub_pool(monkeypatch):
    """Point _run at fake connections so it never touches DuckDB/Postgres."""
    monkeypatch.setattr(duck, "_sources", lambda: None)
    monkeypatch.setattr(duck, "_connect", lambda: _FakeCon())
    pool: queue.Queue = queue.Queue()
    pool.put(_FakeCon())
    monkeypatch.setattr(duck, "_pool_ready", lambda: pool)


def test_acquire_returns_free_connection():
    pool: queue.Queue = queue.Queue()
    sentinel = object()
    pool.put(sentinel)
    assert duck._acquire(pool) is sentinel


def test_acquire_sheds_with_429_when_pool_saturated(monkeypatch):
    monkeypatch.setattr(duck, "_POOL_WAIT", 0.05)
    with pytest.raises(TooManyRequestsException):
        duck._acquire(queue.Queue())


def _request(xff: str | None, peer: str | None = "10.0.0.1"):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    client = SimpleNamespace(host=peer) if peer is not None else None
    return SimpleNamespace(headers=headers, client=client)


def test_client_identifier_uses_last_forwarded_hop():
    # Caddy appends the real client last; a client-supplied first hop must not win.
    assert _client_identifier(_request("9.9.9.9, 203.0.113.7")) == "203.0.113.7"


def test_client_identifier_falls_back_to_peer():
    assert _client_identifier(_request(None, peer="198.51.100.4")) == "198.51.100.4"


def test_client_identifier_unknown_when_no_peer():
    assert _client_identifier(_request(None, peer=None)) == "unknown"


def test_rate_limit_middleware_is_wired():
    from litestar.middleware.rate_limit import RateLimitMiddleware

    assert [m.middleware for m in app.middleware] == [RateLimitMiddleware]
    assert rate_limit_config.rate_limit == ("minute", 120)
    assert rate_limit_config.identifier_for_request is _client_identifier


def _fake_query(name):
    def fn(*args, **kwargs):
        return None

    fn.__name__ = name
    return fn


def test_enqueue_warm_single_flight_dedups(monkeypatch):
    monkeypatch.setattr(duck, "_QUERY_CACHE_DIR", "/tmp/qc")
    submitted: list = []
    monkeypatch.setattr(duck._warm_pool, "submit", lambda *a: submitted.append(a))
    duck._warm_inflight.clear()
    summary = _fake_query("summary")
    allt = {"start": None, "end": None}
    duck._enqueue_warm(summary, "hotosm", allt)
    duck._enqueue_warm(summary, "hotosm", allt)  # same key -> dropped
    duck._enqueue_warm(summary, "osmnepal", allt)  # different key -> submitted
    assert len(submitted) == 2
    assert len(duck._warm_inflight) == 2


def test_enqueue_warm_noop_when_cache_disabled(monkeypatch):
    monkeypatch.setattr(duck, "_QUERY_CACHE_DIR", None)
    submitted: list = []
    monkeypatch.setattr(duck._warm_pool, "submit", lambda *a: submitted.append(a))
    duck._warm_inflight.clear()
    duck._enqueue_warm(_fake_query("summary"), "hotosm", {"start": None, "end": None})
    assert submitted == []
    assert len(duck._warm_inflight) == 0


def test_run_maps_interrupt_to_503(monkeypatch):
    monkeypatch.setattr(duck, "_QUERY_TIMEOUT", 0.0)  # watchdog fires immediately -> interrupted
    monkeypatch.setattr(duck, "_QUERY_CACHE_DIR", None)  # skip the warm side effect
    _stub_pool(monkeypatch)

    def boom(con, hashtag, sources, **kwargs):
        time.sleep(0.05)  # let the zero-timeout watchdog set `interrupted` before we raise
        raise duckdb.Error("interrupted")

    boom.__name__ = "summary"
    with pytest.raises(HTTPException) as ei:
        duck._run(boom, "hotosm", start=None, end=None)
    assert ei.value.status_code == 503


def test_run_reraises_real_db_error_not_as_busy(monkeypatch):
    monkeypatch.setattr(duck, "_QUERY_TIMEOUT", 100.0)  # watchdog never fires
    _stub_pool(monkeypatch)

    def boom(con, hashtag, sources, **kwargs):
        raise duckdb.Error("real failure")

    boom.__name__ = "summary"
    with pytest.raises(duckdb.Error):  # a genuine error is a 500, not masked as 503 busy
        duck._run(boom, "hotosm", start=None, end=None)

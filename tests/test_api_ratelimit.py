"""Load-shed and rate-limit keying for the API."""

import queue
from types import SimpleNamespace

import pytest
from litestar.exceptions import TooManyRequestsException

from api import duck
from api.app import _client_identifier, app, rate_limit_config


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

"""Shared `requests.Session` with retry policy + connect/read timeouts.

Every HTTP call in osmsg goes through this session so retry behaviour and
timeout defaults are consistent. Per-request `timeout=` still wins.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "osmsg"
DEFAULT_TIMEOUT = (30, 120)  # (connect, read) seconds


class _TimeoutSession(requests.Session):
    """Session that applies `DEFAULT_TIMEOUT` whenever the caller did not specify one."""

    def request(self, method, url, *args, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        return super().request(method, url, *args, **kwargs)


def make_session() -> requests.Session:
    """Fresh session with the standard timeout + retry policy (use when a flow needs its own cookie jar)."""
    s = _TimeoutSession()
    retry = Retry(
        total=10,
        connect=10,
        read=10,
        backoff_factor=1.0,
        backoff_max=120,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = USER_AGENT
    return s


session = make_session()

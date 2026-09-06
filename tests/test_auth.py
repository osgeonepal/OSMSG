"""CSRF parsing + retry-equipped session for the OSM/Geofabrik auth flow."""

import pytest
from requests.adapters import HTTPAdapter

from osmsg._http import make_session
from osmsg._http import session as shared_session
from osmsg.auth import _csrf
from osmsg.exceptions import GeofabrikAuthError


def test_csrf_parses_canonical_attribute_order():
    html = '<html><head><meta name="csrf-token" content="abc123"></head></html>'
    assert _csrf(html) == "abc123"


def test_csrf_parses_reversed_attribute_order():
    """The old regex was order-sensitive. html.parser is not."""
    html = '<html><head><meta content="xyz789" name="csrf-token"></head></html>'
    assert _csrf(html) == "xyz789"


def test_csrf_parses_with_extra_attributes():
    html = '<meta http-equiv="content-type" content="ignore-me"><meta data-x="1" name="csrf-token" content="real">'
    assert _csrf(html) == "real"


def test_csrf_raises_when_missing():
    with pytest.raises(GeofabrikAuthError):
        _csrf("<html><body>no token here</body></html>")


def _has_retry_adapter(s) -> bool:
    for prefix in ("http://", "https://"):
        adapter = s.adapters.get(prefix)
        if not isinstance(adapter, HTTPAdapter):
            return False
        # urllib3's Retry is stored on the adapter's max_retries attribute.
        if not adapter.max_retries.total or adapter.max_retries.total < 1:
            return False
    return True


def test_make_session_has_retry_adapter():
    s = make_session()
    assert _has_retry_adapter(s)
    assert s.headers.get("User-Agent") == "osmsg"


def test_shared_session_has_retry_adapter():
    """Regression: the auth flow used to instantiate a bare requests.Session() without retries."""
    assert _has_retry_adapter(shared_session)

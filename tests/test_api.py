from importlib import import_module

import pytest
from litestar import Litestar, get
from litestar.config.cors import CORSConfig
from litestar.testing import TestClient

from api import app as api_app
from api.app import get_cors_origins, health
from api.pg_schema import PG_SCHEMA as API_PG_SCHEMA
from osmsg.pg_schema import PG_SCHEMA as CLI_PG_SCHEMA


def test_pg_schema_in_sync():
    assert API_PG_SCHEMA == CLI_PG_SCHEMA


def test_v2_hashtags_split():
    from litestar.exceptions import HTTPException

    from api.routers.hashtag import _hashtags

    assert _hashtags("hotosm") == ["hotosm"]
    assert _hashtags("hotosm,osmnepal") == ["hotosm", "osmnepal"]
    assert _hashtags(" hotosm , osmnepal ,") == ["hotosm", "osmnepal"]  # trims + drops empties
    with pytest.raises(HTTPException):
        _hashtags(" , ")


def test_query_prefixes_normalization():
    from osmsg.query import _prefixes

    # single string and list both work; # optional; case-insensitive dedupe; order preserved
    assert _prefixes("hotosm") == [("#hotosm", "#hotosn")]
    assert _prefixes(["#HotOSM", "hotosm", "osmnepal"]) == [("#hotosm", "#hotosn"), ("#osmnepal", "#osmnepam")]
    with pytest.raises(ValueError, match="at least one"):
        _prefixes(["", "  "])


def test_v2_window_validation():
    from datetime import UTC, datetime

    from litestar.exceptions import HTTPException

    from api.routers.hashtag import _window

    a = datetime(2026, 7, 1, tzinfo=UTC)
    b = datetime(2026, 7, 10, tzinfo=UTC)
    assert _window(a, b) == (a, b)
    assert _window(None, None) == (None, None)
    assert _window(a, None) == (a, None)
    with pytest.raises(HTTPException):
        _window(b, a)  # inverted range is a client error


def test_global_window_resolution():
    from datetime import UTC, datetime, timedelta

    from litestar.exceptions import HTTPException

    from api.routers.global_stats import _resolve

    now = datetime.now(UTC)
    # window shortcut resolves to [now - window, now]
    s, e = _resolve("24h", None, None)
    assert timedelta(hours=23, minutes=59) < e - s < timedelta(hours=24, minutes=1)
    # explicit recent bounded window is accepted
    a, b = now - timedelta(days=7), now
    assert _resolve(None, a, b) == (a, b)
    # rejected: unknown window, all-time (no bounds), inverted, too-wide, too-old
    for bad in [
        lambda: _resolve("decade", None, None),
        lambda: _resolve(None, None, None),
        lambda: _resolve(None, b, a),
        lambda: _resolve(None, now - timedelta(days=45), now),
        lambda: _resolve(None, now - timedelta(days=200), now - timedelta(days=180)),
    ]:
        with pytest.raises(HTTPException):
            bad()


def test_frontend_dist_serves_spa(tmp_path, monkeypatch):
    """With FRONTEND_DIST set, the app serves the built frontend at / and its assets, and an API route
    still wins over the static catch-all."""
    app_module = import_module("api.app")
    (tmp_path / "index.html").write_text("<title>osmsg spa</title>")
    (tmp_path / "app.js").write_text("console.log('x')")
    monkeypatch.setattr(app_module, "FRONTEND_DIST", str(tmp_path))

    @get("/api/ping")
    async def ping() -> dict:
        return {"pong": True}

    test_app = Litestar(route_handlers=[*app_module._root_handlers(), ping])
    with TestClient(app=test_app) as client:
        assert "osmsg spa" in client.get("/").text
        assert client.get("/app.js").status_code == 200
        assert client.get("/api/ping").json() == {"pong": True}


def test_no_frontend_dist_falls_back_to_home(monkeypatch):
    app_module = import_module("api.app")
    monkeypatch.setattr(app_module, "FRONTEND_DIST", None)
    assert app_module._root_handlers() == [app_module.home]


def test_api_exposes_only_active_public_routes():
    paths = {route.path for route in api_app.routes}
    assert "/health" in paths
    assert "/api/v2/hashtag/{hashtag:str}/summary" in paths
    assert "/api/v2/hashtag/{hashtag:str}/leaderboard" in paths
    assert "/api/v2/hashtag/{hashtag:str}/tags" in paths
    assert "/api/v2/hashtag/{hashtag:str}/editors" in paths
    assert not any(p.startswith("/api/v1") for p in paths)  # v1 retired


def test_health_endpoint_returns_ok():
    with TestClient(Litestar(route_handlers=[health])) as client:
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["last_seq"] is None
    assert data["last_ts"] is None
    assert data["updated_at"] is None


def test_get_cors_origins_reads_comma_separated_env(monkeypatch):
    monkeypatch.setenv(
        "OSMSG_CORS_ORIGINS",
        "https://leaderboard.example.org, http://localhost:5500,  ",
    )

    assert get_cors_origins() == ["https://leaderboard.example.org", "http://localhost:5500"]


def test_get_cors_origins_defaults_include_public_frontends(monkeypatch):
    monkeypatch.delenv("OSMSG_CORS_ORIGINS", raising=False)

    origins = get_cors_origins()

    assert "https://osgeonepal.github.io" in origins
    assert "https://osmsg.osgeonepal.org" in origins


def test_app_cors_allows_whitelisted_browser_preflight(monkeypatch):
    monkeypatch.setenv("OSMSG_CORS_ORIGINS", "https://leaderboard.example.org")
    app = Litestar(
        route_handlers=[health],
        cors_config=CORSConfig(allow_origins=get_cors_origins(), allow_methods=["GET", "OPTIONS"], allow_headers=["*"]),
    )
    with TestClient(app) as client:
        response = client.options(
            "/health",
            headers={
                "Origin": "https://leaderboard.example.org",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://leaderboard.example.org"

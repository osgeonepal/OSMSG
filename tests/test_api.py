import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest
from litestar import Litestar
from litestar.config.cors import CORSConfig
from litestar.testing import TestClient

from api import app as api_app
from api.app import health
from api.db import close_pool, ensure_schema, open_pool
from api.pagination import PaginationParams, paginate_items
from api.pg_schema import PG_SCHEMA as API_PG_SCHEMA
from api.routers.v1 import normalize_hashtags, v1_router
from osmsg.export.psql import to_psql
from osmsg.pg_schema import PG_SCHEMA as CLI_PG_SCHEMA

v1_module = import_module("api.routers.v1")


def test_pg_schema_in_sync():
    assert API_PG_SCHEMA == CLI_PG_SCHEMA


def test_api_exposes_only_active_public_routes():
    paths = {route.path for route in api_app.routes}

    assert "/health" in paths
    assert "/api/v1/stats" in paths
    assert "/api/v1/hashtag-stats" in paths
    assert "/api/v1/editor-stats" in paths
    assert "/api/v1/map" in paths
    assert "/api/v1/hashtag-trends" not in paths
    assert "/api/v1/stats/summary" not in paths
    assert "/api/v1/stats/timeseries" not in paths


def test_health_endpoint_returns_ok():
    with TestClient(Litestar(route_handlers=[health])) as client:
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["last_seq"] is None
    assert data["last_ts"] is None
    assert data["updated_at"] is None


def test_normalize_hashtags_accepts_bare_or_prefixed_values():
    assert normalize_hashtags(["maproulette", "#HOTOSM", "  #roads  ", ""]) == [
        "#maproulette",
        "#HOTOSM",
        "#roads",
    ]


def test_normalize_hashtags_dedupes_case_insensitively():
    assert normalize_hashtags(["maproulette", "#MapRoulette", "#roads"]) == ["#maproulette", "#roads"]


def test_pagination_model_fetches_one_extra_row_for_next_page():
    params = PaginationParams(limit=2, offset=4)
    assert params.query_limit == 3

    page = paginate_items(["a", "b", "c"], params)

    assert page.items == ["a", "b"]
    assert page.limit == 2
    assert page.offset == 4
    assert page.total == 7


def test_app_cors_allows_browser_preflight():
    app = Litestar(
        route_handlers=[v1_router],
        cors_config=CORSConfig(allow_origins=["*"], allow_methods=["GET", "OPTIONS"], allow_headers=["*"]),
    )
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/stats",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"


def _v1_app(monkeypatch, **fakes):
    for name, fake in fakes.items():
        monkeypatch.setattr(v1_module, name, fake)
    return Litestar(route_handlers=[v1_router])


def _stats_app(monkeypatch, fake_fetch):
    return _v1_app(monkeypatch, fetch_user_stats=fake_fetch)


def test_user_stats_endpoint_returns_expected_response(monkeypatch):
    async def fake_fetch_user_stats(*, start, end, hashtag, tag_mode, limit, offset):
        assert start.isoformat() == "2026-05-01T00:00:00+00:00"
        assert end.isoformat() == "2026-05-02T00:00:00+00:00"
        assert hashtag == ["#mapathon", "#roads"]
        assert tag_mode == "keys"
        assert limit == 2
        assert offset == 0
        return [
            {
                "uid": 10,
                "name": "alice",
                "changesets": 2,
                "nodes_create": 40,
                "nodes_modify": 5,
                "nodes_delete": 0,
                "ways_create": 12,
                "ways_modify": 1,
                "ways_delete": 0,
                "rels_create": 0,
                "rels_modify": 0,
                "rels_delete": 0,
                "poi_create": 5,
                "poi_modify": 1,
                "map_changes": 58,
                "rank": 1,
                "hashtags": ["#mapathon", "#roads"],
                "tag_stats": {"building": {"c": 3, "m": 0}},
            }
        ]

    with TestClient(_stats_app(monkeypatch, fake_fetch_user_stats)) as client:
        response = client.get(
            "/api/v1/stats",
            params=[
                ("start", "2026-05-01T00:00:00Z"),
                ("end", "2026-05-02T00:00:00Z"),
                ("hashtag", "mapathon"),
                ("hashtag", "#roads"),
                ("limit", "1"),
            ],
        )

    assert response.status_code == 200
    assert response.json() == {
        "count": 1,
        "start": "2026-05-01T00:00:00Z",
        "end": "2026-05-02T00:00:00Z",
        "hashtag": ["#mapathon", "#roads"],
        "tags": True,
        "tag_mode": "keys",
        "limit": 1,
        "offset": 0,
        "pagination": {
            "items": [
                {
                    "uid": 10,
                    "name": "alice",
                    "changesets": 2,
                    "nodes_create": 40,
                    "nodes_modify": 5,
                    "nodes_delete": 0,
                    "ways_create": 12,
                    "ways_modify": 1,
                    "ways_delete": 0,
                    "rels_create": 0,
                    "rels_modify": 0,
                    "rels_delete": 0,
                    "poi_create": 5,
                    "poi_modify": 1,
                    "map_changes": 58,
                    "rank": 1,
                    "hashtags": ["#mapathon", "#roads"],
                    "tag_stats": {"building": {"c": 3, "m": 0, "len": None}},
                }
            ],
            "limit": 1,
            "offset": 0,
            "total": 1,
        },
        "users": [
            {
                "uid": 10,
                "name": "alice",
                "changesets": 2,
                "nodes_create": 40,
                "nodes_modify": 5,
                "nodes_delete": 0,
                "ways_create": 12,
                "ways_modify": 1,
                "ways_delete": 0,
                "rels_create": 0,
                "rels_modify": 0,
                "rels_delete": 0,
                "poi_create": 5,
                "poi_modify": 1,
                "map_changes": 58,
                "rank": 1,
                "hashtags": ["#mapathon", "#roads"],
                "tag_stats": {"building": {"c": 3, "m": 0, "len": None}},
            }
        ],
    }


def test_user_stats_endpoint_rejects_invalid_date_range(monkeypatch):
    async def fake_fetch_user_stats(**kwargs):
        raise AssertionError("fetch_user_stats should not be called")

    with TestClient(_stats_app(monkeypatch, fake_fetch_user_stats)) as client:
        response = client.get(
            "/api/v1/stats",
            params={"start": "2026-05-02T00:00:00Z", "end": "2026-05-01T00:00:00Z"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "start must be before end"


def test_user_stats_endpoint_tags_false_drops_tag_stats(monkeypatch):
    async def fake_fetch_user_stats(*, tag_mode, **_kwargs):
        assert tag_mode == "none"
        return [
            {
                "uid": 10,
                "name": "alice",
                "changesets": 1,
                "nodes_create": 0,
                "nodes_modify": 0,
                "nodes_delete": 0,
                "ways_create": 0,
                "ways_modify": 0,
                "ways_delete": 0,
                "rels_create": 0,
                "rels_modify": 0,
                "rels_delete": 0,
                "poi_create": 0,
                "poi_modify": 0,
                "map_changes": 0,
                "rank": 1,
                "hashtags": [],
                "tag_stats": None,
            }
        ]

    with TestClient(_stats_app(monkeypatch, fake_fetch_user_stats)) as client:
        response = client.get("/api/v1/stats", params={"tags": "false"})

    assert response.status_code == 200
    body = response.json()
    assert body["tags"] is False
    assert body["tag_mode"] == "none"
    assert body["users"][0]["tag_stats"] is None


def test_user_stats_endpoint_all_tag_values_are_opt_in(monkeypatch):
    async def fake_fetch_user_stats(*, tag_mode, **_kwargs):
        assert tag_mode == "all"
        return []

    with TestClient(_stats_app(monkeypatch, fake_fetch_user_stats)) as client:
        response = client.get("/api/v1/stats", params={"tag_mode": "all"})

    assert response.status_code == 200
    assert response.json()["tag_mode"] == "all"


def test_hashtag_stats_endpoint_returns_expected_response(monkeypatch):
    async def fake_fetch_hashtag_stats(*, start, end, hashtag, limit, offset):
        assert start.isoformat() == "2026-05-01T00:00:00+00:00"
        assert end.isoformat() == "2026-05-02T00:00:00+00:00"
        assert hashtag == ["#mapathon"]
        assert limit == 3
        assert offset == 0
        return [
            {"hashtag": "#mapathon", "changesets": 4, "users": 3, "map_changes": 100, "rank": 1},
            {"hashtag": "#roads", "changesets": 2, "users": 2, "map_changes": 25, "rank": 2},
        ]

    async def fake_fetch_hashtag_trends(*, start, end, interval, hashtag, limit, offset):
        assert start.isoformat() == "2026-05-01T00:00:00+00:00"
        assert end.isoformat() == "2026-05-02T00:00:00+00:00"
        assert interval == "day"
        assert hashtag == ["#mapathon"]
        assert limit == 3
        assert offset == 0
        return [
            {
                "period_start": datetime(2026, 5, 1, tzinfo=UTC),
                "hashtag": "#mapathon",
                "changesets": 4,
                "users": 3,
                "map_changes": 100,
            }
        ]

    with TestClient(
        _v1_app(
            monkeypatch,
            fetch_hashtag_stats=fake_fetch_hashtag_stats,
            fetch_hashtag_trends=fake_fetch_hashtag_trends,
        )
    ) as client:
        response = client.get(
            "/api/v1/hashtag-stats",
            params={
                "start": "2026-05-01T00:00:00Z",
                "end": "2026-05-02T00:00:00Z",
                "hashtag": "mapathon",
                "limit": "2",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "count": 2,
        "start": "2026-05-01T00:00:00Z",
        "end": "2026-05-02T00:00:00Z",
        "hashtag": ["#mapathon"],
        "interval": "day",
        "limit": 2,
        "offset": 0,
        "pagination": {
            "items": [
                {"hashtag": "#mapathon", "changesets": 4, "users": 3, "map_changes": 100, "rank": 1},
                {"hashtag": "#roads", "changesets": 2, "users": 2, "map_changes": 25, "rank": 2},
            ],
            "limit": 2,
            "offset": 0,
            "total": 2,
        },
        "hashtags": [
            {"hashtag": "#mapathon", "changesets": 4, "users": 3, "map_changes": 100, "rank": 1},
            {"hashtag": "#roads", "changesets": 2, "users": 2, "map_changes": 25, "rank": 2},
        ],
        "trends": [
            {
                "period_start": "2026-05-01T00:00:00Z",
                "hashtag": "#mapathon",
                "changesets": 4,
                "users": 3,
                "map_changes": 100,
            },
        ],
    }


def test_hashtag_stats_endpoint_rejects_invalid_interval(monkeypatch):
    async def fake_fetch_hashtag_trends(**kwargs):
        raise AssertionError("fetch_hashtag_trends should not be called")

    with TestClient(_v1_app(monkeypatch, fetch_hashtag_trends=fake_fetch_hashtag_trends)) as client:
        response = client.get("/api/v1/hashtag-stats", params={"interval": "hour"})

    assert response.status_code == 400
    assert response.json()["detail"] == "interval must be one of: day, week, month"


def test_editor_stats_endpoint_returns_expected_response(monkeypatch):
    async def fake_fetch_editor_stats(*, start, end, include_version, limit, offset):
        assert start is None
        assert end is None
        assert include_version is False
        assert limit == 3
        assert offset == 0
        return [
            {"editor": "iD 2.34.0", "changesets": 10, "users": 5, "map_changes": 500, "rank": 1},
            {"editor": "JOSM", "changesets": 4, "users": 2, "map_changes": 120, "rank": 2},
        ]

    with TestClient(_v1_app(monkeypatch, fetch_editor_stats=fake_fetch_editor_stats)) as client:
        response = client.get("/api/v1/editor-stats", params={"limit": "2"})

    assert response.status_code == 200
    assert response.json() == {
        "count": 2,
        "start": None,
        "end": None,
        "include_version": False,
        "limit": 2,
        "offset": 0,
        "pagination": {
            "items": [
                {"editor": "iD 2.34.0", "changesets": 10, "users": 5, "map_changes": 500, "rank": 1},
                {"editor": "JOSM", "changesets": 4, "users": 2, "map_changes": 120, "rank": 2},
            ],
            "limit": 2,
            "offset": 0,
            "total": 2,
        },
        "editors": [
            {"editor": "iD 2.34.0", "changesets": 10, "users": 5, "map_changes": 500, "rank": 1},
            {"editor": "JOSM", "changesets": 4, "users": 2, "map_changes": 120, "rank": 2},
        ],
    }


def test_editor_stats_endpoint_versions_are_opt_in(monkeypatch):
    async def fake_fetch_editor_stats(*, include_version, **_kwargs):
        assert include_version is True
        return []

    with TestClient(_v1_app(monkeypatch, fetch_editor_stats=fake_fetch_editor_stats)) as client:
        response = client.get("/api/v1/editor-stats", params={"include_version": "true"})

    assert response.status_code == 200
    assert response.json()["include_version"] is True


def test_map_endpoint_returns_geojson_feature_collection(monkeypatch):
    async def fake_fetch_map_changes(*, start, end, hashtag, limit, offset):
        assert start.isoformat() == "2026-05-01T00:00:00+00:00"
        assert end.isoformat() == "2026-05-02T00:00:00+00:00"
        assert hashtag == ["#mapathon"]
        assert limit == 2
        assert offset == 0
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [85.5, 27.5]},
                    "properties": {"changeset_id": 1, "name": "alice", "map_changes": 10},
                }
            ],
        }

    with TestClient(_v1_app(monkeypatch, fetch_map_changes=fake_fetch_map_changes)) as client:
        response = client.get(
            "/api/v1/map",
            params={
                "start": "2026-05-01T00:00:00Z",
                "end": "2026-05-02T00:00:00Z",
                "hashtag": "mapathon",
                "limit": "1",
            },
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["features"][0]["geometry"] == {"type": "Point", "coordinates": [85.5, 27.5]}
    assert response.json()["features"][0]["properties"] == {"changeset_id": 1, "name": "alice", "map_changes": 10}


def test_user_stats_sql_omits_tag_ctes_when_tags_false():
    from api.queries import _user_stats_sql

    sql_with = _user_stats_sql(filter_dates=False, filter_hashtags=False, tag_mode="all")
    sql_without = _user_stats_sql(filter_dates=False, filter_hashtags=False, tag_mode="none")
    assert "tag_per_user" in sql_with
    assert "COALESCE(tpu.tag_stats, '{}'::jsonb) AS tag_stats" in sql_with
    assert "tag_per_user" not in sql_without
    assert "NULL::jsonb AS tag_stats" in sql_without
    assert "user_hashtags" in sql_without


def test_user_stats_sql_aggregates_tag_keys_by_default():
    from api.queries import _user_stats_sql

    sql = _user_stats_sql(filter_dates=False, filter_hashtags=False, tag_mode="keys")
    assert "jsonb_object_agg" in sql
    assert "SUM(COALESCE((tv.value->>'c')::bigint, 0))" in sql
    assert "tag_val" not in sql


def test_hashtag_stats_sql_uses_array_overlap_and_lateral_unnest():
    from api.queries import _hashtag_stats_sql

    sql = _hashtag_stats_sql(filter_dates=True, filter_hashtags=True)
    assert "CROSS JOIN LATERAL UNNEST(cs.hashtags)" in sql
    assert "cs.hashtags && $3::TEXT[]" in sql
    assert "ht.hashtag = ANY($3::TEXT[])" in sql
    assert "LIMIT $4 OFFSET $5" in sql


def test_hashtag_trends_sql_uses_bounded_date_range():
    from api.queries import _hashtag_trends_sql

    sql = _hashtag_trends_sql(filter_hashtags=False)
    assert "DATE_TRUNC($3, cs.created_at)" in sql
    assert "cs.created_at >= $1" in sql
    assert "cs.created_at < $2" in sql
    assert "LIMIT $4 OFFSET $5" in sql


def test_hashtag_trends_sql_filters_unnested_hashtags_when_requested():
    from api.queries import _hashtag_trends_sql

    sql = _hashtag_trends_sql(filter_hashtags=True)
    assert "cs.hashtags && $3::TEXT[]" in sql
    assert "ht.hashtag = ANY($3::TEXT[])" in sql
    assert "DATE_TRUNC($4, cs.created_at)" in sql


def test_map_changes_sql_returns_geojson_geometry():
    from api.queries import _map_changes_sql

    sql = _map_changes_sql(
        filter_dates=True,
        filter_hashtags=True,
        geometry_mode="geom",
    )
    assert "ST_AsGeoJSON(ST_Centroid(cs.geom))::TEXT AS geometry" in sql
    assert "cs.hashtags && $3::TEXT[]" in sql
    assert " AS centroid" not in sql
    assert "LIMIT $4 OFFSET $5" in sql


def test_map_changes_sql_can_return_bbox_geojson_geometry():
    from api.queries import _map_changes_sql

    sql = _map_changes_sql(
        filter_dates=True,
        filter_hashtags=False,
        geometry_mode="bbox",
    )
    assert "'type', 'Point'" in sql
    assert "'type', 'Polygon'" not in sql
    assert "cs.min_lon, cs.min_lat, cs.max_lon, cs.max_lat" in sql
    assert "LIMIT $3 OFFSET $4" in sql


def test_editor_stats_sql_groups_blank_editors_as_unknown():
    from api.queries import _editor_stats_sql

    sql = _editor_stats_sql(filter_dates=False, include_version=False)
    assert "REGEXP_REPLACE" in sql
    assert "GROUP BY editor" in sql


def test_editor_stats_sql_can_include_versions():
    from api.queries import _editor_stats_sql

    sql = _editor_stats_sql(filter_dates=False, include_version=True)
    assert "COALESCE(NULLIF(cs.editor, ''), 'unknown') AS editor" in sql
    assert "REGEXP_REPLACE" not in sql


def _seed_pg_via_to_psql(fresh_db, populated_db_factory, dsn):
    populated = populated_db_factory(fresh_db)
    populated.execute(
        "UPDATE changeset_stats SET tag_stats = ?::JSON WHERE changeset_id = 1",
        [
            json.dumps(
                {
                    "building": {"yes": {"c": 5, "m": 1}, "house": {"c": 2, "m": 0}},
                    "highway": {"residential": {"c": 3, "m": 0, "len": 245.7}},
                }
            )
        ],
    )
    populated.execute(
        "UPDATE changeset_stats SET tag_stats = ?::JSON WHERE changeset_id = 2",
        [json.dumps({"natural": {"tree": {"c": 50, "m": 0}}})],
    )
    safe_dsn = dsn.replace("'", "''")
    import duckdb

    wiper = duckdb.connect(":memory:")
    wiper.execute("INSTALL postgres")
    wiper.execute("LOAD postgres")
    wiper.execute(f"ATTACH '{safe_dsn}' AS pg_w (TYPE postgres)")
    try:
        for table in ("changeset_stats", "changesets", "users", "state"):
            wiper.execute(f"CALL postgres_execute('pg_w', $$DELETE FROM {table}$$)")
    finally:
        wiper.execute("DETACH pg_w")
        wiper.close()
    to_psql(populated, dsn)


@asynccontextmanager
async def _api_lifespan(_app):
    await open_pool()
    await ensure_schema()
    try:
        yield
    finally:
        await close_pool()


def _live_api_app() -> Litestar:
    return Litestar(route_handlers=[health, v1_router], lifespan=[_api_lifespan])


@pytest.fixture
def live_api_client(monkeypatch, fresh_db, populated_db_factory):
    dsn = os.environ.get("OSMSG_PG_DSN")
    if not dsn:
        pytest.skip("OSMSG_PG_DSN not set; live API integration not exercised")
    pairs = [kv.strip() for kv in dsn.split() if "=" in kv]
    parts = dict(kv.split("=", 1) for kv in pairs)
    db_url = (
        f"postgresql://{parts.get('user', 'osmsg')}:{parts.get('password', 'osmsg')}"
        f"@{parts.get('host', 'localhost')}:{parts.get('port', '5432')}/{parts.get('dbname', 'osmsg')}"
    )
    monkeypatch.setenv("DATABASE_URL", db_url)

    _seed_pg_via_to_psql(fresh_db, populated_db_factory, dsn)
    with TestClient(_live_api_app()) as client:
        yield client


@pytest.mark.network
def test_live_api_stats_default_returns_dicts_not_strings(live_api_client):
    r = live_api_client.get("/api/v1/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] is True
    assert body["tag_mode"] == "keys"
    assert body["count"] == 2
    by_name = {u["name"]: u for u in body["users"]}
    assert isinstance(by_name["alice"]["tag_stats"], dict)
    assert by_name["alice"]["hashtags"] == ["#mapathon"]
    assert by_name["bob"]["hashtags"] == []
    assert by_name["alice"]["tag_stats"]["building"]["c"] == 7
    assert by_name["alice"]["tag_stats"]["building"]["m"] == 1
    assert by_name["alice"]["tag_stats"]["highway"]["len"] == 245.7
    assert by_name["bob"]["tag_stats"]["natural"]["c"] == 50


@pytest.mark.network
def test_live_api_stats_all_mode_returns_value_breakdown(live_api_client):
    body = live_api_client.get("/api/v1/stats", params={"tag_mode": "all"}).json()
    by_name = {u["name"]: u for u in body["users"]}
    assert by_name["alice"]["tag_stats"]["building"]["yes"]["c"] == 5
    assert by_name["alice"]["tag_stats"]["highway"]["residential"]["len"] == 245.7


@pytest.mark.network
def test_live_api_stats_tags_false_skips_tag_stats(live_api_client):
    r = live_api_client.get("/api/v1/stats", params={"tags": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] is False
    for u in body["users"]:
        assert u["tag_stats"] is None


@pytest.mark.network
def test_live_api_stats_user_totals_match_seed(live_api_client):
    r = live_api_client.get("/api/v1/stats")
    by_name = {u["name"]: u for u in r.json()["users"]}
    alice = by_name["alice"]
    bob = by_name["bob"]
    assert alice == {
        **alice,
        "changesets": 1,
        "nodes_create": 30,
        "ways_create": 8,
        "poi_create": 5,
        "map_changes": 44,
        "rank": 2,
    }
    assert bob == {
        **bob,
        "changesets": 1,
        "nodes_create": 50,
        "ways_create": 0,
        "poi_create": 50,
        "map_changes": 50,
        "rank": 1,
    }


@pytest.mark.network
def test_live_api_stats_hashtag_filters_to_matching_changesets(live_api_client):
    r = live_api_client.get("/api/v1/stats", params={"hashtag": "mapathon"})
    assert r.status_code == 200
    body = r.json()
    assert body["hashtag"] == ["#mapathon"]
    names = {u["name"] for u in body["users"]}
    assert names == {"alice"}
    assert body["users"][0]["hashtags"] == ["#mapathon"]


@pytest.mark.network
def test_live_api_stats_date_range_filters_changesets(live_api_client):
    r = live_api_client.get(
        "/api/v1/stats",
        params={"start": "2026-04-02T00:00:00Z", "end": "2026-04-03T00:00:00Z"},
    )
    assert r.status_code == 200
    body = r.json()
    names = {u["name"] for u in body["users"]}
    assert names == {"bob"}


@pytest.mark.network
def test_live_api_stats_pagination(live_api_client):
    page1 = live_api_client.get("/api/v1/stats", params={"limit": 1, "offset": 0}).json()
    page2 = live_api_client.get("/api/v1/stats", params={"limit": 1, "offset": 1}).json()
    assert page1["limit"] == 1 and page1["offset"] == 0
    assert page2["limit"] == 1 and page2["offset"] == 1
    assert len(page1["users"]) == 1
    assert len(page2["users"]) == 1
    assert page1["users"][0]["name"] != page2["users"][0]["name"]


@pytest.mark.network
def test_live_api_stats_limit_validation_rejects_zero(live_api_client):
    r = live_api_client.get("/api/v1/stats", params={"limit": 0})
    assert r.status_code == 400


@pytest.mark.network
def test_live_api_stats_limit_validation_rejects_too_large(live_api_client):
    r = live_api_client.get("/api/v1/stats", params={"limit": 1001})
    assert r.status_code == 400


@pytest.mark.network
def test_live_api_stats_offset_validation_rejects_negative(live_api_client):
    r = live_api_client.get("/api/v1/stats", params={"offset": -1})
    assert r.status_code == 400


@pytest.mark.network
def test_live_api_stats_response_echoes_query(live_api_client):
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = start + timedelta(days=2)
    r = live_api_client.get(
        "/api/v1/stats",
        params={
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "hashtag": "mapathon",
            "tags": "true",
            "limit": 10,
            "offset": 0,
        },
    )
    body = r.json()
    assert body["start"] == "2026-04-01T00:00:00Z"
    assert body["end"] == "2026-04-03T00:00:00Z"
    assert body["hashtag"] == ["#mapathon"]
    assert body["tags"] is True
    assert body["tag_mode"] == "keys"
    assert body["limit"] == 10
    assert body["offset"] == 0


@pytest.mark.network
def test_live_api_health_reports_seeded_state(live_api_client):
    r = live_api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


@pytest.mark.network
def test_live_api_returns_orphan_stats_when_no_filter(live_api_client):
    """Orphan stats (no parent changesets row) must surface when no filter is applied."""
    dsn = os.environ["OSMSG_PG_DSN"]
    safe_dsn = dsn.replace("'", "''")
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL postgres")
    conn.execute("LOAD postgres")
    conn.execute(f"ATTACH '{safe_dsn}' AS pg (TYPE postgres)")
    try:
        conn.execute(
            "CALL postgres_execute('pg', $$"
            "ALTER TABLE changeset_stats "
            "DROP CONSTRAINT IF EXISTS changeset_stats_changeset_id_fkey"
            "$$)"
        )
        conn.execute(
            "CALL postgres_execute('pg', $$"
            "INSERT INTO changeset_stats (changeset_id, seq_id, uid, nodes_created) "
            "VALUES (9999, 9999, 10, 7) ON CONFLICT DO NOTHING"
            "$$)"
        )
    finally:
        conn.execute("DETACH pg")
        conn.close()

    r = live_api_client.get("/api/v1/stats")
    assert r.status_code == 200
    by_name = {u["name"]: u for u in r.json()["users"]}
    assert by_name["alice"]["nodes_create"] == 30 + 7  # original 30 + orphan stub


@pytest.mark.network
def test_live_api_date_filter_with_no_matches_returns_empty_not_all(live_api_client):
    """Date filter with no matches must return empty, not silently fall back to all."""
    r = live_api_client.get(
        "/api/v1/stats",
        params={"start": "2099-01-01T00:00:00Z", "end": "2099-12-31T00:00:00Z"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["users"] == []


def test_user_stats_sql_no_filter_skips_changesets_join():
    """No-filter path must not JOIN the changesets table, orphan stats would be dropped."""
    from api.queries import _user_stats_sql

    sql = _user_stats_sql(filter_dates=False, filter_hashtags=False, tag_mode="none")
    assert "filtered_changesets" not in sql
    assert "JOIN filtered_changesets" not in sql
    assert "stats_scope AS (SELECT * FROM changeset_stats)" in sql
    assert "LEFT JOIN user_hashtags" in sql


def test_user_stats_sql_filtered_uses_changesets_join():
    """Filtered path must scope through changesets so date/hashtag predicates apply."""
    from api.queries import _user_stats_sql

    sql_dates = _user_stats_sql(filter_dates=True, filter_hashtags=False, tag_mode="none")
    sql_tags = _user_stats_sql(filter_dates=False, filter_hashtags=True, tag_mode="none")
    for sql in (sql_dates, sql_tags):
        assert "filtered_changesets" in sql
        assert "JOIN filtered_changesets" in sql


def test_user_stats_sql_no_unfiltered_fallback_remains():
    """The buggy 'fallback to all stats when matching is empty' branch must be gone."""
    from api.queries import _user_stats_sql

    for combo in [(True, True), (True, False), (False, True), (False, False)]:
        sql = _user_stats_sql(
            filter_dates=combo[0],
            filter_hashtags=combo[1],
            tag_mode="none",
        )
        assert "NOT EXISTS (SELECT 1 FROM matching_stats)" not in sql
        assert "enable_unfiltered_fallback" not in sql

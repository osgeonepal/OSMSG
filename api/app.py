import os
from contextlib import asynccontextmanager
from pathlib import Path

from litestar import Litestar, Request, get
from litestar.config.cors import CORSConfig
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import SwaggerRenderPlugin
from litestar.response import Redirect
from litestar.static_files import create_static_files_router

from .db import close_pool, ensure_schema, open_pool
from .queries import fetch_state
from .routers.global_stats import global_router
from .routers.hashtag import v2_router
from .schemas import HealthResponse

FRONTEND_DIST = os.getenv("FRONTEND_DIST")
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:5520",
    "http://127.0.0.1:5520",
    "https://osgeonepal.github.io",
    "https://osmsg.osgeonepal.org",
)


def get_cors_origins() -> list[str]:
    raw_origins = os.getenv("OSMSG_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or list(DEFAULT_CORS_ORIGINS)


def _client_identifier(request: Request) -> str:
    """Rate-limit key. Behind the reverse proxy the socket peer is the proxy, so key on the last
    X-Forwarded-For hop, the address Caddy appends, which a client-supplied header cannot spoof."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


# Generous per-IP cap: a NAT'd mapathon shares one address, so this only stops a single flooder; the
# DuckDB pool 429 in duck.py is the global load-shed.
_RATE_LIMIT_PER_MINUTE = int(os.getenv("OSMSG_RATE_LIMIT_PER_MINUTE", "120"))
rate_limit_config = RateLimitConfig(
    rate_limit=("minute", _RATE_LIMIT_PER_MINUTE),
    exclude=["/health", "/docs", "/schema"],
    identifier_for_request=_client_identifier,
)


@asynccontextmanager
async def lifespan(app: Litestar):
    await open_pool()
    if os.getenv("OSMSG_SKIP_SCHEMA_ENSURE", "").lower() not in {"1", "true", "yes"}:
        await ensure_schema()
    try:
        yield
    finally:
        await close_pool()


@get("/", include_in_schema=False)
async def home() -> Redirect:
    return Redirect(path="/docs")


def _root_handlers() -> list:
    """Serve the built frontend at / when FRONTEND_DIST is a directory, else redirect to the API docs.
    Explicit API routes (/health, /api/*, /docs) take precedence over the static catch-all; html_mode
    serves index.html."""
    if FRONTEND_DIST and Path(FRONTEND_DIST).is_dir():
        return [create_static_files_router(path="/", directories=[FRONTEND_DIST], html_mode=True)]
    return [home]


@get("/health")
async def health() -> HealthResponse:
    try:
        state = await fetch_state()
    except Exception:
        state = None
    return HealthResponse(
        status="ok",
        last_seq=state["last_seq"] if state else None,
        last_ts=state["last_ts"] if state else None,
        updated_at=state["updated_at"] if state else None,
    )


app = Litestar(
    route_handlers=[*_root_handlers(), health, v2_router, global_router],
    lifespan=[lifespan],
    middleware=[rate_limit_config.middleware],
    cors_config=CORSConfig(
        allow_origins=get_cors_origins(),
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    ),
    openapi_config=OpenAPIConfig(
        title="OSMSG API",
        version="1.0.0",
        path="/docs",
        render_plugins=[SwaggerRenderPlugin()],
    ),
)

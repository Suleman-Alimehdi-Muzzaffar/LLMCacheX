"""LLMCacheX local dashboard API (FastAPI).

Run from ``backend/``:

    python -m uvicorn api.main:app --reload

Interactive docs: http://localhost:8000/docs

Architecture: this API layer lives outside ``src/llmcachex`` so the core
library stays usable with zero dependencies. FastAPI/uvicorn are required
only to run this dashboard server.

CORS is configured for local Vite development origins only — this is
development configuration, not a production deployment setup.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from llmcachex.storage import RedisStorageError

from .routes import activity, analytics, cache, health, stats
from .services import (
    MetricsService,
    get_cache_db_path,
    get_cache_strategy,
    get_redis_prefix,
    get_redis_url,
)

# Local development origins (Vite dev server).
DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def create_app(
    cache_db: str | None = None,
    *,
    cache_strategy: str | None = None,
    redis_url: str | None = None,
    redis_prefix: str | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        cache_db: Path of the SQLite cache file to serve. Defaults to the
            ``LLMCACHEX_CACHE_DB`` environment variable (or
            ``llmcachex.db``). Under ``strategy="redis"`` this file holds
            metrics/analytics while cached values live in Redis.
        cache_strategy: ``"sqlite"`` or ``"redis"``. Defaults to the
            ``CACHE_STRATEGY`` environment variable (or ``"sqlite"``).
        redis_url: Redis URL for the ``"redis"`` strategy. Defaults to
            the ``REDIS_URL`` environment variable (or the local
            development URL). Never exposed through any endpoint.
        redis_prefix: Redis key namespace. Defaults to the
            ``REDIS_KEY_PREFIX`` environment variable.

    Returns:
        Configured FastAPI app instance.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The cache backend is opened when the server starts, not at
        # import time — importing this module must not create files.
        service = MetricsService(
            cache_db if cache_db is not None else get_cache_db_path(),
            strategy=(
                cache_strategy
                if cache_strategy is not None
                else get_cache_strategy()
            ),
            redis_url=(
                redis_url if redis_url is not None else get_redis_url()
            ),
            redis_prefix=(
                redis_prefix
                if redis_prefix is not None
                else get_redis_prefix()
            ),
        )
        app.state.service = service
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="LLMCacheX Dashboard API",
        version="0.1.0",
        description=(
            "Local developer API exposing real LLMCacheX cache/metrics "
            "state. No external providers, no API keys."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,  # development only
        allow_credentials=False,
        allow_methods=["GET", "DELETE"],
        allow_headers=["*"],
    )

    @app.exception_handler(sqlite3.Error)
    def sqlite_error_handler(
        request: Request, exc: sqlite3.Error
    ) -> JSONResponse:
        # Clean error surface: no filesystem paths or tracebacks leaked.
        # 503 (not 500): the backend storage is unavailable — matches the
        # documented contract in README/CHANGELOG.
        return JSONResponse(
            status_code=503,
            content={"detail": "Cache database error"},
        )

    @app.exception_handler(RedisStorageError)
    def redis_error_handler(
        request: Request, exc: RedisStorageError
    ) -> JSONResponse:
        # Clean error surface: no URLs, passwords or tracebacks leaked.
        return JSONResponse(
            status_code=503,
            content={"detail": "Cache backend error"},
        )

    app.include_router(health.router)
    app.include_router(stats.router)
    app.include_router(cache.router)
    app.include_router(activity.router)
    app.include_router(analytics.router)
    return app


app = create_app()

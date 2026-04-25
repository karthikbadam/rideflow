"""Query API — GET /zones/hot + /zones/surge + /drivers/idle + /rides/matches + /health."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Query, Request

from .config import Settings
from .redis_client import RedisClient


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    _configure_logging(settings.log_level)
    log = structlog.get_logger("query_api")
    app.state.settings = settings
    app.state.redis = RedisClient(settings)
    log.info("startup", redis=f"{settings.redis_host}:{settings.redis_port}")
    try:
        yield
    finally:
        await app.state.redis.close()
        log.info("shutdown")


app = FastAPI(title="rideflow-query-api", lifespan=lifespan)
log = structlog.get_logger("query_api")


@app.get("/health")
async def health(request: Request) -> dict:
    try:
        ok = await request.app.state.redis.ping()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"redis unreachable: {e}")
    return {"status": "ok" if ok else "degraded"}


@app.get("/zones/hot")
async def hot_zones(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    settings: Settings = request.app.state.settings
    effective = min(limit, settings.hot_zones_max_limit)
    result = await request.app.state.redis.top_hot_zones(effective)
    return {"limit": effective, **result}


@app.get("/zones/surge")
async def surge_zones(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    settings: Settings = request.app.state.settings
    effective = min(limit, settings.hot_zones_max_limit)
    result = await request.app.state.redis.top_surge_zones(effective)
    return {"limit": effective, **result}


@app.get("/drivers/idle")
async def idle_drivers(
    request: Request,
    limit: int = Query(default=10, ge=1, le=500),
) -> dict:
    result = await request.app.state.redis.idle_drivers(limit)
    return {"limit": limit, **result}


@app.get("/rides/matches")
async def recent_matches(
    request: Request,
    limit: int = Query(default=10, ge=1, le=500),
) -> dict:
    result = await request.app.state.redis.recent_matches(limit)
    return {"limit": limit, **result}


@app.get("/rides/matches/{ride_id}")
async def matches_for_ride(
    request: Request,
    ride_id: str,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    return await request.app.state.redis.matches_for_ride(ride_id, limit)


@app.get("/anomalies/recent")
async def recent_anomalies(
    request: Request,
    limit: int = Query(default=20, ge=1, le=500),
) -> dict:
    result = await request.app.state.redis.recent_anomalies(limit)
    return {"limit": limit, **result}


@app.get("/rides/enriched/{ride_id}")
async def enriched_ride(request: Request, ride_id: str) -> dict:
    data = await request.app.state.redis.enriched_ride(ride_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no enriched record for ride {ride_id}")
    return {"ride_id": ride_id, **data}

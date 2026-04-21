"""FastAPI ingest — POST /events/driver-location + GET /health."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Header, Request

from .config import Settings
from .h3util import cell_for
from .producers import EventProducer
from .schemas import DriverLocationPingedIn, RideRequestedIn


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
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
    log = structlog.get_logger("ingest_api")
    app.state.settings = settings
    app.state.producer = EventProducer(settings)
    log.info(
        "startup",
        bootstrap=settings.kafka_bootstrap_servers,
        location_topic=settings.location_topic,
        ride_request_topic=settings.ride_request_topic,
    )
    try:
        yield
    finally:
        pending = app.state.producer.flush(timeout=10.0)
        log.info("shutdown", undelivered=pending)


app = FastAPI(title="rideflow-ingest-api", lifespan=lifespan)
log = structlog.get_logger("ingest_api")


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or str(uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=trace_id, path=request.url.path)
    t0 = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request_failed")
        raise
    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info("request", method=request.method, status=response.status_code, duration_ms=duration_ms)
    response.headers["x-trace-id"] = trace_id
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/events/driver-location", status_code=202)
async def post_driver_location(
    req: DriverLocationPingedIn,
    request: Request,
    x_trace_id: str | None = Header(default=None),
) -> dict:
    trace_id = x_trace_id or str(uuid4())
    now_ms = int(time.time() * 1000)
    h3_cell = req.h3_cell or cell_for(req.lat, req.lon)
    event_id = str(uuid4())

    event = {
        "envelope": {
            "event_id": event_id,
            "event_type": "DriverLocationPinged",
            "event_version": 1,
            "occurred_at": req.occurred_at_ms or now_ms,
            "ingested_at": now_ms,
            "producer": request.app.state.settings.producer_name,
            "trace_id": trace_id,
        },
        "payload": {
            "driver_id": req.driver_id,
            "lat": req.lat,
            "lon": req.lon,
            "h3_cell": h3_cell,
            "heading_degrees": req.heading_degrees,
            "speed_mps": req.speed_mps,
            "accuracy_meters": req.accuracy_meters,
        },
    }

    request.app.state.producer.produce_location(key=req.driver_id, value=event)
    log.info("accepted", driver_id=req.driver_id, event_id=event_id, h3_cell=h3_cell)
    return {"accepted": True, "event_id": event_id, "trace_id": trace_id}


@app.post("/events/ride-request", status_code=202)
async def post_ride_request(
    req: RideRequestedIn,
    request: Request,
    x_trace_id: str | None = Header(default=None),
) -> dict:
    trace_id = x_trace_id or str(uuid4())
    now_ms = int(time.time() * 1000)
    pickup_cell = req.pickup.h3_cell or cell_for(req.pickup.lat, req.pickup.lon)
    dropoff_cell = req.dropoff.h3_cell or cell_for(req.dropoff.lat, req.dropoff.lon)
    event_id = str(uuid4())

    event = {
        "envelope": {
            "event_id": event_id,
            "event_type": "RideRequested",
            "event_version": 1,
            "occurred_at": req.occurred_at_ms or now_ms,
            "ingested_at": now_ms,
            "producer": request.app.state.settings.producer_name,
            "trace_id": trace_id,
        },
        "payload": {
            "ride_id": req.ride_id,
            "rider_id": req.rider_id,
            "pickup": {"lat": req.pickup.lat, "lon": req.pickup.lon, "h3_cell": pickup_cell},
            "dropoff": {"lat": req.dropoff.lat, "lon": req.dropoff.lon, "h3_cell": dropoff_cell},
            "product_tier": req.product_tier,
            "requested_at": req.requested_at_ms or now_ms,
        },
    }

    # Partition key = rider_id per docs/02 §4.2.
    request.app.state.producer.produce_ride_request(key=req.rider_id, value=event)
    log.info(
        "accepted",
        rider_id=req.rider_id,
        ride_id=req.ride_id,
        event_id=event_id,
        pickup_h3=pickup_cell,
    )
    return {"accepted": True, "event_id": event_id, "trace_id": trace_id}

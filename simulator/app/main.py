"""Paced async event loops — driver-locations and ride-requests to ingest-api."""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import time
from uuid import uuid4

import httpx
import structlog

from .city import make_drivers
from .config import Settings
from .events import build_payload, malform
from .rides import build_ride_payload


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


async def _post_one(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    log,
    stats: dict,
    ok_key: str = "accepted",
) -> None:
    headers = {"x-trace-id": str(uuid4())}
    try:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code == 202:
            stats[ok_key] += 1
        elif r.status_code == 422:
            stats["rejected"] += 1
        else:
            stats["error"] += 1
            log.warning("unexpected_status", status=r.status_code, body=r.text[:200])
    except Exception as e:
        stats["error"] += 1
        log.warning("post_failed", error=str(e))


async def locations_loop(
    settings: Settings,
    client: httpx.AsyncClient,
    log,
    stats: dict,
    stop: asyncio.Event,
    pending: set[asyncio.Task],
) -> None:
    rng = random.Random(settings.sim_seed)
    drivers = make_drivers(settings.sim_drivers, settings.sim_seed)
    endpoint = f"{settings.ingest_api_url.rstrip('/')}/events/driver-location"
    tick_interval = 1.0 / max(settings.sim_eps, 0.1)
    log.info("locations_loop_starting", eps=settings.sim_eps, endpoint=endpoint)

    while not stop.is_set():
        tick_start = time.monotonic()
        d = drivers[rng.randrange(len(drivers))]
        d.tick(rng)

        late = rng.random() < settings.sim_pct_late
        occurred = (
            int(time.time() * 1000) - rng.randint(1_000, settings.sim_late_max_s * 1000)
            if late
            else None
        )
        if rng.random() < settings.sim_pct_teleport:
            # Snap driver coords to ~teleport_km away before building the payload.
            # Stays within bbox via city.tick's clamp on the next step.
            import math as _m
            km_per_deg_lat = 111.0
            km_per_deg_lon = 111.0 * _m.cos(_m.radians(d.lat))
            bearing = rng.uniform(0, 2 * _m.pi)
            d.lat += settings.sim_teleport_km * _m.cos(bearing) / km_per_deg_lat
            d.lon += settings.sim_teleport_km * _m.sin(bearing) / km_per_deg_lon
            stats["teleport"] += 1
        payload = build_payload(d, occurred_at_ms=occurred)
        if late:
            stats["late"] += 1
        if rng.random() < settings.sim_pct_malformed:
            payload = malform(payload)
            stats["malformed"] += 1

        pending.add(asyncio.create_task(_post_one(client, endpoint, payload, log, stats)))

        if rng.random() < settings.sim_pct_dup:
            pending.add(asyncio.create_task(_post_one(client, endpoint, payload, log, stats)))
            stats["dup"] += 1

        elapsed = time.monotonic() - tick_start
        sleep_for = tick_interval - elapsed
        if sleep_for > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass


async def rides_loop(
    settings: Settings,
    client: httpx.AsyncClient,
    log,
    stats: dict,
    stop: asyncio.Event,
    pending: set[asyncio.Task],
) -> None:
    if settings.sim_rides_per_sec <= 0:
        log.info("rides_loop_disabled")
        return
    # Independent seed so ride sampling doesn't collide with locations RNG.
    rng = random.Random(settings.sim_seed + 1)
    endpoint = f"{settings.ingest_api_url.rstrip('/')}/events/ride-request"
    tick_interval = 1.0 / settings.sim_rides_per_sec
    log.info("rides_loop_starting", rps=settings.sim_rides_per_sec, endpoint=endpoint)

    while not stop.is_set():
        tick_start = time.monotonic()
        payload = build_ride_payload(rng, settings.sim_riders)
        pending.add(
            asyncio.create_task(
                _post_one(client, endpoint, payload, log, stats, ok_key="rides_accepted")
            )
        )

        elapsed = time.monotonic() - tick_start
        sleep_for = tick_interval - elapsed
        if sleep_for > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass


async def reaper_loop(
    log,
    stats: dict,
    stop: asyncio.Event,
    pending: set[asyncio.Task],
) -> None:
    last_report = time.monotonic()
    while not stop.is_set():
        done = {t for t in pending if t.done()}
        pending -= done
        now = time.monotonic()
        if now - last_report >= 10.0:
            log.info("stats", **stats)
            last_report = now
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def run() -> None:
    settings = Settings()
    _configure_logging(settings.log_level)
    log = structlog.get_logger("simulator")

    log.info(
        "simulator_starting",
        eps=settings.sim_eps,
        drivers=settings.sim_drivers,
        rides_per_sec=settings.sim_rides_per_sec,
        riders=settings.sim_riders,
        pct_late=settings.sim_pct_late,
        pct_dup=settings.sim_pct_dup,
        pct_malformed=settings.sim_pct_malformed,
    )

    stop = asyncio.Event()

    def _on_signal():
        log.info("shutdown_requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows

    limits = httpx.Limits(max_connections=settings.http_concurrency)
    timeout = httpx.Timeout(settings.http_timeout_s)
    stats = {
        "accepted": 0,
        "rejected": 0,
        "error": 0,
        "dup": 0,
        "late": 0,
        "malformed": 0,
        "teleport": 0,
        "rides_accepted": 0,
    }
    pending: set[asyncio.Task] = set()

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        await asyncio.gather(
            locations_loop(settings, client, log, stats, stop, pending),
            rides_loop(settings, client, log, stats, stop, pending),
            reaper_loop(log, stats, stop, pending),
        )

        if pending:
            log.info("draining", in_flight=len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("shutdown_complete", **stats)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

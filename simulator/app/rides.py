"""Ride request payload generation.

Pickups are sampled near the SF hotspots that drivers also cluster around
(so surge jobs have visible demand peaks). Dropoffs are sampled independently
across the bbox. ride_id is a fresh UUID per request.
"""
from __future__ import annotations

import random
import time
from uuid import uuid4

import h3

from .city import HOTSPOTS, LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, _weighted_hotspot

H3_RES = 9
TIERS = ("economy", "comfort", "premium")
TIER_WEIGHTS = (0.7, 0.22, 0.08)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _near_hotspot(rng: random.Random, jitter: float = 0.008) -> tuple[float, float]:
    lat, lon = _weighted_hotspot(rng)
    return (
        _clamp(lat + rng.uniform(-jitter, jitter), LAT_MIN, LAT_MAX),
        _clamp(lon + rng.uniform(-jitter, jitter), LON_MIN, LON_MAX),
    )


def _anywhere(rng: random.Random) -> tuple[float, float]:
    return (rng.uniform(LAT_MIN, LAT_MAX), rng.uniform(LON_MIN, LON_MAX))


def build_ride_payload(rng: random.Random, riders: int) -> dict:
    pickup_lat, pickup_lon = _near_hotspot(rng)
    dropoff_lat, dropoff_lon = _anywhere(rng)
    tier = rng.choices(TIERS, TIER_WEIGHTS, k=1)[0]
    rider_idx = rng.randrange(riders)
    now_ms = int(time.time() * 1000)
    return {
        "ride_id": str(uuid4()),
        "rider_id": f"rdr-{rider_idx:05d}",
        "pickup": {
            "lat": pickup_lat,
            "lon": pickup_lon,
            "h3_cell": h3.geo_to_h3(pickup_lat, pickup_lon, H3_RES),
        },
        "dropoff": {
            "lat": dropoff_lat,
            "lon": dropoff_lon,
            "h3_cell": h3.geo_to_h3(dropoff_lat, dropoff_lon, H3_RES),
        },
        "product_tier": tier,
        "requested_at_ms": now_ms,
        "occurred_at_ms": now_ms,
    }


__all__ = ["build_ride_payload", "HOTSPOTS"]

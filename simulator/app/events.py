"""Event payload construction — matches DriverLocationPingedIn on ingest."""
from __future__ import annotations

import math
import time

import h3

from .city import ACCURACY_METERS, Driver

H3_RES = 9


def build_payload(d: Driver, *, occurred_at_ms: int | None = None) -> dict:
    heading_deg = (math.degrees(d.heading_rad) + 360.0) % 360.0
    return {
        "driver_id": d.driver_id,
        "lat": d.lat,
        "lon": d.lon,
        "h3_cell": h3.geo_to_h3(d.lat, d.lon, H3_RES),
        "heading_degrees": heading_deg,
        "speed_mps": d.speed_mps,
        "accuracy_meters": ACCURACY_METERS,
        "occurred_at_ms": occurred_at_ms if occurred_at_ms is not None else int(time.time() * 1000),
    }


def malform(payload: dict) -> dict:
    """Corrupt a payload deterministically — used by fault-injection (E-X2)."""
    bad = dict(payload)
    bad.pop("lat", None)   # missing required field -> 422
    return bad
